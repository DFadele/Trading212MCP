"""OpenFIGI identity checks. Yahoo symbols are proposals until FIGIs agree."""

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from trading212_mcp.market.cache import CachedResponse, TTLCache
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.mapping_cache import MappingCache
from trading212_mcp.market.models import Company, MarketError
from trading212_mcp.market.normalization import is_list, is_mapping, number
from trading212_mcp.market.resolution import currency_unit


class Listing(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    figi: str
    ticker: str
    exchCode: str
    compositeFIGI: str | None = None
    marketSector: str
    securityType: str
    securityType2: str | None = None

    def identifiers(self) -> set[str]:
        return {
            value
            for value in (self.figi, self.compositeFIGI)
            if value and re.fullmatch(r"[A-Z0-9]{12}", value)
        }


# Candidate hints only. Reverse FIGI verification, not these suffixes, proves identity.
FIGI_SUFFIXES = {
    "US": "",
    "UN": "",
    "UW": "",
    "UQ": "",
    "UP": "",
    "UA": "",
    "LN": ".L",
    "GY": ".DE",
    "GT": ".DE",
    "GF": ".F",
    "FP": ".PA",
    "NA": ".AS",
    "SW": ".SW",
    "SE": ".SW",
    "IM": ".MI",
    "SM": ".MC",
    "CT": ".TO",
    "CN": ".TO",
    "CV": ".V",
    "AN": ".AX",
    "AU": ".AX",
    "HK": ".HK",
    "JT": ".T",
    "JP": ".T",
    "SS": ".ST",
    "DC": ".CO",
    "FH": ".HE",
    "NO": ".OL",
    "BB": ".BR",
}
# Actual Yahoo profile exchange -> MIC and Yahoo suffix. Unknown exchanges fail closed.
YAHOO_VENUES = {
    "NASDAQ": ("XNGS|XNMS|XNCM", ""),
    "NMS": ("XNGS", ""),
    "NGM": ("XNMS", ""),
    "NCM": ("XNCM", ""),
    "NYSE": ("XNYS", ""),
    "NYQ": ("XNYS", ""),
    "NYSEAMERICAN": ("XASE", ""),
    "ASE": ("XASE", ""),
    "NYSEARCA": ("ARCX", ""),
    "PCX": ("ARCX", ""),
    "BATS": ("BATS", ""),
    "LSE": ("XLON", ".L"),
    "GER": ("XETR", ".DE"),
    "FRA": ("XFRA", ".F"),
    "PAR": ("XPAR", ".PA"),
    "AMS": ("XAMS", ".AS"),
    "EBS": ("XSWX", ".SW"),
    "SWX": ("XSWX", ".SW"),
    "MIL": ("XMIL", ".MI"),
    "MCE": ("XMAD", ".MC"),
    "TOR": ("XTSE", ".TO"),
    "VAN": ("XTSX", ".V"),
    "ASX": ("XASX", ".AX"),
    "HKG": ("XHKG", ".HK"),
    "JPX": ("XTKS", ".T"),
    "TYO": ("XTKS", ".T"),
    "STO": ("XSTO", ".ST"),
    "CPH": ("XCSE", ".CO"),
    "HEL": ("XHEL", ".HE"),
    "OSL": ("XOSL", ".OL"),
    "BRU": ("XBRU", ".BR"),
}
STOCK_TYPES = {
    "Common Stock",
    "Depositary Receipt",
    "ADR",
    "GDR",
    "Preference",
    "Preferred Stock",
    "REIT",
    "Closed-End Fund",
}


def compatible(company: Company, listing: Listing) -> bool:
    if listing.marketSector != "Equity" or not listing.identifiers():
        return False
    if listing.securityType in {"ETP", "ETF", "Exchange Traded Fund"}:
        return company.quote_type == "ETF" and company.is_etf
    return (
        company.quote_type == "EQUITY"
        and not company.is_etf
        and not company.is_fund
        and (
            listing.securityType in STOCK_TYPES or listing.securityType2 in STOCK_TYPES
        )
    )


class OpenFIGIClient:
    def __init__(
        self,
        settings: MarketSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        headers = {"Accept": "application/json"}
        if settings.openfigi_api_key:
            headers["X-OPENFIGI-APIKEY"] = settings.openfigi_api_key.get_secret_value()
        self._http = httpx.AsyncClient(
            base_url="https://api.openfigi.com/v3/",
            headers=headers,
            timeout=min(10, settings.timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )
        self._lock = asyncio.Lock()
        self._next_request = 0.0
        self._cache = TTLCache(settings.cache_ttl_seconds, settings.cache_max_entries)

    async def map(self, jobs: list[dict[str, str]]) -> list[list[Listing]]:
        if not jobs:
            return []
        if len(jobs) > 10:
            raise MarketError("OpenFIGI lookup exceeds the supported ten-job batch.")
        key = json.dumps(jobs, sort_keys=True)
        async with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                for attempt in range(self.settings.retries + 1):
                    delay = self._next_request - time.monotonic()
                    if delay > 10:
                        raise MarketError(
                            "OpenFIGI rate-limit cooldown is active; retry later."
                        )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    # Stay below the documented 25/min unauthenticated allowance.
                    self._next_request = time.monotonic() + 3
                    try:
                        async with asyncio.timeout(
                            min(10, self.settings.timeout_seconds)
                        ):
                            response = await self._http.post("mapping", json=jobs)
                    except (httpx.RequestError, TimeoutError):
                        if attempt < self.settings.retries:
                            self._next_request = max(
                                self._next_request, time.monotonic() + 2**attempt
                            )
                            continue
                        raise MarketError(
                            "OpenFIGI lookup failed or timed out."
                        ) from None
                    if response.status_code == 429:
                        wait = number(response.headers.get("Retry-After")) or 60
                        reset = number(response.headers.get("ratelimit-reset")) or 0
                        self._next_request = time.monotonic() + max(3, wait, reset)
                        raise MarketError(
                            "OpenFIGI rate limit reached; retry after the server cooldown."
                        )
                    if response.status_code >= 500 and attempt < self.settings.retries:
                        self._next_request = max(
                            self._next_request, time.monotonic() + 2**attempt
                        )
                        continue
                    if response.status_code != 200:
                        raise MarketError(
                            f"OpenFIGI lookup failed (HTTP {response.status_code})."
                        )
                    try:
                        payload = response.json()
                    except ValueError:
                        raise MarketError("OpenFIGI returned invalid JSON.") from None
                    if (
                        not is_list(payload)
                        or len(payload) != len(jobs)
                        or any(not is_mapping(row) for row in payload)
                    ):
                        raise MarketError(
                            "OpenFIGI returned an invalid job/result association."
                        )
                    cached = CachedResponse(
                        cast(list[dict[str, object]], payload),
                        datetime.now(timezone.utc),
                    )
                    self._cache.put(key, cached)
                    break
            if cached is None:
                raise MarketError("OpenFIGI lookup failed.")
            results: list[list[Listing]] = []
            for row in cached.rows:
                if "error" in row:
                    raise MarketError(
                        "OpenFIGI could not complete an identifier mapping job."
                    )
                if "warning" in row and "data" not in row:
                    results.append([])
                    continue
                rows = row.get("data")
                if not is_list(rows) or len(rows) > 500:
                    raise MarketError(
                        "OpenFIGI returned an unsupported or overly broad listing set."
                    )
                try:
                    results.append([Listing.model_validate(item) for item in rows])
                except ValidationError:
                    raise MarketError(
                        "OpenFIGI returned invalid listing metadata."
                    ) from None
            return results

    async def aclose(self) -> None:
        await self._http.aclose()


class OpenFIGIResolver:
    def __init__(self, client: OpenFIGIClient, cache: MappingCache) -> None:
        self.client = client
        self.cache = cache

    async def resolve(
        self,
        isin: str,
        currency: str | None,
        profile: Callable[[str], Awaitable[Company]],
        suggestions: list[str] | None = None,
    ) -> list[Company]:
        target = currency_unit(currency)[0] if currency else None
        key = f"verified-v1:{isin}:{target or 'all'}"
        cached = self.cache.get(key)
        entries = cached.get("listings") if cached else None
        if is_list(entries) and entries:
            verified: list[Company] = []
            valid = True
            for raw in entries:
                if not is_mapping(raw) or not isinstance(raw.get("symbol"), str):
                    valid = False
                    break
                symbol = raw["symbol"]
                if not isinstance(symbol, str):
                    valid = False
                    break
                company = await profile(symbol)
                if (
                    company.isin not in (None, isin)
                    or currency_unit(company.currency)[0] != raw.get("currency")
                    or company.exchange != raw.get("exchange")
                    or company.quote_type != raw.get("quote_type")
                    or company.name != raw.get("name")
                    or company.country != raw.get("country")
                    or raw.get("source") != "openfigi_isin_and_reverse_listing"
                    or (
                        target is not None
                        and target != currency_unit(company.currency)[0]
                    )
                    or not isinstance(raw.get("figi"), str)
                    or not re.fullmatch(r"[A-Z0-9]{12}", str(raw["figi"]))
                ):
                    valid = False
                    break
                verified.append(
                    company.model_copy(
                        update={"isin": isin, "identifier_source": "openfigi_cached"}
                    )
                )
            if valid:
                return verified
        job = {"idType": "ID_ISIN", "idValue": isin, "marketSecDes": "Equity"}
        if target:
            job["currency"] = target
        forward = (await self.client.map([job]))[0]
        if not forward:
            return []
        symbols: set[str] = set(suggestions or [])
        for listing in forward:
            suffix = FIGI_SUFFIXES.get(listing.exchCode)
            if (
                suffix is None
                or listing.marketSector != "Equity"
                or not re.fullmatch(r"[A-Za-z0-9./ -]{1,32}", listing.ticker)
            ):
                continue
            local = listing.ticker.replace("/", "-").replace(" ", "-")
            if suffix == ".HK" and local.isdigit():
                local = local.zfill(4)
            if suffix == ".L":
                local = local.rstrip(".")
            symbols.add(local + suffix)
        if len(symbols) > 12:
            raise MarketError(
                "Too many proposed Yahoo listings; an explicit listing selection is needed."
            )
        verified = []
        evidence: list[dict[str, object]] = []
        for symbol in sorted(symbols):
            try:
                company = await profile(symbol)
            except MarketError:
                continue
            if company.isin not in (None, isin) or (
                target and currency_unit(company.currency)[0] != target
            ):
                continue
            venue = YAHOO_VENUES.get(company.exchange or "")
            if venue is None:
                continue
            mic, suffix = venue
            if suffix and not company.symbol.endswith(suffix):
                continue
            local = company.symbol[: -len(suffix)] if suffix else company.symbol
            variants = sorted({local, local.replace("-", "/"), local.replace("-", " ")})
            if local.isdigit():
                variants = sorted(set(variants) | {str(int(local))})
            reverse_jobs = [
                {
                    "idType": "TICKER",
                    "idValue": value,
                    "micCode": mic,
                    "currency": currency_unit(company.currency)[0],
                    "marketSecDes": "Equity",
                }
                for mic in mic.split("|")
                for value in variants
            ]
            reverse = [
                listing
                for result in await self.client.map(reverse_jobs)
                for listing in result
            ]
            match = next(
                (
                    right
                    for right in reverse
                    if compatible(company, right)
                    and any(
                        compatible(company, left)
                        and right.identifiers() & left.identifiers()
                        for left in forward
                    )
                ),
                None,
            )
            if match is not None:
                verified.append(
                    company.model_copy(
                        update={"isin": isin, "identifier_source": "openfigi"}
                    )
                )
                evidence.append(
                    {
                        "symbol": company.symbol,
                        "currency": currency_unit(company.currency)[0],
                        "exchange": company.exchange,
                        "quote_type": company.quote_type,
                        "name": company.name,
                        "country": company.country,
                        "figi": match.figi,
                        "composite_figi": match.compositeFIGI,
                        "source": "openfigi_isin_and_reverse_listing",
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
        if evidence:
            self.cache.put(key, {"listings": evidence})
        return verified
