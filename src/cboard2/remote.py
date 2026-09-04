"""A moved default branch and the user's own open pull requests, read from GitHub.

``git status --porcelain=v2 --branch`` reports ``behind`` against the local
remote-tracking ref, so it stays at zero until something fetches. This module
asks GitHub directly instead, and never fetches: a poll that rewrote
``refs/remotes/origin/*`` under the user would change what their next ``git
log`` shows.

Two network calls cover every repo. One batched GraphQL query returns each
repo's default branch and its tip; one ``gh search prs`` returns every open PR
the user authored, anywhere. Measured on the author's machine: 66 repos in one
query took 4.4s, against 1.7s per repo for ``git ls-remote`` — which is why the
query is batched and why the read runs on its own slow interval rather than the
2s poll tick.

The read runs on two clocks. :meth:`RemoteReader.read` asks GitHub on the slow
one; :meth:`RemoteReader.refresh_local` recomputes whether the answer has been
merged in yet, from the local refs, on every poll. Without that split a pull
stays invisible for the rest of the network interval, which is the whole
question the column exists to answer.

Everything degrades to unknown. A missing ``gh``, an expired token, a
non-GitHub origin and a repo that was deleted upstream all leave the row saying
so, rather than reading as current.
"""

from __future__ import annotations

import json
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


def parse_slug(url: str) -> str | None:
    """Return ``owner/name`` for a GitHub origin URL, or None for anything else."""
    match = _SLUG_PATTERN.search(url.strip())
    if match is None:
        return None
    return f"{match['owner']}/{match['name']}"


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
        gh: GhRunner = run_gh,
        load: CacheLoader | None = None,
        save: CacheSaver | None = None,
    ) -> None:
        self._interval = interval
        self._max_workers = max_workers
        self._runner = runner
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

        slugs = self._slugs(repos)
        self._states = {
            repo.path: self._state(
                slug=slugs.get(repo.path),
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
        """Ask GitHub about every repo and return whether anything was read.

        Returns False without a network call when the interval has not elapsed
        and ``force`` is unset. Finishes by calling :meth:`refresh_local`, so
        the states this leaves behind are already current against the tree.
        """
        if not (force or self.due(now)):
            return False

        slugs = self._slugs(repos)
        defaults = self._defaults(sorted(set(slugs.values())))
        prs = self._prs()

        self._states = {
            repo.path: self._state(
                slug=slugs.get(repo.path),
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
                repo.path,
                heads.get(repo.path, {}),
                state.default_branch,
                state.default_sha,
            )
            if behind != state.behind_default:
                self._states[repo.path] = replace(state, behind_default=behind)

    def _state(
        self,
        *,
        slug: str | None,
        defaults: Mapping[str, tuple[str, str]],
        prs: Mapping[str, tuple[PullRequest, ...]] | None,
    ) -> RemoteState:
        """Assemble one repo's network facts. ``behind_default`` is left to later."""
        if slug is None:
            return UNKNOWN
        default = defaults.get(slug)
        branch, sha = default or (None, None)
        return RemoteState(
            slug=slug,
            default_branch=branch,
            default_sha=sha,
            default_known=default is not None,
            prs=() if prs is None else prs.get(slug, ()),
            prs_known=prs is not None,
        )

    def _behind(
        self,
        path: Path,
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

        The answer is memoized on both shas, so a repo nobody has touched costs
        no process on the next poll.
        """
        if branch is None or sha is None:
            return False
        local = heads.get(branch)
        if local is None or local == sha:
            return False

        key = (path, local, sha)
        contained = self._ancestry.get(key)
        if contained is None:
            contained = (
                self._runner(path, ("merge-base", "--is-ancestor", sha, local))
                is not None
            )
            self._ancestry[key] = contained
        return not contained

    def _slugs(self, repos: Sequence[Repo]) -> dict[Path, str]:
        """Resolve each repo's origin to ``owner/name``, dropping the rest."""

        def slug(repo: Repo) -> tuple[Path, str | None]:
            out = self._runner(repo.path, _ORIGIN_ARGS)
            return repo.path, None if out is None else parse_slug(out)

        return {
            path: found for path, found in self._map(slug, repos) if found is not None
        }

    def _heads(self, repos: Sequence[Repo]) -> dict[Path, dict[str, str]]:
        """Read every repo's local branch tips."""

        def heads(repo: Repo) -> tuple[Path, dict[str, str]]:
            out = self._runner(repo.path, _HEADS_ARGS)
            return repo.path, {} if out is None else parse_heads(out)

        return dict(self._map(heads, repos))

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
        live = {repo.path for repo in repos}
        for path in [key for key in self._states if key not in live]:
            del self._states[path]
        for key in [entry for entry in self._ancestry if entry[0] not in live]:
            del self._ancestry[key]
