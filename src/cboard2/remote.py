"""A moved default branch, the checked-out branch, and the pull requests in play.

All of it is read from the origin. ``git status --porcelain=v2 --branch``
reports ``behind`` against the local remote-tracking ref, so it stays at zero
until something fetches. This module asks the origin directly instead, and
never fetches: a poll that rewrote ``refs/remotes/origin/*`` under the user
would change what their next ``git log`` shows.

Three network calls cover every repo. Two ``gh search prs`` calls return every
open PR the user authored and every one waiting on their review, anywhere; one
batched GraphQL query then returns each repo's default branch and its tip, the
tip of whatever branch is checked out in each of its worktrees, and the checks
rollup of every PR the searches found on a watched repo. Measured on the
author's machine: 66 repos in one query took 4.4s, against 1.7s per repo for
``git ls-remote`` — which is why the query is batched and why the read runs on
its own slow interval rather than the 2s poll tick.

The searches run before the query because the query needs their PR numbers.
They were already sequential, so ordering them costs nothing.

A branch name reaches that query as a GraphQL variable, never as text spliced
into it. Git allows a double quote in a branch name, and the query is a string.

An origin that is not on github.com gets ``git ls-remote --symref origin HEAD``
instead, with the same branches named as extra refs, one call per repo on the
same slow clock. It reads refs and writes none, so the rule above holds for it
too.

A checkout is compared against the branch its upstream names, or against the
same name on ``origin`` when it tracks nothing. A branch tracking some other
remote is left unanswered rather than measured against an origin branch that
happens to share its name.

The read runs on two clocks. :meth:`RemoteReader.read` asks GitHub on the slow
one; :meth:`RemoteReader.refresh_local` recomputes whether the answer has been
merged in yet, from the local refs, on every poll. Without that split a pull
stays invisible for the rest of the network interval, which is the whole
question the column exists to answer.

Everything degrades to unknown. A missing ``gh``, an expired token, an
unreachable host and a repo that was deleted upstream all leave the row saying
so, rather than reading as current.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from cboard2.gitstate import run_git

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from cboard2.discovery import Repo
    from cboard2.gitstate import GitRunner

    type GhRunner = Callable[[Sequence[str]], str | None]
    type CacheLoader = Callable[[], Cached | None]
    type CacheSaver = Callable[[Cached], bool]
    type _Chunk = tuple[
        dict[str, tuple[str, str]],
        dict[str, dict[str, str]],
        dict[str, dict[int, str]],
    ]
    """One chunk's answers: its defaults, its branch tips and its checks states."""

GH_TIMEOUT = 30.0
"""Seconds before a gh call is abandoned and its repos left unknown."""

LS_REMOTE_TIMEOUT = 15.0
"""Seconds before an ls-remote is abandoned and its repo left unknown.

Longer than the 5s cap on a local git call, because this one waits on a host
and an ssh handshake before git says anything.
"""

DEFAULT_REMOTE_INTERVAL = 300.0
"""Seconds between remote reads.

Every repo's default branch moves on someone else's schedule, so reading it
faster than this buys nothing.
"""

DEFAULT_MAX_WORKERS = 8
"""Concurrent calls while resolving slugs and local refs."""

BATCH_SIZE = 30
"""Repos per GraphQL query, run as one batch across the pool."""

PR_SEARCH_LIMIT = 100
"""Open PRs the search returns. A user past this has the oldest ones dropped."""

ORIGIN = "origin"
"""The one remote this module reads. A branch tracking another is left alone."""

_ORIGIN_ARGS = ("remote", "get-url", "origin")
_LS_REMOTE_ARGS = ("ls-remote", "--symref", "origin", "HEAD")
_WORKTREES_ARGS = ("worktree", "list", "--porcelain")
_HEADS_ARGS = (
    "for-each-ref",
    "refs/heads",
    (
        "--format=%(refname:short)%09%(objectname)"
        "%09%(upstream:remotename)%09%(upstream:remoteref)"
    ),
)

_PR_SEARCH_FIELDS = "number,title,isDraft,repository,url,updatedAt"

_PR_SEARCH_ARGS = (
    "search",
    "prs",
    "--author=@me",
    "--state=open",
    "--json",
    _PR_SEARCH_FIELDS,
    "--limit",
    str(PR_SEARCH_LIMIT),
)

_REVIEW_SEARCH_ARGS = (
    "search",
    "prs",
    "--review-requested=@me",
    "--state=open",
    "--json",
    _PR_SEARCH_FIELDS,
    "--limit",
    str(PR_SEARCH_LIMIT),
)
"""The second search: PRs someone has asked the user to review."""

CHECKS_UNKNOWN = "unknown"
CHECKS_NONE = "none"
CHECKS_PASSING = "passing"
CHECKS_FAILING = "failing"
CHECKS_PENDING = "pending"

_ROLLUP_STATES = MappingProxyType(
    {
        "SUCCESS": CHECKS_PASSING,
        "FAILURE": CHECKS_FAILING,
        "ERROR": CHECKS_FAILING,
        "PENDING": CHECKS_PENDING,
        "EXPECTED": CHECKS_PENDING,
    },
)
"""GitHub's ``statusCheckRollup`` states, mapped to the four this module reports.

A PR whose head commit has no rollup reads :data:`CHECKS_NONE` — no checks are
configured — which is a different answer from :data:`CHECKS_UNKNOWN`, where the
query never reached it.
"""

_CHECKS_ORDER = (CHECKS_FAILING, CHECKS_PENDING, CHECKS_PASSING, CHECKS_NONE)
"""Rollup states from the one worth acting on first to the one worth ignoring."""

_CHECKS_MARKS = MappingProxyType(
    {
        CHECKS_PASSING: "✓",
        CHECKS_FAILING: "✗",
        CHECKS_PENDING: "•",
    },
)
"""The glyph each state shows in a table cell. The other two show nothing."""

_SLUG_PATTERN = re.compile(
    r"github\.com[:/]+(?P<owner>[A-Za-z0-9._-]+)/(?P<name>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$",
)
"""Origin URLs this module can name a GitHub repo from.

The charset is the one GitHub allows in an owner or repo name, which also keeps
a hostile remote URL from reaching the GraphQL query it is interpolated into.
An enterprise host does not match, and its repos stay unknown.
"""

_URL_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.*)$",
)
"""Origin URLs written with a scheme, whose host and path :func:`origin_key` reads."""

_SCP_PATTERN = re.compile(r"^(?:[^/@]+@)?(?P<host>[^/:]+):(?P<path>.*)$")
"""The scp-like form ``git@host:owner/repo.git``, which git takes without a scheme."""

LOCAL_ORIGIN = "local"
"""The key :func:`origin_key` gives an origin that is a path rather than a host."""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One open pull request the user authored or was asked to review."""

    number: int
    title: str
    url: str
    draft: bool
    updated_at: float | None
    checks: str = CHECKS_UNKNOWN
    """The head commit's checks rollup, from the five ``CHECKS_*`` values.

    Filled by the GraphQL query, not by the search that found the PR, so a
    failed query leaves it :data:`CHECKS_UNKNOWN` with the PR still listed.
    """


def worst_checks(prs: Sequence[PullRequest]) -> str:
    """Return the state of the PR most worth looking at among ``prs``.

    A failing PR outranks a pending one and a pending one a passing one, so one
    cell can stand for a repo holding several.
    """
    states = {pr.checks for pr in prs}
    return next((state for state in _CHECKS_ORDER if state in states), CHECKS_UNKNOWN)


def checks_mark(state: str) -> str:
    """Return the glyph for a checks state, or "" where there is nothing to say."""
    return _CHECKS_MARKS.get(state, "")


@dataclass(frozen=True, slots=True)
class RemoteState:
    """What the last remote read established about one repo.

    The two ``known`` flags are separate because the two calls fail
    separately: an expired token blanks both, a repo deleted upstream blanks
    only its default branch.
    """

    origin: str | None = None
    """The origin URL. ``slug`` is set only when it names a github.com repo."""

    slug: str | None = None
    default_branch: str | None = None
    default_sha: str | None = None
    default_known: bool = False
    prs: tuple[PullRequest, ...] = ()
    prs_known: bool = False
    review_prs: tuple[PullRequest, ...] = ()
    """Open PRs on this repo that have asked the user for a review."""

    review_prs_known: bool = False
    """Whether the review search succeeded. Separate from ``prs_known``: the two
    searches fail separately, and an empty result is a real answer for either."""

    behind_default: bool = False
    """Whether the remote tip is missing from the local default branch.

    Not decided by the network. :meth:`RemoteReader.refresh_local` recomputes
    it every poll, so a pull clears it without waiting for the next network
    read.
    """

    branch: str | None = None
    """The branch checked out here when the last read ran.

    Held so a checkout that has moved since is caught: the fields below are
    about this branch and say nothing about whatever is checked out now.
    """

    branch_remote: str | None = None
    """The branch on ``origin`` ``branch`` was compared against, if any.

    None when the checkout is detached, tracks another remote, or is the
    default branch — that last one is ``behind_default``'s question already.
    """

    branch_sha: str | None = None
    """The origin's tip of ``branch_remote``, or None when it has no such branch."""

    branch_known: bool = False
    """Whether the read reached an origin that could answer about ``branch``."""

    behind_branch: bool = False
    """Whether the origin's tip of ``branch_remote`` is missing from ``branch``.

    Derived from the local refs on the same clock as ``behind_default``.
    """

    @property
    def draft_count(self) -> int:
        """How many of the open PRs are drafts."""
        return sum(1 for pr in self.prs if pr.draft)


UNKNOWN = RemoteState()
"""The state of a repo no remote read has covered yet."""

_NO_DEFAULTS: Mapping[str, tuple[str, str]] = MappingProxyType({})
_NO_BRANCHES: Mapping[str, Mapping[str, str]] = MappingProxyType({})
_NO_PRS: Mapping[str, tuple[PullRequest, ...]] = MappingProxyType({})
"""Empty defaults for :class:`Cached`, immutable so the dataclass accepts them."""

_NO_CHECKS: Mapping[str, Sequence[int]] = MappingProxyType({})
"""No pull requests to ask about, for a read whose searches both failed."""


@dataclass(frozen=True, slots=True)
class Cached:
    """One remote read, in the shape that survives between processes.

    Only what the network decided. The two behind markers are absent on
    purpose: they come from the local refs, so a stored copy would keep
    reporting ``behind main`` after a pull.
    """

    read_at: float
    defaults: Mapping[str, tuple[str, str]] = _NO_DEFAULTS
    """Slug to its default branch name and tip sha."""

    branches: Mapping[str, Mapping[str, str]] = _NO_BRANCHES
    """Slug to the tip of each branch the read asked about by name.

    Stored so a restart inside the read interval still knows where the origin's
    copy of the checked-out branch stood, rather than waiting out the interval
    with the column blank.
    """

    prs: Mapping[str, tuple[PullRequest, ...]] = _NO_PRS
    prs_known: bool = False
    """Whether the search that produced ``prs`` succeeded.

    False and an empty ``prs`` are different answers: the first is a failed
    search, the second a user with no open PRs anywhere.
    """

    review_prs: Mapping[str, tuple[PullRequest, ...]] = _NO_PRS
    review_prs_known: bool = False
    """The same pair for the PRs waiting on the user's review."""


def run_gh(args: Sequence[str]) -> str | None:
    """Run one gh command and return stdout, or None when there is none.

    The exit code is deliberately ignored. A batched query naming one repo that
    was deleted upstream exits non-zero with the other 65 answers still on
    stdout, and discarding that would blank every row over one dead clone.
    """
    if shutil.which("gh") is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def run_ls_remote(root: Path, args: Sequence[str]) -> str | None:
    """Run one git call that talks to a remote; return stdout, or None on failure.

    The environment makes every credential prompt fail rather than wait: a
    worker blocked on a passphrase holds the read open until its timeout.
    """
    ssh = os.environ.get("GIT_SSH_COMMAND", "ssh")
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": f"{ssh} -o BatchMode=yes",
    }
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "--no-optional-locks", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=LS_REMOTE_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def leaders(repos: Sequence[Repo]) -> list[Repo]:
    """Return one repo per family, in the order given.

    A repo and its linked worktrees share ``refs``, so the origin URL and the
    branch tips are one answer for the whole group. Reading them per row would
    run the same git call once per worktree and put the same slug into the
    query more than once.
    """
    seen: set[Path] = set()
    chosen: list[Repo] = []
    for repo in repos:
        if repo.family in seen:
            continue
        seen.add(repo.family)
        chosen.append(repo)
    return chosen


def parse_slug(url: str) -> str | None:
    """Return ``owner/name`` for a GitHub origin URL, or None for anything else."""
    match = _SLUG_PATTERN.search(url.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['name']}"


def github_slugs(origins: Iterable[str]) -> list[str]:
    """Return the sorted GitHub slugs among ``origins``, dropping every other host."""
    return sorted({slug for url in origins if (slug := parse_slug(url)) is not None})


def origin_key(url: str) -> str | None:
    """Return an origin URL's host and owner, as ``github.com/ove``, or None if empty.

    A host with no dot is an ssh alias or a LAN name, whose first path segment
    is a directory rather than an owner, so it keys on the host alone. A
    filesystem path has no host and keys as :data:`LOCAL_ORIGIN`.
    """
    text = url.strip()
    if not text:
        return None
    if text.startswith("file://"):
        return LOCAL_ORIGIN
    match = _URL_PATTERN.match(text) or _SCP_PATTERN.match(text)
    if match is None:
        return LOCAL_ORIGIN
    host = match["host"].lower()
    if "." not in host:
        return host
    owner = match["path"].strip("/").partition("/")[0].removesuffix(".git").lower()
    return f"{host}/{owner}" if owner else host


def parse_symref(text: str) -> tuple[str, str] | None:
    """Read ``ls-remote --symref origin HEAD`` as a default branch name and tip sha.

    Both lines are required. A server that answers with the sha alone leaves the
    branch unnamed, and the local comparison needs that name to find its branch.
    """
    branch: str | None = None
    sha: str | None = None
    for line in text.splitlines():
        left, tab, ref = line.partition("\t")
        if not tab or ref.strip() != "HEAD":
            continue
        if left.startswith("ref: "):
            target = left.removeprefix("ref: ").strip()
            if target.startswith("refs/heads/"):
                branch = target.removeprefix("refs/heads/")
        else:
            sha = left.strip()
    if branch is None or sha is None:
        return None
    return branch, sha


def parse_heads(text: str) -> dict[str, str]:
    """Parse ``for-each-ref refs/heads`` output into branch name to sha."""
    heads: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 2 and fields[0] and fields[1]:
            heads[fields[0]] = fields[1]
    return heads


def parse_upstreams(text: str) -> dict[str, tuple[str, str]]:
    """Parse the same output into branch name to its remote's name and ref.

    A branch tracking nothing has both fields empty and is left out, which
    reads the same as a branch git never mentioned.
    """
    found: dict[str, tuple[str, str]] = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) >= 4 and fields[0] and fields[2] and fields[3]:
            found[fields[0]] = (fields[2], fields[3])
    return found


def parse_worktrees(text: str) -> dict[str, str]:
    """Parse ``worktree list --porcelain`` into worktree path to branch name.

    A detached worktree has no ``branch`` line and is left out. The paths git
    prints here are resolved, so a caller matching them resolves its own.
    """
    found: dict[str, str] = {}
    path: str | None = None
    for line in text.splitlines():
        if line.startswith("worktree "):
            path = line.removeprefix("worktree ").strip() or None
        elif line.startswith("branch ") and path is not None:
            ref = line.removeprefix("branch ").strip()
            if ref.startswith("refs/heads/"):
                found[path] = ref.removeprefix("refs/heads/")
    return found


def parse_ref_shas(text: str) -> dict[str, str]:
    """Read the ``refs/heads`` lines of ls-remote output as branch name to sha."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        sha, tab, ref = line.partition("\t")
        name = ref.strip()
        if tab and sha.strip() and name.startswith("refs/heads/"):
            found[name.removeprefix("refs/heads/")] = sha.strip()
    return found


def target_branch(branch: str | None, upstream: tuple[str, str] | None) -> str | None:
    """Return the branch on ``origin`` ``branch`` should be compared against.

    A branch tracking nothing falls back to its own name, which is the branch
    a push would create. One tracking another remote returns None: an origin
    branch of the same name is a different line of work.
    """
    if branch is None:
        return None
    if upstream is None:
        return branch
    remote, ref = upstream
    if remote != ORIGIN or not ref.startswith("refs/heads/"):
        return None
    return ref.removeprefix("refs/heads/") or None


def build_query(
    slugs: Sequence[str],
    pairs: Sequence[tuple[str, str]] = (),
    checks: Sequence[tuple[str, int]] = (),
) -> str:
    """Return a GraphQL query for each slug's branches and pull request checks.

    ``pairs`` are the ``(slug, branch)`` lookups to add, each nested under its
    slug as a ``b<index>`` alias reading the variable of the same name.
    ``checks`` are the ``(slug, number)`` pull requests whose head commit's
    rollup to ask for, as a ``c<index>`` alias. :func:`parse_defaults`,
    :func:`parse_branch_tips` and :func:`parse_check_states` read the indexes
    back out, and the slug's own index is its alias because a slug is not a
    legal GraphQL name.

    A PR number is written into the query text rather than bound, because it
    reaches here as an ``int`` off a JSON parse. A branch name is a string the
    user chose and stays a variable.
    """
    nested: dict[str, list[str]] = {}
    for index, (slug, _) in enumerate(pairs):
        nested.setdefault(slug, []).append(
            f"b{index}: ref(qualifiedName: $b{index}) {{ target {{ oid }} }}",
        )
    for index, (slug, number) in enumerate(checks):
        nested.setdefault(slug, []).append(
            f"c{index}: pullRequest(number: {int(number)}) "
            "{ commits(last: 1) { nodes { commit "
            "{ statusCheckRollup { state } } } } }",
        )
    lines = [
        f'  r{index}: repository(owner: "{slug.split("/")[0]}", '
        f'name: "{slug.split("/")[1]}") '
        "{ defaultBranchRef { name target { oid } }"
        + "".join(f" {lookup}" for lookup in nested.get(slug, ()))
        + " }"
        for index, slug in enumerate(slugs)
    ]
    header = ""
    if pairs:
        declared = ", ".join(f"$b{index}: String!" for index in range(len(pairs)))
        header = f"query({declared}) "
    return header + "{\n" + "\n".join(lines) + "\n}"


def branch_variables(pairs: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """Return the gh arguments binding each pair's branch to its query variable.

    Bound as a variable rather than written into the query text: git allows a
    double quote in a branch name, and the query is a string.
    """
    args: list[str] = []
    for index, (_, branch) in enumerate(pairs):
        args += ["-f", f"b{index}=refs/heads/{branch}"]
    return tuple(args)


def parse_defaults(text: str, slugs: Sequence[str]) -> dict[str, tuple[str, str]]:
    """Map each slug to its default branch name and tip sha.

    A slug whose entry is null or incomplete is left out, so the caller reports
    it unknown instead of current.
    """
    payload = _as_dict(_load(text))
    data = _as_dict(payload.get("data"))
    if not data:
        return {}

    found: dict[str, tuple[str, str]] = {}
    for index, slug in enumerate(slugs):
        ref = _as_dict(_as_dict(data.get(f"r{index}")).get("defaultBranchRef"))
        name = ref.get("name")
        oid = _as_dict(ref.get("target")).get("oid")
        if isinstance(name, str) and isinstance(oid, str):
            found[slug] = (name, oid)
    return found


def parse_branch_tips(
    text: str,
    slugs: Sequence[str],
    pairs: Sequence[tuple[str, str]],
) -> dict[str, dict[str, str]]:
    """Map each pair's slug and branch to the tip the query returned for it.

    A branch the origin does not have resolves to null and is left out, which
    the caller reads as no branch to be behind of rather than as no answer.
    """
    payload = _as_dict(_load(text))
    data = _as_dict(payload.get("data"))
    if not data:
        return {}

    aliases = {slug: f"r{index}" for index, slug in enumerate(slugs)}
    found: dict[str, dict[str, str]] = {}
    for index, (slug, branch) in enumerate(pairs):
        repo = _as_dict(data.get(aliases.get(slug, "")))
        oid = _as_dict(_as_dict(repo.get(f"b{index}")).get("target")).get("oid")
        if isinstance(oid, str):
            found.setdefault(slug, {})[branch] = oid
    return found


def parse_check_states(
    text: str,
    slugs: Sequence[str],
    checks: Sequence[tuple[str, int]],
) -> dict[str, dict[int, str]]:
    """Map each asked-about PR to its head commit's checks state, by slug and number.

    A PR whose repository entry is missing is left out and stays
    :data:`CHECKS_UNKNOWN`; one whose head commit carries no rollup reads
    :data:`CHECKS_NONE`, which is the answer that no checks run here.
    """
    payload = _as_dict(_load(text))
    data = _as_dict(payload.get("data"))
    if not data:
        return {}

    aliases = {slug: f"r{index}" for index, slug in enumerate(slugs)}
    found: dict[str, dict[int, str]] = {}
    for index, (slug, number) in enumerate(checks):
        repo = _as_dict(data.get(aliases.get(slug, "")))
        if f"c{index}" not in repo:
            continue
        entry = _as_dict(repo.get(f"c{index}"))
        if not entry:
            continue
        found.setdefault(slug, {})[number] = _rollup_state(entry)
    return found


def _rollup_state(pull_request: dict[str, object]) -> str:
    """Read one ``pullRequest`` entry's checks rollup into a ``CHECKS_*`` value."""
    nodes = _as_dict(pull_request.get("commits")).get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return CHECKS_NONE
    commit = _as_dict(_as_dict(cast("list[object]", nodes)[0]).get("commit"))
    rollup = commit.get("statusCheckRollup")
    if not isinstance(rollup, dict):
        return CHECKS_NONE
    state = cast("dict[str, object]", rollup).get("state")
    if not isinstance(state, str):
        return CHECKS_NONE
    return _ROLLUP_STATES.get(state, CHECKS_PENDING)


def with_checks(
    prs: Mapping[str, tuple[PullRequest, ...]],
    states: Mapping[str, Mapping[int, str]],
) -> dict[str, tuple[PullRequest, ...]]:
    """Return ``prs`` with each entry's checks state filled in where one is known."""
    return {
        slug: tuple(
            replace(pr, checks=states[slug][pr.number])
            if pr.number in states.get(slug, {})
            else pr
            for pr in found
        )
        for slug, found in prs.items()
    }


def check_lookups(
    slugs: Sequence[str],
    *searches: Mapping[str, tuple[PullRequest, ...]] | None,
) -> dict[str, list[int]]:
    """Return the PR numbers to ask about, per watched slug.

    Only the repos on the watch list: the searches cover every repo the user
    touches anywhere, and a rollup for a PR that gets no row is a field nobody
    reads. One PR found by both searches is asked about once.
    """
    watched = set(slugs)
    found: dict[str, set[int]] = {}
    for search in searches:
        for slug, prs in (search or {}).items():
            if slug in watched:
                found.setdefault(slug, set()).update(pr.number for pr in prs)
    return {slug: sorted(numbers) for slug, numbers in found.items()}


def parse_prs(text: str) -> dict[str, tuple[PullRequest, ...]] | None:
    """Group ``gh search prs`` output by ``owner/name``, or None if unreadable.

    None and an empty mapping mean different things: the first is a failed
    search, the second a user with no open PRs anywhere.
    """
    payload = _load(text)
    if not isinstance(payload, list):
        return None

    grouped: dict[str, list[PullRequest]] = {}
    for item in cast("list[object]", payload):
        entry = _pull_request(_as_dict(item))
        if entry is None:
            continue
        slug, request = entry
        grouped.setdefault(slug, []).append(request)
    return {
        slug: tuple(sorted(requests, key=lambda pr: pr.number, reverse=True))
        for slug, requests in grouped.items()
    }


def _pull_request(item: dict[str, object]) -> tuple[str, PullRequest] | None:
    """Read one search result, or return None when a field it needs is missing."""
    number = item.get("number")
    slug = _as_dict(item.get("repository")).get("nameWithOwner")
    if not isinstance(number, int) or not isinstance(slug, str):
        return None
    title = item.get("title")
    url = item.get("url")
    return slug, PullRequest(
        number=number,
        title=title if isinstance(title, str) else "",
        url=url if isinstance(url, str) else "",
        draft=item.get("isDraft") is True,
        updated_at=_timestamp(item.get("updatedAt")),
    )


def _load(text: str) -> object:
    """Parse gh's stdout, returning None rather than raising on malformed JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _as_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a string-keyed mapping, or an empty one.

    Every field below comes out of ``json.loads`` as ``object``, and a mapping
    is the only shape worth reading further into.
    """
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", value)


def _timestamp(value: object) -> float | None:
    """Convert an ISO-8601 field from gh into a unix time, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


class RemoteReader:
    """Holds the last remote reading per repo, refreshed on a slow interval.

    :meth:`read` is the only method that touches the network.
    :meth:`refresh_local` re-derives both behind markers from the local refs
    and is cheap enough for every poll: measured on the author's machine, one
    ``for-each-ref`` across 75 repos takes 0.1s at 16 workers, and the
    ``worktree list`` beside it costs about the same.
    """

    def __init__(
        self,
        interval: float = DEFAULT_REMOTE_INTERVAL,
        *,
        max_workers: int = DEFAULT_MAX_WORKERS,
        runner: GitRunner = run_git,
        ls_remote: GitRunner = run_ls_remote,
        gh: GhRunner = run_gh,
        load: CacheLoader | None = None,
        save: CacheSaver | None = None,
    ) -> None:
        self._interval = interval
        self._max_workers = max_workers
        self._runner = runner
        self._ls_remote = ls_remote
        self._gh = gh
        self._load = load
        self._save = save
        self._primed = False
        self._states: dict[Path, RemoteState] = {}
        self._read_at: float | None = None
        self._ancestry: dict[tuple[Path, str, str], bool] = {}
        self._origin_urls: dict[Path, str | None] = {}

    @property
    def read_at(self) -> float | None:
        """When the last remote read finished, or None before the first one."""
        return self._read_at

    def cached(self, path: Path) -> RemoteState:
        """Return the last reading for ``path``, or :data:`UNKNOWN`."""
        return self._states.get(path, UNKNOWN)

    def due(self, now: float) -> bool:
        """Return True before the first read, then once per interval."""
        if self._read_at is None:
            return True
        return (now - self._read_at) >= self._interval

    def prime(self, repos: Sequence[Repo]) -> bool:
        """Load the stored read, once, and report whether anything came back.

        The timestamp comes from the file, so :meth:`due` treats a cache
        written a minute ago as a read a minute ago and skips the network.
        Without a loader, or with nothing usable on disk, this is a no-op.
        """
        if self._primed or self._load is None:
            return False
        self._primed = True
        cached = self._load()
        if cached is None:
            return False

        origins = self._origins(repos)
        checkouts = self._checkouts(repos)
        self._rebuild(
            repos,
            origins=origins,
            defaults=cached.defaults,
            tips=cached.branches,
            prs=cached.prs if cached.prs_known else None,
            review=cached.review_prs if cached.review_prs_known else None,
            checkouts=checkouts,
            targets=self._targets(repos, checkouts, self._ref_lines(repos)),
        )
        self._read_at = cached.read_at
        self.refresh_local(repos)
        return True

    def read(
        self,
        repos: Sequence[Repo],
        now: float,
        *,
        force: bool = False,
    ) -> bool:
        """Ask every origin about its branches, and report whether the call ran.

        Each origin is asked for its default branch and for whatever branch is
        checked out in the repo and its worktrees. Returns False without a
        network call when the interval has not elapsed and ``force`` is unset.
        Finishes by calling :meth:`refresh_local`, so the states this leaves
        behind are already current against the tree.

        The two PR searches run first, because the query that fills in each
        PR's checks state needs their numbers to ask about.
        """
        if not (force or self.due(now)):
            return False

        origins = self._origins(repos)
        checkouts = self._checkouts(repos)
        lines = self._ref_lines(repos)
        targets = self._targets(repos, checkouts, lines)
        wanted = self._wanted(repos, origins, targets)

        prs = self._prs(_PR_SEARCH_ARGS)
        review = self._prs(_REVIEW_SEARCH_ARGS)
        slugs = github_slugs(origins.values())
        asked = check_lookups(slugs, prs, review)

        defaults, tips, states = self._defaults(slugs, wanted, asked)
        probed, probed_tips = self._probe(repos, origins, wanted)
        defaults.update(probed)
        tips.update(probed_tips)
        prs = None if prs is None else with_checks(prs, states)
        review = None if review is None else with_checks(review, states)

        self._rebuild(
            repos,
            origins=origins,
            defaults=defaults,
            tips=tips,
            prs=prs,
            review=review,
            checkouts=checkouts,
            targets=targets,
        )
        self._read_at = now
        self._apply(self._covered(repos), lines, checkouts)
        self._store(defaults, tips, prs, review, now)
        return True

    def _rebuild(
        self,
        repos: Sequence[Repo],
        *,
        origins: Mapping[Path, str],
        defaults: Mapping[str, tuple[str, str]],
        tips: Mapping[str, Mapping[str, str]],
        prs: Mapping[str, tuple[PullRequest, ...]] | None,
        review: Mapping[str, tuple[PullRequest, ...]] | None,
        checkouts: Mapping[Path, str],
        targets: Mapping[Path, str],
    ) -> None:
        """Replace every state from one set of answers, network or cached."""
        self._states = {
            repo.path: self._state(
                origin=origins.get(repo.path),
                defaults=defaults,
                tips=tips,
                prs=prs,
                review=review,
                branch=checkouts.get(repo.path),
                target=targets.get(repo.path),
            )
            for repo in repos
        }

    def _store(
        self,
        defaults: dict[str, tuple[str, str]],
        tips: dict[str, dict[str, str]],
        prs: Mapping[str, tuple[PullRequest, ...]] | None,
        review: Mapping[str, tuple[PullRequest, ...]] | None,
        now: float,
    ) -> None:
        """Hand this read to the cache, ignoring a write that could not land.

        Only the slugs just read are passed on, so an entry for a repo that
        left the watch list does not sit in the file forever.
        """
        if self._save is None:
            return
        self._save(
            Cached(
                read_at=now,
                defaults=defaults,
                branches=tips,
                prs={} if prs is None else prs,
                prs_known=prs is not None,
                review_prs={} if review is None else review,
                review_prs_known=review is not None,
            ),
        )

    def refresh_local(self, repos: Sequence[Repo]) -> None:
        """Recompute both behind markers against the local refs, making no network call.

        Called on every poll. A pull moves ``refs/heads/main`` without telling
        GitHub anything, so the answer has to be re-derived on the fast clock
        or the column keeps reporting a state the user has already fixed. A
        checkout onto another branch is caught here too.
        """
        self._adopt_origins(repos)
        known = self._covered(repos)
        if not known:
            return
        self._apply(known, self._ref_lines(known), self._checkouts(known))

    def _adopt_origins(self, repos: Sequence[Repo]) -> None:
        """Give a repo its origin URL before any network read has covered it.

        ``git remote get-url`` reads a local config file, so the dashboard can
        name every origin on its first poll rather than after the first network
        read. A repo already asked about is skipped, which leaves the steady
        poll with nothing to run.
        """
        fresh = [repo for repo in repos if repo.path not in self._origin_urls]
        if not fresh:
            return
        for path, url in self._origins(fresh).items():
            state = self._states.get(path, UNKNOWN)
            if state.origin is None:
                self._states[path] = replace(state, origin=url)

    def _covered(self, repos: Sequence[Repo]) -> list[Repo]:
        """Return the repos a read has already answered for."""
        return [
            repo
            for repo in repos
            if self._states.get(repo.path, UNKNOWN).default_sha is not None
        ]

    def _apply(
        self,
        known: Sequence[Repo],
        lines: Mapping[Path, str],
        checkouts: Mapping[Path, str],
    ) -> None:
        """Recompute both behind markers for every repo a read has answered for."""
        heads = {family: parse_heads(text) for family, text in lines.items()}
        for repo in known:
            state = self._states[repo.path]
            fresh = self._rederive(
                repo,
                state,
                heads.get(repo.family, {}),
                checkouts.get(repo.path),
            )
            if fresh != state:
                self._states[repo.path] = fresh

    def _rederive(
        self,
        repo: Repo,
        state: RemoteState,
        heads: dict[str, str],
        checkout: str | None,
    ) -> RemoteState:
        """Return ``state`` with both behind markers recomputed from the local refs.

        A checkout that has moved since the read drops the branch answer
        entirely: it was about the branch that was here then, and the next
        network read is what fills the new one in.
        """
        behind_default = self._behind(
            repo,
            heads,
            state.default_branch,
            state.default_sha,
        )
        if checkout != state.branch:
            return replace(
                state,
                behind_default=behind_default,
                branch=checkout,
                branch_remote=None,
                branch_sha=None,
                branch_known=False,
                behind_branch=False,
            )
        return replace(
            state,
            behind_default=behind_default,
            behind_branch=self._behind(repo, heads, state.branch, state.branch_sha),
        )

    def _state(
        self,
        *,
        origin: str | None,
        defaults: Mapping[str, tuple[str, str]],
        tips: Mapping[str, Mapping[str, str]],
        prs: Mapping[str, tuple[PullRequest, ...]] | None,
        review: Mapping[str, tuple[PullRequest, ...]] | None,
        branch: str | None,
        target: str | None,
    ) -> RemoteState:
        """Assemble one repo's network facts. Both behind markers are left to later.

        A repo off GitHub reports its PRs known and empty: no search could have
        found one, so an unread marker would promise an answer that never comes.
        A checkout of the default branch keeps no branch answer either, because
        ``behind_default`` already carries that comparison.
        """
        if origin is None:
            return UNKNOWN
        slug = parse_slug(origin)
        key = slug or origin
        default = defaults.get(key)
        default_branch, sha = default or (None, None)
        answered = default is not None
        remote_branch = (
            target
            if answered and target is not None and target != default_branch
            else None
        )
        return RemoteState(
            origin=origin,
            slug=slug,
            default_branch=default_branch,
            default_sha=sha,
            default_known=answered,
            prs=() if slug is None or prs is None else prs.get(slug, ()),
            prs_known=slug is None or prs is not None,
            review_prs=() if slug is None or review is None else review.get(slug, ()),
            review_prs_known=slug is None or review is not None,
            branch=branch,
            branch_remote=remote_branch,
            branch_sha=(
                None if remote_branch is None else tips.get(key, {}).get(remote_branch)
            ),
            branch_known=remote_branch is not None,
        )

    def _behind(
        self,
        repo: Repo,
        heads: dict[str, str],
        branch: str | None,
        sha: str | None,
    ) -> bool:
        """Return True when the local default branch does not contain the remote tip.

        A differing sha alone is not enough — a local ``main`` one commit ahead
        also differs, and still has the remote commit in its history. Asking
        ``merge-base --is-ancestor`` separates the two, and reads False for a
        clone that fetched the commit without merging it, which a plain object
        lookup would not.

        The answer is memoized per family on both shas, so a repo nobody has
        touched costs no process on the next poll, and its worktrees cost none
        either.
        """
        if branch is None or sha is None:
            return False
        local = heads.get(branch)
        if local is None or local == sha:
            return False

        key = (repo.family, local, sha)
        contained = self._ancestry.get(key)
        if contained is None:
            contained = (
                self._runner(repo.path, ("merge-base", "--is-ancestor", sha, local))
                is not None
            )
            self._ancestry[key] = contained
        return not contained

    def _origins(self, repos: Sequence[Repo]) -> dict[Path, str]:
        """Read each family's origin URL, and hand it back per path.

        Answered per family and handed back per path, because the caller holds
        one state per row.
        """

        def origin(repo: Repo) -> tuple[Path, str | None]:
            out = self._runner(repo.path, _ORIGIN_ARGS)
            return repo.family, None if out is None else out.strip() or None

        found = {
            family: url
            for family, url in self._map(origin, leaders(repos))
            if url is not None
        }
        self._origin_urls.update({repo.path: found.get(repo.family) for repo in repos})
        return {repo.path: found[repo.family] for repo in repos if repo.family in found}

    def _probe(
        self,
        repos: Sequence[Repo],
        origins: Mapping[Path, str],
        wanted: Mapping[str, Sequence[str]],
    ) -> tuple[dict[str, tuple[str, str]], dict[str, dict[str, str]]]:
        """Ask the origins GitHub cannot answer about HEAD and the named branches.

        Keyed by URL rather than by path, which is the same key the GraphQL
        answers use and never collides with an ``owner/name``. One call carries
        both questions, because a second would be a second ssh handshake.
        """
        elsewhere = [
            repo
            for repo in leaders(repos)
            if (url := origins.get(repo.path)) is not None and parse_slug(url) is None
        ]

        def probe(repo: Repo) -> tuple[str, tuple[str, str] | None, dict[str, str]]:
            url = origins[repo.path]
            args = (
                *_LS_REMOTE_ARGS,
                *(f"refs/heads/{name}" for name in wanted.get(url, ())),
            )
            out = self._ls_remote(repo.path, args)
            if out is None:
                return url, None, {}
            return url, parse_symref(out), parse_ref_shas(out)

        defaults: dict[str, tuple[str, str]] = {}
        tips: dict[str, dict[str, str]] = {}
        for url, default, found in self._map(probe, elsewhere):
            if default is not None:
                defaults[url] = default
            if found:
                tips[url] = found
        return defaults, tips

    def _checkouts(self, repos: Sequence[Repo]) -> dict[Path, str]:
        """Return the branch checked out at each path, read once per family.

        ``worktree list`` answers for a repo and all its worktrees at once, so
        this costs one git call per family rather than one per row. Git prints
        resolved paths, so a row reached through a symlink is looked up under
        both spellings.
        """

        def listing(repo: Repo) -> tuple[Path, dict[str, str]]:
            out = self._runner(repo.path, _WORKTREES_ARGS)
            return repo.family, {} if out is None else parse_worktrees(out)

        by_family = dict(self._map(listing, leaders(repos)))
        found: dict[Path, str] = {}
        for repo in repos:
            listed = by_family.get(repo.family, {})
            branch = listed.get(str(repo.path)) or listed.get(str(repo.path.resolve()))
            if branch is not None:
                found[repo.path] = branch
        return found

    def _ref_lines(self, repos: Sequence[Repo]) -> dict[Path, str]:
        """Read each family's branch tips and upstreams once, unparsed.

        Handed on as text because two callers want different fields out of it,
        and a second ``for-each-ref`` would be the same answer read twice.
        """

        def refs(repo: Repo) -> tuple[Path, str]:
            return repo.family, self._runner(repo.path, _HEADS_ARGS) or ""

        return dict(self._map(refs, leaders(repos)))

    def _targets(
        self,
        repos: Sequence[Repo],
        checkouts: Mapping[Path, str],
        lines: Mapping[Path, str],
    ) -> dict[Path, str]:
        """Return the origin branch each checkout should be compared against."""
        upstreams = {family: parse_upstreams(text) for family, text in lines.items()}
        found: dict[Path, str] = {}
        for repo in repos:
            branch = checkouts.get(repo.path)
            if branch is None:
                continue
            target = target_branch(branch, upstreams.get(repo.family, {}).get(branch))
            if target is not None:
                found[repo.path] = target
        return found

    def _wanted(
        self,
        repos: Sequence[Repo],
        origins: Mapping[Path, str],
        targets: Mapping[Path, str],
    ) -> dict[str, list[str]]:
        """Group the branches to ask about by the remote that can answer for them.

        Two worktrees of one repo sitting on one branch ask once, and a branch
        checked out in two clones of the same repo asks once too.
        """
        wanted: dict[str, set[str]] = {}
        for repo in repos:
            url = origins.get(repo.path)
            target = targets.get(repo.path)
            if url is None or target is None:
                continue
            wanted.setdefault(parse_slug(url) or url, set()).add(target)
        return {key: sorted(names) for key, names in wanted.items()}

    def _defaults(
        self,
        slugs: Sequence[str],
        wanted: Mapping[str, Sequence[str]],
        asked: Mapping[str, Sequence[int]] = _NO_CHECKS,
    ) -> tuple[
        dict[str, tuple[str, str]],
        dict[str, dict[str, str]],
        dict[str, dict[int, str]],
    ]:
        """Run the batched query and merge each chunk's branches and checks states."""
        if not slugs:
            return {}, {}, {}
        chunks = [
            tuple(slugs[start : start + BATCH_SIZE])
            for start in range(0, len(slugs), BATCH_SIZE)
        ]

        def lookup(chunk: tuple[str, ...]) -> _Chunk:
            pairs = [(slug, name) for slug in chunk for name in wanted.get(slug, ())]
            checks = [
                (slug, number) for slug in chunk for number in asked.get(slug, ())
            ]
            query = build_query(chunk, pairs, checks)
            out = self._gh(
                ("api", "graphql", "-f", f"query={query}", *branch_variables(pairs)),
            )
            if out is None:
                return {}, {}, {}
            return (
                parse_defaults(out, chunk),
                parse_branch_tips(out, chunk, pairs),
                parse_check_states(out, chunk, checks),
            )

        merged: dict[str, tuple[str, str]] = {}
        tips: dict[str, dict[str, str]] = {}
        states: dict[str, dict[int, str]] = {}
        for found, branches, rollups in self._map(lookup, chunks):
            merged.update(found)
            tips.update(branches)
            states.update(rollups)
        return merged, tips, states

    def _prs(self, args: Sequence[str]) -> dict[str, tuple[PullRequest, ...]] | None:
        """Run one PR search across every repo, or return None on failure."""
        out = self._gh(args)
        return None if out is None else parse_prs(out)

    def _map[T, R](self, work: Callable[[T], R], items: Sequence[T]) -> list[R]:
        """Run ``work`` over ``items`` across the pool, preserving order."""
        if not items:
            return []
        workers = min(self._max_workers, len(items))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(work, items))

    def forget_absent(self, repos: Iterable[Repo]) -> None:
        """Drop readings, origins and memoized ancestry for repos off the watch list."""
        watched = list(repos)
        live = {repo.path for repo in watched}
        families = {repo.family for repo in watched}
        for path in [key for key in self._states if key not in live]:
            del self._states[path]
        for path in [key for key in self._origin_urls if key not in live]:
            del self._origin_urls[path]
        for key in [entry for entry in self._ancestry if entry[0] not in families]:
            del self._ancestry[key]
