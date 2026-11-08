import argparse
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import ValidationError

from trading212_mcp.auth import OwnerTokenVerifier
from trading212_mcp.config import Settings
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.factory import create_provider
from trading212_mcp.market.models import MarketError
from trading212_mcp.market.service import MarketService
from trading212_mcp.observability import configure_logging, logger, span
from trading212_mcp.runtime_config import RuntimeSettings
from trading212_mcp.tools.account import register_account_tools
from trading212_mcp.tools.history import register_history_tools
from trading212_mcp.tools.orders import register_order_tools
from trading212_mcp.tools.portfolio import register_portfolio_tools
from trading212_mcp.tools.positions import register_position_tools
from trading212_mcp.tools.research import register_research_tools
from trading212_mcp.trading212.client import Trading212Client


class ServerRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Trading212Client | None = None
        self.market: MarketService | None = None
        self.market_settings: MarketSettings | None = None

    async def start(self) -> None:
        if self.client is None:
            self.client = Trading212Client(self.settings)

    async def close(self) -> None:
        try:
            if self.market is not None:
                await self.market.provider.aclose()
        finally:
            self.market = None
            if self.client is not None:
                await self.client.aclose()
                self.client = None
            logger.info("", extra={"event": "runtime_stopped"})

    def get_client(self) -> Trading212Client:
        if self.client is None:
            raise RuntimeError("Trading 212 client is not running")
        return self.client

    def get_market(self) -> MarketService:
        if self.market is None:
            try:
                market_settings = self.market_settings or MarketSettings()
            except ValidationError:
                raise MarketError(
                    "Invalid MARKET_ configuration; check market data settings."
                ) from None
            self.market = MarketService(
                create_provider(market_settings), market_settings
            )
        return self.market


class ObservedFastMCP(FastMCP):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        # SDK v1's single ToolManager hook keeps conversion separate from execution.
        tool = self._tool_manager.get_tool(name)
        with span("tool_execution", tool.name if tool else "unknown_tool"):
            result = await self._tool_manager.call_tool(
                name, arguments, context=self.get_context(), convert_result=False
            )
        tool = self._tool_manager.get_tool(name)
        if tool is None:
            raise RuntimeError("Tool unavailable")
        with span("response_serialization", "sdk_content_and_structured_result"):
            return tool.fn_metadata.convert_result(result)


def create_server(
    settings: Settings,
    runtime: ServerRuntime | None = None,
    *,
    auth: AuthSettings | None = None,
    token_verifier: OwnerTokenVerifier | None = None,
    transport_security: TransportSecuritySettings | None = None,
) -> FastMCP:
    runtime = runtime or ServerRuntime(settings)

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncGenerator[None, None]:
        owned = runtime.client is None
        if owned:
            await runtime.start()
        try:
            yield None
        finally:
            if owned:
                await runtime.close()

    get_client, get_market = runtime.get_client, runtime.get_market
    server = ObservedFastMCP(
        "Trading 212",
        lifespan=lifespan,
        auth=auth,
        token_verifier=token_verifier,
        transport_security=transport_security,
        stateless_http=True,
        json_response=True,
    )
    register_account_tools(server, get_client)
    register_position_tools(server, get_client)
    register_portfolio_tools(server, get_client)
    register_order_tools(server, get_client)
    register_history_tools(server, get_client)
    register_research_tools(server, get_client, get_market)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Trading 212 MCP (stdio or authenticated HTTP)"
    )
    parser.add_argument("--transport", choices=["stdio", "streamable-http"])
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    configure_logging()
    try:
        overrides = {
            name: value for name, value in vars(args).items() if value is not None
        }
        runtime_settings = RuntimeSettings(**overrides)
        settings = Settings(
            _env_file=None  # pyright: ignore[reportCallIssue]
            if runtime_settings.transport == "streamable-http"
            else ".env"
        )  # pyright: ignore[reportCallIssue]
    except ValidationError:
        print(
            "Invalid configuration. Check TRADING212_ credentials and MCP_ transport/authentication settings; values are not logged.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if runtime_settings.transport == "stdio":
        create_server(settings).run(transport="stdio")
    else:
        import uvicorn

        from trading212_mcp.http import create_http_app

        uvicorn.run(
            create_http_app(settings, runtime_settings),
            host=runtime_settings.host,
            port=runtime_settings.port,
            access_log=False,
            log_config=None,
            proxy_headers=False,
            timeout_graceful_shutdown=runtime_settings.shutdown_seconds,
        )
