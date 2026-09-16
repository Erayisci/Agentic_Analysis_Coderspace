"""The Lakehouse tool: discovery, series fetch, and a guarded SQL escape hatch.

Discovery is the tool that matters most. A 27B model asked "konut kredileri"
cannot be expected to know that housing loans live in `tuketici_kredileri` and
not in the sectoral table, that the row is keyed `tuketici_kredileri_konut`,
that it publishes three currencies, or that the interest rate is an EVDS series
in a different table entirely. `discover` answers all of that from the three
index tables the build maintains -- `bulletin_entities`, `weekly_items` and
`macro_series` -- so the planner names a concept and gets back keys.

Search runs on the ASCII-slugified key as well as the published name, because
Turkish case folding breaks the obvious approach: 'I'.lower() is not 'i', so an
ILIKE on a name containing 'İ' misses. A small alias table bridges the other
gap -- a question in English, or using a word the regulator does not ("mortgage",
"NPL", "enflasyon") -- because the corpus is published in Turkish only.
"""
import re
import time
from typing import Any, Dict, List, Optional

import duckdb

from ..core.config import DUCKDB_PATH
from .series import SeriesResult, load_series

# Read-only surface the agent is allowed to query. `observations` and the TBB
# tables are omitted on purpose: they are a second vocabulary for the same BDDK
# table 05, in a different unit, and letting a model choose between them is how
# a 1000x error reaches an answer. Cross-source questions go through
# reconciliation_monitor, which is here for exactly that.
ALLOWED_TABLES = {
    "bulletin_entities", "bulletin_observations", "bulletin_metrics", "bulletin_footnotes",
    "weekly_items", "weekly_observations", "macro_series", "macro_observations",
    "macro_observations_native", "data_quality_report", "reconciliation_monitor",
    "bulletin_lifecycle_report", "weekly_lifecycle_report",
}

FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|copy|export|install|load|pragma|set)\b", re.I)

# Words a question is likely to use that the corpus never publishes. Deliberately
# short and hand-checked: every entry was verified to resolve to a real key.
ALIASES: Dict[str, List[str]] = {
    "mortgage": ["konut"], "housing": ["konut"], "housing loan": ["konut"],
    "home loan": ["konut"], "konut kredisi": ["konut"],
    "vehicle": ["tasit"], "car loan": ["tasit"], "auto": ["tasit"],
    "personal loan": ["ihtiyac"], "consumer": ["tuketici"], "consumer loan": ["tuketici"],
    "credit card": ["kredi_kartlari"], "kredi karti": ["kredi_kartlari"],
    "npl": ["takipteki"], "non-performing": ["takipteki"], "bad loan": ["takipteki"],
    "takipteki kredi": ["takipteki"], "batik": ["takipteki"],
    "deposit": ["mevduat"], "savings": ["mevduat"],
    "capital adequacy": ["sermaye_yeterliligi"], "car ratio": ["sermaye_yeterliligi"],
    "profit": ["kar_zarar"], "earnings": ["kar_zarar"], "net income": ["donem_net_kari"],
    "inflation": ["TUFE", "GENENDEKS"], "enflasyon": ["TUFE", "GENENDEKS"], "cpi": ["GENENDEKS"],
    "tufe": ["GENENDEKS"], "tuketici fiyat": ["GENENDEKS"],
    "house price": ["KFE", "Konut Fiyat"], "konut fiyat": ["KFE"], "hpi": ["KFE"],
    "interest rate": ["faiz", "KTF"], "faiz orani": ["faiz", "KTF"],
    "faiz oranlari": ["faiz", "KTF"], "faizi": ["faiz", "KTF"], "faiz": ["KTF", "faiz"],
    # APIFON4 specifically: EVDS3 publishes no policy-rate series, and the
    # weighted average funding cost is the registered proxy. The group also
    # holds funding AMOUNTS, so the bare group prefix is not specific enough.
    "policy rate": ["APIFON4"], "politika faizi": ["APIFON4"],
    "funding cost": ["APIFON4"], "fonlama maliyeti": ["APIFON4"],
    "exchange rate": ["DK.USD", "kur"], "doviz kuru": ["DK.USD"], "usd": ["DK.USD"],
    "house sales": ["KONUTSAT"], "konut satis": ["KONUTSAT"],
    "mortgaged sales": ["AKONUTSAT2", "IPOTEKLI"], "ipotekli": ["IPOTEKLI", "AKONUTSAT2"],
    "unemployment": ["issizlik"], "gdp": ["GSYIH"], "growth": ["GSYIH"],
}


def _connect():
    if not DUCKDB_PATH.exists():
        raise FileNotFoundError(f"{DUCKDB_PATH} not found -- run `python -m backend.lakehouse.build` first")
    return duckdb.connect(str(DUCKDB_PATH), read_only=True)


# Generic words that match hundreds of rows and mean nothing on their own.
# "faiz" is here because almost every bulletin table has an interest accrual
# line; it stays useful as an alias expansion, just not as a term of its own.
# A qualifier narrows a line to a sub-case. "Konut kredileri" means the loan
# book; "takipteki konut kredileri" is a different, much smaller series that
# happens to contain the first as a substring, so matching alone ranks it
# first. Penalise a qualifier the question did not ask for.
QUALIFIERS = {
    "takipteki": ("takipteki", "npl", "non-performing", "batik", "sorunlu"),
    "dovize_endeksli": ("dovize", "endeksli", "fx-indexed"),
    "reeskont": ("reeskont", "accrual"),
    "bilgi": ("bilgi",),
    "verilen_faizler": ("verilen", "odenen", "gider"),
    "alinan_faizler": ("alinan", "gelir"),
}

STOPWORDS = {
    # English boilerplate
    "the", "and", "for", "with", "show", "give", "what", "which", "how", "over",
    "between", "please", "monthly", "data", "chart", "table", "also", "using",
    # Turkish question boilerplate. A demo question is a sentence, not a keyword:
    # without these, "gosteriniz" and "dagilimini" score as loudly as "konut" and
    # the series the question is actually about falls out of the candidate list.
    "total", "toplam", "olarak", "icin", "için", "nedir", "nasil", "nasıl",
    "grafik", "tablo", "aylik", "aylık", "veri", "veriler", "veriden", "turkiye",
    "türkiye", "turkey", "goster", "göster", "gosteriniz", "gösteriniz",
    "arasinda", "arasında", "arasindaki", "arasındaki", "yillari", "yılları",
    "ayrica", "ayrıca", "lutfen", "lütfen", "kullanilan", "kullanılan",
    "dagilimini", "dağılımını", "dagilim", "dağılım", "buna", "bunu", "bunlar",
    "misin", "misiniz", "musun", "eder", "edebilir", "getir", "getirebilir",
    "donem", "dönem", "donemlerde", "dönemlerde", "miktari", "miktarı",
    "degisim", "değişim", "gostermis", "göstermiş", "olmus", "olmuş",
    "yukselmedigi", "yükselmediği", "dustugu", "düştüğü", "halde", "sadece",
    "yeni", "sutun", "sütun", "hangi", "yapabilir", "verilerini", "kullanarak",
}


def _terms(query: str):
    """Weighted search terms: (term, weight).

    Three rules, each earned by a ranking failure:

    - The full phrase is a strong signal, *unless* the query is a single word --
      then the "phrase" is just the word, and scoring it twice let a balance-sheet
      line called "Odenmis Sermaye Enflasyon Duzeltme Farki" outrank the CPI index
      for the query "enflasyon".
    - Aliases match on word boundaries, not substrings, so "faiz" does not fire
      inside "politika faizi".
    - When two alias phrases overlap, the longer one wins and the shorter is
      dropped. "politika faizi" maps to the funding-cost proxy; without this,
      the "faizi" alias fired alongside it and pulled up interest-rate swaps.
    """
    lowered = query.lower().strip()
    single_word = len(lowered.split()) == 1
    weighted = {lowered: 1.5 if single_word else 4.0}

    consumed = []
    for phrase in sorted(ALIASES, key=len, reverse=True):
        match = re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", lowered)
        if not match:
            continue
        if any(match.start() < end and start < match.end() for start, end in consumed):
            continue                                    # a longer alias already claimed these words
        consumed.append(match.span())
        for expansion in ALIASES[phrase]:
            key = expansion.lower()
            if key == phrase.lower():
                # A self-map ("faiz" -> "faiz") adds no vocabulary; it only
                # triples the weight of a word the question already used, which
                # is how one incidental "faiz" buried the loan series the
                # question was actually about. Let it score as a plain word.
                continue
            weighted[key] = max(weighted.get(key, 0), 3.0)

    for word in re.split(r"[^\w\u00c0-\u024f]+", lowered):
        if len(word) > 3 and word not in STOPWORDS and not word.isdigit():
            weighted.setdefault(word, 1.0)
            # Turkish is agglutinative: "kredilerinin" and "kredileri" are the
            # same concept as "kredi", and an exact-substring search finds
            # neither in a key spelled "tuketici_kredileri_konut". A five-character
            # prefix is a crude stemmer, but it costs nothing and recovers the
            # match that matters.
            if len(word) > 6:
                weighted.setdefault(word[:5], 0.8)
    return sorted(weighted.items(), key=lambda pair: -pair[1])[:18]


def _score(candidate, terms) -> float:
    """Rank a candidate against the weighted terms.

    Beyond term matching, two structural preferences encode what a question
    usually means: a top-level row beats a sub-item of it (asking about housing
    loans means the line, not its FX-indexed sub-row), and a shorter key is the
    more canonical one. EVDS tier 0 is boosted because those series were
    registered precisely as the ones a demo question needs, and province-level
    codes are pushed down -- "konut satislari" means Turkiye unless a city is named.
    """
    key = str(candidate.get("key") or "").lower()
    name = str(candidate.get("name") or "").lower()
    score = 0.0
    for term, weight in terms:
        if term in key:
            score += weight * 2.0
            if key == term or key.startswith(term):
                score += weight
        if term in name:
            score += weight * 1.5
    if not score:
        return 0.0

    query_text = terms[0][0] if terms else ""
    for qualifier, asked_words in QUALIFIERS.items():
        present = qualifier in key or qualifier.replace("_", " ") in name
        asked = any(word in query_text for word in asked_words)
        if present and not asked:
            score -= 6.0

    # sektorel_kredi_dagilimi is the one table published in bin TL rather than
    # milyon TL, and it breaks credit down by the borrower's activity sector. A
    # product question ("konut kredileri") is nearly always answered by the
    # consumer-loan table instead, and mixing the two is a 1000x error.
    if candidate.get("dataset") == "sektorel_kredi_dagilimi" and "sekt" not in query_text:
        score -= 3.0

    # "Konut kredileri" is an amount; "konut satislari" is a count. Both match
    # the word "konut", and EVDS publishes the sales count under a name that
    # contains it. Asking for credit and being handed a unit of "adet" is the
    # error this prevents.
    if candidate.get("unit") == "adet" and re.search(r"\bkredi|\bloan|\btutar|\bbakiye|\bstok", query_text):
        score -= 4.0

    score -= 1.5 * key.count("/")                       # a child row, not the line itself
    score -= min(len(key), 90) / 45.0                   # prefer the canonical short key

    if candidate.get("source") == "macro":
        tier = candidate.get("tier")
        score += 3.0 if tier == 0 else (1.0 if tier == 1 else 0.0)
        raw_key = str(candidate.get("key") or "")
        # EVDS repeats a code per province. "Konut satislari" means Turkiye
        # unless a city is named, and TOPLAM is how TCMB spells the national row.
        # EVDS publishes several national variants under one name -- KTRTOPLAM
        # beside KMA and MA (moving-average forms). Demote the variants rather
        # than promoting the canonical row: a bonus on "TOPLAM" is not confined
        # to the tie it was meant to break, and twice let house SALES outrank
        # the housing-loan rate for "mortgage interest rate".
        if re.search(r"\.K?MA$", raw_key):
            score -= 2.0
        elif "TOPLAM" not in raw_key.upper() and re.search(r"\.(KTR|TR)[A-Z0-9]", raw_key):
            score -= 6.0
        if raw_key.startswith("DERIVED."):
            score += 2.0                                # built at build time for exactly this question
    return round(score, 3)


CLAUSE_SPLIT = re.compile(
    r"[.,;?!]|\bbuna ek olarak\b|\bayrica\b|\bayrıca\b|\bve\b|\bile\b|\bda\b|\bde\b", re.I)


def discover_concepts(question: str, per_concept: int = 3, limit: int = 8):
    """Discovery over a whole question, by splitting it into concepts first.

    A demo question is a paragraph -- "...konut kredilerinin dagilimini aylik
    olarak gosteriniz. Buna ek olarak konut kredisi faiz oranlarini da
    gosteriniz..." -- and scoring it as one string dilutes every content word
    among thirty filler ones. No weighting fixes that: the sentence genuinely
    contains two questions. Splitting on clause boundaries and searching each
    piece recovers both, which is what the planner needs to see.
    """
    chunks = [chunk.strip() for chunk in CLAUSE_SPLIT.split(question or "") if chunk.strip()]
    chunks = [chunk for chunk in chunks
              if any(len(word) > 3 and word.lower() not in STOPWORDS
                     for word in re.split(r"[^\w\u00c0-\u024f]+", chunk))]
    if not chunks:
        chunks = [question]

    merged, seen = [], set()
    for chunk in chunks[:6]:
        for candidate in discover(chunk, limit=per_concept)["candidates"]:
            identity = (candidate["source"], candidate["key"])
            if identity not in seen:
                seen.add(identity)
                merged.append(candidate)
    merged.sort(key=lambda c: -c["score"])

    # The same guarantee `discover` makes per query, applied across the merge:
    # one loud clause ("faiz oranlarini") otherwise fills every slot and the
    # BDDK series the other clause asked for never reaches the planner.
    kept, per_source = [], {}
    for candidate in merged:
        if per_source.get(candidate["source"], 0) < max(2, limit // 3):
            kept.append(candidate)
            per_source[candidate["source"]] = per_source.get(candidate["source"], 0) + 1
    for candidate in merged:
        if len(kept) >= limit:
            break
        if candidate not in kept:
            kept.append(candidate)
    kept = sorted(kept[:limit], key=lambda c: -c["score"])

    return {"query": question, "n_concepts": len(chunks), "concepts": chunks[:6],
            "n_candidates": len(kept), "candidates": kept}


def discover(query: str, source=None, limit: int = 8):
    """Find the lakehouse keys that answer a natural-language concept.

    The candidate pool is every row that matches any term -- not a truncated
    slice of them. An earlier version took an unordered `LIMIT 60` from the
    database and scored that, which meant the correct series was often never
    scored at all and the ranking looked mysteriously unstable. The three index
    tables hold 2,235 rows between them, so scoring all matches in Python costs
    nothing and is the only way the ranking means anything.

    Returns candidates ranked by relevance, each carrying everything the next
    step needs: the key, the unit, the temporal semantics, and which currencies
    and metrics the series actually publishes. A planner that reads this cannot
    invent a filter the data does not support.
    """
    terms = _terms(query)
    if not terms:
        return {"query": query, "terms_used": [], "n_candidates": 0, "candidates": []}

    con = _connect()
    try:
        pooled = []
        wanted = {source} if source else {"bulletin", "macro", "weekly"}
        words = [term for term, _ in terms]

        if "bulletin" in wanted:
            where = " OR ".join(["entity_key ILIKE ? OR entity_name ILIKE ?"] * len(words))
            params = [p for term in words for p in (f"%{term}%", f"%{term}%")]
            pooled += con.execute(
                "SELECT 'bulletin' AS source, dataset, entity_key AS key, entity_name AS name, unit, "
                "temporal_semantics, currencies, metrics, NULL AS tier, n_periods, "
                "first_period::VARCHAR AS first_period, last_period::VARCHAR AS last_period "
                f"FROM bulletin_entities WHERE {where}", params).df().to_dict("records")

        if "macro" in wanted:
            where = " OR ".join(["series_code ILIKE ? OR name_tr ILIKE ? OR coalesce(name_en,'') ILIKE ?"] * len(words))
            params = [p for term in words for p in (f"%{term}%", f"%{term}%", f"%{term}%")]
            pooled += con.execute(
                "SELECT 'macro' AS source, datagroup AS dataset, series_code AS key, name_tr AS name, unit, "
                "temporal_semantics, NULL AS currencies, monthly_rule AS metrics, tier, NULL AS n_periods, "
                "published_start AS first_period, published_end AS last_period "
                f"FROM macro_series WHERE {where}", params).df().to_dict("records")

        if "weekly" in wanted:
            where = " OR ".join(["entity_name ILIKE ?"] * len(words))
            pooled += con.execute(
                "SELECT 'weekly' AS source, dataset, entity_key AS key, entity_name AS name, "
                "'milyon TL' AS unit, 'stock' AS temporal_semantics, 'TL,FX,total' AS currencies, "
                "NULL AS metrics, NULL AS tier, NULL AS n_periods, NULL AS first_period, NULL AS last_period "
                f"FROM weekly_items WHERE retired_on IS NULL AND ({where})",
                [f"%{term}%" for term in words]).df().to_dict("records")
    finally:
        con.close()

    for candidate in pooled:
        for field_name, value in list(candidate.items()):
            if hasattr(value, "item"):
                candidate[field_name] = value.item()
            elif value is not None and str(value) == "nan":
                candidate[field_name] = None
        candidate["score"] = _score(candidate, terms)

    scored = sorted([c for c in pooled if c["score"] > 0], key=lambda c: -c["score"])

    # Guarantee every corpus a seat. A question about loan volumes and interest
    # rates matches dozens of EVDS rate series, and a pure top-k list handed the
    # planner eight macro candidates and not one BDDK loan row -- so it planned
    # against house SALES. The planner can only choose among what it is shown.
    ranked, per_source = [], {}
    for candidate in scored:
        source_count = per_source.get(candidate["source"], 0)
        if source_count < max(2, limit // 3):
            ranked.append(candidate)
            per_source[candidate["source"]] = source_count + 1
    for candidate in scored:
        if len(ranked) >= limit:
            break
        if candidate not in ranked:
            ranked.append(candidate)
    ranked = sorted(ranked[:limit], key=lambda c: -c["score"])

    return {"query": query, "terms_used": [t for t, _ in terms],
            "n_candidates": len(ranked), "candidates": ranked}


def fetch_series(
    key: str,
    source: str = "bulletin",
    dataset: Optional[str] = None,
    currency: Optional[str] = "total",
    metric: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
    **kwargs,
) -> SeriesResult:
    """One series with its unit and semantics attached. Thin by design: the
    domain rules live in `series.load_series` so every tool inherits them."""
    return load_series(key, source=source, dataset=dataset, currency=currency,
                       metric=metric, start=start, end=end, **kwargs)


def run_sql(sql: str, limit: int = 200) -> Dict[str, Any]:
    """The escape hatch, not the main road.

    Read-only connection, single statement, allowlisted tables, enforced LIMIT.
    A model that writes its own SQL against this corpus will eventually compare
    `bin TL` to `milyon TL`; the typed steps exist so it rarely has to.
    """
    statement = sql.strip().rstrip(";")
    if not statement.lower().lstrip("(").startswith(("select", "with")):
        raise ValueError("only SELECT / WITH statements are allowed")
    if ";" in statement:
        raise ValueError("only a single statement is allowed")
    if FORBIDDEN_SQL.search(statement):
        raise ValueError("statement contains a write or session-modifying keyword")

    referenced = set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)", statement, re.I))
    unknown = {name for name in referenced if name.lower() not in ALLOWED_TABLES}
    if unknown:
        raise ValueError(f"table(s) not available to the agent: {sorted(unknown)}; "
                         f"allowed: {sorted(ALLOWED_TABLES)}")

    started = time.perf_counter()
    con = _connect()
    try:
        frame = con.execute(f"SELECT * FROM ({statement}) LIMIT {int(limit)}").df()
    finally:
        con.close()
    return {
        "sql": statement,
        "n_rows": int(len(frame)),
        "columns": [str(c) for c in frame.columns],
        "rows": frame.astype(object).where(frame.notna(), None).to_dict("records"),
        "seconds": round(time.perf_counter() - started, 3),
        "citation": {"table": sorted(referenced), "sql": statement, "n_rows": int(len(frame))},
    }
