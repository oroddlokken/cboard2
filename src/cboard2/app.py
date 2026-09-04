"""Launcher for the dashboard, kept apart from the CLI so ``ls`` stays light.

Importing this module pulls in Textual. :mod:`cboard2.cli` imports it inside
the branch that needs it, so a statusline calling ``cboard2 busy`` never pays
for the UI.
"""

from __future__ import annotations

import sys

from cboard2.board import Board
from cboard2.config import ConfigError, load_config
from cboard2.tui import CboardApp


def launch() -> int:
    """Open the dashboard, or report an unusable config and exit non-zero."""
    try:
        config = load_config()
    except ConfigError as exc:
        sys.stderr.write(f"cboard2: {exc}\n")
        return 2
    CboardApp(Board(config)).run()
    return 0
