"""Tests for reading config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from cboard2.config import (
    DEFAULT_DORMANT_INTERVAL,
    DEFAULT_MAX_DEPTH,
    DEFAULT_REMOTE,
    DEFAULT_WORKTREE_LIMIT,
    ConfigError,
    load_config,
)
from cboard2.remote import DEFAULT_REMOTE_INTERVAL


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "config.toml"
    target.write_text(body, encoding="utf-8")
    return target


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent.toml")

    assert config.roots == (Path.home() / "git",)
    assert config.max_depth == DEFAULT_MAX_DEPTH
    assert config.max_depth == 1
    assert config.dormant == ()
    assert config.dormant_interval == DEFAULT_DORMANT_INTERVAL
    assert config.dormant_interval == 4 * 3600.0
    assert config.remote == DEFAULT_REMOTE
    assert config.remote is True
    assert config.remote_interval == DEFAULT_REMOTE_INTERVAL
    assert config.remote_interval == 300.0
    assert config.worktree_limit == DEFAULT_WORKTREE_LIMIT
    assert config.worktree_limit == 5


def test_reads_the_remote_keys(tmp_path: Path) -> None:
    target = _write(tmp_path, 'remote = false\nremote_interval = "10m"\n')

    config = load_config(target)

    assert config.remote is False
    assert config.remote_interval == 600.0


def test_a_non_boolean_remote_is_rejected(tmp_path: Path) -> None:
    target = _write(tmp_path, 'remote = "yes"\n')

    with pytest.raises(ConfigError, match="remote must be true or false"):
        load_config(target)


def test_an_unparseable_remote_interval_names_its_key(tmp_path: Path) -> None:
    target = _write(tmp_path, 'remote_interval = "soon"\n')

    with pytest.raises(ConfigError, match="remote_interval"):
        load_config(target)


def test_reads_every_key(tmp_path: Path) -> None:
    target = _write(
        tmp_path,
        'roots = ["~/git"]\nmax_depth = 2\ndormant = ["~/git/_old"]\n'
        'dormant_interval = "30m"\n',
    )

    config = load_config(target)

    assert config.roots == (Path.home() / "git",)
    assert config.max_depth == 2
    assert config.dormant == (Path.home() / "git" / "_old",)
    assert config.dormant_interval == 1800.0


def test_reads_the_worktree_limit(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "worktree_limit = 12\n"))

    assert config.worktree_limit == 12


def test_bare_number_interval_is_seconds(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "dormant_interval = 90\n"))

    assert config.dormant_interval == 90.0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('dormant_interval = "4x"\n', "dormant_interval"),
        ("dormant_interval = true\n", "dormant_interval"),
        ('roots = "~/git"\n', "roots"),
        ("dormant = [1, 2]\n", "dormant"),
        ("max_depth = -1\n", "max_depth"),
        ("worktree_limit = -2\n", "worktree_limit"),
        ('worktree_limit = "five"\n', "worktree_limit"),
        ('max_depth = "deep"\n', "max_depth"),
    ],
)
def test_unusable_value_names_its_key(tmp_path: Path, body: str, expected: str) -> None:
    with pytest.raises(ConfigError, match=expected):
        load_config(_write(tmp_path, body))


def test_malformed_toml_names_the_file(tmp_path: Path) -> None:
    target = _write(tmp_path, "roots = [\n")

    with pytest.raises(ConfigError, match=str(target.name)):
        load_config(target)
