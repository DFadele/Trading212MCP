import math

import numpy as np
import pandas as pd
import pytest

from trading212_mcp.quantitative.analytics import portfolio_statistics, risk_metrics


def test_known_risk_metrics():
    returns = pd.Series([0.01, -0.02, 0.03, -0.04])
    result = risk_metrics(returns, confidence=0.75)
    # Mean -0.005, sample variance 0.0029/3, downside RMS sqrt(.002/4).
    expected_vol = math.sqrt(0.0029 / 3) * math.sqrt(252)
    assert result["annualised_volatility"] == pytest.approx(expected_vol)
    assert result["sharpe_ratio"] == pytest.approx(
        -0.005 / math.sqrt(0.0029 / 3) * math.sqrt(252)
    )
    assert result["sortino_ratio"] == pytest.approx(
        -0.005 / math.sqrt(0.002 / 4) * math.sqrt(252)
    )
    assert result["historical_var"] == pytest.approx(0.025)
    assert result["historical_cvar"] == pytest.approx(0.04)
    wealth = [1, 1.01, 0.9898, 1.019494, 0.97871424]
    assert result["maximum_drawdown"] == pytest.approx(
        min(v / max(wealth[: i + 1]) - 1 for i, v in enumerate(wealth))
    )


def test_initial_loss_counts_in_drawdown():
    assert risk_metrics(pd.Series([-0.1, 0.01]))["maximum_drawdown"] == pytest.approx(
        -0.1
    )


def test_covariance_correlation_beta_and_contributions():
    frame = pd.DataFrame(
        {"A": [0.01, -0.02, 0.03, -0.04], "B": [0.02, -0.04, 0.06, -0.08]}
    )
    result = portfolio_statistics(frame, {"A": 0.5, "B": 0.5}, frame["A"])
    variance_a = 0.0029 / 3 * 252
    assert result["annualised_covariance"]["A"]["A"] == pytest.approx(variance_a)
    assert result["annualised_covariance"]["A"]["B"] == pytest.approx(2 * variance_a)
    assert result["correlations"]["A"]["B"] == pytest.approx(1)
    assert result["benchmark_comparison"]["beta"] == pytest.approx(1.5)
    contributions = result["asset_risk_contributions"]
    assert contributions[0]["fraction_of_variance"] == pytest.approx(1 / 3)
    assert contributions[1]["fraction_of_variance"] == pytest.approx(2 / 3)
    assert sum(
        c["annualised_variance_contribution"] for c in contributions
    ) == pytest.approx(2.25 * variance_a)


def test_zero_variance_is_not_infinity():
    result = portfolio_statistics(pd.DataFrame({"A": [0.0, 0.0, 0.0]}), {"A": 1.0})
    assert result["risk_metrics"]["sharpe_ratio"] is None
    assert result["risk_metrics"]["sortino_ratio"] is None
    assert result["asset_risk_contributions"][0]["fraction_of_variance"] is None
    assert result["correlations"]["A"]["A"] is None


def test_risk_free_conversion():
    r = pd.Series([0.01, -0.01, 0.02, -0.02])
    daily = 1.05 ** (1 / 252) - 1
    assert risk_metrics(r, 0.05)["sharpe_ratio"] == pytest.approx(
        -daily / np.std(r, ddof=1) * math.sqrt(252)
    )


@pytest.mark.parametrize("values", [[np.nan, 0.1], [np.inf, 0.1], [-1.0, 0.1], [0.1]])
def test_invalid_returns_rejected(values):
    with pytest.raises(ValueError):
        risk_metrics(pd.Series(values))


def test_independent_scalar_reference_with_unequal_weights_and_hedging():
    """Reference uses scalar arithmetic, independent of NumPy/pandas reductions."""
    a = [0.02, -0.01, 0.03, -0.04, 0.01, -0.02]
    b = [-0.01, 0.02, -0.03, 0.02, -0.01, 0.01]
    p = [0.8 * x + 0.2 * y for x, y in zip(a, b)]

    def mean(values):
        return sum(values) / len(values)

    def covariance(x, y):
        return sum((v - mean(x)) * (w - mean(y)) for v, w in zip(x, y)) / (len(x) - 1)

    result = portfolio_statistics(
        pd.DataFrame({"A": a, "B": b}), {"A": 0.8, "B": 0.2}, pd.Series(b), 0.05, 0.8
    )
    metrics = result["risk_metrics"]
    sigma = math.sqrt(covariance(p, p))
    daily_rf = 1.05 ** (1 / 252) - 1
    excess = [v - daily_rf for v in p]
    assert metrics["annualised_volatility"] == pytest.approx(sigma * math.sqrt(252))
    assert metrics["sharpe_ratio"] == pytest.approx(
        mean(excess) / sigma * math.sqrt(252)
    )
    assert metrics["sortino_ratio"] == pytest.approx(
        mean(excess)
        / math.sqrt(mean([min(v, 0) ** 2 for v in excess]))
        * math.sqrt(252)
    )
    # At 80% confidence and six observations, the linear quantile is the second loss.
    quantile = sorted(p)[1]
    assert metrics["historical_var"] == pytest.approx(max(0, -quantile))
    assert metrics["historical_cvar"] == pytest.approx(
        max(0, -mean([v for v in p if v <= quantile]))
    )
    wealth = peak = 1.0
    worst = 0.0
    for value in p:
        wealth *= 1 + value
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1)
    assert metrics["maximum_drawdown"] == pytest.approx(worst)
    assert result["annualised_covariance"]["A"]["B"] == pytest.approx(
        252 * covariance(a, b)
    )
    assert result["correlations"]["A"]["B"] == pytest.approx(
        covariance(a, b) / math.sqrt(covariance(a, a) * covariance(b, b))
    )
    assert result["benchmark_comparison"]["beta"] == pytest.approx(
        covariance(p, b) / covariance(b, b)
    )
    contributions = result["asset_risk_contributions"]
    assert contributions[1]["annualised_variance_contribution"] < 0
    assert sum(
        c["annualised_variance_contribution"] for c in contributions
    ) == pytest.approx(252 * covariance(p, p))


def test_cvar_includes_exact_quantile_boundary_despite_float_rounding():
    returns = pd.Series([-0.028, -0.014, -0.004, 0.006, 0.014, 0.018])
    result = risk_metrics(returns, confidence=0.8)
    assert result["tail_observations"] == 2
    assert result["historical_var"] == pytest.approx(0.014)
    assert result["historical_cvar"] == pytest.approx(0.021)
    # A genuinely different quantile must still exclude the boundary loss.
    assert risk_metrics(returns, confidence=0.800000001)["tail_observations"] == 1
