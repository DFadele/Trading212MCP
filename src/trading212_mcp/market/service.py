from datetime import date, timedelta
from typing import Any

import pandas as pd

from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.mapping_cache import MappingCache
from trading212_mcp.market.models import Company, MarketError, PriceHistory
from trading212_mcp.market.provider import MarketProvider
from trading212_mcp.market.resolution import InstrumentResolver, currency_unit
from trading212_mcp.trading212.client import Trading212Client


def period_start(years: int, end: date) -> date:
    try:
        return end.replace(year=end.year - years)
    except ValueError:
        return end.replace(year=end.year - years, day=28)


def price_series(history: PriceHistory) -> pd.Series:
    return pd.Series(
        [point.close for point in history.prices],
        index=pd.DatetimeIndex([point.date for point in history.prices]),
        name=history.symbol,
        dtype=float,
    ).sort_index()


class MarketService:
    def __init__(self, provider: MarketProvider, settings: MarketSettings) -> None:
        self.provider = provider
        self.settings = settings
        self.resolver = InstrumentResolver(
            provider,
            settings.instrument_overrides,
            MappingCache(
                settings.resolution_cache_path, settings.resolution_cache_ttl_days
            ),
        )

    async def converted_history(
        self,
        company: Company,
        start: date,
        end: date,
        target: str,
    ) -> tuple[pd.Series, PriceHistory, list[str]]:
        history = await self.provider.history(company, start, end)
        series = price_series(history)
        source, scale = currency_unit(history.currency)
        target_currency, target_scale = currency_unit(target)
        if target_scale != 1:
            raise MarketError(
                "Target currency must be a major currency unit, not pence/cents."
            )
        series = series * scale
        warnings = list(history.warnings)
        if source != target_currency:
            fx = await self.provider.fx(
                source, target_currency, start - timedelta(days=4), end
            )
            rates = price_series(fx).reindex(
                series.index, method="ffill", tolerance=pd.Timedelta(days=4)
            )
            if rates.isna().any():
                warnings.append(
                    "Some FX dates are missing; prices on those dates are excluded."
                )
            series = series * rates
            warnings.append(
                f"Converted {source} to {target_currency} using daily spot FX; backward fill of FX only, at most four calendar days. FX retrieved {fx.retrieved_at.isoformat()}."
            )
        if series.index[-1].date() < end - timedelta(days=7):
            warnings.append("Latest price is more than seven calendar days old.")
        if series.index[0].date() > start + timedelta(days=10):
            warnings.append(
                "Available history starts later than the requested lookback."
            )
        return series.rename(company.symbol), history, warnings

    async def market_data(
        self,
        tickers: list[str],
        lookback_years: int,
        include_fundamentals: bool,
        include_series: bool = False,
        base_currency: str | None = None,
    ) -> dict[str, Any]:
        from trading212_mcp.quantitative.market_data import get_market_data

        return await get_market_data(
            self,
            tickers,
            lookback_years,
            include_fundamentals,
            include_series,
            base_currency,
        )

    async def portfolio_risk(
        self,
        trading: Trading212Client,
        lookback_years: int,
        benchmark: str,
        annual_risk_free_rate: float = 0.0,
        confidence: float = 0.95,
        listing_selections: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        from trading212_mcp.quantitative.risk import analyse_portfolio_risk

        return await analyse_portfolio_risk(
            self,
            trading,
            lookback_years,
            benchmark,
            annual_risk_free_rate,
            confidence,
            listing_selections,
        )
