"""A moved default branch and the user's own open pull requests, read from the origin.

``git status --porcelain=v2 --branch`` reports ``behind`` against the local
remote-tracking ref, so it stays at zero until something fetches. This module
asks the origin directly instead, and never fetches: a poll that rewrote
``refs/remotes/origin/*`` under the user would change what their next ``git
log`` shows.

Two network calls cover every repo. One batched GraphQL query returns each
repo's default branch and its tip; one ``gh search prs`` returns every open PR
the user authored, anywhere. Measured on the author's machine: 66 repos in one
query took 4.4s, against 1.7s per repo for ``git ls-remote`` — which is why the
query is batched and why the read runs on its own slow interval rather than the
2s poll tick.

An origin that is not on github.com gets ``git ls-remote --symref origin HEAD``
instead, one call per repo on the same slow clock. It reads refs and writes
none, so the rule above holds for it too.

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

_ORIGIN_ARGS = ("remote", "get-url", "origin")
_LS_REMOTE_ARGS = ("ls-remote", "--symref", "origin", "HEAD")
_HEADS_ARGS = (
    "for-each-ref",
    "refs/heads",
    "--format=%(refname:short)%09%(objectname)",
)

_PR_SEARCH_ARGS = (
    "search",
    "prs",
    "--author=@me",
    "--state=open",
    "--json",
    "number,title,isDraft,repository,url,updatedAt",
    "--limit",
    str(PR_SEARCH_LIMIT),
)

_SLUG_PATTERN = re.compile(
    r"github\.com[:/]+(?P<owner>[A-Za-z0-9._-]+)/(?P<name>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?/?$",
)
"""Origin URLs this module can name a GitHub repo from.

The charset is the one GitHub allows in an owner or repo name, which also keeps
a hostile remote URL from reaching the GraphQL query it is interpolated into.
An enterprise host does not match, and its repos stay unknown.
"""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """One open pull request the user authored."""

    number: int
    title: str
    url: str
    draft: bool
    updated_at: float | None


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
    behind_default: bool = False
    """Whether the remote tip is missing from the local default branch.

    The one field here the network does not decide.
    :meth:`RemoteReader.refresh_local` recomputes it every poll, so a pull
    clears it without waiting for the next network read.
    """

    @property
    def draft_count(self) -> int:
        """How many of the open PRs are drafts."""
        return sum(1 for pr in self.prs if pr.draft)


UNKNOWN = RemoteState()
"""The state of a repo no remote read has covered yet."""

_NO_DEFAULTS: Mapping[str, tuple[str, str]] = MappingProxyType({})
_NO_PRS: Mapping[str, tuple[PullRequest, ...]] = MappingProxyType({})
"""Empty defaults for :class:`Cached`, immutable so the dataclass accepts them."""


@dataclass(frozen=True, slots=True)
class Cached:
    """One remote read, in the shape that survives between processes.

    Only what the network decided. ``behind_default`` is absent on purpose: it
    comes from the local refs, so a stored copy would keep reporting
    ``behind main`` after a pull.
    """

    read_at: float
    defaults: Mapping[str, tuple[str, str]] = _NO_DEFAULTS
    """Slug to its default branch name and tip sha."""

    prs: Mapping[str, tuple[PullRequest, ...]] = _NO_PRS
    prs_known: bool = False
    """Whether the search that produced ``prs`` succeeded.

    False and an empty ``prs`` are different answers: the first is a failed
    search, the second a user with no open PRs anywhere.
    """


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
        name, tab, sha = line.partition("\t")
        if tab and name and sha:
            heads[name] = sha
    return heads


def build_query(slugs: Sequence[str]) -> str:
    """Return a GraphQL query aliasing one default-branch lookup per slug.

    The alias is the slug's index, because a slug is not a legal GraphQL name.
    :func:`parse_defaults` reads the index back out.
    """
    lines = [
        f'  r{index}: repository(owner: "{slug.split("/")[0]}", '
        f'name: "{slug.split("/")[1]}") '
        "{ defaultBranchRef { name target { oid } } }"
        for index, slug in enumerate(slugs)
    ]
    return "{\n" + "\n".join(lines) + "\n}"


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
    :meth:`refresh_local` re-derives ``behind_default`` from the local refs and
    is cheap enough for every poll: measured on the author's machine, one
    ``for-each-ref`` across 75 repos takes 0.1s at 16 workers.
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
        self._states = {
            repo.path: self._state(
                origin=origins.get(repo.path),
                defaults=cached.defaults,
                prs=cached.prs if cached.prs_known else None,
            )
            for repo in repos
        }
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
        """Ask every origin about its default branch, and report whether it ran.

        Returns False without a network call when the interval has not elapsed
        and ``force`` is unset. Finishes by calling :meth:`refresh_local`, so
        the states this leaves behind are already current against the tree.
        """
        if not (force or self.due(now)):
            return False

        origins = self._origins(repos)
        defaults = self._defaults(github_slugs(origins.values()))
        defaults.update(self._probe(repos, origins))
        prs = self._prs()

        self._states = {
            repo.path: self._state(
                origin=origins.get(repo.path),
                defaults=defaults,
                prs=prs,
            )
            for repo in repos
        }
        self._read_at = now
        self.refresh_local(repos)
        self._store(defaults, prs, now)
        return True

    def _store(
        self,
        defaults: dict[str, tuple[str, str]],
        prs: dict[str, tuple[PullRequest, ...]] | None,
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
                prs={} if prs is None else prs,
                prs_known=prs is not None,
            ),
        )

    def refresh_local(self, repos: Sequence[Repo]) -> None:
        """Recompute ``behind_default`` against the local refs, making no network call.

        Called on every poll. A pull moves ``refs/heads/main`` without telling
        GitHub anything, so the answer has to be re-derived on the fast clock
        or the column keeps reporting a state the user has already fixed.
        """
        known = [
            repo
            for repo in repos
            if self._states.get(repo.path, UNKNOWN).default_sha is not None
        ]
        if not known:
            return

        heads = self._heads(known)
        for repo in known:
            state = self._states[repo.path]
            behind = self._behind(
                repo,
                heads.get(repo.family, {}),
                state.default_branch,
                state.default_sha,
            )
            if behind != state.behind_default:
                self._states[repo.path] = replace(state, behind_default=behind)

    def _state(
        self,
        *,
        origin: str | None,
        defaults: Mapping[str, tuple[str, str]],
        prs: Mapping[str, tuple[PullRequest, ...]] | None,
    ) -> RemoteState:
        """Assemble one repo's network facts. ``behind_default`` is left to later.

        A repo off GitHub reports its PRs known and empty: no search could have
        found one, so an unread marker would promise an answer that never comes.
        """
        if origin is None:
            return UNKNOWN
        slug = parse_slug(origin)
        default = defaults.get(slug or origin)
        branch, sha = default or (None, None)
        return RemoteState(
            origin=origin,
            slug=slug,
            default_branch=branch,
            default_sha=sha,
            default_known=default is not None,
            prs=() if slug is None or prs is None else prs.get(slug, ()),
            prs_known=slug is None or prs is not None,
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
        return {repo.path: found[repo.family] for repo in repos if repo.family in found}

    def _probe(
        self,
        repos: Sequence[Repo],
        origins: Mapping[Path, str],
    ) -> dict[str, tuple[str, str]]:
        """Ask the origins GitHub cannot answer for what their HEAD points at.

        Keyed by URL rather than by path, which is the same key the GraphQL
        answers use and never collides with an ``owner/name``.
        """
        targets = [
            repo
            for repo in leaders(repos)
            if (url := origins.get(repo.path)) is not None and parse_slug(url) is None
        ]

        def probe(repo: Repo) -> tuple[str, tuple[str, str] | None]:
            out = self._ls_remote(repo.path, _LS_REMOTE_ARGS)
            return origins[repo.path], None if out is None else parse_symref(out)

        return {
            url: found for url, found in self._map(probe, targets) if found is not None
        }

    def _heads(self, repos: Sequence[Repo]) -> dict[Path, dict[str, str]]:
        """Read the local branch tips once per family, keyed by family."""

        def heads(repo: Repo) -> tuple[Path, dict[str, str]]:
            out = self._runner(repo.path, _HEADS_ARGS)
            return repo.family, {} if out is None else parse_heads(out)

        return dict(self._map(heads, leaders(repos)))

    def _defaults(self, slugs: Sequence[str]) -> dict[str, tuple[str, str]]:
        """Run the batched default-branch query and merge every chunk's answer."""
        if not slugs:
            return {}
        chunks = [
            tuple(slugs[start : start + BATCH_SIZE])
            for start in range(0, len(slugs), BATCH_SIZE)
        ]

        def lookup(chunk: tuple[str, ...]) -> dict[str, tuple[str, str]]:
            out = self._gh(("api", "graphql", "-f", f"query={build_query(chunk)}"))
            return {} if out is None else parse_defaults(out, chunk)

        merged: dict[str, tuple[str, str]] = {}
        for found in self._map(lookup, chunks):
            merged.update(found)
        return merged

    def _prs(self) -> dict[str, tuple[PullRequest, ...]] | None:
        """Run the one search that covers every repo, or return None on failure."""
        out = self._gh(_PR_SEARCH_ARGS)
        return None if out is None else parse_prs(out)

    def _map[T, R](self, work: Callable[[T], R], items: Sequence[T]) -> list[R]:
        """Run ``work`` over ``items`` across the pool, preserving order."""
        if not items:
            return []
        workers = min(self._max_workers, len(items))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(work, items))

    def forget_absent(self, repos: Iterable[Repo]) -> None:
        """Drop readings and memoized ancestry for repos off the watch list."""
        watched = list(repos)
        live = {repo.path for repo in watched}
        families = {repo.family for repo in watched}
        for path in [key for key in self._states if key not in live]:
            del self._states[path]
        for key in [entry for entry in self._ancestry if entry[0] not in families]:
            del self._ancestry[key]
