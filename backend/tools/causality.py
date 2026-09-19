"""Deterministic causality analysis between two time series.

The tool tests whether past values of one series add predictive information
about another.

It uses:
- Augmented Dickey-Fuller (ADF) tests for stationarity,
- differencing when necessary,
- BIC-based VAR lag selection,
- bidirectional Granger causality tests,
- lead-lag correlation analysis.

Important:
Granger causality is evidence of predictive precedence. It is NOT proof of
real-world or structural causation.

The LLM never calculates p-values or chooses statistical conclusions itself.
"""

from typing import Dict, Tuple

import numpy as np
import pandas as pd
from statsmodels.tsa.api import VAR
from statsmodels.tsa.stattools import adfuller


MIN_OBSERVATIONS = 24
DEFAULT_ALPHA = 0.05
DEFAULT_MAX_LAG = 6
MAX_DIFFERENCES = 2


def _validate_series(series: pd.Series, name: str) -> pd.Series:
    """Validate and clean one input series."""
    if not isinstance(series, pd.Series):
        raise TypeError(f"{name!r} must be a pandas Series")

    if series.index.duplicated().any():
        raise ValueError(f"{name!r} contains duplicate periods")

    clean = pd.to_numeric(series, errors="coerce")
    clean = clean.replace([np.inf, -np.inf], np.nan)
    clean = clean.sort_index().dropna()

    if clean.empty:
        raise ValueError(f"{name!r} has no numeric observations")

    if clean.nunique() < 2:
        raise ValueError(
            f"{name!r} is constant; causality cannot be tested"
        )

    return clean.astype(float)


def _adf_p_value(series: pd.Series) -> float:
    """Run an Augmented Dickey-Fuller test and return its p-value."""
    if series.nunique() < 2:
        raise ValueError("ADF test cannot be run on a constant series")

    result = adfuller(
        series.to_numpy(dtype=float),
        autolag="AIC",
        result_object=False,
    )

    return float(result[1])


def _stationarize(
    series: pd.Series,
    name: str,
    alpha: float,
) -> Tuple[pd.Series, Dict]:
    """Difference a series until it becomes stationary.

    The original and final ADF p-values plus the differencing order are
    returned so the transformation is auditable.
    """
    original_p = _adf_p_value(series)

    current = series.copy()
    current_p = original_p
    difference_order = 0

    while (
        current_p > alpha
        and difference_order < MAX_DIFFERENCES
    ):
        current = current.diff().dropna()
        difference_order += 1

        if len(current) < MIN_OBSERVATIONS:
            raise ValueError(
                f"{name!r} has too few observations after differencing "
                f"{difference_order} time(s): {len(current)}"
            )

        if current.nunique() < 2:
            raise ValueError(
                f"{name!r} became constant after differencing"
            )

        current_p = _adf_p_value(current)

    if current_p > alpha:
        raise ValueError(
            f"{name!r} remains non-stationary after "
            f"{MAX_DIFFERENCES} differences "
            f"(ADF p={current_p:.4f})"
        )

    return current, {
        "difference_order": difference_order,
        "adf_p_value_before": round(original_p, 6),
        "adf_p_value_after": round(current_p, 6),
        "stationary": True,
    }


def _select_lag(
    frame: pd.DataFrame,
    max_lag: int,
) -> Tuple[int, int]:
    """Select a VAR lag using BIC.

    Small samples cannot support arbitrary numbers of lags, so the maximum
    candidate is reduced automatically when needed.
    """
    candidate_max = min(
        max_lag,
        max(1, len(frame) // 5),
    )

    last_error = None

    for current_max in range(candidate_max, 0, -1):
        try:
            model = VAR(frame)

            selection = model.select_order(
                maxlags=current_max
            )

            selected = selection.selected_orders.get("bic")

            # Lag 0 cannot perform a meaningful Granger test.
            if selected is None or selected < 1:
                selected = 1

            return int(selected), int(current_max)

        except (ValueError, np.linalg.LinAlgError) as exc:
            last_error = exc

    raise ValueError(
        f"could not select a valid VAR lag: {last_error}"
    )


def _lead_lag_correlations(
    cause: pd.Series,
    effect: pd.Series,
    max_lag: int,
) -> Dict:
    """Calculate correlations across leads and lags.

    Sign convention:

    lag = +2:
        cause(t-2) is compared with effect(t)

    Therefore a positive lag means that the candidate cause leads the effect.
    """
    correlations = []

    for lag in range(-max_lag, max_lag + 1):
        shifted_cause = cause.shift(lag)

        aligned = pd.concat(
            [
                shifted_cause.rename("cause"),
                effect.rename("effect"),
            ],
            axis=1,
        ).dropna()

        if len(aligned) < 3:
            correlation = None
        else:
            value = aligned["cause"].corr(
                aligned["effect"]
            )

            correlation = (
                None
                if pd.isna(value)
                else round(float(value), 6)
            )

        correlations.append({
            "lag": lag,
            "correlation": correlation,
        })

    valid = [
        item
        for item in correlations
        if item["correlation"] is not None
    ]

    strongest = (
        max(
            valid,
            key=lambda item: abs(item["correlation"]),
        )
        if valid
        else None
    )

    return {
        "sign_convention":
            "positive lag means cause leads effect",
        "correlations": correlations,
        "strongest": strongest,
    }


def _transformation_name(difference_order: int) -> str:
    """Human/machine-readable description of the series used by the tests."""
    if difference_order == 0:
        return "level"
    if difference_order == 1:
        return "first_difference"
    if difference_order == 2:
        return "second_difference"
    return f"difference_order_{difference_order}"


def analyze_causality(
    cause: pd.Series,
    effect: pd.Series,
    *,
    cause_name: str = "cause",
    effect_name: str = "effect",
    alpha: float = DEFAULT_ALPHA,
    max_lag: int = DEFAULT_MAX_LAG,
) -> dict:
    """Analyse predictive direction between two time series.

    Args:
        cause:
            Candidate predictor series.
        effect:
            Candidate target series.
        cause_name:
            Name used for the candidate predictor in the result.
        effect_name:
            Name used for the target in the result.
        alpha:
            Statistical significance threshold.
        max_lag:
            Maximum lag considered by the model.

    Returns:
        A JSON-serialisable dictionary containing statistical evidence.

    The result must not be interpreted as proof of real-world causation.
    """
    if not 0 < alpha < 1:
        raise ValueError(
            "alpha must be between 0 and 1"
        )

    if max_lag < 1:
        raise ValueError(
            "max_lag must be >= 1"
        )

    cause = _validate_series(
        cause,
        cause_name,
    )

    effect = _validate_series(
        effect,
        effect_name,
    )

    # Only dates present in both series can be compared.
    original = pd.concat(
        [
            cause.rename("cause"),
            effect.rename("effect"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    if len(original) < MIN_OBSERVATIONS:
        raise ValueError(
            f"need at least {MIN_OBSERVATIONS} aligned observations, "
            f"have {len(original)}"
        )

    cause_stationary, cause_info = _stationarize(
        original["cause"],
        cause_name,
        alpha,
    )

    effect_stationary, effect_info = _stationarize(
        original["effect"],
        effect_name,
        alpha,
    )

    stationary = pd.concat(
        [
            cause_stationary.rename("cause"),
            effect_stationary.rename("effect"),
        ],
        axis=1,
        join="inner",
    ).dropna()

    if len(stationary) < MIN_OBSERVATIONS:
        raise ValueError(
            "too few aligned observations remain after stationarity "
            f"transforms: {len(stationary)}"
        )

    selected_lag, considered_max_lag = _select_lag(
        stationary,
        max_lag,
    )

    fitted = VAR(stationary).fit(
        selected_lag
    )

    # H0:
    # past cause values do NOT improve prediction of effect.
    forward_test = fitted.test_causality(
        caused="effect",
        causing=["cause"],
        kind="f",
    )

    # Test the reverse direction as well.
    reverse_test = fitted.test_causality(
        caused="cause",
        causing=["effect"],
        kind="f",
    )

    forward_p = float(
        forward_test.pvalue
    )

    reverse_p = float(
        reverse_test.pvalue
    )

    forward_significant = (
        forward_p < alpha
    )

    reverse_significant = (
        reverse_p < alpha
    )

    if (
        forward_significant
        and not reverse_significant
    ):
        classification = (
            "directional_predictive_evidence"
        )
        verdict = "predictive"

    elif (
        reverse_significant
        and not forward_significant
    ):
        classification = (
            "reverse_predictive_evidence"
        )
        verdict = "reverse_predictive"

    elif (
        forward_significant
        and reverse_significant
    ):
        classification = (
            "bidirectional_predictive_evidence"
        )
        verdict = "bidirectional"

    else:
        classification = (
            "no_predictive_evidence"
        )
        verdict = "not_predictive"

    lead_lag = _lead_lag_correlations(
        stationary["cause"],
        stationary["effect"],
        min(
            max_lag,
            considered_max_lag,
        ),
    )

    strongest = lead_lag["strongest"]

    cause_transform = _transformation_name(
        cause_info["difference_order"]
    )

    effect_transform = _transformation_name(
        effect_info["difference_order"]
    )

    interpretation = (
        f"Granger analysis for {cause_name} and {effect_name} "
        f"selected lag {selected_lag}. "
        f"The effective test used {cause_transform} for {cause_name} "
        f"and {effect_transform} for {effect_name}. "
        f"Forward p={forward_p:.4f}; "
        f"reverse p={reverse_p:.4f}. "
        "This is evidence about predictive precedence, "
        "not proof of real-world causation."
    )

    if strongest is not None:
        interpretation += (
            f" Strongest lead-lag correlation occurs at "
            f"lag {strongest['lag']} "
            f"(r={strongest['correlation']:.4f})."
        )

    return {
        "cause": cause_name,
        "effect": effect_name,

        "period_start":
            original.index.min().strftime("%Y-%m"),
        "period_end":
            original.index.max().strftime("%Y-%m"),

        "n_aligned_original":
            int(len(original)),
        "n_aligned_stationary":
            int(len(stationary)),

        "alpha": float(alpha),

        "stationarity": {
            cause_name: cause_info,
            effect_name: effect_info,
        },

        "effective_test": {
            "cause": {
                "name": cause_name,
                "transformation": cause_transform,
                "difference_order":
                    int(cause_info["difference_order"]),
            },
            "effect": {
                "name": effect_name,
                "transformation": effect_transform,
                "difference_order":
                    int(effect_info["difference_order"]),
            },
        },

        "lag_selection": {
            "method": "VAR BIC",
            "requested_max_lag":
                int(max_lag),
            "considered_max_lag":
                int(considered_max_lag),
            "selected_lag":
                int(selected_lag),
        },

        "forward": {
            "direction":
                f"{cause_name} -> {effect_name}",
            "f_statistic":
                round(
                    float(
                        forward_test.test_statistic
                    ),
                    6,
                ),
            "p_value":
                round(forward_p, 6),
            "significant":
                bool(forward_significant),
        },

        "reverse": {
            "direction":
                f"{effect_name} -> {cause_name}",
            "f_statistic":
                round(
                    float(
                        reverse_test.test_statistic
                    ),
                    6,
                ),
            "p_value":
                round(reverse_p, 6),
            "significant":
                bool(reverse_significant),
        },

        "lead_lag": lead_lag,

        "classification": classification,
        "verdict": verdict,

        "interpretation": interpretation,

        "limitations": [
            "Granger causality measures predictive precedence, not structural causation.",
            "Omitted variables may explain an observed relationship.",
            "Results depend on the available sample and selected lag structure.",
            "Lead-lag correlation is descriptive and is not itself a causality test.",
        ],
    }
