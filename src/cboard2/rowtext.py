"""Row-cell text, as plain strings, for both surfaces.

Textual-free on purpose: :mod:`cboard2.cli` calls these on the ``ls`` and
``busy`` path, which must not pay the ~0.2s Textual import (see
:mod:`cboard2.app`). :mod:`cboard2.tui` wraps each string in a styled
:class:`rich.text.Text`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cboard2.gitstate import state_parts
from cboard2.remote import ORIGIN, checks_mark

if TYPE_CHECKING:
    from cboard2.board import Row

_HEAD_SUBJECT_MAX = 40
"""Characters of HEAD's subject a row shows."""


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


def branch_text(row: Row) -> str:
    """Return the branch, or a marker for a detached or unreadable repo."""
    if not row.state.readable:
        return "unreadable"
    if row.state.detached:
        return "(detached)"
    return row.state.branch or "—"


def head_text(row: Row) -> str:
    """Return HEAD's subject, truncated to fit the column."""
    subject = row.state.head_subject
    if not subject:
        return "—"
    if len(subject) <= _HEAD_SUBJECT_MAX:
        return subject
    return subject[: _HEAD_SUBJECT_MAX - 1] + "…"


def state_text(row: Row) -> str:
    """Return the halted operation, the dirty counts and the stash depth.

    The counts are ``U`` conflicted, ``S`` staged, ``M`` unstaged and ``?``
    untracked.
    """
    return " ".join(state_parts(row.state)) or "clean"


def ahead_behind_text(row: Row) -> str:
    """Return ``+ahead -behind`` against upstream, or a dash for neither."""
    state = row.state
    if state.ahead == 0 and state.behind == 0:
        return "—"
    parts = [
        f"{sign}{count}"
        for sign, count in (("+", state.ahead), ("-", state.behind))
        if count
    ]
    return " ".join(parts)


def remote_text(row: Row) -> str:
    """Return which branch has commits on the origin this clone has not pulled.

    The checked-out branch is reported ahead of the default branch, because it
    is the one the user is standing on, and a merged pull request on it
    outranks both behind markers. ``?`` and ``—`` are different answers: the
    first means no remote read covered this repo, the second that it is
    current.
    """
    remote = row.remote
    if remote.branch_merged_pr is not None:
        return f"PR #{remote.branch_merged_pr.number} merged"
    if remote.behind_branch:
        return f"behind {ORIGIN}/{remote.branch_remote}"
    if not remote.default_known:
        return "?"
    if remote.behind_default:
        return f"behind {remote.default_branch}"
    return "—"


def pr_text(row: Row, worst: str) -> str:
    """Return the user's own open PRs and how many wait on their review.

    The two are separate counts because they ask for different work: one is
    theirs to land, the other is somebody else's to unblock.
    """
    remote = row.remote
    if not (remote.prs_known or remote.review_prs_known):
        return "?"
    parts = [part for part in (own_pr_text(row, worst), review_pr_text(row)) if part]
    return "  ".join(parts) or "—"


def own_pr_text(row: Row, worst: str) -> str:
    """Return the user's open PR count, its drafts and ``worst`` as a glyph.

    ``worst`` is :func:`cboard2.remote.worst_checks` over ``row.remote.prs``,
    taken from the caller because the dashboard also colors the cell by it and
    this runs once per visible row per render.
    """
    remote = row.remote
    if not remote.prs:
        return ""
    label = str(len(remote.prs))
    drafts = remote.draft_count
    if drafts:
        label += f" ({drafts} draft{'s' if drafts > 1 else ''})"
    mark = checks_mark(worst)
    return f"{label} {mark}" if mark else label


def review_pr_text(row: Row) -> str:
    """Return how many PRs are waiting on the user's review here."""
    count = len(row.remote.review_prs)
    return f"{count} to review" if count else ""
