"""Frequency alignment and derived series for the EVDS macro corpus.

The lakehouse analyses at the monthly grain. EVDS series arrive daily, weekly,
monthly and quarterly, and the rule that brings each to a month is declared
per series in the catalogue (`monthly_rule`: last / avg / sum) -- never
chosen at query time (Launch.MD §4.2).

Sub-monthly series are collapsed with their rule; `value_avg` and `value_last`
are kept next to the chosen `value` because a rate is usually wanted as a
monthly average while a balance or an exchange rate used for revaluation is
wanted at month end. Quarterly series are stamped on the last month of the
quarter and flagged through `native_frequency`, so a monthly join sees them
where they belong and nowhere else.
"""
import pandas as pd

from ..core import search_text
from ..domain.evds_series import DERIVED_SERIES, PROPERTY_TYPE_WORDS


def _month_start(dates: pd.Series) -> pd.Series:
    return pd.to_datetime(dates).dt.to_period("M").dt.to_timestamp().dt.date


def align_monthly(native: pd.DataFrame, catalogue: pd.DataFrame) -> pd.DataFrame:
    """Native observations -> one row per (series, month)."""
    rules = catalogue.set_index("series_code")["monthly_rule"]
    unknown = set(native.series_code) - set(rules.index)
    if unknown:
        raise ValueError(f"observations for series missing from the catalogue: {sorted(unknown)[:10]}")

    frame = native.copy()
    frame["period"] = _month_start(frame["date"])
    frame = frame.sort_values(["series_code", "date"])

    grouped = frame.groupby(["series_code", "period"], sort=False)
    monthly = grouped.agg(
        datagroup=("datagroup", "first"),
        value_avg=("value", "mean"),
        value_last=("value", "last"),
        value_sum=("value", "sum"),
        n_native_obs=("value", "size"),
        grain=("grain", "first"),
    ).reset_index()

    monthly["monthly_rule"] = monthly["series_code"].map(rules)
    monthly["value"] = monthly["value_avg"]
    is_last = monthly["monthly_rule"] == "last"
    is_sum = monthly["monthly_rule"] == "sum"
    monthly.loc[is_last, "value"] = monthly.loc[is_last, "value_last"]
    monthly.loc[is_sum, "value"] = monthly.loc[is_sum, "value_sum"]

    # A single native observation per month (monthly and quarterly series)
    # makes avg/last/sum identical; drop the sum for non-flows so nobody
    # reads it as meaningful.
    monthly.loc[~is_sum, "value_sum"] = None

    columns = ["period", "series_code", "datagroup", "value", "value_avg", "value_last",
               "value_sum", "n_native_obs", "monthly_rule"]
    return monthly[columns].sort_values(["datagroup", "series_code", "period"]).reset_index(drop=True)


def add_derived_series(monthly: pd.DataFrame) -> pd.DataFrame:
    """Series computed from others, so the agent never divides in its head."""
    frames = [monthly]
    for code, _group, _semantics, _name_tr, _name_en in DERIVED_SERIES:
        if code != "DERIVED.IPOTEKLI_PAY":
            raise NotImplementedError(code)
        frames.append(_ipotekli_share(monthly))
    return pd.concat(frames, ignore_index=True)


def _ipotekli_share(monthly: pd.DataFrame) -> pd.DataFrame:
    """Mortgaged sales / total sales, per month and per region suffix.

    The suffix after `TP.AKONUTSAT{1,2}.` is property type and region together
    -- a leading `K` is Konut, its absence İş Yeri -- so pairing on the whole
    suffix pairs like with like, and 166 shares come out of 83 regions. The
    arithmetic was always right; only the catalogue's names were not.
    """
    total = monthly[monthly.datagroup == "bie_akonutsat1"].copy()
    mortgaged = monthly[monthly.datagroup == "bie_akonutsat2"].copy()
    total["region"] = total.series_code.str.replace("TP.AKONUTSAT1.", "", regex=False)
    mortgaged["region"] = mortgaged.series_code.str.replace("TP.AKONUTSAT2.", "", regex=False)
    joined = mortgaged.merge(total[["period", "region", "value"]], on=["period", "region"],
                             suffixes=("", "_total"))
    joined = joined[joined.value_total > 0]
    share = pd.DataFrame({
        "period": joined.period,
        "series_code": "DERIVED.IPOTEKLI_PAY." + joined.region,
        "datagroup": "bie_akonutsat2",
        "value": 100 * joined.value / joined.value_total,
    })
    share["value_avg"] = share["value"]
    share["value_last"] = share["value"]
    share["value_sum"] = None
    share["n_native_obs"] = 1
    share["monthly_rule"] = "avg"
    return share


def expand_derived_catalogue(catalogue: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    """One catalogue row per derived series actually produced (one per region)."""
    template = catalogue[catalogue.derived].set_index("series_code")
    produced = monthly[monthly.series_code.str.startswith("DERIVED.")].series_code.unique()
    rows = []
    for code in produced:
        base_code = ".".join(code.split(".")[:2])
        region = code.split(".", 2)[2]
        base = template.loc[base_code]
        source = catalogue[catalogue.series_code == f"TP.AKONUTSAT2.{region}"].iloc[0]
        # TCMB names these `{Region}_{PropertyType}_{SalesType}`. Taking only
        # the region dropped the one segment that separates Konut from İş Yeri,
        # so half the derived catalogue said "konut" over commercial-premises
        # sales -- see the note on DERIVED_SERIES.
        region_name, property_type = source["name_tr"].split("_")[:2]
        row = base.to_dict()
        row["series_code"] = code
        tip_tr, tip_en = PROPERTY_TYPE_WORDS.get(property_type, (property_type, property_type))
        row["name_tr"] = f"{base['name_tr'].format(tip=tip_tr)} - {region_name}"
        row["name_en"] = f"{base['name_en'].format(tip=tip_en)} - {region_name}"
        row["level"] = source["level"]  # Türkiye total is level 1, provinces level 2
        row["published_start"] = source["published_start"]
        row["published_end"] = source["published_end"]
        rows.append(row)
    concrete = pd.DataFrame(rows)
    full = pd.concat([catalogue[~catalogue.derived], concrete], ignore_index=True)

    # The catalogue is complete here and nowhere earlier: the derived rows only
    # learn their real names above. Composing the search text before this point
    # would index 83 commercial-premises series under the template's "konut".
    full["search_text"] = [
        search_text.for_macro_series(row.name_tr, row.name_en, row.datagroup_name_tr,
                                     row.category_tr, row.unit)
        for row in full.itertuples()]
    full["search_fold"] = [search_text.searchable(t) for t in full.search_text]
    return full
