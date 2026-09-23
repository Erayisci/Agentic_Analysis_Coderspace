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

from ..core.config import KLOUDEKS_FAST_MODEL
from ..llm import KloudeksClient, LLMError
from .formatting import format_change, format_quantity
from .state import Session
from .verifier import VALUATION_NOTE, attach_sources, quotable_numbers, source_map, unsupported_numbers

COMPOSER_SYSTEM = """Sen bir finansal analistsin. Turkce, net ve profesyonel yaziyorsun.
Vade gruplarinin kapsami yalniz facts.group_definitions inputs listesidir.
Bu listede olmayan bir kalemi (ornegin toplam satiri) gruba dahil diye anlatma.
Kaynak satir adi ayni olsa da metric alanlari farkli vade kovalaridir.

KESIN KURALLAR:
1. SADECE sana verilen "facts" icindeki sayilari, ORADA YAZILDIGI METIN HALIYLE kullan
   ("3,42 trilyon TL", "%49,83", "+27,51 puan"). Olcek DONUSTURME (milyon->milyar vb.),
   yeniden yuvarlama, hesaplama, tahmin, uydurma YOK.
2. Birim metnin icinde hazirdir; oldugu gibi kopyala. Birimsiz sayi yazma.
3. Bir seri 'stock' ise bu bir DONEM SONU STOGUDUR; "kullandirilan kredi" veya "yeni kredi" DEME.
   Aylik degisim net bakiye degisimidir (yeni kullandirim eksi geri odemeler).
4. 'cumulative_ytd' ise yil basindan itibaren birikimlidir.
5. facts.analysis girdilerinin "description" alani NE bulundugunu soyler -- aynen o anlamda kullan:
   causality: Granger = ONGORULEBILIRLIK ("X'in gecmisi Y'yi ongormeye yardim eder/etmez"); "neden oldu" DEME.
   decompose: nominal = fiyat x reel ayristirmasidir; "veri ... ile TUTARLI/TUTARSIZ" de, kanit deme.
   anomaly: "aykiri ay" de (hata/yanlis veri deme); degisimi scored_unit'e gore % veya puan; en fazla 3 ay say.
   changepoint: "seviye/rejim degisimi" de; kirilma ayini ve oncesi/sonrasi ortalamayi ver. Her kirilmanin
   "confidence" degerini soyle (solid/moderate/tentative -> yuksek/orta/dusuk guven); "recent": true olan
   kirilma icin "cok yeni, henuz dogrulanamiyor" de; "warnings" listesi doluysa her uyariyi AYNEN aktar.
6. caveats listesindeki uyarilari cevapta belirt.
7. KISA yaz: en fazla 3 kisa paragraf, toplam ~150 kelime. Once dogrudan cevap, sonra gerekce.
   Gereksiz giris/kapanis cumlesi yazma.
8. "sunum" alaninda tablo=false ise cevapta tablodan bahsetme, "tabloda goruldugu gibi" DEME;
   grafik=false ise grafikten bahsetme. Sayilari duz metin icinde ver.
9. facts icinde find_periods varsa sorunun "... oldugu donemler var mi" kismini ONUNLA cevapla.
   "description" alani listenin NE oldugunu soyler -- aynen o anlamda kullan: n_column_moves
   ilk serinin o yonde hareket ettigi TOPLAM ay sayisi, n_periods bunlarin icinde kosulu
   saglayan ay sayisi, periods[].period o aylar. Ay UYDURMA, listeyi yanlis adlandirma.
10. Bir oran serisinin SEVIYESI yuzdedir ("%18,4"), iki seviye arasindaki DEGISIMI puandir
   (change_points: "+18,9 puan"). Tutar serisinin `change`/`change_pct` ile degisimi yuzdedir;
   `net_change` ile degisimi ise SERININ KENDI BIRIMINDEDIR (orn. "+3.820 milyon TL"), yuzde DEME.
11. KAYNAK ETIKETI: her onemli sayi veya bulgudan hemen sonra, o veriyi tasiyan facts girdisinin
   "kaynak" etiketini koseli parantezle yaz: "... 678.970 milyon TL'ye yukseldi [K1]",
   "... 32 ayin 4'unde yukselmedi [H1]". SADECE "kaynaklar" listesindeki etiketleri kullan,
   yeni etiket uydurma. Etiket listesini sen yazma; cevabin sonuna otomatik eklenecek.
12. facts.notlar varsa oradaki yontem notunu uygula ve cevapta bir cumleyle belirt (ornegin YP
   stokunun TL karsiliginin kurla mekanik olarak arttigi; yorumu TL payi ve USD bazli sutunla yap).
13. Soru "... sutunu ekleyebilir misin / getirebilir misin" gibi bir ekleme/getirme istiyorsa,
   "tablo_sutunlari" icinde o seriye karsilik gelen bir sutun VARSA bu zaten yapilmis demektir --
   "eklenemez/getiremem" DEME, dogrudan eklendigini soyleyip facts'teki degerleriyle cevapla.
   "tablo_sutunlari" listende olmayan bir seyi ekleyip eklemedigini soyleme; sadece orada gordugunu
   yansit, kendi yapabilirligin hakkinda tahmin yurutme.
14. Soru "hangi veriler var / neler var / kapsami ne" gibi bir METADATA sorusuysa ve
   facts.series BOS ama facts.discovered_keys DOLUYSA: bu veri EKSIK DEGIL, sadece
   fetch edilmedi -- "VERI BULUNAMADI" veya "boyle bir seri yok" DEME. Bunun yerine
   discovered_keys listesindeki serileri (name, source, unit, temporal_semantics,
   first_period-last_period) duz cumleyle tanit: hangi kurumdan (source), hangi
   isimle, hangi birimle yayinlaniyor, hangi donem araligini kapsiyor. Ayni kavramin
   birden fazla serisi varsa (orn. Akim ve Stok) HEPSINI ayri ayri say, aralarindaki
   farki (Akim=yeni kullandirim, Stok=bilanco ortalamasi) bir cumleyle belirt.
15. Soru "her ay icin tablo yap" gibi DONEM DONEM (aybası aybası) bir doküm istiyorsa, KENDIN
   markdown tablo KURMA -- facts sana yalnizca ozet degerler (ilk, son, min, max, degisim) verir,
   her ayin kendi sayisini vermez. Eksik aylari "-" ile doldurup "bu aylarin verisi yok" gibi bir
   izlenim birakma: o veri VAR, sadece sana ulasmadi, ve boyle bir tablo gercek bir veri
   boslugunu uydurma bir bosluktan ayirt edilemez hale getirir. Bunun yerine ozet rakamlari
   (ilk/son/min/max/degisim) duz cumleyle ver ve ayrinti icin ekrandaki "Tablo" panelinin zaten
   tum donemleri gosterdigini soyle.
16. Soru "... ile yorumla / ilişkilendir / birlikte değerlendir" diyorsa VE facts hem series
   (K etiketli) hem documents (U etiketli, bir URL/PDF) iceriyorsa: ikisini YAN YANA SAYIP
   BIRAKMA. Ayri bir paragrafta ACIKCA bir baglanti cumlesi yaz -- hangi yonde (ayni yonde /
   ters yonde / bagimsiz) hareket ettiklerini ve bunun ne anlama gelebilecegini belirt (orn.
   "kredi hacmindeki dususle faiz artisi/pazar daralmasi ayni doneme denk geliyor, bu ...
   isaret ediyor olabilir"). Nedensellik iddia etme (korelasyon nedensellik degildir), ama
   ilişkiyi ACIKLAMADAN gecme -- "dis kaynaktan sayisal seri alinamadi" gibi bir cumleyle
   yorumdan kacinma; documents icindeki rakamlar da facts'te yaziyorsa onlar da kullanilabilir
   bir veridir, eksik degildir.
"""


def compose(session: Session, question: str, client: Optional[KloudeksClient] = None,
            think: bool = False, max_tokens: int = 600) -> Dict[str, Any]:
    """Return {'summary', 'composed_by', 'unsupported_numbers', 'caveats', 'sources'}."""
    sources = source_map(session)
    facts = quotable_numbers(session)
    verification = session.facts.get("verification", {})
    caveats: List[str] = verification.get("caveats", [])

    # Group membership is an executed arithmetic fact. Render grouped answers
    # from lineage so a model cannot add an unexecuted bucket in its prose.
    if client is None or facts.get("group_definitions") or facts.get("movement_comparison"):
        return {"summary": attach_sources(deterministic_summary(session, question), sources),
                "composed_by": "template", "unsupported_numbers": [], "caveats": caveats,
                "sources": list(sources.values())}

    payload = {
        "soru": question,
        "facts": facts,
        # Tag -> short label only; the checkable detail and SQL are appended
        # in Python after composition, so the model never spends tokens on it.
        "kaynaklar": {tag: src["label"] for tag, src in sources.items()},
        "tablo_sutunlari": session.view().units(),
        "caveats": caveats,
        "sunum": session.facts.get("presentation", {"tablo": False, "grafik": False}),
    }
    messages = [
        {"role": "system", "content": COMPOSER_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)[:12000]},
    ]
    try:
        # Free-text prose, no schema to violate -- the fast deployment writes
        # fluent Turkish here at a fraction of the planner's latency (measured;
        # see `KLOUDEKS_FAST_MODEL`).
        summary = client.chat(messages, max_tokens=max_tokens, think=think,
                              model=KLOUDEKS_FAST_MODEL).strip()
        composed_by = "llm"
    except LLMError as exc:
        # An unreachable model must not lose the analysis: the numbers are all
        # computed already, so fall back to stating them plainly.
        summary = deterministic_summary(session, question)
        composed_by = f"template (model unavailable: {exc})"

    # Checked before the legend is appended: the legend's own figures (period
    # bounds, point counts) are not claims the model made.
    flagged = unsupported_numbers(summary, facts)
    summary = attach_sources(summary, sources)
    if flagged:
        summary += ("\n\n_Not: bu cevaptaki bazi sayilar hesaplanan verilerle eslesmedi "
                    f"({', '.join(str(n) for n in flagged[:5])}); lutfen tabloyu esas alin._")
    return {"summary": summary, "composed_by": composed_by,
            "unsupported_numbers": flagged, "caveats": caveats,
            "sources": list(sources.values())}


def deterministic_summary(session: Session, question: str) -> str:
    """The answer with no model at all: every computed figure, stated plainly.

    Not a graceful degradation so much as the floor the system guarantees --
    the numbers and their units are already known before any prose is written.
    """
    artifact = session.view()
    if artifact.is_empty():
        if session.facts.get("semantic_limitations"):
            return " ".join(session.facts["semantic_limitations"])
        if any(a.op == "clear_table" and a.ok for a in session.audit) and session.artifact.is_empty():
            return "Tablo temizlendi. Yeni bir soru sorabilirsiniz."
        failures = "; ".join(a.detail for a in session.audit if not a.ok)
        return f"Tablo olusturulamadi. {failures or 'Veri bulunamadi.'}"

    lines = [f"{len(artifact.frame)} donem, {len(artifact.frame.columns)} seri "
             f"({artifact.periods()[0][:7]} .. {artifact.periods()[-1][:7]}):", ""]
    sources = session.facts.get("sources") or {}
    tag_of_column = {s["column"]: tag for tag, s in sources.items() if s.get("column")}
    for name, stats in artifact.summary().items():
        if not stats.get("n"):
            continue
        tag = f" [{tag_of_column[name]}]" if name in tag_of_column else ""
        change = format_change(stats)
        lines.append(
            f"- {stats['label']} ({stats['unit']}, {stats['temporal_semantics']}): "
            f"{stats['first_period']} {format_quantity(stats['first_value'], stats['unit'])} -> "
            f"{stats['last_period']} {format_quantity(stats['last_value'], stats['unit'])}"
            f"{', degisim ' + change if change else ''}{tag}")
    if any((line.transform or "").startswith("in_usd(") for line in artifact.lineage.values()):
        lines += ["", "Not: " + VALUATION_NOTE]
    if any(line.temporal_semantics == "net_change" for line in artifact.lineage.values()):
        lines += ["", "Seriler net bakiye değişimidir; brüt giriş veya yeni mevduat değildir. "
                  "Önceki ayın bakiyesi yoksa net değişim eksik bırakılır."]
    movement = quotable_numbers(session).get("movement_comparison")
    if movement:
        lines += ["", movement]

    for found in session.facts.get("find_periods", []):
        months = ", ".join(p["period"] for p in found["periods"][:8])
        tag = f" [{found['kaynak']}]" if found.get("kaynak") else ""
        lines += ["", f"{found.get('description', found['column'])}: "
                      f"{found['n_periods']} donem: {months or '-'}{tag}"]

    # An analysis that ran is part of the answer even with no model to
    # narrate it -- its own `description` states the finding.
    for key, result in (session.facts.get("analysis") or {}).items():
        if not isinstance(result, dict):
            continue
        method, _, column = key.partition(":")
        tag = f" [{result['kaynak']}]" if result.get("kaynak") else ""
        lines += ["", (result.get("description") or f"{method} analizi: {column}") + tag]

    chart = session.facts.get("chart") or {}
    if chart.get("kind") == "pie":
        lines += ["", f"Pasta grafigi ({chart['period']}): "
                  + ", ".join(f"{label} {share}" for label, share in chart["shares"].items())]
    caveats = session.facts.get("verification", {}).get("caveats", [])
    if caveats:
        lines += ["", "Uyarilar: " + "; ".join(caveats[:3])]
    return "\n".join(lines)
