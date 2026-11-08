import copy
import json
from collections import Counter
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from mcp.server.fastmcp.exceptions import ToolError
from response_fixture import CALLS, RESEARCH, build_fixture

from trading212_mcp.market.models import MarketError
from trading212_mcp.responses import ResearchResponses, warning_groups


@pytest.fixture(scope="module")
def response_fixture(tmp_path_factory):
    return build_fixture(
        tmp_path_factory.mktemp("response-fixture") / "mappings.sqlite3"
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def risk58(response_fixture):
    _, service, trading = response_fixture
    result = await service.portfolio_risk(trading, 3, "SPY")
    assert len(result["portfolio_summary"]["holdings"]) == 58
    assert result["data_quality"]["included_value_fraction"] == 1
    return result


async def test_summary_is_same_calculation_full_and_snapshots_do_not_refetch(risk58):
    cache = ResearchResponses()
    compute = AsyncMock(return_value=risk58)
    original = copy.deepcopy(risk58)
    summary = await cache.respond("analyse_portfolio_risk", {}, compute)
    full = await cache.respond(
        "analyse_portfolio_risk",
        {},
        compute,
        response_mode="full",
        report_id=summary["response"]["report_id"],
    )
    assert compute.await_count == 1
    assert full == original
    assert risk58 == original
    assert summary["risk_metrics"] == full["risk_metrics"]
    assert summary["benchmark_comparison"] == full["benchmark_comparison"]
    assert summary["concentration_analysis"] == full["concentration_analysis"]
    assert summary["diversification_ratio"] == full["diversification_ratio"]
    assert "annualised_covariance" not in summary
    assert "highest_positive" in summary["correlations"]
    assert len(summary["portfolio_summary"]["holdings"]) == 5
    assert summary["response"]["counts"]["holdings"] == {
        "total": 58,
        "matching": 58,
        "returned": 5,
    }
    assert len(json.dumps(summary)) < len(json.dumps(full)) * 0.12


async def test_risk_contributors_are_top_absolute_and_residual_not_renormalised(risk58):
    cache = ResearchResponses()
    summary = await cache.respond(
        "analyse_portfolio_risk", {}, AsyncMock(return_value=risk58), top_n=3
    )
    expected = sorted(
        risk58["asset_risk_contributions"],
        key=lambda r: (-abs(r["annualised_variance_contribution"]), r["symbol"]),
    )[:3]
    actual = summary["asset_risk_contributions"]
    assert actual["largest_absolute_contributors"] == expected
    assert actual["shown_fraction_of_variance"] == pytest.approx(
        sum(r["fraction_of_variance"] for r in expected)
    )
    assert actual["shown_fraction_of_variance"] + actual[
        "remaining_fraction_of_variance"
    ] == pytest.approx(1)


async def test_correlations_are_unique_no_diagonals_and_retain_negative_hedges(risk58):
    raw = copy.deepcopy(risk58)
    raw["correlations"]["STK00"]["STK01"] = -0.95
    raw["correlations"]["STK01"]["STK00"] = -0.95
    summary = await ResearchResponses().respond(
        "analyse_portfolio_risk", {}, AsyncMock(return_value=raw)
    )
    corr = summary["correlations"]
    assert corr["most_negative"][0]["correlation"] == -0.95
    assert corr["most_negative"][0]["left"] == "STK00"
    pairs = corr["highest_positive"] + corr["most_negative"]
    assert all(p["left"] != p["right"] for p in pairs)
    assert len({(p["left"], p["right"]) for p in pairs}) == len(pairs)
    assert all(
        p["correlation"] == raw["correlations"][p["left"]][p["right"]] for p in pairs
    )
    assert summary["response"]["counts"]["correlation_pairs"]["total"] == 58 * 57 // 2


async def test_matrix_rows_can_be_drilled_down_from_same_snapshot(risk58):
    cache = ResearchResponses()
    compute = AsyncMock(return_value=risk58)
    summary = await cache.respond("analyse_portfolio_risk", {}, compute)
    detailed = await cache.respond(
        "analyse_portfolio_risk",
        {},
        compute,
        response_mode="detailed",
        detail_symbols=["STK00_US_EQ"],
        risk_components=["correlations", "covariance", "contributors"],
        report_id=summary["response"]["report_id"],
    )
    assert detailed["correlations"] == {"STK00": risk58["correlations"]["STK00"]}
    assert detailed["annualised_covariance"] == {
        "STK00": risk58["annualised_covariance"]["STK00"]
    }
    assert all(c["symbol"] == "STK00" for c in detailed["asset_risk_contributions"])
    assert "concentration_analysis" not in detailed
    assert detailed["risk_metrics"] == risk58["risk_metrics"]
    assert compute.await_count == 1


async def test_detailed_default_has_no_dense_matrices_and_full_restores_both(risk58):
    cache = ResearchResponses()
    compute = AsyncMock(return_value=risk58)
    detail = await cache.respond(
        "analyse_portfolio_risk", {}, compute, response_mode="detailed"
    )
    assert len(detail["portfolio_summary"]["holdings"]) == 58
    assert "correlations" not in detail and "annualised_covariance" not in detail
    full = await cache.respond(
        "analyse_portfolio_risk",
        {},
        compute,
        response_mode="full",
        report_id=detail["response"]["report_id"],
    )
    assert len(full["correlations"]) == 58 and len(full["annualised_covariance"]) == 58
    assert "response" not in full


async def test_sector_filters_never_recalculate_or_hide_coverage_failures(risk58):
    raw = copy.deepcopy(risk58)
    raw["data_quality"]["included_value_fraction"] = 0.7
    raw["data_quality"]["excluded"] = [
        {"trading212_ticker": "MISSING", "reason": "No verified ISIN"}
    ]
    raw["data_quality"]["warnings"].append(
        "Partial coverage: excluded assets can materially change risk."
    )
    result = await ResearchResponses().respond(
        "analyse_portfolio_risk",
        {},
        AsyncMock(return_value=raw),
        detail_sectors=["Technology"],
    )
    assert all(
        h["sector"] == "Technology" for h in result["portfolio_summary"]["holdings"]
    )
    assert result["risk_metrics"] == raw["risk_metrics"]
    assert result["data_quality"]["included_value_fraction"] == 0.7
    assert result["data_quality"]["excluded"] == raw["data_quality"]["excluded"]
    assert any(
        w["message"].startswith("Partial coverage")
        for w in result["data_quality"]["warnings"]
    )


def test_warning_grouping_preserves_all_messages_scopes_and_duplicate_counts():
    values = [
        "AAA: adjusted",
        "BBB: adjusted",
        "AAA: adjusted",
        "partial portfolio",
        "AAA: missing FX",
        {"code": "unfamiliar_warning"},
    ]
    groups = warning_groups(values, {"AAA", "BBB"})
    adjusted = next(g for g in groups if g["message"] == "adjusted")
    assert adjusted["symbols"] == ["AAA", "BBB"]
    assert adjusted["occurrences_by_symbol"] == {"AAA": 2, "BBB": 1}
    assert sum(g["occurrences"] for g in groups) == len(values)
    assert any(g.get("details") == {"code": "unfamiliar_warning"} for g in groups)
    reconstructed = Counter()
    for g in groups:
        if g["message"] is not None:
            for s, n in g["occurrences_by_symbol"].items():
                reconstructed[f"{s}: {g['message']}"] += n
            reconstructed[g["message"]] += g["unscoped_occurrences"]
    assert +reconstructed == Counter(v for v in values if isinstance(v, str))


async def test_report_is_tool_and_input_scoped_and_expired_reports_never_refresh(
    risk58,
):
    cache = ResearchResponses(ttl=60)
    compute = AsyncMock(return_value=risk58)
    first = await cache.respond("analyse_portfolio_risk", {"benchmark": "SPY"}, compute)
    report_id = first["response"]["report_id"]
    with pytest.raises(MarketError, match="inputs differ"):
        await cache.respond(
            "analyse_portfolio_risk", {"benchmark": "QQQ"}, compute, report_id=report_id
        )
    with pytest.raises(MarketError, match="another tool"):
        await cache.respond(
            "get_market_data", {"benchmark": "SPY"}, compute, report_id=report_id
        )
    cache.reports[report_id].created -= 61
    with pytest.raises(MarketError, match="expired"):
        await cache.respond(
            "analyse_portfolio_risk", {"benchmark": "SPY"}, compute, report_id=report_id
        )
    assert compute.await_count == 1
    assert not ResearchResponses().reports


async def test_report_eviction_is_bounded_and_unknown_id_does_not_compute(risk58):
    cache = ResearchResponses(max_entries=2)
    compute = AsyncMock(return_value=risk58)
    first = await cache.respond("analyse_portfolio_risk", {}, compute)
    await cache.respond("analyse_portfolio_risk", {}, compute)
    await cache.respond("analyse_portfolio_risk", {}, compute)
    assert len(cache.reports) == 2
    with pytest.raises(MarketError, match="unavailable"):
        await cache.respond(
            "analyse_portfolio_risk",
            {},
            compute,
            report_id=first["response"]["report_id"],
        )
    assert compute.await_count == 3


async def test_summary_covariance_request_rejected_before_fetch():
    compute = AsyncMock()
    with pytest.raises(MarketError, match="Covariance"):
        await ResearchResponses().respond(
            "analyse_portfolio_risk", {}, compute, risk_components=["covariance"]
        )
    compute.assert_not_called()


@pytest.mark.parametrize("tool", sorted(RESEARCH))
async def test_mcp_modes_and_cached_full_are_discoverable(response_fixture, tool):
    server, _, _ = response_fixture
    schema = next(t for t in await server.list_tools() if t.name == tool)
    assert schema.inputSchema["properties"]["response_mode"]["default"] == "summary"
    assert schema.inputSchema["properties"]["response_mode"]["enum"] == [
        "summary",
        "detailed",
        "full",
    ]
    _, summary = await server.call_tool(tool, CALLS[tool])
    assert summary["response"]["mode"] == "summary"
    _, full = await server.call_tool(
        tool,
        {
            **CALLS[tool],
            "response_mode": "full",
            "report_id": summary["response"]["report_id"],
        },
    )
    assert "response" not in full
    assert summary["source"] == full["source"]
    if tool == "analyse_candidate_investment":
        for s, f in zip(summary["candidates"], full["candidates"], strict=True):
            assert (
                s["analysis"]["post_investment_metrics"]
                == f["analysis"]["post_investment_metrics"]
            )
            assert (
                s["analysis"]["change_in_risk_metrics"]
                == f["analysis"]["change_in_risk_metrics"]
            )
    if tool == "optimise_new_contribution":
        assert summary["status"] == "optimal"
        assert (
            summary["recommended"]["contribution_allocations"]
            == full["recommended"]["contribution_allocations"]
        )
        assert summary["recommended"]["constraint_violations"] == []
        assert (
            summary["recommended"]["modelled_metrics"]["modelled_annualised_volatility"]
            == full["recommended"]["modelled_metrics"]["modelled_annualised_volatility"]
        )
    if tool == "screen_stocks":
        assert [c["composite_score"] for c in summary["candidates"]] == [
            c["composite_score"] for c in full["candidates"]
        ]
        assert summary["coverage"] == full["coverage"]
        assert (
            summary["candidates"][0]["metrics"]["provenance"]
            == full["candidates"][0]["metrics"]["provenance"]
        )
    if tool == "get_market_data":
        assert len(summary["assets"]) == len(full["assets"]) == 20
        assert (
            summary["assets"][0]["fundamentals"]["provenance"]
            == full["assets"][0]["fundamentals"]["provenance"]
        )
        assert (
            summary["assets"][0]["return_summary"]
            == full["assets"][0]["return_summary"]
        )


async def test_explicit_series_request_is_not_silently_ignored(response_fixture):
    server, _, _ = response_fixture
    _, summary = await server.call_tool(
        "get_market_data", {"tickers": ["STK00"], "include_series": True}
    )
    assert len(summary["assets"][0]["series"]) == 756
    assert summary["assets"][0]["series"][0]["return"] is None


async def test_filtering_market_rows_keeps_all_missing_data_diagnostics(
    response_fixture,
):
    server, _, _ = response_fixture
    _, summary = await server.call_tool(
        "get_market_data", {"tickers": ["STK00", "SPY"], "detail_symbols": ["STK00"]}
    )
    assert len(summary["assets"]) == 1
    assert any(
        i["symbol"] == "SPY" and "fundamentals" in i["missing_data"]
        for i in summary["record_diagnostics"]["issues"]
    )
    assert any(
        w["symbols"] == ["SPY"] for w in summary["record_diagnostics"]["warnings"]
    )


async def test_infeasible_constraints_and_failure_reasons_survive_summary(
    response_fixture,
):
    server, _, _ = response_fixture
    _, summary = await server.call_tool(
        "optimise_new_contribution",
        {
            "investment_amount_gbp": 2000,
            "tickers": ["NEW0"],
            "max_position_weight": 0.00001,
        },
    )
    assert summary["status"] == "infeasible"
    assert summary["recommended"] is None
    assert summary["unavoidable_position_breaches"]
    assert summary["feasibility_warnings"]


async def test_detail_candidate_weights_are_targeted_without_renormalisation(
    response_fixture,
):
    server, _, _ = response_fixture
    args = CALLS["analyse_candidate_investment"]
    _, summary = await server.call_tool("analyse_candidate_investment", args)
    _, full = await server.call_tool(
        "analyse_candidate_investment",
        {
            **args,
            "response_mode": "full",
            "report_id": summary["response"]["report_id"],
        },
    )
    _, detail = await server.call_tool(
        "analyse_candidate_investment",
        {
            **args,
            "response_mode": "detailed",
            "report_id": summary["response"]["report_id"],
            "detail_symbols": ["STK00"],
        },
    )
    assert detail["current_portfolio"]["weights"] == {
        "STK00": full["current_portfolio"]["weights"]["STK00"]
    }
    assert sum(detail["current_portfolio"]["weights"].values()) < 1
    assert (
        detail["current_portfolio"]["metrics"] == full["current_portfolio"]["metrics"]
    )


async def test_invalid_mode_is_an_mcp_validation_error(response_fixture):
    server, _, _ = response_fixture
    with pytest.raises(ToolError):
        await server.call_tool("analyse_portfolio_risk", {"response_mode": "tiny"})


async def test_screening_summary_retains_advanced_scores_and_reporting_metadata(
    response_fixture,
):
    server, _, _ = response_fixture
    args = {
        "factor_weights": {"growth": 0.4, "quality": 0.3, "momentum": 0.3},
        "benchmark": "SPY",
        "min_sector_peers": 5,
        "limit": 2,
    }
    _, summary = await server.call_tool("screen_stocks", args)
    _, full = await server.call_tool(
        "screen_stocks",
        {
            **args,
            "report_id": summary["response"]["report_id"],
            "response_mode": "full",
        },
    )
    assert len(await server.list_tools()) == 12
    assert summary["coverage"] == full["coverage"]
    for short, raw in zip(summary["candidates"], full["candidates"], strict=True):
        assert short["factor_scores"] == raw["factor_scores"]
        assert short["factor_explanations"]["momentum"]["observed_metric_count"]
        assert "metric_scores" not in short["factor_explanations"]["momentum"]
        assert short["metrics"]["momentum_6m"] == raw["metrics"]["momentum_6m"]
        assert "relative_strength" in short["metrics"]
        assert short["metric_metadata_groups"]
        assert short["inclusion_reasons"] == raw["inclusion_reasons"]


@pytest.mark.parametrize(
    "argument",
    [
        {"min_average_volume": -1},
        {"min_metric_coverage": 1.01},
        {"min_sector_peers": 1},
        {"benchmark": ""},
    ],
)
async def test_screening_new_input_bounds_are_validated(response_fixture, argument):
    server, _, _ = response_fixture
    with pytest.raises(ToolError):
        await server.call_tool("screen_stocks", argument)
