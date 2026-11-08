from collections.abc import Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from trading212_mcp.quantitative.portfolio import calculate_allocation
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import AllocationResult


def register_portfolio_tools(
    server: FastMCP,
    get_client: Callable[[], Trading212Client],
) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_portfolio_allocation() -> AllocationResult:
        """Get holding weights by current wallet value, excluding cash.

        Weights are percentages of the sum of open-position values, in the
        account currency. Values and percentages are decimal strings.
        Zero total value produces null weights; no holdings produces an empty
        list. Retrieval time refers to the positions snapshot.
        """
        try:
            client = get_client()
            account = await client.get_account_summary()
            positions = await client.get_positions()
            return calculate_allocation(positions, account.account.currency)
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None
