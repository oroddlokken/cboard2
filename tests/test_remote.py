"""Tests for the remote read: branch staleness and the user's own open PRs."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cboard2.constants import REMOTE_MAX_WORKERS
from cboard2.discovery import Repo
from cboard2.remote import (
    ANCESTRY_LIMIT,
    CHECKS_FAILING,
    CHECKS_NONE,
    CHECKS_PASSING,
    CHECKS_PENDING,
    CHECKS_UNKNOWN,
    PR_SEARCH_LIMIT,
    UNKNOWN,
    Cached,
    PullRequest,
    RemoteReader,
    RemoteState,
    branch_variables,
    build_query,
    checks_mark,
    github_slugs,
    origin_key,
    parse_branch_tips,
    parse_defaults,
    parse_heads,
    parse_merged_prs,
    parse_prs,
    parse_ref_shas,
    parse_slug,
    parse_symref,
    parse_upstreams,
    parse_worktrees,
    ref_mark,
    run_gh,
    target_branch,
    worst_checks,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

NOW = 1_800_000_000.0
"""A fixed clock, so the interval gate is exercised without sleeping."""

REMOTE_SHA = "a" * 40
LOCAL_SHA = "b" * 40

SSH_ORIGIN = "git@git.example.com:acme/one.git"
"""An origin no GitHub call can answer for."""

SYMREF = f"ref: refs/heads/trunk\tHEAD\n{REMOTE_SHA}\tHEAD\n"

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
    """A gh runner returning canned output for the three calls the reader makes.

    ``review`` defaults to an empty result rather than to ``prs``: most tests
    are about the authored search, and echoing it back would put the same PR
    on both lists.
    """

    def __init__(
        self,
        *,
        graphql: str | None,
        prs: str | None,
        review: str | None = "[]",
    ) -> None:
        self.graphql = graphql
        self.prs = prs
        self.review = review
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        """Record the call and answer by which gh command it is."""
        self.calls.append(tuple(args))
        if args[0] == "api":
            return self.graphql
        return self.review if "--review-requested=@me" in args else self.prs


def _repo(root: Path, name: str) -> Repo:
    return Repo(path=root / name, name=name, dormant=False)


def _worktree(root: Path, name: str, main: Repo) -> Repo:
    return Repo(
        path=root / name,
        name=name,
        dormant=False,
        main_git_dir=main.path / ".git",
    )


def _heads(*rows: tuple[str, str, str, str]) -> str:
    """Build ``for-each-ref`` output: branch, sha, upstream remote and its ref."""
    return "".join(
        f"{name}\t{sha}\t{remote}\t{ref}\n" for name, sha, remote, ref in rows
    )


def _listing(*entries: tuple[Path, str | None]) -> str:
    """Build ``worktree list --porcelain`` output; a None branch reads as detached."""
    return "\n".join(
        f"worktree {path}\nHEAD {LOCAL_SHA}\n"
        + (f"branch refs/heads/{branch}\n" if branch else "detached\n")
        for path, branch in entries
    )


def _graphql_refs(default: tuple[str, str] | None, **refs: str) -> str:
    """Build a one-repo response: its default branch and its ``b<n>`` ref tips."""
    entry: dict[str, object] = {
        "defaultBranchRef": None
        if default is None
        else {"name": default[0], "target": {"oid": default[1]}},
    }
    for alias, oid in refs.items():
        entry[alias] = {"target": {"oid": oid}}
    return json.dumps({"data": {"r0": entry}})


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


def _graphql_entry(
    default: tuple[str, str] | None = ("main", REMOTE_SHA),
    **fields: object,
) -> str:
    """Build a one-repo response carrying its default branch and extra aliases."""
    entry: dict[str, object] = {
        "defaultBranchRef": None
        if default is None
        else {"name": default[0], "target": {"oid": default[1]}},
        **fields,
    }
    return json.dumps({"data": {"r0": entry}})


def _rollup(state: str | None) -> dict[str, object]:
    """Build one ``pullRequest`` entry's commits-and-rollup nesting."""
    return {
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": None if state is None else {"state": state}
                    }
                },
            ],
        },
    }


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


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:owner/name.git", "github.com/owner"),
        ("https://github.com/owner/name", "github.com/owner"),
        ("https://github.com/owner/name/", "github.com/owner"),
        ("https://gitlab.com/owner/name.git", "gitlab.com/owner"),
        ("ssh://git@gitlab.com:2222/owner/name.git", "gitlab.com/owner"),
        ("https://github.com/OWNER/Name", "github.com/owner"),
        ("  git@github.com:owner/name.git\n", "github.com/owner"),
        ("git@github.com:name.git", "github.com/name"),  # a user's own top-level repo
        ("nas:git/name.git", "nas"),  # an ssh alias, whose path is a directory
        ("git@nas.local:git/name.git", "nas.local/git"),
        ("/srv/mirrors/name.git", "local"),
        ("file:///srv/mirrors/name.git", "local"),
        ("", None),
    ],
)
def test_origin_key_names_a_host_and_owner(url: str, expected: str | None) -> None:
    assert origin_key(url) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (SYMREF, ("trunk", REMOTE_SHA)),
        (
            f"ref: refs/heads/feature/x\tHEAD\n{REMOTE_SHA}\tHEAD\n",
            ("feature/x", REMOTE_SHA),
        ),
        (f"{REMOTE_SHA}\tHEAD\n", None),  # a server that sends no symref line
        ("ref: refs/heads/trunk\tHEAD\n", None),
        (f"ref: refs/pull/7/head\tHEAD\n{REMOTE_SHA}\tHEAD\n", None),
        ("", None),
        ("fatal: could not read from remote repository\n", None),
    ],
)
def test_parse_symref_needs_both_the_branch_and_the_tip(
    text: str,
    expected: tuple[str, str] | None,
) -> None:
    assert parse_symref(text) == expected


def test_github_slugs_keeps_only_the_hosts_the_query_can_answer() -> None:
    origins = [
        "https://github.com/acme/two.git",
        "git@github.com:acme/one.git",
        SSH_ORIGIN,
        "https://gitlab.com/acme/three.git",
    ]

    assert github_slugs(origins) == ["acme/one", "acme/two"]


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


def test_refresh_local_before_any_read_reads_only_the_origin(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"for-each-ref": f"main\t{LOCAL_SHA}\n"}})
    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None))

    reader.refresh_local([repo])

    assert [args[0] for _, args in git.calls] == ["remote"]
    assert reader.cached(repo.path).behind_default is False


def test_refresh_local_names_the_origin_before_any_network_read(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "git@github.com:acme/one.git\n"}})
    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None))

    reader.refresh_local([repo])
    reader.refresh_local([repo])

    assert reader.cached(repo.path).origin == "git@github.com:acme/one.git"
    assert [args[0] for _, args in git.calls] == ["remote"]  # asked once, then memoized


def test_an_origin_that_answers_nothing_is_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://gitlab.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(), prs="[]")
    reader = RemoteReader(runner=git, ls_remote=FakeGit({}), gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.origin == "https://gitlab.com/acme/one.git"
    assert state.slug is None
    assert state.default_known is False
    assert state.behind_default is False


def test_a_repo_with_no_origin_at_all_is_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    reader = RemoteReader(
        runner=FakeGit({}),
        ls_remote=FakeGit({}),
        gh=FakeGh(graphql=_graphql(), prs="[]"),
    )
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.origin is None
    assert state.prs_known is False


def test_an_origin_off_github_gets_its_default_branch_from_ls_remote(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": f"{SSH_ORIGIN}\n",
                "for-each-ref": f"trunk\t{LOCAL_SHA}\n",
                "merge-base": None,  # local trunk does not contain the remote tip
            },
        },
    )
    probe = FakeGit({repo.path: {"ls-remote": SYMREF}})
    reader = RemoteReader(
        runner=git, ls_remote=probe, gh=FakeGh(graphql=None, prs=None)
    )

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.origin == SSH_ORIGIN
    assert state.slug is None
    assert state.default_branch == "trunk"
    assert state.default_sha == REMOTE_SHA
    assert state.default_known is True
    assert state.behind_default is True


def test_an_origin_off_github_reports_no_prs_rather_than_unread(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": f"{SSH_ORIGIN}\n"}})
    probe = FakeGit({repo.path: {"ls-remote": SYMREF}})
    reader = RemoteReader(
        runner=git, ls_remote=probe, gh=FakeGh(graphql=None, prs=None)
    )
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.prs == ()
    assert state.prs_known is True


def test_a_github_origin_is_never_asked_over_the_network_twice(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    probe = FakeGit({repo.path: {"ls-remote": SYMREF}})
    reader = RemoteReader(
        runner=git,
        ls_remote=probe,
        gh=FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]"),
    )
    reader.read([repo], NOW)

    assert probe.calls == []
    assert reader.cached(repo.path).default_branch == "main"


def test_a_worktree_costs_its_family_one_ls_remote(tmp_path: Path) -> None:
    main = _repo(tmp_path, "one")
    linked = _worktree(tmp_path, "one-wt", main)
    git = FakeGit({main.path: {"remote": f"{SSH_ORIGIN}\n"}})
    probe = FakeGit({main.path: {"ls-remote": SYMREF}})
    reader = RemoteReader(
        runner=git,
        ls_remote=probe,
        gh=FakeGh(graphql=None, prs=None),
    )
    reader.read([main, linked], NOW)

    assert len(probe.calls) == 1
    assert reader.cached(linked.path).default_branch == "trunk"


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
    assert len(gh.calls) == 3

    assert reader.read([repo], NOW + 300.0) is True
    assert len(gh.calls) == 6


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
    assert len([args for args in gh.calls if args[0] == "search"]) == 2


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
    assert [args[0] for args in gh.calls] == ["search", "search"]
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
    assert gh.calls[-1][-1].count('name: "one"') == 1
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


def test_parse_heads_ignores_the_upstream_fields() -> None:
    text = _heads(("main", LOCAL_SHA, "origin", "refs/heads/main"))

    assert parse_heads(text) == {"main": LOCAL_SHA}


def test_parse_upstreams_reads_the_remote_and_the_ref_it_tracks() -> None:
    text = _heads(
        ("main", LOCAL_SHA, "origin", "refs/heads/main"),
        ("fix", LOCAL_SHA, "upstream", "refs/heads/fix"),
        ("spike", LOCAL_SHA, "", ""),
    )

    assert parse_upstreams(text) == {
        "main": ("origin", "refs/heads/main"),
        "fix": ("upstream", "refs/heads/fix"),
    }


def test_parse_worktrees_maps_each_path_to_its_branch(tmp_path: Path) -> None:
    text = _listing(
        (tmp_path / "one", "main"),
        (tmp_path / "one-side", "side"),
        (tmp_path / "one-bisect", None),
    )

    assert parse_worktrees(text) == {
        str(tmp_path / "one"): "main",
        str(tmp_path / "one-side"): "side",
    }


def test_parse_ref_shas_reads_only_branch_refs() -> None:
    text = (
        f"ref: refs/heads/main\tHEAD\n"
        f"{REMOTE_SHA}\tHEAD\n"
        f"{REMOTE_SHA}\trefs/heads/fix\n"
        f"{LOCAL_SHA}\trefs/tags/v1\n"
    )

    assert parse_ref_shas(text) == {"fix": REMOTE_SHA}


@pytest.mark.parametrize(
    ("branch", "upstream", "expected"),
    [
        ("fix", None, "fix"),
        ("fix", ("origin", "refs/heads/fix"), "fix"),
        ("fix", ("origin", "refs/heads/their-fix"), "their-fix"),
        ("fix", ("upstream", "refs/heads/fix"), None),
        ("fix", ("origin", "refs/pull/7/head"), None),
        (None, None, None),
    ],
)
def test_target_branch_follows_the_upstream_only_on_origin(
    branch: str | None,
    upstream: tuple[str, str] | None,
    expected: str | None,
) -> None:
    assert target_branch(branch, upstream) == expected


def test_build_query_binds_each_branch_to_a_variable() -> None:
    pairs = [("acme/one", "fix"), ("acme/two", 'odd"name')]

    query = build_query(("acme/one", "acme/two"), pairs)

    assert query.startswith(
        "query($b0: String!, $h0: String!, $b1: String!, $h1: String!) {",
    )
    assert "b0: ref(qualifiedName: $b0)" in query
    assert "b1: ref(qualifiedName: $b1)" in query
    assert "m0: pullRequests(headRefName: $h0, states: MERGED" in query
    assert "m1: pullRequests(headRefName: $h1, states: MERGED" in query
    assert 'odd"name' not in query
    assert branch_variables(pairs) == (
        "-f",
        "b0=refs/heads/fix",
        "-f",
        "h0=fix",
        "-f",
        'b1=refs/heads/odd"name',
        "-f",
        'h1=odd"name',
    )


def test_build_query_without_branches_is_the_query_it_always_was() -> None:
    assert build_query(("acme/one",)) == build_query(("acme/one",), [])


def test_parse_branch_tips_round_trips_a_built_query() -> None:
    slugs = ("acme/one",)
    pairs = [("acme/one", "fix"), ("acme/one", "gone")]
    text = _graphql_refs(("main", LOCAL_SHA), b0=REMOTE_SHA)

    assert parse_branch_tips(text, slugs, pairs) == {"acme/one": {"fix": REMOTE_SHA}}


def _merged_node(number: int) -> dict[str, object]:
    """Build one ``pullRequests`` connection carrying a single merged PR."""
    return {
        "nodes": [
            {
                "number": number,
                "title": f"Change {number}",
                "url": f"https://github.com/acme/one/pull/{number}",
                "mergedAt": "2027-01-14T09:00:00Z",
            },
        ],
    }


def _merged_graphql(number: int = 12) -> str:
    """Build a one-repo answer whose asked-about branch carries a merged PR."""
    return _graphql_entry(
        ("main", REMOTE_SHA),
        b0={"target": {"oid": REMOTE_SHA}},
        m0=_merged_node(number),
    )


def test_parse_merged_prs_reads_the_pr_of_each_branch_that_has_one() -> None:
    slugs = ("acme/one",)
    pairs = [("acme/one", "fix"), ("acme/one", "live")]
    text = _graphql_entry(m0=_merged_node(12), m1={"nodes": []})

    found = parse_merged_prs(text, slugs, pairs)

    assert set(found["acme/one"]) == {"fix"}
    merged = found["acme/one"]["fix"]
    assert merged.number == 12
    assert merged.url == "https://github.com/acme/one/pull/12"
    assert merged.merged_at is not None


def test_a_branch_whose_pr_merged_reports_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    gh = FakeGh(graphql=_merged_graphql(), prs="[]")
    reader = RemoteReader(runner=_branch_git(repo, merge_base=""), gh=gh)

    assert reader.read([repo], NOW) is True

    merged = reader.cached(repo.path).branch_merged_pr
    assert merged is not None
    assert merged.number == 12
    assert merged.title == "Change 12"


def test_a_checkout_of_the_default_branch_reports_no_merged_pr(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    gh = FakeGh(graphql=_merged_graphql(), prs="[]")
    reader = RemoteReader(runner=_branch_git(repo, branch="main", merge_base=""), gh=gh)

    reader.read([repo], NOW)

    assert reader.cached(repo.path).branch_merged_pr is None


def test_moving_off_the_merged_branch_clears_it_before_the_next_read(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    answers: dict[Path, dict[str, str | None]] = {
        repo.path: {
            "remote": "https://github.com/acme/one.git\n",
            "worktree": _listing((repo.path, "fix")),
            "for-each-ref": _heads(
                ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                ("fix", LOCAL_SHA, "origin", "refs/heads/fix"),
            ),
            "merge-base": "",
        },
    }
    gh = FakeGh(graphql=_merged_graphql(), prs="[]")
    reader = RemoteReader(runner=FakeGit(answers), gh=gh)
    reader.read([repo], NOW)
    assert reader.cached(repo.path).branch_merged_pr is not None

    answers[repo.path]["worktree"] = _listing((repo.path, "main"))
    reader.refresh_local([repo])

    assert reader.cached(repo.path).branch_merged_pr is None


def _branch_git(
    repo: Repo, *, branch: str = "fix", merge_base: str | None = None
) -> FakeGit:
    """Build a runner for a repo sitting on ``branch`` with ``main`` current."""
    return FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, branch)),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                    (branch, LOCAL_SHA, "origin", f"refs/heads/{branch}"),
                ),
                "merge-base": merge_base,
            },
        },
    )


def test_a_branch_missing_the_origin_tip_is_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo)  # merge-base fails: the branch lacks the origin tip
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.branch == "fix"
    assert state.branch_remote == "fix"
    assert state.branch_sha == REMOTE_SHA
    assert state.branch_known is True
    assert state.behind_branch is True
    assert state.behind_default is False


def test_a_branch_ahead_of_the_origin_is_not_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo, merge_base="")  # the branch already holds the origin tip
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_branch is False
    assert reader.cached(repo.path).branch_known is True


def test_a_branch_level_with_the_origin_asks_no_merge_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, "fix")),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                    ("fix", REMOTE_SHA, "origin", "refs/heads/fix"),
                ),
            },
        },
    )
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_branch is False
    assert [args[0] for _, args in git.calls].count("merge-base") == 0


def test_a_branch_the_origin_does_not_have_is_not_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo, branch="spike")
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.branch_remote == "spike"
    assert state.branch_sha is None
    assert state.branch_known is True
    assert state.behind_branch is False


def test_a_renamed_upstream_is_asked_about_under_the_origins_name(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, "fix")),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                    ("fix", LOCAL_SHA, "origin", "refs/heads/their-fix"),
                ),
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert "b0=refs/heads/their-fix" in gh.calls[-1]
    state = reader.cached(repo.path)
    assert state.branch_remote == "their-fix"
    assert state.behind_branch is True


def test_a_branch_tracking_another_remote_is_left_unanswered(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, "fix")),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                    ("fix", LOCAL_SHA, "upstream", "refs/heads/fix"),
                ),
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.branch == "fix"
    assert state.branch_remote is None
    assert state.branch_known is False
    assert state.behind_branch is False
    assert "b0" not in gh.calls[0][3]


def test_a_detached_checkout_has_no_branch_answer(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, None)),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main")
                ),
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.branch is None
    assert state.branch_known is False
    assert state.behind_branch is False


def test_a_checkout_of_the_default_branch_keeps_the_default_answer(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo, branch="main")
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.branch == "main"
    assert state.branch_remote is None
    assert state.branch_known is False
    assert state.behind_branch is False
    assert state.behind_default is True  # the answer the default column carries


def test_an_origin_that_could_not_be_read_leaves_the_branch_unknown(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo)
    reader = RemoteReader(runner=git, gh=FakeGh(graphql=None, prs=None))
    reader.read([repo], NOW)

    state = reader.cached(repo.path)
    assert state.default_known is False
    assert state.branch_known is False
    assert state.behind_branch is False


def test_an_offsite_origin_is_asked_about_the_branch_in_the_same_call(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": f"{SSH_ORIGIN}\n",
                "worktree": _listing((repo.path, "fix")),
                "for-each-ref": _heads(
                    ("trunk", REMOTE_SHA, "origin", "refs/heads/trunk"),
                    ("fix", LOCAL_SHA, "origin", "refs/heads/fix"),
                ),
                "merge-base": None,
            },
        },
    )
    probe = FakeGit(
        {repo.path: {"ls-remote": f"{SYMREF}{REMOTE_SHA}\trefs/heads/fix\n"}},
    )
    reader = RemoteReader(
        runner=git,
        ls_remote=probe,
        gh=FakeGh(graphql=None, prs=None),
    )
    reader.read([repo], NOW)

    assert len(probe.calls) == 1
    assert probe.calls[0][1][-1] == "refs/heads/fix"
    state = reader.cached(repo.path)
    assert state.default_branch == "trunk"
    assert state.branch_remote == "fix"
    assert state.behind_branch is True


def test_a_checkout_that_moved_since_the_read_drops_the_branch_answer(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    answers = {
        repo.path: {
            "remote": "https://github.com/acme/one.git\n",
            "worktree": _listing((repo.path, "fix")),
            "for-each-ref": _heads(
                ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                ("fix", LOCAL_SHA, "origin", "refs/heads/fix"),
            ),
            "merge-base": None,
        },
    }
    git = FakeGit(answers)
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).behind_branch is True

    answers[repo.path]["worktree"] = _listing((repo.path, "other"))
    reader.refresh_local([repo])

    state = reader.cached(repo.path)
    assert state.branch == "other"
    assert state.branch_remote is None
    assert state.branch_known is False
    assert state.behind_branch is False


def test_two_worktrees_ask_about_both_branches_in_one_query(tmp_path: Path) -> None:
    main = _repo(tmp_path, "one")
    side = _worktree(tmp_path, "one-side", main)
    git = FakeGit(
        {
            main.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((main.path, "fix"), (side.path, "spike")),
                "for-each-ref": _heads(
                    ("main", REMOTE_SHA, "origin", "refs/heads/main"),
                    ("fix", LOCAL_SHA, "origin", "refs/heads/fix"),
                    ("spike", LOCAL_SHA, "", ""),
                ),
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(
        graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA, b1=REMOTE_SHA),
        prs="[]",
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([main, side], NOW)

    assert "b0=refs/heads/fix" in gh.calls[-1]
    assert "b1=refs/heads/spike" in gh.calls[-1]
    assert [args[0] for _, args in git.calls].count("worktree") == 1
    assert reader.cached(main.path).branch_remote == "fix"
    assert reader.cached(side.path).branch_remote == "spike"
    assert reader.cached(side.path).behind_branch is True


def test_the_branch_tips_reach_the_cache(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo)
    gh = FakeGh(graphql=_graphql_refs(("main", REMOTE_SHA), b0=REMOTE_SHA), prs="[]")
    saved: list[Cached] = []
    reader = RemoteReader(
        runner=git, gh=gh, save=lambda cached: bool(saved.append(cached))
    )
    reader.read([repo], NOW)

    assert dict(saved[0].branches) == {"acme/one": {"fix": REMOTE_SHA}}


def test_prime_serves_the_stored_branch_tips(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = _branch_git(repo)
    stored = Cached(
        read_at=NOW - 60.0,
        defaults={"acme/one": ("main", REMOTE_SHA)},
        branches={"acme/one": {"fix": REMOTE_SHA}},
    )
    reader = RemoteReader(
        300.0,
        runner=git,
        gh=FakeGh(graphql=None, prs=None),
        load=lambda: stored,
    )

    assert reader.prime([repo]) is True

    state = reader.cached(repo.path)
    assert state.branch_remote == "fix"
    assert state.behind_branch is True


def test_a_review_requested_pr_lands_on_its_own_list(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("SUCCESS")),
        prs="[]",
        review=json.dumps([_pr("acme/one", 12, title="Their change")]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    state = reader.cached(repo.path)

    assert state.prs == ()
    assert state.prs_known is True
    assert [pr.number for pr in state.review_prs] == [12]
    assert state.review_prs[0].title == "Their change"
    assert state.review_prs_known is True


def test_a_failing_rollup_reaches_the_pull_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("FAILURE")),
        prs=json.dumps([_pr("acme/one", 4)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    state = reader.cached(repo.path)

    assert state.prs[0].checks == CHECKS_FAILING
    assert worst_checks(state.prs) == CHECKS_FAILING
    assert checks_mark(CHECKS_FAILING) == "✗"


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ("SUCCESS", CHECKS_PASSING),
        ("FAILURE", CHECKS_FAILING),
        ("ERROR", CHECKS_FAILING),
        ("PENDING", CHECKS_PENDING),
        ("EXPECTED", CHECKS_PENDING),
        (None, CHECKS_NONE),
    ],
)
def test_every_rollup_state_maps_to_one_checks_value(
    tmp_path: Path,
    rollup: str | None,
    expected: str,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup(rollup)),
        prs=json.dumps([_pr("acme/one", 4)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)

    assert reader.cached(repo.path).prs[0].checks == expected


def test_a_repo_holding_both_kinds_keeps_them_apart(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("SUCCESS"), c1=_rollup("FAILURE")),
        prs=json.dumps([_pr("acme/one", 4)]),
        review=json.dumps([_pr("acme/one", 9)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    state = reader.cached(repo.path)

    assert [pr.number for pr in state.prs] == [4]
    assert [pr.number for pr in state.review_prs] == [9]
    assert state.prs[0].checks == CHECKS_PASSING
    assert state.review_prs[0].checks == CHECKS_FAILING


def test_the_query_asks_about_each_found_pr_once(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("SUCCESS")),
        prs=json.dumps([_pr("acme/one", 4)]),
        review=json.dumps([_pr("acme/one", 4), _pr("other/repo", 8)]),
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    query = gh.calls[-1][-1]

    assert query.count("pullRequest(number: 4)") == 1
    assert "pullRequest(number: 8)" not in query


def test_a_failed_query_leaves_the_prs_listed_with_unknown_checks(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(graphql=None, prs=json.dumps([_pr("acme/one", 4)]))
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    state = reader.cached(repo.path)

    assert [pr.number for pr in state.prs] == [4]
    assert state.prs[0].checks == CHECKS_UNKNOWN


def test_a_failed_review_search_is_unknown_without_blanking_the_others(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("SUCCESS")),
        prs=json.dumps([_pr("acme/one", 4)]),
        review=None,
    )
    reader = RemoteReader(runner=git, gh=gh)
    reader.read([repo], NOW)
    state = reader.cached(repo.path)

    assert state.prs_known is True
    assert state.review_prs_known is False
    assert state.review_prs == ()


def test_a_cache_from_before_review_prs_reads_as_unknown(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    stale = Cached(
        read_at=NOW,
        defaults={"acme/one": ("main", REMOTE_SHA)},
        prs={"acme/one": (_pr_object(4),)},
        prs_known=True,
    )
    reader = RemoteReader(
        runner=git,
        gh=FakeGh(graphql=None, prs=None),
        load=lambda: stale,
    )

    assert reader.prime([repo]) is True
    state = reader.cached(repo.path)

    assert [pr.number for pr in state.prs] == [4]
    assert state.prs[0].checks == CHECKS_UNKNOWN
    assert state.review_prs == ()
    assert state.review_prs_known is False


def test_both_pr_lists_are_stored_for_the_next_process(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql_entry(c0=_rollup("PENDING"), c1=_rollup("SUCCESS")),
        prs=json.dumps([_pr("acme/one", 4)]),
        review=json.dumps([_pr("acme/one", 9)]),
    )
    saved: list[Cached] = []
    reader = RemoteReader(
        runner=git, gh=gh, save=lambda cached: bool(saved.append(cached)) or True
    )
    reader.read([repo], NOW)

    assert saved[-1].prs_known is True
    assert saved[-1].review_prs_known is True
    assert [pr.number for pr in saved[-1].review_prs["acme/one"]] == [9]
    assert saved[-1].prs["acme/one"][0].checks == CHECKS_PENDING


def test_worst_checks_ranks_the_state_worth_acting_on_first() -> None:
    failing = replace(_pr_object(1), checks=CHECKS_FAILING)
    pending = replace(_pr_object(2), checks=CHECKS_PENDING)
    passing = replace(_pr_object(3), checks=CHECKS_PASSING)

    assert worst_checks([passing, pending, failing]) == CHECKS_FAILING
    assert worst_checks([passing, pending]) == CHECKS_PENDING
    assert worst_checks([passing]) == CHECKS_PASSING
    assert worst_checks([]) == CHECKS_UNKNOWN
    assert checks_mark(CHECKS_UNKNOWN) == ""
    assert checks_mark(CHECKS_NONE) == ""


def test_a_read_landing_between_the_patch_and_the_fold_keeps_its_reading(
    tmp_path: Path,
) -> None:
    """A poll patch computed before a read folds onto the read, not over it."""
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
    reader = RemoteReader(runner=git, gh=gh)

    patches = reader._adopt_origins([repo])  # noqa: SLF001 — the poll thread's half
    assert reader.read([repo], NOW) is True  # the remote thread's rebuild
    reader._fold(patches)  # noqa: SLF001 — folded after the swap

    state = reader.cached(repo.path)
    assert state.origin == "https://github.com/acme/one.git"
    assert state.default_sha == REMOTE_SHA
    assert [pr.number for pr in state.prs] == [4]


class _SwappingStates(dict[Path, RemoteState]):
    """A states dict handing the reader a rebuilt one the moment it is iterated.

    Stands in for a remote read finishing inside ``forget_absent``.
    """

    def __init__(
        self,
        reader: RemoteReader,
        rebuilt: dict[Path, RemoteState],
    ) -> None:
        super().__init__()
        self.reader = reader
        self.rebuilt = rebuilt

    def __iter__(self) -> Iterator[Path]:
        self.reader._states = self.rebuilt  # noqa: SLF001 — the read landing mid-call
        return super().__iter__()


def test_forget_absent_survives_a_read_landing_mid_call(tmp_path: Path) -> None:
    """A rebuild between the snapshot and the deletes keeps its live entry."""
    live = _repo(tmp_path, "one")
    gone = _repo(tmp_path, "two")
    reader = RemoteReader(runner=FakeGit({}), gh=FakeGh(graphql=None, prs=None))
    fresh = RemoteState(
        origin="https://github.com/acme/one.git",
        default_sha=REMOTE_SHA,
        default_known=True,
    )
    swapping = _SwappingStates(reader, {live.path: fresh})
    swapping[live.path] = RemoteState()
    swapping[gone.path] = RemoteState(origin="https://github.com/acme/two.git")
    reader._states = swapping  # noqa: SLF001 — the dict the poll thread captures

    reader.forget_absent([live])

    assert reader.cached(live.path) == fresh
    assert reader.cached(gone.path) is UNKNOWN


def test_the_ancestry_memo_stops_growing(tmp_path: Path) -> None:
    """A repo that keeps advancing while behind does not grow the memo forever."""
    repo = _repo(tmp_path, "one")
    local = f"{0:040x}"

    def git(_root: Path, args: Sequence[str]) -> str | None:
        if args[0] == "remote":
            return "https://github.com/acme/one.git\n"
        if args[0] == "for-each-ref":
            return f"main\t{local}\n"
        if args[0] == "merge-base":
            return None
        return ""

    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(runner=git, gh=gh)
    assert reader.read([repo], NOW) is True

    for step in range(1, ANCESTRY_LIMIT * 2):
        local = f"{step:040x}"
        reader.refresh_local([repo])

    assert len(reader._ancestry) == ANCESTRY_LIMIT  # noqa: SLF001 — the ceiling


def test_prime_reads_the_local_refs_once(tmp_path: Path) -> None:
    """The stored read derives its behind markers from the refs it already read."""
    repo = _repo(tmp_path, "one")
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/one.git\n",
                "worktree": _listing((repo.path, "main")),
                "for-each-ref": _heads(
                    ("main", LOCAL_SHA, "origin", "refs/heads/main")
                ),
                "merge-base": None,
            },
        },
    )
    stored = Cached(read_at=NOW - 60.0, defaults={"acme/one": ("main", REMOTE_SHA)})
    reader = RemoteReader(
        runner=git,
        gh=FakeGh(graphql=None, prs=None),
        load=lambda: stored,
    )

    assert reader.prime([repo]) is True

    ran = [args[0] for _, args in git.calls]
    assert ran.count("for-each-ref") == 1
    assert ran.count("worktree") == 1
    assert reader.cached(repo.path).behind_default is True


def test_the_worker_count_matches_the_measurement_in_the_docstring() -> None:
    """The pool runs at the count RemoteReader's docstring measured."""
    assert RemoteReader.__doc__ is not None
    assert f"at {REMOTE_MAX_WORKERS} workers" in RemoteReader.__doc__


def test_a_stalled_probe_does_not_hold_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreachable origin is dropped at the deadline; the rest still answer."""
    slow = _repo(tmp_path, "slow")
    quick = _repo(tmp_path, "quick")
    git = FakeGit(
        {
            slow.path: {"remote": "git@git.example.com:acme/slow.git\n"},
            quick.path: {"remote": "git@git.example.com:acme/quick.git\n"},
        },
    )
    released = threading.Event()

    def probe(root: Path, _args: Sequence[str]) -> str | None:
        if root == slow.path:
            released.wait(10.0)
            return None
        return SYMREF

    monkeypatch.setattr("cboard2.remote.PROBE_TIMEOUT", 0.2)
    reader = RemoteReader(
        runner=git,
        ls_remote=probe,
        gh=FakeGh(graphql=None, prs=None),
    )

    started = time.monotonic()
    try:
        assert reader.read([slow, quick], NOW) is True
        elapsed = time.monotonic() - started
    finally:
        released.set()

    assert elapsed < 2.0
    assert reader.cached(quick.path).default_branch == "trunk"
    assert reader.cached(slow.path).default_known is False


def test_the_independent_read_phases_overlap(tmp_path: Path) -> None:
    """The two local git batches and the two PR searches run at once.

    The barrier only clears when all four are in flight, so a sequential read
    breaks it rather than passing. ``worktree list`` is left out: it shares a
    pool task with ``for-each-ref`` and runs after it, so waiting on it would
    hold a barrier the other three have already cleared.
    """
    repo = _repo(tmp_path, "one")
    at_once = threading.Barrier(4)

    def git(_root: Path, args: Sequence[str]) -> str | None:
        if args[0] in {"remote", "for-each-ref"}:
            at_once.wait(timeout=10.0)
        if args[0] == "remote":
            return "https://github.com/acme/one.git\n"
        return ""

    def gh(args: Sequence[str]) -> str | None:
        if args[0] == "search":
            at_once.wait(timeout=10.0)
            return "[]"
        return _graphql(("main", REMOTE_SHA))

    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True
    assert reader.cached(repo.path).default_sha == REMOTE_SHA


@pytest.mark.parametrize(
    ("updated", "expected"),
    [
        ("2026-09-04T12:00:35Z", UPDATED_EPOCH),
        (
            "2026-09-04T14:00:35+02:00",
            datetime(2026, 9, 4, 12, 0, 35, tzinfo=UTC).timestamp(),
        ),
        ("2026-09-04T12:00:35", None),
    ],
)
def test_a_pr_timestamp_needs_an_offset(updated: str, expected: float | None) -> None:
    """An offset-less string is dropped rather than read in the host's zone."""
    entry = _pr("acme/one", 4)
    entry["updatedAt"] = updated

    found = parse_prs(json.dumps([entry]))

    assert found is not None
    assert found["acme/one"][0].updated_at == expected


def _search_results(count: int) -> str:
    """Build a search result set of ``count`` PRs, all on one repo."""
    return json.dumps([_pr("acme/one", number) for number in range(1, count + 1)])


@pytest.mark.parametrize(
    ("count", "expected"),
    [(PR_SEARCH_LIMIT, True), (PR_SEARCH_LIMIT - 1, False)],
    ids=["at-the-limit", "under-it"],
)
def test_a_search_at_the_limit_reports_its_prs_truncated(
    tmp_path: Path,
    count: int,
    *,
    expected: bool,
) -> None:
    """A result set of exactly PR_SEARCH_LIMIT is the only signal gh gives."""
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs=_search_results(count))
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.prs_truncated is expected
    assert state.review_prs_truncated is False


def test_the_review_search_reports_its_own_truncation(tmp_path: Path) -> None:
    """The two searches hit the limit separately, and carry separate flags."""
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(
        graphql=_graphql(("main", REMOTE_SHA)),
        prs="[]",
        review=_search_results(PR_SEARCH_LIMIT),
    )
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.prs_truncated is False
    assert state.review_prs_truncated is True


def test_a_failed_search_reports_no_truncation(tmp_path: Path) -> None:
    """A search that never answered found nothing to cut short."""
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs=None, review=None)
    reader = RemoteReader(runner=git, gh=gh)

    assert reader.read([repo], NOW) is True

    state = reader.cached(repo.path)
    assert state.prs_known is False
    assert state.prs_truncated is False
    assert state.review_prs_truncated is False


def test_prime_serves_the_stored_truncation_flags(tmp_path: Path) -> None:
    """A restart inside the interval keeps saying the PR list is short."""
    repo = _repo(tmp_path, "one")
    git = FakeGit({repo.path: {"remote": "https://github.com/acme/one.git\n"}})
    stored = Cached(
        read_at=NOW - 60.0,
        defaults={"acme/one": ("main", REMOTE_SHA)},
        prs={"acme/one": (_pr_object(4),)},
        prs_known=True,
        prs_truncated=True,
    )
    reader = RemoteReader(
        runner=git,
        gh=FakeGh(graphql=None, prs=None),
        load=lambda: stored,
    )

    assert reader.prime([repo]) is True

    state = reader.cached(repo.path)
    assert state.prs_truncated is True
    assert state.review_prs_truncated is False


def test_an_unchanged_second_poll_runs_no_local_git_batch(git_repo: Path) -> None:
    """The gate this guards: an idle watch list must not respawn git every 2s."""
    repo = Repo(path=git_repo, name="repo", dormant=False)
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/repo.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "worktree": _listing((repo.path, "main")),
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(300.0, runner=git, gh=gh)
    reader.read([repo], NOW)
    reader.refresh_local([repo])
    settled = len(git.calls)

    reader.refresh_local([repo])

    assert len(git.calls) == settled


def test_a_moved_ref_reopens_the_gate(git_repo: Path) -> None:
    repo = Repo(path=git_repo, name="repo", dormant=False)
    git = FakeGit(
        {
            repo.path: {
                "remote": "https://github.com/acme/repo.git\n",
                "for-each-ref": f"main\t{LOCAL_SHA}\n",
                "worktree": _listing((repo.path, "main")),
                "merge-base": None,
            },
        },
    )
    gh = FakeGh(graphql=_graphql(("main", REMOTE_SHA)), prs="[]")
    reader = RemoteReader(300.0, runner=git, gh=gh)
    reader.read([repo], NOW)
    reader.refresh_local([repo])
    settled = len(git.calls)

    # What a commit or a pull leaves behind: refs/heads/main written again.
    head = git_repo / ".git" / "refs" / "heads" / "main"
    head.write_text(f"{REMOTE_SHA}\n", encoding="utf-8")
    os.utime(head, (time.time() + 5, time.time() + 5))
    reader.refresh_local([repo])

    assert len(git.calls) > settled


def test_a_family_with_no_git_directory_is_never_gated(tmp_path: Path) -> None:
    """A path the mark cannot stat has to run both batches every poll."""
    assert ref_mark(tmp_path / "missing" / ".git") is None
