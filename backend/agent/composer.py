"""Writes the answer, over numbers it is forbidden to compute.

The composer receives `quotable_numbers(session)` -- a dict of figures the
tools already calculated -- and the verifier's caveats. It does not receive the
table. That is the mechanism, not a stylistic choice: a model that cannot see
the raw series cannot average it incorrectly, and any figure in the narrative
that is not in the facts dict is detectable as unsupported.

After composition, `unsupported_numbers` re-reads the text and flags anything
that matches nothing the tools produced. A flagged answer is returned with the
warning attached rather than silently trusted.
"""
import json
from typing import Any, Dict, List, Optional

from ..llm import KloudeksClient, LLMError
from .state import Session
from .verifier import quotable_numbers, unsupported_numbers

COMPOSER_SYSTEM = """Sen bir finansal analistsin. Turkce, net ve profesyonel yaziyorsun.

KESIN KURALLAR:
1. SADECE sana verilen "facts" icindeki sayilari kullan. Yeni sayi HESAPLAMA, TAHMIN ETME, UYDURMA.
2. Her sayinin BIRIMINI yaz (milyon TL, %, endeks, adet).
3. Bir seri 'stock' ise bu bir DONEM SONU STOGUDUR; "kullandirilan kredi" veya "yeni kredi" DEME.
   Aylik degisim net bakiye degisimidir (yeni kullandirim eksi geri odemeler).
4. 'cumulative_ytd' ise yil basindan itibaren birikimlidir.
5. Granger sonucu ONGORULEBILIRLIKTIR; "neden oldu" DEME.
6. caveats listesindeki uyarilari cevapta belirt.
7. Kisa yaz: 2-4 paragraf. Once dogrudan cevap, sonra gerekce.
"""


def compose(session: Session, question: str, client: Optional[KloudeksClient] = None,
            think: bool = False, max_tokens: int = 900) -> Dict[str, Any]:
    """Return {'summary', 'unsupported_numbers', 'facts_used'} for one turn."""
    facts = quotable_numbers(session)
    verification = session.facts.get("verification", {})
    caveats: List[str] = verification.get("caveats", [])

    if client is None:
        return {"summary": deterministic_summary(session, question), "composed_by": "template",
                "unsupported_numbers": [], "caveats": caveats}

    payload = {
        "soru": question,
        "facts": facts,
        "tablo_sutunlari": session.artifact.units(),
        "caveats": caveats,
    }
    messages = [
        {"role": "system", "content": COMPOSER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:12000]},
    ]
    try:
        summary = client.chat(messages, max_tokens=max_tokens, think=think).strip()
        composed_by = "llm"
    except LLMError as exc:
        # An unreachable model must not lose the analysis: the numbers are all
        # computed already, so fall back to stating them plainly.
        summary = deterministic_summary(session, question)
        composed_by = f"template (model unavailable: {exc})"

    flagged = unsupported_numbers(summary, facts)
    if flagged:
        summary += ("\n\n_Not: bu cevaptaki bazi sayilar hesaplanan verilerle eslesmedi "
                    f"({', '.join(str(n) for n in flagged[:5])}); lutfen tabloyu esas alin._")
    return {"summary": summary, "composed_by": composed_by,
            "unsupported_numbers": flagged, "caveats": caveats}


def deterministic_summary(session: Session, question: str) -> str:
    """The answer with no model at all: every computed figure, stated plainly.

    Not a graceful degradation so much as the floor the system guarantees --
    the numbers and their units are already known before any prose is written.
    """
    artifact = session.artifact
    if artifact.is_empty():
        failures = "; ".join(a.detail for a in session.audit if not a.ok)
        return f"Tablo olusturulamadi. {failures or 'Veri bulunamadi.'}"

    lines = [f"{len(artifact.frame)} donem, {len(artifact.frame.columns)} seri "
             f"({artifact.periods()[0][:7]} .. {artifact.periods()[-1][:7]}):", ""]
    for name, stats in artifact.summary().items():
        if not stats.get("n"):
            continue
        change = f", degisim %{stats['change_pct']:+.1f}" if stats.get("change_pct") is not None else ""
        lines.append(
            f"- {stats['label']} ({stats['unit']}, {stats['temporal_semantics']}): "
            f"{stats['first_period']} {stats['first_value']:,.2f} -> "
            f"{stats['last_period']} {stats['last_value']:,.2f}{change}")

    for found in session.facts.get("find_periods", []):
        months = ", ".join(p["period"] for p in found["periods"][:8])
        lines += ["", f"{found['column']} '{found['direction']}' yonunde hareket ettigi ve "
                      f"{found.get('against', '-')} beklendigi gibi davranmadigi "
                      f"{found['n_periods']} donem: {months or '-'}"]

    for key, analysis in session.facts.get("analysis", {}).items():
        if not key.startswith("causality:"):
            continue

        cause = analysis.get("cause", "?")
        effect = analysis.get("effect", "?")
        forward = analysis.get("forward", {})
        reverse = analysis.get("reverse", {})
        lag = analysis.get("lag_selection", {}).get("selected_lag")
        classification = analysis.get("classification", "unknown")

        lines += [
            "",
            (
                f"Nedensellik analizi ({cause} -> {effect}): "
                f"Granger ileri yon p={forward.get('p_value')}, "
                f"ters yon p={reverse.get('p_value')}, "
                f"secili gecikme={lag}, "
                f"siniflandirma={classification}."
            ),
            (
                "Bu sonuc nedensellik kaniti degil, "
                "ongorulebilirlik/predictive precedence gostergesidir."
            ),
        ]

    caveats = session.facts.get("verification", {}).get("caveats", [])
    if caveats:
        lines += ["", "Uyarilar: " + "; ".join(caveats[:3])]
    return "\n".join(lines)
