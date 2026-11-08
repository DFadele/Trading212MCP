"""Convex, buy-only contribution optimisation; no trade execution or return forecasts."""

import asyncio
import importlib
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.service import MarketService
from trading212_mcp.observability import timed_sync
from trading212_mcp.quantitative.candidates import (
    CASH,
    ResearchData,
    concentration,
    exposures,
    load_research_data,
    statistics,
)
from trading212_mcp.trading212.client import Trading212Client

Objective = Literal[
    "minimum_volatility", "risk_parity", "risk_budgeting", "maximum_diversification"
]
FloatArray = NDArray[np.float64]
TOL = 1e-7
METHODS = ("minimum_volatility", "risk_budgeting", "maximum_diversification")


@dataclass
class ContributionProblem:
    symbols: list[str]
    purchases: list[str]
    current: FloatArray
    amount: float
    mapping: FloatArray
    lower: FloatArray
    upper: FloatArray
    max_position_weight: float
    sector_rows: dict[str, FloatArray]
    sector_limits: dict[str, float]
    covariance: FloatArray
    sectors: dict[str, str]

    @property
    def total(self) -> float:
        return float(self.current.sum()) + self.amount

    @property
    def base(self) -> FloatArray:
        return self.current / self.total

    @property
    def funding_weight(self) -> float:
        return self.amount / self.total

    def weights(self, allocation: FloatArray) -> FloatArray:
        return self.base + self.funding_weight * (self.mapping @ allocation)

    def violations(self, allocation: FloatArray) -> list[dict[str, Any]]:
        """Independent arithmetic check, including bounds, funding and all positions."""
        if allocation.shape != self.lower.shape or not np.isfinite(allocation).all():
            return [{"constraint": "finite_allocation", "violation": 1.0}]
        checks: list[tuple[str, float]] = [
            ("purchase_budget", abs(float(allocation.sum()) - 1))
        ]
        for i, name in enumerate(self.purchases):
            checks.extend(
                [
                    (f"purchase_min:{name}", float(self.lower[i] - allocation[i])),
                    (f"purchase_max:{name}", float(allocation[i] - self.upper[i])),
                ]
            )
        w = self.weights(allocation)
        checks.append(("resulting_weight_budget", abs(float(w.sum()) - 1)))
        checks.extend(
            [
                (f"no_sale:{s}", float(self.base[i] - w[i]))
                for i, s in enumerate(self.symbols)
            ]
        )
        checks.extend(
            [
                (f"position_max:{s}", float(w[i] - self.max_position_weight))
                for i, s in enumerate(self.symbols)
                if s != CASH
            ]
        )
        checks.extend(
            [
                (f"sector_max:{s}", float(self.sector_rows[s] @ w) - limit)
                for s, limit in self.sector_limits.items()
            ]
        )
        return [{"constraint": name, "violation": v} for name, v in checks if v > TOL]

    def binding(self, allocation: FloatArray) -> list[dict[str, Any]]:
        w = self.weights(allocation)
        bindings: list[dict[str, Any]] = [
            {
                "constraint": "purchase_budget",
                "value": float(allocation.sum()),
                "bound": 1.0,
            }
        ]
        for i, name in enumerate(self.purchases):
            for label, bound in [
                ("purchase_min", self.lower[i]),
                ("purchase_max", self.upper[i]),
            ]:
                if abs(float(allocation[i] - bound)) <= TOL:
                    bindings.append(
                        {
                            "constraint": f"{label}:{name}",
                            "value": float(allocation[i]),
                            "bound": float(bound),
                        }
                    )
        for i, symbol in enumerate(self.symbols):
            if symbol != CASH and abs(float(w[i]) - self.max_position_weight) <= TOL:
                bindings.append(
                    {
                        "constraint": f"position_max:{symbol}",
                        "value": float(w[i]),
                        "bound": self.max_position_weight,
                    }
                )
        for sector, limit in self.sector_limits.items():
            value = float(self.sector_rows[sector] @ w)
            if abs(value - limit) <= TOL:
                bindings.append(
                    {
                        "constraint": f"sector_max:{sector}",
                        "value": value,
                        "bound": limit,
                    }
                )
        return bindings


def shrink_covariance(returns: pd.DataFrame) -> tuple[FloatArray, dict[str, Any]]:
    columns = [s for s in returns if s != CASH]
    if len(returns) < 2 or not columns:
        raise MarketError(
            "Insufficient common risky-asset returns for covariance estimation."
        )
    values: FloatArray = returns[columns].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise MarketError("Non-finite returns; optimisation covariance withheld.")
    estimator = importlib.import_module("sklearn.covariance").LedoitWolf().fit(values)
    covariance = np.asarray(estimator.covariance_, dtype=float) * 252
    covariance = (covariance + covariance.T) / 2
    if (
        covariance.shape != (len(columns), len(columns))
        or not np.isfinite(covariance).all()
    ):
        raise MarketError("Covariance estimator returned invalid results.")
    if float(np.max(np.diag(covariance))) <= 1e-16:
        raise MarketError("No usable historical return variation for optimisation.")
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    floor = max(float(np.trace(covariance)) / len(columns) * 1e-8, 1e-12)
    ridge = max(0.0, floor - float(eigenvalues.min()))
    if ridge:
        covariance = (eigenvectors * np.maximum(eigenvalues, floor)) @ eigenvectors.T
    result: FloatArray = np.zeros((len(returns.columns), len(returns.columns)))
    indices = [returns.columns.get_loc(s) for s in columns]
    for i, row in enumerate(indices):
        for j, col in enumerate(indices):
            result[row, col] = covariance[i, j]
    return result, {
        "estimator": "LedoitWolf",
        "annualisation_days": 252,
        "shrinkage": float(estimator.shrinkage_),
        "observations": len(returns),
        "risky_assets": len(columns),
        "eigenvalue_floor": floor,
        "floor_adjustment": ridge,
        "cash_variance": 0.0,
    }


def model_metrics(
    problem: ContributionProblem, weights: FloatArray, value: float
) -> dict[str, Any] | None:
    if value <= 0:
        return None
    cov_weight: FloatArray = problem.covariance @ weights
    variance = max(0.0, float(weights @ cov_weight))
    volatility = math.sqrt(variance)
    sigma: FloatArray = np.sqrt(np.maximum(np.diag(problem.covariance), 0))
    return {
        "modelled_annualised_volatility": volatility,
        "modelled_annualised_volatility_gbp": volatility * value,
        "diversification_ratio": float(sigma @ weights) / volatility
        if volatility > 1e-12
        else None,
        "risk_contributions": [
            {
                "symbol": s,
                "weight": float(weights[i]),
                "annualised_variance_contribution": float(weights[i] * cov_weight[i]),
                "fraction_of_variance": float(weights[i] * cov_weight[i] / variance)
                if variance > 1e-20
                else None,
            }
            for i, s in enumerate(problem.symbols)
            if weights[i] > 0
        ],
    }


def summarise_allocation(
    problem: ContributionProblem, allocation: FloatArray, returns: pd.DataFrame
) -> dict[str, Any]:
    weights = problem.weights(allocation)
    current_total = float(problem.current.sum())
    before_weights = (
        problem.current / current_total
        if current_total
        else np.zeros(len(problem.symbols))
    )
    before = model_metrics(problem, before_weights, current_total)
    after = model_metrics(problem, weights, problem.total)
    assert after is not None
    correlation = None
    if (
        before
        and before["modelled_annualised_volatility"] > 1e-12
        and after["modelled_annualised_volatility"] > 1e-12
    ):
        correlation = float(before_weights @ problem.covariance @ weights) / (
            before["modelled_annualised_volatility"]
            * after["modelled_annualised_volatility"]
        )
    values = {
        s: float(weights[i] * problem.total)
        for i, s in enumerate(problem.symbols)
        if weights[i] > 0
    }
    sample = statistics(returns, values, None, 0, 0.95, False)
    actual_weights = {
        s: float(weights[i]) for i, s in enumerate(problem.symbols) if weights[i] > 0
    }
    breaches = problem.violations(allocation)
    return {
        "contribution_allocations": {
            s: {
                "fraction": float(allocation[i]),
                "amount_gbp": float(allocation[i] * problem.amount),
            }
            for i, s in enumerate(problem.purchases)
        },
        "resulting_portfolio_value_gbp": problem.total,
        "resulting_portfolio_weights": actual_weights,
        "modelled_metrics": after,
        "change_in_modelled_volatility": after["modelled_annualised_volatility"]
        - before["modelled_annualised_volatility"]
        if before
        else None,
        "change_in_diversification_ratio": after["diversification_ratio"]
        - before["diversification_ratio"]
        if before
        and after["diversification_ratio"] is not None
        and before["diversification_ratio"] is not None
        else None,
        "modelled_correlation_with_current": correlation,
        "concentration": concentration(actual_weights),
        "sector_exposure": exposures(actual_weights, problem.sectors),
        "historical_sample_risk_metrics": sample["risk_metrics"] if sample else None,
        "constraint_compliant": not breaches,
        "constraint_violations": breaches,
        "binding_constraints": problem.binding(allocation),
    }


@timed_sync("quantitative_calculation")
def solve_contribution(
    problem: ContributionProblem,
    returns: pd.DataFrame,
    selected: str,
    risk_budgets: dict[str, float] | None = None,
) -> dict[str, Any]:
    cp = importlib.import_module("cvxpy")
    deadline = time.monotonic() + 20
    risky = np.array([i for i, s in enumerate(problem.symbols) if s != CASH], dtype=int)
    model_cov = cp.psd_wrap(problem.covariance)
    eig, vectors = np.linalg.eigh(problem.covariance)
    root = np.sqrt(np.maximum(eig, 0))[:, None] * vectors.T
    sigma = np.sqrt(np.maximum(np.diag(problem.covariance), 0))
    x = cp.Variable(len(problem.purchases), nonneg=True)

    def constraints(purchase: Any, scale: Any = 1.0) -> tuple[Any, list[Any]]:
        w = problem.base * scale + problem.funding_weight * (problem.mapping @ purchase)
        rules = [
            cp.sum(purchase) == scale,
            purchase >= problem.lower * scale,
            purchase <= problem.upper * scale,
            w[risky] <= problem.max_position_weight * scale,
        ]
        rules.extend(
            problem.sector_rows[s] @ w <= limit * scale
            for s, limit in problem.sector_limits.items()
        )
        return w, rules

    def run(program: Any) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "solver_timeout"
        try:
            program.solve(
                solver="CLARABEL",
                verbose=False,
                max_iter=300,
                time_limit=min(10.0, remaining),
                tol_gap_abs=1e-9,
                tol_gap_rel=1e-9,
                tol_feas=1e-9,
            )
        except cp.error.SolverError:
            return "solver_failed"
        return str(program.status)

    w, rules = constraints(x)
    feasible = cp.Problem(cp.Minimize(0), rules)
    status = run(feasible)
    if status != "optimal":
        return {
            "status": "infeasible" if status == "infeasible" else "solver_failed",
            "solver_status": status,
            "recommended": None,
            "method_comparisons": {},
        }
    methods: dict[str, Any] = {}
    # LP support checks prevent positive log budgets on assets forced to zero.
    possible = problem.base.copy()
    support_failed = False
    for i in risky:
        if problem.base[i] > 0:
            possible[i] = 1
        elif np.any(problem.mapping[i] > 0):
            support = cp.Problem(cp.Maximize(w[i]), rules)
            if run(support) != "optimal":
                support_failed = True
                break
            possible[i] = max(0.0, float(support.value))
    budgets = np.zeros(len(problem.symbols))
    if risk_budgets is None:
        active = [i for i in risky if possible[i] > TOL]
        if active:
            budgets[active] = 1 / len(active)
    else:
        for i, s in enumerate(problem.symbols):
            budgets[i] = risk_budgets.get(s, 0)
    bad_budget = any(budgets[i] > 0 and possible[i] <= TOL for i in range(len(budgets)))
    for method in (selected, *(m for m in METHODS if m != selected)):
        variable = x
        scale: Any = None
        if method == "minimum_volatility":
            program = cp.Problem(cp.Minimize(cp.quad_form(w, model_cov)), rules)
        else:
            if support_failed or not any(possible[i] > TOL for i in risky):
                methods[method] = {
                    "status": "solver_failed"
                    if support_failed
                    else "undefined_no_risky_exposure"
                }
                continue
            y = cp.Variable(len(problem.purchases), nonneg=True)
            scale = cp.Variable(nonneg=True)
            z, scaled_rules = constraints(y, scale)
            scaled_rules.append(scale >= 1e-8)
            variable = y
            if method == "maximum_diversification":
                program = cp.Problem(
                    cp.Maximize(sigma @ z), scaled_rules + [cp.norm(root @ z, 2) <= 1]
                )
            else:
                if bad_budget or not np.any(budgets > 0):
                    methods[method] = {"status": "infeasible_risk_budgets"}
                    continue
                active = np.flatnonzero(budgets > 0)
                program = cp.Problem(
                    cp.Minimize(
                        0.5 * cp.quad_form(z, model_cov)
                        - cp.sum(cp.multiply(budgets[active], cp.log(z[active])))
                    ),
                    scaled_rules,
                )
        status = run(program)
        if status != "optimal" or variable.value is None:
            methods[method] = {"status": "solver_failed", "solver_status": status}
            continue
        divisor = (
            float(scale.value) if scale is not None and scale.value is not None else 1.0
        )
        raw = (
            np.asarray(variable.value, dtype=float).reshape(-1) / divisor
            if divisor > 1e-10
            else np.full(len(problem.purchases), np.nan)
        )
        if (
            not np.isfinite(raw).all()
            or raw.shape != problem.lower.shape
            or np.any(raw < -TOL)
            or abs(float(raw.sum()) - 1) > TOL
        ):
            methods[method] = {"status": "invalid_solver_result"}
            continue
        allocation = np.maximum(raw, 0)
        allocation[problem.upper == 0] = 0
        if allocation.sum() <= 0:
            methods[method] = {"status": "invalid_solver_result"}
            continue
        allocation /= allocation.sum()
        if problem.violations(allocation):
            methods[method] = {
                "status": "constraint_validation_failed",
                "constraint_violations": problem.violations(allocation),
            }
            continue
        result = summarise_allocation(problem, allocation, returns)
        if (
            method == "maximum_diversification"
            and result["modelled_metrics"]["diversification_ratio"] is None
        ):
            methods[method] = {"status": "undefined_no_risky_exposure"}
            continue
        result.update({"status": "optimal", "solver_status": status})
        if method == "risk_budgeting":
            achieved = {
                c["symbol"]: c["fraction_of_variance"]
                for c in result["modelled_metrics"]["risk_contributions"]
            }
            residual = max(
                abs((achieved.get(s) or 0) - budgets[i])
                for i, s in enumerate(problem.symbols)
            )
            result["risk_budgeting"] = {
                "targets": {
                    s: float(budgets[i]) for i, s in enumerate(problem.symbols)
                },
                "maximum_absolute_budget_deviation": float(residual),
                "risk_budget_targets_achieved": bool(residual <= 1e-4),
                "exact_risk_parity_achieved": bool(residual <= 1e-4)
                if risk_budgets is None
                else None,
                "warning": "Buy-only and exposure constraints can prevent exact risk-budget equality; this is a constrained risk-budgeting solution."
                if residual > 1e-4
                else None,
            }
        methods[method] = result
    chosen = methods.get(selected, {"status": "solver_failed"})
    return {
        "status": chosen["status"],
        "recommended": chosen if chosen["status"] == "optimal" else None,
        "method_comparisons": methods,
        "solver": "CVXPY/CLARABEL",
        "constraint_tolerance": TOL,
    }


def coalesce_existing_isins(data: ResearchData) -> None:
    """Known cross-listings must share a position cap, not create extra headroom."""
    by_isin: dict[str, str] = {}
    aliases: dict[str, str] = {}
    values: dict[str, float] = {}
    for symbol, value in data.full_values.items():
        company = data.held[symbol].company
        canonical = (
            by_isin.setdefault(company.isin, symbol)
            if company and company.isin
            else symbol
        )
        aliases[symbol] = canonical
        values[canonical] = values.get(canonical, 0.0) + value
        if canonical != symbol:
            data.warnings.append(
                f"{symbol}: verified shared ISIN consolidated into {canonical} for position limits and risk."
            )
            left, right = (
                data.sectors.get(canonical, "Unknown"),
                data.sectors.get(symbol, "Unknown"),
            )
            if left == "Unknown":
                data.sectors[canonical] = right
            elif right != "Unknown" and left != right:
                data.sectors[canonical] = "Unknown"
    data.full_values = values
    data.analysis_symbols = {
        s: aliases.get(key, key) for s, key in data.analysis_symbols.items()
    }


def _validate_fraction_map(values: dict[str, float] | None, label: str) -> None:
    if values is not None and any(
        not key.strip()
        or isinstance(v, bool)
        or not math.isfinite(v)
        or not 0 <= v <= 1
        for key, v in values.items()
    ):
        raise MarketError(
            f"{label} must contain finite fractions between zero and one."
        )


async def optimise_contribution(
    service: MarketService,
    trading: Trading212Client,
    investment_amount_gbp: float,
    tickers: list[str],
    objective: Objective = "minimum_volatility",
    lookback_years: int = 3,
    max_position_weight: float = 1.0,
    sector_limits: dict[str, float] | None = None,
    min_purchase_allocations: dict[str, float] | None = None,
    max_purchase_allocations: dict[str, float] | None = None,
    allow_cash: bool = False,
    min_cash_allocation: float = 0.0,
    max_cash_allocation: float = 1.0,
    risk_budgets: dict[str, float] | None = None,
    listing_selections: dict[str, str] | None = None,
) -> dict[str, Any]:
    tickers = list(dict.fromkeys(tickers))
    if not tickers or len(tickers) > 10 or CASH in tickers:
        raise MarketError(
            "Supply one to ten distinct candidate listing symbols; the cash symbol is reserved."
        )
    if (
        isinstance(investment_amount_gbp, bool)
        or not math.isfinite(investment_amount_gbp)
        or investment_amount_gbp <= 0
    ):
        raise MarketError("Contribution must be a finite positive GBP amount.")
    if objective not in (*METHODS, "risk_parity"):
        raise MarketError(
            "Unsupported objective; no historical-mean maximum-Sharpe optimisation is offered."
        )
    if (
        not 1 <= lookback_years <= 10
        or not math.isfinite(max_position_weight)
        or not 0 < max_position_weight <= 1
    ):
        raise MarketError("Invalid lookback or maximum resulting position weight.")
    for values, label in [
        (sector_limits, "Sector limits"),
        (min_purchase_allocations, "Minimum purchases"),
        (max_purchase_allocations, "Maximum purchases"),
        (risk_budgets, "Risk budgets"),
    ]:
        _validate_fraction_map(values, label)
    for values in (min_purchase_allocations, max_purchase_allocations):
        if values and set(values) - set(tickers):
            raise MarketError(
                "Purchase allocation bounds must use requested candidate symbols."
            )
    if (
        not all(
            math.isfinite(v) and 0 <= v <= 1
            for v in (min_cash_allocation, max_cash_allocation)
        )
        or min_cash_allocation > max_cash_allocation
    ):
        raise MarketError("Cash bounds must be ordered fractions between zero and one.")
    if not allow_cash and min_cash_allocation > 0:
        raise MarketError("Positive minimum cash allocation requires allow_cash=true.")
    if risk_budgets is not None and (
        not risk_budgets
        or not math.isclose(sum(risk_budgets.values()), 1, rel_tol=0, abs_tol=1e-9)
    ):
        raise MarketError("Risk budgets must sum to one.")
    limits = {k.strip().casefold(): v for k, v in (sector_limits or {}).items()}
    if len(limits) != len(sector_limits or {}):
        raise MarketError("Sector limits contain duplicate case-insensitive names.")
    data = await load_research_data(
        service,
        trading,
        tickers,
        investment_amount_gbp,
        lookback_years,
        None,
        allow_cash,
        0,
        listing_selections,
        include_fundamentals=False,
    )
    quality: dict[str, Any] = {
        "included_holdings_value_fraction": sum(data.covered_values.values())
        / sum(data.full_values.values())
        if sum(data.full_values.values())
        else None,
        "excluded_holdings": data.excluded,
        "common_observations": len(data.returns),
        "excluded_candidates": [
            {"ticker": s, "reason": a.error or "No valid price history"}
            for s, a in data.assets.items()
            if a.prices is None
        ],
        "warnings": list(data.warnings),
        "positions_retrieved_at": data.positions_retrieved_at.isoformat(),
        "valuation_fx": data.valuation_fx,
        "holding_price_provenance": {s: a.provenance for s, a in data.held.items()},
        "candidate_price_provenance": {s: a.provenance for s, a in data.assets.items()},
        "period_used": {
            "start": data.returns.index[0].date().isoformat(),
            "end": data.returns.index[-1].date().isoformat(),
        }
        if not data.returns.empty
        else None,
    }
    missing_existing = set(data.full_values) - set(data.covered_values)
    coalesce_existing_isins(data)
    quality["warnings"] = data.warnings + [
        f"{s}: {w}" for s, a in data.assets.items() for w in a.warnings
    ]
    quality["holding_sectors"] = data.sectors
    quality["candidate_identities"] = {
        s: {
            "analysis_symbol": data.analysis_symbols.get(s),
            "isin": a.company.isin if a.company else None,
            "identifier_source": a.company.identifier_source if a.company else None,
        }
        for s, a in data.assets.items()
    }
    response: dict[str, Any] = {
        "status": "insufficient_data",
        "source": service.provider.name,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "objective": "risk_budgeting" if objective == "risk_parity" else objective,
        "currency": "GBP",
        "contribution_gbp": investment_amount_gbp,
        "current_portfolio": {
            "value_gbp": sum(data.full_values.values()),
            "weights": {
                s: v / sum(data.full_values.values())
                for s, v in data.full_values.items()
            }
            if data.full_values
            else {},
            "modelled_metrics": None,
        },
        "recommended": None,
        "method_comparisons": {},
        "comparisons": {},
        "data_quality": quality,
        "feasibility_warnings": [],
        "assumptions": {
            "funding": "New external GBP contribution; current positions are retained in full. Existing free, reserved and pie cash is excluded. No sales, leverage, shorting, fees, tax, spreads or FX charges. Fractional shares assumed; no orders placed.",
            "constraints": "Purchase bounds are fractions of NEW capital; position/sector limits are fractions of the RESULTING full portfolio. Position caps exclude cash, which has separate purchase bounds. Missing candidates may receive zero; no existing holdings may be omitted.",
            "identity": "Caps apply to resolved listing/security buckets; verified shared ISINs are coalesced, not issuer groups or ETF look-through. Unknown cross-listings are not guessed. Sector limits require known existing sectors and disable unclassified candidates.",
            "risk_model": "Annual Ledoit-Wolf covariance with disclosed numerical eigenvalue floor, common GBP split/dividend-adjusted daily histories. Shrunk model risk is separate from existing historical sample metrics; neither is a guaranteed outcome.",
            "objectives": "Minimum variance is convex QP; maximum diversification is a scaled SOCP maximising sum(asset volatility * weight)/portfolio volatility. Constrained risk budgeting minimises 0.5*z'Cz - sum(b_i*log(z_i)) over scaled buy-only portfolios; exact risk parity may be unattainable.",
            "cash": "Optional assumed GBP cash has zero nominal return and zero variance. Risk-budgeting and diversification objectives can have non-unique cash allocations; cash bounds express the user's preference, not forecast returns.",
            "returns": "No expected returns are inferred from historical means. Maximum Sharpe is intentionally not offered without a separate defensible forward-return model.",
            "precision": "Model GBP amounts are unrounded, not an executable penny/share order list. All bounds are independently rechecked after numerical cleanup, within disclosed fractional tolerance.",
            "limitations": "Current constituents create survivor/selection bias and daily closes across venues are asynchronous. Covariance shrinkage and in-sample optimality do not guarantee future diversification or prevent losses.",
            "comparisons": "Equal-weight and single-candidate/cash baselines are not projected onto constraints; any violations are explicitly marked. No infeasible comparison is a recommendation.",
        },
    }
    diluted_total = sum(data.full_values.values()) + investment_amount_gbp
    unavoidable = [
        {
            "symbol": s,
            "minimum_resulting_weight": value / diluted_total,
            "limit": max_position_weight,
            "necessary_contribution_lower_bound_gbp": max(
                0.0, value / max_position_weight - sum(data.full_values.values())
            ),
        }
        for s, value in data.full_values.items()
        if value / diluted_total > max_position_weight + TOL
    ]
    if unavoidable:
        response.update(
            {"status": "infeasible", "unavoidable_position_breaches": unavoidable}
        )
        response["feasibility_warnings"].append(
            "Existing positions exceed the cap even after adding the full contribution. No sales or relaxed caps are permitted."
        )
        return response
    if missing_existing or data.returns.empty:
        response["feasibility_warnings"].append(
            "Complete existing-holdings histories and a sufficient common sample are required; no covered-subset allocation is recommended."
        )
        return response
    known_sectors = {s: sector for s, sector in data.sectors.items()}
    if limits and any(
        known_sectors.get(s, "Unknown").casefold() == "unknown"
        for s in data.full_values
    ):
        response["feasibility_warnings"].append(
            "Existing holdings have unknown sectors/ETF look-through; requested sector limits cannot be certified."
        )
        return response
    symbols = list(data.full_values)
    for s, asset in data.assets.items():
        if asset.prices is not None and data.analysis_symbols[s] not in symbols:
            symbols.append(data.analysis_symbols[s])
    if allow_cash:
        symbols.append(CASH)
    if not any(data.assets[s].prices is not None for s in tickers) and not allow_cash:
        response["feasibility_warnings"].append(
            "No candidate has usable historical data."
        )
        return response
    lower = np.array(
        [(min_purchase_allocations or {}).get(s, 0.0) for s in tickers]
        + ([min_cash_allocation] if allow_cash else []),
        dtype=float,
    )
    upper = np.array(
        [(max_purchase_allocations or {}).get(s, 1.0) for s in tickers]
        + ([max_cash_allocation] if allow_cash else []),
        dtype=float,
    )
    purchases = tickers + ([CASH] if allow_cash else [])
    mapping: FloatArray = np.zeros((len(symbols), len(purchases)))
    for j, s in enumerate(tickers):
        asset = data.assets[s]
        if asset.prices is not None:
            mapping[symbols.index(data.analysis_symbols[s]), j] = 1
        if asset.prices is None or (
            limits
            and known_sectors.get(data.analysis_symbols.get(s, s), "Unknown").casefold()
            == "unknown"
        ):
            if lower[j] > 0:
                response["feasibility_warnings"].append(
                    f"{s}: required positive purchase lacks validated prices/sector data."
                )
                return response
            upper[j] = 0
            if asset.prices is not None:
                quality["excluded_candidates"].append(
                    {
                        "ticker": s,
                        "reason": "Sector classification unavailable; zero purchase required under sector limits.",
                    }
                )
            continue
    if allow_cash:
        mapping[symbols.index(CASH), -1] = 1
    if risk_budgets and (
        set(risk_budgets) - set(symbols) or risk_budgets.get(CASH, 0) > 0
    ):
        raise MarketError(
            "Risk budgets must use resolved security symbols; cash cannot have a positive risk budget."
        )
    current = np.array([data.full_values.get(s, 0.0) for s in symbols], dtype=float)
    total = float(current.sum()) + investment_amount_gbp
    sector_rows = {
        s: np.array(
            [
                float(
                    (
                        "cash"
                        if name == CASH
                        else known_sectors.get(name, "Unknown").strip().casefold()
                    )
                    == s
                )
                for name in symbols
            ]
        )
        for s in limits
    }
    if any(
        float(row @ current) / total > limits[s] + TOL for s, row in sector_rows.items()
    ):
        response["status"] = "infeasible"
        response["feasibility_warnings"].append(
            "Existing sector value exceeds its cap after dilution by the full new contribution; no buy-only solution exists."
        )
        return response
    for sector, row in sector_rows.items():
        if not row.any():
            response["feasibility_warnings"].append(
                f"Sector limit '{sector}' matches no observed provider sector; it constrains zero known exposure. Labels use exact case-insensitive names."
            )
    if np.any(lower > upper) or lower.sum() > 1 + TOL or upper.sum() < 1 - TOL:
        response["status"] = "infeasible"
        response["feasibility_warnings"].append(
            "Purchase/cash minima and maxima conflict or cannot allocate exactly the new contribution; no bounds were relaxed."
        )
        return response
    returns = data.returns[symbols]

    def compute() -> dict[str, Any]:
        covariance, metadata = shrink_covariance(returns)
        problem = ContributionProblem(
            symbols,
            purchases,
            current,
            investment_amount_gbp,
            mapping,
            lower,
            upper,
            max_position_weight,
            sector_rows,
            limits,
            covariance,
            known_sectors,
        )
        solved = solve_contribution(
            problem, returns, response["objective"], risk_budgets
        )
        solved["covariance_model"] = metadata
        current_total = float(current.sum())
        solved["current_portfolio"] = {
            "value_gbp": current_total,
            "weights": {
                s: float(current[i] / current_total)
                for i, s in enumerate(symbols)
                if current[i] > 0
            }
            if current_total
            else {},
            "modelled_metrics": model_metrics(
                problem, current / current_total, current_total
            )
            if current_total
            else None,
        }
        comparisons: dict[str, Any] = {}
        if all(data.assets[s].prices is not None for s in tickers):
            equal = np.zeros(len(purchases))
            equal[: len(tickers)] = 1 / len(tickers)
            comparisons["equal_weight"] = summarise_allocation(problem, equal, returns)
        else:
            comparisons["equal_weight"] = {"status": "insufficient_data"}
        comparisons["individual_candidates"] = {}
        for i, s in enumerate(tickers):
            if data.assets[s].prices is not None and mapping[:, i].sum() > 0:
                one = np.zeros(len(purchases))
                one[i] = 1
                comparisons["individual_candidates"][s] = summarise_allocation(
                    problem, one, returns
                )
            else:
                comparisons["individual_candidates"][s] = {
                    "status": "insufficient_data"
                }
        if allow_cash:
            cash = np.zeros(len(purchases))
            cash[-1] = 1
            comparisons["hold_cash"] = summarise_allocation(problem, cash, returns)
        solved["comparisons"] = comparisons
        return solved

    try:
        response.update(await asyncio.to_thread(compute))
    except (MarketError, np.linalg.LinAlgError):
        response["status"] = "invalid_covariance"
        response["feasibility_warnings"].append(
            "A valid robust covariance could not be estimated; no allocation returned."
        )
    if response["status"] != "optimal":
        response["feasibility_warnings"].append(
            "No validated recommendation for the requested objective; constraints were not relaxed and no alternative objective was silently substituted."
        )
    else:
        response["recommended"]["objective"] = response["objective"]
    return response
