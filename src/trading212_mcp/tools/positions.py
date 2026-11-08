from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import PositionsResult


def register_position_tools(
    server: FastMCP, get_client: Callable[[], Trading212Client]
) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_positions() -> PositionsResult:
        """Get all open Trading 212 holdings, quantities, values and unrealized P&L.

        Prices are in instrument.currency; walletImpact values are in
        walletImpact.currency. Amounts and quantities are decimal strings.
        Includes demo/live environment and UTC retrieval time. An empty list
        means there are no open positions. Never places trades.
        """
        try:
            return await get_client().get_positions()
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None
