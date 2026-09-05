"""Tests for polling git state and holding dormant repos to their interval."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from conftest import RecordingRunner, git

from cboard2 import gitstate
from cboard2.discovery import Repo
from cboard2.gitstate import Poller, parse_porcelain_v2

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

INTERVAL = 4 * 3600.0

_STATUS_WITH_UPSTREAM = """\
# branch.oid 1111111111111111111111111111111111111111
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -1
1 .M N... 100644 100644 100644 aaa bbb tracked.txt
1 M. N... 100644 100644 100644 ccc ddd staged.txt
2 R. N... 100644 100644 100644 eee fff R100 new.txt\told.txt
u UU N... 100644 100644 100644 100644 ggg hhh iii conflict.txt
? untracked.txt
"""


def _repo(path: Path, *, dormant: bool = False) -> Repo:
    return Repo(path=path, name=path.name, dormant=dormant)


def _worktree(path: Path, main: Path) -> Repo:
    return Repo(
        path=path,
        name=path.name,
        dormant=False,
        main_git_dir=main / ".git",
    )


def test_counts_staged_unstaged_and_untracked(git_repo: Path) -> None:
    (git_repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (git_repo / "staged.txt").write_text("new\n", encoding="utf-8")
    git(git_repo, "add", "staged.txt")
    (git_repo / "untracked.txt").write_text("loose\n", encoding="utf-8")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.readable
    assert (state.staged, state.unstaged, state.untracked) == (1, 1, 1)
    assert state.dirty == 3
    assert state.branch == "main"
    assert state.head_subject == "Add tracked file"
    assert state.head_time is not None


def test_clean_repo_reports_no_changes(git_repo: Path) -> None:
    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty == 0
    assert state.head_sha is not None
    assert state.upstream is None
    assert (state.ahead, state.behind) == (0, 0)


def test_detached_head_has_no_branch(git_repo: Path) -> None:
    git(git_repo, "checkout", "-q", "--detach")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.detached
    assert state.branch is None


def test_repo_without_commits_is_readable(empty_git_repo: Path) -> None:
    state = Poller(INTERVAL).poll([_repo(empty_git_repo)])[0]

    assert state.readable
    assert state.head_sha is None
    assert state.head_subject is None
    assert state.head_time is None
    assert state.branch == "main"


def test_unreadable_repo_keeps_its_row(tmp_path: Path) -> None:
    states = Poller(INTERVAL).poll([_repo(tmp_path / "not-a-repo")])

    assert len(states) == 1
    assert not states[0].readable
    assert states[0].name == "not-a-repo"


def test_every_git_call_passes_no_optional_locks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def fake_run(
        cmd: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, "", "")

    monkeypatch.setattr(gitstate.subprocess, "run", fake_run)

    Poller(INTERVAL).poll([_repo(tmp_path)])

    assert commands
    assert all(cmd[:2] == ["git", "--no-optional-locks"] for cmd in commands)


def test_first_tick_polls_dormant_and_normal_alike(tmp_path: Path) -> None:
    runner = RecordingRunner({"status": ""})
    poller = Poller(INTERVAL, runner=runner)
    repos = [_repo(tmp_path / "live"), _repo(tmp_path / "old", dormant=True)]

    states = poller.poll(repos, now=1000.0)

    assert runner.paths_for("status") == [repo.path for repo in repos]
    assert len(states) == 2


def test_dormant_repo_is_skipped_on_the_next_tick(tmp_path: Path) -> None:
    runner = RecordingRunner({"status": ""})
    poller = Poller(INTERVAL, runner=runner)
    live = _repo(tmp_path / "live")
    old = _repo(tmp_path / "old", dormant=True)
    poller.poll([live, old], now=1000.0)
    runner.calls.clear()

    states = poller.poll([live, old], now=1001.0)

    assert runner.paths_for("status") == [live.path]
    assert [state.path for state in states] == [live.path, old.path]
    assert next(s for s in states if s.path == old.path).polled_at == 1000.0


def test_dormant_repo_is_polled_once_the_interval_passes(tmp_path: Path) -> None:
    runner = RecordingRunner({"status": ""})
    poller = Poller(INTERVAL, runner=runner)
    old = _repo(tmp_path / "old", dormant=True)
    poller.poll([old], now=1000.0)
    runner.calls.clear()

    poller.poll([old], now=1000.0 + INTERVAL)

    assert runner.paths_for("status") == [old.path]


def test_force_polls_a_dormant_repo_inside_its_window(tmp_path: Path) -> None:
    runner = RecordingRunner({"status": ""})
    poller = Poller(INTERVAL, runner=runner)
    old = _repo(tmp_path / "old", dormant=True)
    poller.poll([old], now=1000.0)
    runner.calls.clear()

    poller.poll([old], now=1001.0, force=True)

    assert runner.paths_for("status") == [old.path]


def test_due_is_true_for_a_normal_repo_every_tick(tmp_path: Path) -> None:
    poller = Poller(INTERVAL, runner=RecordingRunner({"status": ""}))
    live = _repo(tmp_path / "live")
    poller.poll([live], now=1000.0)

    assert poller.due(live, 1000.1)


def test_dropping_a_repo_forgets_its_reading(tmp_path: Path) -> None:
    runner = RecordingRunner({"status": ""})
    poller = Poller(INTERVAL, runner=runner)
    old = _repo(tmp_path / "old", dormant=True)
    poller.poll([old], now=1000.0)

    assert poller.poll([], now=1001.0) == []

    runner.calls.clear()
    poller.poll([old], now=1002.0)

    assert runner.paths_for("status") == [old.path]


def test_parses_branch_upstream_and_counts() -> None:
    snap = parse_porcelain_v2(_STATUS_WITH_UPSTREAM)

    assert snap.branch == "main"
    assert snap.upstream == "origin/main"
    assert (snap.ahead, snap.behind) == (2, 1)
    assert snap.staged == 2
    assert snap.unstaged == 1
    assert snap.unmerged == 1
    assert snap.untracked == 1


def test_parses_initial_and_detached_markers() -> None:
    snap = parse_porcelain_v2(
        "# branch.oid (initial)\n# branch.head (detached)\n",
    )

    assert snap.head_sha is None
    assert snap.detached
    assert snap.branch is None


def test_a_worktree_state_labels_the_repo_it_belongs_to(
    git_repo: Path,
    worktree: Path,
) -> None:
    state = Poller(INTERVAL).poll([_worktree(worktree, git_repo)])[0]

    assert state.label == f"{git_repo.name} ⑂ {worktree.name}"
    assert state.row_label == f"  ⑂ {worktree.name}"
    assert state.name == worktree.name
    assert state.branch == "side"
    assert state.main_git_dir == git_repo / ".git"


def test_a_clone_labels_itself(git_repo: Path) -> None:
    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.label == git_repo.name
    assert state.row_label == git_repo.name


def test_a_conflict_counts_apart_from_the_ordinary_edits(git_repo: Path) -> None:
    git(git_repo, "checkout", "-qb", "theirs")
    (git_repo / "tracked.txt").write_text("theirs\n", encoding="utf-8")
    git(git_repo, "commit", "-qam", "Theirs")
    git(git_repo, "checkout", "-q", "main")
    (git_repo / "tracked.txt").write_text("ours\n", encoding="utf-8")
    git(git_repo, "commit", "-qam", "Ours")
    subprocess.run(
        ["git", "merge", "theirs"],  # noqa: S607
        cwd=git_repo,
        capture_output=True,
        check=False,
        timeout=30,
    )

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.unmerged == 1
    assert state.unstaged == 0
    assert state.operation == "merge"
    assert state.halted
    assert gitstate.state_parts(state)[:2] == ["merge", "U1"]


def test_parses_an_unmerged_status_line_into_its_own_count() -> None:
    snap = parse_porcelain_v2(
        "u UU N... 100644 100644 100644 100644 ggg hhh iii conflict.txt\n",
    )

    assert snap.unmerged == 1
    assert snap.unstaged == 0
    assert snap.dirty_paths == ("conflict.txt",)


@pytest.mark.parametrize(
    ("marker", "operation"),
    [
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    ],
)
def test_each_in_progress_marker_names_its_operation(
    git_repo: Path,
    marker: str,
    operation: str,
) -> None:
    target = git_repo / ".git" / marker
    if marker.startswith("rebase-"):
        target.mkdir()
    else:
        target.write_text("1111111111111111111111111111111111111111\n")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.operation == operation
    assert gitstate.state_parts(state)[0] == operation


def test_a_rebase_outranks_the_merge_head_it_leaves_behind(git_repo: Path) -> None:
    (git_repo / ".git" / "rebase-merge").mkdir()
    (git_repo / ".git" / "MERGE_HEAD").write_text(
        "1111111111111111111111111111111111111111\n"
    )

    assert gitstate.read_operation(git_repo / ".git") == "rebase"


def test_a_stash_is_counted_and_shown(git_repo: Path) -> None:
    (git_repo / "tracked.txt").write_text("shelved\n", encoding="utf-8")
    git(git_repo, "stash", "push", "-q", "-m", "first")
    (git_repo / "tracked.txt").write_text("shelved again\n", encoding="utf-8")
    git(git_repo, "stash", "push", "-q", "-m", "second")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.stashed == 2
    assert state.dirty == 0
    assert gitstate.state_parts(state) == ["stash 2"]


def test_a_repo_with_no_stash_counts_none(git_repo: Path) -> None:
    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.stashed == 0
    assert state.operation == gitstate.NO_OPERATION
    assert gitstate.state_parts(state) == []


def test_a_worktree_reports_the_repos_stash(git_repo: Path, worktree: Path) -> None:
    (git_repo / "tracked.txt").write_text("shelved\n", encoding="utf-8")
    git(git_repo, "stash", "push", "-q", "-m", "shared")

    state = Poller(INTERVAL).poll([_worktree(worktree, git_repo)])[0]

    assert state.stashed == 1
