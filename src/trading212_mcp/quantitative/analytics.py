"""Deterministic daily-return statistics. All ratios and weights are fractions."""

import math

import numpy as np
import pandas as pd
from numpy.typing import NDArray

from trading212_mcp.observability import timed_sync

TRADING_DAYS = 252


def finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


@timed_sync("quantitative_calculation")
def risk_metrics(
    returns: pd.Series, annual_risk_free_rate: float = 0.0, confidence: float = 0.95
) -> dict[str, object]:
    values: NDArray[np.float64] = returns.to_numpy(dtype=float)
    if len(values) < 2 or not np.isfinite(values).all() or (values <= -1).any():
        raise ValueError("Need at least two finite simple returns greater than -1.")
    if not -1 < annual_risk_free_rate or not 0.5 < confidence < 1:
        raise ValueError("Invalid risk-free rate or confidence level.")
    daily_rf = (1 + annual_risk_free_rate) ** (1 / TRADING_DAYS) - 1
    excess: NDArray[np.float64] = values - daily_rf
    daily_vol = float(np.std(values, ddof=1))
    downside = float(np.sqrt(np.mean(np.minimum(excess, 0.0) ** 2)))
    wealth = np.concatenate(([1.0], np.cumprod(1 + values)))
    drawdown = wealth / np.maximum.accumulate(wealth) - 1
    position = (1 - confidence) * (len(values) - 1)
    boundary = round(position)
    # Decimal confidence levels can land one ulp below an exact order statistic,
    # excluding the boundary loss from CVaR even though it equals the intended VaR.
    threshold = (
        float(np.partition(values, boundary)[boundary])
        if math.isclose(position, boundary, rel_tol=0, abs_tol=1e-12)
        else float(np.quantile(values, 1 - confidence, method="linear"))
    )
    tail = values[values <= threshold]
    return {
        "annualised_volatility": daily_vol * math.sqrt(TRADING_DAYS),
        "sharpe_ratio": finite(
            float(excess.mean()) / daily_vol * math.sqrt(TRADING_DAYS)
        )
        if daily_vol > 1e-15
        else None,
        "sortino_ratio": finite(
            float(excess.mean()) / downside * math.sqrt(TRADING_DAYS)
        )
        if downside > 1e-15
        else None,
        "maximum_drawdown": float(drawdown.min()),
        "historical_var": max(0.0, -threshold),
        "historical_cvar": max(0.0, -float(tail.mean())),
        "confidence": confidence,
        "tail_observations": len(tail),
        "observations": len(values),
    }


@timed_sync("quantitative_calculation")
def portfolio_statistics(
    asset_returns: pd.DataFrame,
    weights: dict[str, float],
    benchmark: pd.Series | None = None,
    annual_risk_free_rate: float = 0.0,
    confidence: float = 0.95,
) -> dict[str, object]:
    if asset_returns.empty or asset_returns.isna().any().any():
        raise ValueError("Asset returns must be aligned and complete.")
    w = np.array([weights[column] for column in asset_returns.columns], dtype=float)
    if not np.isfinite(w).all() or (w < 0).any() or not np.isclose(w.sum(), 1.0):
        raise ValueError("Weights must be finite, non-negative, and sum to one.")
    covariance = asset_returns.cov(ddof=1) * TRADING_DAYS
    cov = covariance.to_numpy(dtype=float)
    variance = float(w @ cov @ w)
    contributions = w * (cov @ w)
    portfolio_returns = pd.Series(
        asset_returns.to_numpy(dtype=float) @ w,
        index=asset_returns.index,
    )
    metrics = risk_metrics(portfolio_returns, annual_risk_free_rate, confidence)
    correlations = asset_returns.corr()
    beta = None
    benchmark_metrics = None
    benchmark_observations = 0
    if benchmark is not None:
        aligned = pd.concat(
            [portfolio_returns.rename("portfolio"), benchmark.rename("benchmark")],
            axis=1,
            sort=False,
        ).dropna()
        benchmark_observations = len(aligned)
        if len(aligned) >= 2:
            benchmark_variance = float(aligned["benchmark"].var(ddof=1))
            if benchmark_variance > 1e-20:
                beta = finite(
                    float(
                        np.cov(aligned.to_numpy(dtype=float), rowvar=False, ddof=1)[
                            0, 1
                        ]
                    )
                    / benchmark_variance
                )
            benchmark_metrics = risk_metrics(
                aligned["benchmark"], annual_risk_free_rate, confidence
            )

    def matrix(frame: pd.DataFrame) -> dict[str, dict[str, float | None]]:
        values = frame.to_numpy(dtype=float)
        return {
            str(row): {
                str(col): finite(float(values[i, j]))
                for j, col in enumerate(frame.columns)
            }
            for i, row in enumerate(frame.index)
        }

    volatilities = np.sqrt(np.maximum(np.diag(cov), 0))
    weighted_vol = float(w @ volatilities)
    return {
        "risk_metrics": metrics,
        "annualised_covariance": matrix(covariance),
        "correlations": matrix(correlations),
        "asset_risk_contributions": [
            {
                "symbol": str(symbol),
                "effective_weight": float(w[i]),
                "annualised_variance_contribution": float(contributions[i]),
                "fraction_of_variance": float(contributions[i] / variance)
                if variance > 1e-20
                else None,
            }
            for i, symbol in enumerate(asset_returns.columns)
        ],
        "diversification_ratio": weighted_vol / math.sqrt(variance)
        if variance > 1e-20
        else None,
        "benchmark_comparison": {
            "beta": beta,
            "observations": benchmark_observations,
            "benchmark_metrics": benchmark_metrics,
        },
    }
