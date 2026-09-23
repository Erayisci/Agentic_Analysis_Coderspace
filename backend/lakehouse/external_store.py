"""The external zone of the lakehouse: runtime sources as Parquet, read through views.

`data/lakehouse.duckdb` has exactly one writer, `backend.lakehouse.build`, and
every other connection opens it `read_only=True`. A source handed over at
runtime (a demo-day URL) therefore cannot be written into the database by the
API process -- DuckDB allows one writer *or* readers, and the build's own
`CREATE OR REPLACE TABLE` loop is the only place that lock is taken.

It does not need to be. Parquet is the lakehouse's storage layer (every base
table is written as Parquet before it is loaded into DuckDB), so a runtime
source lands as Parquet under `data/external/<source_id>/`, and the build
creates one VIEW per file over the glob `data/external/*/<file>.parquet`.
Measured on DuckDB 1.5.5: a view over a glob is re-bound on every query, so a
read-only connection opened before a source was written sees that source's rows
on its next query, with no reconnect and no lock. The glob must match at least
one file when the view is bound, which is why `seed()` writes zero-row files
with the full schema under `_seed/`.

So the invariant becomes: the DuckDB file has one writer (the build);
`data/external/` has one writer (this module); everything else reads.

Write protocol, because readers are live while a source lands:

- every Parquet file is written to `<name>.parquet.tmp` and moved into place
  with `os.replace`, so a reader sees either the old file or the new one;
- `manifest.parquet` is written LAST -- it is the commit marker
  `existing()` reads, so a half-written source is never reported as landed;
- one source at a time per `source_id`: an in-process lock plus an on-disk
  `.lock` file (`O_CREAT | O_EXCL`, stale after ten minutes) so two sessions
  ingesting the same URL do not interleave their files;
- on Windows a reader mid-query holds the file open and `os.replace` raises
  `PermissionError`; readers are short-lived (every tool opens and closes per
  call), so the move is retried briefly rather than failed.

The raw bytes are archived under `_raw/<source_id>/<sha256>.<ext>`, mirroring
the repo's rule for every other source: the answer is archived beside the
question, so a rebuild can re-parse what was fetched without the network.
"""
import hashlib
import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional
from urllib.parse import urlsplit, urlunsplit

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from ..core.config import EXTERNAL_DIR, EXTERNAL_RAW_DIR, EXTERNAL_SEED_DIR

# Directories under data/external/ that are not sources.
RESERVED_DIRS = {"_seed", "_raw", "_cache"}

# One Parquet schema per file. Every source file is cast to these before it is
# written, so `union_by_name` over the glob never has to reconcile a DATE in
# one file with a TIMESTAMP in another. The vocabulary of `temporal_semantics`
# and `monthly_rule` is the one `metrics`, `bulletin_metrics` and
# `macro_series` share (see CLAUDE.md, "one vocabulary across all three").
SCHEMAS: Dict[str, pa.Schema] = {
    "manifest": pa.schema([
        ("source_id", pa.string()), ("url", pa.string()), ("final_url", pa.string()),
        ("parent_source_id", pa.string()), ("kind", pa.string()), ("content_type", pa.string()),
        ("content_sha256", pa.string()), ("n_bytes", pa.int64()), ("fetched_at", pa.string()),
        ("title", pa.string()), ("hint", pa.string()), ("extraction_route", pa.string()),
        ("n_series", pa.int64()), ("n_observations", pa.int64()), ("status", pa.string()),
        ("warnings", pa.string()),
    ]),
    "series": pa.schema([
        ("series_key", pa.string()), ("source_id", pa.string()), ("name", pa.string()),
        ("name_clean", pa.string()), ("location", pa.string()), ("unit", pa.string()),
        ("unit_source", pa.string()), ("unit_verified", pa.bool_()),
        ("temporal_semantics", pa.string()), ("semantics_source", pa.string()),
        ("native_frequency", pa.string()), ("monthly_rule", pa.string()),
        ("published_start", pa.date32()), ("published_end", pa.date32()),
        ("n_native_obs", pa.int64()), ("n_periods", pa.int64()),
        ("matched_lakehouse_key", pa.string()), ("matched_source", pa.string()),
        ("match_agreement_pct", pa.float64()), ("url", pa.string()),
    ]),
    "observations": pa.schema([
        ("period", pa.date32()), ("series_key", pa.string()), ("source_id", pa.string()),
        ("value", pa.float64()), ("value_avg", pa.float64()), ("value_last", pa.float64()),
        ("value_sum", pa.float64()), ("n_native_obs", pa.int64()), ("monthly_rule", pa.string()),
    ]),
    "observations_native": pa.schema([
        ("date", pa.date32()), ("series_key", pa.string()), ("source_id", pa.string()),
        ("value", pa.float64()), ("grain", pa.string()),
    ]),
    "quality": pa.schema([
        ("source_id", pa.string()), ("series_key", pa.string()), ("check", pa.string()),
        ("passed", pa.bool_()), ("detail", pa.string()),
    ]),
}

# view name -> file stem. The build creates these; nothing else does.
VIEWS: Dict[str, str] = {
    "external_sources": "manifest",
    "external_series": "series",
    "external_observations": "observations",
    "external_observations_native": "observations_native",
    "external_quality_report": "quality",
}


def canonical_url(url: str) -> str:
    """The URL as identity: whitespace stripped, fragment dropped."""
    parts = urlsplit(str(url).strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def source_id_for(url: str) -> str:
    """Twelve hex characters of the canonical URL's sha256: stable, ASCII, short
    enough to prefix every series_key without dominating it."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:12]


def source_dir(source_id: str) -> Path:
    return EXTERNAL_DIR / source_id


@dataclass
class SourceBundle:
    """Everything one landed source writes. Frames may be empty; the schema is
    still enforced so the views stay well-typed."""

    manifest: dict
    series: pd.DataFrame
    observations: pd.DataFrame
    native: pd.DataFrame
    quality: pd.DataFrame
    raw_bytes: Optional[bytes] = None
    raw_ext: str = "bin"
    warnings: List[str] = field(default_factory=list)

    @property
    def source_id(self) -> str:
        return self.manifest["source_id"]


# -- locking -----------------------------------------------------------------

_LOCKS: Dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def source_lock(source_id: str, timeout: float = 60.0, stale_after: float = 600.0) -> Iterator[None]:
    """Serialise writers of one source: threads in this process, then processes
    on this disk. A lock file older than `stale_after` belongs to a writer that
    died and is reclaimed."""
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(source_id, threading.Lock())
    if not lock.acquire(timeout=timeout):
        raise TimeoutError(f"another ingest of source {source_id} is still running")
    try:
        directory = source_dir(source_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                break
            except FileExistsError:
                try:
                    age = time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > stale_after:
                    path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"source {source_id} is locked by another process ({path})")
                time.sleep(0.2)
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
    finally:
        lock.release()


# -- writing -----------------------------------------------------------------

def _to_table(frame: pd.DataFrame, schema: pa.Schema) -> pa.Table:
    """Cast a frame onto the file's schema: missing columns become nulls, extra
    columns are dropped, dates become DATE, ints stay nullable."""
    if frame is None or len(frame) == 0:
        return schema.empty_table()
    out = pd.DataFrame(index=frame.index)
    for fld in schema:
        column = frame[fld.name] if fld.name in frame.columns else pd.Series([None] * len(frame), index=frame.index)
        if pa.types.is_date32(fld.type):
            stamps = pd.to_datetime(column, errors="coerce")
            out[fld.name] = [None if pd.isna(s) else s.date() for s in stamps]
        elif pa.types.is_integer(fld.type):
            out[fld.name] = pd.to_numeric(column, errors="coerce").astype("Int64")
        elif pa.types.is_floating(fld.type):
            out[fld.name] = pd.to_numeric(column, errors="coerce").astype("float64")
        elif pa.types.is_boolean(fld.type):
            out[fld.name] = column.astype("boolean")
        else:
            out[fld.name] = column.astype(object).where(column.notna(), None).map(
                lambda v: None if v is None else str(v))
    return pa.Table.from_pandas(out, schema=schema, preserve_index=False)


def _atomic_write(table: pa.Table, path: Path, attempts: int = 6) -> None:
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp)
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # A reader on Windows has the old file open; readers are per-call
            # and close within milliseconds.
            if attempt == attempts - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def archive_raw(source_id: str, content: bytes, ext: str) -> Path:
    """The bytes as fetched, named by their own hash. Idempotent."""
    digest = hashlib.sha256(content).hexdigest()
    directory = EXTERNAL_RAW_DIR / source_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.{ext.strip('.') or 'bin'}"
    if not path.exists():
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
    return path


def write_source(bundle: SourceBundle) -> Path:
    """Land one source. Returns its directory. Manifest last (the commit marker)."""
    source_id = bundle.source_id
    with source_lock(source_id):
        directory = source_dir(source_id)
        if bundle.raw_bytes is not None:
            archive_raw(source_id, bundle.raw_bytes, bundle.raw_ext)
        for name, frame in (("series", bundle.series), ("observations", bundle.observations),
                            ("observations_native", bundle.native), ("quality", bundle.quality)):
            _atomic_write(_to_table(frame, SCHEMAS[name]), directory / f"{name}.parquet")
        manifest = pd.DataFrame([bundle.manifest])
        _atomic_write(_to_table(manifest, SCHEMAS["manifest"]), directory / "manifest.parquet")
    return directory


def remove_source(source_id: str) -> bool:
    """Delete a landed source and its raw archive. False if it was not there."""
    if source_id in RESERVED_DIRS:
        raise ValueError(f"{source_id!r} is not a source")
    directory = source_dir(source_id)
    if not directory.exists():
        return False
    with source_lock(source_id):
        for target in (directory, EXTERNAL_RAW_DIR / source_id):
            for attempt in range(6):
                try:
                    shutil.rmtree(target, ignore_errors=False)
                    break
                except FileNotFoundError:
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(0.2 * (attempt + 1))
    return True


# -- reading -----------------------------------------------------------------

def _clean(record: dict) -> dict:
    return {k: (None if (v is None or (isinstance(v, float) and pd.isna(v))) else v) for k, v in record.items()}


def read_manifest(source_id: str) -> Optional[dict]:
    path = source_dir(source_id) / "manifest.parquet"
    if not path.exists():
        return None
    frame = pq.read_table(path).to_pandas()
    if frame.empty:
        return None
    return _clean(frame.iloc[0].to_dict())


def existing(url: str) -> Optional[dict]:
    """The manifest of an already-landed URL, or None."""
    return read_manifest(source_id_for(url))


def is_current(url: str, content_sha256: str) -> bool:
    """True when this URL landed before AND the bytes have not changed since."""
    manifest = existing(url)
    return bool(manifest) and manifest.get("content_sha256") == content_sha256


def read_source(source_id: str) -> Dict[str, pd.DataFrame]:
    """Every file of one source as frames (empty frames when absent)."""
    directory = source_dir(source_id)
    out = {}
    for name, schema in SCHEMAS.items():
        path = directory / f"{name}.parquet"
        out[name] = pq.read_table(path).to_pandas() if path.exists() else schema.empty_table().to_pandas()
    return out


def list_sources() -> pd.DataFrame:
    """One manifest row per landed source, newest first. Reads no database."""
    frames = []
    if EXTERNAL_DIR.exists():
        for child in sorted(EXTERNAL_DIR.iterdir()):
            if child.is_dir() and child.name not in RESERVED_DIRS and (child / "manifest.parquet").exists():
                frames.append(pq.read_table(child / "manifest.parquet").to_pandas())
    if not frames:
        return SCHEMAS["manifest"].empty_table().to_pandas()
    return pd.concat(frames, ignore_index=True).sort_values("fetched_at", ascending=False).reset_index(drop=True)


# -- the build's half ----------------------------------------------------------

def seed() -> None:
    """Zero-row files with the full schema, so every view's glob matches."""
    EXTERNAL_SEED_DIR.mkdir(parents=True, exist_ok=True)
    EXTERNAL_RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name, schema in SCHEMAS.items():
        _atomic_write(schema.empty_table(), EXTERNAL_SEED_DIR / f"{name}.parquet")


def view_globs() -> Dict[str, str]:
    """view name -> absolute Parquet glob (POSIX separators, as DuckDB wants)."""
    root = EXTERNAL_DIR.resolve().as_posix()
    return {view: f"{root}/*/{stem}.parquet" for view, stem in VIEWS.items()}


def create_views(connection) -> None:
    """Called by the build on its read-write connection, after `seed()`."""
    for view, glob in view_globs().items():
        connection.execute(
            f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{glob}', union_by_name=true)")
