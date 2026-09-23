# BDDK + linked external data: website acceptance tests

Use **Otomatik / veri analizi**, with a fresh chat for each prompt. The web-only
research mode does not query the local BDDK database. Enable URL research,
link discovery and documents as described in [web-research.md](web-research.md).

The answers below were checked against the local `data/lakehouse.duckdb` and
Borsa İstanbul's 2026 PDF on 2026-09-21. The PDF URL is updated in place; check
its document year when running these examples later.

Both prompts were also run through the website with the live model. The numeric
tables and indices matched the expected answers below, and the imported values
were independently queried from DuckDB after each request.

## 1. Obvious: explicit source and BDDK key

```text
Yerel DuckDB'deki BDDK verisi ile bu PDF'deki veriyi tek bir aylık tabloda birleştir: https://www.borsaistanbul.com/dosyalar/kmtp/veriler/kmp_au.pdf
Dönem: 2026-01-01–2026-02-28. İki veri sütunu istiyorum:
1. BDDK aylık bülteni: source=bulletin, dataset=tuketici_kredileri, key=tuketici_kredileri_konut, currency=total. Ay sonu konut kredisi bakiyesi; birim milyon TL.
2. PDF'deki tüm para birimleri için toplam altın işlem miktarı; birim kg. TL işlemlerinin miktarını toplamla karıştırma.
PDF'yi gerçekten oku; dış veri lakehouse'un dış kaynak bölgesine kaynak URL'si, sayfa, dönem ve birimiyle kalıcı kaydedilir. Sonucu uygulamanın Tablo sekmesine de koy. Kaynakları belirt; eksik değeri tahmin etme.
```

Expected table (dots are thousands separators):

| Month | BDDK housing loans (million TL) | BIST total gold quantity (kg) |
| --- | ---: | ---: |
| January 2026 | 691.343 | 33.584 |
| February 2026 | 715.799 | 32.719 |

The BDDK column comes from `bulletin_observations`, filtered by
`dataset='tuketici_kredileri'`, `entity_key='tuketici_kredileri_konut'`,
`currency='total'`, `metric='balance'`. It is a month-end stock in **million TL**.
The external column comes from [BIST's gold trading PDF](https://www.borsaistanbul.com/dosyalar/kmtp/veriler/kmp_au.pdf),
**2026, page 1**, the **TOPLAM / TOTAL quantity (kg)** column, and is a monthly flow.

## 2. Non-obvious: infer the series, discover the file, compare direction

```text
2026-01-01–2026-02-28 döneminde hanehalkının bankalara olan konut kredisi borcu büyürken Borsa İstanbul'da işlem gören altın miktarı da aynı yönde mi hareket etti?
Konut kredisi ay sonu bakiyesi için sistemdeki BDDK aylık verisini kullan; TL ve yabancı para toplamını milyon TL olarak al. Altında yalnız TL işlemlerini değil, tüm para birimlerinin toplam kg miktarını karşılaştır. İlgili belgeyi bu sayfadaki bağlantılar arasından kendin bul: https://www.borsaistanbul.com/veriler/kiymetli-madenler-ve-kiymetli-taslar-piyasasi/piyasa-verileri
Tek bir tabloda iki ayın ham değerlerini ve her iki serinin Ocak=100 endeksini göster. Bu iki ölçünün stok/akım farkını açıkla; aralarında nedensellik iddia etme. Dış kaynağı ve sayısal verilerini DuckDB'ye kaydet, kaynak belge/yıl/sayfa bilgisini belirt. Tabloyu uygulamanın Tablo sekmesine koy.
```

Expected table:

| Month | Housing loans (million TL) | Total gold quantity (kg) | Housing index (Jan=100) | Gold index (Jan=100) |
| --- | ---: | ---: | ---: | ---: |
| January 2026 | 691.343 | 33.584 | 100,00 | 100,00 |
| February 2026 | 715.799 | 32.719 | 103,54 | 97,42 |

**They moved in opposite directions:** housing-loan balances increased **3.54%**
and gold trading quantity decreased **2.58%** from January to February. Indices
are computed by Python as `100 * value / January_value`; absolute balances and
kg amounts must not be divided or treated as like-for-like magnitudes. The loan
series is outstanding debt at month end, not new lending; the gold series is
activity during the month. Two observations do not establish causality.

## What counts as passing

- **Tablo** contains actual numeric columns and two monthly rows, not only a
  Markdown table in the answer. It opens automatically when a response contains
  table data, and refreshing the page reloads the current working table from
  the API. Starting a new chat clears that working table.
- The audit shows successful `fetch_series` and `ingest_external`; the second
  prompt also needs two `index_to_base` transforms. Source discovery is visible
  in the saved research tool outputs.
- BDDK provenance carries its dataset/key/currency and million-TL unit. PDF
  provenance carries its URL, year, page, kg unit and content hash.
- `landed_sources` in the response names the landed source and its series; the
  `external_*` views hold the typed observations; `evidence` says how many web tool
  results were logged in `research.duckdb`.
- The external report's blank future months are absent, not manufactured zero
  observations. The full imported series can outlast the two-month display
  window; imports retain every published month read from the report.

## Inspect the landed data

Numeric series a URL yields land in the lakehouse's **external zone**:
`data/external/<source_id>/*.parquet`, exposed by the build as the
`external_sources`, `external_series`, `external_observations`,
`external_observations_native` and `external_quality_report` views of
`data/lakehouse.duckdb` (see CLAUDE.md, "The external zone"). Every web tool
result a turn used is logged separately in `data/research.duckdb`
(`research_runs`, `research_tool_results`) and replayed by the Araştırma tab.
The two can be joined in one DuckDB session:

```bash
.venv/bin/python scripts/lakehouse_query.py --sql "
  SELECT b.period, b.value AS housing_million_tl, o.value AS gold_kg
  FROM bulletin_observations b
  JOIN external_observations o USING (period)
  JOIN external_series s USING (series_key)
  WHERE b.dataset = 'tuketici_kredileri' AND b.entity_key = 'tuketici_kredileri_konut'
    AND b.currency = 'total' AND b.metric = 'balance'
    AND s.url = 'https://www.borsaistanbul.com/dosyalar/kmtp/veriler/kmp_au.pdf'
    AND s.name LIKE 'TOPLAM%'
    AND b.period BETWEEN DATE '2026-01-01' AND DATE '2026-02-01'
  ORDER BY b.period"
```

Measured on 2026-09-22 with no model in the loop: the prompt above lands the
PDF (ten series), the plan fetches its TOTAL kg column and the BDDK housing
line, and the table reads 33,584 / 32,719 kg beside 691,343 / 715,799 milyon
TL. The PDF's TL volume column is additionally *verified* against EVDS
`TP.ALTINPIYASA.HACM02`, which agrees with it to 0.000% on every overlapping
month. Excel/CSV/PDF/HTML/image sources all go through the same generic
`backend/ingestion/external` pipeline; there is no per-publisher adapter.
