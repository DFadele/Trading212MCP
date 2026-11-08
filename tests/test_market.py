import logging
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from trading212_mcp.market.cache import CachedResponse, TTLCache
from trading212_mcp.market.fmp import FMPProvider, normalise_prices, number
from trading212_mcp.market.models import MarketError
from trading212_mcp.market.resolution import InstrumentResolver, currency_unit
from trading212_mcp.trading212.models import Instrument


def test_normalisation_sorted_and_deduplicated():
    rows = [
        {"date": "2026-01-02", "adjClose": "12.50"},
        {"date": "2026-01-01", "adjClose": 10},
        {"date": "2026-01-02", "adjClose": "12.50"},
    ]
    prices = normalise_prices(rows, "adjClose", date(2026, 1, 1), date(2026, 1, 2))
    assert [p.close for p in prices] == [10, 12.5]


@pytest.mark.parametrize("value", [0, -1, "nan", "inf", None])
def test_invalid_prices(value):
    with pytest.raises(MarketError):
        normalise_prices(
            [{"date": "2026-01-01", "adjClose": value}],
            "adjClose",
            date(2026, 1, 1),
            date(2026, 1, 2),
        )


def test_conflicting_duplicates():
    with pytest.raises(MarketError):
        normalise_prices(
            [
                {"date": "2026-01-01", "adjClose": 1},
                {"date": "2026-01-01", "adjClose": 2},
            ],
            "adjClose",
            date(2026, 1, 1),
            date(2026, 1, 2),
        )


def test_missing_numbers_not_zero():
    assert number(None) is None and number("NaN") is None and number(True) is None
    assert number(0) == 0


async def test_instrument_resolution_and_ambiguity(provider):
    instrument = Instrument(
        currency="USD", isin="ISIN_A", name="Alpha", ticker="NOT_A_MARKET_SYMBOL"
    )
    resolver = InstrumentResolver(provider, {})
    assert (await resolver.resolve(instrument)).symbol == "AAA"
    provider.companies["DUAL"] = provider.companies["AAA"].model_copy(
        update={"symbol": "DUAL"}
    )
    with pytest.raises(MarketError, match="ambiguous"):
        await resolver.resolve(instrument)
    resolver.overrides[instrument.ticker] = "AAA"
    assert (await resolver.resolve(instrument)).symbol == "AAA"
    resolver.overrides[instrument.ticker] = "BBB"
    with pytest.raises(MarketError, match="no verified"):
        await resolver.resolve(instrument)


async def test_currency_mismatch_is_not_guessed(provider):
    with pytest.raises(MarketError):
        await InstrumentResolver(provider, {}).resolve(
            Instrument(currency="EUR", isin="ISIN_A", name="Alpha", ticker="AAA_US_EQ")
        )


async def test_fx_conversion_and_minor_units(service, provider):
    company = provider.companies["AAA"]
    start, end = provider.dates[0].date(), provider.dates[-1].date()
    series, _, _ = await service.converted_history(company, start, end, "GBP")
    assert series.iloc[0] == pytest.approx(provider.series["AAA"][0] * 0.8)
    provider.companies["AAA"] = company.model_copy(update={"currency": "GBp"})
    series, _, _ = await service.converted_history(
        provider.companies["AAA"], start, end, "GBP"
    )
    assert series.iloc[0] == pytest.approx(provider.series["AAA"][0] * 0.01)
    assert currency_unit("GBP") != currency_unit("GBp")


def test_cache_bound_expiry_and_timestamp(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("trading212_mcp.market.cache.time.monotonic", lambda: clock[0])
    cache = TTLCache(5, 1)
    row = CachedResponse([], datetime(2026, 1, 1, tzinfo=timezone.utc))
    cache.put("a", row)
    assert cache.get("a").retrieved_at == row.retrieved_at
    cache.put("b", row)
    assert cache.get("a") is None
    clock[0] = 6
    assert cache.get("b") is None


async def test_provider_cache_and_secret_redaction(market_settings, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=[{"symbol": "AAA", "companyName": "Alpha", "currency": "USD"}]
        )

    provider = FMPProvider(market_settings, httpx.MockTransport(handler))
    try:
        with caplog.at_level(logging.INFO, logger="httpx"):
            await provider.profile("AAA")
            await provider.profile("AAA")
        assert len(calls) == 1
        assert market_settings.fmp_api_key.get_secret_value() not in caplog.text
    finally:
        await provider.aclose()


@pytest.mark.parametrize("status", [401, 402, 403, 429, 500])
async def test_provider_failures_are_safe(market_settings, status):
    provider = FMPProvider(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(status, json={"secret": "never return this"})
        ),
    )
    try:
        with pytest.raises(MarketError) as error:
            await provider.profile("AAA")
        assert "never return" not in str(error.value) and "unit-test-key" not in str(
            error.value
        )
    finally:
        await provider.aclose()


async def test_retry_after_and_retries(market_settings, monkeypatch):
    market_settings.retries = 1
    calls = []
    sleeps = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(429, headers={"Retry-After": "2"})
            if len(calls) == 1
            else httpx.Response(200, json=[{"symbol": "AAA", "currency": "USD"}])
        )

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("trading212_mcp.market.fmp.asyncio.sleep", sleep)
    provider = FMPProvider(market_settings, httpx.MockTransport(handler))
    try:
        assert (await provider.profile("AAA")).symbol == "AAA"
        assert len(calls) == 2 and sleeps[0] > 1.9
    finally:
        await provider.aclose()


async def test_error_json_not_data(market_settings):
    provider = FMPProvider(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(200, json={"Error Message": "API key secret"})
        ),
    )
    try:
        with pytest.raises(MarketError):
            await provider.profile("AAA")
    finally:
        await provider.aclose()


async def test_market_compact_and_detailed(service):
    compact = await service.market_data(["AAA", "MISSING"], 3, True)
    assert "series" not in compact["assets"][0]
    assert compact["assets"][1]["missing_data"]
    detailed = await service.market_data(["AAA"], 3, False, True)
    assert len(detailed["assets"][0]["series"]) == 300
    assert detailed["assets"][0]["series"][0]["return"] is None


async def test_partial_portfolio_risk(service, provider, trading):
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert result["methodology"]["actual_historical_performance"] is False
    assert result["risk_metrics"]["annualised_volatility"] > 0
    assert result["data_quality"]["included_value_fraction"] == 1
    provider.failed.add("AAA")
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert result["risk_metrics"] is None
    assert result["concentration_analysis"]["largest_weight"] == 1
    assert result["data_quality"]["excluded"]


async def test_missing_calendar_rows_not_filled(service, provider, trading):
    # One missing equity observation removes both adjacent daily returns.
    original = provider.history

    async def history(company, start, end):
        result = await original(company, start, end)
        if company.symbol == "AAA":
            result.prices[100:101] = []
        return result

    provider.history = history
    result = await service.portfolio_risk(trading, 3, "SPY")
    # For a single asset its own calendar is used; explicitly documented.
    assert result["risk_metrics"]["observations"] == 298


async def test_fundamental_normalisation_and_negative_earnings(
    market_settings, monkeypatch
):
    responses = {
        "ratios-ttm": [
            {
                "priceToEarningsRatioTTM": -5,
                "netProfitMarginTTM": 0.2,
                "debtToEquityRatioTTM": 0.5,
            }
        ],
        "key-metrics-ttm": [{"returnOnInvestedCapitalTTM": 0.15}],
        "income-statement": [
            {
                "date": "2025-12-31",
                "reportedCurrency": "USD",
                "revenue": 120,
                "netIncome": 12,
            },
            {
                "date": "2024-12-31",
                "reportedCurrency": "USD",
                "revenue": 100,
                "netIncome": -5,
            },
        ],
        "balance-sheet-statement": [
            {
                "date": "2025-12-31",
                "reportedCurrency": "USD",
                "totalDebt": 50,
                "cashAndCashEquivalents": 20,
            }
        ],
        "cash-flow-statement": [
            {
                "date": "2025-12-31",
                "reportedCurrency": "USD",
                "operatingCashFlow": 30,
                "freeCashFlow": 25,
            }
        ],
        "analyst-estimates": [
            {
                "date": (
                    datetime.now(timezone.utc).date() + timedelta(days=365)
                ).isoformat(),
                "epsAvg": 5,
            }
        ],
    }

    async def sleep(delay):
        pass

    monkeypatch.setattr("trading212_mcp.market.fmp.asyncio.sleep", sleep)
    provider = FMPProvider(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(200, json=responses[r.url.path.split("/")[-1]])
        ),
    )
    try:
        from trading212_mcp.market.models import Company

        result = await provider.fundamentals(
            Company(symbol="AAA", name="A", currency="USD", price=100)
        )
        assert result.revenue_growth == pytest.approx(0.2)
        assert result.earnings_growth is None
        assert result.pe is None and result.forward_pe == 20
        assert (
            result.roic == 0.15
            and result.free_cash_flow == 25
            and result.total_debt == 50
        )
        assert result.statement_currency == "USD"
        assert len(result.annual_history) == 2
        assert result.annual_history[0].free_cash_flow == 25
        assert result.annual_history[1].free_cash_flow is None
        assert result.annual_history[0].filed is None
    finally:
        await provider.aclose()


async def test_screener_uses_provider_pagination_and_discloses_cap(
    market_settings, monkeypatch
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json=[{"symbol": "AAA", "companyName": "Alpha", "marketCap": 1000}]
        )

    provider = FMPProvider(market_settings, httpx.MockTransport(handler))
    try:
        universe = await provider.universe({"sector": "Technology"}, 1)
        assert universe.truncated and universe.pages_fetched == 1
        assert calls[0].url.path.endswith("/company-screener")
        assert calls[0].url.params["page"] == "0"
        assert calls[0].url.params["isEtf"] == "false"
        assert universe.companies[0].symbol == "AAA"
    finally:
        await provider.aclose()


async def test_fx_missing_dates_never_backfilled_from_future(service, provider):
    original = provider.fx

    async def fx(source, target, start, end):
        result = await original(source, target, start, end)
        result.prices = result.prices[10:]
        return result

    provider.fx = fx
    series, _, warnings = await service.converted_history(
        provider.companies["AAA"],
        provider.dates[0].date(),
        provider.dates[-1].date(),
        "GBP",
    )
    assert series.iloc[:10].isna().all()
    assert series.iloc[10:].notna().all()
    assert any("missing" in note for note in warnings)


async def test_two_asset_calendar_gaps_remove_adjacent_returns(
    service, provider, trading
):
    original_positions = trading.get_positions

    async def get_positions():
        result = await original_positions()
        second = result.positions[0].model_copy(deep=True)
        second.instrument.ticker = "BBB_US_EQ"
        second.instrument.isin = "ISIN_B"
        result.positions.append(second)
        return result

    trading.get_positions = get_positions
    original_history = provider.history

    async def history(company, start, end):
        result = await original_history(company, start, end)
        if company.symbol == "BBB":
            result.prices[100:101] = []
        return result

    provider.history = history
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert result["risk_metrics"]["observations"] == 297
    assert result["data_quality"]["included_value_fraction"] == 1
    assert sum(
        row["fraction_of_variance"] for row in result["asset_risk_contributions"]
    ) == pytest.approx(1)


async def test_insufficient_common_history_returns_partial(service, provider, trading):
    service.settings.min_observations = 252
    provider.series["AAA"] = provider.series["AAA"][:10]
    provider.dates = provider.dates[:10]
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert result["risk_metrics"] is None
    assert result["data_quality"]["excluded"]


async def test_timeout_is_safe(market_settings):
    def handler(request):
        raise httpx.ReadTimeout("sensitive URL with unit-test-key", request=request)

    provider = FMPProvider(market_settings, httpx.MockTransport(handler))
    try:
        with pytest.raises(MarketError, match="timed out") as error:
            await provider.profile("AAA")
        assert "unit-test-key" not in str(error.value)
    finally:
        await provider.aclose()


async def test_risk_budget_returns_covered_subset_and_cancels_slow_history(
    service, provider, trading
):
    import asyncio

    original_positions = trading.get_positions

    async def positions():
        snapshot = await original_positions()
        small = snapshot.positions[0].model_copy(deep=True)
        small.instrument.ticker = "BBB_US_EQ"
        small.instrument.isin = "ISIN_B"
        small.walletImpact.currentValue = 500
        snapshot.positions.insert(0, small)
        return snapshot

    trading.get_positions = positions
    original_history = provider.history
    cancelled = asyncio.Event()

    async def history(company, start, end):
        if company.symbol == "BBB":
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return await original_history(company, start, end)

    provider.history = history
    service.settings.risk_timeout_seconds = 0.1
    result = await asyncio.wait_for(
        service.portfolio_risk(trading, 3, "SPY"), timeout=1
    )
    assert cancelled.is_set()
    assert result["data_quality"]["included_value_fraction"] == pytest.approx(2 / 3)
    assert result["risk_metrics"]["observations"] >= 20
    assert result["data_quality"]["excluded"][0]["trading212_ticker"] == "BBB_US_EQ"
    assert "budget" in result["data_quality"]["excluded"][0]["reason"]
    assert result["benchmark_comparison"]["beta"] is None
    assert result["asset_risk_contributions"][0]["effective_weight"] == 1


async def test_risk_budget_covers_account_setup(service, trading):
    import asyncio

    async def slow_summary():
        await asyncio.Event().wait()

    trading.get_account_summary = slow_summary
    service.settings.risk_timeout_seconds = 0.02
    with pytest.raises(MarketError, match="budget expired"):
        await asyncio.wait_for(service.portfolio_risk(trading, 3, "SPY"), timeout=1)


async def test_risk_54_holdings_bounded_parallelism_matches_serial(
    service, provider, trading
):
    import asyncio

    original_positions = trading.get_positions
    snapshot = await original_positions()
    template = snapshot.positions[0]
    snapshot.positions = []
    for i in range(54):
        symbol = f"ASSET{i:02}"
        position = template.model_copy(deep=True)
        position.instrument.ticker = symbol
        position.instrument.isin = f"ISIN_{i}"
        snapshot.positions.append(position)
        provider.companies[symbol] = provider.companies["AAA"].model_copy(
            update={"symbol": symbol, "isin": f"ISIN_{i}"}
        )
        provider.series[symbol] = provider.series["AAA"]

    async def positions():
        return snapshot

    trading.get_positions = positions
    original_history = provider.history
    started = asyncio.Event()
    release = asyncio.Event()
    active = maximum = 0

    async def history(company, start, end):
        nonlocal active, maximum
        if company.symbol == "SPY":
            return await original_history(company, start, end)
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            started.set()
        try:
            await release.wait()
            return await original_history(company, start, end)
        finally:
            active -= 1

    provider.history = history
    task = asyncio.create_task(service.portfolio_risk(trading, 3, "SPY"))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert maximum == 4
    finally:
        release.set()
        parallel = await task
    assert len(parallel["portfolio_summary"]["holdings"]) == 54
    assert parallel["data_quality"]["included_value_fraction"] == 1
    assert parallel["data_quality"]["excluded"] == []
    service.settings.fetch_concurrency = 1
    serial = await service.portfolio_risk(trading, 3, "SPY")
    assert parallel["risk_metrics"] == serial["risk_metrics"]
    assert parallel["asset_risk_contributions"] == serial["asset_risk_contributions"]


async def test_risk_matrix_order_is_independent_of_network_completion(
    service, provider, trading
):
    import asyncio

    original_positions = trading.get_positions

    async def positions():
        result = await original_positions()
        small = result.positions[0].model_copy(deep=True)
        small.instrument.ticker = "BBB_US_EQ"
        small.instrument.isin = "ISIN_B"
        small.walletImpact.currentValue = 500
        result.positions.append(small)
        return result

    trading.get_positions = positions
    original_history = provider.history
    smaller_finished = asyncio.Event()

    async def history(company, start, end):
        if company.symbol == "AAA":
            await asyncio.wait_for(smaller_finished.wait(), 1)
        result = await original_history(company, start, end)
        if company.symbol == "BBB":
            smaller_finished.set()
        return result

    provider.history = history
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert [c["symbol"] for c in result["asset_risk_contributions"]] == ["AAA", "BBB"]
    assert list(result["annualised_covariance"]) == ["AAA", "BBB"]


async def test_fmp_history_rejects_future_filings_and_misaligned_statements(
    market_settings, monkeypatch
):
    from trading212_mcp.market.models import Company

    responses = {
        "income-statement": [
            {
                "date": "2025-12-31",
                "reportedCurrency": "USD",
                "filingDate": "2099-01-01",
                "revenue": 999,
            },
            {
                "date": "2024-12-31",
                "reportedCurrency": "USD",
                "filingDate": "2025-02-01",
                "revenue": 100,
                "netIncome": 10,
            },
        ],
        "balance-sheet-statement": [
            {"date": "2023-12-31", "reportedCurrency": "EUR", "totalAssets": 999}
        ],
        "cash-flow-statement": [
            {"date": "2023-12-31", "reportedCurrency": "USD", "freeCashFlow": 999}
        ],
    }

    async def no_wait(delay):
        pass

    monkeypatch.setattr("trading212_mcp.market.fmp.asyncio.sleep", no_wait)
    provider = FMPProvider(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json=responses.get(r.url.path.split("/")[-1], [])
            )
        ),
    )
    try:
        f = await provider.fundamentals(Company(symbol="A", name="A", currency="USD"))
        assert f.revenue == 100
        assert f.total_assets is None and f.free_cash_flow is None
        assert len(f.annual_history) == 1
        assert f.annual_history[0].end.isoformat() == "2024-12-31"
        assert f.annual_history[0].total_assets is None
        assert any("no matching income fiscal end/currency" in w for w in f.warnings)
    finally:
        await provider.aclose()
