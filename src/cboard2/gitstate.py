"""Read each repo's current git state by polling git, with no stored snapshot.

Two calls per repo — one ``status --porcelain=v2 --branch`` and one ``log -1``
— run across a thread pool. Measured on the author's machine: 94 repos in 0.28s
at 16 workers, against 1.4s serially.

A halted merge or rebase and the stash depth come from the git directory
instead of a third call: both are a marker file whose presence is the answer.

A repo the config marks dormant is held back to its long interval instead of
being polled every tick, so a shelf of archived clones costs nothing per
second. :meth:`Poller.poll` with ``force=True`` ignores that window.
"""

from __future__ import annotations

import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cboard2.discovery import git_dir, main_name
from cboard2.lastedit import newest_mtime

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from cboard2.discovery import Repo

    type GitRunner = Callable[[Path, Sequence[str]], str | None]

GIT_TIMEOUT = 5.0
"""Seconds before a single git call is abandoned and the repo reported unreadable."""

DEFAULT_MAX_WORKERS = 16
"""Concurrent git calls during one poll."""

_STATUS_ARGS = ("status", "--porcelain=v2", "--branch")
_HEAD_ARGS = ("log", "-1", "--format=%s%x09%ct")

_PATH_FIELD_INDEX = {"1": 8, "2": 9, "u": 10}
"""Field index of the path on each kind of porcelain-v2 entry line."""

NO_OPERATION = "none"
"""The value :attr:`RepoState.operation` takes when nothing is halted."""

_OPERATION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rebase", ("rebase-merge", "rebase-apply")),
    ("merge", ("MERGE_HEAD",)),
    ("cherry-pick", ("CHERRY_PICK_HEAD",)),
    ("revert", ("REVERT_HEAD",)),
    ("bisect", ("BISECT_LOG",)),
)
"""Entries git leaves in the git directory while an operation waits on the user.

Checked in the order git resolves them: a rebase that stopped on a conflict
also has a ``MERGE_HEAD``, and rebase is the operation the user has to finish.
"""

_STASH_LOG = "logs/refs/stash"
_STASH_REF = "refs/stash"
"""Where the stash depth is read from, without running ``git stash list``.

The reflog has one line per entry and is rewritten on a drop, so its line count
is the depth. A repo whose reflog is missing falls back to whether the ref
exists at all, which is worth one line of report rather than none.
"""


@dataclass(frozen=True, slots=True)
class RepoState:
    """One repo as of ``polled_at``.

    ``readable`` is False when git failed or timed out; the repo keeps its row
    so a broken clone stays visible rather than vanishing from the dashboard.
    """

    path: Path
    name: str
    dormant: bool
    readable: bool
    polled_at: float
    branch: str | None = None
    detached: bool = False
    head_sha: str | None = None
    head_subject: str | None = None
    head_time: int | None = None
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    unmerged: int = 0
    """Paths git reports as conflicted, counted apart from the ordinary edits."""

    operation: str = NO_OPERATION
    """The halted operation: merge, rebase, cherry-pick, revert, bisect or none."""

    stashed: int = 0
    """Entries on the stash, shared with this repo's linked worktrees."""

    ahead: int = 0
    behind: int = 0
    upstream: str | None = None
    dirty_paths: tuple[str, ...] = ()
    last_edit: float | None = None
    last_edit_capped: bool = False
    main_git_dir: Path | None = None
    """The main repo's git directory when this row is a linked worktree."""

    @property
    def dirty(self) -> int:
        """Total changed entries: staged, unstaged, untracked and conflicted."""
        return self.staged + self.unstaged + self.untracked + self.unmerged

    @property
    def halted(self) -> bool:
        """Whether an operation or a conflict is waiting on the user here."""
        return self.operation != NO_OPERATION or self.unmerged > 0

    @property
    def label(self) -> str:
        """The full name, which for a worktree names the repo it belongs to.

        ``cboard2 ⑂ fix`` rather than ``fix``, for the places that show one repo
        on its own and cannot lean on a neighbouring row for the context.
        """
        if self.main_git_dir is None:
            return self.name
        return f"{main_name(self.main_git_dir)} ⑂ {self.name}"

    @property
    def family(self) -> Path:
        """The git directory this row shares with the repo's other worktrees."""
        return self.main_git_dir or self.path / ".git"

    @property
    def row_label(self) -> str:
        """The name as a table cell: a worktree is indented under its repo.

        Every order paints a repo and its worktrees as one block, so the repo
        name is on the row above and this one carries the worktree directory.
        """
        if self.main_git_dir is None:
            return self.name
        return f"  ⑂ {self.name}"


@dataclass(frozen=True, slots=True)
class Snapshot:
    """The fields ``git status --porcelain=v2 --branch`` reports."""

    branch: str | None
    detached: bool
    head_sha: str | None
    upstream: str | None
    ahead: int
    behind: int
    staged: int
    unstaged: int
    untracked: int
    unmerged: int
    dirty_paths: tuple[str, ...]


def run_git(root: Path, args: Sequence[str]) -> str | None:
    """Run one git command in ``root``; return stdout, or None on any failure.

    ``--no-optional-locks`` is load-bearing: without it a poll refreshes and
    rewrites ``.git/index``, taking ``index.lock`` and racing whatever git
    command the user is running in that repo.
    """
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "--no-optional-locks", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


class Poller:
    """Polls repos on every tick, except dormant ones outside their window.

    Holds the last reading and last poll time per repo, so a skipped dormant
    repo still has a row and the caller can tell how old it is.
    """

    def __init__(
        self,
        dormant_interval: float,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        runner: GitRunner = run_git,
    ) -> None:
        self._dormant_interval = dormant_interval
        self._max_workers = max_workers
        self._runner = runner
        self._states: dict[Path, RepoState] = {}
        self._last_polled: dict[Path, float] = {}

    def due(self, repo: Repo, now: float) -> bool:
        """Return True when ``repo`` should be polled at ``now``.

        A non-dormant repo is always due. A dormant one is due when it has
        never been polled or its last reading is older than the interval.
        """
        if not repo.dormant:
            return True
        last = self._last_polled.get(repo.path)
        return last is None or (now - last) >= self._dormant_interval

    def poll(
        self,
        repos: Sequence[Repo],
        *,
        now: float | None = None,
        force: bool = False,
    ) -> list[RepoState]:
        """Poll every due repo and return a state for each of ``repos``, in order.

        ``force`` polls dormant repos too — what the dashboard's refresh-all
        binding calls.
        """
        moment = time.time() if now is None else now
        self._forget_absent(repos)

        targets = [repo for repo in repos if force or self.due(repo, moment)]
        for state in self._snapshot_all(targets, moment):
            self._states[state.path] = state
            self._last_polled[state.path] = moment

        return [self._states[repo.path] for repo in repos if repo.path in self._states]

    def _snapshot_all(self, targets: Sequence[Repo], moment: float) -> list[RepoState]:
        """Snapshot ``targets`` concurrently, preserving their order."""
        if not targets:
            return []

        def snapshot(repo: Repo) -> RepoState:
            return self._snapshot(repo, moment)

        workers = min(self._max_workers, len(targets))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(snapshot, targets))

    def _snapshot(self, repo: Repo, moment: float) -> RepoState:
        """Read one repo, returning an unreadable state rather than raising."""
        try:
            status = self._runner(repo.path, _STATUS_ARGS)
            if status is None:
                return _unreadable(repo, moment)
            snap = parse_porcelain_v2(status)
            subject, head_time = self._head_meta(repo.path)
            edit = newest_mtime(repo.path, snap.dirty_paths)
            own = git_dir(repo.path)
            operation = read_operation(own)
            stashed = count_stashes(repo.main_git_dir or own)
        except (OSError, ValueError):
            return _unreadable(repo, moment)

        return RepoState(
            path=repo.path,
            name=repo.name,
            dormant=repo.dormant,
            readable=True,
            polled_at=moment,
            branch=snap.branch,
            detached=snap.detached,
            head_sha=snap.head_sha,
            head_subject=subject,
            head_time=head_time,
            staged=snap.staged,
            unstaged=snap.unstaged,
            untracked=snap.untracked,
            unmerged=snap.unmerged,
            operation=operation,
            stashed=stashed,
            ahead=snap.ahead,
            behind=snap.behind,
            upstream=snap.upstream,
            dirty_paths=snap.dirty_paths,
            last_edit=edit.at,
            last_edit_capped=edit.capped,
            main_git_dir=repo.main_git_dir,
        )

    def _head_meta(self, root: Path) -> tuple[str | None, int | None]:
        """Return HEAD's subject and commit time, or (None, None).

        A repo with no commits yet has no HEAD, which is a failure of the git
        call rather than an error worth surfacing.
        """
        out = self._runner(root, _HEAD_ARGS)
        if not out:
            return None, None
        line = out.splitlines()[0]
        if "\t" not in line:
            return None, None
        subject, _, stamp = line.partition("\t")
        try:
            return subject or None, int(stamp)
        except ValueError:
            return subject or None, None

    def _forget_absent(self, repos: Sequence[Repo]) -> None:
        """Drop cached readings for repos no longer in the watch list."""
        live = {repo.path for repo in repos}
        for cache in (self._states, self._last_polled):
            for path in [key for key in cache if key not in live]:
                del cache[path]


def _unreadable(repo: Repo, moment: float) -> RepoState:
    """Build the state for a repo git could not read."""
    return RepoState(
        path=repo.path,
        name=repo.name,
        dormant=repo.dormant,
        readable=False,
        polled_at=moment,
        main_git_dir=repo.main_git_dir,
    )


def parse_porcelain_v2(text: str) -> Snapshot:
    """Parse ``git status --porcelain=v2 --branch`` output."""
    branch: str | None = None
    detached = False
    head_sha: str | None = None
    upstream: str | None = None
    ahead = behind = staged = unstaged = untracked = unmerged = 0
    dirty_paths: list[str] = []

    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ")
            if head == "(detached)":
                detached = True
            else:
                branch = head
        elif line.startswith("# branch.oid "):
            oid = line.removeprefix("# branch.oid ")
            head_sha = None if oid == "(initial)" else oid
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            ahead, behind = _parse_ab(line.removeprefix("# branch.ab "))
        elif line.startswith(("1 ", "2 ")):
            xy = line.split(" ", 2)[1]
            staged += int(xy[0] != ".")
            unstaged += int(len(xy) > 1 and xy[1] != ".")
            dirty_paths.append(_entry_path(line))
        elif line.startswith("u "):
            unmerged += 1
            dirty_paths.append(_entry_path(line))
        elif line.startswith("? "):
            untracked += 1
            dirty_paths.append(unquote_path(line[2:]))

    return Snapshot(
        branch=branch,
        detached=detached,
        head_sha=head_sha,
        upstream=upstream,
        ahead=ahead,
        behind=behind,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        unmerged=unmerged,
        dirty_paths=tuple(dirty_paths),
    )


def read_operation(git_directory: Path) -> str:
    """Return the operation halted in ``git_directory``, or :data:`NO_OPERATION`.

    A handful of ``exists`` calls rather than a git call, because this runs per
    repo on the 2s poll and a subprocess there would cost more than the whole
    status read.
    """
    for name, markers in _OPERATION_MARKERS:
        if any((git_directory / marker).exists() for marker in markers):
            return name
    return NO_OPERATION


def count_stashes(git_directory: Path) -> int:
    """Return how many entries are on the stash, read from its reflog.

    A worktree shares the stash with the repo it belongs to, so the caller
    passes the common git directory rather than the worktree's own.
    """
    try:
        log = (git_directory / _STASH_LOG).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return int((git_directory / _STASH_REF).exists())
    return sum(1 for line in log.splitlines() if line.strip())


def state_parts(state: RepoState) -> list[str]:
    """Return the state cell's parts, in the order they need attention.

    The halted operation leads: a repo stopped mid-rebase has to be finished
    before the size of its diff is worth reading. The stash trails, because it
    is work the user put down on purpose.
    """
    parts = [] if state.operation == NO_OPERATION else [state.operation]
    parts += [
        f"{prefix}{count}"
        for prefix, count in (
            ("U", state.unmerged),
            ("S", state.staged),
            ("M", state.unstaged),
            ("?", state.untracked),
        )
        if count
    ]
    if state.stashed:
        parts.append(f"stash {state.stashed}")
    return parts


def _entry_path(line: str) -> str:
    """Return the current path from a ``1``, ``2`` or ``u`` status line.

    The three shapes put the path at a different field index, and a ``2``
    rename line follows it with a tab and the old path.
    """
    fields = _PATH_FIELD_INDEX[line[0]]
    parts = line.split(" ", fields)
    if len(parts) <= fields:
        return ""
    current, _, _ = parts[fields].partition("\t")
    return unquote_path(current)


def unquote_path(field: str) -> str:
    r"""Undo git's C-style quoting of a path holding unusual bytes.

    ``core.quotePath`` is on by default, so ``æ.txt`` arrives as
    ``"\303\246.txt"``. The octal escapes decode to latin-1 code points, whose
    byte values are the UTF-8 git actually wrote.
    """
    if len(field) < 2 or not (field.startswith('"') and field.endswith('"')):
        return field
    try:
        return (
            field[1:-1]
            .encode("ascii")
            .decode("unicode_escape")
            .encode("latin-1")
            .decode("utf-8")
        )
    except (UnicodeDecodeError, UnicodeEncodeError):
        return field[1:-1]


def _parse_ab(field: str) -> tuple[int, int]:
    """Parse a ``+<ahead> -<behind>`` branch.ab field."""
    ahead = behind = 0
    for token in field.split():
        if token.startswith("+"):
            ahead = int(token[1:])
        elif token.startswith("-"):
            behind = int(token[1:])
    return ahead, behind
