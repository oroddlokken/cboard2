"""Tests for the check-out-and-pull the dashboard's ``P`` key runs."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from cboard2.pull import (
    FETCH_TIMEOUT,
    PULL_TIMEOUT,
    Step,
    find_default_branch,
    pull_default,
    run_step,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

OK = Step(ok=True, out="", err="")
FAIL = Step(ok=False, out="", err="")

_ORIGIN_HEAD = ("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
_HEAD = ("symbolic-ref", "--short", "HEAD")
_CLEAN = ("diff", "--quiet", "HEAD")
_HEAD_SHA = ("rev-parse", "HEAD")

BEFORE_SHA = "b" * 40
AFTER_SHA = "a" * 40


class MovingHead:
    """A runner whose ``rev-parse HEAD`` answers differently after the pull.

    A real pull moves HEAD between the two reads, which is how the outcome
    counts what arrived.
    """

    def __init__(self, answers: Mapping[tuple[str, ...], Step], count: str) -> None:
        self.answers = answers
        self.count = count
        self.calls: list[tuple[str, ...]] = []
        self._pulled = False

    def __call__(self, _root: Path, args: Sequence[str]) -> Step:
        """Answer for these arguments, tracking whether the pull has run."""
        key = tuple(args)
        self.calls.append(key)
        if key[0] == "pull":
            self._pulled = True
            return self.answers.get(key, FAIL)
        if key == _HEAD_SHA:
            return _out(AFTER_SHA if self._pulled else BEFORE_SHA)
        if key[:2] == ("rev-list", "--count"):
            return _out(self.count)
        return self.answers.get(key, FAIL)

    def ran(self, *prefix: str) -> bool:
        """Return True when a call started with ``prefix``."""
        return any(args[: len(prefix)] == prefix for args in self.calls)


def _out(text: str) -> Step:
    return Step(ok=True, out=text, err="")


def _err(text: str) -> Step:
    return Step(ok=False, out="", err=text)


class FakeGit:
    """A step runner answering by the exact git arguments it is handed.

    Anything unlisted fails, which is what a missing ref and an unavailable
    command both look like to the caller.
    """

    def __init__(
        self,
        answers: Mapping[tuple[str, ...], Step],
        *,
        default: Step = FAIL,
    ) -> None:
        self.answers = answers
        self.default = default
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, root: Path, args: Sequence[str]) -> Step:
        """Record the call and answer for those exact arguments."""
        assert root is not None
        self.calls.append(tuple(args))
        return self.answers.get(tuple(args), self.default)

    def ran(self, *prefix: str) -> bool:
        """Return True when a call started with ``prefix``."""
        return any(args[: len(prefix)] == prefix for args in self.calls)


def _answers(**overrides: Step) -> dict[tuple[str, ...], Step]:
    """Build the calls a plain happy run makes, before any override."""
    base: dict[tuple[str, ...], Step] = {
        ("rev-parse", "--git-dir"): _out(".git"),
        ("fetch", "--prune"): OK,
        _ORIGIN_HEAD: _out("origin/main"),
        ("show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): OK,
        _HEAD: _out("feature/x"),
        ("checkout", "main"): OK,
        _CLEAN: OK,
        ("pull", "--rebase"): _out("Successfully rebased and updated refs/heads/main."),
        ("pull", "--ff-only"): _out("Fast-forward"),
        ("rev-parse", "HEAD"): _out(BEFORE_SHA),
    }
    for name, step in overrides.items():
        base[tuple(name.split("__"))] = step
    return base


def test_a_clean_repo_on_a_feature_branch_is_moved_and_rebased(tmp_path: Path) -> None:
    git = FakeGit(_answers())

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is True
    assert outcome.branch == "main"
    assert outcome.message == "already up to date"
    assert git.ran("fetch", "--prune")
    assert git.ran("checkout", "main")
    assert git.ran("pull", "--rebase")


def test_a_repo_already_on_the_default_branch_is_not_checked_out_again(
    tmp_path: Path,
) -> None:
    git = FakeGit({**_answers(), _HEAD: _out("main")})

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is True
    assert git.ran("checkout") is False
    assert git.ran("pull", "--rebase")


def test_a_dirty_tree_pulls_fast_forward_only_and_says_so(tmp_path: Path) -> None:
    git = FakeGit({**_answers(), _CLEAN: FAIL})

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is True
    assert git.ran("pull", "--ff-only")
    assert git.ran("pull", "--rebase") is False
    assert outcome.message == (
        "already up to date, fast-forward only because the tree has changes"
    )


def test_a_failed_fetch_aborts_before_any_checkout(tmp_path: Path) -> None:
    git = FakeGit(
        {**_answers(), ("fetch", "--prune"): _err("fatal: could not read from remote")},
    )

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is False
    assert outcome.message == "fetch failed: fatal: could not read from remote"
    assert git.ran("checkout") is False
    assert git.ran("pull") is False


def test_a_directory_that_is_not_a_repo_is_reported(tmp_path: Path) -> None:
    git = FakeGit({})

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is False
    assert outcome.message == "not a git repository"
    assert git.ran("fetch") is False


def test_a_stale_origin_head_is_repaired_once(tmp_path: Path) -> None:
    answers = _answers()
    del answers[("show-ref", "--verify", "--quiet", "refs/remotes/origin/main")]
    answers[_ORIGIN_HEAD] = _out("origin/trunk")
    answers[("show-ref", "--verify", "--quiet", "refs/remotes/origin/trunk")] = FAIL
    git = FakeGit(answers)

    # After the repair, origin/HEAD names a branch that does exist.
    repaired = {
        **answers,
        _ORIGIN_HEAD: _out("origin/develop"),
        ("show-ref", "--verify", "--quiet", "refs/remotes/origin/develop"): OK,
        ("checkout", "develop"): OK,
    }

    def runner(_root: Path, args: Sequence[str]) -> Step:
        git.calls.append(tuple(args))
        if git.ran("remote", "set-head"):
            return repaired.get(tuple(args), FAIL)
        return answers.get(tuple(args), FAIL)

    outcome = pull_default(tmp_path, runner=runner)

    assert outcome.branch == "develop"
    assert outcome.ok is True
    assert git.ran("remote", "set-head", "origin", "--auto")


@pytest.mark.parametrize("candidate", ["main", "master"])
def test_a_repo_without_origin_head_falls_back_to_the_candidates(
    tmp_path: Path,
    candidate: str,
) -> None:
    git = FakeGit(
        {
            ("rev-parse", "--git-dir"): _out(".git"),
            ("fetch", "--prune"): OK,
            ("show-ref", "--verify", "--quiet", f"refs/heads/{candidate}"): OK,
            _HEAD: _out("feature/x"),
            ("checkout", candidate): OK,
            _CLEAN: OK,
            ("pull", "--rebase"): _out("Already up to date."),
        },
    )

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.branch == candidate
    assert outcome.ok is True


def test_a_candidate_known_only_on_the_remote_still_counts(tmp_path: Path) -> None:
    git = FakeGit(
        {
            ("rev-parse", "--git-dir"): _out(".git"),
            ("fetch", "--prune"): OK,
            ("show-ref", "--verify", "--quiet", "refs/remotes/origin/main"): OK,
            _HEAD: _out("feature/x"),
            ("checkout", "main"): OK,
            _CLEAN: OK,
            ("pull", "--rebase"): _out("Already up to date."),
        },
    )

    assert pull_default(tmp_path, runner=git).branch == "main"


def test_a_repo_with_no_nameable_default_branch_is_reported(tmp_path: Path) -> None:
    git = FakeGit(
        {("rev-parse", "--git-dir"): _out(".git"), ("fetch", "--prune"): OK},
    )

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is False
    assert outcome.message == (
        "no default branch found: no origin/HEAD, main or master"
    )
    assert git.ran("checkout") is False


def test_the_github_branch_name_skips_the_lookup(tmp_path: Path) -> None:
    git = FakeGit(
        {
            ("rev-parse", "--git-dir"): _out(".git"),
            ("fetch", "--prune"): OK,
            _HEAD: _out("feature/x"),
            ("checkout", "trunk"): OK,
            _CLEAN: OK,
            ("pull", "--rebase"): _out("Already up to date."),
        },
    )

    outcome = pull_default(tmp_path, default_branch="trunk", runner=git)

    assert outcome.branch == "trunk"
    assert outcome.ok is True
    assert git.ran("symbolic-ref", "--short", "refs/remotes/origin/HEAD") is False


def test_a_failed_checkout_stops_before_the_pull(tmp_path: Path) -> None:
    git = FakeGit(
        {
            **_answers(),
            ("checkout", "main"): _err(
                "error: Your local changes would be overwritten",
            ),
        },
    )

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is False
    assert outcome.branch == "main"
    assert outcome.message == (
        "could not check out main: error: Your local changes would be overwritten"
    )
    assert git.ran("pull") is False


def test_a_failed_pull_carries_gits_reason(tmp_path: Path) -> None:
    git = FakeGit(
        {**_answers(), ("pull", "--rebase"): _err("fatal: refusing to merge")},
    )

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is False
    assert outcome.message == "pull failed: fatal: refusing to merge"


@pytest.mark.parametrize(
    ("count", "expected"),
    [("1", "pulled 1 commit"), ("12", "pulled 12 commits")],
)
def test_a_pull_that_moves_head_counts_what_arrived(
    tmp_path: Path,
    count: str,
    expected: str,
) -> None:
    """The last line git prints after a rebase is a diffstat entry, not a verdict."""
    git = MovingHead(_answers(), count)

    outcome = pull_default(tmp_path, runner=git)

    assert outcome.ok is True
    assert outcome.message == expected


def test_a_moved_head_with_an_uncountable_range_still_reports_success(
    tmp_path: Path,
) -> None:
    git = MovingHead(_answers(), "not a number")

    assert pull_default(tmp_path, runner=git).message == "updated"


def test_a_repo_with_no_head_reports_up_to_date(tmp_path: Path) -> None:
    answers = _answers()
    del answers[_HEAD_SHA]
    git = FakeGit(answers)

    assert pull_default(tmp_path, runner=git).message == "already up to date"


def test_find_default_branch_prefers_origin_head_over_the_candidates(
    tmp_path: Path,
) -> None:
    git = FakeGit(
        {
            _ORIGIN_HEAD: _out("origin/develop"),
            ("show-ref", "--verify", "--quiet", "refs/remotes/origin/develop"): OK,
            ("show-ref", "--verify", "--quiet", "refs/heads/main"): OK,
        },
    )

    assert find_default_branch(tmp_path, git) == "develop"


def test_an_empty_origin_head_is_not_taken_as_a_branch(tmp_path: Path) -> None:
    git = FakeGit(
        {
            _ORIGIN_HEAD: _out("origin/"),
            ("show-ref", "--verify", "--quiet", "refs/heads/master"): OK,
        },
    )

    assert find_default_branch(tmp_path, git) == "master"


def test_a_real_repo_with_no_remote_fails_at_the_pull(git_repo: Path) -> None:
    """A fetch in a repo with no remote exits zero, so the pull is what fails."""
    outcome = pull_default(git_repo, default_branch="main")

    assert outcome.ok is False
    assert outcome.branch == "main"
    assert outcome.message.startswith("pull failed")


def test_run_step_reports_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args
        assert kwargs["check"] is False
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    step = run_step(Path(), ("fetch", "--prune"))

    assert step.ok is False
    assert step.err == "git fetch timed out"


def test_run_step_reports_a_git_that_will_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args
        assert kwargs
        msg = "no git here"
        raise OSError(msg)

    monkeypatch.setattr(subprocess, "run", fake_run)
    step = run_step(Path(), ("fetch", "--prune"))

    assert step.ok is False
    assert "could not run" in step.err


def test_run_step_disables_the_credential_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def fake_run(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args
        seen.update(cast("dict[str, str]", kwargs["env"]))
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_step(Path(), ("fetch", "--prune"))

    assert seen["GIT_TERMINAL_PROMPT"] == "0"


def test_each_step_is_named_as_it_starts(tmp_path: Path) -> None:
    git = FakeGit(_answers())
    steps: list[str] = []

    pull_default(tmp_path, runner=git, on_step=steps.append)

    assert steps == ["fetching", "checking out main", "pulling main"]


def test_a_repo_already_on_the_default_branch_names_no_checkout(
    tmp_path: Path,
) -> None:
    git = FakeGit({**_answers(), _HEAD: _out("main")})
    steps: list[str] = []

    pull_default(tmp_path, runner=git, on_step=steps.append)

    assert steps == ["fetching", "pulling main"]


def test_the_fetch_gets_a_shorter_timeout_than_the_steps_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    def fake_run(
        *args: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args
        seen.append(cast("float", kwargs["timeout"]))
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_step(Path(), ("fetch", "--prune"))
    run_step(Path(), ("checkout", "main"))

    assert FETCH_TIMEOUT < PULL_TIMEOUT
    assert seen == [FETCH_TIMEOUT, PULL_TIMEOUT]
