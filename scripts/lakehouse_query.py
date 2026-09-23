#!/usr/bin/env python3
"""Query the lakehouse without a SQL client.

    .venv/bin/python scripts/lakehouse_query.py                 # run the manual check plan
    .venv/bin/python scripts/lakehouse_query.py --sql "SELECT ..."
    .venv/bin/python scripts/lakehouse_query.py --repl          # type SQL, empty line to quit

Opens data/lakehouse.duckdb read-only, so it never blocks a build.
"""
import argparse
import sys

import duckdb
import pandas as pd

from backend.core.config import DUCKDB_PATH

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)
pd.set_option("display.max_rows", 120)
pd.set_option("display.float_format", lambda v: f"{v:,.4f}")

DEMO_TABLE = """
WITH loans AS (
  SELECT period, value AS konut_kredisi FROM bulletin_observations
  WHERE dataset='tuketici_kredileri' AND entity_key='tuketici_kredileri_konut' AND currency='total'),
macro AS (
  SELECT period,
    max(CASE WHEN series_code='TP.KTF12' THEN value END) AS faiz,
    max(CASE WHEN series_code='TP.GENENDEKS.T1' THEN value END) AS tufe,
    max(CASE WHEN series_code='TP.KFE.TR' THEN value END) AS kfe,
    max(CASE WHEN series_code='DERIVED.IPOTEKLI_PAY.KTRTOPLAM' THEN value END) AS ipotekli_pay
  FROM macro_observations GROUP BY period)
SELECT l.period, konut_kredisi, faiz, tufe, kfe, ipotekli_pay,
       round(100 * konut_kredisi / first_value(konut_kredisi) OVER (ORDER BY l.period), 1) AS kredi_endeks,
       round(100 * konut_kredisi / tufe / first_value(konut_kredisi / tufe) OVER (ORDER BY l.period), 1) AS reel_endeks,
       round(100 * kfe / first_value(kfe) OVER (ORDER BY l.period), 1) AS kfe_endeks
FROM loans l JOIN macro m USING (period)
WHERE l.period BETWEEN '2021-01-01' AND '2025-12-01' ORDER BY 1
"""

# (title, expectation, sql)
CHECKS = [
    ("1. Tablolar", "13 tablo; macro_series / macro_observations / macro_observations_native dahil",
     "SHOW TABLES"),
    ("1b. Satır sayıları", "1 515 / 89 680 / 158 179",
     "SELECT 'series' AS t, count(*) AS n FROM macro_series "
     "UNION ALL SELECT 'monthly', count(*) FROM macro_observations "
     "UNION ALL SELECT 'native', count(*) FROM macro_observations_native"),
    ("2. Etiketsiz seri", "0",
     "SELECT count(*) AS etiketsiz FROM macro_series "
     "WHERE temporal_semantics IS NULL OR monthly_rule IS NULL OR unit IS NULL"),
    ("2b. Katalog dağılımı", "tier 0: rate/index/flow/stock; quarterly yalnızca bkea ve gsyh*",
     "SELECT tier, temporal_semantics, native_frequency, count(*) AS n "
     "FROM macro_series GROUP BY ALL ORDER BY 1,2,3"),
    ("2c. 'konut kredisi' araması", "TP.KTF12 (haftalık akım) ve TP.BKR.TRY.18 (aylık stok), ikisi rate/avg",
     "SELECT series_code, name_tr, unit, temporal_semantics, monthly_rule, native_frequency "
     "FROM macro_series WHERE name_tr ILIKE '%konut kredisi%'"),
    ("3. Demo serileri kapsama", "hepsi min 2021-01-01, max >= 2026-06-01, 66+ ay",
     "SELECT series_code, count(*) AS ay, min(period) AS ilk, max(period) AS son FROM macro_observations "
     "WHERE series_code IN ('TP.KTF12','TP.GENENDEKS.T1','TP.KFE.TR','TP.AKONUTSAT1.KTRTOPLAM',"
     "'TP.AKONUTSAT2.KTRTOPLAM','DERIVED.IPOTEKLI_PAY.KTRTOPLAM','TP.APIFON4') GROUP BY 1 ORDER BY 1"),
    ("3b. Ocak 2021 değerleri", "KTF12 ~18.39 (n=5), TÜFE 513.30, toplam satış 75 603, ipotekli 11 560, pay ~15.29",
     "SELECT series_code, value, value_avg, value_last, n_native_obs, monthly_rule FROM macro_observations "
     "WHERE period = DATE '2021-01-01' AND series_code IN ('TP.KTF12','TP.GENENDEKS.T1',"
     "'TP.AKONUTSAT1.KTRTOPLAM','TP.AKONUTSAT2.KTRTOPLAM','DERIVED.IPOTEKLI_PAY.KTRTOPLAM') ORDER BY 1"),
    ("4. Haftalık -> aylık", "5 Cuma (1, 8, 15, 22, 29 Ocak); ortalaması 18.39",
     "SELECT date, value FROM macro_observations_native "
     "WHERE series_code='TP.KTF12' AND date BETWEEN '2021-01-01' AND '2021-01-31' ORDER BY date"),
    ("4b. Günlük -> ay sonu vs ortalama (USD, Aralık 2021)", "avg ~13.53, last ~12.98, n=23",
     "SELECT value_avg, value_last, n_native_obs FROM macro_observations "
     "WHERE series_code='TP.DK.USD.A.YTL' AND period=DATE '2021-12-01'"),
    ("4c. Çeyreklik seriler", "3, 6, 9, 12",
     "SELECT DISTINCT month(period) AS ay FROM macro_observations "
     "WHERE series_code='TP.GSYIH20.BY.B1GQ' ORDER BY 1"),
    ("4d. Akım seri YTD değil", "Mayıs (11 282) < Nisan (18 779)",
     "SELECT period, value FROM macro_observations "
     "WHERE series_code='TP.AKONUTSAT2.KTRTOPLAM' AND year(period)=2021 ORDER BY 1"),
    ("5. Kalite raporu", "failed = 0; strict ~60; reported ~1 450",
     "SELECT count(*) FILTER (WHERE \"check\" LIKE 'coverage strict%') AS strict, "
     "count(*) FILTER (WHERE \"check\" LIKE 'coverage reported%') AS reported, "
     "count(*) FILTER (WHERE NOT passed) AS failed FROM data_quality_report WHERE source='TCMB_EVDS'"),
    ("5b. Boşluk bildirilen seriler", "küçük iller, altın piyasası, emekli anket kalemleri; ulusal seri yok",
     "SELECT \"check\", detail FROM data_quality_report "
     "WHERE source='TCMB_EVDS' AND detail LIKE '%gap%' ORDER BY 1"),
    ("6. Demo tablosu", "60 satır, NULL yok",
     DEMO_TABLE),
]


def run(connection, sql: str) -> None:
    frame = connection.execute(sql).df()
    print(frame.to_string(index=False) if not frame.empty else "(boş sonuç)")
    print(f"-- {len(frame)} satır")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sql", help="Run one statement and print the result.")
    parser.add_argument("--repl", action="store_true", help="Interactive: one statement per line.")
    args = parser.parse_args()

    if not DUCKDB_PATH.exists():
        print(f"{DUCKDB_PATH} yok; önce build çalıştırın.")
        return 1
    connection = duckdb.connect(str(DUCKDB_PATH), read_only=True)

    if args.sql:
        run(connection, args.sql)
        return 0

    if args.repl:
        print("SQL yazın, boş satır çıkar.")
        while True:
            try:
                line = input("duckdb> ").strip()
            except EOFError:
                break
            if not line:
                break
            try:
                run(connection, line)
            except Exception as exc:                                     # noqa: BLE001
                print(f"HATA: {exc}")
        return 0

    for title, expectation, sql in CHECKS:
        print("\n" + "=" * 100)
        print(f"{title}\n   beklenen: {expectation}")
        print("-" * 100)
        run(connection, sql)
    return 0


if __name__ == "__main__":
    sys.exit(main())
