"""When a repo was last edited, from the mtimes of the files git reports dirty.

The reflog covers HEAD movements only, so an hour of editing without a commit
leaves no trace in it. That repo is the most interesting row on the dashboard,
so it cannot be the one that sorts last.

The paths come free with the status output the poller already parses, which
makes this one ``stat`` per dirty path and no extra git call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

STAT_CAP = 400
"""Dirty paths stat'ed per repo before the answer is called approximate.

A repo with 50k untracked files would otherwise stall a poll that has to finish
inside a second.
"""


@dataclass(frozen=True, slots=True)
class LastEdit:
    """The newest mtime found, and whether the search was cut short.

    ``capped`` means more paths were dirty than :data:`STAT_CAP`, so ``at`` is
    the newest of those looked at rather than of all of them.
    """

    at: float | None
    capped: bool


def newest_mtime(
    root: Path,
    paths: Sequence[str],
    *,
    cap: int = STAT_CAP,
) -> LastEdit:
    """Return the newest mtime among ``paths``, resolved against ``root``.

    A path that cannot be stat'ed falls back to its parent directory, because a
    deleted file leaves no file to read and bumps the directory instead.
    """
    newest: float | None = None
    for relative in paths[:cap]:
        mtime = _mtime(root / relative)
        if mtime is None or (newest is not None and mtime <= newest):
            continue
        newest = mtime
    return LastEdit(at=newest, capped=len(paths) > cap)


def _mtime(target: Path) -> float | None:
    """Return ``target``'s mtime, or its parent's when it is gone."""
    try:
        return target.stat().st_mtime
    except OSError:
        pass
    try:
        return target.parent.stat().st_mtime
    except OSError:
        return None
