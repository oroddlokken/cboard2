"""Find the repos cboard2 watches by walking the configured roots.

There is no hook and no registration step: a clone appears on the dashboard
because it is on disk under a root. Walking ``~`` to depth 4 finds 109 repos in
0.3s on the author's machine, so this runs at startup rather than being cached
as the source of truth.
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


def discover(config: Config) -> list[Repo]:
    """Return every repo under the configured roots, sorted by path.

    A path reached through two overlapping roots is returned once.
    """
    found: dict[Path, Repo] = {}
    for root in config.roots:
        for path in _walk(root, config.max_depth):
            if path not in found:
                found[path] = Repo(
                    path=path,
                    name=path.name,
                    dormant=is_dormant(path, config.dormant),
                )
    return sorted(found.values(), key=lambda repo: repo.path)


def is_dormant(path: Path, dormant: Iterable[Path]) -> bool:
    """Return True when ``path`` is listed dormant or sits under a listed path."""
    return any(path.is_relative_to(entry) for entry in dormant)


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

    Tested with ``exists`` rather than ``is_dir`` because a submodule's ``.git``
    is a file pointing at the parent's git dir.
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
