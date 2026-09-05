"""Tests for the rescan schedule that keeps the repo list current."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from conftest import git

from cboard2.board import RESCAN_INTERVAL, Board
from cboard2.config import Config
from cboard2.remote import RemoteReader

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

START = 1_800_000_000.0


def _board(root: Path) -> Board:
    config = Config(
        roots=(root,),
        max_depth=1,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=False,
        remote_interval=300.0,
        worktrees=True,
        worktree_limit=5,
    )
    return Board(config)


def _init(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-b", "main", "-q")
    return repo


def test_a_new_repo_appears_after_the_interval(tmp_path: Path) -> None:
    _init(tmp_path, "first")
    board = _board(tmp_path)
    assert len(board.refresh(now=START)) == 1

    _init(tmp_path, "second")

    assert len(board.refresh(now=START + 10)) == 1
    assert len(board.refresh(now=START + RESCAN_INTERVAL)) == 2


def test_a_deleted_repo_leaves_the_rows(tmp_path: Path) -> None:
    doomed = _init(tmp_path, "doomed")
    keeper = _init(tmp_path, "keeper")
    board = _board(tmp_path)
    assert len(board.refresh(now=START)) == 2

    shutil.rmtree(doomed)

    rows = board.refresh(now=START + RESCAN_INTERVAL)

    assert [row.state.path for row in rows] == [keeper]


def test_the_rescan_flag_skips_the_interval(tmp_path: Path) -> None:
    _init(tmp_path, "first")
    board = _board(tmp_path)
    board.refresh(now=START)

    _init(tmp_path, "second")

    assert len(board.refresh(now=START + 1, rescan=True)) == 2


def test_the_first_refresh_always_scans(tmp_path: Path) -> None:
    _init(tmp_path, "only")

    assert len(_board(tmp_path).refresh(now=START)) == 1


def test_a_quiet_rescan_leaves_the_repo_list_alone(tmp_path: Path) -> None:
    only = _init(tmp_path, "only")
    board = _board(tmp_path)
    board.refresh(now=START)

    rows = board.refresh(now=START + RESCAN_INTERVAL, rescan=True)

    assert [row.state.path for row in rows] == [only]


class _CountingGh:
    """A gh runner that answers nothing and counts what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        """Record the call and answer as an unusable gh would."""
        self.calls.append(tuple(args))
        return None


def test_read_remote_walks_the_roots_before_its_first_read(tmp_path: Path) -> None:
    """A dashboard starts this worker beside its first poll, not after it."""
    for name in ("alpha", "beta"):
        repo = _init(tmp_path, name)
        git(repo, "remote", "add", "origin", f"https://github.com/acme/{name}.git")
    config = Config(
        roots=(tmp_path,),
        max_depth=1,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=True,
        remote_interval=300.0,
        worktrees=True,
        worktree_limit=5,
    )
    gh = _CountingGh()
    board = Board(config, remote=RemoteReader(gh=gh))

    assert board.read_remote(now=START) is True

    assert [repo.name for repo in board.repos] == ["alpha", "beta"]
    assert [args[0] for args in gh.calls] == ["api", "search"]


def test_a_worktree_row_follows_the_repo_it_belongs_to(
    git_repo: Path,
    worktree: Path,
) -> None:
    (worktree / "loose.txt").write_text("newer than the repo\n", encoding="utf-8")

    rows = _board(git_repo.parent).refresh()

    assert [row.state.path for row in rows] == [git_repo, worktree]
    assert rows[0].active_at < rows[1].active_at
