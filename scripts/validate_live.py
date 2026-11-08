"""Opt-in real-provider checks. Prints public data and aggregate coverage only.

Run from the repository root. Trading 212 access is GET-only; no account values,
identifiers, positions or credentials are written to the report.
"""

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp_results import call_structured_tool

from trading212_mcp.config import Settings
from trading212_mcp.market.config import MarketSettings
from trading212_mcp.market.factory import create_provider
from trading212_mcp.market.models import MarketError
from trading212_mcp.market.service import MarketService
from trading212_mcp.tools.research import register_research_tools
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error


async def validate(output: Path, portfolio: bool, risk_only: bool = False) -> None:
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = MarketSettings()
    settings.risk_timeout_seconds = 480
    settings.screen_max_candidates = 5
    provider = create_provider(settings)
    service = MarketService(provider, settings)
    trading = Trading212Client(Settings())  # pyright: ignore[reportCallIssue]
    server = FastMCP("Live research validation")
    register_research_tools(server, lambda: trading, lambda: service)
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider.name,
    }

    def save() -> None:
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")

    try:
        results: list[dict[str, Any]] = []
        for symbol in [] if risk_only else ["NVDA", "GOOGL", "APH", "ASML.AS", "SPY"]:
            started = time.monotonic()
            _, data = await call_structured_tool(
                server,
                "get_market_data",
                {
                    "tickers": [symbol],
                    "lookback_years": 3,
                    "include_fundamentals": True,
                    "include_series": True,
                    "base_currency": "GBP",
                    "response_mode": "full",
                },
            )
            asset = data["assets"][0]
            results.append(asset)
            print(
                json.dumps(
                    {
                        "symbol": symbol,
                        "observations": asset.get("observations"),
                        "latest_price_date": asset.get("end"),
                        "missing_data": asset["missing_data"],
                        "warnings": asset["warnings"],
                        "seconds": round(time.monotonic() - started, 2),
                    }
                ),
                flush=True,
            )
            report["market_data"] = results
            save()
        if not risk_only:
            try:
                universe = await provider.universe({"marketCapMoreThan": 1e11}, 105)
                report["discovery"] = universe.model_dump(mode="json")
                print(
                    json.dumps(
                        {
                            "discovery": {
                                "count": len(universe.companies),
                                "pages": universe.pages_fetched,
                                "truncated": universe.truncated,
                                "description": universe.description,
                            }
                        }
                    ),
                    flush=True,
                )
            except MarketError as exc:
                report["discovery"] = {"error": str(exc)}
            save()
            for exclude_owned in [False, True] if portfolio else [False]:
                try:
                    _, data = await call_structured_tool(
                        server,
                        "screen_stocks",
                        {
                            "min_market_cap": 1e11,
                            "exclude_owned": exclude_owned,
                            "factor_weights": {"growth": 0.5, "quality": 0.5},
                            "limit": 5,
                            "response_mode": "full",
                        },
                    )
                    report[f"screen_exclude_owned_{exclude_owned}"] = data
                    print(
                        json.dumps(
                            {
                                "screen_exclude_owned": exclude_owned,
                                "coverage": data["coverage"],
                                "candidates": len(data["candidates"]),
                            }
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    report[f"screen_exclude_owned_{exclude_owned}"] = {
                        "error_type": type(exc).__name__,
                        "error": str(exc)
                        if type(exc).__name__ == "ToolError"
                        else "Unexpected failure; see local diagnostics",
                    }
                save()
        if portfolio:
            started = time.monotonic()
            try:
                _, data = await call_structured_tool(
                    server,
                    "analyse_portfolio_risk",
                    {"lookback_years": 3, "response_mode": "full"},
                )
                quality = data["data_quality"]
                report["portfolio_risk"] = {
                    "seconds": round(time.monotonic() - started, 2),
                    "holdings_count": len(data["portfolio_summary"]["holdings"]),
                    "currency": data["portfolio_summary"]["currency"],
                    "included_value_fraction": quality["included_value_fraction"],
                    "exclusion_count": len(quality["excluded"]),
                    "exclusion_categories": sorted(
                        {
                            "ambiguous"
                            if "ambiguous" in x["reason"]
                            else "timeout"
                            if "budget" in x["reason"] or "timed out" in x["reason"]
                            else "identifier"
                            if "ISIN" in x["reason"]
                            else "provider_or_data"
                            for x in quality["excluded"]
                        }
                    ),
                    "risk_metrics_available": data["risk_metrics"] is not None,
                    "beta_available": data["benchmark_comparison"]["beta"] is not None,
                    "warning_count": len(quality["warnings"]),
                    "weights_sum_to_one": abs(
                        sum(h["weight"] for h in data["portfolio_summary"]["holdings"])
                        - 1
                    )
                    < 1e-9,
                    "contributions_sum_to_one": abs(
                        sum(
                            c["fraction_of_variance"] or 0
                            for c in data["asset_risk_contributions"]
                        )
                        - 1
                    )
                    < 1e-9
                    if data["risk_metrics"]
                    else None,
                }
                print(
                    json.dumps({"portfolio_risk": report["portfolio_risk"]}), flush=True
                )
            except (MarketError, Trading212Error) as exc:
                report["portfolio_risk"] = {"error": str(exc)}
            except Exception as exc:
                report["portfolio_risk"] = {
                    "error_type": type(exc).__name__,
                    "error": str(exc)
                    if type(exc).__name__ == "ToolError"
                    else "Unexpected failure; see local diagnostics",
                }
            save()
    finally:
        await provider.aclose()
        await trading.aclose()
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/trading212-live-validation.json")
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Read real Trading 212 positions and validate risk/ownership",
    )
    parser.add_argument("--risk-only", action="store_true")
    args = parser.parse_args()
    asyncio.run(validate(args.output, args.portfolio, args.risk_only))
