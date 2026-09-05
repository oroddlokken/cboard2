# cboard2

A terminal dashboard over every git repo on your disk. It walks `~/git`, polls
each repo's state from git every two seconds, and puts whatever you touched last
at the top.

![The cboard2 dashboard](assets/cboard2.png)

## Requirements

Python 3.13 and git. A GitHub repo needs [`gh`](https://cli.github.com)
authenticated for its Remote and PR columns; without it those cells read `?` and
the rest still works.

## Install

```bash
brew install oroddlokken/tap/cboard2
cboard
```

The formula keeps the project's name; the command it installs is `cboard`.

From a checkout:

```bash
uv tool install .
cboard
```

## The dashboard

Each row is one repo:

| Column | Content |
|--------|---------|
| Repo, Branch | Name and current branch. The name takes a color per origin host and owner, so clones under one owner match |
| HEAD | Subject of the last commit |
| Last commit | Age of that commit |
| State | `S2 M1 ?3` is two staged files, one modified, three untracked. `U2` is two conflicted paths, `rebase` a rebase you stopped in the middle of, `stash 2` two entries on the stash. Red means something is halted and waiting on you |
| `↑↓` | Commits ahead of and behind the upstream |
| Remote, PR | Which branch the origin has moved on, and the pull requests in play. `behind origin/fix` is the checked-out branch, `behind main` the default one. `2 ✗  3 to review` is two PRs of yours with one failing its checks, and three waiting on your review. PRs come from GitHub; the branches come from any origin |
| Active | When you last touched the repo |

Active counts working-tree edits, not just commits, so an hour of uncommitted
work still sorts you to the top.

| Key | Does |
|-----|------|
| `enter` | On a fold row, show or hide that repo's remaining worktrees. Otherwise detail for the selected repo: remote state, your open PRs, the PRs awaiting your review, what is halted or stashed, changed files, branches, recent HEAD movements |
| `a` | Activity feed across all repos, read from their reflogs |
| `d` `u` `b` `p` | Filter to dirty / unpushed / behind / has-open-PR |
| `/` | Filter by repo name as you type; `escape` clears it. Composes with the toggles above |
| `s` | Sort by recent, name, or dirty |
| `t` | Window: all, 1h, 1d, 7d, 30d |
| `D` | Toggle the selected repo dormant, and write that to the config file |
| `P` | Check out the default branch and pull it |
| `r` `R` | Poll now; `R` also ignores the dormant interval |
| `q` | Quit |

`P` is the only key that writes to a repo. Everything else reads.

## Scripting

```bash
cboard ls                  # the table, as plain columns
cboard ls --remote         # add the remote columns, served from the cache
cboard json --since 2h     # the same rows as JSON
cboard busy --since 5m     # exit 0 if anything moved in the last 5 minutes
cboard busy --remote       # exit 0 if anything is behind or has a PR too
```

`busy` is meant for a tmux statusline or a shell guard. `--since` takes `30s`,
`5m`, `2h`, `1d`. `--remote` counts a repo behind its origin or holding an open
pull request as busy, alongside local activity, and reads the cache rather than
the network. `--refresh` asks the origins now instead, and implies `--remote`.

## Config

`~/.config/cboard2/config.toml`, or `$XDG_CONFIG_HOME/cboard2/config.toml`.
Every key has a default, so the file is optional.

```toml
roots = ["~/git", "~/work"]
max_depth = 2
dormant = ["~/git/old-thing"]
```

| Key | Default | Meaning |
|-----|---------|---------|
| `roots` | `["~/git"]` | Directories to search for repos |
| `max_depth` | `1` | Levels below a root to descend. Use `2` for `~/git/<org>/<repo>` |
| `dormant` | `[]` | Repos polled on the dormant interval instead of every tick |
| `dormant_interval` | `4h` | How often a dormant repo is polled |
| `remote` | `true` | Whether to ask any origin anything at all |
| `remote_interval` | `5m` | How often to ask |
| `origin_colors` | `true` | Whether a repo's name is colored by its origin's host and owner |
| `worktrees` | `true` | Whether a repo's linked worktrees get rows of their own |
| `worktree_limit` | `5` | Worktree rows painted per repo before the rest fold behind one row |

A dormant repo is still discovered, listed and readable. It is polled
rarely, so a shelf of archived clones costs nothing per second. `CBOARD2_CONFIG`
overrides the path.

A linked worktree gets a row of its own, indented as `  ⑂ worktree`, including
one kept inside its repo where the walk would never reach it. Every sort order
paints a repo and its worktrees as one block, placed where the most interesting
member of the group falls. The origin URL, the open PRs and the default branch
are read once for a repo and shown on every one of its worktrees, since they
share `refs`. A repo past `worktree_limit` paints its most recently active
worktrees and folds the rest behind a `  ⑂ 10 more worktrees` row; `enter` on
that row shows them all, and `enter` again folds them back.

## Where the numbers come from

Two git calls per repo feed the table. `status --porcelain=v2 --branch` gives the
branch, dirty counts and ahead/behind; `log -1` gives the HEAD subject and its age.
Both run across a thread pool: 94 repos in 0.28s on the author's machine, which
makes a 2-second poll affordable. A halted merge or rebase and the stash depth
cost no third call: `MERGE_HEAD`, `rebase-merge` and the stash reflog are files
in the git directory, read where they sit.
The `--no-optional-locks` flag keeps the poller from taking `.git/index.lock`
while you are running git yourself.

Recent activity comes from each repo's reflog, which already records every HEAD
movement with a timestamp and a verb. Working-tree edits touch nothing under
`.git`, so "last edited" is the newest mtime among the paths git reports dirty.

The remote columns come from two `gh search prs` calls — one for the PRs you
wrote, one for the PRs awaiting your review — and one batched GraphQL query for
default branches, checked-out branches and each PR's checks rollup. An origin that
is not on github.com gets `git ls-remote --symref origin HEAD` instead, with the
same branches named as extra refs, so a self-hosted or GitLab remote still
reports both; those repos show no PRs. cboard2 never fetches: rewriting
`refs/remotes/origin/*` under you would change what your next `git log` shows.

A checkout is compared against the branch its upstream names, or against the
same name on `origin` when it tracks nothing. A branch tracking another remote
is left unanswered, and the `↑↓` column keeps its own meaning: that one is read
from your remote-tracking refs and moves only when you fetch.

## Development

```bash
uv sync --group dev
just --list
```

`just lint-all` runs ruff and pyright. `just test` runs the suite; `just
test-changed` runs only what your edits touched.
