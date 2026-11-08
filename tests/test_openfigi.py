import json
from unittest.mock import AsyncMock

import httpx
import pytest

from trading212_mcp.market.mapping_cache import MappingCache
from trading212_mcp.market.models import Company, MarketError
from trading212_mcp.market.openfigi import OpenFIGIClient, OpenFIGIResolver
from trading212_mcp.market.resolution import InstrumentResolver
from trading212_mcp.market.yahoo import YFinanceProvider
from trading212_mcp.trading212.models import Instrument

APPLE = dict(
    figi="BBG000B9XRY4",
    ticker="AAPL",
    exchCode="US",
    compositeFIGI="BBG000B9XRY4",
    marketSector="Equity",
    securityType="Common Stock",
    securityType2="Common Stock",
)
APPLE_NASDAQ = {**APPLE, "figi": "BBG000B9Y5X2", "exchCode": "UW"}
VUSA = dict(
    figi="BBG003241JV5",
    ticker="VUSA",
    exchCode="LN",
    compositeFIGI="BBG003241JT8",
    marketSector="Equity",
    securityType="ETP",
    securityType2="Mutual Fund",
)
OTHER = {
    **APPLE,
    "figi": "BBG000BLNNH6",
    "compositeFIGI": "BBG000BLNNH6",
    "ticker": "IBM",
}


@pytest.fixture
def identity_setup(market_settings, monkeypatch):
    monkeypatch.setattr("trading212_mcp.market.openfigi.asyncio.sleep", AsyncMock())
    requests = []

    def handler(request):
        requests.append(request)
        results = []
        for job in json.loads(request.content):
            if job["idType"] == "ID_ISIN":
                results.append(
                    {"data": [VUSA if job["idValue"] == "IE00B3XXRP09" else APPLE]}
                )
            elif job.get("micCode") == "XLON":
                results.append({"data": [VUSA]})
            elif job.get("micCode") == "XNGS":
                results.append({"data": [APPLE_NASDAQ]})
            else:
                results.append({"warning": "No identifier found."})
        return httpx.Response(200, json=results)

    client = OpenFIGIClient(market_settings, httpx.MockTransport(handler))
    cache = MappingCache(market_settings.resolution_cache_path, 30)
    return client, cache, requests


async def test_isin_to_yahoo_verified_by_reverse_figi(identity_setup):
    client, cache, requests = identity_setup
    resolver = OpenFIGIResolver(client, cache)
    profile = AsyncMock(
        return_value=Company(
            symbol="AAPL",
            name="Apple",
            currency="USD",
            exchange="NASDAQ",
            quote_type="EQUITY",
        )
    )
    try:
        matches = await resolver.resolve("US0378331005", "USD", profile)
        assert matches[0].isin == "US0378331005" and matches[0].symbol == "AAPL"
        jobs = [job for request in requests for job in json.loads(request.content)]
        assert jobs[0] == {
            "idType": "ID_ISIN",
            "idValue": "US0378331005",
            "marketSecDes": "Equity",
            "currency": "USD",
        }
        assert any(job.get("micCode") == "XNGS" for job in jobs)
        assert cache.path.exists()
    finally:
        await client.aclose()


async def test_london_etf_minor_currency_and_security_type(identity_setup):
    client, cache, _ = identity_setup
    profile = AsyncMock(
        return_value=Company(
            symbol="VUSA.L",
            name="Vanguard S&P500",
            currency="GBp",
            exchange="LSE",
            quote_type="ETF",
            is_etf=True,
        )
    )
    try:
        matches = await OpenFIGIResolver(client, cache).resolve(
            "IE00B3XXRP09", "GBP", profile
        )
        assert matches[0].symbol == "VUSA.L" and matches[0].isin == "IE00B3XXRP09"
    finally:
        await client.aclose()


async def test_same_name_or_symbol_without_matching_figi_rejected(market_settings):
    client = OpenFIGIClient(
        market_settings,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json=[
                    {"data": [APPLE]}
                    if job["idType"] == "ID_ISIN"
                    else {"data": [OTHER]}
                    for job in json.loads(request.content)
                ],
            )
        ),
    )
    profile = AsyncMock(
        return_value=Company(
            symbol="AAPL",
            name="APPLE INC",
            currency="USD",
            exchange="NASDAQ",
            quote_type="EQUITY",
        )
    )
    try:
        assert (
            await OpenFIGIResolver(
                client, MappingCache(market_settings.resolution_cache_path, 30)
            ).resolve("US0378331005", "USD", profile)
            == []
        )
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "EUR"},
        {"exchange": "UNKNOWN"},
        {"quote_type": "OPTION"},
        {"isin": "WRONG_ISIN"},
    ],
)
async def test_currency_venue_type_and_isin_mismatches_rejected(
    identity_setup, changes
):
    client, cache, _ = identity_setup
    company = Company(
        symbol="AAPL",
        name="Apple",
        currency="USD",
        exchange="NASDAQ",
        quote_type="EQUITY",
    ).model_copy(update=changes)
    try:
        assert (
            await OpenFIGIResolver(client, cache).resolve(
                "US0378331005", "USD", AsyncMock(return_value=company)
            )
            == []
        )
    finally:
        await client.aclose()


async def test_persistent_proof_reused_after_restart_without_caching_quote(
    identity_setup, market_settings
):
    client, cache, _ = identity_setup
    company = Company(
        symbol="AAPL",
        name="Apple",
        currency="USD",
        exchange="NASDAQ",
        quote_type="EQUITY",
        price=100,
    )
    try:
        await OpenFIGIResolver(client, cache).resolve(
            "US0378331005", "USD", AsyncMock(return_value=company)
        )
    finally:
        await client.aclose()
    forbidden = httpx.MockTransport(
        lambda request: pytest.fail("Fresh proof must not call OpenFIGI after restart")
    )
    second = OpenFIGIClient(market_settings, forbidden)
    try:
        fresh = company.model_copy(update={"price": 200})
        result = await OpenFIGIResolver(second, MappingCache(cache.path, 30)).resolve(
            "US0378331005", "USD", AsyncMock(return_value=fresh)
        )
        assert result[0].price == 200
        assert result[0].isin == "US0378331005"
    finally:
        await second.aclose()


async def test_changed_profile_invalidates_cached_identity(identity_setup):
    client, cache, requests = identity_setup
    company = Company(
        symbol="AAPL",
        name="Apple",
        currency="USD",
        exchange="NASDAQ",
        quote_type="EQUITY",
    )
    resolver = OpenFIGIResolver(client, cache)
    try:
        await resolver.resolve("US0378331005", "USD", AsyncMock(return_value=company))
        before = len(requests)
        client._cache._entries.clear()
        changed = company.model_copy(
            update={"isin": "different-security", "name": "Ticker reused"}
        )
        assert (
            await resolver.resolve(
                "US0378331005", "USD", AsyncMock(return_value=changed)
            )
            == []
        )
        assert len(requests) > before
    finally:
        await client.aclose()


def test_expired_and_corrupt_cache_not_trusted(tmp_path, monkeypatch):
    cache = MappingCache(tmp_path / "identifiers.sqlite3", 1)
    clock = [0.0]
    monkeypatch.setattr(
        "trading212_mcp.market.mapping_cache.time.time", lambda: clock[0]
    )
    cache.put("key", {"symbol": "AAPL"})
    assert cache.get("key") == {"symbol": "AAPL"}
    clock[0] = 86401
    assert cache.get("key") is None
    cache.path.write_bytes(b"not a sqlite database")
    assert cache.get("key") is None


async def test_verified_choice_remembered_without_environment_mapping(
    provider, market_settings
):
    provider.companies["SECOND"] = provider.companies["AAA"].model_copy(
        update={"symbol": "SECOND"}
    )
    cache = MappingCache(market_settings.resolution_cache_path, 30)
    instrument = Instrument(
        ticker="AMBIGUOUS", isin="ISIN_A", currency="USD", name="Alpha"
    )
    resolver = InstrumentResolver(provider, {}, cache)
    with pytest.raises(MarketError, match="Verified candidates: AAA, SECOND"):
        await resolver.resolve(instrument)
    assert (
        await resolver.resolve(instrument, selected_symbol="SECOND")
    ).symbol == "SECOND"
    fresh = InstrumentResolver(provider, {}, MappingCache(cache.path, 30))
    assert (await fresh.resolve(instrument)).symbol == "SECOND"
    provider.name += "+sec_edgar_annual"
    assert (await fresh.resolve(instrument)).symbol == "SECOND"
    with pytest.raises(MarketError, match="no verified"):
        await fresh.resolve(instrument, selected_symbol="BBB")


async def test_us_broker_market_hint_never_replaces_identity_proof(provider):
    provider.companies["LONDON"] = provider.companies["AAA"].model_copy(
        update={"symbol": "LONDON", "exchange": "LSE"}
    )
    instrument = Instrument(
        ticker="TOTALLY_DIFFERENT_US_EQ", isin="ISIN_A", currency="USD", name="Alpha"
    )
    assert (await InstrumentResolver(provider, {}).resolve(instrument)).symbol == "AAA"
    instrument.isin = "UNKNOWN"
    with pytest.raises(MarketError):
        await InstrumentResolver(provider, {}).resolve(instrument)


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_openfigi_failures_safe(market_settings, status):
    client = OpenFIGIClient(
        market_settings,
        httpx.MockTransport(
            lambda request: httpx.Response(
                status,
                headers={"Retry-After": "60"},
                json={"secret": "private response"},
            )
        ),
    )
    try:
        with pytest.raises(MarketError) as error:
            await client.map([{"idType": "ID_ISIN", "idValue": "US0378331005"}])
        assert "private response" not in str(error.value)
        if status == 429:
            with pytest.raises(MarketError, match="cooldown"):
                await client.map([{"idType": "ID_ISIN", "idValue": "US0378331005"}])
    finally:
        await client.aclose()


async def test_openfigi_job_association_and_errors_validated(market_settings):
    client = OpenFIGIClient(
        market_settings,
        httpx.MockTransport(lambda request: httpx.Response(200, json=[])),
    )
    try:
        with pytest.raises(MarketError, match="association"):
            await client.map([{"idType": "ID_ISIN", "idValue": "US0378331005"}])
    finally:
        await client.aclose()


async def test_yahoo_adapter_uses_openfigi_without_manual_isins(
    identity_setup, market_settings
):
    # Backend returns no ISIN at all, as Yahoo commonly does.
    from test_yahoo import Backend

    backend = Backend()
    backend.profiles["AAPL"] = {**backend.profiles["AAA"], "symbol": "AAPL"}
    backend.profiles["AAPL"].pop("isin")
    client, _, requests = identity_setup
    yahoo = YFinanceProvider(market_settings, backend, client)
    try:
        instrument = Instrument(
            ticker="AAPL_US_EQ", isin="US0378331005", currency="USD", name="Apple"
        )
        result = await InstrumentResolver(yahoo, {}).resolve(instrument)
        assert result.symbol == "AAPL" and result.isin == "US0378331005"
        assert requests
    finally:
        await yahoo.aclose()
