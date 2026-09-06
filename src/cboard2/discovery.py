"""Find the repos cboard2 watches by walking the configured roots.

There is no hook and no registration step: a clone appears on the dashboard
because it is on disk under a root. Walking ``~`` to depth 4 finds 109 repos in
0.3s on the author's machine, so the walk is the source of truth and the cache
below is only a way to skip repeating it.

:func:`save_repos` and :func:`load_repos` carry the last walk's result between
processes. A one-shot ``cboard ls`` runs once per shell prompt and would
otherwise pay the walk every time. The stored list is keyed by the watch list
that produced it and expires, so a clone or a deletion is still picked up.

A linked worktree gets its own row: it has its own HEAD, branch and dirty tree.
It carries the git directory of the repo it belongs to, so the row can name
that repo and callers can ask about shared refs once per repo instead of once
per worktree.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Sequence

    from cboard2.config import Config

_ENV_REPO_CACHE = "CBOARD2_REPO_CACHE"
"""Env var pointing at an alternate repo-list cache file, for tests."""

CACHE_VERSION = 1
"""Schema version. A file written by another version is read as a cold cache."""


@dataclass(frozen=True, slots=True)
class Repo:
    """A repo found on disk, and whether it is polled on the slow clock."""

    path: Path
    name: str
    dormant: bool
    main_git_dir: Path | None = None
    """The main repo's git directory when this is a linked worktree.

    None for a clone and for a submodule, whose ``.git`` file points into
    ``modules`` rather than ``worktrees``.
    """

    @property
    def family(self) -> Path:
        """The git directory shared with this repo's linked worktrees.

        The origin URL and the branch tips live there, so a caller reading
        either groups on this and asks once per repo.
        """
        return self.main_git_dir or self.path / ".git"


def discover(config: Config) -> list[Repo]:
    """Return every repo under the configured roots, sorted by path.

    A path reached through two overlapping roots is returned once. Unless
    ``worktrees`` is off, each repo's linked worktrees are added too, including
    the ones kept inside the repo where the walk would never reach them.
    """
    dormant = frozenset(config.dormant)
    found: dict[Path, Repo] = {}
    for root in config.roots:
        for path in _walk(root, config.max_depth):
            _add(found, path, dormant)

    if config.worktrees:
        for repo in list(found.values()):
            if repo.main_git_dir is None:
                for tree in linked_worktrees(repo.path):
                    _add(found, tree, dormant)

    return sorted(found.values(), key=lambda repo: repo.path)


def is_dormant(path: Path, dormant: Collection[Path]) -> bool:
    """Return True when ``path`` is listed dormant or sits under a listed path.

    Looks up ``path`` and its ancestors rather than testing every listed path,
    so the cost follows the depth of the repo and not the length of the list.
    Both are lexical comparisons, so the two agree entry for entry.
    """
    if not dormant:
        return False
    return path in dormant or any(parent in dormant for parent in path.parents)


def git_dir(root: Path) -> Path:
    """Return the repo's git directory, following a ``.git`` file if there is one.

    A submodule's and a worktree's ``.git`` is a file holding ``gitdir: <path>``,
    so their refs and reflog live outside the working tree. Read here rather
    than through ``git rev-parse`` to keep discovery and the reflog's mtime gate
    free of subprocesses.
    """
    marker = root / ".git"
    try:
        if marker.is_dir():
            return marker
        text = marker.read_text(encoding="utf-8")
    except OSError:
        return marker

    _, _, target = text.partition("gitdir:")
    pointer = target.strip()
    if not pointer:
        return marker
    resolved = Path(pointer)
    return resolved if resolved.is_absolute() else root / resolved


def main_git_dir(root: Path) -> Path | None:
    """Return the main repo's git directory when ``root`` is a linked worktree.

    A worktree's git directory is ``<main>/worktrees/<name>``, which is what
    separates it from a submodule's ``<main>/modules/<name>``.
    """
    target = git_dir(root)
    if target.parent.name != "worktrees":
        return None
    return target.parent.parent


def main_name(git_directory: Path) -> str:
    """Return the name of the repo whose git directory this is."""
    if git_directory.name == ".git":
        return git_directory.parent.name
    return git_directory.name.removesuffix(".git")


def linked_worktrees(root: Path) -> list[Path]:
    """Return the working trees git has registered as worktrees of ``root``.

    Read from ``<git dir>/worktrees/*/gitdir``, whose contents are the path of
    each worktree's own ``.git`` file. A worktree whose directory has been
    deleted keeps its entry until ``git worktree prune`` runs, so the tree is
    checked for before it is returned.
    """
    try:
        entries = sorted((git_dir(root) / "worktrees").iterdir())
    except OSError:
        return []

    found: list[Path] = []
    for entry in entries:
        try:
            pointer = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not pointer:
            continue
        tree = Path(pointer).parent
        if _is_repo(tree):
            found.append(tree)
    return found


def _add(found: dict[Path, Repo], path: Path, dormant: Collection[Path]) -> None:
    """Record ``path`` as a repo, keeping the entry already there."""
    if path in found:
        return
    found[path] = Repo(
        path=path,
        name=path.name,
        dormant=is_dormant(path, dormant),
        main_git_dir=main_git_dir(path),
    )


def _walk(root: Path, max_depth: int) -> Iterator[Path]:
    """Yield repo roots under ``root``, stopping at each one found.

    A repo is never descended into, so a submodule or a nested clone does not
    produce a second row for the same work.
    """
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if _is_repo(directory):
            yield directory
            continue
        if depth >= max_depth:
            continue
        stack.extend((child, depth + 1) for child in _subdirs(directory))


def _is_repo(directory: Path) -> bool:
    """Return True when ``directory`` holds a ``.git`` entry.

    Tested with ``exists`` rather than ``is_dir`` because a submodule's and a
    worktree's ``.git`` is a file pointing at the git directory.
    """
    return (directory / ".git").exists()


def _subdirs(directory: Path) -> list[Path]:
    """List subdirectories, skipping symlinks and dot-directories.

    Symlinks are skipped because one link back up the tree makes the walk
    unbounded; dot-directories because ``~/.cache`` and its neighbours hold
    nothing worth the traversal. Name either as an explicit root to include it.
    """
    try:
        with os.scandir(directory) as entries:
            return [
                Path(entry.path)
                for entry in entries
                if not entry.name.startswith(".")
                and entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []


def repo_cache_path() -> Path:
    """Return the repo-list cache file path.

    ``CBOARD2_REPO_CACHE`` wins, then ``XDG_CACHE_HOME``, then ``~/.cache`` —
    the order :func:`cboard2.remotecache.cache_path` uses for its own file.
    """
    override = os.environ.get(_ENV_REPO_CACHE)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "cboard2" / "repos.json"


def load_repos(
    path: Path,
    config: Config,
    moment: float,
    ttl: float,
) -> list[Repo] | None:
    """Return the stored repo list, or None when a walk is owed.

    None covers a missing, empty, truncated, mistyped or wrong-version file, a
    file written under a different watch list, and one older than ``ttl``
    seconds or stamped ahead of ``moment``. Nothing here raises: a bad cache
    costs a walk, not a traceback out of a poll.
    """
    body = _read(path)
    if body is None:
        return None
    if body.get("version") != CACHE_VERSION or body.get("watching") != _watching(
        config,
    ):
        return None
    scanned_at = body.get("scanned_at")
    if not isinstance(scanned_at, (int, float)) or isinstance(scanned_at, bool):
        return None
    age = moment - float(scanned_at)
    if age < 0 or age >= ttl:
        return None
    return _stored_repos(body.get("repos"))


def save_repos(
    path: Path,
    config: Config,
    repos: Sequence[Repo],
    moment: float,
) -> bool:
    """Write the repo list and report whether it landed.

    The write goes to a temp file in the same directory and is moved into
    place, so a process reading the file never sees a half-written one. An
    unwritable directory returns False rather than raising: the caller already
    has the list, and losing the cache costs the next process one walk.
    """
    body = {
        "version": CACHE_VERSION,
        "scanned_at": moment,
        "watching": _watching(config),
        "repos": [_stored(repo) for repo in repos],
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


def _read(path: Path) -> dict[str, object] | None:
    """Return the file as a mapping, or None when there is nothing usable in it."""
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
    return cast("dict[str, object]", payload)


def _watching(config: Config) -> dict[str, object]:
    """Return the config keys that decide which repos a walk finds.

    A cache written under different keys answers a different question, so it
    is discarded rather than filtered. ``dormant`` is in here because it is
    stored per repo; the poll intervals are not, because they never are.
    """
    return {
        "roots": sorted(str(root) for root in config.roots),
        "max_depth": config.max_depth,
        "dormant": sorted(str(entry) for entry in config.dormant),
        "worktrees": config.worktrees,
    }


def _stored(repo: Repo) -> dict[str, object]:
    """Return one repo as the fields the file carries."""
    return {
        "path": str(repo.path),
        "name": repo.name,
        "dormant": repo.dormant,
        "main_git_dir": None if repo.main_git_dir is None else str(repo.main_git_dir),
    }


def _stored_repos(value: object) -> list[Repo] | None:
    """Read the stored repos, or None when any entry is unusable.

    One bad entry discards the whole list: a half-read list would drop a repo
    from the dashboard until the next walk, which looks like a deletion.
    """
    if not isinstance(value, list):
        return None
    found: list[Repo] = []
    for item in cast("list[object]", value):
        repo = _stored_repo(item)
        if repo is None:
            return None
        found.append(repo)
    return found


def _stored_repo(item: object) -> Repo | None:
    """Read one stored repo, or None when a field is missing or mistyped."""
    if not isinstance(item, dict):
        return None
    fields = cast("dict[str, object]", item)
    path = fields.get("path")
    name = fields.get("name")
    dormant = fields.get("dormant")
    main = fields.get("main_git_dir")
    if not isinstance(path, str) or not isinstance(name, str):
        return None
    if not isinstance(dormant, bool):
        return None
    if main is not None and not isinstance(main, str):
        return None
    return Repo(
        path=Path(path),
        name=name,
        dormant=dormant,
        main_git_dir=None if main is None else Path(main),
    )
