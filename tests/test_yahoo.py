import asyncio
import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pandas as pd
import pytest

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.openfigi import OpenFIGIClient
from trading212_mcp.market.resolution import InstrumentResolver
from trading212_mcp.market.service import MarketService
from trading212_mcp.market.yahoo import (
    YahooRateLimitError,
    YFinanceProvider,
    yahoo_fundamentals,
    yahoo_prices,
)
from trading212_mcp.quantitative.screening import screen
from trading212_mcp.trading212.models import Instrument


class Backend:
    def __init__(self):
        self.calls = []
        self.dates = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=300)
        self.profiles = {
            symbol: {
                "symbol": symbol,
                "currency": "USD",
                "financialCurrency": "USD",
                "longName": symbol + " Automation",
                "quoteType": "EQUITY",
                "sector": "Technology",
                "industry": "Semiconductors",
                "country": "United States",
                "exchange": "NMS",
                "isin": isin,
                "marketCap": 1000,
                "trailingPE": 20,
                "forwardPE": 15,
                "profitMargins": 0.2,
                "operatingMargins": 0.3,
                "debtToEquity": 50,
            }
            for symbol, isin in [("AAA", "ISIN_A"), ("BBB", "ISIN_B")]
        }
        self.profiles["SPY"] = {
            **self.profiles["AAA"],
            "symbol": "SPY",
            "quoteType": "ETF",
            "isin": "ISIN_SPY",
        }
        self.error = None

    def info(self, symbol):
        self.calls.append(("info", symbol))
        if self.error:
            raise self.error
        return self.profiles[symbol]

    def history(self, symbol, start, end, timeout):
        self.calls.append(("history", symbol))
        return pd.DataFrame(
            {
                "Adj Close": [100 + i * 0.1 for i in range(300)],
                "Close": [200 + i * 0.2 for i in range(300)],
            },
            index=self.dates,
        )

    def statements(self, symbol):
        current = pd.Timestamp("2025-12-31")
        prior = pd.Timestamp("2024-12-31")
        income = pd.DataFrame(
            {
                current: {"TotalRevenue": 120, "NetIncome": 24},
                prior: {"TotalRevenue": 100, "NetIncome": 20},
            }
        )
        balance = pd.DataFrame(
            {current: {"TotalDebt": 50, "CashAndCashEquivalents": 20}}
        )
        cash = pd.DataFrame({current: {"OperatingCashFlow": 30, "FreeCashFlow": 25}})
        return income, balance, cash

    def search(self, isin, timeout):
        return [row for row in self.profiles.values() if row.get("isin") == isin]

    def screen(self, filters, regions, offset, size):
        self.calls.append(("screen", filters, regions, offset, size))
        return {
            "total": 2,
            "quotes": list(self.profiles.values())[:2][offset : offset + size],
        }


@pytest.fixture
async def yahoo(market_settings, monkeypatch):
    monkeypatch.setattr("trading212_mcp.market.yahoo.asyncio.sleep", AsyncMock())
    backend = Backend()
    identifiers = OpenFIGIClient(
        market_settings,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=[{"warning": "No match"} for _ in json.loads(request.content)]
            )
        ),
    )
    provider = YFinanceProvider(market_settings, backend, identifiers)
    yield provider, backend
    await provider.aclose()


async def test_personal_default_needs_no_activation_gate(market_settings):
    provider = YFinanceProvider(market_settings, Backend())
    try:
        assert (await provider.profile("AAA")).symbol == "AAA"
    finally:
        await provider.aclose()


async def test_profile_cache_and_matching(yahoo):
    provider, backend = yahoo
    company = await provider.profile("AAA")
    assert (
        company.isin == "ISIN_A"
        and company.country == "US"
        and company.exchange == "NASDAQ"
    )
    await provider.profile("AAA")
    assert backend.calls.count(("info", "AAA")) == 1
    resolver = InstrumentResolver(provider, {})
    instrument = Instrument(
        ticker="UNRELATED_US_EQ", name="Alpha", currency="USD", isin="ISIN_A"
    )
    assert (await resolver.resolve(instrument)).symbol == "AAA"


async def test_ambiguous_verified_listings_not_chosen(yahoo):
    provider, backend = yahoo
    backend.profiles["BBB"]["isin"] = "ISIN_A"
    resolver = InstrumentResolver(provider, {})
    instrument = Instrument(
        ticker="AAA_US_EQ", name="Alpha", currency="USD", isin="ISIN_A"
    )
    with pytest.raises(MarketError, match="ambiguous"):
        await resolver.resolve(instrument)


async def test_isin_missing_no_fuzzy_assumption(yahoo):
    provider, backend = yahoo
    backend.profiles["AAA"].pop("isin")
    assert (await provider.profile("AAA")).isin is None
    assert await provider.search_isin("ISIN_A") == []
    provider.settings.yahoo_isin_map = {"BBB": "ISIN_A"}
    with pytest.raises(MarketError, match="conflicts"):
        await provider.profile("BBB")


async def test_explicit_verified_isin_map(yahoo):
    provider, backend = yahoo
    backend.profiles["AAA"].pop("isin")
    provider.settings.yahoo_isin_map = {"AAA": "ISIN_A"}
    assert (await provider.search_isin("ISIN_A"))[0].isin == "ISIN_A"


async def test_adjusted_prices_and_fx_are_distinct(yahoo):
    provider, backend = yahoo
    company = await provider.profile("AAA")
    start, end = backend.dates[0].date(), backend.dates[-1].date()
    history = await provider.history(company, start, end)
    fx = await provider.fx("USD", "GBP", start, end)
    assert history.prices[0].close == 100 and fx.prices[0].close == 200
    assert (
        history.price_basis == "split_and_dividend_adjusted"
        and fx.price_basis == "fx_spot"
    )
    assert ("history", "USDGBP=X") in backend.calls


def test_no_raw_price_fallback():
    frame = pd.DataFrame({"Close": [100.0]}, index=pd.DatetimeIndex(["2026-01-01"]))
    with pytest.raises(MarketError, match="fallback"):
        yahoo_prices(frame, "Adj Close", date(2026, 1, 1), date(2026, 1, 2))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_yahoo_prices(value):
    frame = pd.DataFrame({"Adj Close": [value]}, index=pd.DatetimeIndex(["2026-01-01"]))
    with pytest.raises(MarketError):
        yahoo_prices(frame, "Adj Close", date(2026, 1, 1), date(2026, 1, 2))


async def test_fundamental_units_and_periods(yahoo):
    provider, _ = yahoo
    result = await provider.fundamentals(await provider.profile("AAA"))
    assert result.revenue_growth == pytest.approx(0.2)
    assert result.debt_to_equity == 0.5
    assert result.free_cash_flow == 25 and result.roic is None
    assert result.statement_date == date(2025, 12, 31)
    assert result.source == "yfinance"


def test_missing_currency_and_period_not_filled():
    backend = Backend()
    info = backend.profiles["AAA"].copy()
    info.pop("financialCurrency")
    result = yahoo_fundamentals(
        info, backend.statements("AAA"), datetime.now(timezone.utc)
    )
    assert result.free_cash_flow is None and result.revenue_growth is None
    info["financialCurrency"] = "USD"
    income, balance, cash = backend.statements("AAA")
    cash.columns = pd.DatetimeIndex(["2024-12-31"])
    result = yahoo_fundamentals(
        info, (income, balance, cash), datetime.now(timezone.utc)
    )
    assert result.free_cash_flow is None  # Never silently use a different fiscal year.


async def test_real_screener_interface_coverage_and_no_held_only_universe(
    yahoo, trading
):
    provider, backend = yahoo
    universe = await provider.universe({"sector": "Technology"}, 1)
    assert universe.truncated and len(universe.companies) == 1
    assert "provider-reported matches=2" in universe.description
    assert backend.calls[-1][0] == "screen"
    service = MarketService(provider, provider.settings)
    result = await screen(service, trading, {}, True, 20, "quality_growth", None)
    assert [row["company"]["symbol"] for row in result["candidates"]] == ["BBB"]
    assert (
        result["coverage"]["discovered"] == 2
        and result["coverage"]["excluded_owned"] == 1
    )


async def test_yahoo_missing_isin_screen_exclusion_fails_closed(yahoo, trading):
    provider, backend = yahoo
    backend.profiles["BBB"].pop("isin")
    result = await screen(
        MarketService(provider, provider.settings),
        trading,
        {},
        True,
        20,
        "quality_growth",
        None,
    )
    assert result["candidates"] == []
    assert any("ISIN" in row["reason"] for row in result["coverage"]["failures"])


async def test_rate_limit_safe_and_no_repeated_requests(yahoo):
    provider, backend = yahoo
    backend.error = YahooRateLimitError("sensitive SDK details")
    with pytest.raises(MarketError, match="rate limit") as error:
        await provider.profile("AAA")
    assert "sensitive" not in str(error.value)
    with pytest.raises(MarketError, match="cooldown"):
        await provider.profile("AAA")
    assert len(backend.calls) == 1


async def test_provider_error_safe(yahoo):
    provider, backend = yahoo
    backend.error = ValueError("private cookie in SDK message")
    with pytest.raises(MarketError) as error:
        await provider.profile("AAA")
    assert "private cookie" not in str(error.value)


async def test_unknown_screener_coverage_not_reported_as_complete(yahoo):
    provider, backend = yahoo
    backend.screen = lambda *args: {"quotes": []}
    with pytest.raises(MarketError, match="coverage"):
        await provider.universe({}, 20)


async def test_timeout_does_not_spawn_replacement_workers(yahoo):
    import time

    provider, backend = yahoo
    provider.settings.timeout_seconds = 0.005
    original = backend.info

    def slow_info(symbol):
        time.sleep(0.05)
        return original(symbol)

    backend.info = slow_info
    with pytest.raises(MarketError, match="timed out"):
        await provider.profile("AAA")
    with pytest.raises(MarketError, match="still finishing"):
        await provider.profile("BBB")
    await asyncio.gather(*(asyncio.shield(f) for f in provider._pending))
    assert backend.calls == [("info", "AAA")]


async def test_yahoo_shared_service_market_and_risk_contracts(yahoo, trading):
    provider, _ = yahoo
    service = MarketService(provider, provider.settings)
    data = await service.market_data(["AAA"], 3, True)
    assert data["source"] == "yfinance"
    assert data["assets"][0]["fundamentals"]["source"] == "yfinance"
    assert data["assets"][0]["price_basis"] == "split_and_dividend_adjusted"
    risk = await service.portfolio_risk(trading, 3, "SPY")
    assert risk["source"] == "yfinance"
    assert risk["data_quality"]["included_value_fraction"] == 1
    assert risk["risk_metrics"]["observations"] == 299
    assert risk["methodology"]["actual_historical_performance"] is False


async def test_yahoo_overlaps_requests_and_coalesces_cache_misses(yahoo):
    from threading import Event, Lock

    provider, _ = yahoo
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()
    lock = Lock()
    active = 0
    maximum = 0
    calls = 0

    def operation():
        nonlocal active, maximum, calls
        with lock:
            active += 1
            calls += 1
            maximum = max(maximum, active)
            if active == provider.settings.fetch_concurrency:
                loop.call_soon_threadsafe(started.set)
        assert release.wait(3), "worker was never released"
        with lock:
            active -= 1
        return "ok"

    # Avoid accidental lock-stripe collisions in this overlap assertion.
    keys = []
    stripes = set()
    for i in range(1000):
        key = f"overlap:{i}"
        stripe = hash(key) % 64
        if stripe not in stripes:
            keys.append(key)
            stripes.add(stripe)
        if len(keys) == 6:
            break
    tasks = [asyncio.create_task(provider._call(k, operation)) for k in keys]
    duplicate = asyncio.create_task(provider._call(keys[0], operation))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert calls == 4
    finally:
        release.set()
        results = await asyncio.gather(*tasks, duplicate)
    assert maximum == 4
    assert calls == 6
    assert results[0] is results[-1]


async def test_cancelled_yahoo_waiter_keeps_worker_slot(yahoo):
    from threading import Event

    provider, _ = yahoo
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = Event()

    def operation():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(3)
        return "ok"

    task = asyncio.create_task(provider._call("cancel-test", operation))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(provider._pending) == 1
        assert provider._slots._value == provider.settings.fetch_concurrency - 1
    finally:
        release.set()
        await asyncio.gather(*provider._pending)
    assert provider._slots._value == provider.settings.fetch_concurrency


async def test_pagination_honours_candidate_cap_and_reports_truncation(yahoo):
    provider, backend = yahoo
    calls = []

    def page(filters, regions, offset, size):
        calls.append((offset, size))
        return {
            "total": 150,
            "quotes": [
                {**backend.profiles["AAA"], "symbol": f"STOCK{i}"}
                for i in range(offset, min(150, offset + size))
            ],
        }

    backend.screen = page
    universe = await provider.universe({}, 105)
    assert calls == [(0, 100), (100, 5)]
    assert len(universe.companies) == 105
    assert universe.pages_fetched == 2
    assert universe.truncated


async def test_expired_yahoo_cache_does_not_mask_provider_outage(yahoo, monkeypatch):
    provider, backend = yahoo
    await provider.profile("AAA")
    entry = provider._cache._entries["profile:AAA"]
    provider._cache._entries["profile:AAA"] = (0, entry[1])
    backend.error = ConnectionError("private upstream details")
    with pytest.raises(MarketError, match="failed") as error:
        await provider.profile("AAA")
    assert "private" not in str(error.value)
    assert len(backend.calls) == 2


async def test_yahoo_partial_profile_missing_currency_is_not_fabricated(yahoo):
    provider, backend = yahoo
    backend.profiles["AAA"].pop("currency")
    with pytest.raises(MarketError, match="currency"):
        await provider.profile("AAA")


def test_yfinance_18_raises_provider_errors_without_deprecated_flag(monkeypatch):
    from types import SimpleNamespace

    from trading212_mcp.market.yahoo import YFinanceBackend

    calls = []
    frame = pd.DataFrame()

    def history(**kwargs):
        calls.append(kwargs)
        return frame

    sdk = SimpleNamespace(
        config=SimpleNamespace(debug=SimpleNamespace(hide_exceptions=True)),
        Ticker=lambda symbol: SimpleNamespace(history=history),
    )
    monkeypatch.setattr(
        "trading212_mcp.market.yahoo.importlib.import_module", lambda name: sdk
    )
    backend = YFinanceBackend()
    assert sdk.config.debug.hide_exceptions is False
    backend.history("AAA", date(2026, 1, 1), date(2026, 1, 31), 10)
    assert calls == [
        {
            "start": "2026-01-01",
            "end": "2026-02-01",
            "interval": "1d",
            "auto_adjust": False,
            "back_adjust": False,
            "repair": False,
            "actions": True,
            "timeout": 10,
        }
    ]


def test_advanced_annual_history_uses_exact_periods_and_discloses_unknown_filing():
    from trading212_mcp.market.models import Company
    from trading212_mcp.quantitative.factors import financial_features

    periods = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])
    income = pd.DataFrame(
        {
            p: {"TotalRevenue": 100 + i * 10, "NetIncome": 10, "OperatingIncome": 20}
            for i, p in enumerate(periods)
        }
    )
    cash = pd.DataFrame(
        {p: {"OperatingCashFlow": 15, "FreeCashFlow": 12} for p in periods}
    )
    # No matching latest balance date: must not substitute prior-year equity/assets.
    balance = pd.DataFrame(
        {
            p: {"TotalAssets": 200, "StockholdersEquity": 100, "TotalDebt": 50}
            for p in periods[1:]
        }
    )
    f = yahoo_fundamentals(
        {"financialCurrency": "USD", "enterpriseToEbitda": 12},
        (income, balance, cash),
        datetime(2026, 11, 7, tzinfo=timezone.utc),
    )
    assert len(f.annual_history) == 4
    assert f.annual_history[0].equity is None
    metrics, _, warnings = financial_features(
        f, Company(symbol="X", name="X", currency="USD"), date(2026, 11, 7)
    )
    assert metrics["revenue_cagr"] is not None
    assert metrics["roe"] is None
    assert metrics["ev_to_ebitda"] == 12
    assert warnings


def test_yahoo_region_and_liquidity_pushdown_uses_supported_query_fields():
    from trading212_mcp.market.yahoo import YFinanceBackend

    class SDK:
        @staticmethod
        def EquityQuery(operator, values):
            return {"operator": operator, "values": values}

        @staticmethod
        def screen(expression, **kwargs):
            return {"expression": expression, **kwargs}

    backend = YFinanceBackend.__new__(YFinanceBackend)
    backend.sdk = SDK()
    result = backend.screen(
        {"regions": "gb,ca", "minAverageVolume": 100000, "marketCapLowerThan": 1e10},
        ["us"],
        100,
        50,
    )
    expression = json.dumps(result["expression"])
    assert '"gb"' in expression and '"ca"' in expression
    assert '"us"' not in expression
    assert "avgdailyvol3m" in expression
    assert result["offset"] == 100 and result["size"] == 50


def test_yahoo_domicile_filter_does_not_change_listing_region():
    from trading212_mcp.market.yahoo import YFinanceBackend

    class SDK:
        @staticmethod
        def EquityQuery(operator, values):
            return {"operator": operator, "values": values}

        @staticmethod
        def screen(expression, **kwargs):
            return {"expression": expression}

    backend = YFinanceBackend.__new__(YFinanceBackend)
    backend.sdk = SDK()
    result = backend.screen({"country": "GB"}, ["us"], 0, 10)
    assert result["expression"]["values"] == ["region", "us"]
