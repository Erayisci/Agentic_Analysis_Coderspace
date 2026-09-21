"""The text discovery searches, composed once per index row at build time.

Until this module existed, discovery searched two columns -- the key and the
published name -- and everything else a row knows about itself was invisible to
it. That is narrower than both the corpus's own metadata and the words a
question actually uses, and the gap is not academic:

- **"Ağırlıklı Ortalama Ticari Kredi Faizleri" found no rate series.** The word
  "faiz" appears in no `TP.KTF*` name: the series is called "Ticari Krediler
  (TL, Akım, %)" and the phrase the question used lives in its data group's
  title, `Kredi Faiz Oranları (Akım)` -- a column that was in `macro_series`
  and never read.
- **"Takipteki Alacaklar Oranı" found the balance-sheet stock.** Three entities
  share those words: a stock in milyon TL, a provision in milyon TL, and the
  ratio in %. The only word that separates them is "Oranı", and a unit has no
  spelling a lexical search can reach.

So each index row carries a `search_text`: its own name, plus the context that
names it -- the parent line it sits under, the table or data group it belongs
to, the category above that, and words for its unit and kind. The published
name stays its own column and keeps its own, higher, scoring weight; this is
context, and ranking it as loudly as the name would let a group title drown the
series inside it.

What is deliberately NOT here: the source. "BDDK bülteni" and "EVDS" are words
a question really does use, but as search text they match every row of that
corpus equally -- a term that fires on 519 rows discriminates nothing and only
dilutes the words that do. A named source is a filter, and it is handled as one
in `tools.lakehouse`.
"""
from typing import Iterable, Optional

from .labels import fold

# Words a question uses for a unit, which the unit itself does not spell. The
# map is short and one-directional on purpose: it exists so "oranı", "rasyo" and
# "yüzde" can reach a row whose unit is `%`, not to build a synonym dictionary.
# Every monetary unit answers to "tutar"/"bakiye" because that is what a
# question about an amount says, and the unit column keeps the distinction that
# matters (bin TL vs milyon TL is a 1000x trap the agent must never resolve by
# vocabulary).
UNIT_WORDS = {
    "%": "oran oranı rasyo yüzde pay",
    "milyon TL": "tutar bakiye büyüklük",
    "bin TL": "tutar bakiye büyüklük",
    "TL": "tutar bakiye büyüklük",
    "adet": "sayı adet",
    "kişi": "sayı kişi",
    "gün": "vade gün",
}

# What kind of thing the row is, in the words a question would use for it.
# `entity_type` is already a curated column in `domain.bulletin_tables`; this
# only gives it a Turkish spelling a search can match.
KIND_WORDS = {
    "ratio": "rasyo oran",
    "loan_product": "kredi ürünü",
    "loan_type": "kredi türü",
    "deposit_type": "mevduat türü",
    "maturity_bucket": "vade dilimi",
    "security_type": "menkul kıymet",
    "sector": "sektör",
    "counter": "adet sayı",
    "capital_item": "sermaye",
    "fx_position_item": "döviz pozisyonu",
    "liquidity_item": "likidite",
    "income_statement_item": "gelir gider kar zarar",
    "balance_sheet_item": "bilanço kalemi",
    "off_balance_item": "bilanço dışı",
}

# Only the semantics a question names in words. "stock" and "flow" are read off
# the column by the tools, not asked for by a user, and adding them would put
# "stok" on 500 rows.
SEMANTICS_WORDS = {
    "cumulative_ytd": "kümülatif yılbaşından itibaren birikimli",
}


def compose(*parts: Optional[str]) -> str:
    """One searchable string from the parts that exist.

    De-duplicated because the same word arrives from several directions -- a
    data group called "Kredi Faiz Oranları" holding a series called "Ticari
    Krediler" repeats "kredi", and a repeated term would score twice for saying
    one thing once.
    """
    seen, words = set(), []
    for part in parts:
        if not part:
            continue
        for word in str(part).split():
            folded = word.casefold()
            if folded not in seen:
                seen.add(folded)
                words.append(word)
    return " ".join(words)


def _humanise(slug: Optional[str]) -> str:
    """`tuketici_kredileri` -> `tuketici kredileri`, so a dataset slug is words."""
    return (slug or "").replace("_", " ")


def for_bulletin_entity(name: str, parent_name: Optional[str], dataset: Optional[str],
                        table_title: Optional[str], entity_type: Optional[str],
                        unit: Optional[str], semantics: Optional[str]) -> str:
    """A monthly-bulletin line item.

    The parent name matters more here than anywhere else: tables 9, 10 and 11
    repeat labels, so `a) Gerçek Kişiler` is six different series whose only
    distinguishing text is the deposit type above it.
    """
    return compose(name, parent_name, table_title, _humanise(dataset),
                   KIND_WORDS.get(entity_type or ""), UNIT_WORDS.get(unit or ""),
                   SEMANTICS_WORDS.get(semantics or ""))


def for_macro_series(name_tr: str, name_en: Optional[str], datagroup_name: Optional[str],
                     category: Optional[str], unit: Optional[str]) -> str:
    """A TCMB EVDS series.

    The data group's title is the load-bearing part: TCMB names a series by what
    it measures ("Ticari Krediler") and the group by how ("Kredi Faiz
    Oranları"), so neither half alone is what a question says.
    """
    return compose(name_tr, name_en, datagroup_name, category, UNIT_WORDS.get(unit or ""))


def for_weekly_item(name: str, parent_name: Optional[str], dataset: Optional[str],
                    is_informational: bool = False) -> str:
    """A weekly-bulletin line item. Its unit is milyon TL throughout, so the
    unit words are constant and carry no information; the `(Bilgi)` marker is
    already inside the published name."""
    return compose(name, parent_name, _humanise(dataset),
                   UNIT_WORDS["milyon TL"], "bilgi amaçlı" if is_informational else None)


def searchable(text: str) -> str:
    """The folded form the lexical search actually greps.

    Kept as a second column rather than replacing `search_text`, because the
    two have different consumers: SQL needs one alphabet and cannot fold
    Turkish itself, while anything semantic -- an embedding, a name shown to
    the planner -- wants the language as published.
    """
    return fold(text or "")


def all_of(frame_column: Iterable[Optional[str]]) -> Iterable[str]:
    """Convenience for a pandas apply that must never emit NULL: a row with no
    search text would be invisible to every query, which is worse than a row
    that merely ranks badly."""
    return (text or "" for text in frame_column)
