"""Textual dashboard over the live git poll.

Every column is read from git on an interval, so the table shows what is on
disk rather than what some collector recorded earlier. Polling runs in a thread
worker: 95 repos take about a quarter of a second, which is a visible stall if
it happens on the UI thread.

Textual is imported here and nowhere else, so the CLI's ``ls`` path stays free
of it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from cboard2.activity import branches
from cboard2.board import group_families
from cboard2.config import config_path
from cboard2.configwrite import toggled, write_dormant
from cboard2.discovery import main_name
from cboard2.pull import pull_default
from cboard2.remote import ORIGIN

if TYPE_CHECKING:
    from collections.abc import Callable, Container, Iterable, Sequence

    from cboard2.board import Board, Row
    from cboard2.pull import Outcome
    from cboard2.remote import PullRequest

_COLUMNS = (
    "Repo",
    "Branch",
    "HEAD",
    "Last commit",
    "State",
    "↑↓",
    "Remote",
    "PR",
    "Active",
)

type Painted = dict[str, tuple[str, ...]]
"""The cell text last written per row key, so only changed cells are rewritten."""

_SORTS = ("recent", "name", "dirty")
"""Sort orders cycled by ``s``."""

_WINDOWS: tuple[tuple[str, float | None], ...] = (
    ("all", None),
    ("1h", 3600.0),
    ("1d", 86400.0),
    ("7d", 7 * 86400.0),
    ("30d", 30 * 86400.0),
)
"""Time windows cycled by ``t`` — label and lookback in seconds."""

_WORKTREE_LIMIT = 5
"""Worktree rows painted per repo before the rest fold into one row."""

_FOLD_PREFIX = "fold:"
"""Prefix marking a table row key as a fold row rather than a repo path."""

_HEAD_SUBJECT_MAX = 40
_DETAIL_FILE_LIMIT = 40
_DETAIL_ENTRY_LIMIT = 15
_ACTIVITY_LIMIT = 200
_ACTIVITY_COLUMNS = ("When", "Repo", "Verb", "Detail")


@dataclass(frozen=True, slots=True)
class Fold:
    """The fold row under a repo whose worktrees outnumber the cap."""

    family: Path
    total: int
    hidden: int
    """Worktrees left off the table, and zero while the family is expanded."""

    @property
    def key(self) -> str:
        """The table row key, which no repo path collides with."""
        return f"{_FOLD_PREFIX}{self.family}"


def cap_worktrees(
    rows: Sequence[Row],
    *,
    expanded: Container[Path] = frozenset[Path](),
    limit: int = _WORKTREE_LIMIT,
) -> list[Row | Fold]:
    """Fold each repo's worktrees down to the ``limit`` most recently active.

    A repo with thirty worktrees fills the screen on its own, so the rest go
    behind one fold row carrying their count. A family in ``expanded`` keeps
    every row and gets a fold row that collapses it again.
    """
    painted: list[Row | Fold] = []
    for family, block in _families(rows):
        worktrees = [row for row in block if row.state.main_git_dir is not None]
        if len(worktrees) <= limit:
            painted.extend(block)
            continue
        if family in expanded:
            painted.extend(block)
            painted.append(Fold(family=family, total=len(worktrees), hidden=0))
            continue
        kept = {id(row) for row in _most_recent(worktrees, limit)}
        painted.extend(
            row for row in block if row.state.main_git_dir is None or id(row) in kept
        )
        painted.append(
            Fold(family=family, total=len(worktrees), hidden=len(worktrees) - limit),
        )
    return painted


def _families(rows: Sequence[Row]) -> list[tuple[Path, list[Row]]]:
    """Split the rows into the blocks :func:`group_families` left them in."""
    blocks: list[tuple[Path, list[Row]]] = []
    for row in rows:
        family = row.state.family
        if blocks and blocks[-1][0] == family:
            blocks[-1][1].append(row)
        else:
            blocks.append((family, [row]))
    return blocks


def _most_recent(rows: Sequence[Row], limit: int) -> list[Row]:
    """Return the ``limit`` rows whose activity is newest.

    Chosen by activity rather than by position, so the cap keeps the same
    worktrees under the name sort as under the recency sort.
    """
    return sorted(rows, key=lambda row: -row.active_at)[:limit]


def filter_rows(
    rows: Sequence[Row],
    *,
    dirty_only: bool = False,
    unpushed_only: bool = False,
    behind_only: bool = False,
    prs_only: bool = False,
    since: float | None = None,
) -> list[Row]:
    """Keep the rows matching every active filter."""
    kept = list(rows)
    if dirty_only:
        kept = [row for row in kept if row.state.dirty]
    if unpushed_only:
        kept = [row for row in kept if row.state.ahead]
    if behind_only:
        kept = [
            row for row in kept if row.remote.behind_default or row.remote.behind_branch
        ]
    if prs_only:
        kept = [row for row in kept if row.remote.prs]
    if since is not None:
        kept = [row for row in kept if row.active_at >= since]
    return kept


def sort_rows(rows: Sequence[Row], order: str) -> list[Row]:
    """Return the rows in the named order, most interesting first.

    Every order ends in :func:`cboard2.board.group_families`, so a repo and its
    worktrees paint as one block wherever the sort put them.
    """
    if order == "name":
        return group_families(sorted(rows, key=_name_key))
    if order == "dirty":
        return group_families(
            sorted(rows, key=lambda row: (-row.state.dirty, -row.active_at)),
        )
    return group_families(sorted(rows, key=lambda row: -row.active_at))


def _name_key(row: Row) -> tuple[str, int, str]:
    """Sort a repo by name, with its worktrees directly under it.

    Sorting a worktree by its own directory name scatters it away from the repo
    it belongs to, which is the one row the user is comparing it against.
    """
    state = row.state
    if state.main_git_dir is None:
        return state.name.lower(), 0, ""
    return main_name(state.main_git_dir).lower(), 1, state.name.lower()


def relative(seconds: float) -> str:
    """Render an age as ``just now``, ``42s ago``, ``5m ago`` and so on."""
    span = max(0.0, seconds)
    if span < 5:
        return "just now"
    if span < 60:
        return f"{int(span)}s ago"
    if span < 3600:
        return f"{int(span // 60)}m ago"
    if span < 86400:
        return f"{int(span // 3600)}h ago"
    return f"{int(span // 86400)}d ago"


def branch_text(row: Row) -> Text:
    """Render the branch, marking a detached or unreadable repo."""
    state = row.state
    if not state.readable:
        return Text("unreadable", style="red")
    if state.detached:
        return Text("(detached)", style="yellow")
    return Text(state.branch or "—", style="" if state.branch else "dim")


def head_text(row: Row) -> Text:
    """Render HEAD's subject, truncated to the column width."""
    subject = row.state.head_subject
    if not subject:
        return Text("—", style="dim")
    if len(subject) > _HEAD_SUBJECT_MAX:
        subject = subject[: _HEAD_SUBJECT_MAX - 1] + "…"
    return Text(subject)


def state_text(row: Row) -> Text:
    """Render the dirty counts, or ``clean``."""
    state = row.state
    if state.dirty == 0:
        return Text("clean", style="dim")
    parts = [
        f"{prefix}{count}"
        for prefix, count in (
            ("S", state.staged),
            ("M", state.unstaged),
            ("?", state.untracked),
        )
        if count
    ]
    return Text(" ".join(parts), style="yellow")


def ahead_behind_text(row: Row) -> Text:
    """Render the distance from upstream, or a dash when level."""
    state = row.state
    if state.ahead == 0 and state.behind == 0:
        return Text("—", style="dim")
    parts = [
        f"{sign}{count}"
        for sign, count in (("+", state.ahead), ("-", state.behind))
        if count
    ]
    return Text(" ".join(parts), style="cyan" if state.ahead else "magenta")


def remote_text(row: Row) -> Text:
    """Render which branch has commits on the origin this clone has not pulled.

    The checked-out branch is reported ahead of the default branch, because it
    is the one the user is standing on. ``?`` and ``—`` are different answers:
    the first means no remote read covered this repo, the second that it is
    current.
    """
    remote = row.remote
    if remote.behind_branch:
        return Text(f"behind {ORIGIN}/{remote.branch_remote}", style="yellow")
    if not remote.default_known:
        return Text("?", style="dim")
    if remote.behind_default:
        return Text(f"behind {remote.default_branch}", style="yellow")
    return Text("—", style="dim")


def pr_text(row: Row) -> Text:
    """Render the count of the user's open PRs, and how many are drafts."""
    remote = row.remote
    if not remote.prs_known:
        return Text("?", style="dim")
    if not remote.prs:
        return Text("—", style="dim")
    drafts = remote.draft_count
    label = str(len(remote.prs))
    if drafts:
        label += f" ({drafts} draft{'s' if drafts > 1 else ''})"
    return Text(label, style="cyan")


def pr_content(pr: PullRequest, now: float) -> Content:
    """Return one pull request as a linked heading and a dim detail line.

    The heading carries an OSC 8 hyperlink, which the terminal renders as
    clickable and otherwise shows as plain text. The url stays spelled out
    below it, because there it is the only way to reach the PR.

    Assembled rather than written as markup: a PR title is whatever its author
    typed, and neither ``rich.markup.escape`` nor ``textual.markup.escape``
    escapes every bracket a parser would take for a tag.
    """
    heading = f"#{pr.number}  {pr.title}"
    parts: list[str | tuple[str, str]] = [
        (heading, f"link='{pr.url}'") if _linkable(pr.url) else heading,
    ]
    if pr.draft:
        parts.append(("  draft", "magenta"))
    age = "" if pr.updated_at is None else relative(now - pr.updated_at)
    parts.append("\n")
    parts.append((f"{age}  {pr.url}", "dim"))
    return Content.assemble(*parts)


def _joined(lines: Iterable[Content]) -> Content:
    """Stack rendered lines into one block."""
    return Content("\n").join(lines)


def _linkable(url: str) -> bool:
    """Return True when ``url`` can go inside a quoted link style untouched.

    A style value is read up to its closing quote, and there is no way to
    escape one inside it. A GitHub PR url holds no quote, so the guard only
    ever fires on something unexpected.
    """
    return bool(url) and "'" not in url


def active_text(row: Row, now: float) -> Text:
    """Render when the repo was last active, and how old a dormant reading is.

    A dormant repo is polled once every four hours, so its row would otherwise
    read as current when it is not.
    """
    label = relative(now - row.active_at)
    if not row.state.dormant:
        return Text(label)
    age = relative(now - row.state.polled_at)
    return Text(f"{label} (read {age})", style="dim")


def row_cells(row: Row, now: float) -> tuple[Text, ...]:
    """Render one repo as the cells of a table row."""
    state = row.state
    exists = Path(state.path).exists()
    return (
        Text(
            state.row_label if exists else f"✗ {state.row_label.lstrip()}",
            style="" if exists else "strike dim",
        ),
        branch_text(row),
        head_text(row),
        Text(
            "—" if state.head_time is None else relative(now - state.head_time),
            style="dim" if state.head_time is None else "",
        ),
        state_text(row),
        ahead_behind_text(row),
        remote_text(row),
        pr_text(row),
        active_text(row, now),
    )


def fold_cells(fold: Fold) -> tuple[Text, ...]:
    """Render a fold row: the hidden count, and the key that changes it."""
    if fold.hidden:
        plural = "" if fold.hidden == 1 else "s"
        label = f"  ⑂ {fold.hidden} more worktree{plural}"
        action = "enter to expand"
    else:
        label = f"  ⑂ {fold.total} worktrees"
        action = "enter to collapse"
    blanks = (Text("") for _ in range(len(_COLUMNS) - 2))
    return (Text(label, style="dim"), Text(action, style="dim"), *blanks)


def painted_row(entry: Row | Fold, now: float) -> tuple[str, tuple[Text, ...]]:
    """Return the table key and cells for a repo row or a fold row."""
    if isinstance(entry, Fold):
        return entry.key, fold_cells(entry)
    return str(entry.state.path), row_cells(entry, now)


def cursor_key(table: DataTable[str | Text]) -> str | None:
    """Return the row key under the cursor, or None when it is on no row."""
    if table.row_count == 0:
        return None
    try:
        cell = table.coordinate_to_cell_key(table.cursor_coordinate)
    except CellDoesNotExist:
        return None
    return cell.row_key.value


def restore_cursor(table: DataTable[str | Text], key: str | None) -> None:
    """Move the cursor back to ``key``, or leave it alone if that row is gone.

    A repo can be filtered out or deleted between two paints, and landing on
    whatever now occupies that position is better than refusing to paint.
    ``scroll=False`` keeps the viewport where the user left it.
    """
    if key is None:
        return
    try:
        index = table.get_row_index(key)
    except RowDoesNotExist:
        return
    table.move_cursor(row=index, scroll=False)


class DetailScreen(ModalScreen[None]):
    """One repo up close: its changed files, branches and HEAD movements."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,enter", "dismiss_detail", "Close"),
    ]

    CSS = """
    DetailScreen { align: center middle; }
    #detail {
        width: 84%;
        height: 84%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    .detail-heading { text-style: bold; margin-top: 1; }
    """

    def __init__(
        self,
        row: Row,
        board: Board,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._row = row
        self._board = board
        self._clock = clock

    def compose(self) -> ComposeResult:
        """Lay out the repo's files, branches and recent HEAD movements.

        Every section builds a :class:`Content` rather than a markup string.
        Repo names, branch names, paths and commit subjects all reach this
        screen unfiltered, and a subject as ordinary as ``[ci skip]`` parses as
        a style tag and takes the modal down with it.
        """
        state = self._row.state
        now = self._clock()
        with VerticalScroll(id="detail"):
            yield Static(
                Content.assemble(
                    (state.label, "bold"),
                    "\n",
                    (str(state.path), "dim"),
                ),
            )
            yield Static("Remote", classes="detail-heading")
            yield Static(self.remote_content())
            yield Static("My open pull requests", classes="detail-heading")
            yield Static(self.prs_content(now))
            yield Static("Changed files", classes="detail-heading")
            yield Static(self.files_content())
            yield Static("Branches", classes="detail-heading")
            yield Static(self.branches_content(now))
            yield Static("Recent HEAD movements", classes="detail-heading")
            yield Static(self.entries_content(now))

    def remote_content(self) -> Content:
        """Return what the origin says about this repo's branches.

        The checked-out branch comes first when it lags, and the default
        branch's line follows either way: a feature branch behind its own
        remote copy says nothing about whether ``main`` here is current.
        """
        remote = self._row.remote
        if remote.origin is None:
            return Content.styled("no origin", "dim")
        label = remote.slug or remote.origin
        if not remote.default_known:
            return Content.assemble(label, "  ", ("not read", "dim"))
        if remote.behind_branch:
            return Content.assemble(
                label,
                "  ",
                (
                    (
                        f"{ORIGIN}/{remote.branch_remote} has commits "
                        "this branch has not pulled"
                    ),
                    "yellow",
                ),
                "\n",
                (f"remote tip {remote.branch_sha}", "dim"),
                "\n",
                self._default_line(),
            )
        return Content.assemble(label, "  ", self._default_line())

    def _default_line(self) -> Content:
        """Return the origin's word on this repo's default branch, without the label."""
        remote = self._row.remote
        if remote.behind_default:
            return Content.assemble(
                (
                    f"{remote.default_branch} has commits this clone has not pulled",
                    "yellow",
                ),
                "\n",
                (f"remote tip {remote.default_sha}", "dim"),
            )
        return Content.styled(f"{remote.default_branch} is current", "dim")

    def prs_content(self, now: float) -> Content:
        """Return the user's open PRs on this repo, newest number first."""
        remote = self._row.remote
        if not remote.prs_known:
            return Content.styled("not read", "dim")
        if not remote.prs:
            return Content.styled("none open", "dim")
        return _joined(pr_content(pr, now) for pr in remote.prs)

    def files_content(self) -> Content:
        """Return the dirty paths, capped, or a note that the tree is clean."""
        paths = self._row.state.dirty_paths
        if not paths:
            return Content.styled("working tree clean", "dim")
        lines = [Content(path) for path in paths[:_DETAIL_FILE_LIMIT]]
        if len(paths) > _DETAIL_FILE_LIMIT:
            extra = len(paths) - _DETAIL_FILE_LIMIT
            lines.append(Content.styled(f"… and {extra} more", "dim"))
        return _joined(lines)

    def branches_content(self, now: float) -> Content:
        """Return the local branches with the age of each tip."""
        found = branches(self._row.state.path)
        if not found:
            return Content.styled("no local branches", "dim")
        return _joined(
            Content.assemble(
                branch.name,
                "  ",
                (relative(now - branch.committed_at), "dim"),
                "  ",
                branch.subject,
            )
            for branch in found
        )

    def entries_content(self, now: float) -> Content:
        """Return this repo's recent HEAD movements."""
        entries = self._board.activity(limit=_ACTIVITY_LIMIT)
        mine = [entry for entry in entries if entry.repo_path == self._row.state.path][
            :_DETAIL_ENTRY_LIMIT
        ]
        if not mine:
            return Content.styled("no reflog entries", "dim")
        return _joined(
            Content.assemble(
                (f"{relative(now - entry.at):>9}", "dim"),
                "  ",
                f"{entry.verb:<12} {entry.detail}",
            )
            for entry in mine
        )

    def action_dismiss_detail(self) -> None:
        """Close the detail modal."""
        self.dismiss()


class ActivityScreen(ModalScreen[None]):
    """The merged cross-repo reflog feed, newest first."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q,a", "dismiss_activity", "Close"),
    ]

    CSS = """
    ActivityScreen { align: center middle; }
    #activity {
        width: 92%;
        height: 84%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    def __init__(
        self,
        board: Board,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        super().__init__()
        self._board = board
        self._clock = clock

    def compose(self) -> ComposeResult:
        """Lay out the feed table."""
        with VerticalScroll(id="activity"):
            yield Static("[b]Activity[/b] · newest first")
            yield DataTable[str](zebra_stripes=True)

    def on_mount(self) -> None:
        """Add the columns and fill the table once."""
        table = cast("DataTable[str]", self.query_one(DataTable))
        table.add_columns(*_ACTIVITY_COLUMNS)
        now = self._clock()
        for entry in self._board.activity(limit=_ACTIVITY_LIMIT):
            table.add_row(
                relative(now - entry.at),
                entry.repo_name,
                entry.verb,
                entry.detail,
            )

    def action_dismiss_activity(self) -> None:
        """Close the activity modal."""
        self.dismiss()


class CboardApp(App[None]):
    """Live dashboard of every repo under the configured roots."""

    TITLE = "cboard2"

    CSS = """
    DataTable {
        height: 1fr;
    }
    """
    """Without a height the table stops at its last row, leaving its own
    horizontal scrollbar stranded mid-screen above the footer."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("d", "toggle_dirty", "Dirty only"),
        Binding("D", "toggle_dormant", "Dormant"),
        Binding("u", "toggle_unpushed", "Unpushed only"),
        Binding("b", "toggle_behind", "Behind remote"),
        Binding("p", "toggle_prs", "Open PRs"),
        Binding("P", "pull_default", "Pull default"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("t", "cycle_window", "Window"),
        Binding("a", "open_activity", "Activity"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("R", "refresh_all", "Refresh all"),
    ]

    def __init__(
        self,
        board: Board,
        *,
        refresh_interval: float = 2.0,
        clock: Callable[[], float] = time.time,
        config_file: Path | None = None,
    ) -> None:
        super().__init__()
        self._board = board
        self._refresh_interval = refresh_interval
        self._clock = clock
        self._config_file = config_file or config_path()
        self._rows: list[Row] = []
        self._dirty_only = False
        self._unpushed_only = False
        self._behind_only = False
        self._prs_only = False
        self._sort = _SORTS[0]
        self._window_index = 0
        self._painted: Painted = {}
        self._screen_keys: tuple[str, ...] = ()
        self._reorder = True
        self._pulling: set[Path] = set()
        self._expanded: set[Path] = set()

    def compose(self) -> ComposeResult:
        """Lay out the header, repo table and footer."""
        yield Header()
        yield DataTable[str | Text](zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Add columns, poll once, then poll on the interval."""
        table = cast("DataTable[str | Text]", self.query_one(DataTable))
        table.cursor_type = "row"
        table.add_columns(*_COLUMNS)
        self.poll()
        self.set_interval(self._refresh_interval, self.poll)
        if self._board.config.remote:
            self.poll_remote()
            self.set_interval(self._board.config.remote_interval, self.poll_remote)

    @work(thread=True, exclusive=True, group="poll")
    def poll(self, *, force: bool = False, rescan: bool = False) -> None:
        """Read git in a worker thread, then hand the rows back to the UI.

        ``exclusive`` drops a poll still running when the next tick arrives, so
        a slow disk cannot queue up refreshes behind each other. Walking the
        roots happens in here too, which is why it must not run on the UI
        thread.
        """
        rows = self._board.refresh(force=force, rescan=rescan)
        self.call_from_thread(self.apply_rows, rows)

    @work(thread=True, exclusive=True, group="remote")
    def poll_remote(self, *, force: bool = False) -> None:
        """Read GitHub in its own worker, then poll so the rows pick it up.

        A separate group from ``poll`` on purpose: the two gh calls take
        seconds, and sharing the exclusive poll worker would freeze the table
        for that long every interval.
        """
        if self._board.read_remote(force=force):
            self.call_from_thread(self.poll)

    def apply_rows(self, rows: Sequence[Row]) -> None:
        """Store the newest readings and repaint."""
        self._rows = list(rows)
        self.render_rows()

    def visible_rows(self) -> list[Row]:
        """Return the rows the filters and sort leave on screen."""
        _, window = _WINDOWS[self._window_index]
        since = None if window is None else self._clock() - window
        return sort_rows(
            filter_rows(
                self._rows,
                dirty_only=self._dirty_only,
                unpushed_only=self._unpushed_only,
                behind_only=self._behind_only,
                prs_only=self._prs_only,
                since=since,
            ),
            self._sort,
        )

    def render_rows(self) -> None:
        """Write the changed cells, and rebuild only when the row set changes.

        A rebuild is the only way to change row order, and it re-applies the
        recency sort — which moves every row and makes the list unreadable
        while someone is working in a repo. Writing a cell where it already
        sits leaves positions alone, so order refreshes on an explicit key or
        when a repo joins or leaves the visible set.
        """
        try:
            table = cast("DataTable[str | Text]", self.query_one(DataTable))
        except NoMatches:
            return  # screen torn down mid-poll; nothing to draw

        visible = cap_worktrees(
            self.visible_rows(),
            expanded=self._expanded,
            limit=self._board.config.worktree_limit,
        )
        now = self._clock()
        cells = [painted_row(entry, now) for entry in visible]
        keys = tuple(key for key, _ in cells)
        folds = [entry for entry in visible if isinstance(entry, Fold)]

        if self._reorder or set(keys) != set(self._screen_keys):
            self.rebuild(table, cells)
            self._screen_keys = keys
        else:
            self.write_changed(table, cells)

        self._painted = {key: tuple(cell.plain for cell in row) for key, row in cells}
        self._reorder = False
        self.sub_title = self.status_text(
            len(visible) - len(folds),
            sum(fold.hidden for fold in folds),
        )

    def rebuild(
        self,
        table: DataTable[str | Text],
        cells: Sequence[tuple[str, tuple[Text, ...]]],
    ) -> None:
        """Clear and re-add every row, holding the cursor and the scroll offset.

        Clearing resets both, so both are read first and put back after.
        """
        selected = cursor_key(table)
        offset = table.scroll_offset
        table.clear()
        for key, row in cells:
            table.add_row(*row, key=key)
        restore_cursor(table, selected)
        table.scroll_to(offset.x, offset.y, animate=False)

    def write_changed(
        self,
        table: DataTable[str | Text],
        cells: Sequence[tuple[str, tuple[Text, ...]]],
    ) -> None:
        """Overwrite only the cells whose text differs from what was painted."""
        columns = list(table.columns)
        for key, row in cells:
            previous = self._painted.get(key)
            for index, cell in enumerate(row):
                if previous is not None and previous[index] == cell.plain:
                    continue
                table.update_cell(key, columns[index], cell)

    def status_text(self, visible: int, folded: int = 0) -> str:
        """Describe what the table is showing, for the header's subtitle."""
        label, _ = _WINDOWS[self._window_index]
        parts = [f"{visible}/{len(self._rows)} repos", f"sort: {self._sort}"]
        active = [
            name
            for name, on in (
                ("dirty", self._dirty_only),
                ("unpushed", self._unpushed_only),
                ("behind", self._behind_only),
                ("with PRs", self._prs_only),
            )
            if on
        ]
        if active:
            parts.append(f"{' + '.join(active)} only")
        if label != "all":
            parts.append(f"active <{label}")
        if folded:
            parts.append(f"{folded} worktrees folded")
        read_at = self._board.remote_read_at
        if read_at is not None:
            parts.append(f"remote read {relative(self._clock() - read_at)}")
        return " · ".join(parts)

    def action_toggle_dirty(self) -> None:
        """Show only repos with uncommitted changes."""
        self._dirty_only = not self._dirty_only
        self._reorder = True
        self.render_rows()

    def action_toggle_unpushed(self) -> None:
        """Show only repos with commits upstream has not seen."""
        self._unpushed_only = not self._unpushed_only
        self._reorder = True
        self.render_rows()

    def action_toggle_behind(self) -> None:
        """Show only repos whose default branch has commits not pulled yet."""
        self._behind_only = not self._behind_only
        self._reorder = True
        self.render_rows()

    def action_toggle_prs(self) -> None:
        """Show only repos where the user has an open pull request."""
        self._prs_only = not self._prs_only
        self._reorder = True
        self.render_rows()

    def action_pull_default(self) -> None:
        """Check out the selected repo's default branch and pull it.

        No prompt: this is the one key in the dashboard that writes to a repo,
        and it runs on the row under the cursor.
        """
        row = self.selected_row()
        if row is None:
            return
        path = row.state.path
        if path in self._pulling:
            self.notify(f"{row.state.name} is already pulling")
            return

        self._pulling.add(path)
        self.notify(f"pulling {row.state.name}…")
        self.pull(path, row.remote.default_branch)

    @work(thread=True, group="pull")
    def pull(self, path: Path, default_branch: str | None) -> None:
        """Run the pull off the UI thread, then hand the outcome back.

        Not exclusive: pulling two repos one after the other should finish
        both, and a fetch already under way cannot be interrupted anyway.
        """
        outcome = pull_default(path, default_branch=default_branch)
        self.call_from_thread(self.pulled, path, outcome)

    def pulled(self, path: Path, outcome: Outcome) -> None:
        """Report what the pull did and repoll, so the row catches up."""
        self._pulling.discard(path)
        label = f"{path.name}: {outcome.message}"
        if not outcome.ok:
            self.notify(label, severity="error", timeout=10.0)
            return
        self.notify(f"{label} ({outcome.branch})")
        self.poll()

    def selected_row(self) -> Row | None:
        """Return the row under the cursor, or None when there is no row."""
        try:
            table = cast("DataTable[str | Text]", self.query_one(DataTable))
        except NoMatches:
            return None
        key = cursor_key(table)
        if key is None:
            return None
        return next((row for row in self._rows if str(row.state.path) == key), None)

    def action_cycle_sort(self) -> None:
        """Move to the next sort order."""
        self._sort = _SORTS[(_SORTS.index(self._sort) + 1) % len(_SORTS)]
        self._reorder = True
        self.render_rows()

    def action_cycle_window(self) -> None:
        """Move to the next time window."""
        self._window_index = (self._window_index + 1) % len(_WINDOWS)
        self._reorder = True
        self.render_rows()

    def action_refresh_now(self) -> None:
        """Rescan and poll now, still honoring the dormant interval, and re-sort."""
        self._reorder = True
        self.poll(rescan=True)

    def action_refresh_all(self) -> None:
        """Rescan, poll every repo and re-read the remote now, then re-sort."""
        self._reorder = True
        self.poll(force=True, rescan=True)
        if self._board.config.remote:
            self.poll_remote(force=True)

    def action_toggle_dormant(self) -> None:
        """Mark the selected repo dormant, or wake it if it already is.

        Writes the config so the choice outlives the session, and rescans so
        the flag reaches the row without a restart. No reorder flag is set: a
        repo changing its poll rate is not a reason to move every row.
        """
        try:
            table = cast("DataTable[str | Text]", self.query_one(DataTable))
        except NoMatches:
            return
        key = cursor_key(table)
        if key is None or key.startswith(_FOLD_PREFIX):
            return

        selected = Path(key)
        dormant = toggled(self._board.config.dormant, selected)
        try:
            write_dormant(self._config_file, dormant)
        except (OSError, ValueError) as exc:
            self.notify(f"could not write {self._config_file}: {exc}", severity="error")
            return

        self._board.config = replace(self._board.config, dormant=dormant)
        state = "dormant" if selected in dormant else "polled every tick"
        self.notify(f"{selected.name} is now {state}")
        self.poll(rescan=True)

    def action_open_activity(self) -> None:
        """Open the cross-repo activity feed."""
        self.push_screen(ActivityScreen(self._board, clock=self._clock))

    def toggle_fold(self, family: Path) -> None:
        """Paint every worktree of ``family``, or fold it back to the cap."""
        if family in self._expanded:
            self._expanded.discard(family)
        else:
            self._expanded.add(family)
        self._reorder = True
        self.render_rows()

    def on_data_table_row_selected(
        self,
        event: DataTable.RowSelected,
    ) -> None:
        """Open the detail modal, or fold the repo when the row is a fold row."""
        key = event.row_key.value
        if key is None:
            return
        if key.startswith(_FOLD_PREFIX):
            self.toggle_fold(Path(key.removeprefix(_FOLD_PREFIX)))
            return
        for row in self._rows:
            if str(row.state.path) == key:
                self.push_screen(DetailScreen(row, self._board, clock=self._clock))
                return
