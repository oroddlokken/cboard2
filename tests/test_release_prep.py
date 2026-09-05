"""Tests for ``scripts/release-prep`` against a local bare-repo remote.

The script switches branches in the clone rather than in a worktree, so the
cases here cover what that costs: the branch it leaves you on, a release
branch that exists only on the remote, and a rejected push that must not
publish its RC tag.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from conftest import _GIT_ENV, git

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release-prep"

CHANGELOG = "# CHANGELOG\n\n## [Unreleased]\n\n### Fixed\n\n- Something\n"


def _make_gh_stub(bin_dir: Path) -> None:
    """Put a ``gh`` on PATH that reports no PR and creates a fake one.

    release-prep calls gh for PR discovery and creation; neither is under test
    here, and a real gh would try to reach GitHub.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1 $2" == "pr create" ]]; then\n'
        '    echo "https://example.invalid/pr/1"\n'
        "    exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _run_script(
    repo: Path,
    bin_dir: Path,
    args: Sequence[str],
) -> subprocess.CompletedProcess[str]:
    """Run release-prep in ``repo`` with the gh stub ahead of the real PATH."""
    env = {
        **os.environ,
        **_GIT_ENV,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
    }
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


@pytest.fixture
def remote_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Return a bare remote plus a clone on ``main`` holding a CHANGELOG."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", "-q", str(remote))

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main", "-q")
    git(repo, "remote", "add", "origin", str(remote))
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Initial")
    git(repo, "push", "-qu", "origin", "main")
    return remote, repo


@pytest.fixture
def gh_stub(tmp_path: Path) -> Path:
    """Return a bin directory holding the ``gh`` stub."""
    bin_dir = tmp_path / "bin"
    _make_gh_stub(bin_dir)
    return bin_dir


class TestRemoteOnlyBranch:
    """A release branch on the remote that the clone does not have."""

    def test_reuses_remote_branch_instead_of_forking(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The rerun builds on the remote tip and pushes without a conflict.

        Probing only ``refs/heads/<branch>`` would branch off main and produce
        a sibling of the remote tip that no push can fast-forward.
        """
        remote, repo = remote_and_clone

        # Previous RC, cut elsewhere: branch and tag exist only on the remote.
        git(repo, "checkout", "-q", "-b", "release/v9.9.9")
        (repo / "CHANGELOG.md").write_text(
            CHANGELOG.replace(
                "## [Unreleased]",
                "## [Unreleased]\n\n## 9.9.9 (2026-01-01)",
            ),
            encoding="utf-8",
        )
        git(repo, "commit", "-qam", "Prepare changelog for v9.9.9")
        git(repo, "tag", "-a", "v9.9.9-rc.1", "-m", "rc1")
        git(repo, "push", "-q", "origin", "release/v9.9.9", "--tags")
        git(repo, "checkout", "-q", "main")
        git(repo, "branch", "-qD", "release/v9.9.9")
        git(repo, "tag", "-d", "v9.9.9-rc.1")

        # A fix lands on main after that RC, which is why another is cut.
        (repo / "fix.txt").write_text("fix\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "Fix something")
        git(repo, "push", "-q", "origin", "main")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.9"])

        assert result.returncode == 0, result.stdout + result.stderr

        remote_tip = git(remote, "rev-parse", "release/v9.9.9").strip()
        assert git(repo, "rev-parse", "release/v9.9.9").strip() == remote_tip
        # The branch carries the fix, so it was re-cut from main rather than
        # left at the previous RC.
        assert "fix.txt" in git(repo, "ls-tree", "--name-only", remote_tip)
        assert git(remote, "rev-parse", "v9.9.9-rc.2^{}").strip() == remote_tip

    def test_leaves_the_clone_on_the_original_branch(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The checkout is undone on exit, however the run ended."""
        _remote, repo = remote_and_clone

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.7"])

        assert result.returncode == 0, result.stdout + result.stderr
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


class TestRejectedBranchPush:
    """A branch push the remote refuses must not leave a tag behind."""

    def test_rejected_push_publishes_no_tag(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """``push --tags`` would send the RC tag even when the branch failed."""
        remote, repo = remote_and_clone

        # The remote branch holds a commit this clone's branch does not, so
        # the non-fast-forward push is rejected.
        git(repo, "checkout", "-q", "-b", "release/v9.9.8")
        (repo / "theirs.txt").write_text("theirs\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "Their release work")
        git(repo, "push", "-q", "origin", "release/v9.9.8")
        git(repo, "checkout", "-q", "main")
        # Rewind the local branch so it no longer contains the remote tip.
        git(repo, "branch", "-f", "release/v9.9.8", "main")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.8"])

        assert result.returncode != 0, result.stdout
        assert git(remote, "tag", "-l", "v9.9.8-rc.1").strip() == ""
        assert git(repo, "tag", "-l", "v9.9.8-rc.1").strip() == ""
        assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


class TestDirtyTree:
    """An uncommitted edit stops the run before any branch is touched."""

    def test_dirty_tree_aborts(
        self,
        remote_and_clone: tuple[Path, Path],
        gh_stub: Path,
    ) -> None:
        """The edit would otherwise ride into the release commit."""
        _remote, repo = remote_and_clone
        (repo / "CHANGELOG.md").write_text("edited\n", encoding="utf-8")

        result = _run_script(repo, gh_stub, ["-W", "--skip-checks", "9.9.6"])

        assert result.returncode != 0
        assert "dirty" in result.stderr
        assert git(repo, "branch", "-l", "release/v9.9.6").strip() == ""
