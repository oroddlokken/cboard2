"""Tests for walking the roots and flagging dormant repos."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cboard2.config import DEFAULT_MAX_DEPTH, Config
from cboard2.discovery import discover

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import RepoFactory


def _config(
    tree: Path,
    *,
    dormant: tuple[Path, ...] = (),
    max_depth: int = 4,
) -> Config:
    return Config(
        roots=(tree,),
        max_depth=max_depth,
        dormant=dormant,
        dormant_interval=4 * 3600.0,
        remote=False,
        remote_interval=300.0,
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
    )

    assert [found.path for found in discover(config)] == [repo]


def test_dot_directories_are_not_walked(tree: Path, make_repo: RepoFactory) -> None:
    make_repo(".cache/hidden")
    visible = make_repo("visible")

    assert [repo.path for repo in discover(_config(tree))] == [visible]
