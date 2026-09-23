"""Validation for the BDDK weekly-bulletin corpus.

Three checks, and the first two are the weekly counterparts of what
`validation.continuity` does for the monthly bulletin: every series must cover
the history it claims, and every label that states its own arithmetic must
satisfy it. The third has no monthly counterpart because the monthly bulletin
does not publish the fact it verifies.

    weekly_continuity   an active item spans the corpus, or is registered in
                        `KNOWN_WEEKLY_LIFECYCLES` / `KNOWN_WEEKLY_GAPS`. A
                        retired item must stop where BDDK says it stopped --
                        that is a hard check, never registerable, because the
                        source states the answer and a value past it means the
                        column was read from the wrong place.
    row_identity        reused verbatim from the monthly path, on active rows
                        only: a retired label's positions address a layout the
                        table no longer has.
    superseded_series   the evidence that a retired item and the item that
                        replaced it are one series. Where both are present they
                        must agree; a disagreement means the replacement changed
                        the definition and the two must not be read as one.

A note on why retired items are kept at all. They duplicate their successors
over the overlap, so carrying them risks a double count in any aggregate that
ignores `retired_on`. Dropping them would be tidier and would also delete the
only published record of one week -- 2022-01-07 in table 297 -- and of the
pre-2022 securities classification. The corpus keeps them, labels them, and
this module proves what they are.
"""
import calendar

import pandas as pd

from ..core.errors import ValidationError
from ..domain.weekly_tables import (
    KNOWN_WEEKLY_GAPS,
    KNOWN_WEEKLY_LIFECYCLES,
    WEEKLY_MONTHLY_PAIRS,
    WEEKLY_MONTHLY_TOLERANCE_PCT,
)
from .continuity import check_identities

# Values are milyon TL carrying five decimals, so the identities hold to the
# published precision rather than to the monthly corpus's integer rounding.
WEEKLY_TOLERANCE = 0.01
WEEKLY_RELATIVE = 1e-6


def check_weekly_continuity(observations: pd.DataFrame, table_slug: str) -> list:
    """One row per item whose coverage is not what the source says it should be."""
    periods = sorted({str(pd.Timestamp(p).date()) for p in observations["period"].unique()})
    expected = set(periods)
    gaps = set(KNOWN_WEEKLY_GAPS.get(table_slug, ()))

    results = []
    for entity_key, group in observations.groupby("entity_key"):
        covered = {str(pd.Timestamp(p).date()) for p in group["period"].unique()}
        retired_on = group["retired_on"].iloc[0]

        if pd.notna(retired_on):
            # BDDK states the end date, so this is arithmetic, not judgement.
            retired = str(pd.Timestamp(retired_on).date())
            overrun = sorted(p for p in covered if p > retired)
            results.append({
                "dataset": table_slug,
                "check": "weekly_continuity",
                "entity_key": entity_key,
                "passed": not overrun,
                "detail": (
                    f"retired {retired} by BDDK; {len(covered)} week(s) up to {max(covered)}"
                    + (f"; {len(overrun)} week(s) published AFTER retirement: {overrun[:3]}"
                       if overrun else "")
                ),
            })
            continue

        if covered == expected:
            continue

        starts, ends = KNOWN_WEEKLY_LIFECYCLES.get((table_slug, entity_key), (None, None))
        window = {p for p in periods
                  if (starts is None or p >= starts) and (ends is None or p <= ends)}
        missing = sorted((window - covered) - gaps)
        unexpected = sorted(covered - window)
        registered = (table_slug, entity_key) in KNOWN_WEEKLY_LIFECYCLES
        results.append({
            "dataset": table_slug,
            "check": "weekly_continuity",
            "entity_key": entity_key,
            "passed": not missing and not unexpected,
            "detail": (
                (f"registered lifecycle {starts or periods[0]}..{ends or periods[-1]}; "
                 if registered else "unregistered partial coverage; ")
                + f"{len(covered)}/{len(expected)} weeks"
                + (f"; {len(missing)} unexplained gap(s): {missing[:3]}" if missing else "")
                + (f"; {len(unexpected)} week(s) outside the window" if unexpected else "")
                + (f"; {len(gaps & (window - covered))} registered publication gap(s)"
                   if gaps & (window - covered) else "")
            ),
        })

    return results


def check_superseded_series(observations: pd.DataFrame, table_slug: str) -> list:
    """Each retired item must agree with a current item wherever both exist.

    This is what licenses reading a retired item and its replacement as one
    series, and it is measured every build rather than asserted once: if BDDK
    re-issues a table and changes the numbers rather than just the row ids, the
    agreement breaks and the check says so.

    Matching is by value, not by label: table 291 replaced
    'a) Kamu Borçlanma Senetleri' with a differently worded row, and table 297
    replaced items with labels identical to the ones they replaced, so the label
    identifies nothing. The agreement itself is the evidence.
    """
    retired = observations[observations.retired_on.notna()]
    if retired.empty:
        return []
    current = observations[observations.retired_on.isna()]

    wide = current.pivot_table(index=["period", "currency"], columns="entity_key",
                               values="value", aggfunc="first")
    results = []
    for entity_key, group in retired.groupby("entity_key"):
        series = group.set_index(["period", "currency"]).value
        overlap = wide.reindex(series.index).dropna(how="all")
        if overlap.empty:
            results.append({
                "dataset": table_slug, "check": "superseded_series", "entity_key": entity_key,
                "passed": True,
                "detail": "retired before the corpus window; carries no observation",
            })
            continue

        aligned = series.reindex(overlap.index)
        agree = {column: int((overlap[column] - aligned).abs().le(WEEKLY_TOLERANCE).sum())
                 for column in overlap.columns}
        best, matches = max(agree.items(), key=lambda kv: kv[1])
        comparable = int(overlap[best].notna().sum())
        results.append({
            "dataset": table_slug,
            "check": "superseded_series",
            "entity_key": entity_key,
            "passed": comparable > 0 and matches == comparable,
            "detail": (
                f"{len(aligned)} observation(s); current item {best} agrees on "
                f"{matches}/{comparable} of the weeks both publish"
                + ("" if matches == comparable else " -- the re-issue CHANGED the definition")
            ),
        })
    return results


def month_end_weeks(periods) -> list:
    """The weekly observation dates that fall on the last day of their month.

    The only dates on which the two bulletins can be compared without
    interpolating: everywhere else the weekly figure is a Friday inside the
    month and the monthly one is the month's close.
    """
    dates = sorted({pd.Timestamp(p) for p in periods})
    return [d for d in dates if d.day == calendar.monthrange(d.year, d.month)[1]]


def check_weekly_against_monthly(weekly: pd.DataFrame, monthly: pd.DataFrame) -> pd.DataFrame:
    """Cross-check the weekly corpus against the monthly one, series by series.

    Each registered pair is compared only on the dates where a weekly
    observation lands exactly on a month end. The weekly release is a flash
    figure and the monthly one is revised, so they are expected to differ by
    hundredths of a percent; a failure here means a parsing or alignment error,
    which moves a figure by far more than a revision does.
    """
    weekly = weekly[weekly.currency == "total"]
    monthly = monthly[monthly.currency == "total"]
    aligned = month_end_weeks(weekly.period.unique())

    rows = []
    for weekly_slug, item_id, monthly_dataset, monthly_key, monthly_metric in WEEKLY_MONTHLY_PAIRS:
        left = weekly[(weekly.dataset == weekly_slug) & (weekly.entity_key == item_id)]
        right = monthly[(monthly.dataset == monthly_dataset)
                        & (monthly.entity_key == monthly_key)
                        & (monthly.metric == monthly_metric)]
        pair = f"{weekly_slug}/{item_id} vs {monthly_dataset}/{monthly_key}"
        if left.empty or right.empty:
            rows.append({
                "source": "BDDK", "check": f"weekly vs monthly {pair}", "passed": False,
                "detail": "one side of the registered pair is absent from the corpus",
            })
            continue

        left_by_date = left.set_index("period").value
        # A monthly observation is stamped on the first day of the month it closes.
        right_by_month = right.set_index(pd.to_datetime(right.period)).value

        gaps = []
        for date in aligned:
            month = date.replace(day=1)
            if date not in left_by_date.index or month not in right_by_month.index:
                continue
            weekly_value = float(left_by_date.loc[date])
            monthly_value = float(right_by_month.loc[month])
            if not monthly_value:
                continue
            gaps.append((date.date(), 100 * abs(weekly_value - monthly_value) / abs(monthly_value)))

        if not gaps:
            rows.append({
                "source": "BDDK", "check": f"weekly vs monthly {pair}", "passed": False,
                "detail": "no week of the corpus lands on a month end for this pair",
            })
            continue

        worst_date, worst = max(gaps, key=lambda item: item[1])
        rows.append({
            "source": "BDDK",
            "check": f"weekly vs monthly {pair}",
            "passed": worst <= WEEKLY_MONTHLY_TOLERANCE_PCT,
            "detail": (
                f"{len(gaps)} month-end week(s); max divergence {worst:.4f}% at {worst_date}, "
                f"median {sorted(g for _, g in gaps)[len(gaps) // 2]:.4f}% "
                f"(ceiling {WEEKLY_MONTHLY_TOLERANCE_PCT}%)"
            ),
        })

    report = pd.DataFrame(rows)
    failed = report[~report.passed]
    if not failed.empty:
        raise ValidationError(
            "Weekly/monthly cross-validation FAILED:\n"
            + failed[["check", "detail"]].to_string(index=False)
        )
    return report


def run_weekly_validations(observations: pd.DataFrame, table_slug: str) -> pd.DataFrame:
    """Run every weekly check for one table; raise if any fails."""
    active = observations[observations.retired_on.isna()]
    results = (check_weekly_continuity(observations, table_slug)
               + check_identities(active, table_slug,
                                  tolerance=WEEKLY_TOLERANCE, relative=WEEKLY_RELATIVE)
               + check_superseded_series(observations, table_slug))

    report = pd.DataFrame.from_records(results) if results else pd.DataFrame(
        columns=["dataset", "check", "entity_key", "passed", "detail"]
    )
    failures = report[~report["passed"]] if len(report) else report
    if len(failures):
        lines = [f"  {r.check} {r.entity_key}: {r.detail}" for r in failures.itertuples()]
        raise ValidationError(
            f"{table_slug}: {len(failures)} weekly validation(s) failed:\n" + "\n".join(lines)
        )
    return report
