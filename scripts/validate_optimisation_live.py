"""Read-only real-account optimisation/guard check. Prints aggregates only.

An insufficient-data result validates the guard, NOT a live optimal allocation.
Resolve held-instrument mappings and increase the fetch budget for a full solve.
"""

import argparse
import asyncio
import json
import math
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def validate(fetch_seconds: int) -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trading212_mcp"],
        cwd=str(Path.cwd()),
        env={
            **os.environ,
            "MARKET_PROVIDER": "yfinance",
            "MARKET_FUNDAMENTALS_SOURCE": "provider",
            "MARKET_RISK_TIMEOUT_SECONDS": str(fetch_seconds),
        },
    )
    report: dict[str, Any] = {
        "transport": "python_mcp_stdio",
        "fetch_budget_seconds": fetch_seconds,
    }
    with open(os.devnull, "w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (reader, writer):
            async with ClientSession(
                reader,
                writer,
                read_timeout_seconds=timedelta(seconds=fetch_seconds + 90),
            ) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert len(catalog.tools) == 12
                assert all(
                    t.annotations and t.annotations.readOnlyHint for t in catalog.tools
                )
                report["tools_discovered"] = len(catalog.tools)
                arguments: dict[str, Any] = {
                    "response_mode": "full",
                    "investment_amount_gbp": 2000,
                    "tickers": ["NVDA", "APH", "GOOGL"],
                }
                for name, caps in [
                    ("requested_optimisation", {}),
                    ("intentionally_infeasible_cap", {"max_position_weight": 0.000001}),
                ]:
                    result = await session.call_tool(
                        "optimise_new_contribution",
                        {**arguments, **caps, "response_mode": "full"},
                        read_timeout_seconds=timedelta(seconds=fetch_seconds + 90),
                    )
                    assert not result.isError, (
                        "Live tool request failed; account data omitted"
                    )
                    data = result.structuredContent
                    assert data
                    if name == "intentionally_infeasible_cap":
                        assert (
                            data["status"] == "infeasible"
                            and data["recommended"] is None
                        )
                    elif data["status"] == "optimal":
                        allocation = data["recommended"]
                        assert (
                            allocation["constraint_compliant"]
                            and not allocation["constraint_violations"]
                        )
                        assert math.isclose(
                            sum(
                                a["amount_gbp"]
                                for a in allocation["contribution_allocations"].values()
                            ),
                            2000,
                            abs_tol=1e-5,
                        )
                        assert math.isclose(
                            sum(allocation["resulting_portfolio_weights"].values()),
                            1,
                            abs_tol=1e-7,
                        )
                        assert not data["data_quality"]["excluded_holdings"]
                    else:
                        assert data["status"] in {"insufficient_data", "infeasible"}
                        assert data["recommended"] is None
                    report[name] = {
                        "status": data["status"],
                        "allocation_returned": data["recommended"] is not None,
                        "covered_holdings_value_fraction": data["data_quality"][
                            "included_holdings_value_fraction"
                        ],
                        "excluded_holdings_count": len(
                            data["data_quality"]["excluded_holdings"]
                        ),
                        "common_observations": data["data_quality"][
                            "common_observations"
                        ],
                    }
                    print(json.dumps({name: report[name]}, allow_nan=False), flush=True)
                report["live_optimal_allocation_verified"] = (
                    report["requested_optimisation"]["status"] == "optimal"
                )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fetch-budget", type=int, default=50, choices=range(1, 601), metavar="1..600"
    )
    args = parser.parse_args()
    print(
        json.dumps(asyncio.run(validate(args.fetch_budget)), allow_nan=False),
        flush=True,
    )
