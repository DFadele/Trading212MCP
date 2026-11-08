"""Seeded, synthetic 58-holding benchmark; never reads credentials or the network."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from mcp.server.fastmcp import FastMCP

from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.models import (
    Company,
    Fundamentals,
    MarketError,
    Price,
    PriceHistory,
    Universe,
)
from trading212_mcp.market.service import MarketService
from trading212_mcp.tools.account import register_account_tools
from trading212_mcp.tools.history import register_history_tools
from trading212_mcp.tools.orders import register_order_tools
from trading212_mcp.tools.portfolio import register_portfolio_tools
from trading212_mcp.tools.positions import register_position_tools
from trading212_mcp.tools.research import register_research_tools
from trading212_mcp.trading212.models import (
    AccountSummaryResult,
    Dividend,
    HistoricalOrder,
    HistoryResult,
    PendingOrdersResult,
    PositionsResult,
    Transaction,
)

SEED = 21258
CALLS: dict[str, dict[str, Any]] = {
    "get_account_summary": {},
    "get_positions": {},
    "get_portfolio_allocation": {},
    "get_pending_orders": {},
    "get_order_history": {"limit": 50},
    "get_dividend_history": {"limit": 50},
    "get_transaction_history": {"limit": 50},
    "get_market_data": {
        "tickers": [f"STK{i:02}" for i in range(16)] + ["NEW0", "NEW1", "NEW2", "SPY"]
    },
    "analyse_portfolio_risk": {},
    "screen_stocks": {"limit": 20},
    "analyse_candidate_investment": {
        "tickers": ["NEW0", "NEW1", "NEW2"],
        "investment_amount_gbp": 2000,
        "compare_cash": True,
        "allocations": {"NEW0": 0.5, "NEW1": 0.5},
    },
    "optimise_new_contribution": {
        "tickers": ["NEW0", "NEW1", "NEW2"],
        "investment_amount_gbp": 2000,
        "allow_cash": True,
        "max_cash_allocation": 0.25,
    },
}
RESEARCH = set(list(CALLS)[7:])


class BenchmarkProvider:
    name = "synthetic_benchmark"

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.dates = pd.bdate_range(end=self.now.date(), periods=756)
        rng = np.random.default_rng(SEED)
        market = rng.normal(0.00025, 0.008, 756)
        factors = rng.normal(0, 0.004, (6, 756))
        sectors = [
            "Technology",
            "Healthcare",
            "Financial Services",
            "Industrials",
            "Consumer Cyclical",
            "Utilities",
        ]
        self.companies = {}
        self.series = {}
        for i, symbol in enumerate(
            [f"STK{j:02}" for j in range(58)] + [f"NEW{j}" for j in range(24)] + ["SPY"]
        ):
            currency = ["USD", "EUR", "GBp"][i % 3]
            sector = sectors[(i % 12) // 2]
            self.companies[symbol] = Company(
                symbol=symbol,
                name=f"Synthetic company {symbol}",
                currency=currency,
                isin=f"FIXTURE{i:05}",
                exchange={"USD": "NASDAQ", "EUR": "PAR", "GBp": "LSE"}[currency],
                country={"USD": "US", "EUR": "FR", "GBp": "GB"}[currency],
                sector=sector if symbol != "SPY" else None,
                industry=f"{sector} industry",
                market_cap=float((i + 1) * 1e9),
                price=100,
                quote_type="ETF" if symbol == "SPY" else "EQUITY",
                is_etf=symbol == "SPY",
                identifier_source="synthetic_fixture",
            )
            daily = (
                market
                if symbol == "SPY"
                else (0.65 + 0.01 * (i % 30)) * market
                + factors[(i % 12) // 2]
                + rng.normal(0, 0.006, 756)
            )
            self.series[symbol] = (
                100 * np.cumprod(1 + daily) * (100 if currency == "GBp" else 1)
            )
        self.cache = {}
        self.fx_rates = {
            "USD": 0.79 * (1 + 0.012 * np.sin(np.arange(756) / 31)),
            "EUR": 0.86 * (1 + 0.008 * np.cos(np.arange(756) / 27)),
        }

    async def profile(self, symbol):
        if symbol not in self.companies:
            raise MarketError("Synthetic fixture has no such symbol")
        return self.companies[symbol]

    async def search_isin(self, isin, currency=None):
        return [c for c in self.companies.values() if c.isin == isin]

    async def history(self, company, start, end):
        key = (company.symbol, start, end)
        if key not in self.cache:
            self.cache[key] = PriceHistory(
                symbol=company.symbol,
                currency=company.currency,
                source=self.name,
                retrieved_at=self.now,
                price_basis="split_and_dividend_adjusted",
                prices=[
                    Price(date=d.date(), close=float(v))
                    for d, v in zip(
                        self.dates, self.series[company.symbol], strict=True
                    )
                    if start <= d.date() <= end
                ],
                warnings=["Synthetic adjusted-close fixture; not live market data."],
            )
        return self.cache[key]

    async def fx(self, source, target, start, end):
        rates = {**self.fx_rates, "GBP": np.ones(len(self.dates))}
        cross = rates[source] / rates[target]
        return PriceHistory(
            symbol=source + target,
            currency=target,
            source=self.name,
            retrieved_at=self.now,
            price_basis="fx_spot",
            prices=[
                Price(date=d.date(), close=float(v))
                for d, v in zip(self.dates, cross, strict=True)
                if start <= d.date() <= end
            ],
        )

    async def fundamentals(self, company):
        if company.is_etf:
            raise MarketError("Company fundamentals unavailable for synthetic ETF")
        i = list(self.companies).index(company.symbol)
        return Fundamentals(
            source=self.name,
            retrieved_at=self.now,
            statement_date=self.now.date().replace(month=1, day=1) - timedelta(days=1),
            statement_currency="GBP" if company.currency == "GBp" else company.currency,
            revenue=1e10 + i * 1e8,
            net_income=1e9 + i * 1e7,
            total_assets=2e10,
            revenue_growth=0.04 + 0.005 * (i % 30),
            earnings_growth=0.02 + 0.008 * (i % 20),
            pe=10 + i % 30,
            forward_pe=9 + i % 25,
            price_to_book=2 + i % 6,
            price_to_sales=1 + i % 10,
            net_margin=0.05 + 0.01 * (i % 20),
            operating_margin=0.1 + 0.01 * (i % 20),
            roic=None,
            debt_to_equity=0.2 + 0.05 * (i % 10),
            total_debt=2e9,
            cash=1e9,
            operating_cash_flow=1.5e9,
            free_cash_flow=1e9,
            provenance={
                "annual": "Synthetic annual period; fixture definition only; annual amounts retain financial statement currency.",
                "ratios": "Synthetic snapshot ratios for deterministic response benchmarks; no investment meaning.",
            },
            warnings=[
                "Synthetic fundamentals; not live financial statements.",
                "ROIC unavailable in this fixture; not substituted with ROE.",
            ],
        )

    async def universe(self, filters, max_candidates):
        companies = [c for s, c in self.companies.items() if s.startswith("NEW")]
        return Universe(
            source=self.name,
            retrieved_at=self.now,
            companies=companies[:max_candidates],
            truncated=len(companies) > max_candidates,
            pages_fetched=1,
            description="Synthetic 24-company discovery fixture outside the 58 holdings; not live Yahoo coverage.",
        )

    async def aclose(self):
        pass


class BenchmarkTrading:
    def __init__(self, provider):
        self.provider = provider
        rng = np.random.default_rng(SEED + 1)
        values = rng.lognormal(0, 1.0, 58)
        values = np.round(values / values.sum() * 100000, 2)
        values[-1] += 100000 - values.sum()
        positions = []
        for i, value in enumerate(values):
            symbol = f"STK{i:02}"
            company = provider.companies[symbol]
            current = float(provider.series[symbol][-1])
            fx = (
                1
                if company.currency == "GBp"
                else float(provider.fx_rates[company.currency][-1])
            )
            converted = current * (0.01 if company.currency == "GBp" else fx)
            positions.append(
                {
                    "averagePricePaid": str(current * 0.85),
                    "createdAt": "2024-01-02T12:00:00Z",
                    "currentPrice": str(current),
                    "instrument": {
                        "ticker": symbol
                        + ("_US_EQ" if company.currency == "USD" else "_EQ"),
                        "isin": company.isin,
                        "name": company.name,
                        "currency": company.currency,
                    },
                    "quantity": str(float(value) / converted),
                    "quantityAvailableForTrading": str(float(value) / converted),
                    "quantityInPies": "0",
                    "walletImpact": {
                        "currency": "GBP",
                        "currentValue": f"{value:.2f}",
                        "fxImpact": "0",
                        "totalCost": f"{value * 0.85:.2f}",
                        "unrealizedProfitLoss": f"{value * 0.15:.2f}",
                    },
                }
            )
        self.snapshot = PositionsResult.model_validate(
            {
                "environment": "demo",
                "retrieved_at": provider.now,
                "positions": positions,
            }
        )

    async def get_account_summary(self):
        return AccountSummaryResult.model_validate(
            {
                "environment": "demo",
                "retrieved_at": self.provider.now,
                "account": {
                    "id": 1,
                    "currency": "GBP",
                    "totalValue": "102000",
                    "cash": {
                        "availableToTrade": "2000",
                        "inPies": "0",
                        "reservedForOrders": "0",
                    },
                    "investments": {
                        "currentValue": "100000",
                        "realizedProfitLoss": "1000",
                        "totalCost": "85000",
                        "unrealizedProfitLoss": "15000",
                    },
                },
            }
        )

    async def get_positions(self):
        return self.snapshot

    def order(self, i):
        return {
            "id": i,
            "createdAt": self.provider.now,
            "currency": "GBP",
            "instrument": self.snapshot.positions[i % 58].instrument.model_dump(
                mode="json"
            ),
            "type": "LIMIT",
            "side": "BUY",
            "status": "NEW",
            "strategy": "QUANTITY",
            "quantity": "2",
            "filledQuantity": "0",
            "limitPrice": "100",
            "timeInForce": "DAY",
        }

    async def get_pending_orders(self):
        return PendingOrdersResult.model_validate(
            {
                "environment": "demo",
                "retrieved_at": self.provider.now,
                "orders": [self.order(i) for i in range(8)],
            }
        )

    async def get_order_history(self, limit=20, cursor=None, ticker=None):
        return HistoryResult[HistoricalOrder].model_validate(
            {
                "environment": "demo",
                "retrieved_at": self.provider.now,
                "items": [
                    {
                        "order": {
                            **self.order(i),
                            "status": "FILLED",
                            "filledQuantity": "2",
                        },
                        "fill": {
                            "id": i,
                            "filledAt": self.provider.now,
                            "price": "100",
                            "quantity": "2",
                            "type": "MARKET",
                            "tradingMethod": "OTC",
                            "walletImpact": {
                                "currency": "GBP",
                                "fxRate": "1",
                                "netValue": "200",
                                "realisedProfitLoss": "0",
                                "taxes": [
                                    {
                                        "chargedAt": self.provider.now,
                                        "currency": "GBP",
                                        "name": "STAMP_DUTY",
                                        "quantity": "1",
                                    }
                                ],
                            },
                        },
                    }
                    for i in range(limit)
                ],
                "next_cursor": "50",
            }
        )

    async def get_dividend_history(self, limit=20, cursor=None, ticker=None):
        return HistoryResult[Dividend].model_validate(
            {
                "environment": "demo",
                "retrieved_at": self.provider.now,
                "items": [
                    {
                        "amount": "12.34",
                        "currency": "GBP",
                        "paidOn": self.provider.now,
                        "quantity": "10",
                        "reference": str(i),
                        "type": "DIVIDEND",
                        "instrument": self.snapshot.positions[
                            i % 58
                        ].instrument.model_dump(mode="json"),
                        "grossAmountPerShare": "1.234",
                    }
                    for i in range(limit)
                ],
                "next_cursor": "50",
            }
        )

    async def get_transaction_history(self, limit=20, cursor=None, since=None):
        return HistoryResult[Transaction].model_validate(
            {
                "environment": "demo",
                "retrieved_at": self.provider.now,
                "items": [
                    {
                        "amount": "1000",
                        "currency": "GBP",
                        "dateTime": self.provider.now,
                        "reference": str(i),
                        "type": "DEPOSIT",
                    }
                    for i in range(limit)
                ],
                "next_cursor": "50",
            }
        )


def build_fixture(cache_path: Path) -> tuple[FastMCP, MarketService, BenchmarkTrading]:
    provider = BenchmarkProvider()
    trading = BenchmarkTrading(provider)
    settings = MarketSettings(
        _env_file=None,
        provider="yfinance",
        fundamentals_source="provider",
        risk_timeout_seconds=50,
        requests_per_minute=3000,
        screen_max_candidates=24,
        resolution_cache_path=cache_path,
    )
    service = MarketService(provider, settings)
    server = FastMCP("Synthetic 58-holding benchmark")
    for register in [
        register_account_tools,
        register_position_tools,
        register_portfolio_tools,
        register_order_tools,
        register_history_tools,
    ]:
        register(server, lambda: trading)
    register_research_tools(server, lambda: trading, lambda: service)
    return server, service, trading
