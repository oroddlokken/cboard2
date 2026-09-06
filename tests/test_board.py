"""Tests for the rescan schedule that keeps the repo list current."""

from __future__ import annotations

import shutil
import threading
from dataclasses import fields
from typing import TYPE_CHECKING, cast

import pytest
from conftest import git

from cboard2.board import RESCAN_INTERVAL, Board
from cboard2.config import Config
from cboard2.discovery import discover
from cboard2.remote import RemoteReader

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cboard2.discovery import Repo

START = 1_800_000_000.0


def _config(root: Path, *, remote: bool = False) -> Config:
    return Config(
        roots=(root,),
        max_depth=1,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=remote,
        remote_interval=300.0,
        origin_colors=True,
        worktrees=True,
        worktree_limit=5,
    )


def _board(root: Path) -> Board:
    return Board(_config(root))


@pytest.fixture
def repo_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the stored repo list at a file of this test's own."""
    target = tmp_path / "store" / "repos.json"
    monkeypatch.setenv("CBOARD2_REPO_CACHE", str(target))
    return target


class _Walks:
    """Counts the walks a board runs, passing each through to the real one."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, config: Config) -> list[Repo]:
        """Record the walk and return what discovery found."""
        self.count += 1
        return discover(config)


class _GatedWalk(_Walks):
    """A walk that holds the board's scan open until the test lets it finish."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, config: Config) -> list[Repo]:
        """Announce the walk, wait for the test, then return what it found."""
        self.entered.set()
        self.release.wait(30)
        return super().__call__(config)


class _AnnouncingLock:
    """A lock that reports each caller the moment before it waits on it."""

    def __init__(self, inner: threading.Lock) -> None:
        self._inner = inner
        self.arrived = threading.Semaphore(0)

    def __enter__(self) -> None:
        """Announce this caller, then wait for the lock."""
        self.arrived.release()
        self._inner.acquire()

    def __exit__(self, *_: object) -> None:
        """Hand the lock to whoever is waiting."""
        self._inner.release()


def _gate(board: Board, walk: _GatedWalk, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every scan through ``walk`` and announce every caller of the lock."""
    monkeypatch.setattr("cboard2.board.discover", walk)
    monkeypatch.setattr(board, "_scan_lock", _AnnouncingLock(board._scan_lock))  # noqa: SLF001


def _arrived(board: Board, *, count: int) -> bool:
    """Wait until ``count`` callers have announced themselves at the scan lock."""
    lock = cast("_AnnouncingLock", board._scan_lock)  # noqa: SLF001
    return all(lock.arrived.acquire(timeout=30) for _ in range(count))


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
    gh = _CountingGh()
    board = Board(_config(tmp_path, remote=True), remote=RemoteReader(gh=gh))

    assert board.read_remote(now=START) is True

    assert [repo.name for repo in board.repos] == ["alpha", "beta"]
    assert [args[0] for args in gh.calls] == ["search", "search", "api"]


def test_a_worktree_row_follows_the_repo_it_belongs_to(
    git_repo: Path,
    worktree: Path,
) -> None:
    (worktree / "loose.txt").write_text("newer than the repo\n", encoding="utf-8")

    rows = _board(git_repo.parent).refresh()

    assert [row.state.path for row in rows] == [git_repo, worktree]
    assert rows[0].active_at < rows[1].active_at


def test_a_second_board_takes_the_stored_repo_list(
    tmp_path: Path,
    repo_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only = _init(tmp_path, "only")
    walks = _Walks()
    monkeypatch.setattr("cboard2.board.discover", walks)
    _board(tmp_path).refresh(now=START)
    assert repo_cache.exists()

    rows = _board(tmp_path).refresh(now=START + RESCAN_INTERVAL - 1)

    assert [row.state.path for row in rows] == [only]
    assert walks.count == 1


@pytest.mark.usefixtures("repo_cache")
def test_a_repo_cloned_after_the_window_still_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path, "first")
    walks = _Walks()
    monkeypatch.setattr("cboard2.board.discover", walks)
    _board(tmp_path).refresh(now=START)

    _init(tmp_path, "second")
    rows = _board(tmp_path).refresh(now=START + RESCAN_INTERVAL)

    assert [row.state.path.name for row in rows] == ["first", "second"]
    assert walks.count == 2


def test_a_corrupt_stored_repo_list_costs_a_walk_and_nothing_else(
    tmp_path: Path,
    repo_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    only = _init(tmp_path, "only")
    walks = _Walks()
    monkeypatch.setattr("cboard2.board.discover", walks)
    _board(tmp_path).refresh(now=START)
    repo_cache.write_text("{ truncated", encoding="utf-8")

    rows = _board(tmp_path).refresh(now=START + 1)

    assert [row.state.path for row in rows] == [only]
    assert walks.count == 2


@pytest.mark.usefixtures("repo_cache")
def test_a_second_scan_in_one_process_walks_rather_than_reading_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stored list answers a cold start, never a board that has scanned."""
    _init(tmp_path, "first")
    walks = _Walks()
    monkeypatch.setattr("cboard2.board.discover", walks)
    board = _board(tmp_path)
    board.refresh(now=START)

    _init(tmp_path, "second")
    rows = board.refresh(now=START + 1, rescan=True)

    assert [row.state.path.name for row in rows] == ["first", "second"]
    assert walks.count == 2


@pytest.mark.usefixtures("repo_cache")
def test_two_concurrent_rescans_walk_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init(tmp_path, "only")
    walk = _GatedWalk()
    board = _board(tmp_path)
    _gate(board, walk, monkeypatch)

    first = threading.Thread(target=board.rescan, kwargs={"now": START})
    first.start()
    assert walk.entered.wait(30)
    second = threading.Thread(target=board.rescan, kwargs={"now": START})
    second.start()
    assert _arrived(board, count=2)
    walk.release.set()
    first.join(30)
    second.join(30)

    assert walk.count == 1
    assert [repo.name for repo in board.repos] == ["only"]


@pytest.mark.usefixtures("repo_cache")
def test_a_poll_and_a_remote_read_share_the_first_walk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard starts both threads from on_mount, moments apart."""
    repo = _init(tmp_path, "alpha")
    git(repo, "remote", "add", "origin", "https://github.com/acme/alpha.git")
    walk = _GatedWalk()
    board = Board(_config(tmp_path, remote=True), remote=RemoteReader(gh=_CountingGh()))
    _gate(board, walk, monkeypatch)

    poll = threading.Thread(target=board.refresh, kwargs={"now": START})
    poll.start()
    assert walk.entered.wait(30)
    remote = threading.Thread(target=board.read_remote, kwargs={"now": START})
    remote.start()
    assert _arrived(board, count=2)
    walk.release.set()
    poll.join(30)
    remote.join(30)

    assert walk.count == 1
    assert [found.name for found in board.repos] == ["alpha"]


def test_a_rescan_rebuilds_the_path_mapping(tmp_path: Path) -> None:
    """A mapping left over from the last scan cannot place a repo cloned since."""
    _init(tmp_path, "first")
    board = _board(tmp_path)
    board.refresh(now=START)

    _init(tmp_path, "second")
    shutil.rmtree(tmp_path / "first")
    rows = board.refresh(now=START + RESCAN_INTERVAL)

    assert [row.state.path.name for row in rows] == ["second"]


def test_active_at_is_a_stored_field(git_repo: Path) -> None:
    (git_repo / "loose.txt").write_text("newer than HEAD\n", encoding="utf-8")

    (row,) = _board(git_repo.parent).refresh()

    assert "active_at" in {found.name for found in fields(row)}
    assert row.active_at == max(
        at for at in (row.moved_at, row.state.last_edit) if at is not None
    )


def test_entries_reads_only_the_named_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cost this guards: one modal must not merge every watched reflog."""
    (tmp_path / "one").mkdir()
    git(tmp_path / "one", "init", "-b", "main", "-q")
    board = _board(tmp_path)
    board.rescan()
    asked: list[Path] = []

    def feed(*_args: object, **_kwargs: object) -> list[object]:
        message = "activity() merges every repo; entries() must not call it"
        raise AssertionError(message)

    def entries(repo: Repo) -> list[object]:
        asked.append(repo.path)
        return []

    monkeypatch.setattr(board._reader, "feed", feed)  # noqa: SLF001
    monkeypatch.setattr(board._reader, "entries", entries)  # noqa: SLF001
    wanted = board.repos[0].path

    assert board.entries(wanted, limit=15) == []
    assert asked == [wanted]


def test_entries_of_an_unwatched_path_is_empty(tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    git(tmp_path / "one", "init", "-b", "main", "-q")
    board = _board(tmp_path)
    board.rescan()

    assert board.entries(tmp_path / "never-scanned", limit=15) == []
