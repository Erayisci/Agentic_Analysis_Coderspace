"""Durable DuckDB evidence and typed external observations.

Every returned tool output is committed before reaching the model. Validated
external series are stored in SQL-queryable relations in the same transaction.
The curated BDDK lakehouse remains independently rebuildable. Connections are
short-lived; a process lock serializes threads and opening retries allow other
API processes to finish their short transactions.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
import time
from uuid import uuid4

import duckdb

_LOCK = RLock()


class EvidenceStorageError(RuntimeError):
    """Evidence could not be saved; do not claim a successful ingestion."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceStorageError("Research evidence could not be serialized for storage.") from exc


def _rows(cursor):
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


class EvidenceStore:
    def __init__(self, path, legacy_path=None):
        self.path = Path(path)
        if self.path.suffix in (".sqlite", ".sqlite3"):
            legacy_path, self.path = self.path, self.path.with_suffix(".duckdb")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    id VARCHAR PRIMARY KEY, session_id VARCHAR NOT NULL,
                    question VARCHAR NOT NULL, mode VARCHAR NOT NULL,
                    status VARCHAR NOT NULL, created_at VARCHAR NOT NULL,
                    finished_at VARCHAR, response_json JSON
                );
                CREATE TABLE IF NOT EXISTS research_tool_results (
                    id BIGINT PRIMARY KEY, run_id VARCHAR NOT NULL,
                    tool VARCHAR NOT NULL, arguments_json JSON NOT NULL,
                    output_json JSON NOT NULL, status VARCHAR NOT NULL,
                    created_at VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_series (
                    id VARCHAR PRIMARY KEY, source_url VARCHAR NOT NULL,
                    value_column VARCHAR NOT NULL, period_column VARCHAR NOT NULL,
                    unit VARCHAR NOT NULL, temporal_semantics VARCHAR NOT NULL,
                    metadata_json JSON NOT NULL
                );
                CREATE TABLE IF NOT EXISTS external_observations (
                    tool_result_id BIGINT NOT NULL, series_id VARCHAR NOT NULL,
                    period DATE NOT NULL, value DOUBLE NOT NULL,
                    PRIMARY KEY (tool_result_id, series_id, period)
                );
                CREATE OR REPLACE VIEW external_observations_latest AS
                    SELECT o.*, s.source_url, s.value_column, s.unit, s.temporal_semantics
                    FROM external_observations o JOIN external_series s ON s.id=o.series_id
                    WHERE o.tool_result_id = (
                        SELECT max(v.tool_result_id) FROM external_observations v WHERE v.series_id=o.series_id
                    );
            """)
        if legacy_path and Path(legacy_path).exists():
            self._import_sqlite(Path(legacy_path))

    @contextmanager
    def _connect(self):
        with _LOCK:
            conn = None
            try:
                deadline = time.monotonic() + 30
                while conn is None:
                    try:
                        conn = duckdb.connect(str(self.path))
                    except duckdb.IOException as exc:
                        if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                conn.execute("BEGIN TRANSACTION")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
            except duckdb.Error as exc:
                raise EvidenceStorageError("DuckDB research evidence could not be saved or loaded.") from exc
            finally:
                if conn is not None:
                    conn.close()

    def _import_sqlite(self, path):
        """Copy completed legacy runs once per ID; leave the SQLite file intact."""
        try:
            with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as legacy:
                runs = legacy.execute("SELECT * FROM research_runs WHERE status != 'running'").fetchall()
                results = legacy.execute("""SELECT t.* FROM research_tool_results t
                    JOIN research_runs r ON r.id=t.run_id WHERE r.status != 'running' ORDER BY t.id""").fetchall()
            with self._connect() as conn:
                known = {row[0] for row in conn.execute("SELECT id FROM research_runs").fetchall()}
                new_runs = [row for row in runs if row[0] not in known]
                new_ids = {row[0] for row in new_runs}
                if new_runs:
                    conn.executemany("INSERT INTO research_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", new_runs)
                next_id = conn.execute("SELECT coalesce(max(id),0) FROM research_tool_results").fetchone()[0]
                for row in results:
                    if row[1] not in new_ids:
                        continue
                    next_id += 1
                    conn.execute("INSERT INTO research_tool_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (next_id, *row[1:]))
                    if row[2] == "ingest_external":
                        self._store_series(conn, next_id, json.loads(row[4]))
        except sqlite3.Error as exc:
            raise EvidenceStorageError("Legacy research history could not be migrated to DuckDB.") from exc

    def start(self, session_id, question, mode):
        run_id = str(uuid4())
        with self._connect() as conn:
            conn.execute("INSERT INTO research_runs VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL)",
                         (run_id, session_id, question, mode, _now()))
        return run_id

    def record(self, run_id, tool, arguments, output):
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM research_runs WHERE id=?", (run_id,)).fetchone():
                raise EvidenceStorageError("Cannot attach evidence to an unknown research run.")
            record_id = conn.execute("SELECT coalesce(max(id),0)+1 FROM research_tool_results").fetchone()[0]
            conn.execute("INSERT INTO research_tool_results VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (record_id, run_id, tool, _json(arguments), _json(output),
                          output.get("status", "ok"), _now()))
            if tool == "ingest_external" and output.get("status") == "ok":
                self._store_series(conn, record_id, output)

    @staticmethod
    def _store_series(conn, record_id, output):
        citation = output.get("citation", {})
        values = output.get("values", [])
        if not values:
            return
        fields = ("url", "value_column", "period_column", "unit", "temporal_semantics")
        if any(not citation.get(field) for field in fields):
            raise EvidenceStorageError("External series lacks source, columns, unit or temporal semantics.")
        identity = {field: citation.get(field) for field in (*fields, "sheet", "monthly_rule")}
        series_id = hashlib.sha256(_json(identity).encode()).hexdigest()
        observations = []
        for row in values:
            if isinstance(row["value"], bool) or not math.isfinite(float(row["value"])):
                raise EvidenceStorageError("External series contains a non-finite numeric value.")
            observations.append((record_id, series_id, row["period"][:10], row["value"]))
        conn.execute("""INSERT INTO external_series VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT (id) DO UPDATE SET metadata_json=excluded.metadata_json""",
                     (series_id, *(citation[field] for field in fields), _json(citation)))
        conn.executemany("INSERT INTO external_observations VALUES (?, ?, ?, ?)", observations)

    def finish(self, run_id, response, status):
        with self._connect() as conn:
            conn.execute("""UPDATE research_runs SET status=?, finished_at=?, response_json=?
                            WHERE id=?""", (status, _now(), _json(response), run_id))
            count = conn.execute("SELECT count(*) FROM research_tool_results WHERE run_id=?",
                                 (run_id,)).fetchone()[0]
        return {"status": "saved", "format": "duckdb", "run_id": run_id, "tool_results": count}

    def list_runs(self, session_id, limit=50):
        with self._connect() as conn:
            return _rows(conn.execute("""
                SELECT r.id, r.question, r.mode, r.status, r.created_at, r.finished_at,
                    (SELECT count(*) FROM research_tool_results t WHERE t.run_id=r.id) AS tool_results
                FROM research_runs r WHERE session_id=? ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit)))

    def get_run(self, session_id, run_id):
        with self._connect() as conn:
            rows = _rows(conn.execute("SELECT * FROM research_runs WHERE id=? AND session_id=?",
                                      (run_id, session_id)))
            if not rows:
                return None
            result = rows[0]
            result["response"] = json.loads(result.pop("response_json") or "null")
            result["tool_results"] = _rows(conn.execute(
                "SELECT * FROM research_tool_results WHERE run_id=? ORDER BY id", (run_id,)))
            for record in result["tool_results"]:
                record["arguments"] = json.loads(record.pop("arguments_json"))
                record["output"] = json.loads(record.pop("output_json"))
            return result
