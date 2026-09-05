# CHANGELOG

## [Unreleased]

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
