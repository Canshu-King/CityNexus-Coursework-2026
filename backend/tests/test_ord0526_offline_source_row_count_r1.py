"""ORD-0526 · a registered offline source may only claim rows the store actually gained.

The whole point of the offline catalogue source is that the hydration door stops answering
`NO_REGISTERED_SOURCE` for a city this build genuinely holds material for.  The failure mode that
makes that worse rather than better is a source whose *stated* row count is bigger than what lands:
the job would then report a prepared city on the strength of material nobody can list.

So the arm here compares three numbers, and it compares them for every registered source rather
than for the one entry that happens to exist today:

  * `candidate_count`       - what the source file declares usable,
  * `candidates_captured`   - what the capture leg was handed,
  * `places_written`        - the delta read back out of `places` after the write.

`places_written` is deliberately the store's number, not the loop's.  A snapshot that repeats an
`external_id` is the ordinary way the first two agree while the third falls short, so
`test_inflated_fixture_row_count_is_refused_by_the_arm` feeds exactly that file through the same
product code path and requires the arm to refuse it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import catalog_sources
from app.catalog_hydration_operations import _count_city_places, apply_registered_offline_source
from app.db.models import Place
from app.db.session import ensure_schema

ARM = "OFFLINE_SOURCE_ROW_COUNT"
CITY = "CN-TIANJIN"


def _private_store(tmp_path: Path, name: str):
    """A schema-built database of its own, so no test inherits another's material."""

    engine = create_engine(f"sqlite:///{(tmp_path / name).as_posix()}")
    ensure_schema(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def assert_rows_are_not_over_claimed(applied: dict) -> None:
    claim = int(applied["candidate_count"])
    captured = int(applied["candidates_captured"])
    written = int(applied["places_written"])
    assert captured == claim, (
        f"{ARM}: the registered source declares {claim} usable rows but the capture leg produced "
        f"{captured} - the job would be reporting material it never held"
    )
    assert written == captured, (
        f"{ARM}: {captured} rows were captured and the store gained {written}; a claimed count "
        f"above what actually landed is a promise about a catalogue a reader cannot list"
    )


def _snapshot(rows: list[dict]) -> dict:
    return {
        "city_code": CITY,
        "city_name": "Tianjin",
        "captured_at": "2026-09-12T03:15:03.659327+00:00",
        "candidates": rows,
    }


def _row(external_id: str, name: str) -> dict:
    return {
        "external_id": external_id,
        "name": name,
        "category": "museum",
        "latitude": 39.14,
        "longitude": 117.18,
        "provider": "openstreetmap-replay",
    }


def test_every_registered_offline_source_claims_only_rows_that_land(tmp_path: Path) -> None:
    sources = catalog_sources.registered_sources()
    assert sources, "ORD-0526 registers at least one offline catalogue source on this line"
    for source in sources:
        engine, session = _private_store(tmp_path, f"honest_{source.city_code}.db")
        try:
            applied = apply_registered_offline_source(session, source.city_code)
            assert applied["in_effect"], applied
            assert_rows_are_not_over_claimed(applied)
            held = _count_city_places(session, source.city_code)
            # The city ends up holding exactly what the job said it would, and the label on those
            # rows is the offline capture's own, never a provider answer.
            assert held == applied["places_after"] == applied["places_written"] > 0
            markers = sorted(
                {row.provider_key for row in session.query(Place).filter_by(
                    city_code=source.city_code)}
            )
            assert markers == [source.provider_key], markers
        finally:
            engine.dispose()


def test_inflated_fixture_row_count_is_refused_by_the_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five declared rows, three distinct places: the claim is a lie about the catalogue."""

    inflated = _snapshot([
        _row("way/1", "天津文化中心"),
        _row("way/2", "平津战役纪念馆"),
        _row("way/3", "天津义和团纪念馆"),
        _row("way/1", "天津文化中心"),
        _row("way/2", "平津战役纪念馆"),
    ])
    target = tmp_path / "inflated_snapshot.json"
    target.write_text(json.dumps(inflated, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(catalog_sources, "REPO_ROOT", tmp_path)
    source = catalog_sources.OfflineCatalogSource(
        city_code=CITY,
        relative_path=target.name,
        provider_key="openstreetmap-replay",
    )
    facts = catalog_sources.inspect_source(source)
    assert facts["in_effect"] and facts["candidate_count"] == 5, facts
    monkeypatch.setattr(catalog_sources, "OFFLINE_CATALOG_SOURCES", (source,))
    monkeypatch.setattr(catalog_sources, "registered_sources", lambda: (source,))

    engine, session = _private_store(tmp_path, "inflated.db")
    try:
        applied = apply_registered_offline_source(session, CITY)
        assert applied["in_effect"], applied
        assert int(applied["places_written"]) < int(applied["candidate_count"]), applied
        with pytest.raises(AssertionError, match=ARM) as caught:
            assert_rows_are_not_over_claimed(applied)
        assert "5" in str(caught.value) and "3" in str(caught.value), str(caught.value)
    finally:
        engine.dispose()


def test_a_source_about_another_city_is_never_credited_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The city check is what keeps a registered line in a table from becoming a claim about data.

    The Tianjin snapshot's own file says `TSN`, which this build treats as the same city as
    `CN-TIANJIN`; a file that says something else must be refused, because the alternative -
    normalising whatever a file declares - is how one city's material ends up in another's survey.
    """

    other = _snapshot([_row("way/9", "别的城的地点")])
    other["city_code"] = "CN-BEIJING"
    target = tmp_path / "other_city.json"
    target.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(catalog_sources, "REPO_ROOT", tmp_path)
    source = catalog_sources.OfflineCatalogSource(
        city_code=CITY, relative_path=target.name, provider_key="openstreetmap-replay"
    )
    facts = catalog_sources.inspect_source(source)
    assert facts["in_effect"] is False and facts["reason"] == "SOURCE_CITY_MISMATCH", facts
    assert facts["candidate_count"] == 0, facts
