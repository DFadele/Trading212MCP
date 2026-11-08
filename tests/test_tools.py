from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from trading212_mcp.config import Settings
from trading212_mcp.market.models import MarketError
from trading212_mcp.server import create_server
from trading212_mcp.tools.account import register_account_tools
from trading212_mcp.tools.history import register_history_tools
from trading212_mcp.tools.orders import register_order_tools
from trading212_mcp.tools.portfolio import register_portfolio_tools
from trading212_mcp.tools.positions import register_position_tools
from trading212_mcp.tools.research import register_research_tools
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.models import HistoryResult, PendingOrdersResult

OLD_NAMES = {
    "get_account_summary",
    "get_positions",
    "get_portfolio_allocation",
    "get_pending_orders",
    "get_order_history",
    "get_dividend_history",
    "get_transaction_history",
}


async def test_twelve_tools_discoverable_no_market_credentials(monkeypatch):
    monkeypatch.delenv("MARKET_FMP_API_KEY", raising=False)
    settings = Settings(api_key="placeholder", api_secret="placeholder", _env_file=None)
    tools = await create_server(settings).list_tools()
    assert {tool.name for tool in tools} == OLD_NAMES | {
        "get_market_data",
        "analyse_portfolio_risk",
        "screen_stocks",
        "analyse_candidate_investment",
        "optimise_new_contribution",
    }
    assert all(
        tool.annotations.readOnlyHint and not tool.annotations.destructiveHint
        for tool in tools
    )
    for tool in tools:
        assert tool.outputSchema


async def test_old_tool_response_contracts_and_provider_isolation(trading):
    server = FastMCP("compatibility")
    now = datetime.now(timezone.utc)
    trading.get_pending_orders = AsyncMock(
        return_value=PendingOrdersResult(
            environment="demo", retrieved_at=now, orders=[]
        )
    )
    page = HistoryResult(
        environment="demo", retrieved_at=now, items=[], next_cursor=None
    )
    for name in (
        "get_order_history",
        "get_dividend_history",
        "get_transaction_history",
    ):
        setattr(trading, name, AsyncMock(return_value=page))
    for register in (
        register_account_tools,
        register_position_tools,
        register_portfolio_tools,
        register_order_tools,
        register_history_tools,
    ):
        register(server, lambda: trading)

    def unavailable():
        raise MarketError("Market provider unavailable")

    register_research_tools(server, lambda: trading, unavailable)
    expected = {
        "get_account_summary": {"environment", "retrieved_at", "account"},
        "get_positions": {"environment", "retrieved_at", "positions"},
        "get_portfolio_allocation": {
            "environment",
            "retrieved_at",
            "currency",
            "basis",
            "total_investment_value",
            "holdings",
        },
        "get_pending_orders": {"environment", "retrieved_at", "orders"},
        "get_order_history": {"environment", "retrieved_at", "items", "next_cursor"},
        "get_dividend_history": {"environment", "retrieved_at", "items", "next_cursor"},
        "get_transaction_history": {
            "environment",
            "retrieved_at",
            "items",
            "next_cursor",
        },
    }
    with pytest.raises(ToolError):
        await server.call_tool(
            "get_market_data", {"tickers": ["AAA"], "response_mode": "full"}
        )
    for name in OLD_NAMES:
        content, structured = await server.call_tool(name, {})
        assert set(structured) == expected[name]
    _, summary = await server.call_tool("get_account_summary", {})
    assert summary["account"]["totalValue"] == "1100"


async def test_new_tools_serialize_and_validate(service, trading):
    server = FastMCP("research")
    register_research_tools(server, lambda: trading, lambda: service)
    _, market = await server.call_tool(
        "get_market_data", {"tickers": ["AAA"], "response_mode": "full"}
    )
    assert market["assets"][0]["observations"] == 300
    _, risk = await server.call_tool(
        "analyse_portfolio_risk", {"response_mode": "full"}
    )
    assert risk["risk_metrics"]["observations"] >= 20
    _, screen = await server.call_tool(
        "screen_stocks", {"industries": ["Semiconductors"], "response_mode": "full"}
    )
    assert screen["candidates"][0]["company"]["symbol"] == "BBB"
    for name, args in [
        ("get_market_data", {"tickers": []}),
        ("screen_stocks", {"limit": 0}),
        ("analyse_portfolio_risk", {"confidence": 1}),
    ]:
        with pytest.raises(ToolError):
            await server.call_tool(name, args)


async def test_trading_client_pagination_and_read_only():
    settings = Settings(api_key="placeholder", api_secret="placeholder", _env_file=None)
    client = Trading212Client(settings)
    await client._http.aclose()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "items": [],
                "nextPagePath": "/api/v0/equity/history/orders?cursor=123&limit=20",
            },
        )

    client._http = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(handler)
    )
    try:
        page = await client.get_order_history(ticker="AAA")
        assert page.next_cursor == "123"
        assert requests[0].method == "GET" and requests[0].url.params["ticker"] == "AAA"
    finally:
        await client.aclose()


async def test_stdio_initialization_and_discovery():
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trading212_mcp"],
        env={
            "TRADING212_API_KEY": "protocol-placeholder",
            "TRADING212_API_SECRET": "protocol-placeholder",
            "TRADING212_ENVIRONMENT": "demo",
        },
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.list_tools()
            assert {tool.name for tool in result.tools} == OLD_NAMES | {
                "get_market_data",
                "analyse_portfolio_risk",
                "screen_stocks",
                "analyse_candidate_investment",
                "optimise_new_contribution",
            }


async def test_risk_progress_context_hidden_from_tool_schema(service, trading):
    server = FastMCP("progress-contract")
    register_research_tools(server, lambda: trading, lambda: service)
    tools = await server.list_tools()
    risk = next(tool for tool in tools if tool.name == "analyse_portfolio_risk")
    assert set(risk.inputSchema["properties"]) == {
        "lookback_years",
        "benchmark",
        "annual_risk_free_rate",
        "confidence",
        "listing_selections",
        "response_mode",
        "detail_symbols",
        "detail_sectors",
        "top_n",
        "report_id",
        "risk_components",
    }


async def test_risk_progress_notifications_stop_on_completion():
    import asyncio
    from types import SimpleNamespace

    from trading212_mcp.tools.research import risk_progress

    sent = asyncio.Event()

    async def notify(*args, **kwargs):
        sent.set()

    context = SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(progressToken="test")),
        report_progress=AsyncMock(side_effect=notify),
    )
    async with risk_progress(context):
        await asyncio.wait_for(sent.wait(), timeout=1)
    assert context.report_progress.await_count == 1
    assert context.report_progress.call_args.args[0] == 1


async def test_risk_listing_selection_is_validated_and_remembered(
    service, provider, trading
):
    provider.companies["ALIAS"] = provider.companies["AAA"].model_copy(
        update={"symbol": "ALIAS"}
    )
    provider.series["ALIAS"] = provider.series["AAA"]
    server = FastMCP("remembered-listings")
    register_research_tools(server, lambda: trading, lambda: service)
    _, ambiguous = await server.call_tool(
        "analyse_portfolio_risk", {"response_mode": "full"}
    )
    assert ambiguous["risk_metrics"] is None
    assert (
        "Verified candidates: AAA, ALIAS"
        in ambiguous["data_quality"]["excluded"][0]["reason"]
    )
    _, selected = await server.call_tool(
        "analyse_portfolio_risk",
        {"listing_selections": {"AAA_US_EQ": "ALIAS"}, "response_mode": "full"},
    )
    assert selected["portfolio_summary"]["holdings"][0]["market_symbol"] == "ALIAS"
    assert selected["risk_metrics"] is not None
    _, remembered = await server.call_tool(
        "analyse_portfolio_risk", {"response_mode": "full"}
    )
    assert remembered["portfolio_summary"]["holdings"][0]["market_symbol"] == "ALIAS"
    with pytest.raises(ToolError, match="not held"):
        await server.call_tool(
            "analyse_portfolio_risk",
            {"listing_selections": {"UNKNOWN": "AAA"}, "response_mode": "full"},
        )


async def test_trading_successful_last_quota_request_honours_reset():
    import time

    from trading212_mcp.trading212.client import INTERVALS

    settings = Settings(api_key="test", api_secret="test", _env_file=None)
    client = Trading212Client(settings)
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={},
                headers={
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": str(time.time() + 30),
                },
            )
        ),
    )
    try:
        await client._get("equity/positions")
        assert client._next_request["equity/positions"] > time.monotonic() + 25
        assert (
            client._next_request["equity/positions"]
            > time.monotonic() + INTERVALS["equity/positions"]
        )
    finally:
        await client.aclose()


async def test_trading_paces_from_response_completion(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(
        "trading212_mcp.trading212.client.time.monotonic", lambda: clock[0]
    )
    settings = Settings(api_key="test", api_secret="test", _env_file=None)
    client = Trading212Client(settings)
    await client._http.aclose()

    def response(request):
        clock[0] += 0.4
        return httpx.Response(200, json={})

    client._http = httpx.AsyncClient(
        base_url=settings.base_url, transport=httpx.MockTransport(response)
    )
    try:
        await client._get("equity/positions")
        assert client._next_request["equity/positions"] >= 101.4
    finally:
        await client.aclose()
