import json

from trading212_mcp.market.mapping_cache import MappingCache
from trading212_mcp.market.models import Company, MarketError
from trading212_mcp.market.provider import MarketProvider
from trading212_mcp.observability import timed_async
from trading212_mcp.trading212.models import Instrument


def currency_unit(currency: str) -> tuple[str, float]:
    # Case is meaningful: GBp denotes pence, GBP denotes pounds.
    if currency in ("GBp", "GBX"):
        return "GBP", 0.01
    if currency in ("ZAc", "ZAC"):
        return "ZAR", 0.01
    return currency.upper(), 1.0


class InstrumentResolver:
    def __init__(
        self,
        provider: MarketProvider,
        overrides: dict[str, str],
        cache: MappingCache | None = None,
    ) -> None:
        self.provider = provider
        self.overrides = overrides
        self.cache = cache

    @timed_async("identity_resolution")
    async def resolve(
        self, instrument: Instrument, selected_symbol: str | None = None
    ) -> Company:
        if not instrument.isin:
            raise MarketError(
                f"{instrument.ticker}: missing ISIN; cannot safely match security."
            )
        key = json.dumps(
            [
                "selection-v1",
                self.provider.name.partition("+")[0],
                instrument.ticker,
                instrument.isin,
                currency_unit(instrument.currency)[0],
            ]
        )
        stored = self.cache.get(key) if self.cache else None
        override = selected_symbol or self.overrides.get(instrument.ticker)
        cached_symbol = stored.get("symbol") if stored else None
        candidates = await self.provider.search_isin(
            instrument.isin, instrument.currency
        )
        matches = {
            company.symbol: company
            for company in candidates
            if company.isin == instrument.isin
            and currency_unit(company.currency)[0]
            == currency_unit(instrument.currency)[0]
        }
        if (
            override is None
            and cached_symbol not in matches
            and instrument.ticker.endswith("_US_EQ")
        ):
            # Broker market is a venue hint only; ISIN/currency proof above is mandatory.
            matches = {
                symbol: company
                for symbol, company in matches.items()
                if company.exchange
                in {"NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "BATS"}
            }
        selection = override or (cached_symbol if cached_symbol in matches else None)
        if selection is not None:
            matches = {
                symbol: company
                for symbol, company in matches.items()
                if symbol == selection
            }
        if len(matches) != 1:
            reason = (
                "ambiguous listings" if matches else "no verified ISIN/currency match"
            )
            raise MarketError(
                f"{instrument.ticker}: {reason}. Verified candidates: {', '.join(sorted(company.symbol for company in candidates if company.isin == instrument.isin and currency_unit(company.currency)[0] == currency_unit(instrument.currency)[0])) or 'none'}. "
                "For ambiguity, ask the user to select a listing, then pass listing_selections to analyse_portfolio_risk; a verified choice is remembered."
            )
        company = next(iter(matches.values()))
        if self.cache:
            self.cache.put(key, {"symbol": company.symbol})
        return company
