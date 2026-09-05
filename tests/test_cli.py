"""Tests for the ls / json / busy subcommands."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from conftest import git

from cboard2.board import Board
from cboard2.cli import (
    build_parser,
    cmd_busy,
    cmd_json,
    cmd_ls,
    format_table,
    relative,
)
from cboard2.config import Config
from cboard2.remote import RemoteReader
from cboard2.remotecache import load as load_cache
from cboard2.remotecache import save as save_cache

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

NOW = 1_800_000_000.0
"""A fixed clock, for the tests that only check rendering."""


def _now() -> float:
    """Real time, for the tests whose window has to contain a just-made commit."""
    return time.time()


def _board(root: Path, *, remote: bool = False) -> Board:
    config = Config(
        roots=(root,),
        max_depth=4,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=remote,
        remote_interval=300.0,
        worktrees=True,
    )
    return Board(config)


class _RecordingGh:
    """A gh runner that records every call and answers from a script."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        """Record the call and answer by which gh command it is."""
        self.calls.append(tuple(args))
        return self.answers.get(args[0])


def _remote_board(
    root: Path,
    gh: _RecordingGh,
    *,
    cache: Path | None = None,
) -> Board:
    """Return a board whose remote reads go to ``gh`` instead of the network."""
    config = Config(
        roots=(root,),
        max_depth=4,
        dormant=(),
        dormant_interval=4 * 3600.0,
        remote=True,
        remote_interval=300.0,
        worktrees=True,
    )
    reader = RemoteReader(
        300.0,
        gh=gh,
        load=None if cache is None else lambda: load_cache(cache),
        save=None if cache is None else (lambda cached: save_cache(cache, cached)),
    )
    return Board(config, remote=reader)


def test_ls_prints_a_row_per_repo(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cmd_ls(_board(git_repo.parent), None, now=NOW) == 0

    lines = capsys.readouterr().out.splitlines()

    assert lines[0].split() == [
        "NAME",
        "BRANCH",
        "HEAD",
        "STATE",
        "AHEAD/BEHIND",
        "ACTIVE",
    ]
    assert len(lines) == 2
    assert git_repo.name in lines[1]
    assert "main" in lines[1]
    assert "clean" in lines[1]


def test_json_matches_ls_on_the_same_window(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board = _board(git_repo.parent)
    moment = _now()
    assert cmd_json(board, 3600.0, now=moment) == 0
    payload = json.loads(capsys.readouterr().out)

    assert cmd_ls(board, 3600.0, now=moment) == 0
    table = capsys.readouterr().out

    assert [entry["name"] for entry in payload] == [str(git_repo.name)]
    assert payload[0]["path"] == str(git_repo)
    assert payload[0]["branch"] == "main"
    assert payload[0]["readable"] is True
    assert payload[0]["name"] in table


def test_json_window_excludes_an_older_repo(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cmd_json(_board(git_repo.parent), 3600.0, now=_now() + 86400 * 30) == 0

    assert json.loads(capsys.readouterr().out) == []


def test_busy_exits_zero_inside_the_window(git_repo: Path) -> None:
    assert cmd_busy(_board(git_repo.parent), 86400.0, now=_now()) == 0


def test_busy_exits_one_outside_the_window(git_repo: Path) -> None:
    assert cmd_busy(_board(git_repo.parent), 30.0, now=_now() + 86400 * 30) == 1


def test_dirty_and_ahead_columns_render(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (git_repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    (git_repo / "loose.txt").write_text("new\n", encoding="utf-8")
    git(git_repo, "add", "loose.txt")

    assert cmd_ls(_board(git_repo.parent), None, now=NOW) == 0

    row = capsys.readouterr().out.splitlines()[1]

    assert "S1" in row
    assert "M1" in row


def test_a_long_name_does_not_break_the_columns(
    tree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in ("a", "a-name-long-enough-to-widen-the-first-column"):
        repo = tree / name
        repo.mkdir()
        git(repo, "init", "-b", "main", "-q")

    assert cmd_ls(_board(tree), None, now=NOW) == 0

    lines = capsys.readouterr().out.splitlines()
    starts = {
        line.index("unreadable" if "unreadable" in line else "main")
        for line in lines[1:]
    }

    assert len(starts) == 1


def test_unreadable_repo_is_labelled(tree: Path) -> None:
    (tree / "broken").mkdir()
    (tree / "broken" / ".git").mkdir()

    rows = _board(tree).refresh(now=NOW)

    assert format_table(rows, NOW).splitlines()[1].split()[1] == "unreadable"


@pytest.mark.parametrize("text", ["42", "30s", "5m", "2h", "1d"])
def test_since_accepts_every_duration_form(text: str) -> None:
    args = build_parser().parse_args(["ls", "--since", text])

    assert args.since > 0


def test_since_rejects_nonsense_with_a_usage_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["ls", "--since", "5x"])

    assert exit_info.value.code == 2
    assert "--since" in capsys.readouterr().err


def test_a_bare_invocation_selects_the_dashboard() -> None:
    assert build_parser().parse_args([]).command is None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "just now"),
        (4.9, "just now"),
        (42.0, "42s ago"),
        (300.0, "5m ago"),
        (7200.0, "2h ago"),
        (172800.0, "2d ago"),
        (-10.0, "just now"),
    ],
)
def test_relative_renders_an_age(seconds: float, expected: str) -> None:
    assert relative(seconds) == expected


def _graphql(branch: str, oid: str, tip: str | None = None) -> str:
    """Build a one-repo answer, with ``tip`` as the ``b0`` branch lookup."""
    entry: dict[str, object] = {
        "defaultBranchRef": {"name": branch, "target": {"oid": oid}},
    }
    if tip is not None:
        entry["b0"] = {"target": {"oid": tip}}
    return json.dumps({"data": {"r0": entry}})


def test_bare_ls_leaves_the_remote_columns_out(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _RecordingGh({})
    board = _remote_board(git_repo.parent, gh)

    assert cmd_ls(board, None, now=NOW) == 0

    header = capsys.readouterr().out.splitlines()[0].split()
    assert "REMOTE" not in header
    assert "PR" not in header
    assert gh.calls == []


def test_remote_flag_adds_the_two_columns(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    gh = _RecordingGh(
        {
            "api": _graphql("main", "f" * 40),
            "search": json.dumps(
                [
                    {
                        "number": 12,
                        "title": "A change",
                        "url": "https://github.com/acme/repo/pull/12",
                        "isDraft": True,
                        "updatedAt": "2026-09-04T12:00:35Z",
                        "repository": {
                            "name": "repo",
                            "nameWithOwner": "acme/repo",
                        },
                    },
                ],
            ),
        },
    )
    board = _remote_board(git_repo.parent, gh)
    board.rescan()
    assert board.read_remote(force=True) is True

    assert cmd_ls(board, None, now=NOW, remote=True) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "REMOTE" in lines[0].split()
    assert "behind main" in lines[1]
    assert "1 (1 draft)" in lines[1]
    assert [args[0] for args in gh.calls] == ["api", "search"]


def test_the_remote_column_names_a_branch_behind_its_origin_copy(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    git(git_repo, "checkout", "-q", "-b", "fix")
    gh = _RecordingGh({"api": _graphql("main", "f" * 40, "e" * 40), "search": "[]"})
    board = _remote_board(git_repo.parent, gh)
    board.rescan()
    assert board.read_remote(force=True) is True

    assert cmd_ls(board, None, now=NOW, remote=True) == 0

    assert "behind origin/fix" in capsys.readouterr().out.splitlines()[1]


def test_a_disabled_remote_config_makes_no_call(git_repo: Path) -> None:
    gh = _RecordingGh({})
    board = _board(git_repo.parent)
    board._remote = RemoteReader(gh=gh)  # noqa: SLF001 — no public seam for this
    board.rescan()

    assert board.read_remote(force=True) is False
    assert gh.calls == []


def test_json_always_carries_the_remote_object(
    git_repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cmd_json(_board(git_repo.parent), None, now=NOW) == 0

    rows = json.loads(capsys.readouterr().out)

    assert rows[0]["remote"] == {
        "origin": None,
        "slug": None,
        "default_branch": None,
        "default_sha": None,
        "default_known": False,
        "behind_default": False,
        "branch_remote": None,
        "branch_sha": None,
        "branch_known": False,
        "behind_branch": False,
        "prs_known": False,
        "prs": [],
    }


def test_the_parser_defaults_both_remote_flags_off() -> None:
    parser = build_parser()

    args = parser.parse_args(["ls"])

    assert args.remote is False
    assert args.refresh is False
    assert parser.parse_args(["ls", "--refresh"]).refresh is True
    assert parser.parse_args(["busy"]).remote is False
    assert parser.parse_args(["busy"]).refresh is False


def test_a_warm_cache_serves_ls_remote_with_no_gh_call(
    git_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    cache = tmp_path / "remote.json"
    head = git(git_repo, "rev-parse", "HEAD").strip()

    first = _RecordingGh(
        {
            "api": _graphql("main", head),
            "search": json.dumps([]),
        },
    )
    board = _remote_board(git_repo.parent, first, cache=cache)
    assert board.read_remote() is True
    assert [args[0] for args in first.calls] == ["api", "search"]

    second = _RecordingGh({})
    warm = _remote_board(git_repo.parent, second, cache=cache)

    assert warm.read_remote() is True
    assert second.calls == []
    assert cmd_ls(warm, None, now=NOW, remote=True) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "REMOTE" in lines[0].split()
    assert "behind" not in lines[1]


def test_refresh_ignores_a_warm_cache(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    cache = tmp_path / "remote.json"
    head = git(git_repo, "rev-parse", "HEAD").strip()
    answers = {"api": _graphql("main", head), "search": json.dumps([])}

    board = _remote_board(git_repo.parent, _RecordingGh(answers), cache=cache)
    board.read_remote()

    forced = _RecordingGh(answers)
    again = _remote_board(git_repo.parent, forced, cache=cache)

    assert again.read_remote(force=True) is True
    assert [args[0] for args in forced.calls] == ["api", "search"]


def test_a_pull_clears_behind_from_the_cache_alone(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    """The cache stores the remote tip, never the derived behind marker."""
    git(git_repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    cache = tmp_path / "remote.json"
    ahead_sha = git(git_repo, "rev-parse", "HEAD").strip()

    # A read taken while local main sat one commit back.
    (git_repo / "tracked.txt").write_text("two\n", encoding="utf-8")
    git(git_repo, "commit", "-qam", "Move main forward")
    moved = git(git_repo, "rev-parse", "HEAD").strip()
    gh = _RecordingGh({"api": _graphql("main", moved), "search": json.dumps([])})
    board = _remote_board(git_repo.parent, gh, cache=cache)
    board.read_remote()
    assert board.refresh(now=NOW)[0].remote.behind_default is False

    # A cache written when the remote was ahead of this clone.
    git(git_repo, "reset", "-q", "--hard", ahead_sha)
    cold = _remote_board(git_repo.parent, _RecordingGh({}), cache=cache)

    assert cold.read_remote() is True
    assert cold.refresh(now=NOW)[0].remote.behind_default is True


def test_ls_and_json_name_the_repo_a_worktree_belongs_to(
    git_repo: Path,
    worktree: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    board = _board(git_repo.parent)

    assert cmd_ls(board, None, now=_now()) == 0
    printed = capsys.readouterr().out.splitlines()

    assert [line for line in printed if line.startswith(f"  ⑂ {worktree.name}")]
    assert [line for line in printed if line.startswith(git_repo.name)]

    assert cmd_json(board, None, now=_now()) == 0
    rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)}

    assert rows[worktree.name]["worktree_of"] == str(git_repo / ".git")
    assert rows[worktree.name]["branch"] == "side"
    assert rows[git_repo.name]["worktree_of"] is None
