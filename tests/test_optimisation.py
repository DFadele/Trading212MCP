import json
from unittest.mock import AsyncMock

import cvxpy as cp
import numpy as np
import pandas as pd
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from trading212_mcp.market.models import MarketError
from trading212_mcp.quantitative.candidates import CASH
from trading212_mcp.quantitative.optimisation import (
    ContributionProblem,
    optimise_contribution,
    shrink_covariance,
    solve_contribution,
)
from trading212_mcp.tools.research import register_research_tools


async def empty_portfolio(trading):
    snapshot = await trading.get_positions()
    snapshot.positions = []
    trading.get_positions = AsyncMock(return_value=snapshot)


async def test_buy_only_preserves_existing_values_and_allocates_only_contribution(
    service, trading
):
    result = await optimise_contribution(service, trading, 200, ["AAA", "BBB"])
    assert result["status"] == "optimal"
    rec = result["recommended"]
    assert rec["resulting_portfolio_value_gbp"] == 1000
    amounts = {s: p["amount_gbp"] for s, p in rec["contribution_allocations"].items()}
    assert sum(amounts.values()) == pytest.approx(200)
    assert all(v >= 0 for v in amounts.values())
    weights = rec["resulting_portfolio_weights"]
    assert weights["AAA"] * 1000 == pytest.approx(800 + amounts["AAA"])
    assert weights.get("BBB", 0) * 1000 == pytest.approx(amounts["BBB"])
    assert rec["constraint_compliant"] and not rec["constraint_violations"]
    assert set(result["method_comparisons"]) == {
        "minimum_volatility",
        "risk_budgeting",
        "maximum_diversification",
    }
    json.dumps(result, allow_nan=False)


async def test_concentrated_position_cannot_be_repaired_by_small_contribution(
    service, trading
):
    result = await optimise_contribution(
        service, trading, 200, ["BBB"], max_position_weight=0.5
    )
    assert result["status"] == "infeasible"
    assert result["recommended"] is None
    assert result["unavoidable_position_breaches"][0]["minimum_resulting_weight"] == 0.8


async def test_position_caps_apply_to_total_portfolio_not_purchase_weights(
    service, trading
):
    result = await optimise_contribution(
        service, trading, 200, ["AAA", "BBB"], max_position_weight=0.8
    )
    assert result["status"] == "optimal"
    rec = result["recommended"]
    assert rec["contribution_allocations"]["AAA"]["amount_gbp"] < 0.001
    assert rec["resulting_portfolio_weights"]["AAA"] == pytest.approx(0.8, abs=1e-7)
    assert any(
        x["constraint"] == "position_max:AAA" for x in rec["binding_constraints"]
    )
    assert not result["comparisons"]["equal_weight"]["constraint_compliant"]


async def test_sector_limits_after_funding(service, trading, provider):
    provider.companies["BBB"].sector = "Healthcare"
    result = await optimise_contribution(
        service, trading, 200, ["AAA", "BBB"], sector_limits={"technology": 0.8}
    )
    assert result["status"] == "optimal"
    assert result["recommended"]["sector_exposure"]["Technology"] <= 0.8 + 1e-7
    assert result["recommended"]["contribution_allocations"]["BBB"][
        "fraction"
    ] == pytest.approx(1, abs=1e-7)


async def test_unavoidable_sector_limit_is_infeasible(service, trading, provider):
    provider.companies["BBB"].sector = "Healthcare"
    result = await optimise_contribution(
        service, trading, 200, ["BBB"], sector_limits={"Technology": 0.5}
    )
    assert result["status"] == "infeasible"
    assert result["recommended"] is None


async def test_minimum_and_maximum_purchase_bounds(service, trading):
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        min_purchase_allocations={"AAA": 0.2},
        max_purchase_allocations={"BBB": 0.7},
    )
    assert result["status"] == "optimal"
    a = result["recommended"]["contribution_allocations"]
    assert a["AAA"]["fraction"] >= 0.3 - 1e-7
    assert a["BBB"]["fraction"] <= 0.7 + 1e-7
    bad = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        min_purchase_allocations={"AAA": 0.6, "BBB": 0.6},
    )
    assert bad["status"] == "infeasible" and bad["recommended"] is None


async def test_cash_is_optional_and_has_separate_limits(service, trading):
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA"],
        allow_cash=True,
        min_cash_allocation=0.2,
        max_cash_allocation=0.7,
    )
    assert result["status"] == "optimal"
    p = result["recommended"]["contribution_allocations"]
    assert p[CASH]["fraction"] == pytest.approx(0.7, abs=1e-6)
    assert p["AAA"]["fraction"] == pytest.approx(0.3, abs=1e-6)
    assert not result["comparisons"]["hold_cash"]["constraint_compliant"]


@pytest.mark.parametrize(
    "objective", ["minimum_volatility", "risk_parity", "maximum_diversification"]
)
async def test_all_objectives_respect_buy_only_and_caps(service, trading, objective):
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        objective=objective,
        max_position_weight=0.9,
    )
    assert result["status"] == "optimal"
    assert result["recommended"]["constraint_compliant"]
    assert all(
        w <= 0.9 + 1e-7
        for w in result["recommended"]["resulting_portfolio_weights"].values()
    )
    if objective == "risk_parity":
        assert not result["recommended"]["risk_budgeting"]["exact_risk_parity_achieved"]
        assert result["recommended"]["risk_budgeting"]["warning"]


async def test_unknown_existing_sector_blocks_sector_optimisation(
    service, trading, provider
):
    provider.companies["AAA"].sector = None
    result = await optimise_contribution(
        service, trading, 200, ["BBB"], sector_limits={"Technology": 0.9}
    )
    assert result["status"] == "insufficient_data" and result["recommended"] is None


async def test_missing_new_asset_can_receive_zero_but_existing_asset_cannot_be_omitted(
    service, trading
):
    result = await optimise_contribution(service, trading, 200, ["MISSING", "BBB"])
    assert result["status"] == "optimal"
    assert result["recommended"]["contribution_allocations"]["MISSING"]["fraction"] == 0
    assert result["data_quality"]["excluded_candidates"]
    snapshot = await trading.get_positions()
    snapshot.positions[0].instrument.isin = "UNKNOWN"
    trading.get_positions = AsyncMock(return_value=snapshot)
    blocked = await optimise_contribution(service, trading, 200, ["BBB"])
    assert blocked["status"] == "insufficient_data" and blocked["recommended"] is None


async def test_positive_purchase_requirement_cannot_be_filled_with_missing_asset(
    service, trading
):
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["MISSING", "BBB"],
        min_purchase_allocations={"MISSING": 0.1},
    )
    assert result["status"] == "insufficient_data" and result["recommended"] is None


@pytest.mark.parametrize("amount", [0, -1, float("inf"), float("nan"), True])
async def test_invalid_contribution_is_rejected_without_requests(service, amount):
    trading = AsyncMock()
    with pytest.raises(MarketError, match="positive"):
        await optimise_contribution(service, trading, amount, ["AAA"])
    trading.get_positions.assert_not_called()


async def test_solver_failure_never_returns_recommendation(
    service, trading, monkeypatch
):
    monkeypatch.setattr(
        cp.Problem,
        "solve",
        lambda *a, **k: (_ for _ in ()).throw(
            cp.error.SolverError("private raw solver details")
        ),
    )
    result = await optimise_contribution(service, trading, 200, ["BBB"])
    assert result["status"] == "solver_failed" and result["recommended"] is None
    assert "private" not in json.dumps(result)


async def test_zero_value_portfolio_and_risk_parity(service, trading):
    await empty_portfolio(trading)
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        objective="risk_parity",
        max_position_weight=0.8,
    )
    assert result["status"] == "optimal"
    assert result["current_portfolio"]["weights"] == {}
    assert result["recommended"]["risk_budgeting"]["exact_risk_parity_achieved"]
    assert result["recommended"]["resulting_portfolio_value_gbp"] == 200


async def test_optimizer_mcp_discovery_validation_and_serialization(service, trading):
    server = FastMCP("optimiser")
    register_research_tools(server, lambda: trading, lambda: service)
    tool = next(
        t for t in await server.list_tools() if t.name == "optimise_new_contribution"
    )
    assert tool.annotations.readOnlyHint
    assert "ctx" not in tool.inputSchema["properties"]
    _, result = await server.call_tool(
        "optimise_new_contribution",
        {
            "tickers": ["AAA", "BBB"],
            "investment_amount_gbp": 200,
            "response_mode": "full",
        },
    )
    assert result["status"] == "optimal"
    for args in [
        {"investment_amount_gbp": 0},
        {"max_position_weight": 1.1},
        {"max_purchase_allocations": {"AAA": True}},
        {"objective": "maximum_sharpe"},
    ]:
        with pytest.raises(ToolError):
            await server.call_tool(
                "optimise_new_contribution",
                {
                    "tickers": ["AAA"],
                    "investment_amount_gbp": 200,
                    **args,
                    "response_mode": "full",
                },
            )


def test_shrinkage_handles_singular_sample_covariance():
    a = np.sin(np.arange(100)) * 0.01
    frame = pd.DataFrame({"A": a, "B": a, CASH: np.zeros(100)})
    covariance, metadata = shrink_covariance(frame)
    assert np.isfinite(covariance).all()
    assert np.linalg.eigvalsh(covariance[:2, :2]).min() > 0
    assert metadata["shrinkage"] > 0
    assert not covariance[-1].any()


def diagonal_problem(covariance=None):
    cov = np.diag([0.04, 0.16]) if covariance is None else covariance
    returns = pd.DataFrame(
        {"A": np.sin(np.arange(100)) * 0.01, "B": np.cos(np.arange(100)) * 0.02}
    )
    return ContributionProblem(
        ["A", "B"],
        ["A", "B"],
        np.zeros(2),
        1000,
        np.eye(2),
        np.zeros(2),
        np.ones(2),
        1,
        {},
        {},
        cov,
        {},
    ), returns


def test_known_closed_form_min_variance_parity_and_diversification():
    problem, returns = diagonal_problem()
    result = solve_contribution(problem, returns, "minimum_volatility")
    assert result["status"] == "optimal"
    methods = result["method_comparisons"]
    assert methods["minimum_volatility"]["contribution_allocations"]["A"][
        "fraction"
    ] == pytest.approx(0.8, abs=1e-5)
    assert methods["risk_budgeting"]["contribution_allocations"]["A"][
        "fraction"
    ] == pytest.approx(2 / 3, abs=1e-5)
    assert methods["maximum_diversification"]["contribution_allocations"]["A"][
        "fraction"
    ] == pytest.approx(2 / 3, abs=1e-5)
    assert (
        methods["risk_budgeting"]["risk_budgeting"]["maximum_absolute_budget_deviation"]
        < 1e-4
    )


def test_independent_validation_rejects_sell_overbudget_and_cap_breaches():
    problem, _ = diagonal_problem()
    assert problem.violations(np.array([-0.1, 1.1]))
    assert problem.violations(np.array([0.3, 0.3]))
    assert problem.violations(np.array([np.nan, np.nan]))
    problem.max_position_weight = 0.6
    assert problem.violations(np.array([0.8, 0.2]))


def test_whole_portfolio_minimum_variance_differs_from_candidate_only():
    problem, returns = diagonal_problem()
    problem.current = np.array([900.0, 0.0])
    problem.amount = 100
    result = solve_contribution(problem, returns, "minimum_volatility")
    rec = result["recommended"]
    assert rec["contribution_allocations"]["B"]["fraction"] == pytest.approx(
        1, abs=1e-6
    )
    assert rec["resulting_portfolio_weights"]["A"] == pytest.approx(0.9, abs=1e-6)


def test_custom_risk_budgets_known_diagonal_solution():
    problem, returns = diagonal_problem()
    result = solve_contribution(
        problem, returns, "risk_budgeting", {"A": 0.25, "B": 0.75}
    )
    expected = (0.5 / 0.2) / (0.5 / 0.2 + np.sqrt(0.75) / 0.4)
    assert result["recommended"]["contribution_allocations"]["A"][
        "fraction"
    ] == pytest.approx(expected, abs=1e-5)
    assert result["recommended"]["risk_budgeting"]["risk_budget_targets_achieved"]
    assert result["recommended"]["risk_budgeting"]["exact_risk_parity_achieved"] is None


async def test_constrained_zero_candidate_does_not_break_log_budgeting(
    service, trading, provider
):
    provider.companies["BBB"].sector = "Healthcare"
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        objective="risk_parity",
        sector_limits={"Healthcare": 0},
    )
    assert result["status"] == "optimal"
    assert result["recommended"]["contribution_allocations"]["BBB"]["fraction"] < 1e-7
    assert result["recommended"]["risk_budgeting"]["targets"]["BBB"] == 0


async def test_impossible_positive_risk_budget_is_not_silently_replaced(
    service, trading, provider
):
    provider.companies["BBB"].sector = "Healthcare"
    result = await optimise_contribution(
        service,
        trading,
        200,
        ["AAA", "BBB"],
        objective="risk_parity",
        sector_limits={"Healthcare": 0},
        risk_budgets={"AAA": 0.5, "BBB": 0.5},
    )
    assert result["status"] == "infeasible_risk_budgets"
    assert result["recommended"] is None
    assert result["method_comparisons"]["minimum_volatility"]["status"] == "optimal"


async def test_same_isin_existing_listings_cannot_bypass_position_cap(
    service, trading, provider
):
    snapshot = await trading.get_positions()
    alias = snapshot.positions[0].model_copy(deep=True)
    alias.instrument.ticker = "AAA_L_EQ"
    alias.instrument.currency = "GBP"
    alias.walletImpact.currentValue = 1000
    snapshot.positions.append(alias)
    trading.get_positions = AsyncMock(return_value=snapshot)
    provider.companies["AAA.L"] = provider.companies["AAA"].model_copy(
        update={"symbol": "AAA.L", "currency": "GBP"}
    )
    provider.series["AAA.L"] = provider.series["AAA"] * 0.8
    result = await optimise_contribution(
        service, trading, 200, ["BBB"], max_position_weight=0.6
    )
    assert result["status"] == "infeasible"
    assert result["unavoidable_position_breaches"][0][
        "minimum_resulting_weight"
    ] == pytest.approx(1600 / 1800)


async def test_unknown_new_sector_is_zero_and_comparison_is_marked_infeasible(
    service, trading, provider
):
    provider.companies["BBB"].sector = None
    result = await optimise_contribution(
        service, trading, 200, ["AAA", "BBB"], sector_limits={"Technology": 1}
    )
    assert result["status"] == "optimal"
    assert result["recommended"]["contribution_allocations"]["BBB"]["fraction"] < 1e-7
    comparison = result["comparisons"]["individual_candidates"]["BBB"]
    assert sum(comparison["resulting_portfolio_weights"].values()) == pytest.approx(1)
    assert not comparison["constraint_compliant"]


async def test_solver_output_is_independently_checked_after_success_status(
    service, trading, monkeypatch
):
    original = cp.Problem.solve

    def broken(program, *args, **kwargs):
        result = original(program, *args, **kwargs)
        expr = program.objective.expr
        if expr.is_quadratic() and not expr.is_affine():
            for variable in program.variables():
                if variable.shape == (2,):
                    variable._value = np.array([0.2, 0.2])
        return result

    monkeypatch.setattr(cp.Problem, "solve", broken)
    result = await optimise_contribution(service, trading, 200, ["AAA", "BBB"])
    assert result["status"] == "invalid_solver_result"
    assert result["recommended"] is None


async def test_zero_variation_covariance_is_not_presented_as_a_reliable_allocation(
    service, trading, provider
):
    provider.series["AAA"] = np.ones(300) * 100
    provider.series["BBB"] = np.ones(300) * 200
    result = await optimise_contribution(service, trading, 200, ["AAA", "BBB"])
    assert result["status"] == "invalid_covariance" and result["recommended"] is None


async def test_pence_and_account_currency_conversion(service, trading, provider):
    provider.companies["GBPSTOCK.L"] = provider.companies["BBB"].model_copy(
        update={"symbol": "GBPSTOCK.L", "currency": "GBp", "isin": "ISIN_C"}
    )
    provider.series["GBPSTOCK.L"] = provider.series["BBB"] * 100
    result = await optimise_contribution(service, trading, 200, ["GBPSTOCK.L"])
    assert result["status"] == "optimal"
    assert result["current_portfolio"]["value_gbp"] == 800
    assert result["recommended"]["resulting_portfolio_weights"] == pytest.approx(
        {"AAA": 0.8, "GBPSTOCK.L": 0.2}
    )
    assert (
        result["data_quality"]["candidate_price_provenance"]["GBPSTOCK.L"][
            "original_currency"
        ]
        == "GBp"
    )


def test_fractional_constraint_tolerance_is_explicit_and_small():
    problem, _ = diagonal_problem()
    problem.max_position_weight = 0.6
    assert not problem.violations(np.array([0.60000001, 0.39999999]))
    assert problem.violations(np.array([0.6000002, 0.3999998]))


def test_min_variance_and_maximum_diversification_match_independent_grid():
    covariance = np.array([[0.04, -0.01], [-0.01, 0.09]])
    problem, returns = diagonal_problem(covariance)
    problem.current = np.array([40.0, 60.0])
    problem.amount = 100
    result = solve_contribution(problem, returns, "maximum_diversification")
    grid = np.column_stack([np.linspace(0, 1, 10001), 1 - np.linspace(0, 1, 10001)])
    weights = problem.base + problem.funding_weight * grid
    variance = np.einsum("ij,jk,ik->i", weights, covariance, weights)
    ratios = (weights @ np.sqrt(np.diag(covariance))) / np.sqrt(variance)
    methods = result["method_comparisons"]
    assert (
        methods["maximum_diversification"]["modelled_metrics"]["diversification_ratio"]
        >= max(ratios) - 1e-6
    )
    assert (
        methods["minimum_volatility"]["modelled_metrics"][
            "modelled_annualised_volatility"
        ]
        <= np.sqrt(min(variance)) + 1e-6
    )


async def test_reported_model_risk_contributions_are_consistent(service, trading):
    result = await optimise_contribution(service, trading, 200, ["AAA", "BBB"])
    risk = result["recommended"]["modelled_metrics"]
    variance = risk["modelled_annualised_volatility"] ** 2
    assert sum(
        c["annualised_variance_contribution"] for c in risk["risk_contributions"]
    ) == pytest.approx(variance)
    assert sum(
        c["fraction_of_variance"] for c in risk["risk_contributions"]
    ) == pytest.approx(1)
    assert risk["modelled_annualised_volatility_gbp"] == pytest.approx(
        risk["modelled_annualised_volatility"] * 1000
    )


async def test_unknown_sector_label_is_not_silently_treated_as_technology(
    service, trading
):
    result = await optimise_contribution(
        service, trading, 200, ["BBB"], sector_limits={"Tech": 0.1}
    )
    assert result["status"] == "optimal"
    assert any("matches no observed" in w for w in result["feasibility_warnings"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_position_weight": 0},
        {"min_cash_allocation": 0.3},
        {"risk_budgets": {"AAA": 0.7}},
        {"sector_limits": {"Technology": float("nan")}},
        {"min_purchase_allocations": {"OTHER": 0.1}},
        {"sector_limits": {"Technology": 0.5, " technology ": 0.5}},
    ],
)
async def test_invalid_constraints_rejected_before_fetching(service, kwargs):
    trading = AsyncMock()
    with pytest.raises(MarketError):
        await optimise_contribution(service, trading, 200, ["AAA"], **kwargs)
    trading.get_positions.assert_not_called()


async def test_full_zero_purchase_limit_is_exactly_zero(service, trading):
    result = await optimise_contribution(
        service, trading, 200, ["AAA", "BBB"], max_purchase_allocations={"AAA": 0}
    )
    assert result["status"] == "optimal"
    assert result["recommended"]["contribution_allocations"]["AAA"]["amount_gbp"] == 0
    assert result["recommended"]["contribution_allocations"]["BBB"][
        "amount_gbp"
    ] == pytest.approx(200)


async def test_all_cash_constraint_keeps_minimum_volatility_defined(service, trading):
    await empty_portfolio(trading)
    result = await optimise_contribution(
        service, trading, 200, ["AAA"], allow_cash=True, min_cash_allocation=1
    )
    assert result["status"] == "optimal"
    rec = result["recommended"]
    assert rec["contribution_allocations"][CASH]["fraction"] == pytest.approx(
        1, abs=1e-7
    )
    assert rec["modelled_metrics"]["modelled_annualised_volatility"] == pytest.approx(
        0, abs=1e-7
    )
    assert (
        result["method_comparisons"]["risk_budgeting"]["status"]
        == "undefined_no_risky_exposure"
    )
    assert (
        result["method_comparisons"]["maximum_diversification"]["status"]
        == "undefined_no_risky_exposure"
    )


async def test_inaccurate_solver_status_is_not_promoted_to_success(
    service, trading, monkeypatch
):
    def inaccurate(program, *args, **kwargs):
        program._status = cp.OPTIMAL_INACCURATE

    monkeypatch.setattr(cp.Problem, "solve", inaccurate)
    result = await optimise_contribution(service, trading, 200, ["BBB"])
    assert result["status"] == "solver_failed"
    assert result["recommended"] is None
