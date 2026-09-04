"""Tests for rewriting the dormant key without disturbing the rest of the file."""

from __future__ import annotations

from pathlib import Path

import pytest

from cboard2.config import ConfigError, load_config
from cboard2.configwrite import display, quote, render, toggled, write_dormant

_WITH_COMMENTS = """# Directories to search for git repos.
roots = [
    "~/git",
]

# Levels below a root to descend.
max_depth = 1

# Repos polled once per dormant_interval instead of once per tick.
dormant = []
dormant_interval = "4h"

# Trailing note that must survive.
"""


def test_toggled_adds_then_removes() -> None:
    first = Path("/a")
    second = Path("/b")

    added = toggled((first,), second)
    assert added == (first, second)
    assert toggled(added, second) == (first,)
    assert toggled((), first) == (first,)


def test_every_comment_survives_a_write(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_WITH_COMMENTS, encoding="utf-8")

    write_dormant(target, [Path("/srv/old")])
    after_add = target.read_text(encoding="utf-8")
    write_dormant(target, [])
    after_remove = target.read_text(encoding="utf-8")

    assert after_remove == _WITH_COMMENTS
    for comment in (
        "# Directories to search for git repos.",
        "# Levels below a root to descend.",
        "# Repos polled once per dormant_interval instead of once per tick.",
        "# Trailing note that must survive.",
    ):
        assert comment in after_add
    assert 'dormant_interval = "4h"' in after_add
    assert '    "/srv/old",' in after_add


def test_the_written_value_parses_back(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(_WITH_COMMENTS, encoding="utf-8")
    shelf = Path.home() / "git" / "_old"

    write_dormant(target, [shelf, Path("/srv/elsewhere")])

    config = load_config(target)

    assert config.dormant == (shelf, Path("/srv/elsewhere"))
    assert config.max_depth == 1
    assert config.dormant_interval == 4 * 3600.0
    assert '"~/git/_old"' in target.read_text(encoding="utf-8")


def test_a_multi_line_array_is_replaced_whole(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'dormant = [\n    "/one",\n    "/two",  # keep me? no\n]\nmax_depth = 2\n',
        encoding="utf-8",
    )

    write_dormant(target, [Path("/three")])

    text = target.read_text(encoding="utf-8")

    assert "/one" not in text
    assert "/two" not in text
    assert '    "/three",' in text
    assert "max_depth = 2" in text


def test_an_absent_key_is_appended(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('roots = ["~/git"]\n', encoding="utf-8")

    write_dormant(target, [Path("/srv/old")])

    config = load_config(target)

    assert config.dormant == (Path("/srv/old"),)
    assert config.roots == (Path.home() / "git",)


def test_a_missing_file_is_created(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "config.toml"

    write_dormant(target, [Path("/srv/old")])

    assert load_config(target).dormant == (Path("/srv/old"),)


def test_dormant_interval_is_not_mistaken_for_the_key(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('dormant_interval = "2h"\n', encoding="utf-8")

    write_dormant(target, [Path("/srv/old")])

    config = load_config(target)

    assert config.dormant_interval == 7200.0
    assert config.dormant == (Path("/srv/old"),)


def test_a_key_inside_a_table_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        'max_depth = 1\n\n[other]\ndormant = ["/mine"]\n', encoding="utf-8"
    )

    write_dormant(target, [Path("/srv/old")])

    text = target.read_text(encoding="utf-8")

    assert '[other]\ndormant = ["/mine"]' in text
    assert load_config(target).dormant == (Path("/srv/old"),)


def test_an_unclosed_array_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('dormant = [\n    "/one",\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="not closed"):
        write_dormant(target, [])


def test_a_non_list_value_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('dormant = "/one"\n', encoding="utf-8")

    with pytest.raises(ConfigError, match="list of paths"):
        write_dormant(target, [])


def test_render_and_display_and_quote() -> None:
    assert render([]) == "dormant = []"
    assert render([Path("/a")]) == 'dormant = [\n    "/a",\n]'
    assert display(Path.home()) == "~"
    assert display(Path.home() / "git") == "~/git"
    assert display(Path("/srv/x")) == "/srv/x"
    assert quote('a"b\\c') == '"a\\"b\\\\c"'
