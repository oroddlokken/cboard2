# Agent Instructions

## Issue tracking

This project uses **dcat** for issue tracking. Run `dcat prime --opinionated` at the start of each session to load the workflow, then `dcat list --agent-only` to see open issues. Work bugs before features and high priority before low; deviate only on explicit user instruction.

Make a separate parallel Bash tool call per `dcat` command. Chaining them with `&&` and `echo` separators merges the output into one stream, so a failure in the middle is easy to miss.

Mark an issue `in_progress` at the moment you start it, and `in_review` once its work is done. The status is how the user sees what you are working on right now, so it is only useful while it stays accurate.

Working several related issues at once is fine. Mark only the ones you have actually started — a whole backlog marked `in_progress` conveys nothing. On a priority conflict, ask which to take first.

When the user raises a new bug, feature, or anything else that warrants a code change, ask whether to create an issue before writing code. Set labels with `--labels` drawn from the issue's content: `cli`, `tui`, `api`, `docs`, `testing`, `refactor`, `ux`, `performance`.

When research or discussion produces findings relevant to an existing issue, ask these as two separate questions, in order:

1. "Should I update issue [id] with these findings?"
2. "Should I start working on the implementation?"

Keep them separate — the user may want the findings recorded without starting work, and one combined question forces a single answer to both.

### Closing issues

Wait for explicit user approval before closing any issue. `dcat reopen` exists, but reopening loses the workflow state the issue carried, so a premature close costs more than the extra turn.

1. Set status to `in_review`: `dcat update --status in_review $issueId`
2. Tell the user the issue is ready for testing
3. Ask: "Can I close issue [id] '[title]'?"
4. Run `dcat close` after the user confirms
