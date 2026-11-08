from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from trading212_mcp.market.factory import FundamentalsProvider, create_provider
from trading212_mcp.market.fmp import FMPProvider
from trading212_mcp.market.models import MarketError
from trading212_mcp.market.sec import SECStatements, sec_fundamentals


@pytest.fixture
def facts():
    def annual(year, value, accn="annual-2025", filed="2026-02-01"):
        return {
            "start": f"{year}-01-01",
            "end": f"{year}-12-31",
            "val": value,
            "accn": accn,
            "filed": filed,
            "form": "10-K",
            "fp": "FY",
        }

    def concept(tag, values):
        return tag, {"units": {"USD": values}}

    return {
        "cik": 1,
        "facts": {
            "us-gaap": dict(
                [
                    concept("Revenues", [annual(2025, 120), annual(2024, 100)]),
                    concept("NetIncomeLoss", [annual(2025, 24), annual(2024, 20)]),
                    concept("OperatingIncomeLoss", [annual(2025, 30)]),
                    concept(
                        "NetCashProvidedByUsedInOperatingActivities", [annual(2025, 40)]
                    ),
                    concept(
                        "PaymentsToAcquirePropertyPlantAndEquipment", [annual(2025, 10)]
                    ),
                    concept(
                        "CashAndCashEquivalentsAtCarryingValue",
                        [{"end": "2025-12-31", "val": 50, "accn": "annual-2025"}],
                    ),
                ]
            )
        },
    }


ASOF = datetime(2026, 11, 7, tzinfo=timezone.utc)


def test_annual_metrics_authoritative_periods_not_ttm(facts):
    result = sec_fundamentals(facts, ASOF)
    assert result.revenue_growth == pytest.approx(0.2)
    assert result.earnings_growth == pytest.approx(0.2)
    assert result.net_margin == 0.2 and result.operating_margin == 0.25
    assert (
        result.operating_cash_flow == 40
        and result.free_cash_flow == 30
        and result.cash == 50
    )
    assert result.source == "sec_edgar_annual" and result.statement_currency == "USD"
    assert result.pe is None and result.forward_pe is None and result.roic is None
    assert "annual-2025" in result.provenance["current_annual"]


def test_quarterly_and_ytd_not_annual(facts):
    row = facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"][0]
    row.update({"start": "2025-10-01", "form": "10-Q", "fp": "Q4", "val": 9999})
    result = sec_fundamentals(facts, ASOF)
    assert result.revenue_growth is None and result.net_margin is None


def test_restated_latest_accession_no_cross_filing_fill(facts):
    tags = facts["facts"]["us-gaap"]
    for tag, val in [("Revenues", 150), ("NetIncomeLoss", 30)]:
        row = deepcopy(tags[tag]["units"]["USD"][0])
        row.update(val=val, filed="2026-05-01", accn="amended", form="10-K/A")
        tags[tag]["units"]["USD"].append(row)
    result = sec_fundamentals(facts, ASOF)
    assert result.revenue_growth == pytest.approx(0.5)
    assert result.net_margin == 0.2
    assert result.free_cash_flow is None and result.cash is None
    assert "amended" in result.provenance["current_annual"]


def test_mismatched_period_capex_and_currency_not_combined(facts):
    tags = facts["facts"]["us-gaap"]
    tags["PaymentsToAcquirePropertyPlantAndEquipment"]["units"]["USD"][0]["accn"] = (
        "other"
    )
    assert sec_fundamentals(facts, ASOF).free_cash_flow is None
    tags["NetIncomeLoss"]["units"]["EUR"] = deepcopy(
        tags["NetIncomeLoss"]["units"]["USD"]
    )
    with pytest.raises(MarketError, match="currency"):
        sec_fundamentals(facts, ASOF)


def test_negative_denominator_and_missing_fact_not_zero(facts):
    tags = facts["facts"]["us-gaap"]
    tags["NetIncomeLoss"]["units"]["USD"][1]["val"] = -5
    tags.pop("PaymentsToAcquirePropertyPlantAndEquipment")
    result = sec_fundamentals(facts, ASOF)
    assert result.earnings_growth is None and result.free_cash_flow is None


def test_conflicting_duplicate_facts_rejected(facts):
    rows = facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    row = deepcopy(rows[0])
    row["val"] = 999
    rows.append(row)
    with pytest.raises(MarketError, match="Conflicting"):
        sec_fundamentals(facts, ASOF)


def test_no_future_filing_data(facts):
    with pytest.raises(MarketError, match="no supported annual"):
        sec_fundamentals(facts, datetime(2025, 1, 1, tzinfo=timezone.utc))


def test_ifrs_not_silently_mapped():
    with pytest.raises(MarketError, match="IFRS"):
        sec_fundamentals({"facts": {"ifrs-full": {}}}, ASOF)


@pytest.mark.parametrize(
    "agent", [None, "", "Anonymous", "App\r\nX-Key: contact@example.com"]
)
async def test_contact_user_agent_required(market_settings, agent):
    market_settings.sec_user_agent = agent
    client = SECStatements(
        market_settings,
        httpx.MockTransport(
            lambda request: pytest.fail("No SEC request without real contact details")
        ),
    )
    try:
        with pytest.raises(MarketError, match="User-Agent|USER_AGENT"):
            await client._get("https://www.sec.gov/files/company_tickers_exchange.json")
    finally:
        await client.aclose()


async def test_sec_client_identity_headers_and_cache(
    market_settings, provider, facts, monkeypatch
):
    monkeypatch.setattr("trading212_mcp.market.sec.asyncio.sleep", AsyncMock())
    market_settings.sec_user_agent = "Trading212Research contact@example.com"
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "www.sec.gov":
            return httpx.Response(
                200,
                json={
                    "fields": ["cik", "name", "ticker", "exchange"],
                    "data": [[1, "Alpha", "AAA", "Nasdaq"]],
                },
            )
        return httpx.Response(200, json=facts)

    sec = SECStatements(market_settings, httpx.MockTransport(handler))
    try:
        result = await sec.fundamentals(provider.companies["AAA"])
        await sec.fundamentals(provider.companies["AAA"])
        assert result.source == "sec_edgar_annual" and len(requests) == 2
        assert (
            requests[0].headers["User-Agent"]
            == "Trading212Research contact@example.com"
        )
        assert requests[1].url.path.endswith("CIK0000000001.json")
        assert all(request.method == "GET" for request in requests)
    finally:
        await sec.aclose()


async def test_sec_exchange_mismatch_not_name_guessed(market_settings, provider):
    market_settings.sec_user_agent = "App contact@example.com"
    sec = SECStatements(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "fields": ["cik", "ticker", "exchange"],
                    "data": [[1, "AAA", "NYSE"]],
                },
            )
        ),
    )
    try:
        with pytest.raises(MarketError, match="exact ticker/exchange"):
            await sec.fundamentals(provider.companies["AAA"])
    finally:
        await sec.aclose()


@pytest.mark.parametrize("status", [403, 429, 500])
async def test_sec_provider_failure_and_fair_access(market_settings, provider, status):
    market_settings.sec_user_agent = "App contact@example.com"
    sec = SECStatements(
        market_settings,
        httpx.MockTransport(
            lambda r: httpx.Response(status, json={"private": "do not expose"})
        ),
    )
    try:
        with pytest.raises(MarketError) as error:
            await sec.fundamentals(provider.companies["AAA"])
        assert "private" not in str(error.value)
    finally:
        await sec.aclose()


async def test_explicit_supplement_never_falls_back(provider):
    sec = AsyncMock(spec=SECStatements)
    sec.fundamentals.side_effect = MarketError("No annual data")
    provider.fundamentals = AsyncMock(
        side_effect=AssertionError("Do not blend provider metrics")
    )
    sec.name = "sec_edgar_annual"
    combined = FundamentalsProvider(provider, sec)
    assert combined.name == "fixture_provider+sec_edgar_annual"
    assert await combined.profile("AAA") == provider.companies["AAA"]
    with pytest.raises(MarketError, match="No annual data"):
        await combined.fundamentals(provider.companies["AAA"])
    provider.fundamentals.assert_not_called()
    await combined.aclose()


async def test_factory_fmp_preserved_as_explicit_alternative(market_settings):
    market_settings.provider = "fmp"
    market_settings.fundamentals_source = "provider"
    provider = create_provider(market_settings)
    assert isinstance(provider, FMPProvider)
    await provider.aclose()


async def test_sec_mode_response_does_not_insert_vendor_valuation(
    provider, market_settings, facts
):
    from trading212_mcp.market.service import MarketService

    sec = AsyncMock(spec=SECStatements)
    sec.fundamentals.return_value = sec_fundamentals(facts, ASOF)
    sec.name = "sec_edgar_annual"
    combined = FundamentalsProvider(provider, sec)
    result = await MarketService(combined, market_settings).market_data(
        ["AAA"], 3, True
    )
    financial = result["assets"][0]["fundamentals"]
    assert result["source"] == "fixture_provider+sec_edgar_annual"
    assert financial["source"] == "sec_edgar_annual"
    assert (
        financial["pe"] is None
    )  # Fixture vendor has a PE, but SEC mode never fills it.
    assert financial["statement_date"] == "2025-12-31"
    assert result["assets"][0]["source"] == "fixture_provider"


async def test_default_yahoo_sec_missing_contact_does_not_block_prices_or_risk(
    market_settings, provider, trading, monkeypatch
):
    from trading212_mcp.market.service import MarketService

    monkeypatch.setattr(
        "trading212_mcp.market.factory.YFinanceProvider", lambda settings: provider
    )
    assert market_settings.provider == "yfinance"
    assert market_settings.fundamentals_source == "sec"
    combined = create_provider(market_settings)
    assert isinstance(combined, FundamentalsProvider)
    service = MarketService(combined, market_settings)
    try:
        market = await service.market_data(["AAA"], 3, True)
        assert market["assets"][0]["observations"] == 300
        assert "fundamentals" in market["assets"][0]["missing_data"]
        assert any("USER_AGENT" in note for note in market["assets"][0]["warnings"])
        risk = await service.portfolio_risk(trading, 3, "SPY")
        assert risk["risk_metrics"]["observations"] >= 20
    finally:
        await combined.aclose()


async def test_fmp_fundamentals_can_be_selected_with_yahoo_prices(
    market_settings, provider, monkeypatch
):
    market_settings.fundamentals_source = "fmp"
    monkeypatch.setattr(
        "trading212_mcp.market.factory.YFinanceProvider", lambda settings: provider
    )
    financial = AsyncMock(spec=FMPProvider)
    financial.name = "financial_modeling_prep"
    monkeypatch.setattr(
        "trading212_mcp.market.factory.FMPProvider", lambda settings: financial
    )
    combined = create_provider(market_settings)
    assert combined.name == "fixture_provider+financial_modeling_prep"
    await combined.profile("AAA")
    await combined.fundamentals(provider.companies["AAA"])
    financial.fundamentals.assert_awaited_once()
    await combined.aclose()


def test_sec_exposes_reported_revenue_earnings_and_assets(facts):
    facts["facts"]["us-gaap"]["Assets"] = {
        "units": {"USD": [{"end": "2025-12-31", "val": 500, "accn": "annual-2025"}]}
    }
    result = sec_fundamentals(facts, ASOF)
    assert (
        result.revenue == 120 and result.net_income == 24 and result.total_assets == 500
    )
    assert result.statement_currency == "USD"


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("private network"), httpx.ReadTimeout("private timeout")],
)
async def test_sec_network_failures_are_sanitised(market_settings, provider, failure):
    market_settings.sec_user_agent = "App contact@example.com"

    def unavailable(request):
        raise failure

    sec = SECStatements(market_settings, httpx.MockTransport(unavailable))
    try:
        with pytest.raises(MarketError, match="failed or timed out") as error:
            await sec.fundamentals(provider.companies["AAA"])
        assert "private" not in str(error.value)
    finally:
        await sec.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"fields": ["cik", "ticker"], "data": []},
        {"fields": ["cik", "ticker", "exchange"], "data": [[999, "OTHER", "Nasdaq"]]},
    ],
)
async def test_sec_partial_or_missing_company_index_fails_closed(
    market_settings, provider, payload
):
    market_settings.sec_user_agent = "App contact@example.com"
    sec = SECStatements(
        market_settings,
        httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    try:
        with pytest.raises(MarketError):
            await sec.fundamentals(provider.companies["AAA"])
    finally:
        await sec.aclose()


def test_annual_history_preserves_period_accession_and_missing_debt(facts):
    result = sec_fundamentals(facts, ASOF)
    assert [r.end.isoformat() for r in result.annual_history] == [
        "2025-12-31",
        "2024-12-31",
    ]
    current = result.annual_history[0]
    assert current.start.isoformat() == "2025-01-01"
    assert current.filed.isoformat() == "2026-02-01"
    assert current.accession == "annual-2025"
    assert current.free_cash_flow == 30
    assert current.total_debt is None
    assert result.annual_history[1].free_cash_flow is None


def test_future_filed_facts_do_not_enter_history(facts):
    for concept in facts["facts"]["us-gaap"].values():
        for row in concept["units"]["USD"]:
            if row.get("start") == "2025-01-01":
                row["filed"] = "2027-01-01"
    result = sec_fundamentals(facts, ASOF)
    assert all(r.end.year == 2024 for r in result.annual_history)
