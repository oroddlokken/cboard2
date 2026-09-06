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
from cboard2.remote import PR_SEARCH_LIMIT, worst_checks
from cboard2.rowtext import (
    ahead_behind_text,
    branch_text,
    head_text,
    pr_text,
    relative,
    remote_text,
    state_text,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from cboard2.board import Row
    from cboard2.remote import MergedPR, PullRequest, RemoteState

_BUSY_DEFAULT = 30.0
"""Seconds ``busy`` looks back over when no window is given."""

_TRUNCATED_NOTE = (
    f"PR search hit its {PR_SEARCH_LIMIT}-result limit; some PRs are missing."
)
"""Printed under the table when a search dropped results."""

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
        sys.stderr.write(f"cboard: {exc}\n")
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
        return cmd_json(board, since, limit=args.limit)
    return cmd_ls(board, since, remote=remote)


def duration(text: str) -> float:
    """Parse a ``--since`` value.

    Wrapped so argparse names ``duration`` in its usage error instead of
    tracebacking out of :func:`cboard2.duration.parse_duration`.
    """
    return parse_duration(text)


def row_limit(text: str) -> int:
    """Parse a ``--limit`` value, rejecting zero and below.

    Raises :class:`ValueError`, which argparse reports as a usage error.
    """
    value = int(text)
    if value < 1:
        msg = f"limit must be 1 or more: {text!r}"
        raise ValueError(msg)
    return value


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser. A bare ``cboard`` opens the dashboard."""
    parser = argparse.ArgumentParser(
        prog="cboard",
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
        if name == "json":
            child.add_argument(
                "--limit",
                metavar="N",
                type=row_limit,
                help="print at most N rows; without it every row is printed",
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


def cmd_json(
    board: Board,
    since: float | None,
    *,
    now: float | None = None,
    limit: int | None = None,
) -> int:
    """Print the rows as JSON, one object per repo.

    The ``remote`` object is always present. Without ``--remote`` its
    ``default_known`` and ``prs_known`` read false, so a consumer parses one
    shape either way and can tell unknown from current.

    ``limit`` keeps the first N rows, which are the most recently active ones.

    A family emits its PR arrays once: ``remote.prs_row`` names the path of the
    row carrying them, and ``remote.prs`` and ``remote.review_prs`` read null on
    every other row of that family. A repo with no worktrees names itself there
    and carries its own arrays.
    """
    moment = time.time() if now is None else now
    rows = _select(board, since, moment)
    if limit is not None:
        rows = rows[:limit]
    sys.stdout.write(json.dumps(_payload(rows), indent=2) + "\n")
    return 0


def _payload(rows: Sequence[Row]) -> list[dict[str, object]]:
    """Return the rows JSON-ready, with each family's PR arrays on one row.

    The holder is the first row of the family to survive the window and the
    limit, so a cut never leaves a row pointing at an absent one: the rows
    arrive grouped by family and a cut keeps a prefix.
    """
    holders: dict[Path, str] = {}
    payload: list[dict[str, object]] = []
    for row in rows:
        holder = holders.setdefault(row.state.family, str(row.state.path))
        payload.append(as_dict(row, prs_row=holder))
    return payload


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


def as_dict(row: Row, *, prs_row: str | None = None) -> dict[str, object]:
    """Return one row as JSON-ready fields.

    ``prs_row`` is the path of the row carrying this family's PR arrays, and
    defaults to this row's own path.
    """
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
        "remote": _remote_dict(row, prs_row or str(state.path)),
    }


def _remote_dict(row: Row, prs_row: str) -> dict[str, object]:
    """Return the remote fields for one row, JSON-ready.

    The PR arrays read null unless ``prs_row`` is this row's path: the worktrees
    of a repo share one reading, and repeating it per row multiplied the payload
    by the size of the family.
    """
    remote = row.remote
    holder = prs_row == str(row.state.path)
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
        "branch_merged_pr": _merged_dict(remote.branch_merged_pr),
        "prs_row": prs_row,
        "prs_known": remote.prs_known,
        "prs": [_pr_dict(pr) for pr in remote.prs] if holder else None,
        "prs_truncated": _flag(remote, "prs_truncated"),
        "review_prs_known": remote.review_prs_known,
        "review_prs": [_pr_dict(pr) for pr in remote.review_prs] if holder else None,
        "review_prs_truncated": _flag(remote, "review_prs_truncated"),
    }


def truncated(row: Row) -> bool:
    """Return True when either PR search dropped results at its limit."""
    return _flag(row.remote, "prs_truncated") or _flag(
        row.remote,
        "review_prs_truncated",
    )


def _flag(remote: RemoteState, name: str) -> bool:
    """Read a truncation flag, false on a reading that carries none.

    Read through :func:`getattr` because a cache written before ``remote.py``
    tracked the search limit has neither attribute.
    """
    return bool(getattr(remote, name, False))


def _merged_dict(pr: MergedPR | None) -> dict[str, object] | None:
    """Return the merged PR of the checked-out branch, or None when there is none."""
    if pr is None:
        return None
    return {
        "number": pr.number,
        "title": pr.title,
        "url": pr.url,
        "merged_at": pr.merged_at,
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

    A PR search that hit its limit adds a note under the table: the limit is on
    the search, not on one repo, so no column can carry it.
    """
    headers = _HEADERS[:-1] + _REMOTE_HEADERS + _HEADERS[-1:] if remote else _HEADERS
    table = [headers, *(_cells(row, now, remote=remote) for row in rows)]
    widths = [max(len(cell[index]) for cell in table) for index in range(len(headers))]
    text = "\n".join(
        "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(line)).rstrip()
        for line in table
    )
    if any(truncated(row) for row in rows):
        return f"{text}\n{_TRUNCATED_NOTE}"
    return text


def _cells(row: Row, now: float, *, remote: bool) -> tuple[str, ...]:
    """Return one row's cells, in the same order as the headers."""
    leading = (
        row.state.row_label,
        branch_text(row),
        head_text(row),
        state_text(row),
        ahead_behind_text(row),
    )
    middle = ()
    if remote:
        middle = (remote_text(row), pr_text(row, worst_checks(row.remote.prs)))
    return (*leading, *middle, relative(now - row.active_at))


def _select(board: Board, since: float | None, now: float) -> list[Row]:
    """Refresh and return the rows inside the window, newest first."""
    rows = board.refresh(now=now)
    if since is None:
        return rows
    cutoff = now - since
    return [row for row in rows if row.active_at >= cutoff]
