"""The external zone's storage contract: atomic Parquet per source, views over
globs that a read-only connection sees grow, one writer per source.

These tests run against a temporary zone and a temporary DuckDB file, so they
need no build and touch nothing under data/.
"""
import threading

import duckdb
import pandas as pd
import pytest

from backend.lakehouse import external_store as store


@pytest.fixture
def zone(tmp_path, monkeypatch):
    """Point the store at a throwaway directory."""
    root = tmp_path / "external"
    monkeypatch.setattr(store, "EXTERNAL_DIR", root)
    monkeypatch.setattr(store, "EXTERNAL_RAW_DIR", root / "_raw")
    monkeypatch.setattr(store, "EXTERNAL_SEED_DIR", root / "_seed")
    return root


def _bundle(url="https://example.org/a.xlsx", n=3, value=1.0):
    source_id = store.source_id_for(url)
    key = f"{source_id}/sheet1/konut"
    periods = pd.date_range("2021-01-01", periods=n, freq="MS")
    return store.SourceBundle(
        manifest={"source_id": source_id, "url": url, "final_url": url, "kind": "excel",
                  "content_sha256": "abc", "n_bytes": 10, "fetched_at": "2026-09-19T00:00:00+00:00",
                  "n_series": 1, "n_observations": n, "status": "ok", "extraction_route": "in_process"},
        series=pd.DataFrame([{"series_key": key, "source_id": source_id, "name": "Konut", "name_clean": "Konut",
                              "location": "Sheet1", "unit": "milyon TL", "unit_source": "caption",
                              "unit_verified": False, "temporal_semantics": "stock",
                              "semantics_source": "heuristic", "native_frequency": "monthly",
                              "monthly_rule": "last", "published_start": periods[0], "published_end": periods[-1],
                              "n_native_obs": n, "n_periods": n, "url": url}]),
        observations=pd.DataFrame({"period": periods, "series_key": key, "source_id": source_id,
                                   "value": [value] * n, "value_avg": [value] * n, "value_last": [value] * n,
                                   "value_sum": [None] * n, "n_native_obs": 1, "monthly_rule": "last"}),
        native=pd.DataFrame({"date": periods, "series_key": key, "source_id": source_id,
                             "value": [value] * n, "grain": "monthly"}),
        quality=pd.DataFrame([{"source_id": source_id, "series_key": key, "check": "enough_periods",
                               "passed": True, "detail": f"{n}"}]),
        raw_bytes=b"raw", raw_ext="xlsx")


def test_seed_writes_every_schema_as_an_empty_typed_file(zone):
    store.seed()
    for name, schema in store.SCHEMAS.items():
        path = zone / "_seed" / f"{name}.parquet"
        assert path.exists()
        frame = pd.read_parquet(path)
        assert list(frame.columns) == schema.names and frame.empty


def test_a_read_only_connection_sees_a_source_landed_after_the_views_were_created(zone, tmp_path):
    """The mechanism the whole design rests on: the build creates views over a
    glob, and a later Parquet file under the glob is visible to a connection
    that was already open read-only -- no rebuild, no lock, no reconnect."""
    store.seed()
    db = tmp_path / "lh.duckdb"
    con = duckdb.connect(str(db))
    store.create_views(con)
    con.close()

    reader = duckdb.connect(str(db), read_only=True)
    assert reader.execute("SELECT count(*) FROM external_observations").fetchone()[0] == 0
    assert reader.execute("SELECT count(*) FROM external_series").fetchone()[0] == 0

    store.write_source(_bundle(n=3))

    assert reader.execute("SELECT count(*) FROM external_observations").fetchone()[0] == 3
    row = reader.execute(
        "SELECT s.unit, s.unit_verified, o.period FROM external_series s "
        "JOIN external_observations o USING (series_key) ORDER BY o.period LIMIT 1").fetchone()
    assert row[0] == "milyon TL" and row[1] is False and str(row[2]) == "2021-01-01"
    reader.close()


def test_the_manifest_is_the_commit_marker_and_the_cache_key(zone):
    url = "https://example.org/a.xlsx#section"
    assert store.existing(url) is None
    store.write_source(_bundle(url=url))
    manifest = store.existing("https://example.org/a.xlsx")   # the fragment is not identity
    assert manifest and manifest["content_sha256"] == "abc"
    assert store.is_current(url, "abc") and not store.is_current(url, "changed")
    assert store.list_sources().source_id.tolist() == [manifest["source_id"]]
    assert (zone / "_raw" / manifest["source_id"]).exists()      # the bytes were archived
    assert store.remove_source(manifest["source_id"]) is True
    assert store.existing(url) is None and store.list_sources().empty


def test_writers_of_one_source_serialise(zone):
    """Two sessions ingesting the same URL must not interleave their files."""
    errors = []

    def land(value):
        try:
            store.write_source(_bundle(n=5, value=value))
        except Exception as exc:                          # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=land, args=(float(i),)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    parts = store.read_source(store.source_id_for("https://example.org/a.xlsx"))
    assert len(parts["observations"]) == 5
    assert parts["observations"].value.nunique() == 1        # one writer's rows, not a mix
    assert not (zone / parts["manifest"].source_id[0] / ".lock").exists()


def test_frames_are_cast_onto_the_schema(zone):
    """A missing column becomes NULL, an int survives a NaN, a timestamp becomes a DATE."""
    bundle = _bundle()
    bundle.series = bundle.series.drop(columns=["matched_lakehouse_key"], errors="ignore")
    bundle.series["n_periods"] = [None]
    store.write_source(bundle)
    parts = store.read_source(bundle.source_id)
    assert "matched_lakehouse_key" in parts["series"].columns
    assert pd.isna(parts["series"].n_periods.iloc[0])
    assert str(parts["observations"].period.iloc[0]) == "2021-01-01"
