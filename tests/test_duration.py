"""Tests for the shared duration parser."""

from __future__ import annotations

import pytest

from cboard2.duration import parse_duration


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("42", 42.0),
        ("30s", 30.0),
        ("5m", 300.0),
        ("4h", 14400.0),
        ("1d", 86400.0),
        (" 2H ", 7200.0),
        ("0.5h", 1800.0),
    ],
)
def test_parses_accepted_forms(text: str, expected: float) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "5x", "h", "-1", "-1h", "nan", "inf"])
def test_rejects_unusable_input(text: str) -> None:
    with pytest.raises(ValueError, match="duration"):
        parse_duration(text)
