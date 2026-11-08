"""Yahoo prices/discovery for personal experiments; data rights remain separate."""

import asyncio
import importlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Protocol, cast

import pandas as pd
from pydantic import ValidationError

from trading212_mcp.market.cache import CachedResponse, TTLCache
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.mapping_cache import MappingCache
from trading212_mcp.market.models import (
    AnnualFinancials,
    Company,
    Fundamentals,
    MarketError,
    Price,
    PriceHistory,
    Universe,
)
from trading212_mcp.market.normalization import (
    COUNTRIES,
    is_list,
    is_mapping,
    number,
    text,
)
from trading212_mcp.market.openfigi import OpenFIGIClient, OpenFIGIResolver
from trading212_mcp.observability import timed_async

EXCHANGE_CODES = {
    "NASDAQ": ("NMS", "NGM", "NCM"),
    "NYSE": ("NYQ",),
    "NYSEAMERICAN": ("ASE",),
    "NYSEARCA": ("PCX",),
}


class YahooRateLimitError(Exception):
    pass


class YahooBackend(Protocol):
    """Small synchronous SDK boundary; tests never need to contact Yahoo."""

    def info(self, symbol: str) -> dict[str, object]: ...
    def history(
        self, symbol: str, start: date, end: date, timeout: float
    ) -> pd.DataFrame: ...
    def statements(
        self, symbol: str
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]: ...
    def search(self, isin: str, timeout: float) -> list[dict[str, object]]: ...
    def screen(
        self,
        filters: dict[str, str | float],
        regions: list[str],
        offset: int,
        size: int,
    ) -> dict[str, object]: ...


class YFinanceBackend:
    def __init__(self) -> None:
        try:
            self.sdk = importlib.import_module("yfinance")
            # yfinance >=1.8 replaces the per-call raise_errors flag with this
            # process-wide setting. Our adapter must see failures for cooldowns.
            self.sdk.config.debug.hide_exceptions = False
        except ImportError:
            raise MarketError(
                "Install project dependencies with uv sync --locked."
            ) from None

    def info(self, symbol: str) -> dict[str, object]:
        return cast(dict[str, object], self.sdk.Ticker(symbol).get_info())

    def history(
        self, symbol: str, start: date, end: date, timeout: float
    ) -> pd.DataFrame:
        return cast(
            pd.DataFrame,
            self.sdk.Ticker(symbol).history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                interval="1d",
                auto_adjust=False,
                back_adjust=False,
                repair=False,
                actions=True,
                timeout=timeout,
            ),
        )

    def statements(
        self, symbol: str
    ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        ticker = self.sdk.Ticker(symbol)
        return (
            ticker.get_income_stmt(freq="yearly"),
            ticker.get_balance_sheet(freq="yearly"),
            ticker.get_cash_flow(freq="yearly"),
        )

    def search(self, isin: str, timeout: float) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]],
            self.sdk.Search(
                isin,
                max_results=20,
                news_count=0,
                lists_count=0,
                enable_fuzzy_query=False,
                timeout=timeout,
            ).quotes,
        )

    def screen(
        self,
        filters: dict[str, str | float],
        regions: list[str],
        offset: int,
        size: int,
    ) -> dict[str, object]:
        query = self.sdk.EquityQuery
        selected_regions = (
            str(filters["regions"]).lower().split(",")
            if "regions" in filters
            else regions
        )
        clauses = [query("eq", ["region", region]) for region in selected_regions]
        geography = clauses[0] if len(clauses) == 1 else query("or", clauses)
        terms = [geography]
        if "marketCapMoreThan" in filters:
            terms.append(
                query("gte", ["intradaymarketcap", filters["marketCapMoreThan"]])
            )
        for key, field in (("marketCapLowerThan", "intradaymarketcap"),):
            if key in filters:
                terms.append(query("lte", [field, filters[key]]))
        if "minAverageVolume" in filters:
            terms.append(query("gte", ["avgdailyvol3m", filters["minAverageVolume"]]))
        for key in ("sector", "industry"):
            if key in filters:
                terms.append(query("eq", [key, filters[key]]))
        if "exchange" in filters:
            codes = EXCHANGE_CODES.get(
                str(filters["exchange"]).upper(), (str(filters["exchange"]),)
            )
            terms.append(query("is-in", ["exchange", *codes]))
        expression = terms[0] if len(terms) == 1 else query("and", terms)
        return cast(
            dict[str, object],
            self.sdk.screen(
                expression,
                offset=offset,
                size=size,
                sortField="intradaymarketcap",
                sortAsc=False,
            ),
        )


def yahoo_company(
    row: dict[str, object], isin_map: dict[str, str], require_currency: bool = True
) -> Company:
    symbol = text(row.get("symbol"))
    currency = text(row.get("currency"))
    if symbol is None or (require_currency and currency is None):
        raise MarketError("Yahoo profile is missing symbol or quote currency.")
    reported_isin = text(row.get("isin"))
    configured_isin = isin_map.get(symbol)
    if reported_isin and configured_isin and reported_isin != configured_isin:
        raise MarketError(
            "Yahoo ISIN conflicts with the explicitly configured identifier."
        )
    kind = text(row.get("quoteType"))
    return Company(
        symbol=symbol,
        name=text(row.get("longName")) or text(row.get("shortName")) or symbol,
        currency=currency or "",
        isin=reported_isin or configured_isin,
        exchange=next(
            (
                name
                for name, codes in EXCHANGE_CODES.items()
                if row.get("exchange") in codes
            ),
            text(row.get("exchange")),
        ),
        country=COUNTRIES.get(str(row.get("country")), text(row.get("country"))),
        sector=text(row.get("sector")),
        industry=text(row.get("industry")),
        market_cap=number(row.get("marketCap")),
        price=number(row.get("regularMarketPrice")) or number(row.get("currentPrice")),
        volume=number(row.get("regularMarketVolume"))
        if row.get("regularMarketVolume") is not None
        else number(row.get("volume")),
        average_volume=number(row.get("averageDailyVolume3Month"))
        if row.get("averageDailyVolume3Month") is not None
        else number(row.get("averageVolume")),
        average_volume_period="provider_three_month_average",
        is_etf=kind == "ETF",
        is_fund=kind == "MUTUALFUND",
        quote_type=kind,
        identifier_source="yahoo_profile"
        if reported_isin
        else ("operator_verified_map" if configured_isin else None),
    )


def yahoo_prices(frame: object, field: str, start: date, end: date) -> list[Price]:
    if (
        not isinstance(frame, pd.DataFrame)
        or frame.empty
        or field not in frame.columns
        or not isinstance(frame.index, pd.DatetimeIndex)
    ):
        raise MarketError(
            f"Yahoo returned no valid {field} daily series; no raw-price fallback was used."
        )
    points: dict[date, Price] = {}
    try:
        for timestamp, value in frame[field].items():
            day = cast(pd.Timestamp, timestamp).date()
            if start <= day <= end:
                point = Price(date=day, close=value)
                if day in points and points[day].close != point.close:
                    raise MarketError("Conflicting Yahoo prices for the same date.")
                points[day] = point
    except (ValidationError, TypeError):
        raise MarketError(
            "Yahoo returned invalid or non-positive daily prices."
        ) from None
    if not points:
        raise MarketError("No Yahoo prices in the requested period.")
    return [points[day] for day in sorted(points)]


def annual_value(frame: pd.DataFrame, tag: str, period: pd.Timestamp) -> float | None:
    if frame.empty or tag not in frame.index or period not in frame.columns:
        return None
    return number(frame.at[tag, period])


def yahoo_fundamentals(
    info: dict[str, object],
    frames: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame],
    retrieved_at: datetime,
) -> Fundamentals:
    income, balance, cash = frames
    periods = sorted(
        (
            pd.Timestamp(column)
            for column in income.columns
            if pd.Timestamp(column).date() <= retrieved_at.date()
        ),
        reverse=True,
    )
    currency = text(info.get("financialCurrency"))
    warnings = [
        "Yahoo valuation/margin snapshots have vendor-defined periods; they are not SEC annual metrics. No cross-provider fallback.",
        "ROIC unavailable: ROE is not used as a substitute.",
    ]
    current = periods[0] if periods else None

    def value(frame: pd.DataFrame, tag: str) -> float | None:
        return (
            annual_value(frame, tag, current)
            if current is not None and currency
            else None
        )

    def growth(tag: str) -> float | None:
        if (
            len(periods) < 2
            or not currency
            or not 300 <= (periods[0] - periods[1]).days <= 430
        ):
            return None
        latest, previous = (
            annual_value(income, tag, periods[0]),
            annual_value(income, tag, periods[1]),
        )
        return (
            latest / previous - 1
            if latest is not None and previous is not None and previous > 0
            else None
        )

    def positive(key: str) -> float | None:
        val = number(info.get(key))
        return val if val is not None and val > 0 else None

    if not currency:
        warnings.append(
            "Missing financialCurrency; annual statement metrics withheld rather than assuming quote currency."
        )
    debt = number(info.get("debtToEquity"))
    return Fundamentals(
        source="yfinance",
        retrieved_at=retrieved_at,
        statement_date=current.date() if current is not None else None,
        statement_currency=currency,
        revenue=value(income, "TotalRevenue"),
        net_income=value(income, "NetIncome"),
        total_assets=value(balance, "TotalAssets"),
        revenue_growth=growth("TotalRevenue"),
        earnings_growth=growth("NetIncome"),
        pe=positive("trailingPE"),
        forward_pe=positive("forwardPE"),
        price_to_book=positive("priceToBook"),
        price_to_sales=positive("priceToSalesTrailing12Months"),
        net_margin=number(info.get("profitMargins")),
        operating_margin=number(info.get("operatingMargins")),
        ev_to_ebitda=positive("enterpriseToEbitda"),
        annual_history=[
            AnnualFinancials(
                end=period.date(),
                currency=currency,
                revenue=annual_value(income, "TotalRevenue", period),
                net_income=annual_value(income, "NetIncome", period),
                operating_income=annual_value(income, "OperatingIncome", period),
                operating_cash_flow=annual_value(cash, "OperatingCashFlow", period),
                free_cash_flow=annual_value(cash, "FreeCashFlow", period),
                total_assets=annual_value(balance, "TotalAssets", period),
                equity=annual_value(balance, "StockholdersEquity", period),
                total_debt=annual_value(balance, "TotalDebt", period),
            )
            for period in periods[:4]
            if currency and period.date() <= retrieved_at.date()
        ],
        roic=None,
        debt_to_equity=debt / 100 if debt is not None and debt >= 0 else None,
        total_debt=value(balance, "TotalDebt"),
        cash=value(balance, "CashAndCashEquivalents"),
        operating_cash_flow=value(cash, "OperatingCashFlow"),
        free_cash_flow=value(cash, "FreeCashFlow"),
        provenance={
            "annual_statements": f"Yahoo yearly statements, exact income fiscal end shared with balance/cash when present; currency={currency}; date={current}",
            "valuation_margins": "Yahoo info snapshot: trailingPE, forwardPE, priceToBook, priceToSalesTrailing12Months, profitMargins, operatingMargins; no annual/TTM substitution.",
            "debt_to_equity": "Yahoo info.debtToEquity percentage divided by 100; vendor snapshot, not annual SEC debt/equity.",
        },
        warnings=warnings,
    )


class YFinanceProvider:
    name = "yfinance"

    def __init__(
        self,
        settings: MarketSettings,
        backend: YahooBackend | None = None,
        identifier_client: OpenFIGIClient | None = None,
    ) -> None:
        if not settings.yahoo_regions or len(settings.yahoo_regions) > 10:
            raise MarketError(
                "Configure one to ten Yahoo region codes for an explicit discovery universe."
            )
        self.settings = settings
        self._backend = backend or YFinanceBackend()
        self._cache = TTLCache(
            settings.cache_ttl_seconds,
            settings.cache_max_entries,
            settings.cache_path,
            "yfinance",
        )
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(settings.fetch_concurrency)
        self._key_locks = [asyncio.Lock() for _ in range(64)]
        self._executor = ThreadPoolExecutor(
            max_workers=settings.fetch_concurrency, thread_name_prefix="yahoo-data"
        )
        self._pending: set[asyncio.Future[object]] = set()
        self._timed_out: set[asyncio.Future[object]] = set()
        self._next_request = 0.0
        self._closed = False
        self._identifiers = (
            OpenFIGIResolver(
                identifier_client or OpenFIGIClient(settings),
                MappingCache(
                    settings.resolution_cache_path, settings.resolution_cache_ttl_days
                ),
            )
            if settings.openfigi_enabled
            else None
        )

    @timed_async("market_data")
    async def _call(self, key: str, operation: Callable[[], object]) -> CachedResponse:
        # Fixed lock stripes bound memory and coalesce simultaneous cache misses.
        async with self._key_locks[hash(key) % len(self._key_locks)]:
            if self._closed:
                raise MarketError("Yahoo adapter is closed.")
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            for attempt in range(self.settings.retries + 1):
                await self._slots.acquire()
                try:
                    async with self._lock:
                        if self._closed:
                            raise MarketError("Yahoo adapter is closed.")
                        if self._timed_out:
                            raise MarketError(
                                "A timed-out Yahoo SDK request is still finishing; retry later."
                            )
                        delay = self._next_request - time.monotonic()
                        if delay > 10:
                            raise MarketError("Yahoo cooldown is active; retry later.")
                        if delay > 0:
                            await asyncio.sleep(delay)
                        if self._next_request - time.monotonic() > 10:
                            raise MarketError("Yahoo cooldown is active; retry later.")
                        self._next_request = time.monotonic() + 60 / min(
                            30, self.settings.requests_per_minute
                        )
                        pending = asyncio.get_running_loop().run_in_executor(
                            self._executor, operation
                        )
                except BaseException:
                    self._slots.release()
                    raise
                self._pending.add(pending)

                def finished(future: asyncio.Future[object]) -> None:
                    self._pending.discard(future)
                    self._timed_out.discard(future)
                    self._slots.release()
                    if not future.cancelled():
                        future.exception()

                # A cancelled waiter cannot release a still-running SDK worker slot.
                pending.add_done_callback(finished)
                try:
                    async with asyncio.timeout(self.settings.timeout_seconds):
                        value = await asyncio.shield(pending)
                except TimeoutError:
                    if not pending.done():
                        self._timed_out.add(pending)
                    raise MarketError(
                        "Yahoo SDK request timed out; no replacement prices were used."
                    ) from None
                except Exception as exc:
                    if (
                        isinstance(exc, YahooRateLimitError)
                        or type(exc).__name__ == "YFRateLimitError"
                    ):
                        self._next_request = time.monotonic() + 60
                        raise MarketError(
                            "Yahoo rate limit reached; no proxy/cookie evasion attempted."
                        ) from None
                    if (
                        isinstance(exc, (ConnectionError, OSError))
                        and attempt < self.settings.retries
                    ):
                        self._next_request = max(
                            self._next_request, time.monotonic() + 2**attempt
                        )
                        continue
                    raise MarketError(
                        "Yahoo request failed or returned unsupported data."
                    ) from None
                result = CachedResponse(
                    rows=[{"value": value}], retrieved_at=datetime.now(timezone.utc)
                )
                self._cache.put(key, result)
                return result
        raise MarketError("Yahoo request failed.")

    async def profile(self, symbol: str) -> Company:
        response = await self._call(
            "profile:" + symbol, lambda: self._backend.info(symbol)
        )
        info = response.rows[0]["value"]
        if not is_mapping(info):
            raise MarketError("Yahoo returned an invalid profile.")
        company = yahoo_company(info, self.settings.yahoo_isin_map)
        if company.symbol != symbol:
            raise MarketError(
                "Yahoo returned a different listing symbol; explicit resolution is required."
            )
        return company

    async def search_isin(
        self, isin: str, currency: str | None = None
    ) -> list[Company]:
        mapped = [
            symbol
            for symbol, configured in self.settings.yahoo_isin_map.items()
            if configured == isin
        ]
        if mapped:
            return [await self.profile(symbol) for symbol in mapped]
        identifier_error: MarketError | None = None
        if self._identifiers is not None:
            try:
                matches = await self._identifiers.resolve(isin, currency, self.profile)
                if matches:
                    return matches
            except MarketError as exc:
                identifier_error = exc
        response = await self._call(
            "isin:" + isin,
            lambda: self._backend.search(isin, self.settings.timeout_seconds),
        )
        rows = response.rows[0]["value"]
        if not is_list(rows):
            raise MarketError("Yahoo returned invalid identifier search results.")
        matches: list[Company] = []
        for row in rows[:20]:
            if (
                not is_mapping(row)
                or row.get("isin") != isin
                or not text(row.get("symbol"))
            ):
                continue
            company = await self.profile(str(row["symbol"]))
            if company.isin == isin:
                matches.append(company)
        if not matches and self._identifiers is not None:
            suggestions = [
                str(row["symbol"])
                for row in rows
                if is_mapping(row) and text(row.get("symbol"))
            ]
            if suggestions:
                matches = await self._identifiers.resolve(
                    isin, currency, self.profile, suggestions
                )
        if not matches and identifier_error is not None:
            raise identifier_error
        return matches

    async def history(self, company: Company, start: date, end: date) -> PriceHistory:
        response = await self._call(
            f"history:{company.symbol}:{start}:{end}",
            lambda: self._backend.history(
                company.symbol, start, end, self.settings.timeout_seconds
            ),
        )
        frame = response.rows[0]["value"]
        if not isinstance(frame, pd.DataFrame):
            raise MarketError("Yahoo returned an invalid price response.")
        return PriceHistory(
            symbol=company.symbol,
            currency=company.currency,
            source=self.name,
            retrieved_at=response.retrieved_at,
            price_basis="split_and_dividend_adjusted",
            prices=yahoo_prices(frame, "Adj Close", start, end),
            warnings=[
                "Yahoo adjusted close only; corporate-action quality is vendor-dependent. No raw-price fallback or repair."
            ],
        )

    async def fx(
        self, source: str, target: str, start: date, end: date
    ) -> PriceHistory:
        symbol = source + target + "=X"
        response = await self._call(
            f"fx:{symbol}:{start}:{end}",
            lambda: self._backend.history(
                symbol, start, end, self.settings.timeout_seconds
            ),
        )
        frame = response.rows[0]["value"]
        if not isinstance(frame, pd.DataFrame):
            raise MarketError("Yahoo returned an invalid FX response.")
        return PriceHistory(
            symbol=symbol,
            currency=target,
            source=self.name,
            retrieved_at=response.retrieved_at,
            price_basis="fx_spot",
            prices=yahoo_prices(frame, "Close", start, end),
        )

    async def fundamentals(self, company: Company) -> Fundamentals:
        info_response = await self._call(
            "profile:" + company.symbol, lambda: self._backend.info(company.symbol)
        )
        info = info_response.rows[0]["value"]
        if not is_mapping(info):
            raise MarketError("Yahoo returned invalid fundamentals metadata.")
        warnings: list[str] = []
        try:
            response = await self._call(
                "statements:" + company.symbol,
                lambda: self._backend.statements(company.symbol),
            )
            raw_frames = response.rows[0]["value"]
            items = (
                cast(tuple[object, ...], raw_frames)
                if isinstance(raw_frames, tuple)
                else ()
            )
            if len(items) != 3 or any(
                not isinstance(frame, pd.DataFrame) for frame in items
            ):
                raise MarketError("Yahoo returned invalid annual statements.")
            frames = cast(tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame], items)
            timestamp = min(info_response.retrieved_at, response.retrieved_at)
        except MarketError as exc:
            frames = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
            timestamp = info_response.retrieved_at
            warnings.append(str(exc))
        result = yahoo_fundamentals(info, frames, timestamp)
        result.warnings.extend(warnings)
        return result

    async def universe(
        self, filters: dict[str, str | float], max_candidates: int
    ) -> Universe:
        effective_regions = (
            str(filters["regions"]).lower().split(",")
            if "regions" in filters
            else self.settings.yahoo_regions
        )
        companies: dict[str, Company] = {}
        pages = 0
        total: int | None = None
        timestamp = datetime.now(timezone.utc)
        page_size = min(100, max_candidates)
        for offset in range(0, max_candidates, page_size):
            size = min(page_size, max_candidates - offset)
            key = json.dumps(
                ["screen", filters, self.settings.yahoo_regions, offset, size],
                sort_keys=True,
            )
            response = await self._call(
                key,
                lambda offset=offset, size=size: self._backend.screen(
                    filters, self.settings.yahoo_regions, offset, size
                ),
            )
            payload = response.rows[0]["value"]
            if not is_mapping(payload) or not is_list(payload.get("quotes")):
                raise MarketError(
                    "Yahoo screener returned an unsupported response; no sample universe substituted."
                )
            reported_total = number(payload.get("total"))
            if (
                reported_total is None
                or reported_total < 0
                or not reported_total.is_integer()
            ):
                raise MarketError("Yahoo screener omitted a valid coverage count.")
            total = int(reported_total)
            rows = payload["quotes"]
            if not is_list(rows):
                raise MarketError("Yahoo screener returned invalid quotes.")
            pages += 1
            timestamp = min(timestamp, response.retrieved_at)
            before = len(companies)
            for row in rows:
                if not is_mapping(row) or row.get("quoteType") != "EQUITY":
                    continue
                company = yahoo_company(
                    row, self.settings.yahoo_isin_map, require_currency=False
                )
                companies[company.symbol] = company
            if not rows or len(companies) == before or offset + len(rows) >= total:
                break
        return Universe(
            source=self.name,
            retrieved_at=timestamp,
            companies=list(companies.values())[:max_candidates],
            pages_fetched=pages,
            truncated=total is None or total > min(len(companies), max_candidates),
            reported_total=total,
            description=f"Yahoo custom EquityQuery screener, regions={effective_regions}, provider-reported matches={total}; evaluated cap={max_candidates}. Active/delisted completeness is not guaranteed. ISIN gaps can prevent ownership exclusion. Not a global security master.",
        )

    async def aclose(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self._identifiers is not None:
            await self._identifiers.client.aclose()
