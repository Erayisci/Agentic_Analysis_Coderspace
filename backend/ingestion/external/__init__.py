"""Demo-day sources: a URL -> every time series in it -> the lakehouse's external zone.

    url --documents.read_document--> evidence (the extension's sections/rows)
        --tables.tables_from_evidence--> grids
        --tables.series_from_table-----> series with unit / semantics / frequency
        --align.to_monthly-------------> monthly rows (value per monthly_rule)
        --quality-----------------------> non-fatal check rows
        --external_store.write_source--> data/external/<source_id>/*.parquet

The result is visible to every reader of `lakehouse.duckdb` through the
`external_*` views the build created, so `tools.lakehouse.discover` ranks the
new series beside BDDK and EVDS ones and `tools.series.load_series(source=
"external")` fetches them with a citation. Nothing here opens the database.

`ingest_url` is idempotent: the same bytes under the same URL are a cache hit
that writes nothing. A landing page (HTML with links to files) also lands the
top-ranked linked documents, one level deep, each as its own source with
`parent_source_id` set.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from ...extensions.web_tools.asset_common import WARNING as EXTENSION_BOILERPLATE
from ...lakehouse import external_store as store
from .align import to_monthly
from .documents import extraction_route, rank_links, raw_extension, read_document
from .quality import series_checks, source_checks
from .tables import SeriesBundle, series_from_table, tables_from_evidence
from .verify import cross_check, model_labels

MAX_SERIES_PER_SOURCE = 200
MAX_NATIVE_ROWS_PER_SOURCE = 50_000
MAX_CHILDREN = 3


@dataclass
class IngestResult:
    """What one URL produced. `children` are the linked documents a landing page led to."""

    source_id: str
    url: str
    status: str                       # ok | partial | empty | error
    kind: str = "unknown"
    extraction_route: str = "in_process"
    series_keys: List[str] = field(default_factory=list)
    n_series: int = 0
    n_observations: int = 0
    warnings: List[str] = field(default_factory=list)
    cache_hit: bool = False
    error: Optional[str] = None
    children: List["IngestResult"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id, "url": self.url, "status": self.status, "kind": self.kind,
            "extraction_route": self.extraction_route, "series_keys": self.series_keys,
            "n_series": self.n_series, "n_observations": self.n_observations,
            "warnings": self.warnings, "cache_hit": self.cache_hit, "error": self.error,
            "children": [child.to_dict() for child in self.children],
        }

    def all_series_keys(self) -> List[str]:
        keys = list(self.series_keys)
        for child in self.children:
            keys += child.all_series_keys()
        return keys


def ingest_url(url: str, hint: Optional[str] = None, *, force: bool = False, depth: int = 0,
               tools: Optional[dict] = None, follow_links: bool = True,
               parent_source_id: Optional[str] = None, client=None,
               verify_against_lakehouse: bool = True) -> IngestResult:
    """Land one URL. Raises ValueError only when the URL is refused outright
    (SSRF guard, download cap) or the extractor reports an error; a document
    with no usable table lands with status 'empty' and its warnings."""
    url = store.canonical_url(url)
    source_id = store.source_id_for(url)
    evidence = read_document(url, hint=hint or "", tools=tools)
    if evidence.get("status") == "error":
        error = evidence.get("error") or {}
        raise ValueError(f"{url}: {error.get('code', 'error')}: {error.get('message', '')}".strip())

    sha = evidence.get("content_sha256") or ""
    if not force and sha and store.is_current(url, sha):
        manifest = store.existing(url) or {}
        parts = store.read_source(source_id)
        result = IngestResult(
            source_id=source_id, url=url, status=manifest.get("status") or "ok",
            kind=manifest.get("kind") or "unknown", extraction_route=manifest.get("extraction_route") or "",
            series_keys=parts["series"].series_key.tolist(), n_series=int(len(parts["series"])),
            n_observations=int(len(parts["observations"])),
            warnings=[w for w in (manifest.get("warnings") or "").split(" | ") if w], cache_hit=True)
    else:
        result = _land(url, source_id, evidence, hint or "", parent_source_id, client, verify_against_lakehouse)

    if follow_links and depth == 0 and evidence.get("format") == "html":
        for link in rank_links(evidence.get("links") or [], hint or "", url, maximum=MAX_CHILDREN):
            try:
                child = ingest_url(link["url"], hint, force=force, depth=depth + 1, tools=tools,
                                   follow_links=False, parent_source_id=source_id, client=client,
                                   verify_against_lakehouse=verify_against_lakehouse)
            except Exception as exc:                                   # noqa: BLE001 -- one link, not the page
                child = IngestResult(source_id=store.source_id_for(link["url"]), url=link["url"],
                                     status="error", error=f"{type(exc).__name__}: {exc}")
            result.children.append(child)
    return result


def _land(url: str, source_id: str, evidence: dict, hint: str, parent_source_id: Optional[str],
          client=None, verify_against_lakehouse: bool = True) -> IngestResult:
    warnings = [w for w in (evidence.get("warnings") or []) if w != EXTENSION_BOILERPLATE]
    bundles: List[SeriesBundle] = []
    seen: set = set()
    for raw in tables_from_evidence(evidence):
        try:
            bundles.extend(series_from_table(raw, source_id, seen))
        except ValueError as exc:
            warnings.append(f"{raw.location}: {exc}")
    if len(bundles) > MAX_SERIES_PER_SOURCE:
        warnings.append(f"{len(bundles)} series found; only the first {MAX_SERIES_PER_SOURCE} were kept")
        bundles = bundles[:MAX_SERIES_PER_SOURCE]

    # 1. native rows within the budget, and a first monthly alignment.
    kept: List[SeriesBundle] = []
    monthly_by_key: Dict[str, pd.DataFrame] = {}
    native_budget = MAX_NATIVE_ROWS_PER_SOURCE
    for bundle in bundles:
        if native_budget <= 0:
            warnings.append(f"native-row cap of {MAX_NATIVE_ROWS_PER_SOURCE} reached; later series were skipped")
            break
        bundle.native = bundle.native.iloc[:native_budget]
        native_budget -= len(bundle.native)
        kept.append(bundle)
        monthly_by_key[bundle.series_key] = to_monthly(
            bundle.native, bundle.series_key, source_id, bundle.monthly_rule, bundle.native_frequency)

    # 2. let the lakehouse vouch for what it can, then ask the model about the rest.
    findings = {}
    if verify_against_lakehouse:
        try:
            findings = cross_check(kept, monthly_by_key)
        except Exception as error:                                     # noqa: BLE001 -- verification is optional
            warnings.append(f"lakehouse cross-check skipped: {error}")
    if model_labels(client, kept):
        warnings.append("some units/semantics were labelled by the model (semantics_source='model')")
    for bundle in kept:                                                 # a changed rule changes the monthly value
        if monthly_by_key[bundle.series_key].monthly_rule.iloc[0] != bundle.monthly_rule:
            monthly_by_key[bundle.series_key] = to_monthly(
                bundle.native, bundle.series_key, source_id, bundle.monthly_rule, bundle.native_frequency)

    # 3. the rows every view will show.
    series_rows, monthly_frames, native_frames, quality_rows = [], [], [], []
    for bundle in kept:
        native, monthly = bundle.native, monthly_by_key[bundle.series_key]
        finding = findings.get(bundle.series_key) or {}
        verified = finding.get("check") == "matches_lakehouse_series"
        monthly_frames.append(monthly)
        native_frames.append(pd.DataFrame({
            "date": native.index, "series_key": bundle.series_key, "source_id": source_id,
            "value": native.to_numpy(dtype=float), "grain": bundle.native_frequency}))
        series_rows.append({
            "series_key": bundle.series_key, "source_id": source_id, "name": bundle.name,
            "name_clean": bundle.name_clean, "location": bundle.location, "unit": bundle.unit,
            "unit_source": bundle.unit_source, "unit_verified": verified,
            "temporal_semantics": bundle.temporal_semantics, "semantics_source": bundle.semantics_source,
            "native_frequency": bundle.native_frequency, "monthly_rule": bundle.monthly_rule,
            "published_start": native.index.min(), "published_end": native.index.max(),
            "n_native_obs": int(len(native)), "n_periods": int(len(monthly)),
            "matched_lakehouse_key": finding.get("matched_lakehouse_key"),
            "matched_source": finding.get("matched_source"),
            "match_agreement_pct": finding.get("match_agreement_pct"), "url": url,
        })
        quality_rows += series_checks(source_id, bundle.series_key, native, bundle.parse_rate,
                                      bundle.native_frequency, bundle.n_duplicates)
        if finding:
            quality_rows.append({"source_id": source_id, "series_key": bundle.series_key,
                                 "check": finding["check"], "passed": finding["passed"], "detail": finding["detail"]})
            if not finding["passed"]:
                warnings.append(f"{bundle.location}: {finding['detail']}")
        for note in bundle.warnings:
            prefixed = f"{bundle.location}: {note}"
            if prefixed not in warnings:
                warnings.append(prefixed)
    quality_rows += source_checks(source_id, evidence)

    series = pd.DataFrame(series_rows)
    observations = pd.concat(monthly_frames, ignore_index=True) if monthly_frames else pd.DataFrame()
    native = pd.concat(native_frames, ignore_index=True) if native_frames else pd.DataFrame()
    quality = pd.DataFrame(quality_rows)

    extractor_status = evidence.get("status") or "ok"
    status = "empty" if not series_rows else ("partial" if extractor_status == "partial" else "ok")
    manifest = {
        "source_id": source_id, "url": url, "final_url": evidence.get("final_url") or url,
        "parent_source_id": parent_source_id, "kind": evidence.get("kind") or "unknown",
        "content_type": evidence.get("content_type"), "content_sha256": evidence.get("content_sha256"),
        "n_bytes": evidence.get("n_bytes"), "fetched_at": evidence.get("fetched_at") or _now(),
        "title": evidence.get("title"), "hint": hint or None,
        "extraction_route": evidence.get("extraction_route") or extraction_route(),
        "n_series": len(series_rows), "n_observations": int(len(observations)),
        "status": status, "warnings": " | ".join(warnings) if warnings else None,
    }
    store.write_source(store.SourceBundle(
        manifest=manifest, series=series, observations=observations, native=native, quality=quality,
        raw_bytes=evidence.get("_raw_bytes"), raw_ext=raw_extension(evidence)))
    return IngestResult(
        source_id=source_id, url=url, status=status, kind=manifest["kind"],
        extraction_route=manifest["extraction_route"], series_keys=[row["series_key"] for row in series_rows],
        n_series=len(series_rows), n_observations=int(len(observations)), warnings=warnings)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


__all__ = ["IngestResult", "ingest_url", "MAX_SERIES_PER_SOURCE", "MAX_NATIVE_ROWS_PER_SOURCE"]
