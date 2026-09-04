"""Duration strings, shared by the config file and the CLI's ``--since`` flags.

One parser so ``dormant_interval = "4h"`` and ``--since 4h`` cannot drift apart.
"""

from __future__ import annotations

import math

_SUFFIXES: dict[str, float] = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
"""Accepted unit suffixes and their length in seconds."""


def parse_duration(text: str) -> float:
    """Parse ``30s`` / ``5m`` / ``4h`` / ``1d``, or a bare number of seconds.

    Raises :class:`ValueError` for anything else, including an empty string and
    a negative or non-finite value.
    """
    raw = text.strip()
    if not raw:
        msg = "duration must not be empty"
        raise ValueError(msg)

    suffix = raw[-1].lower()
    if suffix in _SUFFIXES:
        number, unit = raw[:-1], _SUFFIXES[suffix]
    else:
        number, unit = raw, 1.0

    try:
        value = float(number)
    except ValueError as exc:
        msg = f"invalid duration: {text!r}"
        raise ValueError(msg) from exc

    if not math.isfinite(value) or value < 0:
        msg = f"duration must be a non-negative number of seconds: {text!r}"
        raise ValueError(msg)
    return value * unit
