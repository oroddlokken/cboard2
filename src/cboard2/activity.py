"""Recent activity per repo, read from git's own reflog.

This is what replaces a Claude Code hook. The reflog already records every HEAD
movement with a timestamp and a verb, for every clone on disk, going back as
far as git's expiry settings — so activity is read retroactively rather than
captured as it happens.

Re-reading 100 reflogs on every tick would be waste, so each read is gated on
the mtime of ``logs/HEAD``: a HEAD movement always writes that file, and
nothing else does.

The gate is cold on the first tick, when every repo needs its reflog read, so
those reads run across a thread pool. Measured on the author's machine: 75
repos in 0.12s, against 0.61s one after another.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cboard2.constants import ACTIVITY_MAX_WORKERS
from cboard2.discovery import git_dir
from cboard2.gitstate import run_git

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from cboard2.discovery import Repo
    from cboard2.gitstate import GitRunner

MAX_ENTRIES_PER_REPO = 200
"""Reflog entries read per repo. A repo with more has older ones left unread."""

_REFLOG_ARGS = (
    "reflog",
    f"--max-count={MAX_ENTRIES_PER_REPO}",
    "--date=unix",
    "--format=%gd%x09%gs%x09%h",
)
"""``%gd`` under ``--date=unix`` is the entry's own time, not the commit's."""

_BRANCH_ARGS = (
    "for-each-ref",
    "--sort=-committerdate",
    "refs/heads",
    "--format=%(refname:short)%09%(committerdate:unix)%09%(subject)",
)


@dataclass(frozen=True, slots=True)
class Entry:
    """One HEAD movement in one repo."""

    at: float
    repo_path: Path
    repo_name: str
    verb: str
    detail: str
    sha: str


@dataclass(frozen=True, slots=True)
class Branch:
    """A local branch and the age of its tip."""

    name: str
    committed_at: float
    subject: str


class ActivityReader:
    """Reads reflogs, skipping any repo whose ``logs/HEAD`` has not moved.

    Holds each repo's entries with the mtime they were read at, as one pair, so
    a quiet repo costs one ``stat`` per tick instead of a git process. The pair
    is one dict value because the poll thread and the UI thread both reach this
    cache, and two dicts can be written out of step.
    """

    def __init__(
        self,
        runner: GitRunner = run_git,
        *,
        max_workers: int = ACTIVITY_MAX_WORKERS,
    ) -> None:
        self._runner = runner
        self._max_workers = max_workers
        self._cache: dict[Path, tuple[float, list[Entry]]] = {}

    def prime(self, repos: Sequence[Repo]) -> None:
        """Read the reflogs that moved since last time, all at once.

        :meth:`entries` reads one repo per call, so a caller looping over 75
        repos with a cold cache waits through 75 ``git reflog`` processes in a
        row. Call this first and the loop finds every answer cached.
        """
        stale = [pair for pair in map(self._staleness, repos) if pair is not None]
        if not stale:
            return

        def read(pair: tuple[Repo, float]) -> tuple[Repo, float, list[Entry]]:
            repo, mtime = pair
            return repo, mtime, read_reflog(repo, self._runner)

        workers = min(self._max_workers, len(stale))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(read, stale))

        for repo, mtime, entries in results:
            self._cache[repo.path] = (mtime, entries)

    def _staleness(self, repo: Repo) -> tuple[Repo, float] | None:
        """Return ``repo`` with its ``logs/HEAD`` mtime, or None if it is current.

        None also covers a repo with no reflog at all, which has nothing to read.
        """
        mtime = log_mtime(repo.path)
        cached = self._cache.get(repo.path)
        if mtime is None or (cached is not None and cached[0] == mtime):
            return None
        return repo, mtime

    def entries(self, repo: Repo) -> list[Entry]:
        """Return ``repo``'s reflog entries, newest first, re-reading if it moved."""
        mtime = log_mtime(repo.path)
        if mtime is None:
            return []
        cached = self._cache.get(repo.path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

        entries = read_reflog(repo, self._runner)
        self._cache[repo.path] = (mtime, entries)
        return entries

    def latest(self, repo: Repo) -> float | None:
        """Return when ``repo``'s HEAD last moved, or None if it never has."""
        entries = self.entries(repo)
        return entries[0].at if entries else None

    def feed(
        self,
        repos: Sequence[Repo],
        *,
        since: float | None = None,
        limit: int = 200,
    ) -> list[Entry]:
        """Merge every repo's entries into one newest-first feed.

        ``since`` drops entries older than that timestamp; ``limit`` caps the
        result after the merge, so one busy repo cannot crowd out the rest of a
        short window.
        """
        self.prime(repos)
        merged = [entry for repo in repos for entry in self.entries(repo)]
        if since is not None:
            merged = [entry for entry in merged if entry.at >= since]
        merged.sort(key=lambda entry: entry.at, reverse=True)
        return merged[:limit]

    def forget_absent(self, repos: Iterable[Repo]) -> None:
        """Drop cached reflogs for repos no longer in the watch list."""
        live = {repo.path for repo in repos}
        for path in [key for key in self._cache if key not in live]:
            del self._cache[path]


def read_reflog(repo: Repo, runner: GitRunner = run_git) -> list[Entry]:
    """Read one repo's reflog, newest first.

    A repo with no reflog — a fresh ``git init``, or ``core.logAllRefUpdates``
    turned off — contributes nothing and is not an error.
    """
    out = runner(repo.path, _REFLOG_ARGS)
    if not out:
        return []

    entries: list[Entry] = []
    for line in out.splitlines():
        entry = _parse_line(line, repo)
        if entry is not None:
            entries.append(entry)
    return entries


def branches(root: Path, runner: GitRunner = run_git) -> list[Branch]:
    """Return local branches, most recently committed first."""
    out = runner(root, _BRANCH_ARGS)
    if not out:
        return []

    found: list[Branch] = []
    for line in out.splitlines():
        name, _, rest = line.partition("\t")
        stamp, _, subject = rest.partition("\t")
        try:
            committed_at = float(stamp)
        except ValueError:
            continue
        found.append(Branch(name=name, committed_at=committed_at, subject=subject))
    return found


def last_fetch(root: Path) -> float | None:
    """Return when this repo last fetched, or None if it never has.

    Read from ``FETCH_HEAD``'s mtime because a fetch that moved no ref writes
    no reflog entry at all.
    """
    return _mtime(git_dir(root) / "FETCH_HEAD")


def log_mtime(root: Path) -> float | None:
    """Return the mtime of ``logs/HEAD``, or None when the repo has no reflog."""
    return _mtime(git_dir(root) / "logs" / "HEAD")


def _mtime(path: Path) -> float | None:
    """Return ``path``'s mtime, or None when it cannot be read."""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _parse_line(line: str, repo: Repo) -> Entry | None:
    """Parse one tab-separated ``%gd %gs %h`` reflog line, or None if unusable."""
    selector, _, rest = line.partition("\t")
    message, _, sha = rest.partition("\t")
    at = _selector_time(selector)
    if at is None:
        return None

    verb, detail = split_message(message)
    if verb is None:
        return None
    return Entry(
        at=at,
        repo_path=repo.path,
        repo_name=repo.name,
        verb=verb,
        detail=detail,
        sha=sha,
    )


def _selector_time(selector: str) -> float | None:
    """Pull the unix time out of a ``HEAD@{1788524131}`` selector."""
    _, _, tail = selector.partition("{")
    stamp = tail.removesuffix("}")
    try:
        return float(stamp)
    except ValueError:
        return None


def split_message(message: str) -> tuple[str | None, str]:
    """Split a reflog subject into its verb and the rest.

    The verb is the first word before the colon, which collapses
    ``commit (initial)``, ``commit (amend)`` and ``pull --rebase`` onto
    ``commit``, ``commit`` and ``pull``. It is lowercased because git writes
    ``Branch: renamed ...`` with a capital and every other verb without one.
    """
    head, sep, tail = message.partition(":")
    words = (head if sep else message).split()
    if not words:
        return None, ""
    return words[0].lower(), tail.strip()
