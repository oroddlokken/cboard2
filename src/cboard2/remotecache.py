"""Read and write the remote cache file, so a restart and a repeat CLI call are free.

The shape it carries is :class:`cboard2.remote.Cached`.

Only what the network told us is stored, keyed by ``owner/name`` on GitHub and
by the origin URL elsewhere: the default branch and its tip, the tip of each
branch the read asked about by name, and the user's open pull requests. The two
behind markers are deliberately absent — they are derived from the local refs,
so a cached copy would keep reporting ``behind main`` after a pull.
:meth:`cboard2.remote.RemoteReader.refresh_local` recomputes them on load.

The key is the remote rather than the repo path because that is what the answer
is about. Two clones of one repo share an entry, and moving a clone does not
invalidate it.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from cboard2.remote import Cached, PullRequest

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_CACHE_PATH = "CBOARD2_CACHE"
"""Env var pointing at an alternate cache file, for tests and power users."""

VERSION = 2
"""Schema version. A file written by another version is read as a cold cache."""


def cache_path() -> Path:
    """Return the cache file path.

    ``CBOARD2_CACHE`` wins, then ``XDG_CACHE_HOME``, then ``~/.cache`` — the
    same order :func:`cboard2.config.config_path` uses for its own file.
    """
    override = os.environ.get(_ENV_CACHE_PATH)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "cboard2" / "remote.json"


def load(path: Path) -> Cached | None:
    """Read the cache, or return None when there is nothing usable to read.

    A missing, empty, truncated, mistyped or wrong-version file all read as
    None. Nothing here raises: a bad cache costs a network read, not a
    traceback out of a poll.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    body = cast("dict[str, object]", payload)
    if body.get("version") != VERSION:
        return None
    read_at = body.get("read_at")
    if not isinstance(read_at, (int, float)) or isinstance(read_at, bool):
        return None

    defaults: dict[str, tuple[str, str]] = {}
    branches: dict[str, Mapping[str, str]] = {}
    prs: dict[str, tuple[PullRequest, ...]] = {}
    for key, entry in _as_dict(body.get("repos")).items():
        fields = _as_dict(entry)
        branch = fields.get("default_branch")
        sha = fields.get("default_sha")
        if isinstance(branch, str) and isinstance(sha, str):
            defaults[key] = (branch, sha)
        tips = _tips(fields.get("branches"))
        if tips:
            branches[key] = tips
        found = _requests(fields.get("prs"))
        if found:
            prs[key] = found

    return Cached(
        read_at=float(read_at),
        defaults=defaults,
        branches=branches,
        prs=prs,
        prs_known=body.get("prs_known") is True,
    )


def save(path: Path, cached: Cached) -> bool:
    """Write the cache and report whether it landed.

    The write goes to a temp file in the same directory and is moved into
    place, so a dashboard reading the file never sees a half-written one. An
    unwritable directory returns False rather than raising: the process still
    has the reading in memory, and losing the cache is not worth failing a
    poll over.
    """
    body = {
        "version": VERSION,
        "read_at": cached.read_at,
        "prs_known": cached.prs_known,
        "repos": {
            key: _entry(cached, key)
            for key in sorted(
                set(cached.defaults) | set(cached.branches) | set(cached.prs),
            )
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name,
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(body, handle, indent=2)
            temp = Path(handle.name)
    except OSError:
        return False

    try:
        os.replace(temp, path)  # noqa: PTH105 — Path has no atomic-replace method
    except OSError:
        temp.unlink(missing_ok=True)
        return False
    return True


def _entry(cached: Cached, key: str) -> dict[str, object]:
    """Return one remote's stored fields."""
    branch, sha = cached.defaults.get(key, (None, None))
    return {
        "default_branch": branch,
        "default_sha": sha,
        "branches": dict(cached.branches.get(key, {})),
        "prs": [
            {
                "number": pr.number,
                "title": pr.title,
                "url": pr.url,
                "draft": pr.draft,
                "updated_at": pr.updated_at,
            }
            for pr in cached.prs.get(key, ())
        ],
    }


def _tips(value: object) -> dict[str, str]:
    """Read one remote's stored branch tips, skipping anything not two strings."""
    return {
        name: sha
        for name, sha in _as_dict(value).items()
        if isinstance(sha, str) and name
    }


def _requests(value: object) -> tuple[PullRequest, ...]:
    """Read one remote's stored PRs, skipping any entry missing a number."""
    if not isinstance(value, list):
        return ()
    found: list[PullRequest] = []
    for item in cast("list[object]", value):
        fields = _as_dict(item)
        number = fields.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        updated = fields.get("updated_at")
        found.append(
            PullRequest(
                number=number,
                title=_text(fields.get("title")),
                url=_text(fields.get("url")),
                draft=fields.get("draft") is True,
                updated_at=float(updated)
                if isinstance(updated, (int, float)) and not isinstance(updated, bool)
                else None,
            ),
        )
    return tuple(found)


def _as_dict(value: object) -> dict[str, object]:
    """Return ``value`` as a string-keyed mapping, or an empty one."""
    if not isinstance(value, dict):
        return {}
    return cast("dict[str, object]", value)


def _text(value: object) -> str:
    """Return ``value`` when it is a string, else the empty string."""
    return value if isinstance(value, str) else ""
