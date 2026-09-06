"""Tests for the dashboard, driven through Textual's pilot."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from conftest import RecordingRunner, git
from textual.widgets import DataTable, Footer, Header
from textual.worker import WorkerCancelled

from cboard2.board import Board, Row, group_families
from cboard2.config import Config, load_config
from cboard2.gitstate import Poller, RepoState
from cboard2.pull import Outcome
from cboard2.remote import UNKNOWN, MergedPR, PullRequest, RemoteState
from cboard2.tui import (
    _COLUMNS,
    ActivityScreen,
    CboardApp,
    DetailScreen,
    Fold,
    NameFilter,
    _origin_color,
    _paint,
    active_text,
    cap_worktrees,
    cursor_key,
    filter_rows,
    fold_cells,
    origin_style,
    pr_content,
    pr_text,
    relative,
    remote_text,
    row_cells,
    sort_rows,
    state_text,
    worker_cancelled,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from rich.text import Text
    from textual.content import Content
    from textual.notifications import SeverityLevel

NEVER = 100_000.0
"""A refresh interval long enough that only explicit polls fire."""


def _config(root: Path, *, dormant: tuple[Path, ...] = ()) -> Config:
    return Config(
        roots=(root,),
        max_depth=4,
        dormant=dormant,
        dormant_interval=4 * 3600.0,
        remote=False,
        remote_interval=300.0,
        origin_colors=True,
        worktrees=True,
        worktree_limit=5,
    )


def _board(root: Path, *, dormant: tuple[Path, ...] = ()) -> Board:
    return Board(_config(root, dormant=dormant))


def _row(
    name: str,
    *,
    dirty: int = 0,
    ahead: int = 0,
    active_at: float = 0.0,
    dormant: bool = False,
    polled_at: float = 0.0,
    remote: RemoteState = UNKNOWN,
    main_git_dir: Path | None = None,
) -> Row:
    state = RepoState(
        path=Path("/tmp") / name,  # noqa: S108 — never touched, only rendered
        name=name,
        dormant=dormant,
        readable=True,
        polled_at=polled_at,
        branch="main",
        unstaged=dirty,
        ahead=ahead,
        main_git_dir=main_git_dir,
    )
    return Row(state=state, moved_at=active_at, remote=remote)


async def _settle(app: CboardApp) -> None:
    """Wait for the poll worker so the table holds real readings.

    A superseded exclusive poll raises out of ``wait_for_complete``; that is
    the dashboard working, not a failure.
    """
    with contextlib.suppress(WorkerCancelled):
        await app.workers.wait_for_complete()  # pyright: ignore[reportUnknownMemberType]


def _table(app: CboardApp) -> DataTable[str | Text]:
    """Return the repo table, typed so its cells are not Unknown."""
    return cast("DataTable[str | Text]", app.query_one(DataTable))


@pytest.mark.asyncio
async def test_lists_every_repo_with_live_state(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
    app = CboardApp(_board(git_repo.parent), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        cells = [str(cell) for cell in table.get_row_at(0)]

    assert table.row_count == 1
    assert cells[0] == git_repo.name
    assert cells[1] == "main"
    assert "?1" in cells[4]
    assert not (tmp_path / "cache" / "cboard2" / "remote.json").exists()


@pytest.mark.asyncio
async def test_dirty_filter_hides_clean_repos(git_repo: Path, tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    git(clean, "init", "-b", "main", "-q")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        assert _table(app).row_count == 2

        (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
        await pilot.press("r")
        await _settle(app)
        await pilot.press("d")
        await pilot.pause()
        table = _table(app)
        remaining = table.row_count
        names = [str(table.get_row_at(0)[0])]

    assert remaining == 1
    assert names == [git_repo.name]


@pytest.mark.asyncio
async def test_unpushed_filter_hides_repos_level_with_upstream(
    git_repo: Path,
) -> None:
    app = CboardApp(_board(git_repo.parent), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.press("u")
        await pilot.pause()
        rows = _table(app).row_count

    assert rows == 0


@pytest.mark.asyncio
async def test_an_external_commit_reaches_the_activity_feed(git_repo: Path) -> None:
    app = CboardApp(_board(git_repo.parent), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        (git_repo / "tracked.txt").write_text("more\n", encoding="utf-8")
        git(git_repo, "commit", "-qam", "Committed from another terminal")

        await pilot.press("r")
        await _settle(app)
        await pilot.press("a")
        await pilot.pause()

        screen = app.screen
        assert isinstance(screen, ActivityScreen)
        feed = cast("DataTable[str]", screen.query_one(DataTable))
        details = [str(feed.get_row_at(index)[3]) for index in range(feed.row_count)]

    assert "Committed from another terminal" in details


@pytest.mark.asyncio
async def test_a_deleted_repo_is_flagged_until_the_next_rescan(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    keeper = tmp_path / "keeper"
    keeper.mkdir()
    git(keeper, "init", "-b", "main", "-q")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        shutil.rmtree(git_repo)

        # A plain poll stays inside the rescan interval, so the repo is still
        # on the watch list and renders struck through rather than vanishing.
        app.poll()
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        names = [str(table.get_row_at(index)[0]) for index in range(table.row_count)]

    assert f"✗ {git_repo.name}" in names
    assert keeper.name in names


@pytest.mark.asyncio
async def test_a_rescan_drops_a_deleted_repo(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    keeper = tmp_path / "keeper"
    keeper.mkdir()
    git(keeper, "init", "-b", "main", "-q")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        shutil.rmtree(git_repo)

        await pilot.press("r")
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        names = [str(table.get_row_at(index)[0]) for index in range(table.row_count)]

    assert names == [keeper.name]


@pytest.mark.asyncio
async def test_pressing_r_picks_up_a_new_clone(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        assert _table(app).row_count == 1

        fresh = tmp_path / "fresh"
        fresh.mkdir()
        git(fresh, "init", "-b", "main", "-q")

        await pilot.press("r")
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        names = {str(table.get_row_at(index)[0]) for index in range(table.row_count)}

    assert names == {git_repo.name, "fresh"}


@pytest.mark.asyncio
async def test_shift_r_polls_a_dormant_repo_inside_its_window(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({"status": "# branch.head main\n"})
    old = tmp_path / "old"
    old.mkdir()
    (old / ".git").mkdir()
    board = Board(
        _config(tmp_path, dormant=(old,)),
        poller=Poller(4 * 3600.0, runner=runner),
    )
    app = CboardApp(board, refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        runner.calls.clear()

        await pilot.press("r")
        await _settle(app)
        assert runner.paths_for("status") == []

        await pilot.press("R")
        await _settle(app)

    assert runner.paths_for("status") == [old]


def test_detail_screen_reports_files_branches_and_movements(git_repo: Path) -> None:
    (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
    board = _board(git_repo.parent)
    screen = DetailScreen(board.refresh()[0], board)
    now = time.time()

    assert "loose.txt" in screen.files_content().plain
    assert "main" in screen.branches_content(now).plain
    assert "Add tracked file" in screen.branches_content(now).plain
    assert "commit" in screen.entries_content(now).plain


def test_detail_screen_says_so_when_the_tree_is_clean(git_repo: Path) -> None:
    board = _board(git_repo.parent)
    screen = DetailScreen(board.refresh()[0], board)

    assert "clean" in screen.files_content().plain


@pytest.mark.asyncio
async def test_the_cursor_stays_on_its_repo_across_a_poll(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main", "-q")
    (other / "seed.txt").write_text("one\n", encoding="utf-8")
    git(other, "add", "seed.txt")
    git(other, "commit", "-qm", "Seed the other repo")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        table.move_cursor(row=1)
        selected = cursor_key(table)

        (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
        await pilot.press("r")
        await _settle(app)
        await pilot.pause()
        after = cursor_key(_table(app))

    assert selected is not None
    assert after == selected


@pytest.mark.asyncio
async def test_an_unchanged_poll_does_not_repaint(git_repo: Path) -> None:
    app = CboardApp(
        _board(git_repo.parent),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        # A rebuild clears the table and mints fresh RowKey objects, so the
        # identity of the existing key is what proves nothing was redrawn.
        before = list(_table(app).rows)

        app.render_rows()

        assert next(iter(_table(app).rows)) is before[0]

        (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
        await pilot.press("r")
        await _settle(app)

        assert next(iter(_table(app).rows)) is not before[0]

        app.action_cycle_window()

    assert app.sub_title.endswith("active <1h")


@pytest.mark.asyncio
async def test_filtering_out_the_selected_repo_does_not_raise(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    git(clean, "init", "-b", "main", "-q")
    (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        table = _table(app)
        table.move_cursor(row=table.get_row_index(str(clean)))

        await pilot.press("d")
        await pilot.pause()
        remaining = _table(app).row_count
        landed = cursor_key(_table(app))

    assert remaining == 1
    assert landed == str(git_repo)


def _keys(app: CboardApp) -> list[str | None]:
    """Return the row keys in the order the table is showing them."""
    return [key.value for key in _table(app).rows]


@pytest.mark.asyncio
async def test_a_recency_change_does_not_move_the_row(tmp_path: Path) -> None:
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )
    beta = str(Path("/tmp/beta"))  # noqa: S108 — never touched, only keyed on
    alpha = str(Path("/tmp/alpha"))  # noqa: S108

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("beta", active_at=100.0), _row("alpha", active_at=50.0)])
        await pilot.pause()
        assert _keys(app) == [beta, alpha]

        # alpha is now the most recent, so a rebuild would put it first.
        app.apply_rows(
            [_row("beta", active_at=100.0), _row("alpha", active_at=1_799_999_940.0)],
        )
        await pilot.pause()
        order = _keys(app)
        table = _table(app)
        alpha_active = str(table.get_row_at(1)[_COLUMNS.index("Active")])

    assert order == [beta, alpha]
    assert alpha_active == "1m ago"


@pytest.mark.asyncio
async def test_a_commit_updates_the_head_cell_in_place(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main", "-q")
    (other / "seed.txt").write_text("one\n", encoding="utf-8")
    git(other, "add", "seed.txt")
    git(other, "commit", "-qm", "Seed the other repo")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        order = _keys(app)

        (git_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        git(git_repo, "commit", "-qam", "Committed while the dashboard was open")
        app.poll()
        await _settle(app)
        await pilot.pause()

        table = _table(app)
        heads = {
            str(table.get_row_at(index)[0]): str(table.get_row_at(index)[2])
            for index in range(table.row_count)
        }
        after = _keys(app)

    assert after == order
    assert heads[git_repo.name] == "Committed while the dashboard was open"
    assert heads["other"] == "Seed the other repo"


@pytest.mark.asyncio
async def test_a_poll_leaves_the_scroll_offset_alone(tmp_path: Path) -> None:
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )
    rows = [_row(f"repo{index:02d}", active_at=1000.0 - index) for index in range(40)]

    async with app.run_test(size=(120, 12)) as pilot:
        await _settle(app)
        app.apply_rows(rows)
        await pilot.pause()
        table = _table(app)
        table.scroll_to(y=8, animate=False)
        await pilot.pause()
        offset = table.scroll_offset

        app.apply_rows(
            [*rows[:5], _row("repo05", dirty=3, active_at=9000.0), *rows[6:]]
        )
        await pilot.pause()
        after = _table(app).scroll_offset

    assert offset.y > 0
    assert after == offset


@pytest.mark.asyncio
async def test_a_repo_joining_the_visible_set_reorders(tmp_path: Path) -> None:
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )
    rows = [_row("beta", active_at=100.0), _row("alpha", active_at=50.0)]

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows(rows)
        await pilot.pause()
        assert _keys(app) == [str(Path("/tmp/beta")), str(Path("/tmp/alpha"))]  # noqa: S108

        # Board hands rows over newest first, which is where gamma lands.
        app.apply_rows([_row("gamma", active_at=500.0), *rows])
        await pilot.pause()
        joined = _keys(app)

    assert joined[0] == str(Path("/tmp/gamma"))  # noqa: S108


@pytest.mark.asyncio
async def test_pressing_s_reorders_the_rows(tmp_path: Path) -> None:
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("beta", active_at=100.0), _row("alpha", active_at=50.0)])
        await pilot.pause()

        await pilot.press("s")
        await pilot.pause()
        sorted_by_name = _keys(app)

    assert app.sub_title.endswith("sort: name")
    assert sorted_by_name == [str(Path("/tmp/alpha")), str(Path("/tmp/beta"))]  # noqa: S108


class RecordingApp(CboardApp):
    """The dashboard, with its notifications captured instead of shown.

    Toasts do not mount under ``run_test()``, so overriding ``notify`` is the
    only way to see what the app told the user.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # pyright: ignore[reportArgumentType]
        self.told: list[str] = []

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """Record the message rather than raising a toast."""
        self.told.append(message)
        super().notify(
            message,
            title=title,
            severity=severity,
            timeout=timeout,
            markup=markup,
        )


@pytest.mark.asyncio
async def test_pressing_shift_d_writes_the_repo_into_the_config(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "# keep me\nmax_depth = 1\ndormant = []\n",
        encoding="utf-8",
    )
    app = RecordingApp(
        _board(git_repo.parent),
        refresh_interval=NEVER,
        config_file=config_file,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        _table(app).move_cursor(row=0)

        await pilot.press("D")
        await _settle(app)
        await pilot.pause()
        written = config_file.read_text(encoding="utf-8")
        told = list(app.told)
        rows = _table(app).row_count

    assert "# keep me" in written
    assert str(git_repo) in written or git_repo.name in written
    assert load_config(config_file).dormant == (git_repo,)
    assert app._board.config.dormant == (git_repo,)  # noqa: SLF001
    assert rows == 1
    assert any("dormant" in line for line in told)


@pytest.mark.asyncio
async def test_pressing_shift_d_twice_wakes_the_repo(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    original = "max_depth = 1\ndormant = []\n"
    config_file.write_text(original, encoding="utf-8")
    app = CboardApp(
        _board(git_repo.parent),
        refresh_interval=NEVER,
        config_file=config_file,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        _table(app).move_cursor(row=0)

        await pilot.press("D")
        await _settle(app)
        await pilot.press("D")
        await _settle(app)
        await pilot.pause()

    assert config_file.read_text(encoding="utf-8") == original
    assert load_config(config_file).dormant == ()


@pytest.mark.asyncio
async def test_a_repo_toggled_dormant_is_skipped_by_the_next_poll(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "shelf"
    repo.mkdir()
    (repo / ".git").mkdir()
    runner = RecordingRunner({"status": "# branch.head main\n"})
    board = Board(_config(tmp_path), poller=Poller(4 * 3600.0, runner=runner))
    config_file = tmp_path / "config.toml"
    app = CboardApp(board, refresh_interval=NEVER, config_file=config_file)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        _table(app).move_cursor(row=0)
        runner.calls.clear()

        await pilot.press("D")
        await _settle(app)
        await pilot.pause()
        polled = runner.paths_for("status")
        rows = _table(app).row_count

    assert load_config(config_file).dormant == (repo,)
    assert polled == []
    assert rows == 1


@pytest.mark.asyncio
async def test_the_toggle_does_not_reorder_the_rows(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-b", "main", "-q")
    config_file = tmp_path / "config.toml"
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        config_file=config_file,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        order = _keys(app)
        _table(app).move_cursor(row=1)

        await pilot.press("D")
        await _settle(app)
        await pilot.pause()
        after = _keys(app)

    assert after == order
    assert set(after) == {str(git_repo), str(other)}


@pytest.mark.asyncio
async def test_an_unwritable_config_notifies_instead_of_raising(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    app = RecordingApp(
        _board(git_repo.parent),
        refresh_interval=NEVER,
        config_file=blocker / "config.toml",
    )

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.pause()
        _table(app).move_cursor(row=0)

        await pilot.press("D")
        await _settle(app)
        await pilot.pause()
        told = list(app.told)

    assert any("could not write" in line for line in told)
    assert app._board.config.dormant == ()  # noqa: SLF001


def test_dirty_filter_keeps_only_dirty_rows() -> None:
    rows = [_row("clean"), _row("messy", dirty=3)]

    assert [row.state.name for row in filter_rows(rows, dirty_only=True)] == ["messy"]


def test_unpushed_filter_keeps_only_rows_ahead() -> None:
    rows = [_row("level"), _row("ahead", ahead=2)]

    assert [row.state.name for row in filter_rows(rows, unpushed_only=True)] == [
        "ahead"
    ]


def test_since_filter_drops_older_rows() -> None:
    rows = [_row("old", active_at=100.0), _row("new", active_at=500.0)]

    assert [row.state.name for row in filter_rows(rows, since=200.0)] == ["new"]


def test_sorts_by_recent_name_and_dirty() -> None:
    rows = [
        _row("beta", active_at=100.0, dirty=5),
        _row("alpha", active_at=500.0, dirty=1),
    ]

    assert [row.state.name for row in sort_rows(rows, "recent")] == ["alpha", "beta"]
    assert [row.state.name for row in sort_rows(rows, "name")] == ["alpha", "beta"]
    assert [row.state.name for row in sort_rows(rows, "dirty")] == ["beta", "alpha"]


def test_dormant_row_shows_the_age_of_its_reading() -> None:
    now = 100_000.0
    dormant = _row("old", active_at=now - 60, dormant=True, polled_at=now - 7200)
    live = _row("live", active_at=now - 60, polled_at=now)

    assert active_text(dormant, now).plain == "1m ago (read 2h ago)"
    assert active_text(live, now).plain == "1m ago"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.0, "just now"), (42.0, "42s ago"), (300.0, "5m ago"), (172800.0, "2d ago")],
)
def test_relative_renders_an_age(seconds: float, expected: str) -> None:
    assert relative(seconds) == expected


def _record_notes(
    app: CboardApp,
    notes: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect the app's notifications instead of showing them."""

    def note(message: object, **_kwargs: object) -> None:
        notes.append(str(message))

    monkeypatch.setattr(app, "notify", note)


def _record_severities(
    app: CboardApp,
    severities: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect the severity of each notification the app raises."""

    def note(_message: object, **kwargs: object) -> None:
        severities.append(str(kwargs.get("severity", "information")))

    monkeypatch.setattr(app, "notify", note)


def _record_both(
    app: CboardApp,
    notes: list[str],
    severities: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Collect each notification's message and its severity."""

    def note(message: object, **kwargs: object) -> None:
        notes.append(str(message))
        severities.append(str(kwargs.get("severity", "information")))

    monkeypatch.setattr(app, "notify", note)


def _styles(content: Content) -> list[tuple[str, str]]:
    """Return each styled run as its text and its style, in document order."""
    return [
        (content.plain[span.start : span.end], str(span.style))
        for span in content.spans
    ]


def _pr(number: int, *, draft: bool = False) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"Change {number}",
        url=f"https://github.com/acme/repo/pull/{number}",
        draft=draft,
        updated_at=1_799_999_940.0,
    )


BEHIND = RemoteState(
    origin="https://github.com/acme/repo.git",
    slug="acme/repo",
    default_branch="main",
    default_sha="f" * 40,
    behind_default=True,
    default_known=True,
    prs_known=True,
)
CURRENT = RemoteState(
    origin="https://github.com/acme/repo.git",
    slug="acme/repo",
    default_branch="main",
    default_sha="f" * 40,
    default_known=True,
    prs_known=True,
)
BEHIND_BRANCH = RemoteState(
    origin="https://github.com/acme/repo.git",
    slug="acme/repo",
    default_branch="main",
    default_sha="f" * 40,
    default_known=True,
    prs_known=True,
    branch="fix",
    branch_remote="fix",
    branch_sha="e" * 40,
    branch_known=True,
    behind_branch=True,
)


def test_remote_column_separates_behind_current_and_unknown() -> None:
    assert remote_text(_row("a", remote=BEHIND)).plain == "behind main"
    assert remote_text(_row("b", remote=CURRENT)).plain == "—"
    assert remote_text(_row("c")).plain == "?"


MERGED_BRANCH = replace(
    BEHIND_BRANCH,
    branch_merged_pr=MergedPR(
        number=12,
        title="Fix the thing",
        url="https://github.com/acme/repo/pull/12",
        merged_at=1_799_999_940.0,
    ),
)


def test_remote_column_reports_a_merged_pr_ahead_of_both_behind_markers() -> None:
    behind_too = replace(MERGED_BRANCH, behind_default=True)

    assert remote_text(_row("a", remote=MERGED_BRANCH)).plain == "PR #12 merged"
    assert remote_text(_row("b", remote=behind_too)).plain == "PR #12 merged"
    assert str(remote_text(_row("c", remote=MERGED_BRANCH)).style) == "green"


def test_remote_column_names_the_branch_ahead_of_the_default() -> None:
    both = replace(BEHIND_BRANCH, behind_default=True)

    assert remote_text(_row("a", remote=BEHIND_BRANCH)).plain == "behind origin/fix"
    assert remote_text(_row("b", remote=both)).plain == "behind origin/fix"


def test_origin_style_groups_repos_by_host_and_owner() -> None:
    ssh = _row("a", remote=replace(UNKNOWN, origin="git@github.com:ove/one.git"))
    https = _row("b", remote=replace(UNKNOWN, origin="https://github.com/ove/two"))
    other = _row("c", remote=replace(UNKNOWN, origin="https://gitlab.com/ove/x.git"))

    assert origin_style(ssh) == origin_style(https) != ""
    assert origin_style(other) != origin_style(ssh)
    assert origin_style(_row("d")) == ""


def test_the_origin_color_is_hashed_once_per_host_and_owner() -> None:
    _origin_color.cache_clear()
    ssh = _row("a", remote=replace(UNKNOWN, origin="git@github.com:ove/one.git"))
    https = _row("b", remote=replace(UNKNOWN, origin="https://github.com/ove/two"))

    color = origin_style(ssh)

    assert origin_style(https) == color
    assert origin_style(ssh) == color
    assert _origin_color.cache_info().misses == 1


def test_repo_name_carries_the_origin_color(tmp_path: Path) -> None:
    row = _row("a", remote=replace(UNKNOWN, origin="git@github.com:ove/one.git"))
    here = replace(row, state=replace(row.state, path=tmp_path))
    gone = replace(row, state=replace(row.state, path=tmp_path / "deleted"))

    assert row_cells(here, 0.0)[0].style == origin_style(here)
    assert row_cells(gone, 0.0)[0].style == "strike dim"


def test_origin_colors_off_leaves_the_name_uncolored(tmp_path: Path) -> None:
    row = _row("a", remote=replace(UNKNOWN, origin="git@github.com:ove/one.git"))
    here = replace(row, state=replace(row.state, path=tmp_path))

    assert row_cells(here, 0.0, colors=False)[0].style == ""


def test_a_repo_that_gains_an_origin_repaints_its_name(tmp_path: Path) -> None:
    plain = replace(_row("a"), state=replace(_row("a").state, path=tmp_path))
    read = replace(plain, remote=replace(UNKNOWN, origin="git@github.com:ove/one.git"))

    assert _paint(row_cells(plain, 0.0)[0]) != _paint(row_cells(read, 0.0)[0])


def test_pr_column_counts_drafts_apart() -> None:
    two = RemoteState(prs_known=True, prs=(_pr(9, draft=True), _pr(7)))
    three = RemoteState(
        prs_known=True,
        prs=(_pr(9, draft=True), _pr(8, draft=True), _pr(7)),
    )
    plain = RemoteState(prs_known=True, prs=(_pr(7),))

    assert pr_text(_row("a", remote=two)).plain == "2 (1 draft)"
    assert pr_text(_row("b", remote=three)).plain == "3 (2 drafts)"
    assert pr_text(_row("c", remote=plain)).plain == "1"
    assert pr_text(_row("d", remote=RemoteState(prs_known=True))).plain == "—"
    assert pr_text(_row("e")).plain == "?"


def test_behind_filter_keeps_only_repos_missing_the_remote_tip() -> None:
    rows = [
        _row("stale", remote=BEHIND),
        _row("branch", remote=BEHIND_BRANCH),
        _row("fresh", remote=CURRENT),
        _row("mute"),
    ]

    kept = filter_rows(rows, behind_only=True)

    assert [row.state.name for row in kept] == ["stale", "branch"]


def test_prs_filter_keeps_only_repos_with_an_open_pr() -> None:
    with_pr = RemoteState(prs_known=True, prs=(_pr(3),))
    rows = [
        _row("mine", remote=with_pr),
        _row("none", remote=RemoteState(prs_known=True)),
        _row("mute"),
    ]

    kept = filter_rows(rows, prs_only=True)

    assert [row.state.name for row in kept] == ["mine"]


@pytest.mark.asyncio
async def test_pressing_b_and_p_narrow_the_table(tmp_path: Path) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)
    with_pr = RemoteState(prs_known=True, prs=(_pr(3),))

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows(
            [
                _row("stale", remote=BEHIND),
                _row("mine", remote=with_pr),
                _row("quiet", remote=CURRENT),
            ],
        )
        await pilot.pause()
        assert len(_keys(app)) == 3

        await pilot.press("b")
        assert _keys(app) == [str(Path("/tmp/stale"))]  # noqa: S108

        await pilot.press("b")
        await pilot.press("p")
        assert _keys(app) == [str(Path("/tmp/mine"))]  # noqa: S108


def test_the_detail_screen_lists_open_prs_and_the_remote_state(
    tmp_path: Path,
) -> None:
    state = RemoteState(
        origin="https://github.com/acme/repo.git",
        slug="acme/repo",
        default_branch="main",
        default_sha="f" * 40,
        behind_default=True,
        default_known=True,
        prs=(_pr(9, draft=True), _pr(7)),
        prs_known=True,
    )
    screen = DetailScreen(
        _row("repo", remote=state),
        _board(tmp_path),
        clock=lambda: 0.0,
    )

    remote = screen.remote_content().plain
    prs = screen.prs_content(0.0).plain

    assert "acme/repo" in remote
    assert "has commits" in remote
    assert "#9" in prs
    assert "draft" in prs
    assert "#7" in prs
    assert "/pull/7" in prs


def test_the_detail_screen_reports_the_branch_and_the_default_branch(
    tmp_path: Path,
) -> None:
    screen = DetailScreen(
        _row("repo", remote=BEHIND_BRANCH),
        _board(tmp_path),
        clock=lambda: 0.0,
    )

    remote = screen.remote_content().plain

    assert "origin/fix has commits this branch has not pulled" in remote
    assert f"remote tip {'e' * 40}" in remote
    assert "main is current" in remote


def test_the_detail_screen_labels_a_repo_off_github_by_its_origin(
    tmp_path: Path,
) -> None:
    state = RemoteState(
        origin="git@git.example.com:acme/repo.git",
        default_branch="trunk",
        default_sha="f" * 40,
        default_known=True,
        prs_known=True,
    )
    screen = DetailScreen(
        _row("repo", remote=state), _board(tmp_path), clock=lambda: 0.0
    )

    assert "git@git.example.com:acme/repo.git" in screen.remote_content().plain
    assert "trunk is current" in screen.remote_content().plain
    assert screen.prs_content(0.0).plain == "none open"


def test_the_detail_screen_says_so_when_there_is_no_origin(tmp_path: Path) -> None:
    screen = DetailScreen(_row("repo"), _board(tmp_path), clock=lambda: 0.0)

    assert screen.remote_content().plain == "no origin"


def test_a_pr_heading_carries_a_link_and_the_url_stays_plain() -> None:
    pr = PullRequest(
        number=24,
        title="chore: bump alz-addon-resources",
        url="https://github.com/acme/repo/pull/24",
        draft=False,
        updated_at=0.0,
    )

    content = pr_content(pr, 90.0)

    assert content.plain.splitlines() == [
        "#24  chore: bump alz-addon-resources",
        "1m ago  https://github.com/acme/repo/pull/24",
    ]
    assert _styles(content) == [
        (
            "#24  chore: bump alz-addon-resources",
            "link='https://github.com/acme/repo/pull/24'",
        ),
        ("1m ago  https://github.com/acme/repo/pull/24", "dim"),
    ]


def test_a_draft_marker_sits_outside_the_link() -> None:
    pr = PullRequest(
        number=9,
        title="Parked",
        url="https://github.com/acme/repo/pull/9",
        draft=True,
        updated_at=None,
    )

    content = pr_content(pr, 0.0)

    assert content.plain.splitlines()[0] == "#9  Parked  draft"
    assert _styles(content)[:2] == [
        ("#9  Parked", "link='https://github.com/acme/repo/pull/9'"),
        ("  draft", "magenta"),
    ]


def test_a_title_holding_markup_renders_literally() -> None:
    pr = PullRequest(
        number=3,
        title="Fix [WIP] handling of [a][b]",
        url="https://github.com/acme/repo/pull/3",
        draft=False,
        updated_at=None,
    )

    assert "Fix [WIP] handling of [a][b]" in pr_content(pr, 0.0).plain


def test_a_pr_with_no_url_is_not_linked() -> None:
    pr = PullRequest(number=5, title="No url", url="", draft=False, updated_at=None)

    assert _styles(pr_content(pr, 0.0))[0][1] != "link"
    assert not any("link" in style for _, style in _styles(pr_content(pr, 0.0)))


@pytest.mark.parametrize(
    "title",
    ["A change", "Hybrid søk", "Fix [WIP] parsing", "Don't break", "a=b:c"],
)
def test_a_pr_line_parses_through_textuals_markup(title: str) -> None:
    """A Static renders through Textual's parser, not Rich's.

    An unquoted link value passed Rich and crashed Textual on the colon in
    ``https:``, so the parser under test has to be the one the widget uses.
    """
    pr = PullRequest(
        number=7,
        title=title,
        url="https://github.com/acme/repo/pull/7",
        draft=True,
        updated_at=0.0,
    )

    content = pr_content(pr, 0.0)

    assert title in content.plain
    assert "#7" in content.plain
    assert "draft" in content.plain


def test_a_url_holding_a_quote_is_not_linked() -> None:
    pr = PullRequest(
        number=5,
        title="Odd url",
        url="https://example.invalid/it's",
        draft=False,
        updated_at=None,
    )

    assert _styles(pr_content(pr, 0.0))[0][1] != "link"
    assert not any("link" in style for _, style in _styles(pr_content(pr, 0.0)))


@pytest.mark.asyncio
async def test_the_table_fills_the_space_between_header_and_footer(
    tmp_path: Path,
) -> None:
    """A short repo list must not strand the table's scrollbar mid-screen."""
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app)
        app.apply_rows([_row("one"), _row("two")])
        await pilot.pause()

        table = _table(app)
        header = app.query_one(Header)
        footer = app.query_one(Footer)
        expected = app.screen.size.height - header.size.height - footer.size.height

        assert table.row_count == 2
        assert table.size.height == expected
        assert table.region.bottom == app.screen.size.height - footer.size.height


@pytest.mark.asyncio
async def test_pressing_shift_p_pulls_the_selected_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[Path, str | None]] = []

    def fake_pull(
        root: Path,
        *,
        default_branch: str | None = None,
        **_kwargs: object,
    ) -> Outcome:
        asked.append((root, default_branch))
        return Outcome(ok=True, message="pulled 3 commits", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", fake_pull)
    notes: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("stale", remote=BEHIND)])
        await pilot.pause()
        _record_notes(app, notes, monkeypatch)

        await pilot.press("P")
        await _settle(app)
        await pilot.pause()

    assert asked == [(Path("/tmp/stale"), "main")]  # noqa: S108
    assert notes[0] == "pulling stale…"
    assert notes[-1] == "stale: pulled 3 commits (main)"


@pytest.mark.asyncio
async def test_a_second_shift_p_does_not_start_a_second_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two git processes in one repo fight over index.lock."""
    started = threading.Event()
    release = threading.Event()
    runs: list[Path] = []

    def blocking_pull(
        root: Path,
        *,
        default_branch: str | None = None,
        **_kwargs: object,
    ) -> Outcome:
        assert default_branch == "main"
        runs.append(root)
        started.set()
        release.wait(timeout=5.0)
        return Outcome(ok=True, message="already up to date", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", blocking_pull)
    notes: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("stale", remote=BEHIND)])
        await pilot.pause()
        _record_notes(app, notes, monkeypatch)

        await pilot.press("P")
        assert started.wait(timeout=5.0)
        await pilot.press("P")
        await pilot.pause()
        second = list(notes)
        release.set()
        await _settle(app)

    assert len(runs) == 1
    assert "stale is already pulling" in second


@pytest.mark.asyncio
async def test_a_failed_pull_notifies_as_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_pull(_root: Path, **_kwargs: object) -> Outcome:
        return Outcome(
            ok=False,
            message="fetch failed: fatal: could not read from remote",
        )

    monkeypatch.setattr("cboard2.tui.pull_default", failing_pull)
    severities: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("stale", remote=BEHIND)])
        await pilot.pause()
        _record_severities(app, severities, monkeypatch)

        await pilot.press("P")
        await _settle(app)
        await pilot.pause()

    assert severities[-1] == "error"


@pytest.mark.asyncio
async def test_shift_p_on_an_empty_table_does_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs: list[Path] = []

    def recording_pull(root: Path, **_kwargs: object) -> Outcome:
        runs.append(root)
        return Outcome(ok=True, message="already up to date", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", recording_pull)
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        await pilot.press("P")
        await pilot.pause()

    assert runs == []


def test_name_order_puts_a_worktree_under_its_repo() -> None:
    rows = [
        _row("zulu"),
        _row("side", main_git_dir=Path("/tmp/mid/.git")),  # noqa: S108 — rendered only
        _row("alpha"),
        _row("mid"),
    ]

    ordered = sort_rows(rows, "name")

    assert [row.state.row_label for row in ordered] == [
        "alpha",
        "mid",
        "  ⑂ side",
        "zulu",
    ]


def test_every_order_keeps_a_repo_and_its_worktrees_together() -> None:
    mid = Path("/tmp/mid/.git")  # noqa: S108 — rendered only, never opened
    rows = [
        _row("zulu", active_at=50.0, dirty=9),
        _row("side", active_at=90.0, main_git_dir=mid),
        _row("alpha", active_at=70.0),
        _row("mid", active_at=10.0, dirty=1),
    ]

    for order in ("recent", "dirty", "name"):
        ordered = sort_rows(rows, order)
        names = [row.state.name for row in ordered]

        assert names.index("side") == names.index("mid") + 1, order


def test_grouping_leaves_a_worktree_whose_repo_is_filtered_out() -> None:
    rows = [_row("side", main_git_dir=Path("/tmp/mid/.git"))]  # noqa: S108 — rendered

    assert group_families(rows) == rows


_HUB = Path("/tmp/hub")  # noqa: S108 — never touched, only rendered
_HUB_GIT = _HUB / ".git"


def _family(count: int, *, newest: float = 1000.0) -> list[Row]:
    """Return the hub repo and ``count`` worktrees under it, newest first."""
    return [
        _row("hub", active_at=newest + 1),
        *(
            _row(f"tree{index:02d}", active_at=newest - index, main_git_dir=_HUB_GIT)
            for index in range(count)
        ),
    ]


def _names(painted: list[Row | Fold]) -> list[str]:
    """Return the repo names painted, leaving the fold rows out."""
    return [entry.state.name for entry in painted if isinstance(entry, Row)]


def test_worktrees_over_the_limit_fold_into_one_row() -> None:
    painted = cap_worktrees(_family(15), limit=5)
    folds = [entry for entry in painted if isinstance(entry, Fold)]

    assert _names(painted) == ["hub", "tree00", "tree01", "tree02", "tree03", "tree04"]
    assert [(fold.family, fold.total, fold.hidden) for fold in folds] == [
        (_HUB_GIT, 15, 10),
    ]
    assert painted[-1] is folds[0]


def test_a_repo_at_the_limit_gets_no_fold_row() -> None:
    painted = cap_worktrees(_family(5), limit=5)

    assert not [entry for entry in painted if isinstance(entry, Fold)]
    assert len(_names(painted)) == 6


def test_the_fold_keeps_the_worktrees_with_the_newest_activity() -> None:
    rows = _family(4)
    rows.append(_row("stale", active_at=1.0, main_git_dir=_HUB_GIT))
    rows.append(_row("fresh", active_at=5000.0, main_git_dir=_HUB_GIT))

    painted = cap_worktrees(sort_rows(rows, "name"), limit=2)

    assert _names(painted) == ["hub", "fresh", "tree00"]


def test_worktrees_tied_on_activity_keep_the_listed_order() -> None:
    rows = [
        _row("hub", active_at=100.0),
        _row("first", active_at=7.0, main_git_dir=_HUB_GIT),
        _row("second", active_at=7.0, main_git_dir=_HUB_GIT),
        _row("third", active_at=7.0, main_git_dir=_HUB_GIT),
    ]

    painted = cap_worktrees(rows, limit=2)

    assert _names(painted) == ["hub", "first", "second"]


def test_an_expanded_family_paints_every_worktree() -> None:
    painted = cap_worktrees(_family(15), expanded={_HUB_GIT}, limit=5)
    folds = [entry for entry in painted if isinstance(entry, Fold)]

    assert len(_names(painted)) == 16
    assert [fold.hidden for fold in folds] == [0]


def test_the_fold_row_names_the_key_that_changes_it() -> None:
    collapsed = fold_cells(Fold(family=_HUB_GIT, total=15, hidden=10))
    expanded = fold_cells(Fold(family=_HUB_GIT, total=15, hidden=0))

    assert len(collapsed) == len(_COLUMNS)
    assert collapsed[0].plain == "  ⑂ 10 more worktrees"
    assert collapsed[1].plain == "enter to expand"
    assert expanded[0].plain == "  ⑂ 15 worktrees"
    assert expanded[1].plain == "enter to collapse"


@pytest.mark.asyncio
async def test_enter_on_the_fold_row_expands_then_collapses(tmp_path: Path) -> None:
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows(_family(8))
        await pilot.pause()
        folded = _keys(app)

        _table(app).move_cursor(row=len(folded) - 1)
        await pilot.press("enter")
        await pilot.pause()
        expanded = _keys(app)
        subtitle_expanded = app.sub_title

        await pilot.press("enter")
        await pilot.pause()
        collapsed = _keys(app)
        subtitle_folded = app.sub_title

    fold_key = f"fold:{_HUB_GIT}"
    assert folded == [
        str(_HUB),
        *(str(Path("/tmp") / f"tree{index:02d}") for index in range(5)),  # noqa: S108
        fold_key,
    ]
    assert len(expanded) == 10
    assert expanded[-1] == fold_key
    assert collapsed == folded
    assert "3 worktrees folded" in subtitle_folded
    assert "folded" not in subtitle_expanded


@pytest.mark.asyncio
async def test_shift_d_on_the_fold_row_writes_nothing(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    app = CboardApp(
        _board(tmp_path),
        refresh_interval=NEVER,
        clock=lambda: 1_800_000_000.0,
        config_file=config_file,
    )

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows(_family(8))
        await pilot.pause()
        _table(app).move_cursor(row=len(_keys(app)) - 1)

        await pilot.press("D")
        await pilot.pause()

    assert not config_file.exists()
    assert app._board.config.dormant == ()  # noqa: SLF001


@pytest.mark.asyncio
async def test_slash_opens_the_name_filter_and_typing_narrows_the_table(
    tmp_path: Path,
) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("keyforge"), _row("cboard2"), _row("notes")])
        await pilot.pause()

        await pilot.press("slash")
        await pilot.pause()
        entry = app.query_one(NameFilter)

        assert entry.has_class("open")
        assert app.focused is entry

        await pilot.press("f", "o", "r")
        await pilot.pause()

        assert _keys(app) == [str(Path("/tmp/keyforge"))]  # noqa: S108


@pytest.mark.asyncio
async def test_the_name_filter_matches_without_regard_to_case(tmp_path: Path) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("KeyForge"), _row("notes")])
        await pilot.pause()

        await pilot.press("slash")
        await pilot.press("k", "e", "y")
        await pilot.pause()

        assert _keys(app) == [str(Path("/tmp/KeyForge"))]  # noqa: S108


@pytest.mark.asyncio
async def test_a_name_filter_matching_nothing_empties_the_table(
    tmp_path: Path,
) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("keyforge"), _row("notes")])
        await pilot.pause()

        await pilot.press("slash")
        await pilot.press("z", "z", "z")
        await pilot.pause()

        assert _keys(app) == []
        assert _table(app).row_count == 0


@pytest.mark.asyncio
async def test_the_name_filter_composes_with_the_dirty_toggle(tmp_path: Path) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows(
            [
                _row("keyforge", dirty=2),
                _row("keyforge-docs"),
                _row("notes", dirty=1),
            ],
        )
        await pilot.pause()

        await pilot.press("d")
        await pilot.press("slash")
        await pilot.press("k", "e", "y")
        await pilot.pause()

        assert _keys(app) == [str(Path("/tmp/keyforge"))]  # noqa: S108
        assert "name ~ key" in app.sub_title


@pytest.mark.asyncio
async def test_escape_clears_the_name_filter_and_restores_every_row(
    tmp_path: Path,
) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("keyforge"), _row("cboard2"), _row("notes")])
        await pilot.pause()

        await pilot.press("slash")
        await pilot.press("k", "e", "y")
        await pilot.pause()
        assert len(_keys(app)) == 1

        await pilot.press("escape")
        await pilot.pause()
        entry = app.query_one(NameFilter)

        assert entry.value == ""
        assert not entry.has_class("open")
        assert len(_keys(app)) == 3
        assert app.focused is _table(app)


@pytest.mark.asyncio
async def test_the_name_filter_keeps_the_selected_row_selected(tmp_path: Path) -> None:
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("keyforge"), _row("keyforge-docs"), _row("notes")])
        await pilot.pause()
        _table(app).move_cursor(row=1)

        await pilot.press("slash")
        await pilot.press("k", "e", "y")
        await pilot.pause()

        assert cursor_key(_table(app)) == str(Path("/tmp/keyforge-docs"))  # noqa: S108


def test_a_worktree_matches_the_name_of_the_repo_it_belongs_to(
    tmp_path: Path,
) -> None:
    repo = _row("keyforge")
    tree = _row("fix", main_git_dir=tmp_path / "keyforge" / ".git")

    assert filter_rows([repo, tree], name="keyforge") == [repo, tree]
    assert filter_rows([repo, tree], name="fix") == [tree]


def _state_row(
    *,
    operation: str = "none",
    unmerged: int = 0,
    unstaged: int = 0,
    stashed: int = 0,
) -> Row:
    """Build a row carrying the working-tree fields the State column reads."""
    state = RepoState(
        path=Path("/tmp/repo"),  # noqa: S108 — never touched, only rendered
        name="repo",
        dormant=False,
        readable=True,
        polled_at=0.0,
        branch="main",
        operation=operation,
        unmerged=unmerged,
        unstaged=unstaged,
        stashed=stashed,
    )
    return Row(state=state, moved_at=0.0)


def test_the_state_cell_reds_a_repo_stopped_mid_rebase() -> None:
    cell = state_text(_state_row(operation="rebase", unmerged=2, unstaged=1))

    assert cell.plain == "rebase U2 M1"
    assert str(cell.style) == "red"


def test_the_state_cell_yellows_ordinary_dirt_and_dims_a_lone_stash() -> None:
    dirty = state_text(_state_row(unstaged=2))
    stashed = state_text(_state_row(stashed=1))

    assert (dirty.plain, str(dirty.style)) == ("M2", "yellow")
    assert (stashed.plain, str(stashed.style)) == ("stash 1", "dim")
    assert state_text(_state_row()).plain == "clean"


def test_the_detail_screen_reports_the_halted_operation_and_the_stash(
    tmp_path: Path,
) -> None:
    screen = DetailScreen(
        _state_row(operation="cherry-pick", unmerged=1, stashed=2),
        _board(tmp_path),
        clock=lambda: 0.0,
    )

    text = screen.working_content().plain

    assert "cherry-pick in progress, unfinished" in text
    assert "1 conflicted path" in text
    assert "2 stashes" in text
    assert (
        "nothing halted"
        in DetailScreen(
            _state_row(),
            _board(tmp_path),
            clock=lambda: 0.0,
        )
        .working_content()
        .plain
    )


def test_the_pr_cell_counts_the_review_queue_beside_the_users_own() -> None:
    state = RemoteState(
        prs_known=True,
        prs=(replace(_pr(3), checks="passing"),),
        review_prs_known=True,
        review_prs=(_pr(8), _pr(9)),
    )

    cell = pr_text(_row("repo", remote=state))

    assert cell.plain == "1 ✓  2 to review"
    assert str(cell.style) == "cyan"


def test_the_pr_cell_reds_a_failing_check_of_the_users_own() -> None:
    state = RemoteState(
        prs_known=True,
        prs=(replace(_pr(3), checks="failing"),),
        review_prs_known=True,
    )

    cell = pr_text(_row("repo", remote=state))

    assert cell.plain == "1 ✗"
    assert str(cell.style) == "red"


def test_the_pr_cell_stays_unknown_until_a_search_answers() -> None:
    assert pr_text(_row("repo")).plain == "?"
    assert (
        pr_text(
            _row("repo", remote=RemoteState(prs_known=True, review_prs_known=True)),
        ).plain
        == "—"
    )


def test_the_detail_screen_lists_the_prs_awaiting_review(tmp_path: Path) -> None:
    state = RemoteState(
        prs_known=True,
        review_prs_known=True,
        review_prs=(replace(_pr(12), checks="failing"),),
    )
    screen = DetailScreen(
        _row("repo", remote=state), _board(tmp_path), clock=lambda: 0.0
    )

    text = screen.review_content(0.0).plain

    assert "#12" in text
    assert "✗ failing" in text
    assert screen.prs_content(0.0).plain == "none open"


def test_the_detail_screen_names_the_pr_that_merged_this_branch(
    tmp_path: Path,
) -> None:
    screen = DetailScreen(
        _row("repo", remote=MERGED_BRANCH), _board(tmp_path), clock=lambda: 0.0
    )

    text = screen.remote_content().plain

    assert "fix was merged in #12" in text
    assert "https://github.com/acme/repo/pull/12" in text
    assert "main is current" in text


def test_an_unread_review_search_says_so(tmp_path: Path) -> None:
    screen = DetailScreen(_row("repo"), _board(tmp_path), clock=lambda: 0.0)

    assert screen.review_content(0.0).plain == "not read"


@pytest.mark.asyncio
async def test_a_cancelled_poll_does_not_apply_its_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling stops the awaiting task, so the thread finishes with stale rows."""
    started = threading.Event()
    release = threading.Event()
    checked = threading.Event()
    seen: list[bool] = []
    applied: list[int] = []

    def blocking_refresh(**_kwargs: object) -> list[Row]:
        started.set()
        release.wait(timeout=5.0)
        return [_row("stale")]

    def watched() -> bool:
        answer = worker_cancelled()
        seen.append(answer)
        checked.set()
        return answer

    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        monkeypatch.setattr(app._board, "refresh", blocking_refresh)  # noqa: SLF001

        def record(rows: Sequence[Row]) -> None:
            applied.append(len(rows))

        monkeypatch.setattr(app, "apply_rows", record)
        monkeypatch.setattr("cboard2.tui.worker_cancelled", watched)

        worker = app.poll()
        assert await asyncio.to_thread(started.wait, 5.0)
        worker.cancel()
        release.set()
        assert await asyncio.to_thread(checked.wait, 5.0)
        await pilot.pause()

    assert seen == [True]
    assert applied == []


@pytest.mark.asyncio
async def test_a_poll_that_raises_notifies_and_leaves_the_app_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_refresh(**_kwargs: object) -> list[Row]:
        message = "disk gone"
        raise OSError(message)

    notes: list[str] = []
    severities: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        monkeypatch.setattr(app._board, "refresh", exploding_refresh)  # noqa: SLF001
        _record_both(app, notes, severities, monkeypatch)

        app.poll()
        await _settle(app)
        await pilot.pause()

        assert app.is_running

    assert notes[-1] == "poll failed: disk gone"
    assert severities[-1] == "error"


@pytest.mark.asyncio
async def test_a_remote_read_that_raises_notifies_and_leaves_the_app_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_read(**_kwargs: object) -> bool:
        message = "gh is gone"
        raise OSError(message)

    notes: list[str] = []
    severities: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        monkeypatch.setattr(app._board, "read_remote", exploding_read)  # noqa: SLF001
        _record_both(app, notes, severities, monkeypatch)

        app.poll_remote()
        await _settle(app)
        await pilot.pause()

        assert app.is_running

    assert notes[-1] == "remote read failed: gh is gone"
    assert severities[-1] == "error"


@pytest.mark.asyncio
async def test_a_pull_that_raises_releases_the_path_and_notifies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the release, that repo would refuse every later pull."""

    def exploding_pull(_root: Path, **_kwargs: object) -> Outcome:
        message = "git blew up"
        raise RuntimeError(message)

    monkeypatch.setattr("cboard2.tui.pull_default", exploding_pull)
    notes: list[str] = []
    severities: list[str] = []
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("stale", remote=BEHIND)])
        await pilot.pause()
        _record_both(app, notes, severities, monkeypatch)

        await pilot.press("P")
        await _settle(app)
        await pilot.pause()

        assert app.is_running
        assert app._pulling == set()  # noqa: SLF001

    assert notes[-1] == "stale: pull could not run: git blew up"
    assert severities[-1] == "error"


async def _wait_for_a_pull(started: threading.Semaphore) -> bool:
    """Wait off the event loop for one more pull worker to reach its git call."""
    return await asyncio.to_thread(started.acquire, timeout=5.0)


@pytest.mark.asyncio
async def test_pulls_past_the_cap_wait_their_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six repos, four workers: the rest run as the earlier ones finish."""
    started = threading.Semaphore(0)
    release = threading.Event()
    counter = threading.Lock()
    runs: list[Path] = []
    inflight = 0
    peak = 0

    def blocking_pull(root: Path, **_kwargs: object) -> Outcome:
        nonlocal inflight, peak
        with counter:
            inflight += 1
            peak = max(peak, inflight)
            runs.append(root)
        started.release()
        release.wait(timeout=5.0)
        with counter:
            inflight -= 1
        return Outcome(ok=True, message="already up to date", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", blocking_pull)
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row(f"repo{index}", remote=BEHIND) for index in range(6)])
        await pilot.pause()

        for _ in range(6):
            await pilot.press("P")
            await pilot.press("down")
        for _ in range(4):
            assert await _wait_for_a_pull(started)

        await pilot.pause()
        assert peak == 4
        assert len(runs) == 4

        release.set()
        for _ in range(2):
            assert await _wait_for_a_pull(started)
        await _settle(app)
        await pilot.pause()

        assert app._pulling == set()  # noqa: SLF001

    assert peak == 4
    assert len(runs) == 6


def test_the_pr_cell_reads_the_checks_state_once_per_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def counting_worst(prs: object) -> str:
        calls.append(len(cast("tuple[PullRequest, ...]", prs)))
        return "failing"

    monkeypatch.setattr("cboard2.tui.worst_checks", counting_worst)
    state = RemoteState(
        prs_known=True,
        prs=(replace(_pr(3), checks="failing"),),
        review_prs_known=True,
        review_prs=(_pr(8),),
    )

    cell = pr_text(_row("repo", remote=state))

    assert len(calls) == 1
    assert cell.plain == "1 ✗  1 to review"
    assert str(cell.style) == "red"


@pytest.mark.asyncio
async def test_the_subtitle_names_the_step_a_pull_is_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_pull(
        _root: Path,
        *,
        on_step: Callable[[str], None] = lambda _step: None,
        **_kwargs: object,
    ) -> Outcome:
        on_step("fetching")
        started.set()
        release.wait(timeout=5.0)
        return Outcome(ok=True, message="already up to date", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", blocking_pull)
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([_row("stale", remote=BEHIND)])
        await pilot.pause()

        await pilot.press("P")
        assert await asyncio.to_thread(started.wait, 5.0)
        await pilot.pause()
        during = app.sub_title

        release.set()
        await _settle(app)
        await pilot.pause()
        after = app.sub_title

    assert "stale fetching" in during
    assert "fetching" not in after


@pytest.mark.asyncio
async def test_a_family_that_leaves_disk_loses_its_expansion_state(
    tmp_path: Path,
) -> None:
    """Both sets would otherwise grow for the length of the session."""
    gone = _row("gone")
    kept = _row("kept")
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        app.apply_rows([gone, kept])
        app.toggle_fold(gone.state.family)
        app.toggle_fold(kept.state.family)
        app._pulling.add(gone.state.path)  # noqa: SLF001
        await pilot.pause()

        app.apply_rows([kept])
        await pilot.pause()

        assert app._expanded == {kept.state.family}  # noqa: SLF001
        assert app._pulling == set()  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_queued_pull_keeps_its_entry_through_a_rescan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Semaphore(0)
    release = threading.Event()

    def blocking_pull(_root: Path, **_kwargs: object) -> Outcome:
        started.release()
        release.wait(timeout=5.0)
        return Outcome(ok=True, message="already up to date", branch="main")

    monkeypatch.setattr("cboard2.tui.pull_default", blocking_pull)
    app = CboardApp(_board(tmp_path), refresh_interval=NEVER, clock=lambda: 0.0)

    async with app.run_test() as pilot:
        await _settle(app)
        pulling = _row("pulling", remote=BEHIND)
        app.apply_rows([pulling])
        await pilot.pause()

        await pilot.press("P")
        assert await _wait_for_a_pull(started)

        app.apply_rows([])
        await pilot.pause()

        assert app._pulling == {pulling.state.path}  # noqa: SLF001

        release.set()
        await _settle(app)
        await pilot.pause()

    assert app._pulling == set()  # noqa: SLF001
