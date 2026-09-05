"""Non-interactive surface: a table, JSON, and an exit code.

For tmux statuslines, watch-loops and shell guards. ``prune``, ``add`` and
``forget`` from changeboard have no counterpart here: there is no store to
prune, discovery replaces registration, and the ``dormant`` config key replaces
unregistration.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import TYPE_CHECKING

from cboard2.board import Board
from cboard2.config import ConfigError, load_config
from cboard2.duration import parse_duration
from cboard2.gitstate import state_parts
from cboard2.remote import ORIGIN, checks_mark, worst_checks

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cboard2.board import Row
    from cboard2.remote import PullRequest

_BUSY_DEFAULT = 30.0
"""Seconds ``busy`` looks back over when no window is given."""

_HEAD_SUBJECT_MAX = 40
"""Characters of HEAD's subject shown in the ``ls`` table."""

_HEADERS = ("NAME", "BRANCH", "HEAD", "STATE", "AHEAD/BEHIND", "ACTIVE")
_REMOTE_HEADERS = ("REMOTE", "PR")
"""Columns ``--remote`` adds, between AHEAD/BEHIND and ACTIVE."""


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and run the requested subcommand."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"cboard2: {exc}\n")
        return 2

    if args.command is None:
        from cboard2.app import launch  # noqa: PLC0415 — Textual costs ~0.2s to import

        return launch()

    board = Board(config)
    since: float | None = args.since
    remote = args.remote or args.refresh
    if remote:
        board.read_remote(force=args.refresh)
    if args.command == "busy":
        window = _BUSY_DEFAULT if since is None else since
        return cmd_busy(board, window, remote=remote)
    if args.command == "json":
        return cmd_json(board, since)
    return cmd_ls(board, since, remote=remote)


def duration(text: str) -> float:
    """Parse a ``--since`` value.

    Wrapped so argparse names ``duration`` in its usage error instead of
    tracebacking out of :func:`cboard2.duration.parse_duration`.
    """
    return parse_duration(text)


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. A bare ``cboard2`` opens the dashboard."""
    parser = argparse.ArgumentParser(
        prog="cboard2",
        description="Report the git state of every repo under the configured roots.",
    )
    sub = parser.add_subparsers(dest="command")

    for name, help_text in (
        ("ls", "print a table of repos, most recently active first"),
        ("json", "print the same rows as JSON"),
        ("busy", "exit 0 if any repo was active within the window"),
    ):
        child = sub.add_parser(name, help=help_text)
        child.add_argument(
            "--since",
            metavar="DUR",
            type=duration,
            help="only count repos active within this window, e.g. 30s, 5m, 2h, 1d",
        )
        child.add_argument(
            "--remote",
            action="store_true",
            help=(
                "also read whether a branch has been pulled and which PRs are "
                "open, served from the cache when it is younger than "
                "remote_interval; without it nothing is read"
            ),
        )
        child.add_argument(
            "--refresh",
            action="store_true",
            help="ask the origins now, ignoring the cache; implies --remote",
        )
    return parser


def cmd_ls(
    board: Board,
    since: float | None,
    *,
    now: float | None = None,
    remote: bool = False,
) -> int:
    """Print a fixed-width table of repos to stdout."""
    moment = time.time() if now is None else now
    rows = _select(board, since, moment)
    sys.stdout.write(format_table(rows, moment, remote=remote) + "\n")
    return 0


def cmd_json(board: Board, since: float | None, *, now: float | None = None) -> int:
    """Print the rows as JSON, one object per repo.

    The ``remote`` object is always present. Without ``--remote`` its
    ``default_known`` and ``prs_known`` read false, so a consumer parses one
    shape either way and can tell unknown from current.
    """
    moment = time.time() if now is None else now
    rows = _select(board, since, moment)
    sys.stdout.write(json.dumps([as_dict(row) for row in rows], indent=2) + "\n")
    return 0


def cmd_busy(
    board: Board,
    since: float,
    *,
    now: float | None = None,
    remote: bool = False,
) -> int:
    """Exit 0 when any repo is busy, 1 otherwise.

    Local activity inside the window always counts. ``remote`` adds the repos
    the last remote read left behind their origin or holding a pull request,
    which a prompt asks about precisely because they show no local activity.
    """
    moment = time.time() if now is None else now
    rows = board.refresh(now=moment)
    if any(row.active_at >= moment - since for row in rows):
        return 0
    if remote and any(waiting(row) for row in rows):
        return 0
    return 1


def waiting(row: Row) -> bool:
    """Return True when the origin holds something this repo has not dealt with."""
    remote = row.remote
    return bool(
        remote.behind_default
        or remote.behind_branch
        or remote.prs
        or remote.review_prs,
    )


def as_dict(row: Row) -> dict[str, object]:
    """Return one row as JSON-ready fields."""
    state = row.state
    return {
        "path": str(state.path),
        "name": state.name,
        "worktree_of": None if state.main_git_dir is None else str(state.main_git_dir),
        "dormant": state.dormant,
        "readable": state.readable,
        "branch": state.branch,
        "detached": state.detached,
        "head_sha": state.head_sha,
        "head_subject": state.head_subject,
        "head_time": state.head_time,
        "staged": state.staged,
        "unstaged": state.unstaged,
        "untracked": state.untracked,
        "unmerged": state.unmerged,
        "operation": state.operation,
        "stashed": state.stashed,
        "ahead": state.ahead,
        "behind": state.behind,
        "upstream": state.upstream,
        "last_edit": state.last_edit,
        "moved_at": row.moved_at,
        "active_at": row.active_at,
        "polled_at": state.polled_at,
        "remote": _remote_dict(row),
    }


def _remote_dict(row: Row) -> dict[str, object]:
    """Return the remote fields for one row, JSON-ready."""
    remote = row.remote
    return {
        "origin": remote.origin,
        "slug": remote.slug,
        "default_branch": remote.default_branch,
        "default_sha": remote.default_sha,
        "default_known": remote.default_known,
        "behind_default": remote.behind_default,
        "branch_remote": remote.branch_remote,
        "branch_sha": remote.branch_sha,
        "branch_known": remote.branch_known,
        "behind_branch": remote.behind_branch,
        "prs_known": remote.prs_known,
        "prs": [_pr_dict(pr) for pr in remote.prs],
        "review_prs_known": remote.review_prs_known,
        "review_prs": [_pr_dict(pr) for pr in remote.review_prs],
    }


def _pr_dict(pr: PullRequest) -> dict[str, object]:
    """Return one pull request as JSON-ready fields."""
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "draft": pr.draft,
        "updated_at": pr.updated_at,
        "checks": pr.checks,
    }


def format_table(rows: Sequence[Row], now: float, *, remote: bool = False) -> str:
    """Render the rows as columns padded to their widest cell.

    ``remote`` adds the two remote columns. They are left out otherwise
    because a bare ``ls`` makes no network call and would print a column of
    question marks.
    """
    headers = _HEADERS[:-1] + _REMOTE_HEADERS + _HEADERS[-1:] if remote else _HEADERS
    table = [headers, *(_cells(row, now, remote=remote) for row in rows)]
    widths = [max(len(cell[index]) for cell in table) for index in range(len(headers))]
    return "\n".join(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)).rstrip()
        for line in table
    )


def _cells(row: Row, now: float, *, remote: bool) -> tuple[str, ...]:
    """Return one row's cells, in the same order as the headers."""
    leading = (
        row.state.row_label,
        branch_text(row),
        head_text(row),
        state_text(row),
        ab_text(row),
    )
    middle = (remote_text(row), pr_text(row)) if remote else ()
    return (*leading, *middle, relative(now - row.active_at))


def remote_text(row: Row) -> str:
    """Return which branch has commits on the origin this clone has not pulled.

    The checked-out branch is reported ahead of the default branch, because it
    is the one the user is standing on.
    """
    state = row.remote
    if state.behind_branch:
        return f"behind {ORIGIN}/{state.branch_remote}"
    if not state.default_known:
        return "?"
    if state.behind_default:
        return f"behind {state.default_branch}"
    return "—"


def pr_text(row: Row) -> str:
    """Return the user's own open PRs and how many wait on their review.

    The two are separate counts because they ask for different work: one is
    theirs to land, the other is somebody else's to unblock.
    """
    state = row.remote
    if not (state.prs_known or state.review_prs_known):
        return "?"
    parts = [part for part in (own_pr_text(row), review_pr_text(row)) if part]
    return "  ".join(parts) or "—"


def own_pr_text(row: Row) -> str:
    """Return the user's open PR count, its drafts and its worst checks state."""
    state = row.remote
    if not state.prs:
        return ""
    label = str(len(state.prs))
    drafts = state.draft_count
    if drafts:
        label += f" ({drafts} draft{'s' if drafts > 1 else ''})"
    mark = checks_mark(worst_checks(state.prs))
    return f"{label} {mark}" if mark else label


def review_pr_text(row: Row) -> str:
    """Return how many PRs are waiting on the user's review here."""
    count = len(row.remote.review_prs)
    return f"{count} to review" if count else ""


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


def ab_text(row: Row) -> str:
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


def _select(board: Board, since: float | None, now: float) -> list[Row]:
    """Refresh and return the rows inside the window, newest first."""
    rows = board.refresh(now=now)
    if since is None:
        return rows
    cutoff = now - since
    return [row for row in rows if row.active_at >= cutoff]
