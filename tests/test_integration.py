"""Explicit opt-in only. Consumes real FMP quota; no Trading 212 requests."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.fmp import FMPProvider


@pytest.mark.skipif(
    os.environ.get("RUN_MARKET_INTEGRATION") != "1",
    reason="Live provider smoke check is opt-in",
)
async def test_live_fmp_profile_and_adjusted_prices():
    settings = MarketSettings()
    if settings.fmp_api_key is None or not settings.fmp_api_key.get_secret_value():
        pytest.skip("MARKET_FMP_API_KEY is not configured")
    provider = FMPProvider(settings)
    try:
        company = await provider.profile("AAPL")
        end = datetime.now(timezone.utc).date()
        history = await provider.history(company, end - timedelta(days=30), end)
        assert company.symbol == "AAPL" and company.currency
        assert history.price_basis == "split_and_dividend_adjusted"
        assert len(history.prices) > 1
    finally:
        await provider.aclose()


@pytest.mark.skipif(
    os.environ.get("RUN_YAHOO_SEC_INTEGRATION") != "1",
    reason="Real Yahoo/SEC requests require explicit opt-in",
)
async def test_live_yahoo_sec_five_instruments_and_screening():
    """Success requires actual prices and SEC responses, not an empty tool result."""
    import math

    from mcp.server.fastmcp import FastMCP

    from trading212_mcp.market.factory import create_provider
    from trading212_mcp.market.models import MarketError
    from trading212_mcp.market.service import MarketService
    from trading212_mcp.tools.research import register_research_tools

    settings = MarketSettings()
    assert settings.sec_user_agent, "Configure a real SEC contact before this live test"
    settings.provider = "yfinance"
    settings.fundamentals_source = "sec"
    provider = create_provider(settings)
    service = MarketService(provider, settings)
    server = FastMCP("Live public research verification")

    from unittest.mock import Mock

    from trading212_mcp.trading212.client import Trading212Client

    public_client = Mock(spec=Trading212Client)
    public_client.get_positions.side_effect = AssertionError(
        "Unexpected positions request"
    )
    public_client.get_account_summary.side_effect = AssertionError(
        "Unexpected account request"
    )

    def no_account():
        return public_client

    register_research_tools(server, no_account, lambda: service)
    ciks = {"NVDA": 1045810, "GOOGL": 1652044, "APH": 820313}
    try:
        for symbol in ["NVDA", "GOOGL", "APH", "ASML.AS", "SPY"]:
            _, result = await server.call_tool(
                "get_market_data",
                {
                    "tickers": [symbol],
                    "lookback_years": 3,
                    "include_fundamentals": True,
                    "include_series": True,
                    "base_currency": "GBP",
                    "response_mode": "full",
                },
            )
            asset = result["assets"][0]
            assert asset["company"]["symbol"] == symbol
            assert asset["company"]["currency"] == (
                "EUR" if symbol == "ASML.AS" else "USD"
            )
            assert asset["currency"] == "GBP"
            assert asset["price_basis"] == "split_and_dividend_adjusted"
            assert asset["observations"] > 650
            assert (
                datetime.now(timezone.utc).date()
                - datetime.fromisoformat(asset["end"]).date()
            ).days <= 7
            # Cross-check GBP conversion against the actual raw SDK frames,
            # independently of converted_history's pandas alignment.
            from trading212_mcp.market.factory import FundamentalsProvider
            from trading212_mcp.market.yahoo import YFinanceProvider

            assert isinstance(provider, FundamentalsProvider)
            assert isinstance(provider.base, YFinanceProvider)
            start = datetime.fromisoformat(result["requested_period"]["start"]).date()
            end = datetime.fromisoformat(result["requested_period"]["end"]).date()
            raw_history = provider.base._cache.get(
                f"history:{symbol}:{start}:{end}"
            ).rows[0]["value"]
            native = asset["company"]["currency"]
            fx_frame = provider.base._cache.get(
                f"fx:{native}GBP=X:{start - timedelta(days=4)}:{end}"
            ).rows[0]["value"]
            adjusted = {
                day.date(): float(value)
                for day, value in raw_history["Adj Close"].items()
            }
            rates = {
                day.date(): float(value) for day, value in fx_frame["Close"].items()
            }
            for point in asset["series"]:
                day = datetime.fromisoformat(point["date"]).date()
                earlier = [d for d in rates if d <= day and (day - d).days <= 4]
                expected = adjusted[day] * rates[max(earlier)] if earlier else None
                assert (
                    point["adjusted_close"] == pytest.approx(expected)
                    if expected is not None
                    else point["adjusted_close"] is None
                )
            points = asset["series"]
            valid = [
                p["adjusted_close"] for p in points if p["adjusted_close"] is not None
            ]
            assert all(math.isfinite(v) and v > 0 for v in valid)
            assert asset["return_summary"]["cumulative_return"] == pytest.approx(
                valid[-1] / valid[0] - 1
            )
            for previous, current in zip(points, points[1:]):
                if current["return"] is not None:
                    assert current["return"] == pytest.approx(
                        current["adjusted_close"] / previous["adjusted_close"] - 1
                    )
            if symbol in ciks:
                f = asset["fundamentals"]
                assert f["source"] == "sec_edgar_annual"
                assert f["statement_currency"] == "USD"
                assert all(
                    f[k] is not None
                    for k in [
                        "revenue",
                        "net_income",
                        "total_assets",
                        "operating_cash_flow",
                    ]
                )
                assert f["pe"] is None and f["roic"] is None
                # Independently look up the exact raw SEC contexts selected by the adapter.
                import re

                from trading212_mcp.market.factory import FundamentalsProvider
                from trading212_mcp.market.sec import SECStatements

                assert isinstance(provider, FundamentalsProvider)
                assert isinstance(provider.statements, SECStatements)
                response = await provider.statements._get(
                    f"https://data.sec.gov/api/xbrl/companyfacts/CIK{ciks[symbol]:010d}.json"
                )
                raw = response.rows[0]["value"]
                assert raw["cik"] == ciks[symbol]
                provenance = f["provenance"]["current_annual"]
                accession = re.search(r"accession=([^;]+)", provenance).group(1)
                for field, tags in {
                    "revenue": [
                        "RevenueFromContractWithCustomerExcludingAssessedTax",
                        "Revenues",
                        "SalesRevenueNet",
                    ],
                    "net_income": ["NetIncomeLoss"],
                    "total_assets": ["Assets"],
                    "operating_cash_flow": [
                        "NetCashProvidedByUsedInOperatingActivities"
                    ],
                }.items():
                    matches = [
                        row["val"]
                        for tag in tags
                        for row in raw["facts"]["us-gaap"]
                        .get(tag, {})
                        .get("units", {})
                        .get("USD", [])
                        if row.get("accn") == accession
                        and row.get("end") == f["statement_date"]
                        and (
                            field == "total_assets"
                            and "start" not in row
                            or field != "total_assets"
                            and "start" in row
                            and 300
                            <= (
                                datetime.fromisoformat(row["end"])
                                - datetime.fromisoformat(row["start"])
                            ).days
                            <= 400
                        )
                    ]
                    assert f[field] in matches
            else:
                assert "fundamentals" in asset["missing_data"]
        # Explicit Yahoo-only annual mode also supports the Dutch listing; no
        # SEC fields are merged into this separate fundamentals block.
        asml = await provider.base.profile("ASML.AS")
        international = await provider.base.fundamentals(asml)
        assert international.source == "yfinance"
        assert international.statement_currency == "EUR"
        assert (
            international.revenue is not None and international.total_assets is not None
        )
        assert international.statement_date is not None
        assert (
            datetime.now(timezone.utc).date() - international.statement_date
        ).days < 550
        universe = await provider.universe({"marketCapMoreThan": 1e11}, 105)
        assert len(universe.companies) == 105
        assert universe.pages_fetched == 2 and universe.truncated
        settings.screen_max_candidates = 5
        arguments = {
            "min_market_cap": 1e11,
            "exclude_owned": False,
            "factor_weights": {"growth": 0.5, "quality": 0.5},
            "limit": 5,
        }
        _, first = await server.call_tool("screen_stocks", arguments)
        _, second = await server.call_tool("screen_stocks", arguments)
        assert first["coverage"]["discovered"] == 5
        assert first["candidates"] and not first["coverage"]["failures"]
        assert [
            (c["company"]["symbol"], c["composite_score"]) for c in first["candidates"]
        ] == [
            (c["company"]["symbol"], c["composite_score"]) for c in second["candidates"]
        ]
        assert all(c["company"]["market_cap"] >= 1e11 for c in first["candidates"])
        with pytest.raises(MarketError):
            await provider.profile("NOT_A_REAL_SYMBOL_212_VALIDATION")
    finally:
        await provider.aclose()
