"""Which repos cboard2 watches, how deep it looks, and which it polls rarely.

Every key has a default, so an absent config file is the normal case and not an
error. A file that exists but holds an unusable value raises
:class:`ConfigError` naming the key, rather than falling back and hiding it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cboard2.duration import parse_duration
from cboard2.remote import DEFAULT_REMOTE_INTERVAL

_ENV_CONFIG_PATH = "CBOARD2_CONFIG"
"""Env var pointing at an alternate config file, for tests and power users."""

DEFAULT_ROOTS: tuple[str, ...] = ("~/git",)
"""Searched when no ``roots`` key is set.

Narrower than ``~`` on purpose: a home-wide walk also turns up the clones in
``~/Trash`` and ``~/Library``, which nobody is working in. Add a root for
anything kept elsewhere.
"""

DEFAULT_MAX_DEPTH = 1
"""Directory levels below a root that the walk descends before giving up.

1 means direct children only, so ``~/git/keyforge`` is found and
``~/git/_old/network-routing-overview`` is not. Set 2 for a
``~/git/<org>/<repo>`` layout.
"""

DEFAULT_DORMANT_INTERVAL = 4 * 3600.0
"""Seconds between polls of a dormant repo."""

DEFAULT_REMOTE = True
"""Whether the dashboard asks each origin about default branches and pull requests.

Set ``remote = false`` to keep cboard2 entirely off the network. Leaving it on
costs nothing where ``gh`` is missing or unauthed: those repos read as unknown.
"""

DEFAULT_ORIGIN_COLORS = True
"""Whether a repo's name is colored by the host and owner of its origin.

Every clone under one owner takes one color, so a table mixing work and
personal repos separates at a glance. Set ``origin_colors = false`` for
uncolored names. The origin is read from local git config, so this is
independent of ``remote``.
"""

DEFAULT_WORKTREES = True
"""Whether a repo's linked worktrees get rows of their own.

A worktree has its own HEAD and its own dirty tree, so it is work the dashboard
would otherwise hide. Set ``worktrees = false`` to see one row per clone.
"""

DEFAULT_WORKTREE_LIMIT = 5
"""Worktree rows the dashboard paints per repo before folding the rest away.

A repo with thirty worktrees fills the screen on its own, so the rest sit
behind one row that enter expands. 0 folds every worktree away.
"""


class ConfigError(Exception):
    """A config file was found, but one of its keys cannot be used."""


@dataclass(frozen=True, slots=True)
class Config:
    """The watch list: where to look, how deep, and what to poll on the slow clock."""

    roots: tuple[Path, ...]
    max_depth: int
    dormant: tuple[Path, ...]
    dormant_interval: float
    remote: bool
    remote_interval: float
    origin_colors: bool
    worktrees: bool
    worktree_limit: int


def config_path() -> Path:
    """Return the config file path.

    ``CBOARD2_CONFIG`` wins, then ``XDG_CONFIG_HOME``, then ``~/.config``.
    """
    override = os.environ.get(_ENV_CONFIG_PATH)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "cboard2" / "config.toml"


def default_config() -> Config:
    """Return the config used when no file exists."""
    return Config(
        roots=tuple(Path(root).expanduser() for root in DEFAULT_ROOTS),
        max_depth=DEFAULT_MAX_DEPTH,
        dormant=(),
        dormant_interval=DEFAULT_DORMANT_INTERVAL,
        remote=DEFAULT_REMOTE,
        remote_interval=DEFAULT_REMOTE_INTERVAL,
        origin_colors=DEFAULT_ORIGIN_COLORS,
        worktrees=DEFAULT_WORKTREES,
        worktree_limit=DEFAULT_WORKTREE_LIMIT,
    )


def load_config(path: Path | None = None) -> Config:
    """Read the config file, or return :func:`default_config` if it is absent."""
    target = path or config_path()
    try:
        raw = target.read_bytes()
    except OSError:
        return default_config()

    try:
        data: dict[str, object] = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"{target} is not valid TOML: {exc}"
        raise ConfigError(msg) from exc

    return Config(
        roots=_paths(data, "roots", DEFAULT_ROOTS),
        max_depth=_depth(data),
        dormant=_paths(data, "dormant", ()),
        dormant_interval=_interval(data, "dormant_interval", DEFAULT_DORMANT_INTERVAL),
        remote=_flag(data, "remote", fallback=DEFAULT_REMOTE),
        remote_interval=_interval(data, "remote_interval", DEFAULT_REMOTE_INTERVAL),
        origin_colors=_flag(data, "origin_colors", fallback=DEFAULT_ORIGIN_COLORS),
        worktrees=_flag(data, "worktrees", fallback=DEFAULT_WORKTREES),
        worktree_limit=_worktree_limit(data),
    )


def _paths(
    data: dict[str, object], key: str, fallback: tuple[str, ...]
) -> tuple[Path, ...]:
    """Read a list-of-strings key into expanded paths."""
    value = data.get(key, list(fallback))
    if not isinstance(value, list):
        msg = f"{key} must be a list of paths"
        raise ConfigError(msg)
    items = cast("list[object]", value)
    entries = [item for item in items if isinstance(item, str)]
    if len(entries) != len(items):
        msg = f"every entry in {key} must be a string path"
        raise ConfigError(msg)
    return tuple(Path(entry).expanduser() for entry in entries)


def _depth(data: dict[str, object]) -> int:
    """Read ``max_depth``, rejecting a non-integer or negative value."""
    value = data.get("max_depth", DEFAULT_MAX_DEPTH)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = "max_depth must be a non-negative integer"
        raise ConfigError(msg)
    return value


def _worktree_limit(data: dict[str, object]) -> int:
    """Read ``worktree_limit``, rejecting a non-integer or negative value."""
    value = data.get("worktree_limit", DEFAULT_WORKTREE_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        msg = "worktree_limit must be a non-negative integer"
        raise ConfigError(msg)
    return value


def _flag(data: dict[str, object], key: str, *, fallback: bool) -> bool:
    """Read a boolean key, rejecting anything but a TOML boolean."""
    value = data.get(key, fallback)
    if not isinstance(value, bool):
        msg = f"{key} must be true or false"
        raise ConfigError(msg)
    return value


def _interval(data: dict[str, object], key: str, fallback: float) -> float:
    """Read a duration key through the shared parser."""
    value = data.get(key)
    if value is None:
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        msg = f"{key} must be a duration string such as '4h'"
        raise ConfigError(msg)
    try:
        return parse_duration(value)
    except ValueError as exc:
        msg = f"{key}: {exc}"
        raise ConfigError(msg) from exc
