from datetime import date

from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.fmp import FMPProvider
from trading212_mcp.market.models import Company, Fundamentals, PriceHistory, Universe
from trading212_mcp.market.provider import MarketProvider
from trading212_mcp.market.sec import SECStatements
from trading212_mcp.market.yahoo import YFinanceProvider


class FundamentalsProvider:
    """Explicit whole-block replacement, never metric-by-metric fallback."""

    def __init__(
        self, base: MarketProvider, statements: SECStatements | MarketProvider
    ) -> None:
        self.base = base
        self.statements = statements
        self.name = base.name + "+" + statements.name

    async def profile(self, symbol: str) -> Company:
        return await self.base.profile(symbol)

    async def search_isin(
        self, isin: str, currency: str | None = None
    ) -> list[Company]:
        return await self.base.search_isin(isin, currency)

    async def history(self, company: Company, start: date, end: date) -> PriceHistory:
        return await self.base.history(company, start, end)

    async def fx(
        self, source: str, target: str, start: date, end: date
    ) -> PriceHistory:
        return await self.base.fx(source, target, start, end)

    async def fundamentals(self, company: Company) -> Fundamentals:
        return await self.statements.fundamentals(company)

    async def universe(
        self, filters: dict[str, str | float], max_candidates: int
    ) -> Universe:
        return await self.base.universe(filters, max_candidates)

    async def aclose(self) -> None:
        try:
            await self.statements.aclose()
        finally:
            await self.base.aclose()


def create_provider(settings: MarketSettings) -> MarketProvider:
    base: MarketProvider = (
        FMPProvider(settings)
        if settings.provider == "fmp"
        else YFinanceProvider(settings)
    )
    if settings.fundamentals_source == "sec":
        return FundamentalsProvider(base, SECStatements(settings))
    if settings.fundamentals_source == "fmp" and settings.provider != "fmp":
        return FundamentalsProvider(base, FMPProvider(settings))
    return base
