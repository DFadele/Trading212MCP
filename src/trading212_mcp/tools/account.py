from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import AccountSummaryResult


def register_account_tools(
    server: FastMCP, get_client: Callable[[], Trading212Client]
) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_account_summary() -> AccountSummaryResult:
        """Get Trading 212 cash, investments, P&L and total account value.

        Values are in the account currency. Monetary amounts are decimal strings
        to preserve precision. Includes demo/live environment and UTC retrieval
        time. This reads the configured account and never places trades.
        """
        try:
            return await get_client().get_account_summary()
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None
