"""The watch list and its current readings, shared by the CLI and the dashboard.

Ties the pieces together: :mod:`cboard2.config` says where to look,
:mod:`cboard2.discovery` finds the repos, :mod:`cboard2.gitstate` reads their
current state, :mod:`cboard2.activity` reads what happened in them and
:mod:`cboard2.remote` reads what their origin knows that they do not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cboard2.activity import ActivityReader
from cboard2.discovery import discover
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

DEFAULT_FEED_LIMIT = 200
"""Activity entries returned by :meth:`Board.activity` unless asked otherwise."""

RESCAN_INTERVAL = 30.0
"""Seconds between walks of the roots, so a clone or a deletion is noticed.

The walk costs about 0.3s over ``~/git``, too much for every 2s poll and
cheap enough twice a minute.
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

    @property
    def active_at(self) -> float:
        """Newest of the last HEAD movement and the last working-tree edit.

        Falls back to HEAD's commit time, which is all a repo has when its
        reflog is disabled and its tree is clean.
        """
        known = [at for at in (self.moved_at, self.state.last_edit) if at is not None]
        return max(known) if known else float(self.state.head_time or 0)


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
        self._scanned_at: float | None = None

    @property
    def repos(self) -> list[Repo]:
        """The repos found by the last scan."""
        return self._repos

    def rescan(self, *, now: float | None = None) -> list[Repo]:
        """Walk the roots again and drop readings for repos that went away."""
        self._repos = discover(self.config)
        self._scanned_at = time.time() if now is None else now
        self._reader.forget_absent(self._repos)
        self._remote.forget_absent(self._repos)
        return self._repos

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
        what an explicit refresh key asks for. Without either, the roots are
        walked on the first call and then every interval, so a repo cloned or
        deleted while a dashboard is open is picked up.

        The remote reading is re-derived against the local refs here, never
        re-fetched: a pull has to clear the behind marker on the next poll
        rather than at the end of the network interval.
        """
        moment = time.time() if now is None else now
        if rescan or self._due_for_scan(moment):
            self.rescan(now=moment)
        states = self._poller.poll(self._repos, now=now, force=force)
        self._remote.refresh_local(self._repos)
        by_path = {repo.path: repo for repo in self._repos}
        self._reader.prime(self._repos)
        rows = [
            Row(
                state=state,
                moved_at=self._reader.latest(by_path[state.path]),
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

        Walks the roots first when nothing has yet. The dashboard starts this
        worker alongside its first poll, and without the walk it would read an
        empty list and then sit out its whole interval before trying again.
        """
        if not self.config.remote:
            return False
        moment = time.time() if now is None else now
        if not self._repos:
            self.rescan(now=moment)
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
