"""Portfolio risk workflow, coverage and historical calculations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pandas as pd

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.service import period_start
from trading212_mcp.quantitative.analytics import portfolio_statistics
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.models import Position

if TYPE_CHECKING:
    from trading212_mcp.market.service import MarketService


async def analyse_portfolio_risk(
    service: MarketService,
    trading: Trading212Client,
    lookback_years: int,
    benchmark: str,
    annual_risk_free_rate: float = 0.0,
    confidence: float = 0.95,
    listing_selections: dict[str, str] | None = None,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + service.settings.risk_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            account = await trading.get_account_summary()
            snapshot = await trading.get_positions()
    except TimeoutError:
        raise MarketError(
            "Risk analysis time budget expired while fetching the Trading 212 account/positions. Try again later."
        ) from None
    currency = account.account.currency
    if listing_selections and set(listing_selections) - {
        p.instrument.ticker for p in snapshot.positions
    }:
        raise MarketError(
            "Listing selections contain a ticker not held in the current portfolio."
        )
    total = sum(float(p.walletImpact.currentValue) for p in snapshot.positions)
    if any(
        p.walletImpact.currency != currency or p.walletImpact.currentValue < 0
        for p in snapshot.positions
    ):
        raise MarketError(
            "Risk analysis requires non-negative holdings in one account currency."
        )
    end = datetime.now(timezone.utc).date()
    start = period_start(lookback_years, end)
    warnings: list[str] = []
    excluded: list[dict[str, Any]] = []
    series: dict[str, pd.Series] = {}
    values: dict[str, float] = {}
    exposures: dict[str, dict[str, float]] = {"sector": {}, "industry": {}}
    holdings: list[dict[str, Any]] = []
    provenance: dict[str, Any] = {}
    slots = asyncio.Semaphore(service.settings.fetch_concurrency)

    async def fetch_position(position: Position) -> None:
        async with slots:
            value = float(position.walletImpact.currentValue)
            weight = value / total if total > 0 else 0.0
            holding: dict[str, Any] = {
                "trading212_ticker": position.instrument.ticker,
                "value": value,
                "weight": weight,
            }
            holdings.append(holding)
            company = None
            try:
                if loop.time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    company = await service.resolver.resolve(
                        position.instrument,
                        selected_symbol=(listing_selections or {}).get(
                            position.instrument.ticker
                        ),
                    )
                holding["market_symbol"] = company.symbol
                holding["identifier_source"] = company.identifier_source
                holding["sector"] = company.sector or "Unknown"
                holding["industry"] = company.industry or "Unknown"
            except TimeoutError:
                excluded.append(
                    {
                        **holding,
                        "reason": "Risk analysis time budget exhausted before instrument resolution completed.",
                    }
                )
            except MarketError as exc:
                excluded.append({**holding, "reason": str(exc)})
            for field in exposures:
                label = getattr(company, field) if company else None
                label = label or "Unknown"
                exposures[field][label] = exposures[field].get(label, 0.0) + weight
            if company is None or value <= 0:
                return
            try:
                async with asyncio.timeout_at(deadline):
                    prices, history, notes = await service.converted_history(
                        company, start, end, currency
                    )
                if (
                    len(prices.pct_change(fill_method=None).dropna())
                    < service.settings.min_observations
                ):
                    raise MarketError("Insufficient daily price/FX observations.")
                if prices.dropna().empty or cast(
                    pd.Timestamp, prices.last_valid_index()
                ).date() < end - timedelta(days=7):
                    raise MarketError(
                        "Stale price history; latest price is over seven calendar days old."
                    )
                if company.symbol in series:
                    values[company.symbol] += value
                else:
                    series[company.symbol] = prices
                    values[company.symbol] = value
                provenance[company.symbol] = {
                    "retrieved_at": history.retrieved_at.isoformat(),
                    "price_basis": history.price_basis,
                    "currency": currency,
                }
                warnings.extend(f"{company.symbol}: {note}" for note in notes)
            except TimeoutError:
                excluded.append(
                    {
                        **holding,
                        "reason": "Risk analysis time budget exhausted before price/FX history completed.",
                    }
                )
            except MarketError as exc:
                excluded.append({**holding, "reason": str(exc)})

    await asyncio.gather(
        *(
            fetch_position(position)
            for position in sorted(
                snapshot.positions,
                key=lambda p: p.walletImpact.currentValue,
                reverse=True,
            )
        )
    )
    # Network completion order must not change matrix/contribution ordering.
    ordered_symbols = list(
        dict.fromkeys(
            h["market_symbol"] for h in holdings if h.get("market_symbol") in series
        )
    )
    series = {symbol: series[symbol] for symbol in ordered_symbols}
    values = {symbol: values[symbol] for symbol in ordered_symbols}
    included_value = sum(values.values())
    coverage = included_value / total if total else 0.0
    weights = (
        {symbol: value / included_value for symbol, value in values.items()}
        if included_value
        else {}
    )
    result: dict[str, Any] = {
        "source": service.provider.name,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "portfolio_summary": {
            "environment": snapshot.environment,
            "currency": currency,
            "positions_retrieved_at": snapshot.retrieved_at.isoformat(),
            "total_holdings_value": total,
            "holdings": holdings,
        },
        "concentration_analysis": {
            "largest_weight": max((h["weight"] for h in holdings), default=0.0),
            "top_five_weight": sum(
                sorted((h["weight"] for h in holdings), reverse=True)[:5]
            ),
            "herfindahl_index": sum(h["weight"] ** 2 for h in holdings),
        },
        "diversification_analysis": exposures,
        "data_quality": {
            "included_value_fraction": coverage,
            "excluded": excluded,
            "provenance": provenance,
            "warnings": warnings,
        },
        "methodology": {
            "fetch_budget_seconds": service.settings.risk_timeout_seconds,
            "fetch_order": "Bounded concurrent fetches, queued by highest current value; unfinished assets are explicitly excluded when the overall budget expires.",
            "type": "hypothetical_current_weight_backtest",
            "actual_historical_performance": False,
            "weights": "Current open-position wallet values; cash excluded. Excluded assets removed and remaining weights renormalised. Daily rebalancing to those fixed weights.",
            "annualisation_days": 252,
            "returns": "Simple daily returns from provider split/dividend-adjusted prices, converted to account currency with daily FX.",
            "alignment": "Union of observed asset dates; no equity price filling. A return is retained only when every included asset has a price on that date and the immediately preceding union-calendar date. No future FX rates.",
            "covariance": "Sample covariance (ddof=1), annualised by 252.",
            "risk_free_rate_annual": annual_risk_free_rate,
            "sharpe": "Mean daily excess return / sample daily volatility * sqrt(252).",
            "sortino": "Mean daily excess return / RMS negative excess return over ALL observations * sqrt(252).",
            "drawdown": "Negative peak-to-trough fraction; includes initial wealth of one.",
            "var_cvar": f"One-day historical loss fractions at confidence {confidence}; linear quantile; CVaR is the mean loss of observations at or below the return quantile. Floors losses at zero.",
            "risk_contributions": "w_i * (annual covariance @ w)_i; fractions sum to one when variance is nonzero; may be negative for hedging assets.",
            "limitations": "Current constituents introduce survivorship bias; no historical trades, cashflows, taxes or transaction costs are modelled. Daily closes across exchanges are not synchronous.",
        },
        "risk_metrics": None,
        "asset_risk_contributions": [],
        "benchmark_comparison": {"symbol": benchmark, "beta": None},
    }
    if loop.time() >= deadline:
        warnings.append(
            "Overall risk fetch budget exhausted; results are partial. Exclusions and included value fraction describe the covered subset."
        )
    if coverage < 1:
        warnings.append(
            "Risk metrics describe only the covered subset, reweighted to 100%; exclusions can materially change risk."
        )
    if not series:
        warnings.append(
            "No holdings have usable price history. Only current concentration/exposure is available."
        )
        return result
    price_frame = pd.concat(series, axis=1).sort_index()
    returns = (
        price_frame.pct_change(fill_method=None)
        .replace([np.inf, -np.inf], np.nan)
        .dropna()
    )
    if len(returns) < service.settings.min_observations:
        warnings.append(
            "Too few common daily returns after calendar/FX alignment; risk metrics withheld."
        )
        return result
    warnings.append(
        f"Used {len(returns)} common daily returns; {len(price_frame) - 1 - len(returns)} potential rows dropped for missing prices/calendars."
    )
    benchmark_returns = None
    try:
        if loop.time() >= deadline:
            raise TimeoutError
        async with asyncio.timeout_at(deadline):
            company = await service.provider.profile(benchmark)
            (
                benchmark_prices,
                benchmark_history,
                notes,
            ) = await service.converted_history(company, start, end, currency)
        benchmark_returns = (
            benchmark_prices.reindex(price_frame.index)
            .pct_change(fill_method=None)
            .dropna()
        )
        if (
            len(returns.index.intersection(benchmark_returns.index))
            < service.settings.min_observations
        ):
            benchmark_returns = None
            warnings.append(
                "Insufficient matched benchmark observations; beta withheld."
            )
        if benchmark_prices.dropna().empty or cast(
            pd.Timestamp, benchmark_prices.last_valid_index()
        ).date() < end - timedelta(days=7):
            benchmark_returns = None
            warnings.append("Benchmark prices are stale; beta withheld.")
        provenance[benchmark] = {
            "retrieved_at": benchmark_history.retrieved_at.isoformat(),
            "price_basis": benchmark_history.price_basis,
        }
        warnings.extend(f"{benchmark}: {note}" for note in notes)
    except TimeoutError:
        warnings.append(
            "Benchmark unavailable: overall risk fetch budget exhausted; beta withheld."
        )
    except MarketError as exc:
        warnings.append(f"Benchmark unavailable: {exc}")
    result.update(
        portfolio_statistics(
            returns, weights, benchmark_returns, annual_risk_free_rate, confidence
        )
    )
    result["benchmark_comparison"]["symbol"] = benchmark
    result["data_quality"]["period_used"] = {
        "start": returns.index[0].date().isoformat(),
        "end": returns.index[-1].date().isoformat(),
    }
    return result
