"""Tests for walking the roots and flagging dormant repos."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest
from conftest import git

from cboard2.config import DEFAULT_MAX_DEPTH, Config
from cboard2.discovery import (
    Repo,
    discover,
    load_repos,
    repo_cache_path,
    save_repos,
)

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import RepoFactory

START = 1_800_000_000.0
TTL = 30.0
"""The window a board gives the stored list, matching board.RESCAN_INTERVAL."""


def _config(
    tree: Path,
    *,
    dormant: tuple[Path, ...] = (),
    max_depth: int = 4,
    worktrees: bool = True,
) -> Config:
    return Config(
        roots=(tree,),
        max_depth=max_depth,
        dormant=dormant,
        dormant_interval=4 * 3600.0,
        remote=False,
        remote_interval=300.0,
        origin_colors=True,
        worktrees=worktrees,
        worktree_limit=5,
    )


def test_finds_outer_repos_and_skips_nested(tree: Path, make_repo: RepoFactory) -> None:
    alpha = make_repo("alpha")
    beta = make_repo("nested/beta")
    make_repo("alpha/vendor/inner")
    (tree / "plain/subdir").mkdir(parents=True)

    found = discover(_config(tree))

    assert [repo.path for repo in found] == sorted([alpha, beta])
    assert [repo.name for repo in found] == sorted(["alpha", "beta"])


def test_submodule_gitfile_counts_as_a_repo(tree: Path, make_repo: RepoFactory) -> None:
    sub = make_repo("sub", gitfile=True)

    assert [repo.path for repo in discover(_config(tree))] == [sub]


def test_dormant_matches_the_repo_root(tree: Path, make_repo: RepoFactory) -> None:
    old = make_repo("old")
    live = make_repo("live")

    found = {
        repo.path: repo.dormant for repo in discover(_config(tree, dormant=(old,)))
    }

    assert found == {old: True, live: False}


def test_dormant_parent_flags_every_repo_under_it(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    shelf = tree / "shelf"
    first = make_repo("shelf/first")
    second = make_repo("shelf/second")
    live = make_repo("live")

    found = {
        repo.path: repo.dormant for repo in discover(_config(tree, dormant=(shelf,)))
    }

    assert found == {first: True, second: True, live: False}


def test_the_default_depth_finds_only_direct_children(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    alpha = make_repo("alpha")
    make_repo("shelf/beta")

    found = discover(_config(tree, max_depth=DEFAULT_MAX_DEPTH))

    assert [repo.path for repo in found] == [alpha]


def test_depth_two_reaches_a_nested_layout(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    make_repo("alpha")
    beta = make_repo("shelf/beta")

    found = discover(_config(tree, max_depth=2))

    assert beta in [repo.path for repo in found]


def test_max_depth_stops_the_walk(tree: Path, make_repo: RepoFactory) -> None:
    shallow = make_repo("shallow")
    make_repo("a/b/c/deep")

    assert [repo.path for repo in discover(_config(tree, max_depth=1))] == [shallow]


def test_overlapping_roots_return_each_repo_once(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    repo = make_repo("nested/alpha")
    config = Config(
        roots=(tree, tree / "nested"),
        max_depth=4,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=False,
        remote_interval=300.0,
        origin_colors=True,
        worktrees=True,
        worktree_limit=5,
    )

    assert [found.path for found in discover(config)] == [repo]


def test_dot_directories_are_not_walked(tree: Path, make_repo: RepoFactory) -> None:
    make_repo(".cache/hidden")
    visible = make_repo("visible")

    assert [repo.path for repo in discover(_config(tree))] == [visible]


def test_a_worktree_names_the_repo_it_belongs_to(
    tree: Path,
    git_repo: Path,
    worktree: Path,
) -> None:
    found = {repo.path: repo for repo in discover(_config(tree))}

    assert found[worktree].main_git_dir == git_repo / ".git"
    assert found[git_repo].main_git_dir is None
    assert found[worktree].family == found[git_repo].family


def test_a_submodule_is_not_a_worktree(tree: Path, make_repo: RepoFactory) -> None:
    make_repo("sub", gitfile=True)

    (found,) = discover(_config(tree))

    assert found.main_git_dir is None
    assert found.family == found.path / ".git"


def test_a_worktree_inside_its_repo_is_found(tree: Path, git_repo: Path) -> None:
    inner = git_repo / ".worktrees" / "inner"
    git(git_repo, "worktree", "add", "-q", "-b", "inner", str(inner))

    found = discover(_config(tree))

    assert [repo.path for repo in found] == [git_repo, inner]
    assert found[1].main_git_dir == git_repo / ".git"


def test_worktrees_off_leaves_the_walk_alone(tree: Path, git_repo: Path) -> None:
    inner = git_repo / ".worktrees" / "inner"
    git(git_repo, "worktree", "add", "-q", "-b", "inner", str(inner))

    found = discover(_config(tree, worktrees=False))

    assert [repo.path for repo in found] == [git_repo]


def test_a_deleted_worktree_directory_is_dropped(tree: Path, git_repo: Path) -> None:
    inner = git_repo / ".worktrees" / "gone"
    git(git_repo, "worktree", "add", "-q", "-b", "gone", str(inner))
    shutil.rmtree(inner)

    assert [repo.path for repo in discover(_config(tree))] == [git_repo]


def test_a_dormant_root_covers_its_worktrees(
    tree: Path,
    git_repo: Path,
    worktree: Path,
) -> None:
    found = {
        repo.path: repo.dormant for repo in discover(_config(tree, dormant=(tree,)))
    }

    assert found == {git_repo: True, worktree: True}


def test_a_dormant_entry_does_not_flag_a_name_it_prefixes(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    shelf = tree / "shelf"
    old = make_repo("shelf/old")
    live = make_repo("shelfed")

    found = {
        repo.path: repo.dormant for repo in discover(_config(tree, dormant=(shelf,)))
    }

    assert found == {old: True, live: False}


def test_a_dormant_entry_reaches_a_deeply_nested_repo(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    shelf = tree / "shelf"
    deep = make_repo("shelf/one/two/three")

    (found,) = discover(_config(tree, dormant=(shelf,)))

    assert (found.path, found.dormant) == (deep, True)


def test_the_stored_list_round_trips(
    tree: Path, git_repo: Path, worktree: Path
) -> None:
    config = _config(tree)
    target = tree / "store" / "repos.json"
    walked = discover(config)

    assert save_repos(target, config, walked, START) is True
    assert load_repos(target, config, START + 1, TTL) == walked
    assert [repo.path for repo in walked] == [git_repo, worktree]


def test_the_stored_list_expires(tree: Path, make_repo: RepoFactory) -> None:
    make_repo("alpha")
    config = _config(tree)
    target = tree / "store" / "repos.json"
    save_repos(target, config, discover(config), START)

    assert load_repos(target, config, START + TTL, TTL) is None


def test_a_stored_list_stamped_ahead_of_now_is_refused(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    """A clock that went backwards must not pin the list until it catches up."""
    make_repo("alpha")
    config = _config(tree)
    target = tree / "store" / "repos.json"
    save_repos(target, config, discover(config), START)

    assert load_repos(target, config, START - 1, TTL) is None


def test_a_different_watch_list_ignores_the_stored_list(
    tree: Path,
    make_repo: RepoFactory,
) -> None:
    make_repo("shelf/alpha")
    config = _config(tree)
    target = tree / "store" / "repos.json"
    save_repos(target, config, discover(config), START)

    assert load_repos(target, _config(tree, max_depth=9), START, TTL) is None
    assert load_repos(target, _config(tree, dormant=(tree,)), START, TTL) is None
    assert load_repos(target, _config(tree, worktrees=False), START, TTL) is None


@pytest.mark.parametrize(
    "written",
    [
        "",
        "{ truncated",
        "[]",
        '{"version": 0, "scanned_at": 1.0, "repos": []}',
        '{"version": 1, "repos": []}',
    ],
    ids=["empty", "truncated", "not-an-object", "wrong-version", "no-timestamp"],
)
def test_an_unusable_stored_list_reads_as_cold(tree: Path, written: str) -> None:
    target = tree / "repos.json"
    target.write_text(written, encoding="utf-8")

    assert load_repos(target, _config(tree), START, TTL) is None


def test_a_missing_stored_list_reads_as_cold(tree: Path) -> None:
    assert load_repos(tree / "absent.json", _config(tree), START, TTL) is None


def test_one_unusable_entry_discards_the_whole_stored_list(tree: Path) -> None:
    config = _config(tree)
    target = tree / "store" / "repos.json"
    save_repos(target, config, [Repo(path=tree / "a", name="a", dormant=False)], START)
    body = json.loads(target.read_text(encoding="utf-8"))
    body["repos"].append({"path": str(tree / "b"), "name": "b"})
    target.write_text(json.dumps(body), encoding="utf-8")

    assert load_repos(target, config, START, TTL) is None


def test_an_unwritable_directory_reports_the_write_failed(tree: Path) -> None:
    blocked = tree / "blocked"
    blocked.write_text("a file, not a directory\n", encoding="utf-8")

    assert save_repos(blocked / "repos.json", _config(tree), [], START) is False


def test_the_stored_list_path_follows_the_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CBOARD2_REPO_CACHE", str(tmp_path / "named.json"))

    assert repo_cache_path() == tmp_path / "named.json"

    monkeypatch.delenv("CBOARD2_REPO_CACHE")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert repo_cache_path() == tmp_path / "cboard2" / "repos.json"
