---
skill: should-i-abstract
mode: Full
target: root
rev: fadb3ec
dirty: yes
languages: Python, Shell, YAML
---

## DRY Review

### Abstract This -- True Knowledge Duplication

1. [ ] **cli.py and tui.py duplicate nine row-rendering functions** [High] -- the CLI table and the Textual dashboard each reimplement the same "what does this cell say" logic, wrapped in a plain `str` vs. a styled `Text`.
   `src/cboard2/cli.py:280-296` (`remote_text`) + `src/cboard2/tui.py:324-345` (`remote_text`) -- byte-identical branching (merged-PR outranks behind-branch outranks behind-default) and identical message strings (`"PR #{n} merged"`, `"behind {branch}"`, `"?"`, `"—"`), only the wrapper differs (`str` vs `Text(..., style=...)`).
   Also duplicated the same way: `relative()` (`cli.py:372-383` / `tui.py:226-237`, **byte-for-byte identical**, including the docstring), `own_pr_text()` (`cli.py:312-322` / `tui.py:364-374`), `review_pr_text()` (`cli.py:325-328` / `tui.py:377-380`), `branch_text()` (`cli.py:331-337` / `tui.py:240-247`), `head_text()` (`cli.py:340-347` / `tui.py:250-257`), `state_text()` (`cli.py:350-356` / `tui.py:260-272`), `ab_text()`/`ahead_behind_text()` (`cli.py:359-369` / `tui.py:275-285`), and `pr_text()` (`cli.py:299-309` / `tui.py:348-361`). The `_HEAD_SUBJECT_MAX = 40` constant is also declared twice (`cli.py:32`, `tui.py:90`).
   *Why*: **Test 1** — these change for the same reason (a wording or precedence change to "what the Remote/PR/State column says") by the same person, and both are already covered by near-duplicate tests (`tests/test_cli.py::test_the_state_column_names_the_halted_operation_and_the_conflicts` vs. `tests/test_tui.py::test_the_state_cell_reds_a_repo_stopped_mid_rebase`, and both files have their own `test_relative_renders_an_age`). **Test 3** — the codebase already has the right pattern for this exact problem: `gitstate.state_parts(state) -> list[str]` is one shared function both `cli.py` and `tui.py` call and then style independently; the other eight functions never got the same treatment. **Test 6** — what varies (the style/wrapper) is cleanly separable from what's shared (branch conditions and message text), which is exactly why `state_parts` already works this way. Fix: extract the plain-string builders (and `relative`, `_HEAD_SUBJECT_MAX`) into a small Textual-free module (e.g. `cboard2/rowtext.py`) that both import — `cli.py` uses them directly, `tui.py` wraps each in `Text(text, style=...)`. This respects the project's explicit constraint that `cli.py` must stay free of the Textual import (see `app.py`'s docstring on why Textual costs ~0.2s to import and must be deferred).

2. [ ] **`_as_dict` is a byte-for-byte reimplementation across two modules** [Medium] -- the "coerce a JSON value to a string-keyed dict or return `{}`" helper is written twice.
   `src/cboard2/remote.py:859-867` + `src/cboard2/remotecache.py:259-263` -- identical bodies (`if not isinstance(value, dict): return {}` then `cast`), only the docstring is shortened in the second copy — a classic sign of two separate writing passes rather than one shared decision.
   *Why*: **Test 1** (the reason to change — "how do we safely read a field out of untrusted JSON" — is identical in both) and **Test 3** (`remotecache.py` already imports `CHECKS_UNKNOWN, Cached, MergedPR, PullRequest` from `remote.py`, so a shared helper is one import away; move `_as_dict` to `remote.py` as `as_dict` — or a tiny shared module — and drop the copy in `remotecache.py`).

3. [ ] **`config.py`'s two non-negative-integer validators are a copy-paste pair** [Medium] -- `_depth` and `_worktree_limit` are the same validation rule against two different keys.
   `src/cboard2/config.py:162-168` (`_depth`) + `src/cboard2/config.py:171-177` (`_worktree_limit`) -- identical structure: `isinstance(value, bool) or not isinstance(value, int) or value < 0` then raise `ConfigError` naming the key.
   *Why*: **Test 2's business-rule exception** — this is a validation rule, one of the exception's own named examples ("permission check, **validation rule**"), so it's worth flagging even at 2 instances rather than waiting for a third. **Test 3** — the same file already generalizes this exact pattern for other types: `_flag(data, key, fallback)` (`config.py:180-186`) is shared by `remote`, `origin_colors`, and `worktrees`, and `_interval(data, key, fallback)` (`config.py:189-203`) is shared by `dormant_interval` and `remote_interval`. `_depth`/`_worktree_limit` are the one place that pattern wasn't followed. Fix: add `_non_negative_int(data, key, fallback) -> int` alongside `_flag`/`_interval` and delete both call sites' duplicated bodies.

### Inline This -- Wrong Abstractions

No abstraction in this codebase showed conditional accumulation, caller-specific branch forests, or boolean-flag pileup. `RemoteReader`'s multi-parameter internal methods (`_rebuild`, `_state`) and its generic `_map` thread-pool helper looked like candidates at first glance, but each parameter is a distinct piece of domain data (not a flag switching behavior), and `_map` has five legitimate call sites collapsing repeated `ThreadPoolExecutor` boilerplate — a correctly-sized abstraction, not an over-abstraction. Nothing here earns a flag.

### Leave Alone -- Incidental Duplication

1. **Four subprocess-running wrappers look alike but encode different failure contracts** -- `run_git`, `run_ls_remote`, `run_gh`, and `run_step` all wrap `subprocess.run(capture_output=True, text=True, check=False, ...)`, which reads like a single reusable helper waiting to happen.
   `src/cboard2/gitstate.py:166-185` (`run_git`) + `src/cboard2/remote.py:399-424` (`run_ls_remote`) + `src/cboard2/remote.py:377-396` (`run_gh`) + `src/cboard2/pull.py:64-83` (`run_step`) -- each already diverges in a way a caller depends on: `run_git` drops output silently on non-zero exit; `run_gh` deliberately *ignores* the exit code ("A batched query naming one repo that was deleted upstream exits non-zero with the other 65 answers still on stdout"); `run_ls_remote` injects `GIT_SSH_COMMAND`/`BatchMode=yes` to keep an ssh prompt from hanging; `run_step` is the only writer (git-mutating), keeps full stdout+stderr in a `Step`, and separates `TimeoutExpired` into its own message. Forcing these into one function would need a `strip_output`, `check_returncode`, `optional_locks`, and `extra_env` flag — exactly the "boolean-flag pileup" Test 5 warns against.
   *Why*: **Test 1** — each already changes for a different reason (poll-speed budget vs. network-probe budget vs. GitHub-API budget vs. write-safety budget), so a merge would create coupling the moment one caller's timeout or failure semantics need to move independently of the others.

2. **`DEFAULT_MAX_WORKERS` is redeclared with different values in three modules** -- looks like a shared constant, isn't.
   `src/cboard2/activity.py:36` (`= 16`) + `src/cboard2/gitstate.py:37` (`= 16`) + `src/cboard2/remote.py:97` (`= 8`) -- same name, but `remote.py`'s pool caps GitHub/network calls at a lower concurrency than the two purely-local-disk pools.
   *Why*: **Test 2's constant exception does not apply here** because these are not the same fact — they're three independently-tuned budgets that happen to share two of three values by coincidence, not by shared meaning. Unifying them would either force a network-call concurrency limit onto local git polls or vice versa.

3. **cli.py's JSON row fields and remotecache.py's on-disk cache fields look like the same serializer** -- both turn a `RemoteState`/`PullRequest`/`MergedPR` into a `dict`.
   `src/cboard2/cli.py:205-224,227-236,239-248` (`_remote_dict`, `_merged_dict`, `_pr_dict`) + `src/cboard2/remotecache.py:181-190,193-200` (`_stored`, `_stored_merged`) -- structurally similar dict-building, but they serve two contracts that must evolve on separate schedules: the CLI's JSON output is a public scripting API covered by `tests/test_cli.py::test_json_always_carries_the_remote_object`, while the cache file already carries its own explicit schema version (`remotecache.VERSION = 4`) and intentionally omits fields (the two `behind_*` markers) that the JSON output includes.
   *Why*: **Test 4** — different boundaries (a public CLI contract vs. an internal, versioned cache format) that must be free to diverge; the cache format already changed its shape (version 4) without touching the JSON output, which is the proof that merging them would be the wrong move.

### Stats

- Files reviewed: 39 (23 source/script/config files, 15 test files, README excerpt)
- Abstractions evaluated: 12 (state_parts sharing, `_flag`/`_interval` config generalizers, `RemoteReader._map`, the four subprocess runners, the three `DEFAULT_MAX_WORKERS` constants, the cli/tui render-function pairs, `_as_dict`, `_depth`/`_worktree_limit`, the JSON-vs-cache serializers, test `Config`-builder helpers (skipped as test setup), `RemoteReader.__init__`'s DI parameters)
- True duplication found: 3 findings (~11 duplicated symbols/constants total)
- Wrong abstractions found: 0 instances
- Incidental duplication (correctly separate): 3 instances
