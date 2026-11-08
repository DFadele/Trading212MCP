from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from trading212_mcp.market.models import AnnualFinancials, Company, Fundamentals
from trading212_mcp.quantitative.factors import financial_features, momentum_features


@pytest.fixture
def annual_financials():
    history = [
        AnnualFinancials(
            end=date(year, 12, 31),
            start=date(year, 1, 1),
            filed=date(year + 1, 2, 15),
            currency="USD",
            revenue=100 * 1.1**i,
            net_income=10 + i * 2,
            operating_income=20 + i * 4,
            operating_cash_flow=15 + i * 3,
            free_cash_flow=10 + i * 2,
            total_assets=200 + i * 10,
            equity=100 + i * 10,
            total_debt=80 - i * 10,
        )
        for i, year in enumerate(range(2022, 2026))
    ]
    return Fundamentals(
        source="test_annual",
        retrieved_at=datetime(2026, 11, 7, tzinfo=timezone.utc),
        statement_date=date(2025, 12, 31),
        statement_currency="USD",
        annual_history=history,
        pe=20,
        ev_to_ebitda=12,
    )


@pytest.fixture
def dollar_company():
    return Company(symbol="X", name="Test", currency="USD", market_cap=1000)


def test_dated_annual_features_known_examples(annual_financials, dollar_company):
    values, metadata, warnings = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["revenue_cagr"] == pytest.approx(1.331 ** (365.25 / 1096) - 1)
    assert metadata["revenue_cagr"]["years"] == pytest.approx(1096 / 365.25)
    assert values["free_cash_flow_growth"] == pytest.approx(16 / 14 - 1)
    assert values["margin_expansion"] == pytest.approx(32 / 133.1 - 28 / 121)
    assert values["roe"] == pytest.approx(16 / 125)
    assert values["operating_profitability"] == pytest.approx(32 / 225)
    assert values["cash_conversion"] == pytest.approx(24 / 16)
    assert values["debt_to_assets"] == pytest.approx(50 / 230)
    assert values["deleveraging"] == pytest.approx(60 / 220 - 50 / 230)
    assert values["financial_stability"] == 1
    assert values["earnings_yield"] == 0.05
    assert values["free_cash_flow_yield"] == 0.016
    assert metadata["earnings_yield"]["periods"] == []
    assert metadata["free_cash_flow_growth"]["periods"][0]["end"] == "2024-12-31"
    assert warnings == []


def test_annual_missing_and_negative_denominators_not_zero(
    annual_financials, dollar_company
):
    annual_financials.annual_history[-2].free_cash_flow = -1
    annual_financials.annual_history[-1].net_income = -10
    annual_financials.annual_history[-1].equity = -1
    annual_financials.annual_history[-1].operating_cash_flow = None
    values, _, _ = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["free_cash_flow_growth"] is None
    assert values["cash_conversion"] is None
    assert values["roe"] is None
    assert values["financial_stability"] is None
    assert values["cash_flow_improvement"] is None


def test_currency_and_period_gaps_withhold_comparisons(
    annual_financials, dollar_company
):
    annual_financials.annual_history[-2].currency = "EUR"
    values, _, _ = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["free_cash_flow_growth"] is None
    assert values["revenue_cagr"] is None
    dollar_company.currency = "EUR"
    values, _, _ = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["free_cash_flow_yield"] is None
    annual_financials.annual_history = annual_financials.annual_history[::3]
    values, _, _ = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["revenue_cagr"] is None


def test_future_filing_and_conflicting_periods_not_used(
    annual_financials, dollar_company
):
    annual_financials.annual_history[-1].filed = date(2027, 1, 1)
    values, _, warnings = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["revenue_cagr"] is None
    assert values["cash_conversion"] is None
    assert warnings
    annual_financials.annual_history.append(
        annual_financials.annual_history[0].model_copy()
    )
    values, _, warnings = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert values["free_cash_flow_growth"] is None
    assert "Conflicting" in warnings[0]


def test_unknown_filing_dates_are_disclosed(annual_financials, dollar_company):
    annual_financials.annual_history[0].filed = None
    _, _, warnings = financial_features(
        annual_financials, dollar_company, date(2026, 11, 7)
    )
    assert "point-in-time" in warnings[0]


def test_momentum_known_endpoints_excludes_recent_month():
    index = pd.bdate_range(end="2026-11-06", periods=253)
    prices = pd.Series(np.arange(100, 353, dtype=float), index=index)
    benchmark = prices * 3
    metrics, metadata = momentum_features(prices, benchmark)
    assert metrics["momentum_6m"] == pytest.approx(352 / 226 - 1)
    assert metrics["momentum_12_1"] == pytest.approx(331 / 100 - 1)
    assert metrics["relative_strength"] == pytest.approx(0)
    assert metadata["relative_strength"]["observations"] == 127
    prices.iloc[-21:] *= 2
    updated, _ = momentum_features(prices)
    assert updated["momentum_12_1"] == metrics["momentum_12_1"]
    assert updated["momentum_6m"] != metrics["momentum_6m"]


def test_short_history_and_unaligned_benchmark_are_missing():
    prices = pd.Series([1.0] * 126, index=pd.bdate_range("2026-01-01", periods=126))
    metrics, _ = momentum_features(prices)
    assert all(v is None for v in metrics.values())
    prices = pd.Series(
        np.arange(1.0, 254), index=pd.bdate_range("2025-01-01", periods=253)
    )
    benchmark = prices.iloc[::3]
    metrics, _ = momentum_features(prices, benchmark)
    assert metrics["relative_strength"] is None
    assert metrics["momentum_12_1"] is not None
