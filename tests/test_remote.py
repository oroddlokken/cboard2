"""Tests for the GitHub read: default-branch staleness and the user's own PRs."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from cboard2.discovery import Repo
from cboard2.remote import (
    Cached,
    PullRequest,
    RemoteReader,
    build_query,
    parse_defaults,
    parse_heads,
    parse_prs,
    parse_slug,
    run_gh,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

NOW = 1_800_000_000.0
"""A fixed clock, so the interval gate is exercised without sleeping."""

REMOTE_SHA = "a" * 40
LOCAL_SHA = "b" * 40

UPDATED_AT = "2026-09-04T12:00:35Z"
"""The ``updatedAt`` every canned search result carries."""

UPDATED_EPOCH = datetime(2026, 9, 4, 12, 0, 35, tzinfo=UTC).timestamp()


class FakeGit:
    """A git runner answering per repo path and git subcommand.

    A path with no entry, or a subcommand with no entry, returns None — which
    every caller treats as a failed git call.
    """

    def __init__(self, answers: Mapping[Path, Mapping[str, str | None]]) -> None:
        self.answers = answers
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, root: Path, args: Sequence[str]) -> str | None:
        """Record the call and answer for ``root``'s entry, or None."""
        self.calls.append((root, tuple(args)))
        return self.answers.get(root, {}).get(args[0])


class FakeGh:
    """A gh runner returning canned output for the two calls the reader makes."""

    def __init__(self, *, graphql: str | None, prs: str | None) -> None:
        self.graphql = graphql
        self.prs = prs
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        """Record the call and answer by which gh command it is."""
        self.calls.append(tuple(args))
        return self.graphql if args[0] == "api" else self.prs


def _repo(root: Path, name: str) -> Repo:
    return Repo(path=root / name, name=name, dormant=False)


def _worktree(root: Path, name: str, main: Repo) -> Repo:
    return Repo(
        path=root / name,
        name=name,
        dormant=False,
        main_git_dir=main.path / ".git",
    )


def _graphql(*entries: tuple[str, str] | None) -> str:
    """Build a GraphQL response, using None for a repo that resolved to null."""
    data: dict[str, object] = {}
    for index, entry in enumerate(entries):
        if entry is None:
            data[f"r{index}"] = None
        else:
            branch, oid = entry
            data[f"r{index}"] = {
                "defaultBranchRef": {"name": branch, "target": {"oid": oid}}
            }
    return json.dumps({"data": data})


def _pr_object(number: int, *, draft: bool = False) -> PullRequest:
    """Build a PullRequest directly, for the cache paths that skip gh's JSON."""
    return PullRequest(
        number=number,
        title=f"Change {number}",
        url=f"https://github.com/acme/one/pull/{number}",
        draft=draft,
        updated_at=UPDATED_EPOCH,
    )


def _pr(
    slug: str,
    number: int,
    *,
    draft: bool = False,
    title: str = "Some change",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "url": f"https://github.com/{slug}/pull/{number}",
        "isDraft": draft,
        "updatedAt": UPDATED_AT,
        "repository": {"name": slug.split("/")[1], "nameWithOwner": slug},
    }


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/name.git", "owner/name"),
        ("https://github.com/owner/name", "owner/name"),
        ("https://github.com/owner/name/", "owner/name"),
        ("git@github.com:owner/name.git", "owner/name"),
        ("ssh://git@github.com/owner/name", "owner/name"),
        ("https://github.com/owner/dot.name.git", "owner/dot.name"),
        ("  https://github.com/owner/name.git\n", "owner/name"),
        ("https://gitlab.com/owner/name.git", None),
        ("https://github.example.com/owner/name.git", None),
        ("/srv/mirrors/name.git", None),
    ],
)
def test_parse_slug_names_only_github_origins(url: str, expected: str | None) -> None:
    assert parse_slug(url) == expected


def test_parse_heads_reads_name_and_sha() -> None:
    text = f"main\t{LOCAL_SHA}\nfeature/x\t{REMOTE_SHA}\nbroken-line\n"

    assert parse_heads(text) == {"main": LOCAL_SHA, "feature/x": REMOTE_SHA}


def test_build_query_aliases_each_slug_by_index() -> None:
    query = build_query(("acme/one", "acme/two"))

    assert 'r0: repository(owner: "acme", name: "one")' in query
    assert 'r1: repository(owner: "acme", name: "two")' in query
    assert query.startswith("{")
    assert query.endswith("}")


def test_parse_defaults_round_trips_a_built_query() -> None:
    slugs = ("acme/one", "acme/two")
    text = _graphql(("main", REMOTE_SHA), ("master", LOCAL_SHA))

    assert parse_defaults(text, slugs) == {
        "acme/one": ("main", REMOTE_SHA),
        "acme/two": ("master", LOCAL_SHA),
    }


def test_parse_defaults_drops_a_repo_that_resolved_to_null() -> None:
    slugs = ("acme/gone", "acme/here")
    text = _graphql(None, ("main", REMOTE_SHA))

    assert parse_defaults(text, slugs) == {"acme/here": ("main", REMOTE_SHA)}


@pytest.mark.parametrize("text", ["not json", "[]", '{"errors": []}', ""])
def test_parse_defaults_survives_unusable_output(text: str) -> None:
    assert parse_defaults(text, ("acme/one",)) == {}


def test_parse_prs_groups_by_slug_and_marks_drafts() -> None:
    text = json.dumps(
        [
            _pr("acme/one", 7),
            _pr("acme/one", 9, draft=True),
            _pr("acme/two", 3),
        ],
    )

    grouped = parse_prs(text)

    assert grouped is not None
    assert [pr.number for pr in grouped["acme/one"]] == [9, 7]
    assert [pr.draft for pr in grouped["acme/one"]] == [True, False]
    assert grouped["acme/one"][0].url.endswith("/pull/9")
    assert grouped["acme/one"][0].updated_at == UPDATED_EPOCH
    assert [pr.number for pr in grouped["acme/two"]] == [3]


def test_parse_prs_skips_an_entry_missing_its_repo() -> None:
    text = json.dumps([{"number": 1, "title": "No repo"}, _pr("acme/one", 2)])

    grouped = parse_prs(text)

    assert grouped is not None
    assert list(grouped) == ["acme/one"]
    assert [pr.number for pr in grouped["acme/one"]] == [2]


def test_parse_prs_reports_no_open_prs_as_an_empty_mapping() -> None:
    assert parse_prs("[]") == {}


@pytest.mark.parametrize("text", ["not json", "{}", ""])
def test_parse_prs_reports_unusable_output_as_unknown(text: str) -> None:
    assert parse_prs(text) is None


def test_a_repo_missing_the_remote_tip_is_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "merge-base": None,  # local main does not contain the remote tip
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.slug == "acme/one"
    assert state.default_branch == "main"
    assert state.default_sha == REMOTE_SHA
    assert state.default_known is True
    assert state.behind_default is True


def test_a_repo_level_with_the_remote_is_not_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_default is False
    assert [args[0] for _, args in git.calls].count("merge-base") == 0


def test_a_local_default_branch_ahead_of_the_remote_is_not_behind(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "merge-base": "",  # local main already contains the remote tip
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_default is False


def test_a_repo_without_the_default_branch_locally_is_not_behind(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"feature/x\t{LOCAL_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_default is False


def test_a_pull_clears_behind_without_another_network_call(tmp_path: Path) -> None:
    """The bug this guards: a pull must not wait out the network interval."""
    repo = _repo(tmp_path, "one")
    answers: dict[str, str | None] = {
        "remote": "https://github.com/acme/one.git\n",
        "for-each-ref": f"main\t{LOCAL_SHA}\n",
        "merge-base": None,
    }
    git = FakeGit({repo.path: answers})
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(300.0, runner=git, gh=gh)
    reader.read([repo], NOW)
    assert reader.cached(repo.path).behind_default is True
    calls_after_read = len(gh.calls)

    # The user runs git pull: local main now sits on the remote tip.
    answers["for-each-ref"] = f"main\t{REMOTE_SHA}\n"
    reader.refresh_local([repo])

    assert reader.cached(repo.path).behind_default is False
    assert reader.cached(repo.path).default_sha == REMOTE_SHA
    assert len(gh.calls) == calls_after_read


def test_a_fetch_without_a_merge_stays_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                # The object is in the store, but main's history does not reach it.
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_default is True


def test_an_untouched_repo_costs_no_second_merge_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    once = [args[0] for _, args in git.calls].count("merge-base")

    reader.refresh_local([repo])
    reader.refresh_local([repo])

    assert once == 1
    assert [args[0] for _, args in git.calls].count("merge-base") == 1
    assert reader.cached(repo.path).behind_default is True


def test_refresh_local_before_any_read_does_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"for-each-ref": f"main\t{LOCAL_SHA}\n"}})
    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None))

    reader.refresh_local([repo])

    assert git.calls == []
    assert reader.cached(repo.path).behind_default is False


def test_a_repo_with_no_github_origin_is_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://gitlab.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.slug is None
    assert state.default_known is False
    assert state.prs_known is False
    assert state.behind_default is False


def test_a_repo_the_query_omitted_is_unknown_not_current(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(None), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.default_known is False
    assert state.behind_default is False


def test_prs_reach_the_repo_that_matches_their_slug(tmp_path: Path) -> None:
    mine = _repo(tmp_path, "one")
    theirs = _repo(tmp_path, "two")
    git = FakeGit(
        {
            mine.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
            theirs.path: {
                "remote": "https://github.com/acme/two.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(
        graphql=_graphql(("main", REMOTE_SHA), ("main", REMOTE_SHA)),
        prs=json.dumps([_pr("acme/one", 4), _pr("acme/one", 5, draft=True)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([mine, theirs], NOW)

    assert [pr.number for pr in reader.cached(mine.path).prs] == [5, 4]
    assert reader.cached(mine.path).draft_count == 1
    assert reader.cached(theirs.path).prs == ()
    assert reader.cached(theirs.path).prs_known is True


def test_a_repo_cloned_twice_gets_the_same_prs(tmp_path: Path) -> None:
    first = _repo(tmp_path, "one")
    second = _repo(tmp_path, "one-again")
    answers = {
        "remote": "https://github.com/acme/one.git\n",
        "for-each-ref": f"main\t{REMOTE_SHA}\n",
    }
    git = FakeGit({first.path: answers, second.path: answers})
    gh = FakeGh(
        graphql=_graphql(("main", REMOTE_SHA)),
        prs=json.dumps([_pr("acme/one", 4)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([first, second], NOW)

    assert [pr.number for pr in reader.cached(first.path).prs] == [4]
    assert [pr.number for pr in reader.cached(second.path).prs] == [4]


def test_a_failed_search_leaves_prs_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs=None)
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.prs_known is False
    assert state.default_known is True


def test_gh_returning_nothing_leaves_every_repo_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
            },
        },
    )
    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None))
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.default_known is False
    assert state.prs_known is False
    assert state.behind_default is False


def test_a_second_read_inside_the_interval_makes_no_call(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(300.0, runner=git, gh=gh)

    assert reader.read([repo], NOW) is True
    assert reader.read([repo], NOW + 299.0) is False
    assert len(gh.calls) == 2

    assert reader.read([repo], NOW + 300.0) is True
    assert len(gh.calls) == 4


def test_force_ignores_the_interval(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(300.0, runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.read([repo], NOW + 1.0, force=True) is True
    assert reader.read_at == NOW + 1.0


def test_the_query_is_chunked(tmp_path: Path) -> None:
    repos = [_repo(tmp_path, f"repo{index}") for index in range(70)]
    git = FakeGit(
        {
            repo.path: {"remote": f"https://github.com/acme/{repo.name}.git\n"}
            for repo in repos
        },
    )
    gh = FakeGh(graphql=_graphql(), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read(repos, NOW)

    graphql_calls = [args for args in gh.calls if args[0] == "api"]
    assert len(graphql_calls) == 3
    assert len([args for args in gh.calls if args[0] == "search"]) == 1


def test_forget_absent_drops_a_deleted_repo(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    assert reader.cached(repo.path).slug == "acme/one"

    reader.forget_absent([])

    assert reader.cached(repo.path).slug is None


def test_no_repos_skips_the_default_branch_query() -> None:
    git = FakeGit({})
    gh = FakeGh(graphql=_graphql(), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([], NOW) is True
    assert [args[0] for args in gh.calls] == ["search"]
    assert git.calls == []


def _found(_name: str) -> str:
    """Stand in for ``shutil.which`` finding gh on PATH."""
    return "/usr/bin/gh"


def _absent(_name: str) -> None:
    """Stand in for ``shutil.which`` finding no gh at all."""
    return


def test_run_gh_keeps_stdout_from_a_partly_failed_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _graphql(("main", REMOTE_SHA), None)

    def fake_run(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            args=["gh"],
            returncode=1,
            stdout=payload + "\n",
            stderr="gh: Could not resolve to a Repository\n",
        )

    monkeypatch.setattr("cboard2.remote.shutil.which", _found)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_gh(("api", "graphql")) == payload


def test_run_gh_reports_a_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cboard2.remote.shutil.which", _absent)

    assert run_gh(("api", "graphql")) is None


def test_run_gh_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args
        assert kwargs
        raise subprocess.TimeoutExpired(cmd="gh", timeout=1.0)

    monkeypatch.setattr("cboard2.remote.shutil.which", _found)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_gh(("api", "graphql")) is None


def test_prime_serves_the_stored_read_without_touching_the_network(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    stored = Cached(
        read_at=NOW - 60.0,
        defaults={"acme/one": ("main", REMOTE_SHA)},
        prs={"acme/one": (_pr_object(4),)},
        prs_known=True,
    )
    reader = RemoteReader(300.0, runner=git, gh=gh, load=lambda: stored)

    assert reader.prime([repo]) is True

    state = reader.cached(repo.path)
    assert state.default_known is True
    assert state.behind_default is True
    assert [pr.number for pr in state.prs] == [4]
    assert reader.read_at == NOW - 60.0

    assert reader.read([repo], NOW) is False
    assert gh.calls == []


def test_prime_runs_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    loads: list[int] = []

    def load() -> Cached:
        loads.append(1)
        return Cached(read_at=NOW, defaults={"acme/one": ("main", REMOTE_SHA)})

    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None), load=load)

    assert reader.prime([repo]) is True
    assert reader.prime([repo]) is False
    assert len(loads) == 1


def test_prime_without_a_loader_or_a_file_is_a_no_op(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})

    assert RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None)).prime(
        [repo]
    ) is (False)
    assert (
        RemoteReader(
            runner=git,
            gh=FakeGh(graphql=None, prs=None),
            load=lambda: None,
        ).prime([repo])
        is False
    )
    assert git.calls == []


def test_a_stale_cache_still_lets_the_network_read_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    stored = Cached(read_at=NOW - 301.0, defaults={"acme/one": ("main", LOCAL_SHA)})
    reader = RemoteReader(300.0, runner=git, gh=gh, load=lambda: stored)
    reader.prime([repo])

    assert reader.read([repo], NOW) is True
    assert reader.cached(repo.path).default_sha == REMOTE_SHA


def test_a_read_hands_only_this_read_to_the_saver(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(
        graphql=_graphql(("main", REMOTE_SHA)),
        prs=json.dumps([_pr("acme/one", 4)]),
    )
    saved: list[Cached] = []
    reader = RemoteReader(
        runner=git, gh=gh, save=lambda cached: bool(saved.append(cached))
    )
    reader.read([repo], NOW)

    assert len(saved) == 1
    assert saved[0].read_at == NOW
    assert dict(saved[0].defaults) == {"acme/one": ("main", REMOTE_SHA)}
    assert saved[0].prs_known is True
    assert [pr.number for pr in saved[0].prs["acme/one"]] == [4]


def test_a_failed_search_is_saved_as_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs=None)
    saved: list[Cached] = []
    reader = RemoteReader(
        runner=git, gh=gh, save=lambda cached: bool(saved.append(cached))
    )
    reader.read([repo], NOW)

    assert saved[0].prs_known is False
    assert dict(saved[0].prs) == {}


def test_a_worktree_shares_its_repos_remote_reading(tmp_path: Path) -> None:
    main = _repo(tmp_path, "one")
    side = _worktree(tmp_path, "one-side", main)
    answers = {
        "remote": "https://github.com/acme/one.git\n",
        "for-each-ref": f"main\t{LOCAL_SHA}\n",
        "merge-base": None,  # local main does not contain the remote tip
    }
    git = FakeGit({main.path: answers, side.path: answers})
    gh = FakeGh(
        graphql=_graphql(("main", REMOTE_SHA)),
        prs=json.dumps([_pr("acme/one", 7)]),
    )
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([main, side], NOW) is True

    made = [(root, args[0]) for root, args in git.calls]
    assert made.count((main.path, "remote")) == 1
    assert made.count((main.path, "for-each-ref")) == 1
    assert made.count((main.path, "merge-base")) == 1
    assert [call for call in made if call[0] == side.path] == []
    assert gh.calls[0][-1].count('name: "one"') == 1
    for path in (main.path, side.path):
        state = reader.cached(path)
        assert state.slug == "acme/one"
        assert [pr.number for pr in state.prs] == [7]
        assert state.behind_default is True


def test_a_worktree_without_its_repo_resolves_on_its_own(tmp_path: Path) -> None:
    main = _repo(tmp_path, "one")
    side = _worktree(tmp_path, "one-side", main)
    git = FakeGit(
        {
            side.path: {
                "remote": "https://github.com/acme/one.git\n",
                "for-each-ref": f"main\t{REMOTE_SHA}\n",
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([side], NOW) is True

    assert reader.cached(side.path).slug == "acme/one"
    assert reader.cached(side.path).behind_default is False
