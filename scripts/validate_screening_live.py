"""Small opt-in Yahoo screener smoke check; never loads Trading 212 credentials.

Uses explicit provider or SEC fundamentals, three US candidates,
no owned-position filtering or portfolio factor, and no broker requests.
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path
from typing import Literal, cast

from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.factory import FundamentalsProvider
from trading212_mcp.market.models import MarketError
from trading212_mcp.market.provider import MarketProvider
from trading212_mcp.market.sec import SECStatements
from trading212_mcp.market.service import MarketService
from trading212_mcp.market.yahoo import YFinanceBackend, YFinanceProvider
from trading212_mcp.quantitative.screening import screen
from trading212_mcp.trading212.client import Trading212Client


async def main(source: Literal["provider", "sec"] = "provider") -> None:
    with tempfile.TemporaryDirectory(prefix="screening-live-") as directory:
        settings = MarketSettings(
            _env_file=None,  # pyright: ignore[reportCallIssue]
            provider="yfinance",
            fundamentals_source=source,
            sec_user_agent=MarketSettings().sec_user_agent if source == "sec" else None,
            yahoo_regions=["us"],
            screen_max_candidates=3,
            timeout_seconds=8,
            retries=0,
            openfigi_enabled=False,
            resolution_cache_path=Path(directory) / "mappings.sqlite3",
        )
        if source == "sec" and not settings.sec_user_agent:
            raise SystemExit(
                "Set MARKET_SEC_USER_AGENT to a real application/contact before SEC validation."
            )
        backend = YFinanceBackend()
        backend.sdk.set_tz_cache_location(str(Path(directory) / "yahoo-cache"))
        provider: MarketProvider = YFinanceProvider(settings, backend)
        if source == "sec":
            provider = FundamentalsProvider(provider, SECStatements(settings))
        try:
            async with asyncio.timeout(55):
                result = await screen(
                    MarketService(provider, settings),
                    cast(Trading212Client, object()),
                    {"countries": ["US"]},
                    False,
                    3,
                    "quality_growth",
                    {"growth": 0.35, "valuation": 0.3, "quality": 0.35},
                )
            verified = (
                bool(result["candidates"])
                and result["coverage"]["stages"]["fundamentals_available"] > 0
                and result["coverage"]["stages"]["price_history_available"] > 0
            )
            print(
                json.dumps(
                    {
                        "status": "live_data_verified"
                        if verified
                        else "live_partial_failure",
                        "source": result["source"],
                        "coverage": result["coverage"],
                        "candidates": [
                            {
                                "symbol": c["company"]["symbol"],
                                "score": c["composite_score"],
                                "statement_date": c["metrics"]["statement_date"],
                                "available_factor_weight": c["available_factor_weight"],
                                "missing_metrics": c["missing_metrics"],
                                "warnings": c["warnings"],
                            }
                            for c in result["candidates"]
                        ],
                    },
                    indent=2,
                )
            )
            if not verified:
                raise SystemExit(2)
        except (MarketError, TimeoutError) as exc:
            print(
                json.dumps(
                    {
                        "status": "live_validation_failed",
                        "reason": str(exc) or "55-second smoke-check budget exhausted",
                    }
                )
            )
            raise SystemExit(1) from None
        finally:
            await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fundamentals-source", choices=["provider", "sec"], default="provider"
    )
    arguments = parser.parse_args()
    asyncio.run(main(arguments.fundamentals_source))
