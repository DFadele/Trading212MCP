"""Opt-in SEC annual US-GAAP statements; never a substitute for price history."""

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from trading212_mcp.market.cache import CachedResponse, TTLCache
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.models import (
    AnnualFinancials,
    Company,
    Fundamentals,
    MarketError,
)
from trading212_mcp.market.normalization import is_list, is_mapping, number
from trading212_mcp.observability import timed_async


@dataclass
class AnnualReport:
    start: date
    end: date
    currency: str
    accession: str
    filed: date
    values: dict[str, float] = field(default_factory=dict[str, float])
    tags: dict[str, str] = field(default_factory=dict[str, str])


TAGS = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss",),
    "operating_income": ("OperatingIncomeLoss",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment",),
}


def mapping(value: object) -> dict[str, object]:
    return value if is_mapping(value) else {}


def annual_reports(payload: dict[str, object], as_of: date) -> list[AnnualReport]:
    gaap = mapping(mapping(payload.get("facts")).get("us-gaap"))
    if not gaap:
        raise MarketError(
            "SEC supplement supports annual standard US-GAAP concepts only; IFRS/custom tags are not guessed."
        )
    reports: dict[tuple[date, date, str, str], AnnualReport] = {}
    for metric, tags in TAGS.items():
        for tag in tags:
            for currency, records in mapping(
                mapping(gaap.get(tag)).get("units")
            ).items():
                if not re.fullmatch("[A-Z]{3}", currency) or not is_list(records):
                    continue
                for raw in records:
                    row = mapping(raw)
                    if (
                        row.get("form") not in ("10-K", "10-K/A")
                        or row.get("fp") != "FY"
                    ):
                        continue
                    try:
                        start = date.fromisoformat(str(row["start"]))
                        end = date.fromisoformat(str(row["end"]))
                        filed = date.fromisoformat(str(row["filed"]))
                    except (KeyError, ValueError):
                        continue
                    accession = row.get("accn")
                    value = number(row.get("val"))
                    if (
                        not isinstance(accession, str)
                        or not accession
                        or value is None
                        or not 300 <= (end - start).days <= 400
                        or end > as_of
                        or filed > as_of
                    ):
                        continue
                    key = (start, end, accession, currency)
                    report = reports.setdefault(
                        key, AnnualReport(start, end, currency, accession, filed)
                    )
                    if metric in report.tags and report.tags[metric] != tag:
                        continue  # Ordered, semantically compatible concept preference.
                    if metric in report.values and report.values[metric] != value:
                        raise MarketError(
                            "Conflicting SEC facts for the same period, unit and accession."
                        )
                    report.values[metric] = value
                    report.tags[metric] = tag
    latest: dict[tuple[date, str], AnnualReport] = {}
    for report in reports.values():
        key = (report.end, report.currency)
        old = latest.get(key)
        if old is None or (report.filed, report.accession) > (old.filed, old.accession):
            latest[key] = report
    result = sorted(latest.values(), key=lambda r: (r.end, r.filed), reverse=True)
    if not result:
        raise MarketError(
            "SEC has no supported annual statements; no quarterly/YTD periods substituted."
        )
    newest = result[0].end
    if len({report.currency for report in result if report.end == newest}) > 1:
        raise MarketError(
            "SEC annual reporting currency is ambiguous; metrics withheld."
        )
    return result


def sec_fundamentals(
    payload: dict[str, object], retrieved_at: datetime
) -> Fundamentals:
    reports = annual_reports(payload, retrieved_at.date())
    current = reports[0]
    prior = next(
        (
            report
            for report in reports[1:]
            if report.currency == current.currency
            and 300 <= (current.end - report.end).days <= 430
        ),
        None,
    )

    def growth(metric: str) -> float | None:
        latest = current.values.get(metric)
        previous = prior.values.get(metric) if prior else None
        if latest is None or previous is None or previous <= 0:
            return None
        return latest / previous - 1

    revenue = current.values.get("revenue")

    def margin(metric: str) -> float | None:
        value = current.values.get(metric)
        return (
            value / revenue
            if value is not None and revenue is not None and revenue > 0
            else None
        )

    operating = current.values.get("operating_cash_flow")
    capex = current.values.get("capex")

    def instant(tag: str, report: AnnualReport = current) -> float | None:
        units = mapping(
            mapping(mapping(mapping(payload.get("facts")).get("us-gaap")).get(tag)).get(
                "units"
            )
        )
        records = units.get(report.currency, [])
        if not is_list(records):
            return None
        matches = {
            value
            for raw in records
            if (row := mapping(raw)).get("accn") == report.accession
            and row.get("end") == report.end.isoformat()
            and "start" not in row
            and (value := number(row.get("val"))) is not None
        }
        return matches.pop() if len(matches) == 1 else None

    return Fundamentals(
        source="sec_edgar_annual",
        retrieved_at=retrieved_at,
        statement_date=current.end,
        statement_currency=current.currency,
        revenue_growth=growth("revenue"),
        earnings_growth=growth("net_income"),
        net_margin=margin("net_income"),
        operating_margin=margin("operating_income"),
        revenue=revenue,
        net_income=current.values.get("net_income"),
        total_assets=instant("Assets"),
        cash=instant("CashAndCashEquivalentsAtCarryingValue"),
        operating_cash_flow=operating,
        free_cash_flow=operating - capex
        if operating is not None and capex is not None and capex >= 0
        else None,
        annual_history=[
            AnnualFinancials(
                start=r.start,
                end=r.end,
                filed=r.filed,
                currency=r.currency,
                accession=r.accession,
                revenue=r.values.get("revenue"),
                net_income=r.values.get("net_income"),
                operating_income=r.values.get("operating_income"),
                operating_cash_flow=r.values.get("operating_cash_flow"),
                free_cash_flow=r.values["operating_cash_flow"] - r.values["capex"]
                if "operating_cash_flow" in r.values and r.values.get("capex", -1) >= 0
                else None,
                total_assets=instant("Assets", r),
                equity=instant("StockholdersEquity", r),
            )
            for r in reports[:4]
        ],
        provenance={
            "current_annual": f"SEC companyfacts US-GAAP; start={current.start}; end={current.end}; filed={current.filed}; accession={current.accession}; currency={current.currency}; tags={current.tags}",
            "prior_annual": f"end={prior.end}; filed={prior.filed}; accession={prior.accession}; currency={prior.currency}"
            if prior
            else "Unavailable comparable prior annual report",
            "free_cash_flow": "Same accession and full-year context: operating cash flow minus payments to acquire PP&E; no missing-capex-as-zero assumption.",
        },
        warnings=[
            "Explicit SEC annual-statement mode, not TTM or vendor snapshot fundamentals. Provider fundamentals are not merged or used as fallback.",
            "SEC provides no current P/E, forward P/E, price/book, price/sales, price history or FX. These fields remain null. ROIC and total-debt metrics are withheld without a complete supported definition.",
            "Standard US-GAAP entity-wide 10-K/10-K/A facts only; no custom taxonomy, IFRS, quarterly-to-TTM assembly or historical point-in-time reconstruction. Latest filed restatements may change older figures.",
        ],
    )


EXCHANGES = {
    "NASDAQ": "NASDAQ",
    "NMS": "NASDAQ",
    "NGM": "NASDAQ",
    "NCM": "NASDAQ",
    "NYSE": "NYSE",
    "NYQ": "NYSE",
    "NYSEAMERICAN": "NYSEAMERICAN",
    "ASE": "NYSEAMERICAN",
    "NYSEARCA": "NYSEARCA",
    "PCX": "NYSEARCA",
    "BATS": "BATS",
    "BTS": "BATS",
}


def validate_user_agent(agent: str | None) -> str:
    if (
        not agent
        or "\n" in agent
        or "\r" in agent
        or not re.search(r"\S+@\S+\.\S+", agent)
    ):
        raise MarketError(
            "SEC mode requires MARKET_SEC_USER_AGENT with application name and a real contact email."
        )
    return agent


class SECStatements:
    name = "sec_edgar_annual"

    def __init__(
        self,
        settings: MarketSettings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            headers={"Accept-Encoding": "gzip, deflate"},
            timeout=settings.timeout_seconds,
            transport=transport,
            follow_redirects=False,
        )
        self._cache = TTLCache(
            settings.cache_ttl_seconds,
            settings.cache_max_entries,
            settings.cache_path,
            "sec",
        )
        self._lock = asyncio.Lock()
        self._next_request = 0.0

    @timed_async("market_data")
    async def _get(self, url: str) -> CachedResponse:
        async with self._lock:
            # Missing contact details must not disable price/discovery/risk tools.
            self._http.headers["User-Agent"] = validate_user_agent(
                self.settings.sec_user_agent
            )
            cached = self._cache.get(url)
            if cached is not None:
                return cached
            for attempt in range(self.settings.retries + 1):
                delay = self._next_request - time.monotonic()
                if delay > 10:
                    raise MarketError(
                        "SEC fair-access cooldown is active; retry later."
                    )
                if delay > 0:
                    await asyncio.sleep(delay)
                # Two requests/second at most, also bounded by configured plan pacing.
                self._next_request = time.monotonic() + max(
                    0.5, 60 / self.settings.requests_per_minute
                )
                try:
                    async with asyncio.timeout(self.settings.timeout_seconds):
                        response = await self._http.get(url)
                except (httpx.RequestError, TimeoutError):
                    if attempt < self.settings.retries:
                        self._next_request = max(
                            self._next_request, time.monotonic() + 2**attempt
                        )
                        continue
                    raise MarketError("SEC request failed or timed out.") from None
                if response.status_code in (403, 429):
                    self._next_request = time.monotonic() + 60
                    raise MarketError(
                        "SEC fair-access restriction; verify User-Agent and retry later. No bypass attempted."
                    )
                if response.status_code >= 500 and attempt < self.settings.retries:
                    self._next_request = max(
                        self._next_request, time.monotonic() + 2**attempt
                    )
                    continue
                if response.status_code != 200:
                    raise MarketError(
                        f"SEC request failed (HTTP {response.status_code})."
                    )
                try:
                    payload = response.json()
                except ValueError:
                    raise MarketError("SEC returned invalid JSON.") from None
                if not is_mapping(payload):
                    raise MarketError("SEC returned an unsupported response shape.")
                result = CachedResponse(
                    [{"value": payload}], datetime.now(timezone.utc)
                )
                self._cache.put(url, result)
                return result
        raise MarketError("SEC request failed.")

    async def fundamentals(self, company: Company) -> Fundamentals:
        index = await self._get(
            "https://www.sec.gov/files/company_tickers_exchange.json"
        )
        payload = mapping(index.rows[0]["value"])
        fields, rows = payload.get("fields"), payload.get("data")
        if (
            not is_list(fields)
            or not is_list(rows)
            or any(not isinstance(key, str) for key in fields)
            or len(set(fields)) != len(fields)
            or not {"cik", "ticker", "exchange"} <= set(fields)
        ):
            raise MarketError("SEC ticker/exchange index has an unsupported schema.")
        candidates: set[int] = set()
        expected_exchange = EXCHANGES.get(
            (company.exchange or "").upper().replace(" ", "")
        )
        for raw in rows:
            if not is_list(raw) or len(raw) != len(fields):
                continue
            record = dict(zip(fields, raw, strict=True))
            actual_exchange = EXCHANGES.get(
                str(record.get("exchange", "")).upper().replace(" ", "")
            )
            if (
                record.get("ticker") == company.symbol
                and expected_exchange
                and expected_exchange == actual_exchange
            ):
                cik = number(record.get("cik"))
                if cik is not None and cik.is_integer() and 0 < cik < 10**10:
                    candidates.add(int(cik))
        if len(candidates) != 1:
            raise MarketError(
                "SEC requires a unique exact ticker/exchange match to a CIK; unsupported listings are not guessed."
            )
        cik = next(iter(candidates))
        result = await self._get(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
        )
        facts = mapping(result.rows[0]["value"])
        if facts.get("cik") != cik:
            raise MarketError("SEC facts CIK does not match the resolved company.")
        return sec_fundamentals(facts, result.retrieved_at)

    async def aclose(self) -> None:
        await self._http.aclose()
