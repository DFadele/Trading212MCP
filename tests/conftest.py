from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

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
from trading212_mcp.trading212.models import AccountSummaryResult, PositionsResult


@pytest.fixture
def market_settings(tmp_path):
    return MarketSettings(
        _env_file=None,
        fmp_api_key="unit-test-key",
        requests_per_minute=3000,
        min_observations=20,
        retries=0,
        cache_ttl_seconds=3600,
        resolution_cache_path=tmp_path / "instrument-mappings.sqlite3",
    )


class FakeProvider:
    name = "fixture_provider"

    def __init__(self):
        self.companies = {
            "AAA": Company(
                symbol="AAA",
                name="Alpha Automation",
                currency="USD",
                isin="ISIN_A",
                sector="Technology",
                industry="Semiconductors",
                country="US",
                exchange="NASDAQ",
                market_cap=1000,
                price=100,
            ),
            "BBB": Company(
                symbol="BBB",
                name="Beta Automation",
                currency="USD",
                isin="ISIN_B",
                sector="Technology",
                industry="Semiconductors",
                country="US",
                exchange="NASDAQ",
                market_cap=2000,
                price=50,
            ),
            "SPY": Company(
                symbol="SPY",
                name="Benchmark ETF",
                currency="USD",
                isin="ISIN_SPY",
                is_etf=True,
            ),
        }
        self.failed = set()
        self.dates = pd.bdate_range(end=datetime.now(timezone.utc).date(), periods=300)
        self.series = {
            key: 100 * np.cumprod(1 + 0.001 + 0.01 * np.sin(np.arange(300) * frequency))
            for key, frequency in [("AAA", 0.7), ("BBB", 1.1), ("SPY", 0.3)]
        }

    async def profile(self, symbol):
        if symbol in self.failed or symbol not in self.companies:
            raise MarketError("Fixture provider unavailable")
        return self.companies[symbol]

    async def search_isin(self, isin, currency=None):
        return [c for c in self.companies.values() if c.isin == isin]

    async def history(self, company, start, end):
        if company.symbol in self.failed:
            raise MarketError("Fixture provider unavailable")
        return PriceHistory(
            symbol=company.symbol,
            currency=company.currency,
            source=self.name,
            retrieved_at=datetime.now(timezone.utc),
            price_basis="split_and_dividend_adjusted",
            prices=[
                Price(date=d.date(), close=float(v))
                for d, v in zip(self.dates, self.series[company.symbol], strict=True)
                if start <= d.date() <= end
            ],
        )

    async def fundamentals(self, company):
        better = company.symbol == "AAA"
        return Fundamentals(
            source=self.name,
            retrieved_at=datetime.now(timezone.utc),
            pe=10 if better else 30,
            forward_pe=9 if better else 25,
            price_to_book=2,
            price_to_sales=3,
            revenue_growth=0.3 if better else 0.1,
            earnings_growth=0.4 if better else 0.05,
            net_margin=0.2 if better else 0.1,
            roic=0.2 if better else 0.1,
            debt_to_equity=0.2 if better else 1.5,
            free_cash_flow=100,
        )

    async def fx(self, source, target, start, end):
        return PriceHistory(
            symbol=source + target,
            currency=target,
            source=self.name,
            retrieved_at=datetime.now(timezone.utc),
            price_basis="fx_spot",
            prices=[Price(date=d.date(), close=0.8) for d in self.dates],
        )

    async def universe(self, filters, max_candidates):
        return Universe(
            source=self.name,
            retrieved_at=datetime.now(timezone.utc),
            companies=list(self.companies.values())[:max_candidates],
            truncated=False,
            pages_fetched=1,
            description="Fixture provider universe",
        )

    async def aclose(self):
        pass


class FakeTrading:
    async def get_account_summary(self):
        return AccountSummaryResult.model_validate(
            {
                "environment": "demo",
                "retrieved_at": datetime.now(timezone.utc),
                "account": {
                    "id": 1,
                    "currency": "USD",
                    "totalValue": "1100",
                    "cash": {
                        "availableToTrade": "100",
                        "inPies": "0",
                        "reservedForOrders": "0",
                    },
                    "investments": {
                        "currentValue": "1000",
                        "totalCost": "900",
                        "realizedProfitLoss": "0",
                        "unrealizedProfitLoss": "100",
                    },
                },
            }
        )

    async def get_positions(self):
        return PositionsResult.model_validate(
            {
                "environment": "demo",
                "retrieved_at": datetime.now(timezone.utc),
                "positions": [
                    {
                        "averagePricePaid": "90",
                        "currentPrice": "100",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "instrument": {
                            "currency": "USD",
                            "isin": "ISIN_A",
                            "name": "Alpha",
                            "ticker": "AAA_US_EQ",
                        },
                        "quantity": "10",
                        "quantityAvailableForTrading": "10",
                        "quantityInPies": "0",
                        "walletImpact": {
                            "currency": "USD",
                            "currentValue": "1000",
                            "totalCost": "900",
                            "unrealizedProfitLoss": "100",
                            "fxImpact": None,
                        },
                    }
                ],
            }
        )


@pytest.fixture
def provider():
    return FakeProvider()


@pytest.fixture
def service(provider, market_settings):
    return MarketService(provider, market_settings)


@pytest.fixture
def trading():
    return FakeTrading()
