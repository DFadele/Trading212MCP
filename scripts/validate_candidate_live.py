"""Read-only real-portfolio MCP smoke check; prints only aggregate checks.

Run from the repository root with configured Trading 212 credentials and SEC
contact. Uses Yahoo/SEC explicitly. Investment scenarios never execute trades.
"""

import asyncio
import json
import math
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def validate() -> dict[str, Any]:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "trading212_mcp"],
        cwd=str(Path.cwd()),
        env={
            **os.environ,
            "MARKET_PROVIDER": "yfinance",
            "MARKET_FUNDAMENTALS_SOURCE": "sec",
            "MARKET_RISK_TIMEOUT_SECONDS": "180",
        },
    )
    arguments = {
        "response_mode": "full",
        "tickers": ["NVDA", "APH", "GOOGL", "SPY"],
        "investment_amount_gbp": 2000,
        "compare_cash": True,
        "allocations": {"NVDA": 0.5, "APH": 0.5},
        "use_historical_mean_returns": True,
    }
    started = time.monotonic()
    with open(os.devnull, "w") as errlog:
        async with stdio_client(parameters, errlog=errlog) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=300)
            ) as session:
                await session.initialize()
                catalog = await session.list_tools()
                assert len(catalog.tools) == 12
                assert all(
                    t.annotations and t.annotations.readOnlyHint for t in catalog.tools
                )
                result = await session.call_tool(
                    "analyse_candidate_investment",
                    arguments,
                    read_timeout_seconds=timedelta(seconds=300),
                )
                assert not result.isError, (
                    "Candidate tool returned an error; raw account data omitted"
                )
                data = result.structuredContent
                assert data and data["currency"] == "GBP"
                assert len(data["candidates"]) == 4
                assert all(
                    c["analysis"] and c["analysis"]["post_investment_metrics"]
                    for c in data["candidates"]
                ), (
                    "Some scenarios lack sufficient real common history; not counted as a successful live check"
                )
                assert data["allocated_scenario"] and data["cash_comparison"]
                assert data["data_quality"]["common_observations"] >= 60
                current = data["current_portfolio"]
                if current["weights"]:
                    assert math.isclose(
                        sum(current["weights"].values()), 1, abs_tol=1e-9
                    )
                for scenario in [c["analysis"] for c in data["candidates"]] + [
                    data["allocated_scenario"],
                    data["cash_comparison"],
                ]:
                    assert math.isclose(
                        sum(scenario["post_investment_weights"].values()),
                        1,
                        abs_tol=1e-9,
                    )
                    assert math.isclose(
                        sum(scenario["analysed_weights"].values()), 1, abs_tol=1e-9
                    )
                    assert math.isclose(
                        scenario["post_investment_holdings_value_gbp"],
                        current["holdings_value_gbp"] + 2000,
                        rel_tol=1e-12,
                        abs_tol=1e-7,
                    )
                    assert (
                        scenario["post_investment_metrics"]["risk_metrics"][
                            "observations"
                        ]
                        == data["data_quality"]["common_observations"]
                    )
                return {
                    "transport": "python_mcp_stdio",
                    "seconds": round(time.monotonic() - started, 2),
                    "tools_discovered": len(catalog.tools),
                    "candidates": [
                        {
                            "ticker": c["ticker"],
                            "status": c["status"],
                            "fundamentals_source": c["fundamentals"]["source"]
                            if c["fundamentals"]
                            else None,
                            "beta_available": c["analysis"]["post_investment_metrics"][
                                "beta"
                            ]
                            is not None,
                        }
                        for c in data["candidates"]
                    ],
                    "covered_holdings_value_fraction": data["data_quality"][
                        "included_holdings_value_fraction"
                    ],
                    "excluded_holdings_count": len(
                        data["data_quality"]["excluded_holdings"]
                    ),
                    "common_observations": data["data_quality"]["common_observations"],
                    "comparison_scope": data["comparison"]["scope"],
                    "allocation_validated": True,
                    "cash_validated": True,
                    "post_purchase_values_and_weights_validated": True,
                }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(validate()), allow_nan=False), flush=True)
