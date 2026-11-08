import asyncio
import json
from datetime import date, timedelta
from unittest.mock import AsyncMock

import numpy as np
import pandas as pd
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from trading212_mcp.market.models import MarketError
from trading212_mcp.quantitative.analytics import portfolio_statistics
from trading212_mcp.quantitative.candidates import analyse_candidates
from trading212_mcp.tools.research import register_research_tools


async def test_single_candidate_new_capital_weights_fx_and_risk_consistency(
    service, trading, provider
):
    result = await analyse_candidates(
        service, trading, ["BBB"], 200, use_historical_mean_returns=True
    )
    current = result["current_portfolio"]
    assert current["holdings_value_gbp"] == 800
    assert current["weights"] == {"AAA": 1}
    after = result["candidates"][0]["analysis"]
    assert after["post_investment_holdings_value_gbp"] == 1000
    assert after["post_investment_weights"] == {"AAA": 0.8, "BBB": 0.2}
    assert after["analysed_weights"] == after["post_investment_weights"]
    returns = (
        pd.DataFrame(
            {s: provider.series[s] * 0.8 for s in ["AAA", "BBB", "SPY"]},
            index=provider.dates,
        )
        .pct_change(fill_method=None)
        .dropna()
    )
    expected = portfolio_statistics(
        returns[["AAA", "BBB"]], {"AAA": 0.8, "BBB": 0.2}, returns["SPY"]
    )
    assert after["post_investment_metrics"]["risk_metrics"] == pytest.approx(
        expected["risk_metrics"]
    )
    assert after["post_investment_metrics"]["beta"] == pytest.approx(
        expected["benchmark_comparison"]["beta"]
    )
    assert after["change_in_concentration"]["herfindahl_index"] == pytest.approx(-0.32)
    assert after["change_in_risk_metrics"]["annualised_volatility"] == pytest.approx(
        after["post_investment_metrics"]["risk_metrics"]["annualised_volatility"]
        - current["metrics"]["risk_metrics"]["annualised_volatility"]
    )
    json.dumps(result, allow_nan=False)


async def test_existing_holding_does_not_create_new_position(service, trading):
    result = await analyse_candidates(service, trading, ["AAA"], 200)
    after = result["candidates"][0]["analysis"]
    assert after["post_investment_weights"] == {"AAA": 1}
    assert after["change_in_concentration"]["herfindahl_index"] == 0
    assert after["change_in_risk_metrics"]["annualised_volatility"] == pytest.approx(0)
    assert after["correlation_with_existing_portfolio"] == pytest.approx(1)
    assert after["marginal_risk"][
        "funded_volatility_derivative_per_additional_gbp"
    ] == pytest.approx(0, abs=1e-12)
    assert after["post_investment_metrics"]["risk_metrics"]["sharpe_ratio"] is None
    assert after["change_in_risk_metrics"]["sharpe_ratio"] is None


async def test_multiple_independent_candidates_and_joint_allocation(service, trading):
    result = await analyse_candidates(
        service,
        trading,
        ["AAA", "BBB", "SPY"],
        200,
        allocations={"AAA": 0.25, "BBB": 0.75},
        compare_cash=True,
    )
    assert len(result["candidates"]) == 3
    assert all(c["analysis"]["investment_gbp"] == 200 for c in result["candidates"])
    joint = result["allocated_scenario"]
    assert joint["purchases_gbp"] == {"AAA": 50, "BBB": 150}
    assert joint["post_investment_weights"] == {"AAA": 0.85, "BBB": 0.15}
    cash = result["cash_comparison"]
    assert cash["post_investment_weights"] == {"AAA": 0.8, "__cash_gbp__": 0.2}
    assert cash["post_investment_metrics"]["risk_metrics"][
        "annualised_volatility"
    ] == pytest.approx(
        result["current_portfolio"]["metrics"]["risk_metrics"]["annualised_volatility"]
        * 0.8
    )
    assert "hold_cash" not in result["comparison"]["lowest_resulting_volatility"]
    assert result["comparison"]["composite_score"] is None


async def test_zero_value_portfolio(service, trading):
    snapshot = await trading.get_positions()
    snapshot.positions = []
    trading.get_positions = AsyncMock(return_value=snapshot)
    result = await analyse_candidates(service, trading, ["BBB"], 500, compare_cash=True)
    assert result["current_portfolio"]["weights"] == {}
    assert result["current_portfolio"]["metrics"] is None
    after = result["candidates"][0]["analysis"]
    assert after["post_investment_weights"] == {"BBB": 1}
    assert after["post_investment_holdings_value_gbp"] == 500
    assert after["correlation_with_existing_portfolio"] is None
    assert after["change_in_risk_metrics"]["annualised_volatility"] is None
    assert (
        result["cash_comparison"]["post_investment_metrics"]["risk_metrics"][
            "annualised_volatility"
        ]
        == 0
    )
    json.dumps(result, allow_nan=False)


async def test_missing_fundamentals_does_not_remove_price_analysis(
    service, trading, provider
):
    provider.fundamentals = AsyncMock(side_effect=MarketError("No annual statements"))
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    row = result["candidates"][0]
    assert row["fundamentals"] is None
    assert row["analysis"]["post_investment_metrics"] is not None
    assert row["missing_fundamentals"] == ["fundamentals"]
    assert any("Fundamentals unavailable" in w for w in row["warnings"])


async def test_insufficient_history_does_not_redistribute_allocated_funds(
    service, trading, provider
):
    original = provider.history

    async def history(company, start, end):
        result = await original(company, start, end)
        if company.symbol == "BBB":
            result.prices = result.prices[-10:]
        return result

    provider.history = history
    result = await analyse_candidates(
        service, trading, ["AAA", "BBB"], 200, allocations={"AAA": 0.5, "BBB": 0.5}
    )
    assert result["candidates"][0]["analysis"] is not None
    assert result["candidates"][1]["analysis"] is None
    assert result["allocated_scenario"] is None
    assert result["comparison"]["lowest_resulting_volatility"] == ["AAA"]
    assert result["data_quality"]["common_observations"] == 299


async def test_candidates_use_identical_common_dates(service, trading, provider):
    original = provider.history

    async def history(company, start, end):
        result = await original(company, start, end)
        if company.symbol == "BBB":
            result.prices = result.prices[-100:]
        return result

    provider.history = history
    result = await analyse_candidates(service, trading, ["AAA", "BBB"], 200)
    assert result["data_quality"]["common_observations"] == 99
    assert result["current_portfolio"]["metrics"]["risk_metrics"]["observations"] == 99
    assert all(
        c["analysis"]["post_investment_metrics"]["risk_metrics"]["observations"] == 99
        for c in result["candidates"]
    )


async def test_partial_portfolio_keeps_actual_and_analysed_weights_separate(
    service, trading, provider
):
    snapshot = await trading.get_positions()
    missing = snapshot.positions[0].model_copy(deep=True)
    missing.instrument.isin = "UNKNOWN"
    missing.instrument.ticker = "UNKNOWN_US_EQ"
    missing.walletImpact.currentValue = 1000
    snapshot.positions.append(missing)
    trading.get_positions = AsyncMock(return_value=snapshot)
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    assert result["data_quality"]["included_holdings_value_fraction"] == 0.5
    assert result["comparison"]["scope"] == "covered_subset_only"
    assert result["candidates"][0]["status"] == "partial"
    after = result["candidates"][0]["analysis"]
    assert after["post_investment_weights"]["BBB"] == pytest.approx(200 / 1800)
    assert after["analysed_weights"]["BBB"] == 0.2
    assert after["analysed_value_fraction"] == pytest.approx(1000 / 1800)
    assert sum(after["post_investment_weights"].values()) == pytest.approx(1)
    assert result["data_quality"]["excluded_holdings"]


async def test_no_covered_holdings_withholds_portfolio_risk(service, trading):
    snapshot = await trading.get_positions()
    snapshot.positions[0].instrument.isin = "UNKNOWN"
    trading.get_positions = AsyncMock(return_value=snapshot)
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    assert result["current_portfolio"]["metrics"] is None
    assert result["candidates"][0]["analysis"]["post_investment_metrics"] is None
    assert result["comparison"]["lowest_resulting_volatility"] == []


@pytest.mark.parametrize("amount", [-1, 0, float("nan"), float("inf"), True])
async def test_invalid_amount_rejected_before_network(service, amount):
    trading = AsyncMock()
    with pytest.raises(MarketError, match="amount"):
        await analyse_candidates(service, trading, ["AAA"], amount)
    trading.get_positions.assert_not_called()


@pytest.mark.parametrize(
    "allocation",
    [
        {},
        {"AAA": 0.2},
        {"AAA": -1, "BBB": 2},
        {"NO": 1},
        {"AAA": float("nan")},
        {"AAA": True},
    ],
)
async def test_invalid_allocation_rejected(service, trading, allocation):
    with pytest.raises(MarketError, match="Allocations"):
        await analyse_candidates(
            service, trading, ["AAA", "BBB"], 200, allocations=allocation
        )


async def test_gbp_account_does_not_convert_current_holdings(service, trading):
    account = await trading.get_account_summary()
    account.account.currency = "GBP"
    positions = await trading.get_positions()
    positions.positions[0].walletImpact.currency = "GBP"
    trading.get_account_summary = AsyncMock(return_value=account)
    trading.get_positions = AsyncMock(return_value=positions)
    result = await analyse_candidates(service, trading, ["BBB"], 1000)
    assert result["current_portfolio"]["holdings_value_gbp"] == 1000
    assert result["candidates"][0]["analysis"]["post_investment_weights"] == {
        "AAA": 0.5,
        "BBB": 0.5,
    }
    assert result["current_portfolio"]["valuation_fx"]["rate"] == 1


async def test_sector_changes_and_period_matched_quality_leaders(
    service, trading, provider
):
    provider.companies["BBB"].sector = "Healthcare"
    original = provider.fundamentals

    async def fundamentals(company):
        result = await original(company)
        result.statement_date = date(2025, 12, 31)
        result.statement_currency = "USD"
        return result

    provider.fundamentals = fundamentals
    result = await analyse_candidates(service, trading, ["AAA", "BBB"], 200)
    after = result["candidates"][1]["analysis"]
    assert after["sector_exposure"] == {"Technology": 0.8, "Healthcare": 0.2}
    assert after["change_in_sector_exposure"]["Technology"] == pytest.approx(-0.2)
    assert result["comparison"]["financial_quality_metric_leaders"][0]["leaders"] == [
        "AAA"
    ]
    assert result["candidates"][0]["fundamentals"]["pe"] == 10


async def test_fundamental_periods_are_not_pooled(service, trading, provider):
    original = provider.fundamentals

    async def fundamentals(company):
        result = await original(company)
        result.statement_currency = "USD"
        result.statement_date = (
            date(2025, 12, 31) if company.symbol == "AAA" else date(2026, 1, 31)
        )
        return result

    provider.fundamentals = fundamentals
    result = await analyse_candidates(service, trading, ["AAA", "BBB"], 200)
    assert result["comparison"]["financial_quality_metric_leaders"] == []


async def test_discoverable_and_validated_mcp_schema(service, trading):
    server = FastMCP("candidate test")
    register_research_tools(server, lambda: trading, lambda: service)
    tool = next(
        t for t in await server.list_tools() if t.name == "analyse_candidate_investment"
    )
    assert tool.annotations.readOnlyHint
    assert "ctx" not in tool.inputSchema["properties"]
    assert tool.outputSchema
    _, result = await server.call_tool(
        "analyse_candidate_investment",
        {"tickers": ["BBB"], "investment_amount_gbp": 200, "response_mode": "full"},
    )
    assert result["candidates"][0]["analysis"] is not None
    for amount in [-1, 0, True, float("nan")]:
        with pytest.raises(ToolError):
            await server.call_tool(
                "analyse_candidate_investment",
                {
                    "tickers": ["BBB"],
                    "investment_amount_gbp": amount,
                    "response_mode": "full",
                },
            )


async def test_timeout_preserves_full_weights_and_partial_results(
    service, trading, provider
):
    original = provider.fundamentals

    async def fundamentals(company):
        if company.symbol == "BBB":
            await asyncio.Event().wait()
        return await original(company)

    provider.fundamentals = fundamentals
    service.settings.risk_timeout_seconds = 0.1
    result = await asyncio.wait_for(
        analyse_candidates(service, trading, ["BBB"], 200), 1
    )
    assert result["candidates"][0]["analysis"]["post_investment_metrics"] is not None
    assert any("budget" in w for w in result["candidates"][0]["warnings"])


async def test_stale_account_valuation_fx_fails_closed(service, trading, provider):
    original = provider.fx

    async def fx(source, target, start, end):
        result = await original(source, target, start, end)
        result.prices = [
            p.model_copy(update={"date": end - timedelta(days=8)})
            for p in result.prices[:1]
        ]
        return result

    provider.fx = fx
    with pytest.raises(MarketError, match="valuation FX"):
        await analyse_candidates(service, trading, ["AAA"], 200)


async def test_funded_marginal_volatility_matches_numerical_derivative(
    service, trading
):
    lower = await analyse_candidates(service, trading, ["BBB"], 199.99)
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    upper = await analyse_candidates(service, trading, ["BBB"], 200.01)

    def volatility(data):
        return data["candidates"][0]["analysis"]["post_investment_metrics"][
            "risk_metrics"
        ]["annualised_volatility"]

    expected = (volatility(upper) - volatility(lower)) / 0.02
    assert result["candidates"][0]["analysis"]["marginal_risk"][
        "funded_volatility_derivative_per_additional_gbp"
    ] == pytest.approx(expected, abs=1e-10)


async def test_alias_with_same_verified_isin_is_one_position(
    service, trading, provider
):
    provider.companies["AAA.L"] = provider.companies["AAA"].model_copy(
        update={"symbol": "AAA.L", "currency": "GBP"}
    )
    provider.series["AAA.L"] = provider.series["AAA"] * 0.8
    service.settings.instrument_overrides["AAA_US_EQ"] = "AAA"
    result = await analyse_candidates(service, trading, ["AAA.L"], 200)
    candidate = result["candidates"][0]
    assert candidate["analysis_symbol"] == "AAA"
    assert candidate["analysis"]["post_investment_weights"] == {"AAA": 1}
    assert any("Same verified ISIN" in warning for warning in candidate["warnings"])


async def test_two_candidate_aliases_coalesce_in_allocated_scenario(
    service, trading, provider
):
    provider.companies["BBB.L"] = provider.companies["BBB"].model_copy(
        update={"symbol": "BBB.L", "currency": "GBP"}
    )
    provider.series["BBB.L"] = provider.series["BBB"] * 0.8
    result = await analyse_candidates(
        service, trading, ["BBB", "BBB.L"], 200, allocations={"BBB": 0.5, "BBB.L": 0.5}
    )
    assert result["allocated_scenario"]["purchases_gbp"] == {"BBB": 200}
    assert result["allocated_scenario"]["post_investment_weights"] == {
        "AAA": 0.8,
        "BBB": 0.2,
    }


async def test_missing_benchmark_does_not_discard_other_risk_estimates(
    service, trading, provider
):
    provider.failed.add("SPY")
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    metrics = result["candidates"][0]["analysis"]["post_investment_metrics"]
    assert metrics["beta"] is None
    assert metrics["risk_metrics"]["annualised_volatility"] is not None
    assert result["data_quality"]["benchmark"]["available"] is False


async def test_missing_and_unsupported_candidates_do_not_rank(
    service, trading, provider
):
    provider.companies["OPTION"] = provider.companies["BBB"].model_copy(
        update={"symbol": "OPTION", "quote_type": "OPTION"}
    )
    result = await analyse_candidates(
        service, trading, ["MISSING", "OPTION", "BBB"], 200
    )
    assert [c["status"] for c in result["candidates"]] == [
        "unavailable",
        "unavailable",
        "available",
    ]
    assert result["comparison"]["lowest_resulting_volatility"] == ["BBB"]


async def test_sparse_incompatible_calendars_withhold_all_comparisons(
    service, trading, provider
):
    original = provider.history

    async def history(company, start, end):
        result = await original(company, start, end)
        if company.symbol == "AAA":
            result.prices = result.prices[::2]
        elif company.symbol == "BBB":
            result.prices = result.prices[1::2]
        return result

    provider.history = history
    result = await analyse_candidates(service, trading, ["AAA", "BBB"], 200)
    assert result["data_quality"]["common_observations"] == 0
    assert all(
        c["analysis"]["post_investment_metrics"] is None for c in result["candidates"]
    )
    assert result["comparison"]["lowest_resulting_volatility"] == []


async def test_cash_return_assumption_is_applied_on_same_calendar(service, trading):
    result = await analyse_candidates(
        service,
        trading,
        ["BBB"],
        200,
        compare_cash=True,
        annual_cash_return=0.05,
        use_historical_mean_returns=True,
    )
    cash = result["cash_comparison"]
    assert cash["post_investment_metrics"]["risk_metrics"][
        "annualised_volatility"
    ] == pytest.approx(
        result["current_portfolio"]["metrics"]["risk_metrics"]["annualised_volatility"]
        * 0.8
    )
    assert result["assumptions"]["annual_cash_return"] == 0.05
    assert cash["post_investment_metrics"]["risk_metrics"]["sharpe_ratio"] is not None


async def test_daily_currency_moves_are_included_in_risk(service, trading, provider):
    original = provider.fx
    rates = 0.8 + 0.005 * np.sin(np.arange(300) * 0.2)

    async def fx(source, target, start, end):
        result = await original(source, target, start, end)
        result.prices = [
            p.model_copy(update={"close": float(rate)})
            for p, rate in zip(result.prices, rates, strict=True)
        ]
        return result

    provider.fx = fx
    result = await analyse_candidates(
        service, trading, ["BBB"], 200, use_historical_mean_returns=True
    )
    value = 1000 * rates[-1]
    weights = {"AAA": value / (value + 200), "BBB": 200 / (value + 200)}
    frame = (
        pd.DataFrame(
            {s: provider.series[s] * rates for s in ["AAA", "BBB", "SPY"]},
            index=provider.dates,
        )
        .pct_change(fill_method=None)
        .dropna()
    )
    expected = portfolio_statistics(frame[["AAA", "BBB"]], weights, frame["SPY"])
    actual = result["candidates"][0]["analysis"]
    assert result["current_portfolio"]["holdings_value_gbp"] == pytest.approx(value)
    assert actual["post_investment_metrics"]["risk_metrics"] == pytest.approx(
        expected["risk_metrics"]
    )
    assert actual["post_investment_metrics"]["beta"] == pytest.approx(
        expected["benchmark_comparison"]["beta"]
    )


async def test_empty_portfolio_does_not_need_account_valuation_fx(
    service, trading, provider
):
    snapshot = await trading.get_positions()
    snapshot.positions = []
    trading.get_positions = AsyncMock(return_value=snapshot)
    original = provider.fx

    async def fx(source, target, start, end):
        if start == end - timedelta(days=7):
            raise AssertionError("Zero account needs no valuation FX")
        return await original(source, target, start, end)

    provider.fx = fx
    result = await analyse_candidates(service, trading, ["BBB"], 200)
    assert result["current_portfolio"]["valuation_fx"]["rate"] is None
    assert result["candidates"][0]["analysis"]["post_investment_weights"] == {"BBB": 1}


async def test_full_mcp_call_with_all_scenario_options(service, trading):
    server = FastMCP("full candidate call")
    register_research_tools(server, lambda: trading, lambda: service)
    _, result = await server.call_tool(
        "analyse_candidate_investment",
        {
            "tickers": ["AAA", "BBB", "SPY"],
            "investment_amount_gbp": 2000,
            "compare_cash": True,
            "allocations": {"AAA": 0.5, "BBB": 0.5},
            "use_historical_mean_returns": True,
            "response_mode": "full",
        },
    )
    assert result["allocated_scenario"]["investment_gbp"] == 2000
    assert result["cash_comparison"] is not None
    for bad in [{"AAA": True}, {"AAA": 1.1}, {"AAA": "1"}]:
        with pytest.raises(ToolError):
            await server.call_tool(
                "analyse_candidate_investment",
                {
                    "tickers": ["AAA"],
                    "investment_amount_gbp": 100,
                    "allocations": bad,
                    "response_mode": "full",
                },
            )
