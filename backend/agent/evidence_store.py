"""Durable web evidence, separate from the rebuilt, read-only analytics lakehouse.

Every tool result is committed before the caller gives it to the model. Store
the complete returned extraction, including limits/errors, not just excerpts
that fit in the model context. SQLite connections are short-lived and WAL lets
the website inspect evidence while another request appends it.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4


class EvidenceStorageError(RuntimeError):
    """Evidence could not be saved; do not claim a successful ingestion."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise EvidenceStorageError("Research evidence could not be serialized for storage.") from exc


class EvidenceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS research_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                    question TEXT NOT NULL, mode TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    finished_at TEXT, response_json TEXT
                );
                CREATE INDEX IF NOT EXISTS research_runs_session
                    ON research_runs(session_id, created_at);
                CREATE TABLE IF NOT EXISTS research_tool_results (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL
                        REFERENCES research_runs(id),
                    tool TEXT NOT NULL, arguments_json TEXT NOT NULL,
                    output_json TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_results_run
                    ON research_tool_results(run_id);
            """)

    @contextmanager
    def _connect(self):
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        except sqlite3.Error as exc:
            raise EvidenceStorageError("Research evidence could not be saved or loaded.") from exc
        finally:
            if conn is not None:
                conn.close()

    def start(self, session_id, question, mode):
        run_id = str(uuid4())
        with self._connect() as conn:
            conn.execute("INSERT INTO research_runs VALUES (?, ?, ?, ?, 'running', ?, NULL, NULL)",
                         (run_id, session_id, question, mode, _now()))
        return run_id

    def record(self, run_id, tool, arguments, output):
        with self._connect() as conn:
            conn.execute("""INSERT INTO research_tool_results
                (run_id, tool, arguments_json, output_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                         (run_id, tool, _json(arguments), _json(output),
                          output.get("status", "ok"), _now()))

    def finish(self, run_id, response, status):
        with self._connect() as conn:
            conn.execute("""UPDATE research_runs SET status=?, finished_at=?, response_json=?
                            WHERE id=?""", (status, _now(), _json(response), run_id))
            count = conn.execute("SELECT count(*) FROM research_tool_results WHERE run_id=?",
                                 (run_id,)).fetchone()[0]
        return {"status": "saved", "run_id": run_id, "tool_results": count}

    def list_runs(self, session_id, limit=50):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT r.id, r.question, r.mode, r.status, r.created_at, r.finished_at,
                    (SELECT count(*) FROM research_tool_results t WHERE t.run_id=r.id) AS tool_results
                FROM research_runs r WHERE session_id=? ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit))]

    def get_run(self, session_id, run_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM research_runs WHERE id=? AND session_id=?",
                               (run_id, session_id)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["response"] = json.loads(result.pop("response_json") or "null")
            result["tool_results"] = []
            for item in conn.execute("SELECT * FROM research_tool_results WHERE run_id=? ORDER BY id", (run_id,)):
                record = dict(item)
                record["arguments"] = json.loads(record.pop("arguments_json"))
                record["output"] = json.loads(record.pop("output_json"))
                result["tool_results"].append(record)
            return result
