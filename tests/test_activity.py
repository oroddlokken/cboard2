"""Tests for reading recent activity out of the reflog."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from conftest import RecordingRunner, git

from cboard2 import activity
from cboard2.activity import (
    ActivityReader,
    branches,
    git_dir,
    last_fetch,
    log_mtime,
    read_reflog,
    split_message,
)
from cboard2.discovery import Repo

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_REFLOG = """\
HEAD@{1788524131}\tcheckout: moving from main to other\t2ddf989
HEAD@{1788524100}\tcommit: Add the thing\t2ddf989
HEAD@{1788524073}\tcommit (initial): init\te4ad5cf
"""


def _repo(path: Path) -> Repo:
    return Repo(path=path, name=path.name, dormant=False)


def test_reads_commit_then_checkout_newest_first(git_repo: Path) -> None:
    git(git_repo, "checkout", "-q", "-b", "other")

    entries = read_reflog(_repo(git_repo))

    assert [entry.verb for entry in entries] == ["checkout", "commit"]
    assert entries[0].at >= entries[1].at
    assert entries[0].at > 1_700_000_000
    assert entries[0].detail == "moving from main to other"
    assert entries[0].repo_name == git_repo.name


def test_initial_commit_normalises_to_commit(git_repo: Path) -> None:
    entries = read_reflog(_repo(git_repo))

    assert [entry.verb for entry in entries] == ["commit"]
    assert entries[0].detail == "Add tracked file"
    assert entries[0].sha


def test_repo_without_a_reflog_yields_nothing(git_repo: Path) -> None:
    shutil.rmtree(git_repo / ".git" / "logs")

    assert read_reflog(_repo(git_repo)) == []
    assert log_mtime(git_repo) is None
    assert ActivityReader().entries(_repo(git_repo)) == []


def test_unchanged_log_mtime_spawns_no_git_call(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repo = _repo(git_repo)
    first = reader.entries(repo)
    runner.calls.clear()

    second = reader.entries(repo)

    assert runner.calls == []
    assert second == first
    assert [entry.verb for entry in second] == ["checkout", "commit", "commit"]


def test_a_head_movement_triggers_a_re_read(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repo = _repo(git_repo)
    reader.entries(repo)
    runner.calls.clear()

    git(git_repo, "checkout", "-q", "-b", "other")

    reader.entries(repo)

    assert runner.paths_for("reflog") == [git_repo]


def test_prime_fills_the_cache_so_entries_spawns_no_git_call(
    git_repo: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repos = [_repo(git_repo), _repo(other)]

    reader.prime(repos)
    read_during_prime = runner.paths_for("reflog")
    runner.calls.clear()

    assert sorted(read_during_prime) == sorted([git_repo, other])
    assert [reader.latest(repo) for repo in repos] == [1788524131.0, 1788524131.0]
    assert runner.calls == []


def test_prime_skips_a_repo_whose_reflog_has_not_moved(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repos = [_repo(git_repo)]
    reader.prime(repos)
    runner.calls.clear()

    reader.prime(repos)

    assert runner.calls == []


def test_prime_ignores_a_repo_without_a_reflog(git_repo: Path) -> None:
    shutil.rmtree(git_repo / ".git" / "logs")
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)

    reader.prime([_repo(git_repo)])

    assert runner.calls == []


def test_latest_is_the_newest_entry_time(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)

    assert reader.latest(_repo(git_repo)) == 1788524131.0


def test_feed_merges_repos_newest_first(git_repo: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    shutil.copytree(git_repo, other)
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repos = [_repo(git_repo), _repo(other)]

    feed = reader.feed(repos)

    assert len(feed) == 6
    assert [entry.at for entry in feed] == sorted(
        (entry.at for entry in feed),
        reverse=True,
    )


def test_feed_honors_since_and_limit(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repos = [_repo(git_repo)]

    assert len(reader.feed(repos, since=1788524100.0)) == 2
    assert len(reader.feed(repos, limit=1)) == 1


def test_forget_absent_drops_the_cache(git_repo: Path) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repo = _repo(git_repo)
    reader.entries(repo)
    reader.forget_absent([])
    runner.calls.clear()

    reader.entries(repo)

    assert runner.paths_for("reflog") == [git_repo]


def test_entries_survives_the_cache_dropping_mid_call(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repo = _repo(git_repo)
    cached = reader.entries(repo)
    real = activity.log_mtime

    class DroppingMtime(float):
        """An mtime that drops the cache when compared, as the poll thread can."""

        def __eq__(self, other: object) -> bool:
            reader.forget_absent([])
            return isinstance(other, float) and float(self) == other

        __hash__ = float.__hash__

    def drop_while_comparing(root: Path) -> float | None:
        mtime = real(root)
        return None if mtime is None else DroppingMtime(mtime)

    monkeypatch.setattr(activity, "log_mtime", drop_while_comparing)

    assert cached
    assert reader.entries(repo) == cached


def test_entries_returns_empty_when_the_repo_is_forgotten_mid_call(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = RecordingRunner({"reflog": _REFLOG})
    reader = ActivityReader(runner=runner)
    repo = _repo(git_repo)
    reader.entries(repo)
    runner.outputs["reflog"] = ""
    real = activity.log_mtime

    def drop_then_stat(root: Path) -> float | None:
        reader.forget_absent([])
        return real(root)

    monkeypatch.setattr(activity, "log_mtime", drop_then_stat)

    assert reader.entries(repo) == []


def test_branches_lists_local_heads_newest_first(git_repo: Path) -> None:
    git(git_repo, "branch", "later")

    found = branches(git_repo)

    assert {branch.name for branch in found} == {"main", "later"}
    assert found[0].committed_at >= found[-1].committed_at
    assert found[0].subject == "Add tracked file"


def test_last_fetch_reads_fetch_head_mtime(git_repo: Path) -> None:
    assert last_fetch(git_repo) is None

    (git_repo / ".git" / "FETCH_HEAD").write_text("", encoding="utf-8")

    assert last_fetch(git_repo) is not None


def test_git_dir_follows_a_gitdir_pointer(tmp_path: Path) -> None:
    real = tmp_path / "store"
    real.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")

    assert git_dir(work) == real


def test_git_dir_resolves_a_relative_pointer(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: ../store/modules/sub\n", encoding="utf-8")

    assert git_dir(work) == work / "../store/modules/sub"


def test_split_message_takes_the_first_word() -> None:
    assert split_message("commit (initial): init") == ("commit", "init")
    assert split_message("commit (amend): redo") == ("commit", "redo")
    assert split_message("pull --rebase: Fast-forward") == ("pull", "Fast-forward")
    assert split_message("clone: from git@example.invalid") == (
        "clone",
        "from git@example.invalid",
    )
    assert split_message("rebase (finish): returning") == ("rebase", "returning")
    assert split_message("reset") == ("reset", "")
    assert split_message("Branch: renamed refs/heads/a to refs/heads/b") == (
        "branch",
        "renamed refs/heads/a to refs/heads/b",
    )
    assert split_message("") == (None, "")
