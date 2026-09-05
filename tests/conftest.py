"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Protocol

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class RepoFactory(Protocol):
    """Creates a directory that discovery will treat as a git repo."""

    def __call__(self, relative: str, *, gitfile: bool = False) -> Path:
        """Create the repo at ``relative`` under the tree root and return its path."""
        ...


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Return an empty directory to build a repo layout under."""
    return tmp_path


@pytest.fixture
def make_repo(tree: Path) -> RepoFactory:
    """Return a factory that plants ``.git`` markers inside :fixture:`tree`.

    The marker is a directory by default and a file when ``gitfile`` is set,
    which is how git records a submodule.
    """

    def factory(relative: str, *, gitfile: bool = False) -> Path:
        repo = tree / relative
        repo.mkdir(parents=True, exist_ok=True)
        marker = repo / ".git"
        if gitfile:
            marker.write_text("gitdir: ../.git/modules/sub\n", encoding="utf-8")
        else:
            marker.mkdir(exist_ok=True)
        return repo

    return factory


_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}
"""Env that keeps test repos out of reach of the machine's real git config."""


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stdout, raising on a non-zero exit."""
    return _git(cwd, args, check=True).stdout


def git_may_fail(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` and return the result, non-zero exit included.

    For commands whose failure is the point, such as a merge that conflicts.
    """
    return _git(cwd, args, check=False)


def _git(
    cwd: Path,
    args: Sequence[str],
    *,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Path:
    """Return an initialized repo on ``main`` with no commits yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main", "-q")
    return repo


@pytest.fixture
def git_repo(empty_git_repo: Path) -> Path:
    """Return a repo on ``main`` holding one commit of ``tracked.txt``."""
    (empty_git_repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    git(empty_git_repo, "add", "tracked.txt")
    git(empty_git_repo, "commit", "-qm", "Add tracked file")
    return empty_git_repo


@pytest.fixture
def worktree(git_repo: Path) -> Path:
    """Return a linked worktree of :fixture:`git_repo`, checked out on ``side``."""
    tree = git_repo.parent / "side"
    git(git_repo, "worktree", "add", "-q", "-b", "side", str(tree))
    return tree


class RecordingRunner:
    """A git runner that records every call and replays canned output.

    ``outputs`` is keyed by git subcommand; a subcommand with no entry returns
    None, which every caller treats as a failed git call.
    """

    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[Path, tuple[str, ...]]] = []

    def __call__(self, root: Path, args: Sequence[str]) -> str | None:
        """Record the call and return the canned output for its subcommand."""
        self.calls.append((root, tuple(args)))
        return self.outputs.get(args[0])

    def paths_for(self, subcommand: str) -> list[Path]:
        """Return the repo paths ``subcommand`` was called on, in call order."""
        return [root for root, args in self.calls if args[0] == subcommand]
