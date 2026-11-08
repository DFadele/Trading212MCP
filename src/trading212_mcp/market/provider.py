from datetime import date
from typing import Protocol

from trading212_mcp.market.models import Company, Fundamentals, PriceHistory, Universe


class MarketProvider(Protocol):
    name: str

    async def profile(self, symbol: str) -> Company: ...
    async def search_isin(
        self, isin: str, currency: str | None = None
    ) -> list[Company]: ...
    async def history(
        self, company: Company, start: date, end: date
    ) -> PriceHistory: ...
    async def fundamentals(self, company: Company) -> Fundamentals: ...
    async def fx(
        self, source: str, target: str, start: date, end: date
    ) -> PriceHistory: ...
    async def universe(
        self, filters: dict[str, str | float], max_candidates: int
    ) -> Universe: ...
    async def aclose(self) -> None: ...
