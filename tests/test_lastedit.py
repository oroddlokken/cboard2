"""Tests for last-edit recency and for the dirty paths it stats."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from conftest import git

from cboard2.discovery import Repo
from cboard2.gitstate import Poller, parse_porcelain_v2, unquote_path
from cboard2.lastedit import newest_mtime

if TYPE_CHECKING:
    from pathlib import Path

INTERVAL = 4 * 3600.0

_STATUS_QUOTED = (
    "# branch.oid 1111111111111111111111111111111111111111\n"
    "# branch.head main\n"
    "1 .M N... 100644 100644 100644 aaa bbb a file with spaces.txt\n"
    "1 .M N... 100644 100644 100644 ccc ddd "
    '"\\303\\246\\303\\270\\303\\245-\\303\\274n\\303\\257code.txt"\n'
    "2 R. N... 100644 100644 100644 eee fff R100 "
    '"renamed \\303\\246.txt"\t"tab\\tname.txt"\n'
    "u UU N... 100644 100644 100644 100644 ggg hhh iii conflict.txt\n"
    '? "untracked \\303\\246\\303\\270\\303\\245.txt"\n'
)


def _repo(path: Path) -> Repo:
    return Repo(path=path, name=path.name, dormant=False)


def test_uncommitted_edit_is_newer_than_the_commit(git_repo: Path) -> None:
    head_time = int(git(git_repo, "log", "-1", "--format=%ct").strip())
    tracked = git_repo / "tracked.txt"
    tracked.write_text("edited\n", encoding="utf-8")
    os.utime(tracked, (head_time + 600, head_time + 600))

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.last_edit is not None
    assert state.last_edit > head_time
    assert not state.last_edit_capped


def test_clean_repo_has_no_last_edit(git_repo: Path) -> None:
    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty_paths == ()
    assert state.last_edit is None


def test_untracked_file_counts_as_an_edit(git_repo: Path) -> None:
    (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty_paths == ("loose.txt",)
    assert state.last_edit is not None


def test_rename_is_stated_at_its_new_path(git_repo: Path) -> None:
    git(git_repo, "mv", "tracked.txt", "moved.txt")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty_paths == ("moved.txt",)
    assert state.last_edit is not None


def test_deleted_file_falls_back_to_its_directory(git_repo: Path) -> None:
    (git_repo / "tracked.txt").unlink()

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty_paths == ("tracked.txt",)
    assert state.last_edit is not None


def test_paths_with_spaces_and_non_ascii_are_stated(git_repo: Path) -> None:
    awkward = git_repo / "a file with æøå.txt"
    awkward.write_text("new\n", encoding="utf-8")

    state = Poller(INTERVAL).poll([_repo(git_repo)])[0]

    assert state.dirty_paths == ("a file with æøå.txt",)
    assert state.last_edit is not None


def test_exceeding_the_cap_flags_the_answer(git_repo: Path) -> None:
    for index in range(5):
        (git_repo / f"loose{index}.txt").write_text("x\n", encoding="utf-8")

    edit = newest_mtime(git_repo, [f"loose{index}.txt" for index in range(5)], cap=2)

    assert edit.capped
    assert edit.at is not None


def test_a_missing_path_is_skipped_not_raised(tmp_path: Path) -> None:
    edit = newest_mtime(tmp_path / "gone", ["nothing.txt"])

    assert edit.at is None
    assert not edit.capped


def test_parses_every_path_shape_and_unquotes() -> None:
    snap = parse_porcelain_v2(_STATUS_QUOTED)

    assert snap.dirty_paths == (
        "a file with spaces.txt",
        "æøå-ünïcode.txt",
        "renamed æ.txt",
        "conflict.txt",
        "untracked æøå.txt",
    )
    assert snap.staged == 1
    assert snap.unstaged == 3
    assert snap.untracked == 1


def test_unquote_leaves_an_unquoted_path_alone() -> None:
    assert unquote_path("a file with spaces.txt") == "a file with spaces.txt"
    assert unquote_path('"tab\\tname.txt"') == "tab\tname.txt"
    assert unquote_path('""') == ""
    assert unquote_path('"') == '"'
