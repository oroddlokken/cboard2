"""The three pool sizes, kept together so a change to one is read against the others.

They are separate numbers, not one shared budget. Two of them size a pool
waiting on local disk; the third sizes a pool waiting on the network. Raising
the network one costs API calls in flight, raising the disk ones costs
subprocesses. They happen to be equal today.
"""

from __future__ import annotations

ACTIVITY_MAX_WORKERS = 16
"""Reflog reads in flight in :class:`cboard2.activity.ActivityReader`."""

GITSTATE_MAX_WORKERS = 16
"""Repos polled at once by :class:`cboard2.gitstate.Poller`."""

REMOTE_MAX_WORKERS = 16
"""Git calls in flight per fan-out in :class:`cboard2.remote.RemoteReader`.

Measured at this count; the class docstring cites the run.
"""
