from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class MarketError(Exception):
    """Safe to expose; never contains credentials or raw provider responses."""


class MarketModel(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class Company(MarketModel):
    symbol: str
    name: str
    currency: str
    isin: str | None = None
    exchange: str | None = None
    country: str | None = None
    sector: str | None = None
    industry: str | None = None
    market_cap: FiniteFloat | None = None
    price: FiniteFloat | None = None
    volume: FiniteFloat | None = None
    average_volume: FiniteFloat | None = None
    average_volume_period: str | None = None
    is_etf: bool = False
    is_fund: bool = False
    quote_type: str | None = None
    identifier_source: str | None = None


class Price(MarketModel):
    date: date
    close: FiniteFloat = Field(gt=0)


class PriceHistory(MarketModel):
    symbol: str
    currency: str
    source: str
    retrieved_at: datetime
    price_basis: Literal["split_and_dividend_adjusted", "fx_spot"]
    prices: list[Price] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list[str])


class AnnualFinancials(MarketModel):
    end: date
    start: date | None = None
    filed: date | None = None
    currency: str
    accession: str | None = None
    revenue: FiniteFloat | None = None
    net_income: FiniteFloat | None = None
    operating_income: FiniteFloat | None = None
    operating_cash_flow: FiniteFloat | None = None
    free_cash_flow: FiniteFloat | None = None
    total_assets: FiniteFloat | None = None
    equity: FiniteFloat | None = None
    total_debt: FiniteFloat | None = None


class Fundamentals(MarketModel):
    source: str
    retrieved_at: datetime
    statement_date: date | None = None
    statement_currency: str | None = None
    revenue: FiniteFloat | None = None
    net_income: FiniteFloat | None = None
    total_assets: FiniteFloat | None = None
    revenue_growth: FiniteFloat | None = None
    earnings_growth: FiniteFloat | None = None
    pe: FiniteFloat | None = None
    forward_pe: FiniteFloat | None = None
    price_to_book: FiniteFloat | None = None
    price_to_sales: FiniteFloat | None = None
    net_margin: FiniteFloat | None = None
    operating_margin: FiniteFloat | None = None
    roic: FiniteFloat | None = None
    debt_to_equity: FiniteFloat | None = None
    total_debt: FiniteFloat | None = None
    cash: FiniteFloat | None = None
    operating_cash_flow: FiniteFloat | None = None
    free_cash_flow: FiniteFloat | None = None
    annual_history: list[AnnualFinancials] = Field(
        default_factory=list[AnnualFinancials]
    )
    ev_to_ebitda: FiniteFloat | None = None
    provenance: dict[str, str] = Field(default_factory=dict[str, str])
    warnings: list[str] = Field(default_factory=list[str])


class Universe(MarketModel):
    source: str
    retrieved_at: datetime
    companies: list[Company]
    truncated: bool
    pages_fetched: int
    reported_total: int | None = None
    description: str
