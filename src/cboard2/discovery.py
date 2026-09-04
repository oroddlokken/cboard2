"""Find the repos cboard2 watches by walking the configured roots.

There is no hook and no registration step: a clone appears on the dashboard
because it is on disk under a root. Walking ``~`` to depth 4 finds 109 repos in
0.3s on the author's machine, so this runs at startup rather than being cached
as the source of truth.

A linked worktree gets its own row: it has its own HEAD, branch and dirty tree.
It carries the git directory of the repo it belongs to, so the row can name
that repo and callers can ask about shared refs once per repo instead of once
per worktree.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from cboard2.config import Config


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
    found: dict[Path, Repo] = {}
    for root in config.roots:
        for path in _walk(root, config.max_depth):
            _add(found, path, config)

    if config.worktrees:
        for repo in list(found.values()):
            if repo.main_git_dir is None:
                for tree in linked_worktrees(repo.path):
                    _add(found, tree, config)

    return sorted(found.values(), key=lambda repo: repo.path)


def is_dormant(path: Path, dormant: Iterable[Path]) -> bool:
    """Return True when ``path`` is listed dormant or sits under a listed path."""
    return any(path.is_relative_to(entry) for entry in dormant)


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


def _add(found: dict[Path, Repo], path: Path, config: Config) -> None:
    """Record ``path`` as a repo, keeping the entry already there."""
    if path in found:
        return
    found[path] = Repo(
        path=path,
        name=path.name,
        dormant=is_dormant(path, config.dormant),
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
