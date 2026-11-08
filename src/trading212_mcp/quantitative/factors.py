"""Dated financial features and adjusted-price momentum; no estimated fundamentals."""

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from trading212_mcp.market.models import AnnualFinancials, Company, Fundamentals
from trading212_mcp.observability import timed_sync

ADVANCED_METRICS = (
    "revenue_cagr",
    "free_cash_flow_growth",
    "margin_expansion",
    "ev_to_ebitda",
    "earnings_yield",
    "free_cash_flow_yield",
    "roe",
    "operating_profitability",
    "cash_conversion",
    "debt_to_assets",
    "profitability_improvement",
    "cash_flow_improvement",
    "deleveraging",
    "financial_stability",
)


def ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    value = numerator / denominator
    return value if np.isfinite(value) else None


@timed_sync("quantitative_calculation")
def financial_features(
    fundamentals: Fundamentals,
    company: Company,
    as_of: date,
) -> tuple[dict[str, float | None], dict[str, Any], list[str]]:
    f = fundamentals
    metrics: dict[str, float | None] = dict.fromkeys(ADVANCED_METRICS)
    metadata: dict[str, Any] = {}
    warnings: list[str] = []
    history = sorted(
        (
            r
            for r in f.annual_history
            if r.end <= as_of and (r.filed is None or r.filed <= as_of)
        ),
        key=lambda r: r.end,
        reverse=True,
    )
    # A conflicting duplicate fiscal end cannot safely define a growth comparison.
    if len({r.end for r in history}) != len(history):
        history = []
        warnings.append("Conflicting annual periods; annual-history features withheld.")
    current = history[0] if history else None
    if current and (
        f.statement_date != current.end or f.statement_currency != current.currency
    ):
        current = None
        warnings.append(
            "Annual history does not match the selected statement end/currency; history features withheld."
        )
    prior = history[1] if current and len(history) > 1 else None
    if (
        prior
        and current
        and (
            prior.currency != current.currency
            or not 300 <= (current.end - prior.end).days <= 430
        )
    ):
        prior = None

    def set_metric(
        name: str,
        value: float | None,
        basis: str = "annual",
        periods: list[AnnualFinancials] | None = None,
    ) -> None:
        metrics[name] = value if value is not None and np.isfinite(value) else None
        used = periods if periods is not None else ([current] if current else [])
        metadata[name] = {
            "source": f.source,
            "basis": basis,
            "periods": [
                {
                    "start": r.start.isoformat() if r.start else None,
                    "end": r.end.isoformat(),
                    "filed": r.filed.isoformat() if r.filed else None,
                    "currency": r.currency,
                }
                for r in used
            ],
            "available": metrics[name] is not None,
        }

    set_metric(
        "ev_to_ebitda",
        f.ev_to_ebitda if f.ev_to_ebitda is not None and f.ev_to_ebitda > 0 else None,
        "vendor_snapshot",
        [],
    )
    set_metric("earnings_yield", ratio(1.0, f.pe), "reciprocal_positive_vendor_pe", [])
    if current:
        if prior:
            used = [prior, current]
            fcf_ratio = ratio(current.free_cash_flow, prior.free_cash_flow)
            set_metric(
                "free_cash_flow_growth",
                fcf_ratio - 1 if fcf_ratio is not None else None,
                periods=used,
            )
            for name, field, denominator in (
                ("margin_expansion", "operating_income", "revenue"),
                ("profitability_improvement", "net_income", "total_assets"),
                ("cash_flow_improvement", "operating_cash_flow", "revenue"),
                ("deleveraging", "total_debt", "total_assets"),
            ):
                latest = ratio(getattr(current, field), getattr(current, denominator))
                previous = ratio(getattr(prior, field), getattr(prior, denominator))
                change = (
                    latest - previous
                    if latest is not None and previous is not None
                    else None
                )
                set_metric(
                    name,
                    -change
                    if name == "deleveraging" and change is not None
                    else change,
                    periods=used,
                )
            average_equity = (
                (current.equity + prior.equity) / 2
                if current.equity is not None
                and prior.equity is not None
                and current.equity > 0
                and prior.equity > 0
                else None
            )
            average_assets = (
                (current.total_assets + prior.total_assets) / 2
                if current.total_assets is not None
                and prior.total_assets is not None
                and current.total_assets > 0
                and prior.total_assets > 0
                else None
            )
            set_metric("roe", ratio(current.net_income, average_equity), periods=used)
            set_metric(
                "operating_profitability",
                ratio(current.operating_income, average_assets),
                periods=used,
            )
        set_metric(
            "cash_conversion", ratio(current.operating_cash_flow, current.net_income)
        )
        set_metric("debt_to_assets", ratio(current.total_debt, current.total_assets))
        comparable = [current]
        for report in history[1:]:
            if (
                report.currency != current.currency
                or not 300 <= (comparable[-1].end - report.end).days <= 430
            ):
                break
            comparable.append(report)
        old = next(
            (
                r
                for r in comparable[1:]
                if 2.5 <= (current.end - r.end).days / 365.25 <= 3.5
            ),
            None,
        )
        if (
            old
            and current.revenue is not None
            and old.revenue is not None
            and current.revenue > 0
            and old.revenue > 0
        ):
            years = (current.end - old.end).days / 365.25
            set_metric(
                "revenue_cagr",
                (current.revenue / old.revenue) ** (1 / years) - 1,
                "approximately_three_year_cagr_actual_elapsed_years",
                [old, current],
            )
            metadata["revenue_cagr"]["years"] = years
        stable = comparable[:3]
        if len(stable) == 3 and all(
            r.net_income is not None and r.operating_cash_flow is not None
            for r in stable
        ):
            set_metric(
                "financial_stability",
                sum(
                    r.net_income > 0 and r.operating_cash_flow > 0
                    for r in stable
                    if r.net_income is not None and r.operating_cash_flow is not None
                )
                / 3,
                "fraction_of_three_annual_periods_profitable_with_positive_operating_cash_flow",
                stable,
            )
        # Quote market cap is major currency units, never multiply it by a minor-unit share price.
        quote_currency = "GBP" if company.currency == "GBp" else company.currency
        if current.currency == quote_currency:
            set_metric(
                "free_cash_flow_yield",
                ratio(current.free_cash_flow, company.market_cap),
                "latest_annual_fcf_over_current_market_cap",
            )
            metadata["free_cash_flow_yield"]["market_cap_source"] = (
                "configured_price_provider_profile"
            )
            metadata["free_cash_flow_yield"]["market_cap_currency"] = quote_currency
        if any(r.filed is None for r in history):
            warnings.append(
                "Annual filing availability dates are unknown for some vendor records; current screening only, not a point-in-time historical backtest."
            )
    for name in ADVANCED_METRICS:
        metadata.setdefault(
            name, {"source": f.source, "basis": "unavailable", "available": False}
        )
    return metrics, metadata, warnings


@timed_sync("quantitative_calculation")
def momentum_features(
    prices: pd.Series, benchmark: pd.Series | None = None
) -> tuple[dict[str, float | None], dict[str, Any]]:
    values = prices.dropna().sort_index()
    output: dict[str, float | None] = {
        "momentum_6m": None,
        "momentum_12_1": None,
        "relative_strength": None,
    }
    metadata: dict[str, Any] = {}
    for name, begin, end in (("momentum_6m", -127, -1), ("momentum_12_1", -253, -22)):
        if len(values) >= -begin:
            output[name] = float(values.iloc[end] / values.iloc[begin] - 1)
            metadata[name] = {
                "start": values.index[begin].date().isoformat(),
                "end": values.index[end].date().isoformat(),
                "observations": -begin,
                "basis": "adjusted_close_common_base_currency",
            }
    if benchmark is not None:
        aligned = (
            pd.concat({"asset": values, "benchmark": benchmark}, axis=1, sort=False)
            .dropna()
            .sort_index()
        )
        if len(aligned) >= 127:
            window = aligned.iloc[-127:]
            growth = window.iloc[-1] / window.iloc[0]
            output["relative_strength"] = float(
                growth["asset"] / growth["benchmark"] - 1
            )
            metadata["relative_strength"] = {
                "start": window.index[0].date().isoformat(),
                "end": window.index[-1].date().isoformat(),
                "observations": 127,
                "basis": "six_month_relative_wealth_ratio_on_common_observed_dates",
            }
    output = {
        name: value if value is not None and np.isfinite(value) else None
        for name, value in output.items()
    }
    return output, metadata
