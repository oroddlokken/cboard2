"""Check out a repo's default branch and pull, as the dashboard's ``P`` key does.

This is the user's ``gmp`` script in Python, so cboard2 does the same thing on a
machine that has no dotfiles. Every git call here writes to the repo, which is
why it is the only module in cboard2 that does: the rest only reads.

The default branch is asked for in three ways, because no single one is
reliable. GitHub's answer is best and cboard2 usually holds it already.
``refs/remotes/origin/HEAD`` is next, but it is written once at clone time and
never refreshed, so a renamed default branch leaves it pointing at a ref that
is gone — hence the repair step. ``main`` and ``master`` are the last resort.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    type StepRunner = Callable[[Path, Sequence[str]], Step]

PULL_TIMEOUT = 180.0
"""Seconds a single git call gets. A fetch is not a five-second operation."""

CANDIDATE_BRANCHES = ("main", "master")
"""Tried in order when neither GitHub nor ``origin/HEAD`` names the branch."""

_PULL_ENV = {"GIT_TERMINAL_PROMPT": "0"}
"""Keeps a fetch that wants credentials from hanging the worker forever."""


@dataclass(frozen=True, slots=True)
class Step:
    """One git call's result, with both streams kept.

    :func:`cboard2.gitstate.run_git` cannot stand in for this: it throws away
    the exit code and stderr, and the user needs to be told what failed.
    """

    ok: bool
    out: str
    err: str

    @property
    def text(self) -> str:
        """The call's output, stdout first, with blank lines dropped."""
        return "\n".join(part.strip() for part in (self.out, self.err) if part.strip())


@dataclass(frozen=True, slots=True)
class Outcome:
    """What the pull did, in a form the dashboard can put in a notification."""

    ok: bool
    message: str
    branch: str | None = None


def run_step(root: Path, args: Sequence[str]) -> Step:
    """Run one git command in ``root``, keeping its exit code and both streams."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=PULL_TIMEOUT,
            check=False,
            env={**os.environ, **_PULL_ENV},
        )
    except subprocess.TimeoutExpired:
        return Step(ok=False, out="", err=f"git {args[0]} timed out")
    except (OSError, subprocess.SubprocessError) as exc:
        return Step(ok=False, out="", err=f"git {args[0]} could not run: {exc}")
    return Step(
        ok=result.returncode == 0,
        out=result.stdout,
        err=result.stderr,
    )


def pull_default(
    root: Path,
    *,
    default_branch: str | None = None,
    runner: StepRunner = run_step,
) -> Outcome:
    """Fetch, move to the default branch and pull, stopping at the first failure.

    ``default_branch`` is GitHub's answer where cboard2 has one, which skips
    the guessing entirely.
    """
    if not runner(root, ("rev-parse", "--git-dir")).ok:
        return Outcome(ok=False, message="not a git repository")

    fetched = runner(root, ("fetch", "--prune"))
    if not fetched.ok:
        return Outcome(ok=False, message=_reason("fetch failed", fetched))

    branch = default_branch or find_default_branch(root, runner)
    if branch is None:
        return Outcome(
            ok=False,
            message="no default branch found: no origin/HEAD, main or master",
        )

    moved = _checkout(root, branch, runner)
    if moved is not None:
        return moved

    return _pull(root, branch, runner)


def find_default_branch(root: Path, runner: StepRunner) -> str | None:
    """Name the default branch from ``origin/HEAD``, then from the candidates."""
    named = _origin_head(root, runner)
    if named is not None:
        return named
    for candidate in CANDIDATE_BRANCHES:
        if _has_ref(root, f"refs/heads/{candidate}", runner) or _has_ref(
            root,
            f"refs/remotes/origin/{candidate}",
            runner,
        ):
            return candidate
    return None


def _origin_head(root: Path, runner: StepRunner) -> str | None:
    """Read ``origin/HEAD``, repairing it once when it points at a gone ref."""
    named = _symbolic_origin_head(root, runner)
    if named is None:
        return None
    if _has_ref(root, f"refs/remotes/origin/{named}", runner):
        return named

    runner(root, ("remote", "set-head", "origin", "--auto"))
    repaired = _symbolic_origin_head(root, runner)
    if repaired is None:
        return None
    if _has_ref(root, f"refs/remotes/origin/{repaired}", runner):
        return repaired
    return None


def _symbolic_origin_head(root: Path, runner: StepRunner) -> str | None:
    """Return ``origin/HEAD``'s target with the ``origin/`` prefix removed."""
    step = runner(root, ("symbolic-ref", "--short", "refs/remotes/origin/HEAD"))
    if not step.ok:
        return None
    named = step.out.strip().removeprefix("origin/")
    return named or None


def _has_ref(root: Path, ref: str, runner: StepRunner) -> bool:
    """Return True when ``ref`` exists in this repo."""
    return runner(root, ("show-ref", "--verify", "--quiet", ref)).ok


def _checkout(root: Path, branch: str, runner: StepRunner) -> Outcome | None:
    """Move to ``branch``, or return the failure. None means nothing went wrong."""
    current = runner(root, ("symbolic-ref", "--short", "HEAD"))
    if current.ok and current.out.strip() == branch:
        return None

    step = runner(root, ("checkout", branch))
    if not step.ok:
        return Outcome(
            ok=False,
            message=_reason(f"could not check out {branch}", step),
            branch=branch,
        )
    return None


def _pull(root: Path, branch: str, runner: StepRunner) -> Outcome:
    """Pull ``branch``, rebasing only when the tree is clean.

    A rebase needs a clean tree; a fast-forward does not, and only fails when
    the incoming commits touch a file that is modified locally. So a dirty tree
    takes the weaker pull and keeps its changes, rather than the pull refusing
    to run at all.
    """
    before = _head_sha(root, runner)
    clean = runner(root, ("diff", "--quiet", "HEAD")).ok
    args = ("pull", "--rebase") if clean else ("pull", "--ff-only")
    step = runner(root, args)

    if not step.ok:
        return Outcome(ok=False, message=_reason("pull failed", step), branch=branch)

    after = _head_sha(root, runner)
    return Outcome(
        ok=True,
        message=_summary(root, before, after, runner, clean=clean),
        branch=branch,
    )


def _summary(
    root: Path,
    before: str | None,
    after: str | None,
    runner: StepRunner,
    *,
    clean: bool,
) -> str:
    """Say how much arrived, counted rather than read off git's output.

    The last line git prints after a real rebase is a diffstat entry, which
    tells the user nothing about what they just pulled.
    """
    note = "" if clean else ", fast-forward only because the tree has changes"
    if before is None or after is None or before == after:
        return f"already up to date{note}"

    counted = runner(root, ("rev-list", "--count", f"{before}..{after}")).out.strip()
    if not counted.isdigit() or counted == "0":
        return f"updated{note}"
    plural = "" if counted == "1" else "s"
    return f"pulled {counted} commit{plural}{note}"


def _head_sha(root: Path, runner: StepRunner) -> str | None:
    """Return HEAD's sha, or None when there is no commit to name."""
    step = runner(root, ("rev-parse", "HEAD"))
    return step.out.strip() or None if step.ok else None


def _reason(prefix: str, step: Step) -> str:
    """Return ``prefix`` with git's own first line of complaint, when it gave one."""
    detail = _first_line(step)
    return f"{prefix}: {detail}" if detail else prefix


def _first_line(step: Step) -> str:
    """Return the first line git wrote, or the empty string."""
    lines = step.text.splitlines()
    return lines[0] if lines else ""
