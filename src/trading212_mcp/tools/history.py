from collections.abc import Callable
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import AwareDatetime, Field

from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import (
    Dividend,
    HistoricalOrder,
    HistoryResult,
    Transaction,
)

PageLimit = Annotated[int, Field(ge=1, le=50)]
NumericCursor = Annotated[str, Field(pattern=r"^[0-9]+$", max_length=20)]
Ticker = Annotated[str, Field(min_length=1, max_length=100)]
TransactionCursor = Annotated[str, Field(min_length=1, max_length=1000)]


def register_history_tools(
    server: FastMCP,
    get_client: Callable[[], Trading212Client],
) -> None:
    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_order_history(
        limit: PageLimit = 20,
        cursor: NumericCursor | None = None,
        ticker: Ticker | None = None,
    ) -> HistoryResult[HistoricalOrder]:
        """Get one page of historical orders and execution details.

        Optional ticker uses Trading 212 IDs such as AAPL_US_EQ. Pass the
        returned next_cursor with the same limit and ticker for the next page;
        null means finished. A page is not the complete account history.
        Prices use order currency; fill wallet amounts use wallet currency.
        Monetary values and quantities are decimal strings.
        """
        try:
            return await get_client().get_order_history(limit, cursor, ticker)
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_dividend_history(
        limit: PageLimit = 20,
        cursor: NumericCursor | None = None,
        ticker: Ticker | None = None,
    ) -> HistoryResult[Dividend]:
        """Get one page of paid dividends, optionally filtered by Trading 212 ticker.

        Pass next_cursor with the same filters and limit to continue; null means
        finished. This is one page, not total dividend income. amount is in
        account currency; grossAmountPerShare is in instrument currency.
        Monetary values and quantities are decimal strings.
        """
        try:
            return await get_client().get_dividend_history(limit, cursor, ticker)
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_transaction_history(
        limit: PageLimit = 20,
        cursor: TransactionCursor | None = None,
        since: AwareDatetime | None = None,
    ) -> HistoryResult[Transaction]:
        """Get one page of account cash movements, including deposits and withdrawals.

        since is an optional ISO 8601 start time with timezone, passed as the
        upstream time filter. Pass next_cursor with the same since and limit
        to continue; null means finished. Amounts are decimal strings in each
        transaction's currency. This is not trade execution history.
        """
        try:
            return await get_client().get_transaction_history(
                limit,
                cursor,
                since.isoformat() if since else None,
            )
        except Trading212Error as exc:
            raise ToolError(str(exc)) from None
