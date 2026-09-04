"""Rewrite the ``dormant`` key in config.toml, leaving the rest of the file alone.

The standard library reads TOML and cannot write it, and a config file people
hand-edit carries comments explaining each key. Regenerating the file from a
parsed :class:`~cboard2.config.Config` would delete those comments, so only
the bytes of one key's value are replaced.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from cboard2.config import ConfigError

if TYPE_CHECKING:
    from collections.abc import Sequence

_KEY = "dormant"

_ABSENT_BLOCK = """
# Repos polled once per dormant_interval instead of once per tick. They still
# get a row; shift+R in the dashboard polls them now.
"""
"""Comment written above the key when the file has no ``dormant`` at all."""


def toggled(dormant: Sequence[Path], path: Path) -> tuple[Path, ...]:
    """Add ``path`` to ``dormant``, or drop it when it is already there."""
    if path in dormant:
        return tuple(entry for entry in dormant if entry != path)
    return (*dormant, path)


def write_dormant(config_file: Path, dormant: Sequence[Path]) -> None:
    """Replace the ``dormant`` array in ``config_file`` with ``dormant``.

    Creates the file when it does not exist, and appends the key when the file
    exists without it. The write goes through a temp file in the same directory
    so an interrupted write cannot truncate a config someone hand-wrote.
    """
    try:
        text = config_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    rendered = render(dormant)
    span = _key_span(text)
    if span is not None:
        start, end = span
        updated = text[:start] + rendered + text[end:]
    else:
        updated = _insert_key(text, rendered)

    _replace_file(config_file, updated)


def _insert_key(text: str, rendered: str) -> str:
    """Add the key at the end of the top-level region, above any table header.

    Appending at the end of the file would land the key inside the last
    ``[table]``, which is a different key that happens to share the name.
    """
    cut = _top_level_end(text)
    head = text[:cut]
    tail = text[cut:]
    if head and not head.endswith("\n"):
        head += "\n"
    block = _ABSENT_BLOCK.lstrip("\n")
    spacer = "\n" if tail else ""
    return f"{head}{block}{rendered}\n{spacer}{tail}"


def render(dormant: Sequence[Path]) -> str:
    """Render the key and its value as TOML.

    Empty stays on one line; anything else goes one path per line, matching how
    ``roots`` is usually written.
    """
    if not dormant:
        return f"{_KEY} = []"
    entries = "".join(f"    {quote(display(path))},\n" for path in dormant)
    return f"{_KEY} = [\n{entries}]"


def display(path: Path) -> str:
    """Return ``path`` written relative to home when it sits under it.

    Keeps the file readable next to the hand-written ``~/git`` entries, rather
    than expanding every line to an absolute path.
    """
    home = Path.home()
    if path == home:
        return "~"
    if home in path.parents:
        return f"~/{path.relative_to(home)}"
    return str(path)


def quote(text: str) -> str:
    """Wrap ``text`` as a TOML basic string, escaping what has to be escaped."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _key_span(text: str) -> tuple[int, int] | None:
    """Return the span of the whole ``dormant = [...]`` assignment, or None.

    Only the region above the first table header is searched, so a ``dormant``
    key inside some future ``[section]`` is not mistaken for this one.
    """
    region = _top_level(text)
    index = _find_key(region)
    if index is None:
        return None
    equals = region.index("=", index)
    bracket = region.find("[", equals)
    if bracket == -1:
        msg = "dormant must be a list of paths"
        raise ConfigError(msg)
    return index, _array_end(region, bracket)


def _top_level(text: str) -> str:
    """Return the part of the file above the first table header."""
    return text[: _top_level_end(text)]


def _top_level_end(text: str) -> int:
    """Return the offset of the first table header, or the end of the file."""
    for offset, line in _lines(text):
        if line.startswith("["):
            return offset
    return len(text)


def _lines(text: str) -> list[tuple[int, str]]:
    """Return each line with the offset it starts at."""
    offsets: list[tuple[int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        offsets.append((offset, line))
        offset += len(line)
    return offsets


def _find_key(region: str) -> int | None:
    """Return the offset of the ``dormant`` assignment, ignoring comments.

    ``dormant_interval`` starts with the same letters, so the character after
    the key has to be checked before calling it a match.
    """
    for offset, line in _lines(region):
        stripped = line.lstrip()
        if not stripped.startswith(_KEY):
            continue
        rest = stripped[len(_KEY) :]
        if rest[:1].isalnum() or rest[:1] == "_":
            continue
        if "=" not in rest:
            continue
        return offset + (len(line) - len(stripped))
    return None


def _array_end(text: str, start: int) -> int:
    """Return the offset just past the ``]`` closing the array at ``start``."""
    depth = 0
    index = start
    quote_char = ""
    while index < len(text):
        char = text[index]
        if quote_char:
            if char == "\\":
                index += 2
                continue
            if char == quote_char:
                quote_char = ""
        elif char in "\"'":
            quote_char = char
        elif char == "#":
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
            continue
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    msg = "dormant array is not closed"
    raise ConfigError(msg)


def _replace_file(target: Path, text: str) -> None:
    """Write ``text`` over ``target`` through a temp file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f"{target.name}.",
        delete=False,
    ) as handle:
        handle.write(text)
        staged = Path(handle.name)
    staged.replace(target)
