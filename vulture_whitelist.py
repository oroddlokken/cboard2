"""Names vulture cannot see a use for, with the reason each one stays.

Vulture reads one module at a time, so it has no view of a later issue's
consumer or of an entry point declared in packaging metadata. Textual's
name-based dispatch is handled by ``--ignore-names`` in the justfile instead.
"""

from cboard2.activity import Entry
from cboard2.board import Board
from cboard2.cli import main

Entry.repo_path  # the dashboard's activity view keys its rows on this
Board.activity  # the dashboard's activity view calls this; the CLI does not
main  # the cboard2 console script declared in pyproject.toml
