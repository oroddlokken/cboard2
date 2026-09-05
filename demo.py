"""Open the dashboard on a fixed set of invented repos, for screenshots.

    uv run python demo.py

Nothing here reads git or GitHub: the 61 rows are built in this file, so a
screenshot shows the same repos on any machine and no private repo name reaches
an image. Three module attributes in :mod:`cboard2.tui` would still reach the
disk, and are replaced below for the run.

Every key works — the filters, the sort and window cycles, the detail modal on
enter and the activity feed on ``a``. ``D`` writes to a throwaway config file
and ``P`` reports a pull that never ran.
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

from cboard2 import tui
from cboard2.activity import Branch, Entry
from cboard2.board import DEFAULT_FEED_LIMIT, Board, Row
from cboard2.config import Config
from cboard2.gitstate import RepoState
from cboard2.pull import Outcome
from cboard2.remote import UNKNOWN, PullRequest, RemoteState
from cboard2.tui import CboardApp

MINUTE = 60.0
HOUR = 3600.0
DAY = 86400.0

DEMO_ROOT = Path.home() / "git"
"""Where the invented repos claim to live. No path under it is opened."""

_OWNER = "acme"

_FILENAMES = (
    "handlers.py",
    "models.py",
    "routes.ts",
    "settings.yaml",
    "README.md",
    "client.go",
    "queries.sql",
)
"""Cycled to give a repo with dirty counts a plausible file list in the modal."""


def _sha(seed: str) -> str:
    """Return a stable seven-character hex string for ``seed``."""
    return f"{abs(hash(seed)):08x}"[:7]


def _pr(
    number: int,
    title: str,
    slug: str,
    *,
    ago: float,
    now: float,
    draft: bool = False,
    checks: str = "passing",
) -> PullRequest:
    """Build one open pull request on ``slug``."""
    return PullRequest(
        number=number,
        title=title,
        url=f"https://github.com/{slug}/pull/{number}",
        draft=draft,
        updated_at=now - ago,
        checks=checks,
    )


def _known(
    name: str,
    *,
    behind: bool = False,
    behind_branch: str | None = None,
    prs: tuple[PullRequest, ...] = (),
    review_prs: tuple[PullRequest, ...] = (),
) -> RemoteState:
    """Return a remote reading where both GitHub calls answered for ``name``.

    ``behind_branch`` names a checked-out branch whose copy on the origin has
    moved on, which the Remote column reports ahead of the default branch.
    """
    return RemoteState(
        origin=f"https://github.com/{_OWNER}/{name}.git",
        slug=f"{_OWNER}/{name}",
        default_branch="main",
        default_sha=_sha(name),
        default_known=True,
        prs=prs,
        prs_known=True,
        review_prs=review_prs,
        review_prs_known=True,
        behind_default=behind,
        branch=behind_branch,
        branch_remote=behind_branch,
        branch_sha=None if behind_branch is None else _sha(behind_branch),
        branch_known=behind_branch is not None,
        behind_branch=behind_branch is not None,
    )


def _offsite(
    name: str,
    origin: str,
    *,
    branch: str = "master",
    behind: bool = False,
) -> RemoteState:
    """Return a reading for an origin ls-remote answered and no GitHub call could.

    The PRs read known and empty, which is what a repo with no GitHub side gets:
    there is no search that could have missed one.
    """
    return RemoteState(
        origin=origin,
        default_branch=branch,
        default_sha=_sha(name),
        default_known=True,
        prs_known=True,
        review_prs_known=True,
        behind_default=behind,
    )


def _paths(name: str, count: int) -> tuple[str, ...]:
    """Return ``count`` invented dirty paths inside ``name``."""
    stem = name.replace("-", "_")
    return tuple(
        f"src/{stem}/{_FILENAMES[index % len(_FILENAMES)]}" for index in range(count)
    )


def _row(
    name: str,
    *,
    now: float,
    ago: float,
    branch: str | None = "main",
    detached: bool = False,
    readable: bool = True,
    subject: str | None = None,
    staged: int = 0,
    unstaged: int = 0,
    untracked: int = 0,
    unmerged: int = 0,
    operation: str = "none",
    stashed: int = 0,
    ahead: int = 0,
    behind: int = 0,
    dirty_paths: tuple[str, ...] = (),
    dormant: bool = False,
    polled_ago: float = 1.0,
    remote: RemoteState = UNKNOWN,
    main_git_dir: Path | None = None,
) -> Row:
    """Build one row as if the pollers had read this repo ``ago`` seconds back."""
    dirty = staged + unstaged + untracked + unmerged
    state = RepoState(
        path=DEMO_ROOT / name,
        name=name,
        dormant=dormant,
        readable=readable,
        polled_at=now - polled_ago,
        branch=branch,
        detached=detached,
        head_sha=_sha(name),
        head_subject=subject,
        head_time=int(now - ago),
        staged=staged,
        unstaged=unstaged,
        untracked=untracked,
        unmerged=unmerged,
        operation=operation,
        stashed=stashed,
        ahead=ahead,
        behind=behind,
        upstream=None if branch is None else f"origin/{branch}",
        dirty_paths=dirty_paths or _paths(name, dirty),
        last_edit=now - ago,
        main_git_dir=main_git_dir,
    )
    return Row(state=state, moved_at=now - ago, remote=remote)


def _featured(now: float) -> list[Row]:
    """Return the repos worth putting at the top of a screenshot, one per state."""
    orbit = _known(
        "orbit-web",
        prs=(
            _pr(
                412,
                "Pricing table: three tiers and an annual toggle",
                f"{_OWNER}/orbit-web",
                ago=18 * MINUTE,
                now=now,
                checks="failing",
            ),
        ),
        review_prs=(
            _pr(
                409,
                "Drop the legacy checkout banner",
                f"{_OWNER}/orbit-web",
                ago=50 * MINUTE,
                now=now,
            ),
            _pr(
                405,
                "Bump the design tokens to 3.1",
                f"{_OWNER}/orbit-web",
                ago=6 * HOUR,
                now=now,
            ),
        ),
    )
    api = _known(
        "acme-api",
        prs=(
            _pr(
                1180,
                "Key rotation without a restart",
                f"{_OWNER}/acme-api",
                ago=3 * HOUR,
                now=now,
            ),
            _pr(
                1174,
                "WIP: move the rate limiter to Redis",
                f"{_OWNER}/acme-api",
                ago=2 * DAY,
                now=now,
                draft=True,
                checks="pending",
            ),
        ),
        review_prs=(
            _pr(
                1169,
                "Add a metrics endpoint for the signer",
                f"{_OWNER}/acme-api",
                ago=4 * HOUR,
                now=now,
            ),
        ),
    )
    return [
        _row(
            "orbit-web",
            now=now,
            ago=4 * MINUTE,
            branch="feat/pricing-table",
            subject="Split the pricing table out of the marketing page",
            staged=2,
            unstaged=3,
            untracked=1,
            ahead=2,
            dirty_paths=(
                "src/pricing/table.tsx",
                "src/pricing/table.test.tsx",
                "src/pricing/tiers.ts",
                "src/app/layout.tsx",
                "docs/pricing.md",
                "src/pricing/.snapshot.tmp",
            ),
            remote=orbit,
        ),
        _row(
            "hotfix-checkout",
            now=now,
            ago=12 * MINUTE,
            branch="hotfix/checkout-500",
            subject="Guard the checkout callback against an empty cart",
            unstaged=1,
            ahead=1,
            dirty_paths=("src/checkout/callback.ts",),
            main_git_dir=DEMO_ROOT / "orbit-web" / ".git",
            remote=orbit,
        ),
        _row(
            "ledger-sync",
            now=now,
            ago=25 * MINUTE,
            branch="chore/rebase-onto-main",
            subject="Retry the settlement fetch on a 502",
            operation="rebase",
            unmerged=2,
            unstaged=1,
            dirty_paths=(
                "src/ledger/settlement.py",
                "src/ledger/retry.py",
                "tests/test_settlement.py",
            ),
            remote=_known("ledger-sync", behind=True),
        ),
        _row(
            "acme-api",
            now=now,
            ago=2 * HOUR,
            subject="Reject an expired signing key at the edge",
            unstaged=1,
            ahead=1,
            behind=3,
            dirty_paths=("api/auth/keys.py",),
            remote=api,
        ),
        _row(
            "key-rotation",
            now=now,
            ago=35 * MINUTE,
            branch="feat/key-rotation",
            subject="Rotate signing keys without dropping in-flight requests",
            staged=1,
            unstaged=2,
            stashed=2,
            ahead=4,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "redis-limiter",
            now=now,
            ago=2 * DAY,
            branch="spike/redis-limiter",
            subject="Move the rate limiter behind a Redis token bucket",
            untracked=3,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "perf-triage",
            now=now,
            ago=5 * HOUR,
            branch="release/4.2",
            subject="Profile the signing hot path under load",
            behind=6,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "signing-audit",
            now=now,
            ago=20 * MINUTE,
            branch="fix/signing-audit-log",
            subject="Log every signing-key rotation to the audit trail",
            unstaged=2,
            ahead=1,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "edge-timeouts",
            now=now,
            ago=3 * HOUR,
            branch="fix/edge-timeouts",
            subject="Cut the edge read timeout to two seconds",
            ahead=2,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "pr-4821",
            now=now,
            ago=8 * HOUR,
            branch="review/pr-4821",
            subject="Merge remote-tracking branch 'origin/main' into review",
            behind=4,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "token-bucket-bench",
            now=now,
            ago=18 * HOUR,
            branch="spike/token-bucket-bench",
            subject="Benchmark the token bucket against the leaky bucket",
            untracked=2,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "webhook-replay",
            now=now,
            ago=DAY,
            branch="feat/webhook-replay",
            subject="Replay a failed webhook from the delivery log",
            staged=1,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "grpc-gateway",
            now=now,
            ago=3 * DAY,
            branch="feat/grpc-gateway",
            subject="Serve the v2 endpoints through the gRPC gateway",
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "audit-export",
            now=now,
            ago=4 * DAY,
            branch="feat/audit-export",
            subject="Export the audit trail as newline-delimited JSON",
            ahead=3,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "bisect-latency",
            now=now,
            ago=6 * DAY,
            branch=None,
            detached=True,
            subject="Bisecting the p99 latency regression",
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "renovate-fastapi",
            now=now,
            ago=9 * DAY,
            branch="renovate/fastapi-0.115",
            subject="Bump fastapi from 0.111 to 0.115",
            behind=12,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "docs-runbook",
            now=now,
            ago=12 * DAY,
            branch="docs/auth-runbook",
            subject="Write the on-call runbook for a leaked signing key",
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "hsm-prototype",
            now=now,
            ago=20 * DAY,
            branch="spike/hsm-signing",
            subject="Sign with a key that never leaves the HSM",
            untracked=5,
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "release-4-1",
            now=now,
            ago=30 * DAY,
            branch="release/4.1",
            subject="Tag 4.1.3 and close the branch",
            main_git_dir=DEMO_ROOT / "acme-api" / ".git",
            remote=api,
        ),
        _row(
            "pipeline-tools",
            now=now,
            ago=6 * HOUR,
            branch=None,
            detached=True,
            subject="Bisecting the parquet writer regression",
            remote=_known("pipeline-tools"),
        ),
        _row(
            "ops-runbooks",
            now=now,
            ago=50 * MINUTE,
            branch="master",
            subject="Write down the failover drill",
            unstaged=2,
            remote=_offsite(
                "ops-runbooks",
                "git@git.acme.internal:ops/runbooks.git",
                behind=True,
            ),
        ),
        _row(
            "chart-lab",
            now=now,
            ago=3 * DAY,
            branch="spike/vega",
            subject="Try a layered vega spec for the funnel",
            untracked=7,
        ),
        _row(
            "k8s-manifests",
            now=now,
            ago=9 * DAY,
            subject="Pin the ingress controller to 1.11.2",
            dormant=True,
            polled_ago=3 * HOUR,
            remote=_known("k8s-manifests"),
        ),
        _row(
            "old-scraper",
            now=now,
            ago=40 * DAY,
            branch=None,
            readable=False,
        ),
    ]


def _rest(now: float) -> list[Row]:
    """Return the repos that fill the table past one screen."""
    payments = _known(
        "payments-core",
        prs=(
            _pr(
                88,
                "Idempotency keys on every refund path",
                f"{_OWNER}/payments-core",
                ago=40 * MINUTE,
                now=now,
            ),
        ),
    )
    return [
        _row(
            "payments-core",
            now=now,
            ago=12 * MINUTE,
            branch="fix/idempotency",
            subject="Make the refund handler idempotent",
            unstaged=2,
            ahead=1,
            remote=payments,
        ),
        _row(
            "refund-backfill",
            now=now,
            ago=4 * HOUR,
            branch="chore/refund-backfill",
            subject="Replay the September refunds through the new handler",
            unstaged=1,
            untracked=2,
            main_git_dir=DEMO_ROOT / "payments-core" / ".git",
            remote=payments,
        ),
        _row(
            "auth-gateway",
            now=now,
            ago=48 * MINUTE,
            subject="Log the token audience on a rejected request",
            remote=_known("auth-gateway", behind=True),
        ),
        _row(
            "invoice-service",
            now=now,
            ago=55 * MINUTE,
            branch="fix/rounding",
            subject="Round the tax line the way the ledger does",
            unstaged=1,
            ahead=1,
            remote=_known("invoice-service", behind_branch="fix/rounding"),
        ),
        _row(
            "design-tokens",
            now=now,
            ago=70 * MINUTE,
            branch="chore/contrast",
            subject="Raise the contrast on the muted text token",
            staged=1,
            untracked=2,
            remote=_known("design-tokens"),
        ),
        _row(
            "notify-worker",
            now=now,
            ago=90 * MINUTE,
            subject="Drain the queue before shutting down",
            ahead=4,
            remote=_known("notify-worker"),
        ),
        _row(
            "search-index",
            now=now,
            ago=3 * HOUR,
            branch="perf/bulk-writes",
            subject="Batch the bulk writes at 500 documents",
            unstaged=5,
            remote=_known(
                "search-index",
                prs=(
                    _pr(
                        231,
                        "Batch bulk writes",
                        f"{_OWNER}/search-index",
                        ago=5 * HOUR,
                        now=now,
                        draft=True,
                    ),
                ),
            ),
        ),
        _row(
            "billing-ui",
            now=now,
            ago=5 * HOUR,
            subject="Show the proration line on the invoice preview",
            behind=2,
            remote=_known("billing-ui", behind=True),
        ),
        _row(
            "infra-terraform",
            now=now,
            ago=8 * HOUR,
            branch="feat/eu-west-1",
            subject="Add the eu-west-1 replica bucket",
            staged=3,
            remote=_known("infra-terraform"),
        ),
        _row(
            "docs-site",
            now=now,
            ago=DAY,
            subject="Drop the beta banner",
            remote=_known("docs-site"),
        ),
        _row(
            "sdk-python",
            now=now,
            ago=DAY + 4 * HOUR,
            subject="Type the pagination iterator",
            remote=_known(
                "sdk-python",
                prs=(
                    _pr(
                        57,
                        "Typed pagination",
                        f"{_OWNER}/sdk-python",
                        ago=DAY,
                        now=now,
                    ),
                ),
            ),
        ),
        _row(
            "sdk-typescript",
            now=now,
            ago=2 * DAY,
            branch="release/3.2",
            subject="Cut 3.2.0",
            remote=_known("sdk-typescript"),
        ),
        _row(
            "event-schemas",
            now=now,
            ago=2 * DAY + 6 * HOUR,
            subject="Add the subscription.paused event",
            untracked=1,
            remote=_known("event-schemas"),
        ),
        _row(
            "webhook-relay",
            now=now,
            ago=4 * DAY,
            subject="Retry with a jittered backoff",
            remote=_known("webhook-relay", behind=True),
        ),
        _row(
            "status-page",
            now=now,
            ago=6 * DAY,
            subject="Move the incident feed to the edge cache",
            remote=_known("status-page"),
        ),
        _row(
            "firmware-bridge",
            now=now,
            ago=6 * HOUR,
            branch="master",
            subject="Retry the flash after a USB reset",
            untracked=1,
            remote=_offsite(
                "firmware-bridge",
                "https://gitlab.com/acme/firmware-bridge.git",
            ),
        ),
        _row(
            "backup-scripts",
            now=now,
            ago=11 * DAY,
            branch="master",
            subject="Rotate the weekly snapshots",
            dormant=True,
            polled_ago=3 * HOUR,
            remote=_offsite(
                "backup-scripts",
                "vault:/srv/git/backup-scripts.git",
                behind=True,
            ),
        ),
        _row(
            "data-warehouse",
            now=now,
            ago=8 * DAY,
            branch="model/retention",
            subject="Add the weekly retention model",
            unstaged=1,
            untracked=3,
        ),
        _row(
            "ml-playground",
            now=now,
            ago=12 * DAY,
            branch="spike/embeddings",
            subject="Compare two embedding models on the support corpus",
            untracked=12,
        ),
        _row(
            "legacy-billing",
            now=now,
            ago=20 * DAY,
            subject="Freeze before the migration",
            dormant=True,
            polled_ago=2 * HOUR,
            remote=_known("legacy-billing"),
        ),
        _row(
            "conference-talks",
            now=now,
            ago=45 * DAY,
            subject="Slides for the observability talk",
            dormant=True,
            polled_ago=4 * HOUR,
        ),
        _row(
            "feature-flags",
            now=now,
            ago=20 * MINUTE,
            branch="fix/stale-cache",
            subject="Expire the flag cache on a webhook",
            unstaged=1,
            ahead=2,
            remote=_known("feature-flags"),
        ),
        _row(
            "rate-limiter",
            now=now,
            ago=55 * MINUTE,
            subject="Move the counters to a sliding window",
            staged=2,
            unstaged=1,
            remote=_known("rate-limiter", behind=True),
        ),
        _row(
            "mobile-app",
            now=now,
            ago=2 * HOUR + 20 * MINUTE,
            branch="feat/offline-mode",
            subject="Queue writes while the device is offline",
            untracked=4,
            ahead=3,
            remote=_known(
                "mobile-app",
                prs=(
                    _pr(
                        604,
                        "Offline write queue",
                        f"{_OWNER}/mobile-app",
                        ago=2 * HOUR,
                        now=now,
                    ),
                    _pr(
                        598,
                        "Bump the minimum iOS version to 16",
                        f"{_OWNER}/mobile-app",
                        ago=3 * DAY,
                        now=now,
                        draft=True,
                    ),
                ),
            ),
        ),
        _row(
            "image-proxy",
            now=now,
            ago=4 * HOUR,
            subject="Serve avif when the client accepts it",
            remote=_known("image-proxy"),
        ),
        _row(
            "cron-runner",
            now=now,
            ago=7 * HOUR,
            branch=None,
            detached=True,
            subject="Reproducing the missed midnight run",
            remote=_known("cron-runner"),
        ),
        _row(
            "telemetry-agent",
            now=now,
            ago=10 * HOUR,
            subject="Drop the hostname label from the metrics",
            behind=1,
            remote=_known("telemetry-agent", behind=True),
        ),
        _row(
            "support-bot",
            now=now,
            ago=14 * HOUR,
            branch="feat/handoff",
            subject="Hand the thread to a human after two failures",
            unstaged=3,
            remote=_known(
                "support-bot",
                prs=(
                    _pr(
                        41,
                        "Human handoff after two failed answers",
                        f"{_OWNER}/support-bot",
                        ago=12 * HOUR,
                        now=now,
                    ),
                ),
            ),
        ),
        _row(
            "invoice-pdf",
            now=now,
            ago=20 * HOUR,
            subject="Render the VAT breakdown per line",
            remote=_known("invoice-pdf"),
        ),
        _row(
            "partner-portal",
            now=now,
            ago=3 * DAY + 4 * HOUR,
            branch="chore/deps",
            subject="Bump the framework to 15.4",
            staged=1,
            untracked=1,
            remote=_known("partner-portal"),
        ),
        _row(
            "geo-lookup",
            now=now,
            ago=5 * DAY,
            subject="Refresh the city database",
            remote=_known("geo-lookup"),
        ),
        _row(
            "session-store",
            now=now,
            ago=7 * DAY,
            subject="Halve the idle timeout",
            ahead=1,
            remote=_known("session-store", behind=True),
        ),
        _row(
            "bench-suite",
            now=now,
            ago=10 * DAY,
            branch="spike/criterion",
            subject="Port the read benchmarks to criterion",
            untracked=2,
        ),
        _row(
            "migration-scripts",
            now=now,
            ago=15 * DAY,
            subject="Backfill the customer region column",
            remote=_known("migration-scripts"),
        ),
        _row(
            "marketing-site",
            now=now,
            ago=25 * DAY,
            subject="Swap the hero screenshot",
            dormant=True,
            polled_ago=90 * MINUTE,
            remote=_known("marketing-site"),
        ),
        _row(
            "api-docs-openapi",
            now=now,
            ago=30 * DAY,
            subject="Regenerate from the 2.9 spec",
            dormant=True,
            polled_ago=2 * HOUR,
        ),
        _row(
            "dotfiles",
            now=now,
            ago=60 * DAY,
            subject="Switch the prompt to starship",
            ahead=1,
            remote=_known("dotfiles"),
        ),
    ]


def demo_rows(now: float) -> list[Row]:
    """Return all 61 invented repos, newest activity first."""
    rows = _featured(now) + _rest(now)
    return sorted(rows, key=lambda row: -row.active_at)


_VERBS = (
    ("commit", "Split the pricing table out of the marketing page", 4 * MINUTE),
    ("checkout", "main to feat/pricing-table", 52 * MINUTE),
    ("commit", "Retry the settlement fetch on a 502", 25 * MINUTE),
    ("pull", "fast-forward to a41f9c2", 3 * HOUR),
    ("rebase (finish)", "onto main", 5 * HOUR),
    ("reset", "moving to HEAD~1", 6 * HOUR),
    ("merge", "release/2.4 into main", DAY),
    ("commit (amend)", "Batch the bulk writes at 500 documents", DAY + 2 * HOUR),
    ("checkout", "main to spike/embeddings", 2 * DAY),
    ("clone", "from github.com/acme/chart-lab", 3 * DAY),
    ("branch", "created release/3.2", 4 * DAY),
    ("cherry-pick", "the proration fix onto main", 6 * DAY),
)


def demo_entries(now: float, rows: list[Row]) -> list[Entry]:
    """Return an activity feed spread across the invented repos, newest first."""
    feed = [
        Entry(
            at=now - ago,
            repo_path=rows[index % len(rows)].state.path,
            repo_name=rows[index % len(rows)].state.name,
            verb=verb,
            detail=detail,
            sha=_sha(detail),
        )
        for index, (verb, detail, ago) in enumerate(_VERBS)
    ]
    return sorted(feed, key=lambda entry: -entry.at)


_BRANCHES = (
    ("feat/pricing-table", 4 * MINUTE, "Split the pricing table out"),
    ("main", 2 * HOUR, "Bump the design tokens package"),
    ("fix/stale-session", 6 * DAY, "Clear the session cookie on logout"),
)


class DemoBoard(Board):
    """A board that answers from the invented rows instead of from disk."""

    def __init__(self, rows: list[Row], entries: list[Entry], *, read_at: float):
        super().__init__(_demo_config())
        self._rows = rows
        self._entries = entries
        self._read_at = read_at

    def refresh(
        self,
        *,
        force: bool = False,
        rescan: bool = False,
        now: float | None = None,
    ) -> list[Row]:
        """Return the invented rows, whatever the caller asked to re-read."""
        return list(self._rows)

    def read_remote(self, *, force: bool = False, now: float | None = None) -> bool:
        """Report that the remote reading has not moved, so no repoll follows."""
        return False

    @property
    def remote_read_at(self) -> float | None:
        """When the invented remote read happened, for the header subtitle."""
        return self._read_at

    def activity(
        self,
        *,
        since: float | None = None,
        limit: int = DEFAULT_FEED_LIMIT,
    ) -> list[Entry]:
        """Return the invented feed, newest first."""
        return self._entries[:limit]


def _demo_config() -> Config:
    """Config with no roots, so the base board's readers find nothing to poll."""
    return Config(
        roots=(),
        max_depth=1,
        dormant=(),
        dormant_interval=4 * HOUR,
        remote=False,
        remote_interval=300.0,
        origin_colors=True,
        worktrees=True,
        worktree_limit=5,
    )


class _Present(Path):
    """A path that reports itself as existing.

    ``row_cells`` strikes out a repo whose directory is gone, which every
    invented repo here would be.
    """

    def exists(self, *, follow_symlinks: bool = True) -> bool:
        """Say yes without touching the filesystem."""
        return True


def _demo_branches(root: Path, *_args: object, **_kwargs: object) -> list[Branch]:
    """Return canned branches for the detail modal, which otherwise runs git."""
    now = time.time()
    return [
        Branch(name=name, committed_at=now - ago, subject=subject)
        for name, ago, subject in _BRANCHES
    ]


def _demo_pull(root: Path, **_kwargs: object) -> Outcome:
    """Report a pull that never ran, so ``P`` is safe on an invented repo."""
    return Outcome(ok=True, message="already up to date", branch="main")


def main() -> None:
    """Patch the three disk-reading attributes, then run the dashboard."""
    tui.Path = _Present
    tui.branches = _demo_branches
    tui.pull_default = _demo_pull

    now = time.time()
    rows = demo_rows(now)
    board = DemoBoard(rows, demo_entries(now, rows), read_at=now - 45)
    scratch = Path(tempfile.mkdtemp(prefix="cboard2-demo-")) / "config.toml"
    CboardApp(board, config_file=scratch).run()


if __name__ == "__main__":
    main()
