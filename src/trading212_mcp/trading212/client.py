import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import TypeVar
from urllib.parse import parse_qs, urlsplit

import httpx
from pydantic import TypeAdapter, ValidationError

from trading212_mcp.config import Settings
from trading212_mcp.observability import timed_async
from trading212_mcp.trading212.errors import Trading212Error
from trading212_mcp.trading212.models import (
    AccountSummary,
    AccountSummaryResult,
    Dividend,
    HistoricalOrder,
    HistoryPage,
    HistoryResult,
    Order,
    PendingOrdersResult,
    Position,
    PositionsResult,
    Transaction,
)

Item = TypeVar("Item")

INTERVALS = {
    "equity/account/summary": 5.0,
    "equity/positions": 1.0,
    "equity/orders": 5.0,
    "equity/history/orders": 3.0,
    "equity/history/dividends": 3.0,
    "equity/history/transactions": 3.0,
}


class Trading212Client:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.base_url,
            auth=httpx.BasicAuth(
                settings.api_key.get_secret_value(),
                settings.api_secret.get_secret_value(),
            ),
            timeout=20.0,
            follow_redirects=False,
        )
        self._locks = {path: asyncio.Lock() for path in INTERVALS}
        self._next_request: dict[str, float] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    @timed_async("account_retrieval")
    async def _get(
        self, path: str, params: dict[str, str | int] | None = None
    ) -> httpx.Response:
        # Serialize requests in this process; other clients share the upstream limit.
        async with self._locks[path]:
            delay = self._next_request.get(path, 0.0) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_request[path] = time.monotonic() + INTERVALS[path]
            try:
                response = await self._http.get(path, params=params)
            except httpx.TimeoutException:
                raise Trading212Error(
                    "Trading 212 request timed out. Try again later."
                ) from None
            except httpx.RequestError:
                raise Trading212Error("Unable to connect to Trading 212.") from None

            # Pace from completion: variable network latency must not put arrivals
            # inside the upstream interval. Other clients still share this limit.
            self._next_request[path] = max(
                self._next_request[path], time.monotonic() + INTERVALS[path] + 0.05
            )
            if (
                response.status_code == 429
                or response.headers.get("x-ratelimit-remaining") == "0"
            ):
                # Respect reset headers on successful final-quota requests too.
                try:
                    reset_delay = (
                        float(response.headers["x-ratelimit-reset"]) - time.time()
                    )
                    if 0 < reset_delay < 86400:
                        self._next_request[path] = max(
                            self._next_request[path], time.monotonic() + reset_delay
                        )
                except (KeyError, ValueError):
                    pass
            if response.status_code == 429:
                raise Trading212Error(
                    "Trading 212 rate limit reached. Try again later."
                )
            if response.status_code == 401:
                raise Trading212Error(
                    "Trading 212 authentication failed. Check credentials and environment."
                )
            if response.status_code == 403:
                raise Trading212Error(
                    "Trading 212 denied access. Check API permissions and IP restrictions."
                )
            if response.status_code != 200:
                raise Trading212Error(
                    f"Trading 212 request failed (HTTP {response.status_code})."
                )

            return response

    async def get_account_summary(self) -> AccountSummaryResult:
        response = await self._get("equity/account/summary")
        try:
            payload = json.loads(response.content, parse_float=Decimal)
            account = AccountSummary.model_validate(payload)
        except (ValueError, ValidationError):
            raise Trading212Error(
                "Trading 212 returned an invalid account summary."
            ) from None
        return AccountSummaryResult(
            environment=self.settings.environment,
            retrieved_at=datetime.now(timezone.utc),
            account=account,
        )

    async def get_positions(self) -> PositionsResult:
        response = await self._get("equity/positions")
        try:
            payload = json.loads(response.content, parse_float=Decimal)
            positions = TypeAdapter(list[Position]).validate_python(payload)
        except (ValueError, ValidationError):
            raise Trading212Error(
                "Trading 212 returned invalid positions data."
            ) from None
        return PositionsResult(
            environment=self.settings.environment,
            retrieved_at=datetime.now(timezone.utc),
            positions=positions,
        )

    async def get_pending_orders(self) -> PendingOrdersResult:
        response = await self._get("equity/orders")
        try:
            orders = TypeAdapter(list[Order]).validate_python(
                json.loads(response.content, parse_float=Decimal)
            )
        except ValueError:
            raise Trading212Error(
                "Trading 212 returned invalid pending orders."
            ) from None
        return PendingOrdersResult(
            environment=self.settings.environment,
            retrieved_at=datetime.now(timezone.utc),
            orders=orders,
        )

    async def _history(
        self,
        path: str,
        page_type: type[HistoryPage[Item]],
        limit: int,
        cursor: str | None,
        filters: dict[str, str],
    ) -> HistoryResult[Item]:
        if not 1 <= limit <= 50:
            raise Trading212Error("History limit must be between 1 and 50.")
        params: dict[str, str | int] = {"limit": limit, **filters}
        if cursor is not None:
            params["cursor"] = cursor
        response = await self._get(path, params=params)
        try:
            page = page_type.model_validate(
                json.loads(response.content, parse_float=Decimal)
            )
            next_cursor = None
            if page.nextPagePath is not None:
                next_page = urlsplit(page.nextPagePath)
                cursors = parse_qs(next_page.query).get("cursor", [])
                if (
                    next_page.scheme
                    or next_page.netloc
                    or next_page.fragment
                    or next_page.path != f"/api/v0/{path}"
                    or len(cursors) != 1
                ):
                    raise ValueError("Invalid pagination path")
                next_cursor = cursors[0]
        except ValueError:
            raise Trading212Error(
                "Trading 212 returned invalid history data."
            ) from None
        return HistoryResult[Item](
            environment=self.settings.environment,
            retrieved_at=datetime.now(timezone.utc),
            items=page.items,
            next_cursor=next_cursor,
        )

    async def get_order_history(
        self,
        limit: int = 20,
        cursor: str | None = None,
        ticker: str | None = None,
    ) -> HistoryResult[HistoricalOrder]:
        return await self._history(
            "equity/history/orders",
            HistoryPage[HistoricalOrder],
            limit,
            cursor,
            {"ticker": ticker} if ticker else {},
        )

    async def get_dividend_history(
        self,
        limit: int = 20,
        cursor: str | None = None,
        ticker: str | None = None,
    ) -> HistoryResult[Dividend]:
        return await self._history(
            "equity/history/dividends",
            HistoryPage[Dividend],
            limit,
            cursor,
            {"ticker": ticker} if ticker else {},
        )

    async def get_transaction_history(
        self,
        limit: int = 20,
        cursor: str | None = None,
        since: str | None = None,
    ) -> HistoryResult[Transaction]:
        return await self._history(
            "equity/history/transactions",
            HistoryPage[Transaction],
            limit,
            cursor,
            {"time": since} if since else {},
        )
