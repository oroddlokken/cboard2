"""The watch list and its current readings, shared by the CLI and the dashboard.

Ties the pieces together: :mod:`cboard2.config` says where to look,
:mod:`cboard2.discovery` finds the repos, :mod:`cboard2.gitstate` reads their
current state, :mod:`cboard2.activity` reads what happened in them and
:mod:`cboard2.remote` reads what their origin knows that they do not.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from cboard2.activity import ActivityReader
from cboard2.discovery import discover, load_repos, repo_cache_path, save_repos
from cboard2.gitstate import Poller
from cboard2.remote import UNKNOWN, RemoteReader
from cboard2.remotecache import cache_path
from cboard2.remotecache import load as load_cache
from cboard2.remotecache import save as save_cache

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cboard2.activity import Entry
    from cboard2.config import Config
    from cboard2.discovery import Repo
    from cboard2.gitstate import RepoState
    from cboard2.remote import RemoteState

_READERS = 3
"""Readers a tick runs at once: the poller, the remote refresh and the reflogs."""

DEFAULT_FEED_LIMIT = 200
"""Activity entries returned by :meth:`Board.activity` unless asked otherwise."""

RESCAN_INTERVAL = 30.0
"""Seconds between walks of the roots, so a clone or a deletion is noticed.

The walk costs about 0.3s over ``~/git``, too much for every 2s poll and
cheap enough twice a minute. Doubles as the life of the stored repo list, so a
one-shot ``cboard ls`` reuses a walk another process paid for within the window.
"""


@dataclass(frozen=True, slots=True)
class Row:
    """One repo's current state, with when it was last active."""

    state: RepoState
    moved_at: float | None
    remote: RemoteState = UNKNOWN
    """What the last remote read said, or :data:`cboard2.remote.UNKNOWN`.

    Defaulted because a caller testing a renderer has no remote reading to
    supply, and unknown is the honest value there.
    """

    active_at: float = field(init=False)
    """Newest of the last HEAD movement and the last working-tree edit.

    Falls back to HEAD's commit time, which is all a repo has when its reflog
    is disabled and its tree is clean. Computed once here because a sort, a
    filter and two renderers read it within one pass over the same frozen row.
    """

    def __post_init__(self) -> None:
        known = [at for at in (self.moved_at, self.state.last_edit) if at is not None]
        newest = max(known) if known else float(self.state.head_time or 0)
        object.__setattr__(self, "active_at", newest)


class Board:
    """Holds the repo list and the readers, so callers refresh rather than rebuild."""

    def __init__(
        self,
        config: Config,
        *,
        poller: Poller | None = None,
        reader: ActivityReader | None = None,
        remote: RemoteReader | None = None,
    ) -> None:
        self.config = config
        self._poller = poller or Poller(config.dormant_interval)
        self._reader = reader or ActivityReader()
        self._remote = remote or _default_reader(config)
        self._repos: list[Repo] = []
        self._by_path: dict[Path, Repo] = {}
        self._scanned_at: float | None = None
        self._scan_lock = threading.Lock()
        self._scans = 0

    @property
    def repos(self) -> list[Repo]:
        """The repos found by the last scan."""
        return self._repos

    def rescan(self, *, now: float | None = None) -> list[Repo]:
        """Walk the roots again and drop readings for repos that went away."""
        return self._scan(time.time() if now is None else now, stored=False)

    def _scan(self, moment: float, *, stored: bool) -> list[Repo]:
        """Take a fresh repo list, from the cache file when ``stored`` allows it.

        A caller that arrives while a scan is running waits for it and takes
        its result. The dashboard starts its poll thread and its remote thread
        moments apart and both scan on the first tick, so without this the
        roots are walked twice at once.
        """
        seen = self._scans
        with self._scan_lock:
            if self._scans > seen:
                return self._repos
            target = repo_cache_path()
            found = (
                load_repos(target, self.config, moment, RESCAN_INTERVAL)
                if stored
                else None
            )
            if found is None:
                found = discover(self.config)
                save_repos(target, self.config, found, moment)
            self._adopt(found, moment)
            self._scans += 1
            return self._repos

    def _adopt(self, repos: list[Repo], moment: float) -> None:
        """Take ``repos`` as the current list and drop readings for what went away."""
        self._repos = repos
        self._by_path = {repo.path: repo for repo in repos}
        self._scanned_at = moment
        self._reader.forget_absent(repos)
        self._remote.forget_absent(repos)

    def refresh(
        self,
        *,
        force: bool = False,
        rescan: bool = False,
        now: float | None = None,
    ) -> list[Row]:
        """Poll every due repo and return the rows, most recently active first.

        ``force`` polls dormant repos too, ignoring their interval. ``rescan``
        walks the roots without waiting for :data:`RESCAN_INTERVAL`, which is
        what an explicit refresh key asks for. Without either, the repo list is
        taken on the first call and then every interval, so a repo cloned or
        deleted while a dashboard is open is picked up. Only the first call may
        take the list another process stored; every later one walks.

        The remote reading is re-derived against the local refs here, never
        re-fetched: a pull has to clear the behind marker on the next poll
        rather than at the end of the network interval.

        The three readers run at once. None of them reads another's answer,
        and each spends its time waiting on git subprocesses, so the tick
        costs the slowest of the three rather than their sum.
        """
        moment = time.time() if now is None else now
        if rescan:
            self.rescan(now=moment)
        elif self._due_for_scan(moment):
            self._scan(moment, stored=self._scanned_at is None)
        repos = self._repos
        with ThreadPoolExecutor(max_workers=_READERS) as pool:
            states_at = pool.submit(self._poller.poll, repos, now=now, force=force)
            local_at = pool.submit(self._remote.refresh_local, repos)
            primed_at = pool.submit(self._reader.prime, repos)
            states = states_at.result()
            local_at.result()
            primed_at.result()
        rows = [
            Row(
                state=state,
                moved_at=self._reader.latest(self._by_path[state.path]),
                remote=self._remote.cached(state.path),
            )
            for state in states
        ]
        rows.sort(key=lambda row: row.active_at, reverse=True)
        return group_families(rows)

    def _due_for_scan(self, moment: float) -> bool:
        """Return True on the first call, then once per :data:`RESCAN_INTERVAL`."""
        if self._scanned_at is None:
            return True
        return (moment - self._scanned_at) >= RESCAN_INTERVAL

    def read_remote(self, *, force: bool = False, now: float | None = None) -> bool:
        """Bring the remote reading up to date, and report whether anything changed.

        Kept out of :meth:`refresh` because the two calls take seconds while a
        poll takes a fraction of one. The dashboard drives this from its own
        worker so a remote read never stalls the table.

        Loads the stored read first, which is often the whole answer: a cache
        written inside ``remote_interval`` leaves nothing for the network to
        do. ``force`` skips that gate.

        Takes the repo list first when nothing has yet. The dashboard starts
        this worker alongside its first poll, and without it the worker would
        read an empty list and sit out its whole interval before trying again.
        """
        if not self.config.remote:
            return False
        moment = time.time() if now is None else now
        if not self._repos:
            self._scan(moment, stored=self._scanned_at is None)
        primed = self._remote.prime(self._repos)
        return self._remote.read(self._repos, moment, force=force) or primed

    @property
    def remote_read_at(self) -> float | None:
        """When the last remote read finished, or None before the first one."""
        return self._remote.read_at

    def activity(
        self,
        *,
        since: float | None = None,
        limit: int = DEFAULT_FEED_LIMIT,
    ) -> list[Entry]:
        """Return the merged cross-repo activity feed, newest first."""
        return self._reader.feed(self._repos, since=since, limit=limit)

    def entries(self, path: Path, *, limit: int) -> list[Entry]:
        """Return one repo's reflog entries, newest first.

        Kept apart from :meth:`activity` because a caller showing one repo
        would otherwise merge and sort every watched repo's reflog to throw
        all but one away. An unwatched path has no entries.
        """
        repo = self._by_path.get(path)
        if repo is None:
            return []
        return self._reader.entries(repo)[:limit]


def group_families(rows: Sequence[Row]) -> list[Row]:
    """Reorder ``rows`` so a repo and its worktrees stay together.

    The rows arrive in whatever order a sort left them. A family lands where
    its best-placed member was, the repo leads it, and the worktrees keep the
    order they had — a worktree row shows a directory name and nothing else, so
    the repo has to be the row above it.
    """
    grouped: dict[Path, list[Row]] = {}
    for row in rows:
        grouped.setdefault(row.state.family, []).append(row)
    return [row for group in grouped.values() for row in _repo_first(group)]


def _repo_first(group: list[Row]) -> list[Row]:
    """Put the repo above its worktrees, keeping the worktrees' own order."""
    if len(group) == 1:
        return group
    repos = [row for row in group if row.state.main_git_dir is None]
    worktrees = [row for row in group if row.state.main_git_dir is not None]
    return repos + worktrees


def _default_reader(config: Config) -> RemoteReader:
    """Return a reader wired to the cache file on disk."""
    target = cache_path()
    return RemoteReader(
        config.remote_interval,
        load=lambda: load_cache(target),
        save=lambda cached: save_cache(target, cached),
    )
