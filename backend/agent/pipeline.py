"""Wires interpretation, planning and execution into one turn across sessions.

    route -> semantics -> (pre-ingest URLs) -> plan -> execute -> verify -> compose

A URL in the prompt is landed in the lakehouse's external zone BEFORE the
planner runs (`pre_ingest`). That is what makes demo-day sources automatic:
by the time a plan is written, the file's series are real lakehouse keys the
planner sees in its context, and the no-model fallback fetches them just as
it fetches a BDDK or EVDS series. A URL that yields no series (a prose
document) is still read for the composer through `read_url`.

Written as plain functions rather than a LangGraph graph because the control
flow genuinely is a line with one bounded repair edge. A graph framework buys
persistence and conditional routing that this shape does not need yet; when a
stage grows a real loop, `run_turn` is the function a graph node would wrap.

The planner is given a *context block* rather than a bare question: the
lakehouse keys that discovery already found, and the columns the current table
holds. A 27B model asked to plan without that invents keys; asked to choose
among candidates it was handed, it mostly picks correctly.
"""
import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Generator, List, Optional

from ..core.labels import fold
from ..ingestion.external import IngestResult, ingest_url
from ..lakehouse import external_store
from ..llm import KloudeksClient, LLMError
from ..tools.lakehouse import concept_identity, discover_concepts, extract_currency, extract_sources, split_clauses
from .composer import compose
from .evidence_store import EvidenceStorageError
from .executor import DATASET_SOURCES, Executor, _column_name, _normalise_key, match_column
from .planner import Plan, Step, planner_messages, template_plan
from .router import Route, extract_single_month, extract_urls, route, without_urls
from .semantics import QuerySemantics, explicit_balance_movement, parse_query_semantics
from .state import AuditStep, Session
from .verifier import verify

# Six was too few: a question with several interest-rate clauses filled every
# slot with rate series and the planner never saw the loan series it was asked
# about. Twelve costs ~200 prompt tokens and gives each corpus four seats.
MAX_CANDIDATES_IN_CONTEXT = 12
MAX_LANDED_IN_CONTEXT = 12

logger = logging.getLogger("kkb.agent")


def interpret_query(question: str, session: Session, route_result: Route, client=None) -> QuerySemantics:
    """Structured interaction overrides legacy routing only after a valid parse."""
    view = session.view()
    semantics = parse_query_semantics(question, client, context={
        "has_artifact": session.has_artifact(),
        "columns": {name: {"label": line.label, "semantics": line.temporal_semantics}
                    for name, line in view.lineage.items()},
        "window": view.periods()[:1] + view.periods()[-1:],
        "previous_question": session.turns[-2]["question"] if len(session.turns) > 1 else None,
    })
    if semantics is None:
        # The existing router remains the interaction fallback. Output intent
        # is inferred only from an explicit, unambiguous stock exclusion.
        inferred_movement = explicit_balance_movement(question)
        semantics = QuerySemantics(
            interaction=("presentation_only" if route_result.presentation_only else
                         "extend_previous" if route_result.is_followup else "new_analysis"),
            preserve_existing_window=route_result.is_followup,
            requested_output="absolute_change" if inferred_movement else "unspecified")
        session.facts["semantic_source"] = "fallback_explicit_movement" if inferred_movement else "fallback"
    else:
        session.facts["semantic_source"] = "llm"
        follows = session.has_artifact() and semantics.interaction != "new_analysis"
        route_result.is_followup = follows
        route_result.presentation_only = follows and semantics.interaction == "presentation_only"
        if follows:
            route_result.wants_table = True
            if not route_result.urls:
                route_result.intent = "followup"
        elif route_result.intent == "followup":
            route_result.intent = "series_analysis"
        route_result.decided_by = "semantic"
        route_result.reason = f"semantic interaction={semantics.interaction}; existing_table={session.has_artifact()}"
    session.facts["query_semantics"] = semantics.model_dump()
    logger.info("semantic -> interaction=%s output=%s basis=%s frequency=%s currencies=%s preserve_window=%s",
                semantics.interaction, semantics.requested_output, semantics.requested_basis,
                semantics.frequency, semantics.currencies, semantics.preserve_existing_window)
    return semantics


def pre_ingest(question: str, session: Session, route_result: Route, client=None) -> List[IngestResult]:
    """Land every URL the router found, before planning. One failure costs an
    audit row and a caveat, never the turn; a URL landed on an earlier turn
    (or by another session) is a cache hit that costs nothing."""
    results: List[IngestResult] = []
    for url in route_result.urls:
        started = time.perf_counter()
        try:
            result = ingest_url(url, hint=question, client=client)
        except Exception as exc:                                       # noqa: BLE001 -- one URL, not the turn
            result = IngestResult(source_id=external_store.source_id_for(url), url=url,
                                  status="error", error=f"{type(exc).__name__}: {exc}")
        results.append(result)
        keys = result.all_series_keys()
        detail = result.error or (f"{result.status}: {len(keys)} series in the external zone"
                                  + (" (cache hit)" if result.cache_hit else ""))
        session.audit.append(AuditStep(index=0, op="ingest_source", arguments={"url": url},
                                       ok=result.status != "error", detail=detail[:500],
                                       seconds=time.perf_counter() - started))
        logger.info("  pre-ingest %s -> %s", url, detail[:120])
        if result.status != "error":
            session.cite({"source": "external", "table": "external_sources", "source_id": result.source_id,
                          "url": result.url, "status": result.status, "n_series": result.n_series})
    session.facts["landed_sources"] = [r.to_dict() for r in results]
    return results


def landed_series(results: List[IngestResult], question: str,
                  limit: int = MAX_LANDED_IN_CONTEXT) -> List[Dict[str, Any]]:
    """The series the pre-ingested URLs produced, as discovery-shaped
    candidates, the ones matching the question first."""
    rows: List[Dict[str, Any]] = []
    for result in results:
        for landed in [result] + list(result.children):
            if landed.status == "error" or not landed.series_keys:
                continue
            series = external_store.read_source(landed.source_id)["series"]
            title = (external_store.read_manifest(landed.source_id) or {}).get("title") or ""
            for row in series.itertuples():
                rows.append({"key": row.series_key, "source": "external", "dataset": row.source_id,
                             "name": row.name_clean or row.name, "unit": row.unit,
                             "temporal_semantics": row.temporal_semantics, "location": row.location,
                             "unit_verified": bool(row.unit_verified), "url": landed.url,
                             "source_title": title})
    # Ranked by the question's own words, always: the no-model plan takes the
    # first two, and "toplam altin islem miktari" must reach the TOTAL column
    # of a ten-column PDF rather than whichever column came first. The raw
    # folded words, not `_terms`: the lakehouse stopword list drops "toplam"
    # and "miktari" as filler, and in a file's column names they are the
    # signal. A stable sort keeps the file's order among unnamed rows.
    #
    # The source's own name is part of the haystack because one landing page
    # lands siblings: Borsa Istanbul's gold, silver and platinum reports carry
    # identical column names, so "altin" reaches the right one only through the
    # title -- the text of the link that led to it.
    words = {w for w in re.split(r"[^\w]+", fold(question or "")) if len(w) > 2}
    words |= {_LANDED_SYNONYMS[w] for w in list(words) if w in _LANDED_SYNONYMS}
    for row in rows:
        haystack = fold(f"{row['key']} {row['name']} {row.get('location') or ''} "
                        f"{row.get('source_title') or ''}")
        row["score"] = sum(1 for w in words if w in haystack)
    rows.sort(key=lambda row: -row["score"])
    return rows[:limit]


# A file published in two languages names its columns in both; the question
# uses one. Just the pairs the brief's own sources need.
_LANDED_SYNONYMS = {"toplam": "total", "total": "toplam", "hacim": "volume", "volume": "hacim",
                    "miktar": "amount", "amount": "miktar", "sayisi": "number", "adet": "number"}


def _landed_column_name(key: str) -> str:
    """The readable column for an external series_key '<source_id>/<location>/<name>'."""
    return re.sub(r"[^\w]+", "_", key.rsplit("/", 1)[-1]).strip("_") or "dis_kaynak"


@contextmanager
def _timed(timings: Dict[str, float], stage: str) -> Generator[None, None, None]:
    """Record how long a stage took, in `timings` and in the log.

    The log line is the bottleneck finder: a turn that takes 40s says at INFO
    which of route / plan / execute / verify / compose it spent them in, and
    the executor's own audit trail then says which step.
    """
    started = time.perf_counter()
    try:
        yield
    finally:
        seconds = round(time.perf_counter() - started, 3)
        timings[stage] = seconds
        logger.info("stage %-8s %7.3fs", stage, seconds)


def build_context(question: str, session: Session, route_result: Route,
                  discovery: Optional[Dict[str, Any]] = None,
                  landed: Optional[List[Dict[str, Any]]] = None,
                  semantics: Optional[QuerySemantics] = None) -> str:
    """What the planner needs to know before it can name a key.

    Discovery runs first, deterministically, for exactly this reason: the plan
    then chooses among real keys instead of inventing plausible ones. This is
    the highest-leverage prompt decision in the system. `make_plan` passes the
    discovery it already ran so the dimension guard sees the same candidates.
    """
    blocks: List[str] = []
    if semantics is not None:
        blocks.append("QUERY SEMANTICS (authoritative intent; resolve identities from discovery): "
                      + semantics.model_dump_json() + "\n"
                      "absolute_change on stock amounts requires net_change after any sums; "
                      "percent_change requires change. flow requires a flow source: stock differences "
                      "are not gross inflows. level keeps source levels. unspecified forces no transform.")

    if session.has_artifact() and route_result.is_followup:
        # The table the user is looking at, which is the last turn's columns --
        # not every column the conversation has ever built. The rest are named
        # so a question that does mean one ("NPL sutununu geri getir") can
        # reach it, but they are not presented as the current table.
        view = session.view()
        columns = "\n".join(
            f"  - {name} ({line.unit}, {line.temporal_semantics}) = {line.label}"
            for name, line in view.lineage.items())
        blocks.append(f"MEVCUT TABLO ({len(view.frame)} satir):\n{columns}")
        hidden = [name for name in session.artifact.column_names() if name not in view.frame.columns]
        if hidden:
            blocks.append("ONCEKI SORULARDAN KALAN SUTUNLAR (bu tabloda gosterilmiyor; sadece "
                          "soru acikca isterse kullan): " + ", ".join(hidden))
        blocks.append("Bu bir DEVAM sorusu: mevcut sutunlari KORU, sadece yeni sutun ekle.")
    elif session.has_artifact():
        # A question the router did not read as a follow-up is planned on its
        # own words, whatever the table holds. Shown the previous question's
        # columns as the current table, the model reused them: "konut" from an
        # Istanbul FinTurk question stood in for the national housing-loan
        # line of the next, unrelated, question -- quarterly province data
        # under a monthly national answer. So the table is presented as empty
        # and the leftovers are named only so the model does not invent a
        # column by that name; `apply_scope` enforces the same rule on the plan.
        leftover = ", ".join(session.artifact.column_names())
        blocks.append("MEVCUT TABLO: bos -- bu YENI bir soru. Onceki sorulardan kalan sutunlar "
                      f"({leftover}) bu soru icin KULLANILMAZ: sorunun gerektirdigi HER seriyi "
                      "asagidaki anahtarlardan fetch_series ile getir; column/against alanlarina "
                      "sadece bu planda fetch ettigin sutun adlarini yaz.")
    else:
        blocks.append("MEVCUT TABLO: bos.")

    if landed:
        lines = "\n".join(
            f"  - key={c['key']} | source=external | {c['name']} | {c['unit']}"
            f"{'' if c.get('unit_verified') else ' (tahmin)'} | {c['temporal_semantics']}"
            for c in landed)
        blocks.append("YENI YUKLENEN KAYNAKLAR (prompt'taki URL'den lakehouse'a alindi; fetch_series ile "
                      "source=external ve key AYNEN, dataset/currency BOS):\n" + lines)

    found = discovery if discovery is not None else discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
    if found["candidates"]:
        # Currencies and the period span come from discovery already; showing
        # them is what lets the plan choose a filter the data supports and
        # lets a coverage question be answered without a fetch. A slice the
        # question named ("YP mevduat") is shown as the currency to copy.
        lines = "\n".join(
            f"  - key={c['key']} | source={c['source']}"
            + (f" | dataset={c['dataset']}" if c["source"] in DATASET_SOURCES else "")
            + f" | {c['name']} | {c['unit']} | {c['temporal_semantics']}"
            + (f" | currency={c['currency']} (soruda istenen dilim)" if c.get("currency")
               else (f" | cur={','.join(c['currencies'])}" if c.get("currencies") else ""))
            + (f" | province={c['province']} (soruda istenen il; aynen kopyala)" if c.get("province") else "")
            + (f" | metrics={c['metrics']} (fetch_series metric alani)" if c.get("metrics")
               and c["source"] == "bulletin" else "")
            + (f" | {str(c['first_period'])[:7]}..{str(c['last_period'])[:7]} ({c['n_periods']} donem)"
               if c.get("n_periods") else "")
            for c in found["candidates"])
        blocks.append("BULUNAN ANAHTARLAR (key alanina TAM olarak bunlardan birini yaz):\n" + lines)
    else:
        blocks.append("BULUNAN ANAHTARLAR: yok -- once discover adimi kullan.")

    if route_result.start or route_result.end:
        blocks.append(f"TARIH ARALIGI: start={route_result.start} end={route_result.end}")
    if route_result.wants_analysis:
        hints = {"anomaly": "analyze(method=anomaly, column=ana seri)",
                 "changepoint": "analyze(method=changepoint, column=ana seri)",
                 "causality": "analyze(method=causality, column=hedef, against=aday oncu)",
                 "decompose": "fiyat endeksini fetch et + analyze(method=decompose, column=nominal tutar, "
                              "against=fiyat endeksi)"}
        blocks.append("ANALIZ ISTEGI: " + "; ".join(hints[m] for m in route_result.wants_analysis))
    return "\n\n".join(blocks)


def deterministic_series_plan(question: str, route_result: Route, limit: int = 3,
                              discovery: Optional[Dict[str, Any]] = None,
                              landed: Optional[List[Dict[str, Any]]] = None) -> Plan:
    """A real plan with no model: fetch what discovery ranked highest.

    The bare template (`discover` alone) produces an empty table, which is a
    worse failure than no fallback at all -- it looks like an answer. This
    fetches the top candidate plus the best from each other corpus, which is
    usually the shape of the question anyway: one BDDK series and one macro
    series that contextualises it. A chart is added by `apply_presentation`
    only when the question asked for one. Series the prompt's own URLs
    produced come first: they are what the question is about.
    """
    # The same clause-splitting discovery the planner is shown: plain
    # `discover` on a thirty-word question diluted the one content word that
    # mattered, which is exactly what `discover_concepts` exists to prevent.
    found = discovery if discovery is not None else discover_concepts(question, limit=MAX_CANDIDATES_IN_CONTEXT)
    by_identity = {concept_identity(c): c for c in found["candidates"]}
    chosen, taken = [], set()
    if landed:
        limit += 1          # two landed series plus two from the base corpus

    def take(candidate) -> None:
        identity = concept_identity(candidate)
        if identity not in taken and len(chosen) < limit:
            taken.add(identity)
            chosen.append(candidate)

    for candidate in (landed or [])[:2]:
        take(candidate)
    roles = found.get("currency_roles") or {}
    for candidate in roles.values():
        take(candidate)
    for candidate in sorted(found["candidates"], key=lambda c: -c.get("name_match", 0)):
        if candidate.get("name_match"):
            if any(candidate["source"] == c["source"] and candidate["key"] == c["key"]
                   for c in roles.values()):
                continue  # the requested dataset already supplies this published row
            take(candidate)
    # Each clause's first choice first ("konut kredileri" -> the loan book,
    # "faiz oranlari" -> the rate), then the best of any corpus not yet seen.
    for clause, ranked in zip(found.get("concepts") or [], found.get("by_concept") or []):
        for identity in ranked[:1]:
            if identity in by_identity:
                candidate = by_identity[identity]
                if any(_same_measure_family(c, candidate["source"], candidate.get("dataset"), candidate["key"])
                       for c in roles.values()):
                    continue
                if roles and candidate["source"] in {c["source"] for c in roles.values()}:
                    # A generic clause such as "vade yapısı" may rank an
                    # unrelated "vadeli" instrument. Require a whole named
                    # concept word before adding a second family from the
                    # same corpus; "konut kredisi" still adds housing loans.
                    words = set(re.findall(r"\w+", fold(clause)))
                    named = set(re.findall(r"\w+", fold(
                        f"{candidate['key'].replace('_', ' ')} {candidate.get('name') or ''}")))
                    if not any(len(word) >= 5 and word in named for word in words):
                        continue
                take(candidate)
    for candidate in found["candidates"]:
        if candidate["source"] == "external" and landed:
            continue                    # the landed list already chose the file's series
        if candidate["source"] not in {c["source"] for c in chosen}:
            take(candidate)
    if not chosen:
        return template_plan("metadata", question)

    # A deliberate `as_name` replaces a same-named column outright, and the
    # executor's collision guard only covers the names it infers itself. One
    # landing page lands siblings whose columns are named identically (Borsa
    # Istanbul's gold and silver reports both publish "TOPLAM / TOTAL
    # Miktar/Amount (KG)"), so two chosen series would arrive under one name
    # and the second would silently replace the first -- gold reported as
    # silver, with a citation that agreed. Suffix the way the executor does.
    steps, named = [], set()
    for c in chosen:
        as_name = (_slice_name(c["key"], c["currency"]) if c.get("currency")
                   else _landed_column_name(c["key"]) if c["source"] == "external" else None)
        if as_name is not None:
            candidate, suffix = as_name, 2
            while candidate in named:
                candidate, suffix = f"{as_name}_{suffix}", suffix + 1
            as_name = candidate
            named.add(as_name)
        steps.append(Step(op="fetch_series", key=c["key"], source=c["source"],
                          dataset=c["dataset"] if c["source"] in DATASET_SOURCES else None,
                          currency=c.get("currency"), province=c.get("province"),
                          as_name=as_name))
    return Plan(intent="series_analysis", start=route_result.start, end=route_result.end,
                steps=steps, reasoning="deterministic: top-ranked discovery candidates")


def _slice_name(base: str, currency: str) -> str:
    slug = re.sub(r"[^\w]+", "_", base).strip("_")
    return f"{slug}_{currency.lower()}"


def fetch_identity(step: Step) -> tuple:
    """The complete address, independent of its presentation alias."""
    return (step.source, step.dataset, _normalise_key(step.key), step.metric or "toplam",
            step.currency or "total", step.province)


def _same_measure_family(candidate: dict, source: str, dataset: str, key: str) -> bool:
    """Limit a published-row repair to its catalog family, not its whole source."""
    if source and source != candidate["source"]:
        return False
    if _normalise_key(key) == candidate["key"]:
        return True
    family = (candidate.get("dataset") or "").split("_")[0]
    tokens = set(re.findall(r"\w+", f"{dataset or ''} {key or ''}".replace("_", " ")))
    return bool(family and family in tokens)


def apply_currency_roles(plan: Plan, discovery: dict, session: Session, followup: bool) -> Plan:
    roles = discovery.get("currency_roles") or {}
    if not roles:
        return plan
    by_key = {c["key"]: role for role, c in roles.items()}
    identities = {}
    for step in plan.steps:
        if step.op != "fetch_series":
            continue
        alias_role, _ = extract_currency((step.as_name or "").replace("_", " "))
        previous = session.view().lineage.get(step.as_name) if followup else None
        if previous and previous.source != "derived" and alias_role not in roles:
            filters = previous.citation.get("filters") or {}
            if (step.metric or "toplam") == (filters.get("metric") or "toplam"):
                step.source, step.key = previous.source, previous.key
                step.dataset, step.currency = filters.get("dataset"), filters.get("currency")
        role = alias_role or by_key.get(_normalise_key(step.key)) or step.currency
        candidate = roles.get(role)
        if not candidate or not _same_measure_family(candidate, step.source, step.dataset, step.key):
            continue
        # The published row itself encodes currency; do not apply a second
        # currency filter, or reuse a generic total under a currency alias.
        step.source, step.dataset, step.key = candidate["source"], candidate["dataset"], candidate["key"]
        step.currency = candidate.get("currency")
        identities[role] = fetch_identity(step)[:3] + (step.currency, step.province)
    if len(identities) > 1 and len(set(identities.values())) != len(identities):
        raise ValueError("distinct currency roles resolved to the same series identity")
    session.facts["currency_role_identities"] = identities
    if not followup:
        return plan
    # Reuse an existing total by its address, never overwrite a previous
    # currency total merely because the planner recycled its alias.
    existing = {}
    for name, line in session.view().lineage.items():
        if line.source == "derived":
            continue
        f = line.citation.get("filters") or {}
        existing[(line.source, f.get("dataset"), line.key, f.get("metric") or "toplam",
                  f.get("currency") or "total", f.get("province"))] = name
    aliases, kept = {}, []
    for step in plan.steps:
        if step.op == "fetch_series":
            identity = fetch_identity(step)
            if identity in existing:
                aliases[step.as_name or step.key] = existing[identity]
                continue
            if step.as_name in session.visible_columns:
                base, n = f"{step.as_name}_{step.metric or 'input'}", 2
                name = base
                while name in session.artifact.frame:
                    name, n = f"{base}_{n}", n + 1
                aliases[step.as_name] = name
                step.as_name = name
        for field in ("column", "other_column", "against"):
            value = getattr(step, field, None)
            if value in aliases:
                setattr(step, field, aliases[value])
        if step.columns:
            step.columns = [aliases.get(c, c) for c in step.columns]
        kept.append(step)
    plan.steps = kept
    return plan


def repair_three_month_groups(plan: Plan, question: str, discovery: dict, session: Session) -> Plan:
    """Build a requested three-month split from the fetched published metrics.

    Model aliases can collide even when their fetch addresses differ. Use the
    metric identities to reconstruct the two sums instead of trusting those
    aliases or an invented bucket membership.
    """
    words = fold(question)
    if not re.search(r"\b3\s*ay", words) or "fazla" not in words or "kadar" not in words:
        return plan
    short = ("bir_aya_kadar", "bir_ay_uc_ay")
    long = ("uc_ay_alti_ay", "alti_ay_bir_yil", "bir_yil")
    groups = {}
    for step in plan.steps:
        if step.op == "fetch_series" and step.metric in short + long:
            address = (step.source, step.dataset, _normalise_key(step.key), step.currency, step.province)
            groups.setdefault(address, {})[step.metric] = step
    role, _ = extract_currency(question)
    expected = (discovery.get("currency_roles") or {}).get(role)
    for address, metrics in groups.items():
        if not all(metric in metrics for metric in short + long):
            continue
        if expected and address[:3] != (expected["source"], expected["dataset"], expected["key"]):
            continue
        old_aliases = {metrics[metric].as_name or metrics[metric].key for metric in short + long}
        model_sums = [step for step in plan.steps if step.op == "transform"
                      and step.operation == "sum_columns"
                      and any(name in old_aliases for name in step.columns or [])]
        if len(old_aliases) == len(short + long) and len(model_sums) >= 2:
            continue
        prefix = (role or "vade").lower()
        for metric in short + long:
            metrics[metric].as_name = f"{prefix}_{metric}"
        plan.steps = [step for step in plan.steps if step not in model_sums]
        insert_at = max((i + 1 for i, step in enumerate(plan.steps) if step.op == "fetch_series"), default=0)
        plan.steps[insert_at:insert_at] = [
            Step(op="transform", operation="sum_columns", columns=[f"{prefix}_{m}" for m in bucket],
                 as_name=f"{prefix}_{suffix}")
            for bucket, suffix in ((short, "3aya_kadar"), (long, "3aydan_fazla"))]
        session.facts["maturity_group_repair"] = {"source": address[:3], "short": short, "long": long}
        break
    return plan


def constrain_external_discovery(question: str, found: dict, landed: list) -> dict:
    """A requested document is a source commitment, not a lexical hint.

    Local sources in a mixed request must have their own explicitly named
    source clause. An external acquisition failure cannot open local search.
    """
    allowed = []
    for clause in split_clauses(without_urls(question)):
        sources, _ = extract_sources(clause)
        if not sources:
            continue
        local = discover_concepts(clause, limit=4)
        for c in local["candidates"][:1]:
            if c["source"] in sources and concept_identity(c) not in {concept_identity(x) for x in allowed}:
                allowed.append(c)
    return {**found, "candidates": allowed,
            "by_concept": [[concept_identity(c)] for c in allowed], "currency_roles": {}}


def apply_external_scope(plan: Plan, found: dict, landed: list, session: Session) -> Plan:
    allowed = {(c["source"], c.get("dataset"), c["key"]) for c in found["candidates"] + landed}
    kept, rejected = [], set()
    for step in plan.steps:
        if step.op == "fetch_series":
            matches = [i for i in allowed if i[2] == _normalise_key(step.key)
                       and (not step.source or i[0] == step.source)
                       and (not step.dataset or i[1] == step.dataset)]
            if not matches:
                # Preserve an external role's alias for downstream indexes,
                # but bind it only to an acquired document with matching text.
                replacement = next((c for c in landed if _names_candidate(
                    step.as_name or step.key, {**c, "name": f"{c['name']} {c.get('source_title', '')}"})), None)
                if replacement:
                    step.key, step.source, step.dataset = replacement["key"], "external", None
                    step.currency, step.metric = None, None
                else:
                    rejected.add(step.as_name or step.key)
                    session.facts.setdefault("source_limitations", []).append(
                        f"{step.as_name or step.key}: istenen kaynakla eşleşmedi; yerel ikame kullanılmadı.")
                    continue
        refs = [step.column, step.other_column, step.against] + (step.columns or [])
        if any(ref in rejected for ref in refs if ref):
            if step.as_name:
                rejected.add(step.as_name)
            continue
        kept.append(step)
    if not landed:
        session.facts.setdefault("source_limitations", []).append(
            "İstenen dış kaynaktan sayısal seri alınamadı; sonuç yalnız doğrulanmış yerel verileri içerir.")
    plan.steps = kept
    return plan


def remove_nested_totals(plan: Plan, session: Session, is_followup: bool) -> None:
    """Published totals and demand deposits are separate from maturity buckets."""
    series = {}
    grouped_datasets = set()
    asks_for_demand = bool(session.turns and re.search(r"\bvadesiz\b", fold(session.turns[-1]["question"])))
    if is_followup:
        for name, line in session.view().lineage.items():
            if line.source == "derived":
                continue
            filters = line.citation.get("filters") or {}
            series[name] = ((line.source, filters.get("dataset"), line.key),
                            filters.get("metric") or "toplam")
    for step in plan.steps:
        if step.op == "fetch_series":
            name = _column_name(step, re.sub(r"[^\w]+", "_", step.key or ""))
            series[name] = ((step.source, step.dataset, _normalise_key(step.key)),
                            step.metric or "toplam")
        elif step.op == "transform" and step.operation == "sum_columns" and step.columns:
            resolved = [(name, series.get(match_column(name, list(series)) or "")) for name in step.columns]
            components = {item[0] for _, item in resolved if item and item[1] != "toplam"}
            term_components = {item[0] for _, item in resolved
                               if item and item[1] not in ("toplam", "vadesiz")}
            kept = [name for name, item in resolved if not (
                item and ((item[1] == "toplam" and item[0] in components)
                          or (item[1] == "vadesiz" and item[0] in term_components
                              and not asks_for_demand)))]
            if len(kept) >= 2 and len(kept) < len(step.columns):
                session.facts.setdefault("sum_input_repairs", []).append(
                    {"output": step.as_name, "removed": [name for name in step.columns if name not in kept]})
                step.columns = kept
            grouped_datasets.update((item[0][0], item[0][1]) for _, item in resolved
                                    if item and item[1] not in ("toplam", "vadesiz"))
    if grouped_datasets:
        session.facts["grouped_datasets"] = grouped_datasets


def apply_output_semantics(plan: Plan, semantics: QuerySemantics, session: Session,
                           discovery: Dict[str, Any], is_followup: bool = False) -> Plan:
    """Enforce structured intent using published metadata, never question text.

    Totals/groups are outputs; source buckets remain hidden lineage. Stock
    differences cannot satisfy gross flow requests.
    """
    from ..tools.transforms import RELATIVE_UNITS

    remove_nested_totals(plan, session, is_followup)
    requested = semantics.requested_output
    if requested == "unspecified":
        return plan
    candidates = discovery.get("candidates") or []
    named = {(c["source"], c["key"], c.get("dataset")) for c in candidates
             if c.get("name_match") and c.get("temporal_semantics") == "stock"}
    columns, consumed, aliases = {}, set(), {}
    if is_followup:
        for name, line in session.view().lineage.items():
            filters = line.citation.get("filters") or {}
            columns[name] = dict(semantics=line.temporal_semantics, unit=line.unit, roots=set(),
                                 operation=(line.transform or "").split("(")[0],
                                 source_dataset=(line.source, filters.get("dataset")),
                                 metric=filters.get("metric") or "toplam", was_existing=True)
    kept_steps = []
    for step in plan.steps:
        # Removing an inappropriate difference also repairs its consumers.
        for field in ("column", "other_column", "against"):
            value = getattr(step, field, None)
            if value in aliases:
                setattr(step, field, aliases[value])
        if step.columns:
            step.columns = [aliases.get(c, c) for c in step.columns]
        if step.op == "fetch_series":
            key = _normalise_key(step.key)
            matches = [c for c in candidates if c["key"] == key
                       and (not step.source or step.source == c["source"])
                       and (not step.dataset or step.dataset == c.get("dataset"))]
            if matches:
                c = matches[0]
                name = _column_name(step, re.sub(r"[^\w]+", "_", key))
                step.as_name = name
                columns[name] = dict(semantics=c["temporal_semantics"], unit=c["unit"],
                                     currency_slice=bool(c.get("currencies") and step.currency in ("TL", "FX")),
                                     roots={(c["source"], key, c.get("dataset"))},
                                     source_dataset=(c["source"], c.get("dataset")),
                                     metric=step.metric or "toplam")
        elif step.op == "transform":
            references = step.columns if step.operation == "sum_columns" else [step.column, step.other_column]
            inputs = [match_column(c, list(columns)) for c in references if c]
            if not inputs or any(c is None for c in inputs):
                kept_steps.append(step)
                continue
            first = columns[inputs[0]]
            stock_amount = (first["semantics"] == "stock" and first["unit"]
                            and first["unit"].lower() not in RELATIVE_UNITS)
            if step.operation in ("net_change", "change"):
                default = (f"{inputs[0]}_net_change{step.periods or 1}" if step.operation == "net_change"
                           else f"{inputs[0]}_{'yoy' if step.periods == 12 else f'chg{step.periods or 1}'}_pct")
                old_name = _column_name(step, default)
                if requested in ("level", "flow"):
                    aliases[old_name] = inputs[0]
                    continue
                if requested == "absolute_change" and stock_amount:
                    step.operation = "net_change"
                elif requested == "percent_change":
                    step.operation = "change"
                step.as_name = old_name
            if step.operation != "index_to_base":
                consumed.update(inputs)
            output_semantics = {"net_change": "net_change", "change": "rate", "ratio": "ratio",
                                "index_to_base": "index"}.get(step.operation, first["semantics"])
            if step.operation == "sum_columns":
                output_semantics = first["semantics"] if all(columns[c]["semantics"] == first["semantics"]
                                                            for c in inputs) else None
                default = "_plus_".join(inputs)
            else:
                default = step.as_name or {
                    "index_to_base": f"{inputs[0]}_endeks", "deflate": f"{inputs[0]}_reel",
                    "in_usd": f"{inputs[0]}_usd",
                    "ratio": f"{inputs[0]}_over_{inputs[-1]}",
                }.get(step.operation)
            if default:
                name = _column_name(step, default)
                step.as_name = name
                columns[name] = dict(semantics=output_semantics,
                                     unit="%" if step.operation in ("change", "ratio") else first["unit"],
                                     operation=step.operation,
                                     roots=set().union(*(columns[c]["roots"] for c in inputs)))
        kept_steps.append(step)

    selected_named = named & set().union(*(c["roots"] for c in columns.values())) if columns else set()
    outputs, added, limitations = [], [], []
    for name, meta in list(columns.items()):
        if name in consumed:
            continue
        stock = meta["semantics"] == "stock" and meta["unit"] and meta["unit"].lower() not in RELATIVE_UNITS
        if stock and meta.get("currency_slice") and selected_named and not meta["roots"] & selected_named:
            continue
        operation = None
        if requested == "absolute_change" and stock:
            operation = "net_change"
        elif requested == "percent_change" and meta.get("operation") != "change":
            if meta["unit"] and meta["semantics"] in ("stock", "flow", "rate", "index", "ratio", "net_change"):
                operation = "change"
            else:
                limitations.append(f"{name}: bu kaynağın zaman anlamı yüzde değişim hesabını desteklemiyor.")
                continue
        elif requested == "flow" and meta["semantics"] != "flow":
            limitations.append(
                f"{name}: seçilen kaynak {meta['semantics']} verisi içeriyor; dönem içindeki brüt giriş "
                "veya yeni mevduat doğrudan hesaplanamaz. Net bakiye değişimi brüt akış değildir; "
                "işlem/akış kaynağı gerekir.")
            continue
        if operation:
            suffix_name = "net" if operation == "net_change" else "pct"
            target, suffix = f"{name}_{suffix_name}", 2
            while target in columns or target in outputs:
                target, suffix = f"{name}_{suffix_name}_{suffix}", suffix + 1
            added.append(Step(op="transform", operation=operation, column=name, as_name=target))
            outputs.append(target)
        else:
            outputs.append(name)
    grouped_datasets = session.facts.get("grouped_datasets") or set()
    if is_followup and grouped_datasets:
        outputs = [name for name in outputs if not (
            columns[name].get("was_existing") and columns[name].get("source_dataset") in grouped_datasets
            and columns[name].get("metric") not in (None, "toplam")
            and not columns[name].get("operation"))]
    if requested == "flow" and not columns:
        limitations.append("Akış kaynağı doğrulanamadı; brüt giriş veya yeni mevduat gösterilemiyor.")
    if limitations:
        session.facts["semantic_limitations"] = limitations
    if not columns and not limitations:
        return plan
    charts = [step for step in kept_steps if step.op == "chart"]
    plan.steps = [step for step in kept_steps if step.op != "chart"] + added + charts
    for step in charts:
        step.columns = outputs
    session.facts["output_columns"] = outputs
    session.facts["semantic_status"] = "unsupported" if limitations else "enforced"
    note = "net bakiye degisimi; brut akis degil" if requested == "absolute_change" else requested
    marker = f"[output semantics: {note}]"
    if marker not in (plan.reasoning or ""):
        plan.reasoning = f"{plan.reasoning or ''} {marker}".strip()
    return plan


def apply_dimensions(plan: Plan, discovery: Dict[str, Any]) -> Plan:
    """The currency slice the question named is on the fetch, whatever the plan source.

    Discovery peeled "YP" / "TL" off the concept and tagged the candidates
    that publish that slice with `currency`. This puts it on the plan: a
    fetch of a tagged key with no currency gets the slice (or one step per
    slice, when the question asked for both), and a tagged first-choice key
    the plan never fetched is fetched -- the same guarantee `apply_analysis`
    gives an analysis step. Measured before this existed: the model fetched
    the deposit line as `total` twice from two tables, divided one by the
    other, and reported a 99.97% "share".
    """
    # The province is the FinTurk fact table's slice, and the question names
    # at most one: every FinTurk fetch the plan wrote without it gets it.
    # (Not a search term -- "İstanbul" is in no row's name -- so the model's
    # plan cannot be expected to carry it; discovery peeled it off the concept.)
    provinces = {c["province"] for c in discovery.get("candidates") or []
                 if c.get("source") == "finturk" and c.get("province")}
    if len(provinces) == 1:
        province = next(iter(provinces))
        for step in plan.steps:
            if step.op == "fetch_series" and step.source == "finturk" and not step.province:
                step.province = province

    roles = discovery.get("currency_roles") or {}
    tagged: Dict[tuple, List[str]] = {}
    for candidate in discovery.get("candidates") or []:
        represented = any(_same_measure_family(c, candidate["source"], candidate.get("dataset"), candidate["key"])
                          for c in roles.values())
        if candidate.get("currency") and not represented:
            slices = tagged.setdefault((candidate["source"], candidate["key"], candidate.get("dataset")), [])
            if candidate["currency"] not in slices:
                slices.append(candidate["currency"])
    if not tagged:
        return plan
    first_choices = {(ranked[0][0], ranked[0][1], ranked[0][3])
                     for ranked in discovery.get("by_concept") or [] if ranked}

    steps: List[Step] = []
    fetched: Dict[tuple, List[Step]] = {}
    for step in plan.steps:
        identity = (step.source, _normalise_key(step.key), step.dataset) if step.op == "fetch_series" and step.key else None
        if identity and identity not in tagged and not step.dataset:
            choices = [i for i in tagged if i[:2] == identity[:2]]
            if len(choices) == 1:
                identity = choices[0]
        slices = tagged.get(identity) if identity else None
        if not slices:
            steps.append(step)
            continue
        covered = {s.currency for s in fetched.get(identity, [])}
        base = step.as_name or step.key
        if step.currency in slices:
            wanted = [step.currency]
        else:
            wanted = [s for s in slices if s not in covered] or [slices[0]]
        for i, currency in enumerate(wanted):
            clone = step if i == 0 else step.model_copy()
            clone.currency = currency
            if len(wanted) > 1 or clone.as_name is None:
                clone.as_name = _slice_name(base, currency)
            steps.append(clone)
            fetched.setdefault(identity, []).append(clone)

    # A slice the question named for a first-choice key that the plan never
    # fetched at all.
    for identity, slices in tagged.items():
        if identity not in first_choices or identity in fetched:
            continue
        candidate = next(c for c in discovery["candidates"]
                         if (c["source"], c["key"], c.get("dataset")) == identity)
        insert_at = next((i + 1 for i, s in reversed(list(enumerate(steps))) if s.op == "fetch_series"), 0)
        for currency in slices:
            steps.insert(insert_at, Step(
                op="fetch_series", key=identity[1], source=identity[0],
                dataset=candidate.get("dataset") if identity[0] in DATASET_SOURCES else None,
                currency=currency, as_name=_slice_name(identity[1], currency)))
            insert_at += 1
        plan.reasoning = f"{plan.reasoning or ''} [dimension: {identity[1]} fetched as {','.join(slices)}]".strip()
    plan.steps = steps
    return plan


COLUMN_FIELDS = ("column", "against", "other_column")


def _produced_columns(steps: List[Step]) -> List[str]:
    """The column names the executor will assign to what `steps` produce."""
    names: List[str] = []
    for step in steps:
        if step.op in ("fetch_series", "ingest_external"):
            names.append(_column_name(step, re.sub(r"[^\w]+", "_", step.key or step.value_column or step.op)))
        elif step.op == "transform" and step.as_name:
            names.append(_column_name(step, step.op))
    return names


def _names_candidate(reference: str, candidate: Dict[str, Any]) -> bool:
    """Does a column reference ("konut") name this discovery candidate
    (`tuketici_kredileri_konut`, "Tüketici Kredileri - Konut")?"""
    cleaned = re.sub(r"[^\w]+", "_", reference).strip("_").lower()
    haystack = f"{candidate['key']} {fold(candidate.get('name') or '')}".lower()
    return any(len(word) > 2 and word in haystack for word in cleaned.split("_"))


def apply_scope(plan: Plan, route_result: Route, session: Session, discovery: Dict[str, Any]) -> Plan:
    """A fresh question's plan references only columns it produces itself.

    The session's table outlives the question that built it, and a question
    the router did not read as a follow-up must not lean on it: measured
    live, "İstanbul'daki konut kredilerini il bazinda goster" followed by the
    demo's national housing-loan question produced a plan that fetched only
    the rate and ran `find_periods` against the previous turn's `konut` --
    22 quarterly points of one province under a 60-month national answer, 0
    matching periods, and a FinTurk citation the question never asked for.
    `build_context` no longer shows the model those columns; this makes it a
    guarantee. A reference that resolves to a column this plan produces is
    normalised to it. One that resolves only to a previous question's column
    is a series the model should have fetched: the question's own discovery
    offered it (each clause's first choice the plan does not fetch), so that
    fetch is inserted under the referenced name and the reference stands.
    When discovery offers no matching series, the unresolved reference is
    rejected by the executor's current-turn column scope.
    """
    if route_result.is_followup or not session.has_artifact():
        return plan
    stale = session.artifact.column_names()
    by_identity = {concept_identity(c): c for c in discovery.get("candidates") or []}
    fetched = {(s.source, _normalise_key(s.key)) for s in plan.steps if s.op == "fetch_series" and s.key}
    spare: List[Dict[str, Any]] = []
    for ranked in discovery.get("by_concept") or []:
        for identity in ranked[:1]:
            candidate = by_identity.get(identity)
            if candidate and (candidate["source"], candidate["key"]) not in fetched and candidate not in spare:
                spare.append(candidate)

    steps: List[Step] = []
    notes: List[str] = []
    for step in plan.steps:
        produced = _produced_columns(steps)
        for field in COLUMN_FIELDS:
            reference = getattr(step, field, None)
            if not reference:
                continue
            hit = match_column(reference, produced)
            if hit:
                setattr(step, field, hit)
                continue
            if match_column(reference, stale) is None:
                continue                # names nothing at all; the executor reports it
            pick = next((c for c in spare if _names_candidate(reference, c)), None)
            if pick is None:
                continue
            spare.remove(pick)
            name = re.sub(r"[^\w]+", "_", reference).strip("_") or pick["key"]
            steps.append(Step(op="fetch_series", key=pick["key"], source=pick["source"],
                              dataset=pick.get("dataset") if pick["source"] in DATASET_SOURCES else None,
                              currency=pick.get("currency"), province=pick.get("province"), as_name=name))
            setattr(step, field, name)
            notes.append(f"{reference!r} was a previous question's column; fetched {pick['key']} as {name}")
        if step.op == "chart" and step.columns:
            produced = _produced_columns(steps)
            step.columns = [c for c in (match_column(c, produced) for c in step.columns) if c] or None
        steps.append(step)
    plan.steps = steps
    if notes:
        plan.reasoning = f"{plan.reasoning or ''} [scope: {'; '.join(notes)}]".strip()
    return plan


EXCHANGE_RATE_KEY = re.compile(r"^TP\.DK\.(USD|EUR)\.", re.I)


def apply_valuation_guard(plan: Plan, session: Session, is_followup: bool = True) -> Plan:
    """An FX stock in TL moves with the exchange rate by construction; make the
    comparison the question meant, in Python.

    When a plan fetches a currency slice of a balance-sheet line beside a
    USD/TRY series, this adds -- unasked and model-free -- the other slices of
    that line, the TL share (`ratio` TL/total) and the FX slice in dollars
    (`in_usd`). "USD rose in 42 months and FX deposits rose in all 42" was the
    finding before this existed; it is an identity, not a correlation.
    """
    fetches = [s for s in plan.steps if s.op == "fetch_series" and s.key]
    rate_name = None
    for step in fetches:
        if step.source == "macro" and EXCHANGE_RATE_KEY.match(_normalise_key(step.key)):
            rate_name = _column_name(step, re.sub(r"[^\w]+", "_", step.key))
            break
    # A rate an earlier turn fetched counts only when this turn extends that
    # table; a fresh question stands on what it fetches itself.
    if rate_name is None and is_followup and session.has_artifact():
        rate_name = next((name for name, line in session.artifact.lineage.items()
                          if line.key and EXCHANGE_RATE_KEY.match(line.key)), None)
    if rate_name is None:
        return plan

    sliced = [s for s in fetches if s.source in ("bulletin", "weekly") and s.currency in ("FX", "TL")]
    if not sliced:
        return plan
    existing = set(session.artifact.column_names()) if is_followup and session.has_artifact() else set()
    planned = {_column_name(s, re.sub(r"[^\w]+", "_", s.key or s.op)) for s in plan.steps if s.op != "analyze"}

    entities: List[tuple] = []
    for step in sliced:
        identity = (step.source, _normalise_key(step.key), step.dataset)
        if identity not in entities:
            entities.append(identity)
    added: List[Step] = []
    for source, key, dataset in entities:
        own = [s for s in fetches if (s.source, _normalise_key(s.key), s.dataset) == (source, key, dataset)]
        base = re.sub(r"_(fx|tl|total)$", "", own[0].as_name or re.sub(r"[^\w]+", "_", key))
        names = {s.currency: _column_name(s, re.sub(r"[^\w]+", "_", key)) for s in own}
        for currency in ("TL", "FX", "total"):
            if currency in names:
                continue
            step = Step(op="fetch_series", key=key, source=source, dataset=dataset,
                        currency=currency, as_name=_slice_name(base, currency))
            names[currency] = step.as_name
            added.append(step)
        share, usd = f"{base}_tl_payi", f"{base}_fx_usd"
        if share not in existing | planned:
            added.append(Step(op="transform", operation="ratio", column=names["TL"],
                              other_column=names["total"], as_name=share))
        if usd not in existing | planned:
            added.append(Step(op="transform", operation="in_usd", column=names["FX"],
                              other_column=rate_name, as_name=usd))
    if not added:
        return plan
    insert_at = next((i + 1 for i, s in reversed(list(enumerate(plan.steps))) if s.op == "fetch_series"), 0)
    plan.steps[insert_at:insert_at] = added
    plan.reasoning = f"{plan.reasoning or ''} [valuation guard: TL payi ve USD bazli YP eklendi]".strip()
    return plan


PRICE_INDEX_HINT = re.compile(r"(KFE|GENENDEKS|TUFE|ENDEKS|fiyat)", re.I)
DEFAULT_DEFLATOR = ("TP.GENENDEKS.T1", "tufe")


def _planned_columns(plan: Plan, session: Session, include_existing: bool = True) -> List[Dict[str, Any]]:
    """The columns the table will hold after the plan runs, in order, with
    what is known about each at plan time: existing lineage for the artifact's
    columns (only when the turn extends that table), the step's own fields
    for the ones it is about to fetch."""
    columns: List[Dict[str, Any]] = []
    if include_existing and session.has_artifact():
        for name, line in session.artifact.lineage.items():
            columns.append({"name": name, "source": line.source, "unit": line.unit,
                            "semantics": line.temporal_semantics, "key": line.key or ""})
    for step in plan.steps:
        if step.op in ("fetch_series", "ingest_external"):
            fallback = re.sub(r"[^\w]+", "_", step.key or step.value_column or step.op)
            columns.append({"name": _column_name(step, fallback), "source": step.source or "external",
                            "unit": step.unit or "", "semantics": "", "key": step.key or ""})
    return columns


def _looks_like_price_index(column: Dict[str, Any]) -> bool:
    return column["semantics"] == "index" or bool(
        PRICE_INDEX_HINT.search(f"{column['name']} {column['key']}"))


def _looks_monetary(column: Dict[str, Any]) -> bool:
    return "TL" in column["unit"] or (column["source"] == "bulletin" and not column["unit"])


def apply_analysis(plan: Plan, route_result: Route, session: Session) -> Plan:
    """Every analysis the question asked for has a step in the plan.

    The router reads the request from the question's own words; this appends
    the `analyze` step when the plan lacks it, mirroring `apply_presentation`
    for charts. Column names are the ones the executor will assign, computed
    with its own `_column_name`, so the step resolves at run time. A wrong
    guess costs one step (the executor's `against` fallback and the verifier's
    input check make it visible), never the turn.
    """
    have = {step.method for step in plan.steps if step.op == "analyze"}
    for method in route_result.wants_analysis:
        if method in have:
            continue
        # A fresh question's analysis runs on what it fetched, not on the
        # first column a previous question left in the table.
        columns = _planned_columns(plan, session, include_existing=route_result.is_followup)
        if not columns:
            logger.info("apply_analysis: no columns to run %s on; skipped", method)
            continue
        if method in ("anomaly", "changepoint"):
            plan.steps.append(Step(op="analyze", method=method, column=columns[0]["name"]))
        elif method == "causality":
            if len(columns) < 2:
                logger.info("apply_analysis: causality needs two columns, have %d; skipped", len(columns))
                continue
            plan.steps.append(Step(op="analyze", method="causality",
                                   column=columns[0]["name"], against=columns[1]["name"]))
        elif method == "decompose":
            nominal = next((c for c in columns if _looks_monetary(c) and not _looks_like_price_index(c)),
                           columns[0])
            price = next((c for c in columns if _looks_like_price_index(c) and c is not nominal), None)
            if price is None:
                # A decomposition needs a price index; CPI is the canonical one
                # and the question cannot be answered without it.
                key, name = DEFAULT_DEFLATOR
                insert_at = next((i + 1 for i, s in reversed(list(enumerate(plan.steps)))
                                  if s.op in ("fetch_series", "ingest_external")), 0)
                plan.steps.insert(insert_at, Step(op="fetch_series", key=key, source="macro", as_name=name))
                plan.reasoning = f"{plan.reasoning or ''} [decompose: {key} fetched as deflator]".strip()
                price = {"name": name}
            plan.steps.append(Step(op="analyze", method="decompose",
                                   column=nominal["name"], against=price["name"]))
        have.add(method)
    if route_result.wants_footnotes and not any(step.op == "footnotes" for step in plan.steps):
        plan.steps.append(Step(op="footnotes"))
    return plan


DATA_PRODUCING_OPS = ("fetch_series", "transform", "ingest_external", "ingest_source")


def apply_presentation(plan: Plan, route_result: Route, question: str,
                       session: Optional[Session] = None) -> Plan:
    """A chart step exists in the plan iff the question asked for a chart.

    Enforced here, after planning, so it holds for every plan source alike --
    the model (which the prompt also tells, but a prompt is a request and this
    is a guarantee), the deterministic series plan and the templates. When a
    chart *was* asked for and no step draws one, one is appended over whatever
    the plan fetched -- or, when the plan fetches nothing but the session
    already holds a table, over that table. Measured before this took the
    session into account: "bunun grafiğini çiz" after a table question had
    `wants_chart=True`, the model returned exactly `[{"op": "chart"}]`, this
    function stripped it, found no fetch to hang a chart on, and the turn
    ended with an empty plan and `figure: None`. The gate exists to stop a
    chart nobody asked for, not one the user just asked for by name.
    """
    named = [step for step in plan.steps if step.op == "chart"]
    plan.steps = [step for step in plan.steps if step.op != "chart"]
    if not route_result.wants_chart or (session is not None and session.facts.get("output_columns") == []):
        return plan
    has_data = any(step.op in DATA_PRODUCING_OPS for step in plan.steps)
    # Only a FOLLOW-UP may chart the table it extends: a fresh question whose
    # series was not found must not come back with a chart of whatever the
    # previous question left on screen, dressed up as its answer.
    has_table = (session is not None and session.has_artifact() and route_result.is_followup
                 and not any(step.op == "clear_table" for step in plan.steps))
    if has_data or has_table:
        # Keep the columns/title the model named, if it named any.
        chart = named[0] if named else Step(op="chart")
        if session is not None and session.facts.get("output_columns") is not None:
            chart.columns = session.facts["output_columns"]
        # No title from the question: "bunun grafigini ciz" says nothing
        # about what is drawn. Left empty, the executor titles the chart
        # from the series it actually holds and the period it covers.
        # A single month named in a follow-up ("2024 Haziran pastasi"): the
        # pie's snapshot period, carried on the step rather than as a plan
        # window that would cut the table down to one row.
        month = extract_single_month(question)
        if month and not chart.base_period:
            chart.base_period = month
        plan.steps.append(chart)
    return plan


def make_plan(question: str, session: Session, route_result: Route,
              client: Optional[KloudeksClient],
              ingested: Optional[List[IngestResult]] = None,
              semantics: Optional[QuerySemantics] = None) -> Plan:
    """A validated plan: from the model when possible, from a template otherwise.

    Discovery runs once here and feeds three consumers -- the planner's
    context, the deterministic fallback and `apply_dimensions` -- so they
    cannot disagree about which candidates carry which currency slice.
    `ingested` is what `pre_ingest` landed from the prompt's URLs: a question
    whose file produced series is a series question, whatever the router
    called it, and the plan must reach those series.
    """
    # Discovery and the landed ranking read the question's words; the URL in it
    # is an address the router already carries on `route_result.urls`. See
    # `router.without_urls` for what its path words cost when they rank as
    # search terms. The model still sees the question as the user wrote it.
    semantics = semantics if semantics is not None else interpret_query(question, session, route_result, client)
    concepts = without_urls(question)
    landed = landed_series(ingested or [], concepts)
    series_intent = route_result.intent in ("series_analysis", "followup") or bool(landed)

    # "bunun grafiğini çiz" / "tablo yap" over an existing table: there is
    # nothing to plan. No discovery (its words name no series -- and the
    # stem of "tabloyu" matched the EVDS money-supply code TP.HPBITABLO1,
    # which is how a chart request once fetched M1), no model round trip.
    # `apply_presentation` adds the chart step when one was asked for; an
    # empty follow-up plan re-presents the table as it stands.
    if route_result.presentation_only and session.has_artifact():
        return Plan(intent="followup", steps=[],
                    reasoning="presentation-only follow-up: re-present the current table")

    # "tabloyu temizle" / "bastan basla" is an op with exactly one meaning,
    # so it does not wait for a model: the same words empty the table in
    # both modes. With a new question attached, the clear runs first and
    # the rest is planned below; alone, it is the whole plan.
    if route_result.wants_clear and route_result.is_followup:
        return Plan(intent="followup", steps=[Step(op="clear_table")],
                    reasoning="deterministic: explicit clear request")

    found = (discover_concepts(concepts, limit=MAX_CANDIDATES_IN_CONTEXT,
                               requested_basis=semantics.requested_basis, frequency=semantics.frequency)
             if client is not None or series_intent else {"candidates": [], "by_concept": []})
    if route_result.urls:
        found = constrain_external_discovery(question, found, landed)

    def fallback() -> Plan:
        if series_intent:
            return apply_dimensions(
                deterministic_series_plan(concepts, route_result, discovery=found, landed=landed), found)
        return template_plan(route_result.intent, question, route_result.urls,
                             route_result.start, route_result.end)

    plan: Optional[Plan] = None
    if client is not None:
        try:
            plan = client.structured(
                planner_messages(question, build_context(question, session, route_result,
                                                         discovery=found, landed=landed, semantics=semantics)),
                Plan, max_tokens=1400)
            session.facts["raw_model_plan"] = plan.model_dump(exclude_none=True)
            session.facts["plan_source"] = "llm"
        except LLMError:
            plan = None
    if plan is None:
        # The deterministic plan goes through the SAME tail as a model plan
        # below. It used to `return` here, which skipped the follow-up window
        # inheritance: in modelless mode "bu tabloyu bozmadan ..." fetched the
        # new column unwindowed (67 months) and the outer join stretched the
        # 60-row table it was told not to disturb.
        plan = fallback()
        session.facts["plan_source"] = "deterministic"
    else:
        # A plan that only discovers is a *valid* plan but a dead end -- measured
        # live, a model handed a fresh question sometimes emits just `discover`
        # and stops rather than committing to the fetch it just found candidates
        # for. Rule 1 in the prompt ("don't invent a key") makes this the *safe*
        # failure, but a safe non-answer is still not an answer: the deterministic
        # plan used for an unreachable model recovers a real table here too. A
        # follow-up over an existing table is not a dead end and is left alone --
        # but a table a *previous* question built does not rescue a fresh one.
        extends_table = session.has_artifact() and (route_result.is_followup or plan.intent == "followup")
        if (series_intent and not extends_table
                and not any(step.op in DATA_PRODUCING_OPS for step in plan.steps)):
            plan = deterministic_series_plan(question, route_result, discovery=found, landed=landed)
            session.facts["plan_source"] = "deterministic"
            plan.reasoning = f"{plan.reasoning} [model plan produced no data; replaced]"
        plan = apply_currency_roles(plan, found, session, route_result.is_followup)
        plan = apply_dimensions(plan, found)
        plan = apply_scope(plan, route_result, session, found)

    plan = apply_currency_roles(plan, found, session, route_result.is_followup)
    plan = repair_three_month_groups(plan, question, found, session)
    if route_result.urls:
        plan = apply_external_scope(plan, found, landed, session)

    if route_result.wants_clear and not any(step.op == "clear_table" for step in plan.steps):
        plan.steps.insert(0, Step(op="clear_table"))
        plan.reasoning = f"{plan.reasoning or ''} [clear_table prepended: explicit clear request]".strip()

    # The router's regex read of the date range beats the model's: it is exact,
    # and a plan that silently drops the window returns 67 months for a
    # question that asked for 60.
    if route_result.start:
        plan.start = route_result.start
    if route_result.end:
        plan.end = route_result.end

    # A follow-up inherits the window of the table it extends. "Bu tabloyu hic
    # bozmadan" states no dates, so without this the new column arrives with its
    # own full history and the outer join stretches the table from 60 rows to 67
    # -- disturbing precisely what the question said not to disturb.
    if semantics.preserve_existing_window and session.has_artifact():
        # The window of the table on screen, which a narrower focus may have
        # shortened -- "bozmadan" protects what the last turn showed.
        index = session.view().frame.index
        if len(index):
            plan.start = route_result.start or index.min().strftime("%Y-%m-%d")
            plan.end = route_result.end or index.max().strftime("%Y-%m-%d")
    # A URL that produced no series is a document to read, not a table to
    # join: give the composer its text. A URL that did produce series is
    # already in the plan's reach as external keys and needs no read step.
    landed_urls = {r.url for r in (ingested or []) if r.all_series_keys()}
    prose_urls = [url for url in route_result.urls
                  if external_store.canonical_url(url) not in landed_urls]
    if prose_urls and not any(step.op == "read_url" for step in plan.steps):
        plan.steps = template_plan("url_analysis", question, prose_urls).steps + plan.steps
    metadata = {**found, "candidates": found["candidates"] + landed}
    return apply_output_semantics(plan, semantics, session, metadata, route_result.is_followup)


def _table_payload(session: Session) -> Dict[str, Any]:
    """The table as this turn presents it: `Session.focus`'s columns only.

    The session's artifact is unchanged and still holds everything -- a later
    follow-up can still reach a column this turn did not show.
    """
    view = session.view()
    return {"columns": view.column_names(), "units": view.units(), "rows": view.to_records(),
            "all_columns": session.artifact.column_names()}


def _run_turn(question: str, session: Optional[Session] = None,
              client: Optional[KloudeksClient] = None,
              url_reader=None, web_search=None,
              compose_answer: bool = True, research_runner=None, mode: str = "auto",
              on_tool_result=None) -> Dict[str, Any]:
    """One question through the whole pipeline. Returns the API payload."""
    session = session or Session()
    session.start_turn(question)
    timings: Dict[str, float] = {}
    turn_started = time.perf_counter()
    logger.info("turn start: %.120s", question)

    with _timed(timings, "route"):
        if mode == "research":
            # The website's "Web araştırması" mode: the bounded research loop
            # decides its own searches and reads; nothing here plans.
            route_result = Route(intent="search", urls=extract_urls(question),
                                 reason="website web research mode")
        else:
            route_result = route(question, has_artifact=session.has_artifact(), client=client)
    with _timed(timings, "semantic"):
        semantics = interpret_query(question, session, route_result, client)
    if mode == "research":
        route_result.intent = "search"  # an explicit UI mode still selects its runner
    logger.info("route -> %s (%s) chart=%s table=%s", route_result.intent,
                route_result.decided_by, route_result.wants_chart, route_result.wants_table)
    if route_result.intent == "search" and research_runner is not None:
        return _research_turn(question, session, route_result, research_runner, on_tool_result, timings)
    if mode == "research":
        raise ValueError("Web research is disabled; enable WEB_TOOLS_ENABLED and WEB_AGENT_ENABLED.")

    # A URL in the question lands in the lakehouse's external zone first, so
    # the planner chooses among real keys -- the demo-day rule (see module doc).
    ingested: List[IngestResult] = []
    if route_result.urls:
        with _timed(timings, "ingest"):
            ingested = pre_ingest(question, session, route_result, client)

    # `plan` includes discovery (`build_context`) and the planner's model call;
    # `KloudeksClient` logs each call separately, so the two are separable.
    with _timed(timings, "plan"):
        plan = make_plan(question, session, route_result, client, ingested, semantics)
        plan = apply_valuation_guard(plan, session, is_followup=route_result.is_followup)
        plan = apply_analysis(plan, route_result, session)
        plan = apply_presentation(plan, route_result, question, session)
    raw = session.facts.get("raw_model_plan") or {}
    final_steps = [s.model_dump(exclude_none=True) for s in plan.steps]
    session.facts["plan_diagnostics"] = {
        "plan_source": session.facts.get("plan_source", "deterministic"),
        "raw_model_reasoning": raw.get("reasoning"),
        "repairs_applied": final_steps != raw.get("steps", []),
        "added_or_changed_steps": [s for s in final_steps if s not in raw.get("steps", [])],
        "removed_or_replaced_steps": [s for s in raw.get("steps", []) if s not in final_steps],
    }
    plan.reasoning = "Effective plan: " + ", ".join(
        f"{s.op}:{s.operation or s.key or s.method or ''}" for s in plan.steps)
    logger.info("plan -> %s", [step.op + (f":{step.method}" if step.method else "") for step in plan.steps])
    session.facts["intent"] = plan.intent
    session.facts["wants_analysis"] = list(route_result.wants_analysis)
    session.facts["is_followup"] = route_result.is_followup

    with _timed(timings, "execute"):
        Executor(session, url_reader=url_reader, web_search=web_search).run(plan)
    for step in session.audit:
        logger.info("  step %d %-16s %7.3fs %s %s", step.index, step.op, step.seconds,
                    "ok " if step.ok else "ERR", str(step.detail)[:100])

    # What this turn is about, before anything is checked or said about it.
    # The artifact keeps every column the conversation has built; the answer,
    # the checks and the table shown all narrow to the columns this turn
    # touched (plus the previous turn's, when the question said to keep them).
    visible = session.focus(keep_previous=route_result.is_followup)
    hidden = [c for c in session.artifact.column_names() if c not in visible]
    logger.info("focus -> %s%s", visible, f" (hidden: {hidden})" if hidden else "")

    with _timed(timings, "verify"):
        verification = verify(session)

    # Read by the composer, so an answer over an unrequested table does not
    # say "as the table shows".
    presentation = {"table": route_result.wants_table, "chart": route_result.wants_chart}
    session.facts["presentation"] = {"tablo": presentation["table"], "grafik": presentation["chart"]}

    with _timed(timings, "compose"):
        answer = compose(session, question, client) if compose_answer else {
            "summary": "", "composed_by": "skipped", "unsupported_numbers": [], "caveats": [],
            "sources": []}

    timings["total"] = round(time.perf_counter() - turn_started, 3)
    logger.info("turn done in %.3fs: %s", timings["total"],
                " ".join(f"{k}={v:.3f}" for k, v in timings.items() if k != "total"))

    return {
        "question": question,
        "semantics": semantics.model_dump(),
        "semantic_status": session.facts.get("semantic_status"),
        "route": route_result.model_dump(),
        "plan": plan.model_dump(exclude_none=True),
        "plan_diagnostics": session.facts.get("plan_diagnostics"),
        "summary": answer["summary"],
        "composed_by": answer["composed_by"],
        "unsupported_numbers": answer["unsupported_numbers"],
        "sources": answer["sources"],
        "presentation": presentation,
        "table": _table_payload(session),
        "figure": session.facts.get("figure") if presentation["chart"] else None,
        "analysis": session.facts.get("analysis"),
        "find_periods": session.facts.get("find_periods"),
        "landed_sources": session.facts.get("landed_sources"),
        "citations": session.turn_citations(),
        "verification": verification,
        "audit": [step.to_dict() for step in session.audit],
        "timings": timings,
        "session": session,
    }


def _research_turn(question: str, session: Session, route_result: Route, runner, on_tool_result,
                   timings: Dict[str, float]) -> Dict[str, Any]:
    """A turn answered by the web-tools extension's bounded research loop:
    the model chooses searches and reads, cites sources by id, and every tool
    result is recorded through `on_tool_result` before the model sees it. The
    table is left as it stands; the verification here is provenance and
    coverage only, never the truth of the prose."""
    with _timed(timings, "research"):
        result = runner(question, urls=route_result.urls, on_tool_result=on_tool_result)
    sources = result.get("sources", [])
    citations = [{**s, "source": "web", "cited": s["id"] in result.get("citations", [])} for s in sources]
    caveats = list(result.get("warnings", [])) + list(result.get("missing_information", []))
    if result.get("error"):
        caveats.append(result["error"].get("message", result["error"].get("code", "Research failed")))
    checks = [{"check": "research_completed", "passed": result["status"] == "ok",
               "severity": "error" if result["status"] == "error" else "warning",
               "detail": result.get("stop_reason") or result["status"]}]
    timings["total"] = round(sum(timings.values()), 3)
    return {
        "question": question, "route": route_result.model_dump(),
        "plan": {"intent": "search", "reasoning": "bounded model-selected research tools"},
        "summary": result.get("answer") or "Araştırma tamamlanamadı. Kaynaklar ve araç hatalarını inceleyin.",
        "composed_by": "llm" if result.get("answer") else "unavailable",
        "unsupported_numbers": [], "sources": [],
        "presentation": {"table": False, "chart": False},
        # The table as it stands: a research turn touches no column, so the
        # turn-scoped view would be empty while the conversation's table is not.
        "table": {"columns": session.artifact.column_names(), "units": session.artifact.units(),
                  "rows": session.artifact.to_records(), "all_columns": session.artifact.column_names()},
        "figure": None, "analysis": None, "find_periods": None, "landed_sources": None,
        "citations": citations,
        "verification": {"scope": "web_provenance", "passed": result["status"] == "ok", "n_checks": 1,
                         "n_errors": int(result["status"] == "error"),
                         "n_warnings": len(caveats), "checks": checks, "caveats": caveats},
        "audit": [{"index": i + 1, "op": t["tool"], "arguments": t["arguments"],
                   "ok": t["status"] != "error", "detail": str(t.get("error") or t["status"])}
                  for i, t in enumerate(result.get("trace", []))],
        "research": {k: v for k, v in result.items() if k != "evidence"},
        "timings": timings,
        "session": session,
    }


def run_turn(question: str, session: Optional[Session] = None,
             client: Optional[KloudeksClient] = None, url_reader=None, web_search=None,
             compose_answer: bool = True, research_runner=None, evidence_store=None,
             mode: str = "auto") -> Dict[str, Any]:
    """`_run_turn` with every web tool result saved before it is used.

    With an `evidence_store` (backend.agent.evidence_store), each read_url /
    web_search / research tool call is committed to `data/research.duckdb`
    under this turn's run before the model sees it, and the finished payload
    is linked to the run; the payload's `evidence` says so. Without one this is
    exactly `_run_turn`. A storage failure raises rather than letting a turn
    report evidence it did not keep.
    """
    session = session or Session()
    run_id = evidence_store.start(session.session_id, question, mode) if evidence_store else None

    def record(name, arguments, output):
        if evidence_store:
            evidence_store.record(run_id, name, arguments, output)

    def recorded(tool, name, argument):
        if tool is None:
            return None

        def call(value):
            try:
                result = tool(value)
            except EvidenceStorageError:
                raise
            except Exception as exc:
                record(name, {argument: value}, {"status": "error", "error": {"code": type(exc).__name__}})
                raise
            record(name, {argument: value}, result)
            return result
        return call

    try:
        result = _run_turn(question, session, client=client,
                           url_reader=recorded(url_reader, "read_url", "url"),
                           web_search=recorded(web_search, "search_web", "query"),
                           compose_answer=compose_answer, research_runner=research_runner,
                           mode=mode, on_tool_result=record if evidence_store else None)
        if evidence_store:
            status = (result.get("research") or {}).get("status") or (
                "partial" if (any(not step["ok"] for step in result["audit"])
                              or not result["verification"]["passed"]) else "ok")
            result["evidence"] = evidence_store.finish(
                run_id, {k: v for k, v in result.items() if k != "session"}, status)
        return result
    except EvidenceStorageError:
        raise
    except Exception as exc:
        if evidence_store:
            evidence_store.finish(run_id, {"error": type(exc).__name__}, "error")
        raise


class Agent:
    """A conversation: one Session per session_id, reused across turns."""

    def __init__(self, client: Optional[KloudeksClient] = None, url_reader=None, web_search=None,
                 research_runner=None, evidence_store=None):
        self.client = client
        self.url_reader = url_reader
        self.web_search = web_search
        self.research_runner = research_runner
        self.evidence_store = evidence_store
        self.sessions: Dict[str, Session] = {}

    def session(self, session_id: str) -> Session:
        if session_id not in self.sessions:
            self.sessions[session_id] = Session(session_id=session_id)
        return self.sessions[session_id]

    def ask(self, question: str, session_id: str = "default", **kwargs) -> Dict[str, Any]:
        return run_turn(question, self.session(session_id), client=self.client,
                        url_reader=self.url_reader, web_search=self.web_search,
                        research_runner=self.research_runner, evidence_store=self.evidence_store, **kwargs)
