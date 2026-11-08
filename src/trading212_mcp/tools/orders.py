from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import PendingOrdersResult


def register_order_tools(
    server: FastMCP,
    get_client: Callable[[], Trading212Client],
) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_pending_orders() -> PendingOrdersResult:
        """Get all active Trading 212 orders; never creates or cancels orders.

        Includes unfilled and partially filled orders. Prices and amounts use
        each order's currency. Numbers are decimal strings. Order-specific
        fields can be null when not applicable. No active orders returns [].
        """
        try:
            return await get_client().get_pending_orders()
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None
