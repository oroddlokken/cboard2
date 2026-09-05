# CHANGELOG

## [Unreleased]

## 0.1.2 (2026-09-05)

### Development

- **A release stops before its tag when `HOMEBREW_TAP_TOKEN` cannot push to the tap.** v0.1.1's `update-homebrew` job made its formula commit and then took a 403 on the push, after the tag and the release were already public. The publish job now probes `info/refs?service=git-receive-pack` with the token, the endpoint git itself hits, and fails there naming the token and the fix. Reading `permissions.push` from the repository API would have passed: it reports the authenticated user's role, which is push for the owner however narrow the token.

## 0.1.1 (2026-09-05)

### Development

- **The published sdist is 59 KiB instead of 1.29 MiB.** `pyproject.toml` declared only `[tool.hatch.build.targets.wheel]`, so hatchling swept the working tree into the tarball. v0.1.0 shipped `assets/cboard2.png` at 1.2 MiB and the `.dogcats` issue store at 839 KiB, plus `CLAUDE.md`, `.github/`, `tests/` and `demo.py`. The new `[tool.hatch.build.targets.sdist]` section is an include-list, so the next unignored directory at the repo root stays out by default. `just check-sdist` fails a tarball over 1 MiB or carrying a top-level entry outside its allowlist. It runs on every pull request and in `publish.yml` ahead of the tag push, the first irreversible step.

## 0.1.0 (2026-09-05)

### Added

- **A repo's linked worktrees get rows of their own**, indented as `  ⑂ worktree`, including one kept inside its repo where the directory walk never reaches it. Every sort order paints a repo and its worktrees as one block, placed where the group's most interesting member falls. The origin URL, the open PRs and the default branch are read once per repo and shown on each of its worktrees, since they share `refs`.
- **A repo past `worktree_limit` (default 5) folds its remaining worktrees behind one `  ⑂ 10 more worktrees` row.** `enter` on that row shows them all and `enter` again folds them back, so a repo with thirty worktrees no longer owns the screen.
- **The Remote column names the checked-out branch when it is behind its own remote branch**, ahead of the default branch, because that is the branch the user is standing on. A branch tracking a remote other than its own name is left unanswered rather than compared against the wrong ref.
- **The PR column counts the pull requests waiting on the user's review** beside their own open ones: `2 ✗  3 to review`. The two are separate counts because one is theirs to land and the other is somebody else's to unblock.
- **The State column reports a halted merge or rebase, conflicted paths and the stash depth.** `U2` is two conflicted paths, `rebase` a rebase stopped in the middle, `stash 2` two entries. None of it costs a third git call: `MERGE_HEAD`, `rebase-merge` and the stash reflog are files in the git directory, read where they sit.
- **`/` filters repos by name as you type, and `escape` clears it.** It composes with the `d` `u` `b` `p` toggles rather than replacing them.

### Changed

- **A repo's name takes a color derived from its origin's host and owner**, so every clone under one owner matches at a glance. `origin_colors = false` turns it off.
- **A non-GitHub origin's default branch comes from `git ls-remote --symref origin HEAD`**, with the checked-out branch named as an extra ref, so a GitLab or self-hosted remote reports both branches' behind-ness. Those repos show no PRs, which needs `gh`.

### Development

- **`just release-prep` waits for the first PR check to register instead of dying on an empty rollup.** `gh pr checks --watch` ran seconds after `gh pr create`, and gh exits non-zero for an empty rollup exactly as for a failed check. The first real release run stopped there, after the branch, the RC tag and the PR had already landed. The poll counts the rollup with `gh pr view --json statusCheckRollup`, which the status summary already calls, and gives up after 150 seconds.
