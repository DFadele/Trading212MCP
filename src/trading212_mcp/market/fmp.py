import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import cast
from urllib.parse import quote, quote_plus

import httpx
from pydantic import ValidationError

from trading212_mcp.market.cache import CachedResponse, TTLCache
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.models import (
    AnnualFinancials,
    Company,
    Fundamentals,
    MarketError,
    Price,
    PriceHistory,
    Universe,
)
from trading212_mcp.market.normalization import is_list, is_mapping, number, text
from trading212_mcp.observability import timed_async


def company_from_row(row: dict[str, object]) -> Company:
    symbol = text(row.get("symbol"))
    if symbol is None:
        raise MarketError("FMP returned a company without a symbol.")
    return Company(
        symbol=symbol,
        name=text(row.get("companyName")) or text(row.get("name")) or symbol,
        currency=text(row.get("currency")) or "",
        isin=text(row.get("isin")),
        identifier_source="fmp_profile" if text(row.get("isin")) else None,
        exchange=text(row.get("exchangeShortName")) or text(row.get("exchange")),
        country=text(row.get("country")),
        sector=text(row.get("sector")),
        industry=text(row.get("industry")),
        market_cap=number(row.get("marketCap")),
        price=number(row.get("price")),
        volume=number(row.get("volume")),
        average_volume=number(row.get("averageVolume")),
        average_volume_period="provider_defined_average",
        is_etf=row.get("isEtf") is True,
        is_fund=row.get("isFund") is True,
    )


def normalise_prices(
    rows: list[dict[str, object]], field: str, start: date, end: date
) -> list[Price]:
    by_date: dict[date, Price] = {}
    try:
        for row in rows:
            price = Price.model_validate(
                {"date": row.get("date"), "close": row.get(field)}
            )
            if start <= price.date <= end:
                if price.date in by_date and by_date[price.date].close != price.close:
                    raise MarketError("Conflicting prices for the same date.")
                by_date[price.date] = price
    except ValidationError:
        raise MarketError(
            "FMP returned invalid or non-positive historical prices."
        ) from None
    if not by_date:
        raise MarketError("No price history available for the requested period.")
    return [by_date[d] for d in sorted(by_date)]


class _SecretFilter(logging.Filter):
    def __init__(self, secret: str) -> None:
        super().__init__()
        self.values = (secret, quote(secret, safe=""), quote_plus(secret, safe=""))

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.values:
            if secret:
                message = message.replace(secret, "[REDACTED]")
        record.msg, record.args = message, ()
        return True


class FMPProvider:
    name = "financial_modeling_prep"

    def __init__(
        self,
        settings: MarketSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            settings.fmp_api_key is None
            or not settings.fmp_api_key.get_secret_value().strip()
        ):
            raise MarketError(
                "Set MARKET_FMP_API_KEY to enable market data and stock screening."
            )
        self.settings = settings
        self._key = settings.fmp_api_key.get_secret_value()
        self._http = httpx.AsyncClient(
            base_url="https://financialmodelingprep.com/stable/",
            timeout=settings.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self._cache = TTLCache(
            settings.cache_ttl_seconds,
            settings.cache_max_entries,
            settings.cache_path,
            "fmp",
        )
        # Serialisation also prevents concurrent cache misses exhausting a small plan.
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        self._redact = _SecretFilter(self._key)
        self._loggers = [
            logging.getLogger("httpx"),
            logging.getLogger("httpcore.http11"),
            logging.getLogger("httpcore.http2"),
        ]
        for logger in self._loggers:
            logger.addFilter(self._redact)

    async def aclose(self) -> None:
        await self._http.aclose()
        for logger in self._loggers:
            logger.removeFilter(self._redact)

    @timed_async("market_data")
    async def _request(
        self, endpoint: str, params: dict[str, str | int | float]
    ) -> CachedResponse:
        cache_key = json.dumps([endpoint, params], sort_keys=True)
        async with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
            for attempt in range(self.settings.retries + 1):
                delay = self._next_request - time.monotonic()
                if delay > 10:
                    raise MarketError("FMP rate-limit cooldown is active; retry later.")
                if delay > 0:
                    await asyncio.sleep(delay)
                self._next_request = (
                    time.monotonic() + 60 / self.settings.requests_per_minute
                )
                try:
                    async with asyncio.timeout(self.settings.timeout_seconds):
                        response = await self._http.get(
                            endpoint, params={**params, "apikey": self._key}
                        )
                except (httpx.RequestError, TimeoutError):
                    if attempt < self.settings.retries:
                        self._next_request = max(
                            self._next_request, time.monotonic() + 2**attempt
                        )
                        continue
                    raise MarketError("FMP connection failed or timed out.") from None
                if response.status_code in (401, 402, 403):
                    raise MarketError(
                        "FMP denied access: check API key and endpoint/exchange entitlements."
                    )
                if response.status_code == 429 or response.status_code >= 500:
                    wait = float(2**attempt)
                    retry = response.headers.get("Retry-After")
                    if retry:
                        try:
                            wait = max(0, float(retry))
                        except ValueError:
                            try:
                                wait = max(
                                    0,
                                    (
                                        parsedate_to_datetime(retry)
                                        - datetime.now(timezone.utc)
                                    ).total_seconds(),
                                )
                            except (ValueError, TypeError, OverflowError):
                                pass
                    if not math.isfinite(wait):
                        wait = 60
                    self._next_request = max(
                        self._next_request, time.monotonic() + wait
                    )
                    if attempt < self.settings.retries and wait <= 10:
                        continue
                    raise MarketError(
                        "FMP rate limit or temporary outage; retry later."
                    )
                if response.status_code != 200:
                    raise MarketError(
                        f"FMP request failed (HTTP {response.status_code})."
                    )
                try:
                    rows = response.json()
                except ValueError:
                    raise MarketError("FMP returned invalid JSON.") from None
                if not is_list(rows) or any(not is_mapping(row) for row in rows):
                    raise MarketError(
                        "FMP returned an error or an unsupported response shape."
                    )
                result = CachedResponse(
                    rows=cast(list[dict[str, object]], rows),
                    retrieved_at=datetime.now(timezone.utc),
                )
                self._cache.put(cache_key, result)
                return result
        raise MarketError("FMP request failed.")

    async def profile(self, symbol: str) -> Company:
        response = await self._request("profile", {"symbol": symbol})
        matches = [
            company_from_row(row)
            for row in response.rows
            if row.get("symbol") == symbol
        ]
        if len(matches) != 1 or not matches[0].currency:
            raise MarketError(f"No unique company profile with currency for {symbol}.")
        return matches[0]

    async def search_isin(
        self, isin: str, currency: str | None = None
    ) -> list[Company]:
        response = await self._request("search-isin", {"isin": isin})
        companies: list[Company] = []
        symbols = sorted(
            {str(row["symbol"]) for row in response.rows if text(row.get("symbol"))}
        )
        if len(symbols) > 20:
            raise MarketError(
                "ISIN lookup returned too many listings; configure a verified symbol override."
            )
        for symbol in symbols:
            profile = await self.profile(symbol)
            if profile.isin == isin:
                companies.append(profile)
        return companies

    async def history(self, company: Company, start: date, end: date) -> PriceHistory:
        response = await self._request(
            "historical-price-eod/dividend-adjusted",
            {
                "symbol": company.symbol,
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )
        return PriceHistory(
            symbol=company.symbol,
            currency=company.currency,
            source=self.name,
            retrieved_at=response.retrieved_at,
            price_basis="split_and_dividend_adjusted",
            prices=normalise_prices(response.rows, "adjClose", start, end),
        )

    async def fx(
        self, source: str, target: str, start: date, end: date
    ) -> PriceHistory:
        response = await self._request(
            "historical-price-eod/full",
            {
                "symbol": source + target,
                "from": start.isoformat(),
                "to": end.isoformat(),
            },
        )
        return PriceHistory(
            symbol=source + target,
            currency=target,
            source=self.name,
            retrieved_at=response.retrieved_at,
            price_basis="fx_spot",
            prices=normalise_prices(response.rows, "close", start, end),
        )

    async def fundamentals(self, company: Company) -> Fundamentals:
        endpoints = (
            "ratios-ttm",
            "key-metrics-ttm",
            "income-statement",
            "balance-sheet-statement",
            "cash-flow-statement",
            "analyst-estimates",
        )
        rows: dict[str, list[dict[str, object]]] = {}
        timestamps: list[datetime] = []
        warnings: list[str] = []
        for endpoint in endpoints:
            params: dict[str, str | int | float] = {"symbol": company.symbol}
            if endpoint not in ("ratios-ttm", "key-metrics-ttm"):
                params.update({"period": "annual", "limit": 4})
            if endpoint == "analyst-estimates":
                params.update({"limit": 10, "page": 0})
            try:
                result = await self._request(endpoint, params)
                rows[endpoint] = sorted(
                    result.rows, key=lambda row: str(row.get("date", "")), reverse=True
                )
                timestamps.append(result.retrieved_at)
                if not result.rows:
                    warnings.append(f"{endpoint}: no data")
            except MarketError as exc:
                warnings.append(f"{endpoint}: {exc}")
                rows[endpoint] = []

        for endpoint in (
            "income-statement",
            "balance-sheet-statement",
            "cash-flow-statement",
        ):
            available: list[dict[str, object]] = []
            for record in rows[endpoint]:
                try:
                    end = date.fromisoformat(str(record["date"]))
                    filing = text(record.get("filingDate")) or text(
                        record.get("fillingDate")
                    )
                    if end > datetime.now(timezone.utc).date() or (
                        filing
                        and date.fromisoformat(filing[:10])
                        > datetime.now(timezone.utc).date()
                    ):
                        continue
                    available.append(record)
                except (ValueError, KeyError):
                    warnings.append(
                        f"{endpoint}: invalid statement availability date; record excluded."
                    )
            rows[endpoint] = available

        def first(endpoint: str) -> dict[str, object]:
            return rows[endpoint][0] if rows[endpoint] else {}

        ratios, metrics = first("ratios-ttm"), first("key-metrics-ttm")
        income, balance, cash = (
            first("income-statement"),
            first("balance-sheet-statement"),
            first("cash-flow-statement"),
        )

        for endpoint in ("balance-sheet-statement", "cash-flow-statement"):
            matching = next(
                (
                    row
                    for row in rows[endpoint]
                    if row.get("date") == income.get("date")
                    and row.get("reportedCurrency") == income.get("reportedCurrency")
                ),
                cast(dict[str, object], {}),
            )
            if rows[endpoint] and not matching:
                warnings.append(
                    f"{endpoint}: no matching income fiscal end/currency; annual amounts withheld."
                )
            if endpoint == "balance-sheet-statement":
                balance = matching
            else:
                cash = matching

        def growth(field: str) -> float | None:
            statements = rows["income-statement"]
            if len(statements) < 2:
                return None
            current, prior = (
                number(statements[0].get(field)),
                number(statements[1].get(field)),
            )
            # A loss turning into a profit is not a meaningful percentage growth rate.
            if current is None or prior is None or prior <= 0:
                return None
            if statements[0].get("reportedCurrency") != statements[1].get(
                "reportedCurrency"
            ):
                return None
            try:
                days = (
                    date.fromisoformat(str(statements[0]["date"]))
                    - date.fromisoformat(str(statements[1]["date"]))
                ).days
            except (KeyError, ValueError):
                return None
            if not 300 <= days <= 430:
                return None
            return current / prior - 1

        estimates = sorted(
            (
                row
                for row in rows["analyst-estimates"]
                if datetime.now(timezone.utc).date().isoformat()
                < str(row.get("date", ""))
                <= (datetime.now(timezone.utc).date() + timedelta(days=550)).isoformat()
            ),
            key=lambda row: str(row.get("date", "")),
        )
        forward_eps = number(estimates[0].get("epsAvg")) if estimates else None
        forward_pe = (
            company.price / forward_eps
            if company.price is not None
            and forward_eps is not None
            and company.price > 0
            and forward_eps > 0
            and company.currency == income.get("reportedCurrency")
            else None
        )
        fields = dict(
            revenue=number(income.get("revenue")),
            net_income=number(income.get("netIncome")),
            total_assets=number(balance.get("totalAssets")),
            revenue_growth=growth("revenue"),
            earnings_growth=growth("netIncome"),
            pe=number(ratios.get("priceToEarningsRatioTTM")),
            forward_pe=forward_pe,
            price_to_book=number(ratios.get("priceToBookRatioTTM")),
            price_to_sales=number(ratios.get("priceToSalesRatioTTM")),
            net_margin=number(ratios.get("netProfitMarginTTM")),
            operating_margin=number(ratios.get("operatingProfitMarginTTM")),
            roic=number(metrics.get("returnOnInvestedCapitalTTM")),
            debt_to_equity=number(ratios.get("debtToEquityRatioTTM")),
            total_debt=number(balance.get("totalDebt")),
            cash=number(balance.get("cashAndCashEquivalents")),
            operating_cash_flow=number(cash.get("operatingCashFlow")),
            free_cash_flow=number(cash.get("freeCashFlow")),
        )
        for key in ("pe", "forward_pe", "price_to_book", "price_to_sales"):
            value = fields[key]
            if value is not None and value <= 0:
                fields[key] = None
        statement_date = None
        try:
            statement_date = date.fromisoformat(str(income["date"])) if income else None
        except (ValueError, KeyError):
            warnings.append("Invalid income statement date")
        currencies = {
            text(row.get("reportedCurrency")) for row in (income, balance, cash) if row
        }
        if len(currencies) > 1:
            warnings.append(
                "Financial statement currencies differ; amounts retain their individual provenance."
            )
        annual_history: list[AnnualFinancials] = []
        for statement in rows["income-statement"]:
            try:
                end = date.fromisoformat(str(statement["date"]))
                currency = text(statement.get("reportedCurrency"))
                if not currency or end > datetime.now(timezone.utc).date():
                    continue
                aligned = {
                    endpoint: next(
                        (
                            r
                            for r in rows[endpoint]
                            if r.get("date") == end.isoformat()
                            and r.get("reportedCurrency") == currency
                        ),
                        cast(dict[str, object], {}),
                    )
                    for endpoint in ("balance-sheet-statement", "cash-flow-statement")
                }
                balance_row, cash_row = (
                    aligned["balance-sheet-statement"],
                    aligned["cash-flow-statement"],
                )
                filing = text(statement.get("filingDate")) or text(
                    statement.get("fillingDate")
                )
                filed = date.fromisoformat(filing[:10]) if filing else None
                annual_history.append(
                    AnnualFinancials(
                        end=end,
                        filed=filed,
                        currency=currency,
                        revenue=number(statement.get("revenue")),
                        net_income=number(statement.get("netIncome")),
                        operating_income=number(statement.get("operatingIncome")),
                        total_assets=number(balance_row.get("totalAssets")),
                        equity=number(balance_row.get("totalStockholdersEquity")),
                        total_debt=number(balance_row.get("totalDebt")),
                        operating_cash_flow=number(cash_row.get("operatingCashFlow")),
                        free_cash_flow=number(cash_row.get("freeCashFlow")),
                    )
                )
            except (ValueError, KeyError):
                warnings.append(
                    "Invalid annual-history date; derived factors withheld for that record."
                )
        return Fundamentals(
            source=self.name,
            retrieved_at=min(timestamps) if timestamps else datetime.now(timezone.utc),
            statement_date=statement_date,
            statement_currency=text(income.get("reportedCurrency")),
            annual_history=annual_history,
            ev_to_ebitda=number(ratios.get("enterpriseValueMultipleTTM")),
            provenance={
                endpoint: f"{endpoint}; date={first(endpoint).get('date', 'TTM snapshot')}; currency={first(endpoint).get('reportedCurrency', 'ratios')}"
                for endpoint in endpoints
            },
            warnings=warnings,
            **fields,
        )

    async def universe(
        self, filters: dict[str, str | float], max_candidates: int
    ) -> Universe:
        companies: dict[str, Company] = {}
        pages = 0
        timestamps: list[datetime] = []
        truncated = True
        page_size = min(100, max_candidates)
        for page in range(10):
            response = await self._request(
                "company-screener",
                {
                    **filters,
                    "isEtf": "false",
                    "isFund": "false",
                    "isActivelyTrading": "true",
                    "limit": page_size,
                    "page": page,
                },
            )
            pages += 1
            timestamps.append(response.retrieved_at)
            before = len(companies)
            for row in response.rows:
                company = company_from_row(row)
                companies[company.symbol] = company
            if len(response.rows) < page_size:
                truncated = False
                break
            if len(companies) >= max_candidates or len(companies) == before:
                break
        return Universe(
            source=self.name,
            retrieved_at=min(timestamps),
            companies=list(companies.values())[:max_candidates],
            truncated=truncated,
            pages_fetched=pages,
            description="FMP active company-screener results, limited by subscription, filters, provider ordering and configured candidate cap; not the entire market.",
        )
