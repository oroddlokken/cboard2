"""Tests for the on-disk remote cache."""

from __future__ import annotations

import json
import os
import stat
from typing import TYPE_CHECKING

import pytest

from cboard2.remote import Cached, PullRequest
from cboard2.remotecache import VERSION, cache_path, load, save

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

READ_AT = 1_800_000_000.0
SHA = "a" * 40


def _pr(number: int, *, draft: bool = False) -> PullRequest:
    return PullRequest(
        number=number,
        title=f"Change {number}",
        url=f"https://github.com/acme/one/pull/{number}",
        draft=draft,
        updated_at=READ_AT - 60.0,
    )


def _cached(
    *,
    defaults: Mapping[str, tuple[str, str]] | None = None,
    prs: Mapping[str, tuple[PullRequest, ...]] | None = None,
    prs_known: bool = True,
) -> Cached:
    return Cached(
        read_at=READ_AT,
        defaults={"acme/one": ("main", SHA)} if defaults is None else defaults,
        prs={"acme/one": (_pr(9, draft=True), _pr(7))} if prs is None else prs,
        prs_known=prs_known,
    )


def test_cache_path_prefers_the_env_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "elsewhere.json"
    monkeypatch.setenv("CBOARD2_CACHE", str(target))

    assert cache_path() == target


def test_cache_path_falls_back_to_xdg_then_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CBOARD2_CACHE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

    assert cache_path() == tmp_path / "cboard2" / "remote.json"


def test_a_read_survives_a_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "remote.json"

    assert save(target, _cached()) is True

    loaded = load(target)

    assert loaded is not None
    assert loaded.read_at == READ_AT
    assert loaded.defaults == {"acme/one": ("main", SHA)}
    assert loaded.prs_known is True
    assert [pr.number for pr in loaded.prs["acme/one"]] == [9, 7]
    assert [pr.draft for pr in loaded.prs["acme/one"]] == [True, False]
    assert loaded.prs["acme/one"][0].url.endswith("/pull/9")
    assert loaded.prs["acme/one"][0].updated_at == READ_AT - 60.0


def test_an_origin_url_round_trips_as_its_own_key(tmp_path: Path) -> None:
    origin = "git@git.example.com:acme/one.git"
    target = tmp_path / "remote.json"
    save(target, _cached(defaults={origin: ("trunk", SHA)}, prs={}))

    loaded = load(target)

    assert loaded is not None
    assert loaded.defaults == {origin: ("trunk", SHA)}


def test_a_failed_search_round_trips_as_unknown(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    save(target, _cached(prs={}, prs_known=False))

    loaded = load(target)

    assert loaded is not None
    assert loaded.prs_known is False
    assert loaded.prs == {}
    assert loaded.defaults == {"acme/one": ("main", SHA)}


def test_only_the_slugs_handed_over_are_written(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    save(target, _cached())
    save(target, _cached(defaults={"acme/two": ("master", SHA)}, prs={}))

    loaded = load(target)

    assert loaded is not None
    assert list(loaded.defaults) == ["acme/two"]
    assert loaded.prs == {}


def test_a_missing_file_reads_as_cold(tmp_path: Path) -> None:
    assert load(tmp_path / "absent.json") is None


def test_a_directory_in_place_of_the_file_reads_as_cold(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.mkdir()

    assert load(target) is None


@pytest.mark.parametrize(
    "body",
    [
        "",
        "not json",
        "[]",
        '{"version": 1}',
        '{"version": 1, "read_at": "soon"}',
        '{"version": 1, "read_at": true}',
        '{"version": 999, "read_at": 1.0}',
        '{"read_at": 1.0}',
    ],
)
def test_an_unusable_file_reads_as_cold(tmp_path: Path, body: str) -> None:
    target = tmp_path / "remote.json"
    target.write_text(body, encoding="utf-8")

    assert load(target) is None


def test_a_truncated_file_reads_as_cold(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    save(target, _cached())
    whole = target.read_text(encoding="utf-8")
    target.write_text(whole[: len(whole) // 2], encoding="utf-8")

    assert load(target) is None


def test_an_entry_missing_its_branch_is_dropped_not_guessed(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.write_text(
        json.dumps(
            {
                "version": VERSION,
                "read_at": READ_AT,
                "prs_known": True,
                "repos": {
                    "acme/one": {"default_sha": SHA, "prs": []},
                    "acme/two": {"default_branch": "main", "default_sha": SHA},
                },
            },
        ),
        encoding="utf-8",
    )

    loaded = load(target)

    assert loaded is not None
    assert list(loaded.defaults) == ["acme/two"]


def test_a_pr_entry_missing_its_number_is_skipped(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.write_text(
        json.dumps(
            {
                "version": VERSION,
                "read_at": READ_AT,
                "prs_known": True,
                "repos": {
                    "acme/one": {
                        "default_branch": "main",
                        "default_sha": SHA,
                        "prs": [{"title": "No number"}, {"number": 4}],
                    },
                },
            },
        ),
        encoding="utf-8",
    )

    loaded = load(target)

    assert loaded is not None
    assert [pr.number for pr in loaded.prs["acme/one"]] == [4]
    assert loaded.prs["acme/one"][0].title == ""


def test_an_unwritable_directory_reports_failure_instead_of_raising(
    tmp_path: Path,
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        assert save(locked / "remote.json", _cached()) is False
    finally:
        locked.chmod(stat.S_IRWXU)


def test_a_write_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"

    save(target, _cached())

    assert sorted(entry.name for entry in os.scandir(tmp_path)) == ["remote.json"]
