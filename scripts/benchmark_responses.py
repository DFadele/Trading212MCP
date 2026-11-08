"""Offline audit of all 12 tools on a synthetic, seeded 58-position portfolio.

Token estimates are ceil(UTF-8 bytes / 4), not a model-specific tokenizer.
MCP sizes include BOTH content text and structuredContent, as sent by FastMCP.
"""

import argparse
import asyncio
import json
import math
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from mcp.types import CallToolResult, ContentBlock  # noqa: E402
from mcp_results import call_structured_tool  # noqa: E402
from response_fixture import CALLS, RESEARCH, SEED, build_fixture  # noqa: E402


def sizes(content: list[ContentBlock], structured: dict[str, Any]) -> dict[str, int]:
    compact = json.dumps(
        structured, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    wire = CallToolResult(
        content=content, structuredContent=structured, isError=False
    ).model_dump_json(by_alias=True, exclude_none=True)
    n = len(compact.encode())
    w = len(wire.encode())
    return {
        "payload_bytes": n,
        "estimated_payload_tokens": math.ceil(n / 4),
        "mcp_bytes": w,
        "estimated_mcp_tokens": math.ceil(w / 4),
        "pretty_payload_lines": len(json.dumps(structured, indent=2).splitlines()),
    }


async def benchmark() -> dict[str, Any]:
    report: dict[str, Any] = {
        "fixture": {
            "holdings": 58,
            "history_sessions": 756,
            "seed": SEED,
            "data": "synthetic; no credentials/network/live holdings",
            "as_of": datetime.now(timezone.utc).date().isoformat(),
        },
        "token_estimator": "ceil(UTF-8 bytes / 4); approximation, not a tokenizer or billing count",
        "transport": "FastMCP content text plus structuredContent; excludes JSON-RPC framing",
        "tools": {},
    }
    with tempfile.TemporaryDirectory(
        prefix="trading212-response-benchmark-"
    ) as directory:
        server, _, _ = build_fixture(Path(directory) / "mappings.sqlite3")
        catalog = await server.list_tools()
        assert len(catalog) == 12
        for tool, args in CALLS.items():
            calls = {**args}
            if tool in RESEARCH:
                calls["response_mode"] = "full"
            start = time.perf_counter()
            content, full = await call_structured_tool(server, tool, calls)
            elapsed = (time.perf_counter() - start) * 1000
            entry: dict[str, Any] = {
                "input": args,
                "input_schema_bytes": len(
                    json.dumps(
                        next(t for t in catalog if t.name == tool).inputSchema,
                        separators=(",", ":"),
                    ).encode()
                ),
                "output_schema_bytes": len(
                    json.dumps(
                        next(t for t in catalog if t.name == tool).outputSchema,
                        separators=(",", ":"),
                    ).encode()
                ),
                "legacy_full": sizes(content, full),
                "full_call_latency_ms": round(elapsed, 3),
            }
            if tool in RESEARCH:
                start = time.perf_counter()
                content, summary = await call_structured_tool(
                    server, tool, {**args, "response_mode": "summary"}
                )
                entry["fresh_summary_call_latency_ms"] = round(
                    (time.perf_counter() - start) * 1000, 3
                )
                entry["summary"] = sizes(content, summary)
                report_id = summary["response"]["report_id"]
                timings: list[float] = []
                full_timings: list[float] = []
                serialized_timings: list[float] = []
                full_serialized_timings: list[float] = []
                for _ in range(7):
                    start = time.perf_counter()
                    c, r = await call_structured_tool(
                        server,
                        tool,
                        {**args, "response_mode": "summary", "report_id": report_id},
                    )
                    timings.append((time.perf_counter() - start) * 1000)
                    sizes(c, r)
                    serialized_timings.append((time.perf_counter() - start) * 1000)
                    start = time.perf_counter()
                    c, r = await call_structured_tool(
                        server,
                        tool,
                        {**args, "response_mode": "full", "report_id": report_id},
                    )
                    full_timings.append((time.perf_counter() - start) * 1000)
                    sizes(c, r)
                    full_serialized_timings.append((time.perf_counter() - start) * 1000)
                entry["cached_summary_call_median_ms"] = round(
                    statistics.median(timings), 3
                )
                entry["cached_full_call_median_ms"] = round(
                    statistics.median(full_timings), 3
                )
                entry["cached_summary_with_size_serialization_median_ms"] = round(
                    statistics.median(serialized_timings), 3
                )
                entry["cached_full_with_size_serialization_median_ms"] = round(
                    statistics.median(full_serialized_timings), 3
                )
                content, detailed = await call_structured_tool(
                    server,
                    tool,
                    {**args, "response_mode": "detailed", "report_id": report_id},
                )
                entry["detailed"] = sizes(content, detailed)
                content, cached_full = await call_structured_tool(
                    server,
                    tool,
                    {**args, "response_mode": "full", "report_id": report_id},
                )
                entry["cached_full"] = sizes(content, cached_full)
                entry["summary_mcp_reduction_percent"] = round(
                    100
                    * (
                        1
                        - entry["summary"]["mcp_bytes"]
                        / entry["legacy_full"]["mcp_bytes"]
                    ),
                    2,
                )
                if tool == "analyse_portfolio_risk":
                    content, drill = await call_structured_tool(
                        server,
                        tool,
                        {
                            **args,
                            "response_mode": "detailed",
                            "report_id": report_id,
                            "detail_symbols": ["STK00"],
                            "risk_components": ["correlations", "contributors"],
                        },
                    )
                    entry["single_holding_drill_down"] = sizes(content, drill)
            report["tools"][tool] = entry
            print(
                tool,
                entry.get(
                    "summary_mcp_reduction_percent", "unchanged brokerage contract"
                ),
                flush=True,
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/trading212-response-benchmark.json")
    )
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(asyncio.run(benchmark()), indent=2, allow_nan=False) + "\n"
    )
    print(f"Report: {args.output}")
