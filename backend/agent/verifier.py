"""Checks a finished turn before anything is said about it.

The verifier is the reason the system can claim its answers are trustworthy
rather than merely plausible. It runs after execution and before composition,
so a problem is reported as a caveat the narrative must carry -- or suppresses
the claim entirely -- instead of surfacing as a confidently wrong sentence.

Every check is arithmetic or structural. None of them ask a model anything.
"""
import re
from typing import Any, Dict, List, Optional

from .formatting import format_change, format_quantity
from .state import Session

VALUATION_NOTE = (
    "YP (doviz) stoku TL cinsinden yayimlanir ve kurla carpilarak olusur: kur yukselince TL "
    "karsiligi mekanik olarak artar, bu bir bulgu degildir. Kurla karsilastirma icin TL payi "
    "(TL / toplam, %) ve dolar bazli YP stoku (YP / kur, USD) sutunlari eklendi; yorumu onlar uzerinden yap.")

# A column with more missing periods than this is reported as incomplete: an
# average or a "change since 2021" over a half-empty column is misleading even
# when every individual number in it is right.
MAX_MISSING_SHARE = 0.10


def verify(session: Session) -> Dict[str, Any]:
    """Run every check. Returns a report; never raises."""
    checks: List[Dict[str, Any]] = []
    # The turn's columns, not the conversation's: a coverage warning about a
    # series the previous question fetched is not a caveat this answer should
    # carry. `Session.focus` decided which ones those are.
    artifact = session.view()
    evidence = session.evidence_view()

    def record(name: str, passed: bool, detail: str, severity: str = "error") -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail,
                       "severity": severity if not passed else "info"})

    failed_steps = [a for a in session.audit if not a.ok]
    limitations = session.facts.get("semantic_limitations") or []
    if limitations:
        record("requested_output_supported", False, "; ".join(limitations))
    source_limits = session.facts.get("source_limitations") or []
    if source_limits:
        record("requested_sources_satisfied", False, "; ".join(source_limits))
    role_ids = list((session.facts.get("currency_role_identities") or {}).values())
    if len(role_ids) > 1:
        record("currency_roles_are_distinct", len(set(role_ids)) == len(role_ids),
               "currency role identities must refer to different published series")
    record("all_steps_ran", not failed_steps,
           "; ".join(f"{a.op}: {a.detail}" for a in failed_steps) or "every step completed",
           severity="warning")

    # An analysis the question asked for and no step delivered is a caveat the
    # answer must carry: the composer would otherwise write "no anomalies were
    # found" over a table nobody scored.
    analyses = session.facts.get("analysis") or {}
    ran = {key.partition(":")[0] for key in analyses}
    missing = [m for m in session.facts.get("wants_analysis") or [] if m not in ran]
    record("requested_analyses_ran", not missing,
           ("istenen analiz calismadi: " + ", ".join(missing)) if missing
           else ("analyses: " + ", ".join(sorted(ran)) if ran else "no analysis requested"),
           severity="warning")

    # A chart that could not carry every column it was asked for (a third
    # unit) is a caveat, not a silent omission: the column is in the table,
    # and the answer must say it is not in the picture.
    chart = session.facts.get("chart") or {}
    record("chart_as_requested", not chart.get("note"),
           chart.get("note") or (f"chart kind: {chart.get('kind')}" if chart else "no chart requested"),
           severity="warning")
    record("chart_shows_every_column", not chart.get("dropped"),
           chart.get("dropped_note") or ("chart: " + ", ".join(chart.get("columns") or [])
                                          if chart else "no chart requested"),
           severity="warning")

    if artifact.is_empty():
        cleared = any(a.op == "clear_table" and a.ok for a in session.audit)
        if cleared and session.artifact.is_empty():
            record("table_has_data", True, "table cleared on request")
        elif session.facts.get("intent") == "metadata" and session.facts.get("discovery"):
            # A metadata question is answered by discovery alone; an empty
            # table is the expected shape, not a failure.
            record("table_has_data", True, "metadata turn: discovery only, no table expected")
        else:
            record("table_has_data", False, "no columns were produced")
        return _summarise(checks, session)

    # An analysis quoting a column the table no longer holds is a number
    # nobody can trace.
    orphan_analyses = [key for key, result in analyses.items() if isinstance(result, dict)
                       and any(c not in artifact.frame.columns for c in result.get("inputs") or [])]
    record("analysis_inputs_present", not orphan_analyses,
           f"analyses over missing columns: {orphan_analyses}" if orphan_analyses
           else "every analysis reads columns the table holds")

    record("table_has_data", True, f"{len(artifact.frame)} rows, {len(artifact.frame.columns)} columns")

    # Every column must be describable and quotable, or the composer cannot
    # honestly report it.
    unlabelled = [c for c in artifact.frame.columns
                  if c not in artifact.lineage or not artifact.lineage[c].unit]
    record("every_column_has_a_unit", not unlabelled,
           f"columns without a unit: {unlabelled}" if unlabelled else "all columns carry a unit")

    uncited = [c for c, line in evidence.lineage.items()
               if not line.citation and line.source != "derived"]
    record("every_fetched_column_is_cited", not uncited,
           f"uncited: {uncited}" if uncited else "every fetched column has provenance")

    # A derived column whose parents are gone is a number nobody can explain.
    orphans = [c for c, line in evidence.lineage.items()
               if line.derived_from and any(p not in evidence.frame.columns for p in line.derived_from)]
    record("derived_columns_keep_their_inputs", not orphans,
           f"derived columns whose inputs were dropped: {orphans}" if orphans else "lineage intact")

    empty = [c for c in artifact.frame.columns if artifact.frame[c].dropna().empty]
    record("no_empty_columns", not empty, f"all-null columns: {empty}" if empty else "no empty columns")

    # A quarterly series on the monthly grid is empty eight months in twelve
    # by construction, not by omission: its coverage is judged on the
    # quarter-end months it can publish. Only FinTurk is quarterly today;
    # EVDS quarterly series are already aligned by the build.
    def _missing_share(column: str) -> float:
        values = artifact.frame[column]
        if artifact.lineage[column].source == "finturk":
            values = values[values.index.month.isin((3, 6, 9, 12))]
        return float(values.isna().mean()) if len(values) else 0.0

    incomplete = {c: round(_missing_share(c), 3)
                  for c in artifact.frame.columns if _missing_share(c) > MAX_MISSING_SHARE}
    record("coverage_is_complete", not incomplete,
           f"columns with gaps: {incomplete}" if incomplete else "no material gaps",
           severity="warning")

    # Mixing units in one comparison is the single most likely way this corpus
    # produces a confidently wrong answer -- bin TL and milyon TL differ by 1000x.
    # An exchange rate is priced in TL but is not an amount; it never sums
    # with a balance, so it does not count as a second monetary unit.
    monetary = {c: artifact.lineage[c].unit for c in artifact.frame.columns
                if "TL" in (artifact.lineage[c].unit or "")
                and artifact.lineage[c].temporal_semantics != "rate"}
    distinct = set(monetary.values())
    record("monetary_columns_share_one_unit", len(distinct) <= 1,
           f"mixed monetary units in one table: {monetary}" if len(distinct) > 1
           else f"monetary unit: {distinct or 'none'}")

    # A column from an external file carries a unit and semantics the ingester
    # inferred from a header or caption, not ones the publisher declared. Say
    # so, every time, until a cross-check against the base corpus verifies it.
    unverified = {c: line.unit for c, line in artifact.lineage.items()
                  if c in artifact.frame.columns and line.citation.get("unit_verified") is False}
    record("external_units_are_verified", not unverified,
           (f"birim ve zaman anlami dosyadan okundu, dogrulanmadi: {unverified}" if unverified
            else "no unverified external columns"), severity="warning")

    # Two series can share a unit, sit on the same monthly index, and still be
    # incomparable: the weekly bulletin publishes on Fridays and runs weeks
    # ahead of the monthly one, so a ratio built from one of each pairs
    # observations that were never measured over the same period -- and it
    # returns a confident, plausible, meaningless number.
    cross_grain = {}
    for column, line in evidence.lineage.items():
        if not line.derived_from:
            continue
        grains = {evidence.lineage[parent].grain
                  for parent in line.derived_from
                  if parent in evidence.lineage and evidence.lineage[parent].grain}
        if len(grains) > 1:
            cross_grain[column] = sorted(grains)
    record("derived_columns_share_one_grain", not cross_grain,
           (f"farkli yayin sikliklarindaki seriler birlestirildi: {cross_grain} -- "
            "bu seriler ayni donemleri olcmedigi icin oran/degisim gecersizdir"
            if cross_grain else "no cross-grain derivation"))

    # The same mismatch one step earlier: columns of different grains sitting
    # in one table are not yet wrong, but any comparison drawn between them
    # would be, so the narrative has to say which is which.
    grains_present = {c: artifact.lineage[c].grain for c in artifact.frame.columns
                      if artifact.lineage.get(c) and artifact.lineage[c].grain}
    distinct_grains = set(grains_present.values())
    record("columns_share_one_grain", len(distinct_grains) <= 1,
           (f"tabloda farkli yayin sikliklari var: {grains_present}" if len(distinct_grains) > 1
            else f"grain: {distinct_grains or 'none'}"), severity="warning")

    # A stock differenced and described as "new lending" is a domain error no
    # arithmetic check would catch, so the semantics are surfaced explicitly.
    record("temporal_semantics_declared",
           all(artifact.lineage[c].temporal_semantics for c in artifact.frame.columns),
           ", ".join(f"{c}={artifact.lineage[c].temporal_semantics}" for c in artifact.frame.columns))

    index = artifact.frame.index
    record("periods_are_unique_and_sorted",
           index.is_unique and index.is_monotonic_increasing,
           f"{index.min():%Y-%m}..{index.max():%Y-%m}" if len(index) else "empty index")

    return _summarise(checks, session)


def _summarise(checks: List[Dict[str, Any]], session: Session) -> Dict[str, Any]:
    errors = [c for c in checks if not c["passed"] and c["severity"] == "error"]
    warnings = [c for c in checks if not c["passed"] and c["severity"] == "warning"]
    report = {
        "passed": not errors,
        "n_checks": len(checks),
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "checks": checks,
        "caveats": [c["detail"] for c in errors + warnings],
    }
    session.facts["verification"] = report
    return report


FACT_TABLES = ("bulletin_observations", "weekly_observations", "macro_observations", "finturk_observations",
               "external_observations")

# Tags the narrative cites: K = kaynak (a lakehouse series or an external
# file), H = hesaplama (a transform, find_periods or analyze result over K's),
# U = a URL or web result.
SOURCE_TAG = re.compile(r"\[([KHU]\d+)\]")


def _sql_for(citation: Dict[str, Any]) -> Optional[str]:
    """One statement that reproduces a cited column, runnable as written in
    `scripts/lakehouse_query.py --sql`. That is what makes a tag checkable
    rather than decorative."""
    table = citation.get("table")
    if table not in FACT_TABLES:
        return None
    where = " AND ".join(f"{k} = '{v}'" if isinstance(v, str) else f"{k} = {v}"
                         for k, v in (citation.get("filters") or {}).items())
    for k, v in (citation.get("exclude") or {}).items():
        where += f" AND {k} <> '{v}'"
    if citation.get("period_start") and citation.get("period_end"):
        where += f" AND period BETWEEN '{citation['period_start']}' AND '{citation['period_end']}'"
    value = citation.get("value_column") or "value"
    # macro_observations and external_observations carry no unit column; the
    # unit lives in macro_series / external_series.
    unit = ", unit" if table not in ("macro_observations", "external_observations") else ""
    if citation.get("aggregate"):
        # A FinTurk national figure is a sum over provinces -- there is no
        # published Türkiye row -- so the reproduction groups by period.
        return (f"SELECT period, SUM({value}) AS value{', any_value(unit) AS unit' if unit else ''} "
                f"FROM {table} WHERE {where} GROUP BY period ORDER BY period")
    return f"SELECT period, {value} AS value{unit} FROM {table} WHERE {where} ORDER BY period"


def source_map(session: Session) -> Dict[str, Dict[str, Any]]:
    """Every fact the composer may cite, keyed by a short tag.

    Built from the lineage and the facts the tools already recorded, so a tag
    is a pointer into the audit trail rather than a description the model
    wrote. Each entry carries `label` (what the composer sees), `detail` (what
    the reader is shown) and, for a lakehouse series, `sql` to re-run.
    """
    artifact = session.evidence_view()
    sources: Dict[str, Dict[str, Any]] = {}
    tag_of_column: Dict[str, str] = {}
    counters = {"K": 0, "H": 0, "U": 0}

    def new_tag(kind: str) -> str:
        counters[kind] += 1
        return f"{kind}{counters[kind]}"

    # Fetched columns first, in table order, so a derived column can point at
    # its inputs' tags.
    for name, line in artifact.lineage.items():
        if line.source == "derived" or not line.citation:
            continue
        tag = new_tag("K")
        tag_of_column[name] = tag
        cit = line.citation
        if cit.get("table") in FACT_TABLES:
            filters = ", ".join(f"{k}={v}" for k, v in (cit.get("filters") or {}).items())
            detail = (f"{cit['table']} · {filters} · {cit.get('value_column', 'value')} · "
                      f"{line.unit}, {line.temporal_semantics} · "
                      f"{str(cit.get('period_start'))[:7]}..{str(cit.get('period_end'))[:7]} "
                      f"({cit.get('n_points')} nokta)")
            if cit.get("table") == "external_observations":
                # A landed source: say where the file came from and whether
                # the unit is the lakehouse's word or the ingester's guess.
                verified = cit.get("unit_verified") is True
                detail += (f" · dış kaynak {cit.get('url')} ({cit.get('location')}) · birim "
                           + ("lakehouse ile doğrulandı" if verified else f"doğrulanmadı ({cit.get('unit_source')})"))
        else:  # ingest_external
            detail = (f"dış dosya {cit.get('url')} · sütun={cit.get('value_column')} · "
                      f"{line.unit} (birim doğrulanmadı) · "
                      f"{str(cit.get('period_start'))[:7]}..{str(cit.get('period_end'))[:7]}")
        sources[tag] = {"tag": tag, "kind": "series", "column": name,
                        "label": f"{name} = {line.label} ({line.unit})",
                        "detail": detail, "sql": _sql_for(cit), "citation": cit}

    for name, line in artifact.lineage.items():
        if line.source != "derived":
            continue
        tag = new_tag("H")
        tag_of_column[name] = tag
        inputs = [tag_of_column.get(c, c) for c in line.derived_from]
        sources[tag] = {"tag": tag, "kind": "transform", "column": name,
                        "label": f"{name} = {line.transform or 'türetilmiş'} ({line.unit})",
                        "detail": f"{line.transform or 'türetilmiş'} · girdi: {', '.join(inputs) or '-'} · "
                                  f"{line.unit}, {line.temporal_semantics}",
                        "inputs": inputs}

    for found in session.facts.get("find_periods") or []:
        tag = new_tag("H")
        found["kaynak"] = tag
        col, against = found["column"], found.get("against")
        rule = f"{col} aylık farkı {'<' if found['direction'] == 'down' else '>'} 0"
        if against:
            rule += f" VE {against} aylık farkı {'> 0' if found['against_direction'] == 'up' else '<= 0'}"
        inputs = [tag_of_column.get(c) for c in (col, against) if c and tag_of_column.get(c)]
        sources[tag] = {"tag": tag, "kind": "find_periods",
                        "label": found.get("description", "dönem taraması"),
                        "detail": f"find_periods · kural: {rule} · girdi: {', '.join(inputs) or '-'} · "
                                  f"{found.get('n_column_moves', '?')} aydan {found['n_periods']} ay eşleşti",
                        "inputs": inputs, "periods": [p["period"] for p in found["periods"]]}

    for key, result in (session.facts.get("analysis") or {}).items():
        tag = new_tag("H")
        result = result if isinstance(result, dict) else {}
        result["kaynak"] = tag
        method, _, rest = key.partition(":")
        col, _, against = rest.partition("~")
        inputs = [tag_of_column[c] for c in (result.get("inputs") or [col, against])
                  if c and c in tag_of_column]
        sources[tag] = {"tag": tag, "kind": "analysis",
                        "label": result.get("description") or f"{method} analizi: {col}",
                        "detail": _analysis_detail(method, result, inputs),
                        "inputs": inputs}

    for found in session.facts.get("footnotes") or []:
        tag = new_tag("K")
        found["kaynak"] = tag
        sources[tag] = {"tag": tag, "kind": "footnotes",
                        "label": f"{found['dataset']} tablosunun BDDK dipnotlari ({found['n_notes']})",
                        "detail": f"bulletin_footnotes · dataset={found['dataset']} · {found['n_notes']} not · "
                                  + "; ".join(f"{n['first_period']}..{n['last_period']}" for n in found["notes"]),
                        "sql": found["citation"]["sql"], "citation": found["citation"]}

    for doc in session.facts.get("documents") or []:
        tag = new_tag("U")
        doc["kaynak"] = tag
        sources[tag] = {"tag": tag, "kind": "url", "label": doc.get("title") or doc.get("url"),
                        "detail": f"read_url · {doc.get('url')} ({doc.get('kind')})", "url": doc.get("url")}
    for result in session.facts.get("search") or []:
        for hit in (result.get("results") or [])[:5]:
            tag = new_tag("U")
            hit["kaynak"] = tag
            sources[tag] = {"tag": tag, "kind": "web", "label": hit.get("title") or hit.get("url"),
                            "detail": f"search · {hit.get('url')}", "url": hit.get("url")}

    session.facts["sources"] = sources
    return sources


def _analysis_detail(method: str, result: Dict[str, Any], inputs: List[str]) -> str:
    """The legend line for an analysis tag: parameters and the key numbers,
    so the reader can judge the finding without re-running it."""
    girdi = f"girdi: {', '.join(inputs) or '-'}"
    span = (f"{result.get('period_start')}..{result.get('period_end')}"
            if result.get("period_start") else "")
    if method == "anomaly":
        months = ", ".join(a["period"] for a in (result.get("anomalies") or [])[:6])
        return (f"analyze · anomaly · {result.get('scored_on', '?')} ({result.get('scored_unit', '')}) · "
                f"onceki {result.get('window', '?')} ay baz · |z|>={result.get('z_threshold', '?')} VE "
                f"IQR x{result.get('iqr_multiplier', '?')} · {span} · {girdi} · "
                f"{result.get('n_anomalies', '?')} ay: {months or '-'}")
    if method == "changepoint":
        breaks = "; ".join(
            f"{b['period']} ({b.get('before')} -> {b.get('after')}, {b.get('shift', 0):+} {b.get('shift_unit', '')}, "
            f"{b.get('confidence', '?')}{', cok yeni' if b.get('recent') else ''})"
            for b in (result.get("breaks") or [])[:4])
        return (f"analyze · changepoint · {result.get('kind', '?')} · {result.get('method', 'PELT')} · "
                f"{span} · {girdi} · {result.get('n_breakpoints', '?')} kirilma: {breaks or '-'}"
                + (f" · uyari: {' | '.join(result['warnings'])}" if result.get("warnings") else ""))
    if method == "causality":
        dirs = " · ".join(f"{name} p={d.get('p_value')}" for name, d in (result.get("directions") or {}).items())
        return (f"analyze · causality · Granger, {'fark' if result.get('differenced') else 'seviye'} serileri · "
                f"lag={result.get('lag', '?')} ({result.get('lag_rule', '')}) · {dirs} · "
                f"koint p={result.get('cointegration_p', '-')} · fark korelasyonu {result.get('correlation_diff', '-')} · "
                f"{girdi}")
    if method == "decompose":
        return (f"analyze · decompose · reel = nominal / fiyat endeksi · {span} · "
                f"nominal {result.get('nominal_pct', '?'):+}% · fiyat {result.get('price_pct', '?'):+}% · "
                f"reel {result.get('real_pct', '?'):+}% · {girdi}")
    return f"analyze · method={method} · {girdi}"


def attach_sources(text: str, sources: Dict[str, Dict[str, Any]]) -> str:
    """Drop tags the narrative invented, then append the legend for the ones
    it used -- so every bracket in the answer resolves to a checkable line."""
    used: List[str] = []
    for tag in SOURCE_TAG.findall(text or ""):
        if tag in sources and tag not in used:
            used.append(tag)
    cleaned = SOURCE_TAG.sub(lambda m: m.group(0) if m.group(1) in sources else "", text or "")
    if not used:
        return cleaned
    lines = ["", "Kaynaklar:"]
    for tag in used:
        src = sources[tag]
        lines.append(f"[{tag}] {src['detail']}")
        if src.get("sql"):
            lines.append(f"      SQL: {src['sql']}")
    return cleaned.rstrip() + "\n" + "\n".join(lines)


def quotable_numbers(session: Session) -> Dict[str, Any]:
    """The only numbers the composer is allowed to state.

    Handing the model a computed summary rather than the table is what makes
    "the model never does arithmetic" enforceable rather than aspirational: a
    figure that is not in here did not come from the data.

    Every entry carries `kaynak`, the tag from `source_map`, so the narrative
    can cite where each figure came from.
    """
    artifact = session.view()
    sources = session.facts.get("sources") or source_map(session)
    tag_of_column = {s["column"]: tag for tag, s in sources.items() if s.get("column")}
    # The composer gets each figure as finished text ("3,42 trilyon TL",
    # "%49,83", "+27,51 puan") and never the raw value: the raw 3423006 with
    # unit "milyon TL" came back as "3,42 milyar TL" once, a thousandfold
    # error made in the model's head. Formatting is Python's job.
    series = {}
    for name, stats in artifact.summary().items():
        entry = {k: stats[k] for k in ("label", "unit", "temporal_semantics", "n") if k in stats}
        if stats.get("n"):
            unit = stats["unit"]
            entry["ilk"] = f"{stats['first_period']}: {format_quantity(stats['first_value'], unit)}"
            entry["son"] = f"{stats['last_period']}: {format_quantity(stats['last_value'], unit)}"
            entry["degisim"] = format_change(stats)
            entry["min"] = f"{stats['min_period']}: {format_quantity(stats['min_value'], unit)}"
            entry["max"] = f"{stats['max_period']}: {format_quantity(stats['max_value'], unit)}"
        if name in tag_of_column:
            entry["kaynak"] = tag_of_column[name]
        series[name] = entry
    allowed: Dict[str, Any] = {"series": series}
    sums = {name: {"operation": "sum_columns", "inputs": [
        {"column": p, "label": session.artifact.lineage[p].label,
         "metric": session.artifact.lineage[p].citation.get("filters", {}).get("metric")}
        for p in line.derived_from if p in session.artifact.lineage]}
        for name, line in session.evidence_view().lineage.items()
        if (line.transform or "").startswith("sum_columns(")}
    if sums:
        allowed["group_definitions"] = sums
    raw = [(name, line) for name, line in artifact.lineage.items() if line.source != "derived"]
    if len(raw) == 2 and {line.temporal_semantics for _, line in raw} == {"stock", "flow"}:
        paired = artifact.frame[[name for name, _ in raw]].dropna()
        if len(paired) >= 2:
            changes = paired.iloc[-1] - paired.iloc[0]
            product = changes.iloc[0] * changes.iloc[1]
            movement = "zıt yönlerde" if product < 0 else "aynı yönde" if product > 0 else "en az biri değişmeden"
            labels = "; ".join(f"{name}: " + ("dönem sonu stok (bakiye)" if line.temporal_semantics == "stock"
                                               else "dönem içi işlem miktarı (akım)") for name, line in raw)
            allowed["movement_comparison"] = (
                f"{labels}. Ortak pencerenin ilk ve son gözlemi arasında {movement} hareket ettiler. "
                "Bu karşılaştırma nedensellik göstermez.")
    if session.facts.get("semantic_limitations"):
        allowed["semantic_limitations"] = session.facts["semantic_limitations"]
    valuation = [line for line in artifact.lineage.values()
                 if (line.transform or "").startswith("in_usd(")]
    if valuation:
        allowed["notlar"] = [VALUATION_NOTE]
    if any(line.temporal_semantics == "net_change" for line in artifact.lineage.values()):
        allowed.setdefault("notlar", []).append(
            "Seriler net bakiye değişimidir; brüt giriş veya yeni mevduat değildir. "
            "Önceki ayın bakiyesi yoksa net değişim eksik bırakılır.")
    if session.facts.get("find_periods"):
        allowed["find_periods"] = session.facts["find_periods"]
    chart = session.facts.get("chart") or {}
    if chart.get("kind") == "pie":
        # The shares are computed here, in Python, at one period; the model
        # copies them and never divides.
        allowed["pasta"] = {"donem": chart["period"], "paylar": chart["shares"]}
    if session.facts.get("analysis"):
        allowed["analysis"] = {key: _trim_analysis(result)
                               for key, result in session.facts["analysis"].items()}
    if session.facts.get("footnotes"):
        allowed["footnotes"] = [{k: v for k, v in found.items() if k != "citation"}
                                for found in session.facts["footnotes"]]
    if session.facts.get("documents"):
        allowed["documents"] = [
            {k: v for k, v in doc.items()
             if k in ("kind", "url", "title", "n_pages", "sheet_names", "text", "kaynak")}
            for doc in session.facts["documents"]]
    if session.facts.get("search"):
        allowed["search"] = session.facts["search"]
    if session.facts.get("landed_sources"):
        # What the prompt's own URLs produced: status and series, so the
        # composer can say "the file held three series" without inventing it.
        allowed["landed_sources"] = [
            {k: v for k, v in source.items() if k in ("url", "status", "kind", "n_series", "series_keys", "error")}
            for source in session.facts["landed_sources"]]
    if session.facts.get("discovery"):
        # The span and currencies are what a "hangi seriler var / hangi
        # donemler" question is actually asking; discovery already has them.
        allowed["discovered_keys"] = [
            {k: c.get(k) for k in ("key", "name", "source", "dataset", "unit", "temporal_semantics",
                                   "currencies", "first_period", "last_period", "n_periods")
             if c.get(k) not in (None, [], "")}
            for result in session.facts["discovery"] for c in result["candidates"][:5]]
    return allowed


MAX_ANOMALIES_QUOTED = 6


def _trim_analysis(result: Any) -> Any:
    """What the composer sees of an analysis: the finding, not the audit.

    The citation is already in the source legend, and a long anomaly list
    crowds the other facts out of the composer's 12,000-character budget --
    the six largest by |z| are the ones worth a sentence.
    """
    if not isinstance(result, dict):
        return result
    trimmed = {k: v for k, v in result.items() if k != "citation"}
    anomalies = trimmed.get("anomalies")
    if isinstance(anomalies, list) and len(anomalies) > MAX_ANOMALIES_QUOTED:
        trimmed["anomalies"] = sorted(anomalies, key=lambda a: -abs(a.get("z_score") or 0))[:MAX_ANOMALIES_QUOTED]
        trimmed["n_anomalies_shown"] = MAX_ANOMALIES_QUOTED
    return trimmed


def numbers_in_text(text: str) -> List[float]:
    """Every number a narrative states, for the unsupported-claim check."""
    out = []
    # A minus counts only when it follows nothing word-like: "2021-2025" is a
    # year range and "2023 sonu-2024 başı" a phrase, not the figures -2025
    # and -2024, and flagging them made every answer naming a window look
    # unsupported.
    for token in re.findall(r"(?<![\w])-?\d[\d.,]*", text or ""):
        cleaned = token.rstrip(".,")
        # Turkish notation: 1.234,56 -- thousands grouped with dots, decimal
        # comma. "678.970" therefore means 678970, and reading it as 678.97
        # made every correctly-quoted large figure look unsupported.
        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(".", "").replace(",", ".")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
            cleaned = cleaned.replace(".", "")
        try:
            out.append(float(cleaned))
        except ValueError:
            continue
    return out


def unsupported_numbers(text: str, allowed: Dict[str, Any], tolerance: float = 0.02) -> List[float]:
    """Numbers in the narrative that match nothing the tools computed.

    Years and small integers are ignored -- "2021" and "60 ay" are structure,
    not claims, and so is a bulletin period stamp ("202512", the way the
    brief writes a month). Everything else must appear in the computed facts
    within a tolerance, since a model may round 245.34 to 245.3 legitimately.
    """
    import json as _json

    def harvest(node, into):
        if isinstance(node, dict):
            for value in node.values():
                harvest(value, into)
        elif isinstance(node, list):
            for value in node:
                harvest(value, into)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            into.append(float(node))
        elif isinstance(node, str):
            for token in numbers_in_text(node):
                into.append(token)

    known: List[float] = []
    harvest(_json.loads(_json.dumps(allowed, default=str)), known)

    unsupported = []
    for value in numbers_in_text(text):
        if abs(value) < 100 and float(value).is_integer():
            continue                                   # counts, lags, small integers
        if 1900 <= value <= 2100 and float(value).is_integer():
            continue                                   # years
        if 190001 <= value <= 210012 and float(value).is_integer() and 1 <= value % 100 <= 12:
            continue                                   # YYYYMM period stamps
        # Magnitude only: Turkish prose puts the sign before the percent sign
        # ("-%52,2") or in the verb ("%80,2 daraldı"), so a computed -80.21
        # is quoted as 80.21 and the sign lives in the words, not the number.
        magnitude = abs(value)
        if any(abs(magnitude - abs(k)) <= max(tolerance * abs(k), 0.05) for k in known):
            continue
        unsupported.append(value)
    return unsupported
