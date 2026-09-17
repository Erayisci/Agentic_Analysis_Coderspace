"""Bounded JSON result cache and persistent model-call counters, never pickle."""

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .asset_common import AssetFailure


class AssetStore:
    def __init__(self, config, directory=None):
        self.config = config
        directory = Path(directory or os.environ.get("WEB_ASSET_CACHE_DIR", "/opt/web-tools-cache"))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / "assets.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, created REAL, body TEXT, size INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (hour INTEGER PRIMARY KEY, count INTEGER)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def key(self, request):
        policy = asdict(self.config)
        policy.pop("kloudeks_api_key", None)
        request = {k: v for k, v in request.items() if k != "refresh"}
        return hashlib.sha256(json.dumps(["assets-v2", request, policy], sort_keys=True).encode()).hexdigest()

    def get(self, key):
        now = time.time()
        with self.connect() as db:
            row = db.execute("SELECT created, body FROM cache WHERE key=?", (key,)).fetchone()
        if not row or now - row[0] > self.config.asset_cache_ttl_seconds:
            return None
        result = json.loads(row[1])
        result["cache"] = {"hit": True, "age_seconds": max(0, int(now - row[0]))}
        return result

    def put(self, key, result):
        data = json.dumps(result, ensure_ascii=False)
        size = len(data.encode())
        maximum = self.config.asset_cache_max_bytes
        if size > maximum or result.get("status") not in {"ok", "empty", "partial"}:
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM cache WHERE created < ?", (time.time() - self.config.asset_cache_ttl_seconds,))
            db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?, ?)", (key, time.time(), data, size))
            while db.execute("SELECT COALESCE(SUM(size), 0) FROM cache").fetchone()[0] > maximum:
                db.execute("DELETE FROM cache WHERE key = (SELECT key FROM cache ORDER BY created LIMIT 1)")
        # SQLite reuses freed pages; cap disk growth with the logical byte budget
        # plus small SQLite overhead. Compact outside the write transaction.
        if self.path.stat().st_size > maximum + 1048576:
            with self.connect() as db:
                db.execute("VACUUM")

    def consume_model_call(self):
        maximum = self.config.model_max_calls_per_hour
        hour = int(time.time() // 3600)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM calls WHERE hour < ?", (hour,))
            row = db.execute("SELECT count FROM calls WHERE hour=?", (hour,)).fetchone()
            count = row[0] if row else 0
            if count >= maximum:
                raise AssetFailure("model_limit")
            db.execute("INSERT OR REPLACE INTO calls VALUES (?, ?)", (hour, count + 1))
