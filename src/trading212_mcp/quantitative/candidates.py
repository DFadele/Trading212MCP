"""Read-only, same-sample comparisons of externally funded investment scenarios."""

import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd

from trading212_mcp.market.models import Company, Fundamentals, MarketError
from trading212_mcp.market.service import MarketService, period_start
from trading212_mcp.quantitative.analytics import finite, portfolio_statistics
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.models import Position

CASH = "__cash_gbp__"


@dataclass
class Asset:
    company: Company | None = None
    prices: pd.Series | None = None
    fundamentals: Fundamentals | None = None
    provenance: dict[str, Any] = field(default_factory=dict[str, Any])
    warnings: list[str] = field(default_factory=list[str])
    error: str | None = None


def concentration(weights: dict[str, float]) -> dict[str, float]:
    return {
        "herfindahl_index": sum(w * w for w in weights.values()),
        "largest_weight": max(weights.values(), default=0.0),
        "top_five_weight": sum(sorted(weights.values(), reverse=True)[:5]),
    }


def exposures(weights: dict[str, float], sectors: dict[str, str]) -> dict[str, float]:
    result: dict[str, float] = {}
    for symbol, weight in weights.items():
        sector = "Cash" if symbol == CASH else sectors.get(symbol, "Unknown")
        result[sector] = result.get(sector, 0.0) + weight
    return result


def difference(after: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in after.keys() | before.keys():
        latest, previous = after.get(name), before.get(name)
        result[name] = (
            latest - previous if latest is not None and previous is not None else None
        )
    return result


def statistics(
    returns: pd.DataFrame,
    values: dict[str, float],
    benchmark: pd.Series | None,
    risk_free_rate: float,
    confidence: float,
    historical_sharpe: bool,
) -> dict[str, Any] | None:
    total = sum(values.values())
    if total <= 0 or returns.empty:
        return None
    weights = {s: v / total for s, v in values.items() if v > 0}
    result: dict[str, Any] = portfolio_statistics(
        returns[list(weights)], weights, benchmark, risk_free_rate, confidence
    )
    if not historical_sharpe:
        result["risk_metrics"]["sharpe_ratio"] = None
    return result


def compact(stats: dict[str, Any] | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {
        "risk_metrics": stats["risk_metrics"],
        "beta": stats["benchmark_comparison"]["beta"],
        "benchmark_observations": stats["benchmark_comparison"]["observations"],
        "diversification_ratio": stats["diversification_ratio"],
    }


def scenario(
    name: str,
    additions: dict[str, float],
    full_values: dict[str, float],
    covered_values: dict[str, float],
    returns: pd.DataFrame,
    baseline: dict[str, Any] | None,
    sectors: dict[str, str],
    benchmark: pd.Series | None,
    risk_free_rate: float,
    confidence: float,
    historical_sharpe: bool,
) -> dict[str, Any]:
    before_total = sum(full_values.values())
    total = before_total + sum(additions.values())
    after_values = dict(full_values)
    model_values = dict(covered_values)
    for symbol, value in additions.items():
        after_values[symbol] = after_values.get(symbol, 0.0) + value
        model_values[symbol] = model_values.get(symbol, 0.0) + value
    before_weights = (
        {s: v / before_total for s, v in full_values.items()} if before_total else {}
    )
    weights = {s: v / total for s, v in after_values.items()}
    after = statistics(
        returns, model_values, benchmark, risk_free_rate, confidence, historical_sharpe
    )
    before_concentration = concentration(before_weights)
    after_concentration = concentration(weights)
    before_sectors = exposures(before_weights, sectors)
    after_sectors = exposures(weights, sectors)
    before_risk: dict[str, Any] = baseline["risk_metrics"] if baseline else {}
    before_beta = baseline["benchmark_comparison"]["beta"] if baseline else None
    model_total = sum(model_values.values())
    model_weights = {s: v / model_total for s, v in model_values.items() if v > 0}
    correlation = None
    marginal: dict[str, Any] = {}
    if after is not None:
        basket = sum(
            (returns[s] * v / sum(additions.values()) for s, v in additions.items()),
            start=pd.Series(0.0, index=returns.index),
        )
        if sum(covered_values.values()) > 0:
            current = sum(
                (
                    returns[s] * v / sum(covered_values.values())
                    for s, v in covered_values.items()
                ),
                start=pd.Series(0.0, index=returns.index),
            )
            if current.std(ddof=1) > 1e-15 and basket.std(ddof=1) > 1e-15:
                correlation = finite(float(current.corr(basket)))
        volatility = after["risk_metrics"]["annualised_volatility"]
        basket_covariance = 0.0
        for symbol, added in additions.items():
            covariance = sum(
                after["annualised_covariance"][symbol][other] * weight
                for other, weight in model_weights.items()
            )
            derivative = covariance / volatility if volatility > 1e-15 else None
            marginal[symbol] = {
                "marginal_volatility_per_unit_weight": derivative,
                "added_capital_euler_volatility_contribution": added
                / model_total
                * derivative
                if derivative is not None
                else None,
                "post_investment_holding_risk_contribution": next(
                    c
                    for c in after["asset_risk_contributions"]
                    if c["symbol"] == symbol
                ),
            }
            basket_covariance += added / sum(additions.values()) * covariance
        marginal["funded_volatility_derivative_per_additional_gbp"] = (
            (basket_covariance - volatility**2) / (model_total * volatility)
            if volatility > 1e-15
            else None
        )
    return {
        "scenario": name,
        "investment_gbp": sum(additions.values()),
        "purchases_gbp": additions,
        "post_investment_holdings_value_gbp": total,
        "post_investment_weights": weights,
        "analysed_weights": model_weights,
        "analysed_value_fraction": model_total / total,
        "post_investment_metrics": compact(after),
        "change_in_risk_metrics": difference(
            {
                k: v
                for k, v in after["risk_metrics"].items()
                if k not in {"confidence", "observations", "tail_observations"}
            },
            {
                k: v
                for k, v in before_risk.items()
                if k not in {"confidence", "observations", "tail_observations"}
            },
        )
        if after
        else None,
        "change_in_beta": after["benchmark_comparison"]["beta"] - before_beta
        if after
        and after["benchmark_comparison"]["beta"] is not None
        and before_beta is not None
        else None,
        "concentration": after_concentration,
        "change_in_concentration": difference(
            after_concentration, before_concentration
        ),
        "sector_exposure": after_sectors,
        "change_in_sector_exposure": {
            s: after_sectors.get(s, 0.0) - before_sectors.get(s, 0.0)
            for s in before_sectors.keys() | after_sectors.keys()
        },
        "correlation_with_existing_portfolio": correlation,
        "historical_diversification_benefit": 1 - correlation
        if correlation is not None
        else None,
        "marginal_risk": marginal,
    }


def leaders(
    results: list[dict[str, Any]], field: str, maximise: bool = False
) -> list[str]:
    available = [(r["scenario"], r[field]) for r in results if r.get(field) is not None]
    if not available:
        return []
    best = (max if maximise else min)(value for _, value in available)
    return [
        name
        for name, value in available
        if math.isclose(value, best, rel_tol=1e-9, abs_tol=1e-12)
    ]


def quality_leaders(assets: dict[str, Asset]) -> list[dict[str, Any]]:
    """Compare individual metrics only within explicit statement/provider groups."""
    groups: dict[tuple[str, str, str], list[tuple[str, Fundamentals]]] = defaultdict(
        list
    )
    for symbol, asset in assets.items():
        f = asset.fundamentals
        # These providers' quality ratios are vendor snapshots/TTM, whose
        # metric-specific ends are not established by the annual statement date.
        if f and f.source in {"yfinance", "financial_modeling_prep"}:
            continue
        if f and f.statement_date and f.statement_currency:
            groups[
                (f.source, f.statement_date.isoformat(), f.statement_currency)
            ].append((symbol, f))
    result: list[dict[str, Any]] = []
    for (source, period, currency), peers in sorted(groups.items()):
        for metric, higher in [
            ("net_margin", True),
            ("roic", True),
            ("debt_to_equity", False),
        ]:
            values = [
                (s, getattr(f, metric))
                for s, f in peers
                if getattr(f, metric) is not None
            ]
            if metric == "debt_to_equity":
                values = [(s, v) for s, v in values if v >= 0]
            if len(values) < 2:
                continue
            best = (max if higher else min)(v for _, v in values)
            result.append(
                {
                    "metric": metric,
                    "source": source,
                    "statement_date": period,
                    "currency": currency,
                    "peer_count": len(values),
                    "leaders": [s for s, v in values if math.isclose(v, best)],
                    "value": best,
                }
            )
    return result


@dataclass
class ResearchData:
    assets: dict[str, Asset]
    held: dict[str, Asset]
    full_values: dict[str, float]
    covered_values: dict[str, float]
    sectors: dict[str, str]
    analysis_symbols: dict[str, str]
    returns: pd.DataFrame
    benchmark_returns: pd.Series | None
    benchmark_asset: Asset
    valuation_fx: dict[str, Any]
    positions_retrieved_at: datetime
    start: date
    end: date
    warnings: list[str]
    excluded: list[dict[str, str]]


async def load_research_data(
    service: MarketService,
    trading: Trading212Client,
    tickers: list[str],
    investment_amount_gbp: float,
    lookback_years: int,
    benchmark: str | None,
    compare_cash: bool,
    annual_cash_return: float,
    listing_selections: dict[str, str] | None,
    *,
    include_fundamentals: bool = True,
) -> ResearchData:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + service.settings.risk_timeout_seconds
    try:
        async with asyncio.timeout_at(deadline):
            account = await trading.get_account_summary()
            positions = await trading.get_positions()
    except TimeoutError:
        raise MarketError(
            "Candidate analysis fetch budget expired retrieving the account/positions."
        ) from None
    if listing_selections and set(listing_selections) - {
        p.instrument.ticker for p in positions.positions
    }:
        raise MarketError(
            "Listing selections contain a ticker not held in this portfolio."
        )
    end = datetime.now(timezone.utc).date()
    start = period_start(lookback_years, end)
    currency = account.account.currency
    if any(
        p.walletImpact.currency != currency
        or not math.isfinite(float(p.walletImpact.currentValue))
        or p.walletImpact.currentValue < 0
        for p in positions.positions
    ):
        raise MarketError(
            "Candidate analysis requires finite non-negative holdings in the account currency."
        )
    valuation_fx: dict[str, Any] = {
        "source_currency": currency,
        "target_currency": "GBP",
        "rate": 1.0,
        "date": end.isoformat(),
        "basis": "identity" if currency == "GBP" else "latest_daily_spot",
    }
    has_holdings_value = any(
        p.walletImpact.currentValue > 0 for p in positions.positions
    )
    if currency != "GBP" and has_holdings_value:
        try:
            async with asyncio.timeout_at(deadline):
                fx = await service.provider.fx(
                    currency, "GBP", end - timedelta(days=7), end
                )
            points = [p for p in fx.prices if end - timedelta(days=7) <= p.date <= end]
            if not points:
                raise MarketError("No recent account-to-GBP valuation FX rate.")
            point = max(points, key=lambda p: p.date)
            valuation_fx.update(
                {
                    "rate": point.close,
                    "date": point.date.isoformat(),
                    "retrieved_at": fx.retrieved_at.isoformat(),
                }
            )
        except TimeoutError:
            raise MarketError(
                "Candidate analysis fetch budget expired converting portfolio values to GBP."
            ) from None
    elif currency != "GBP":
        valuation_fx.update(
            {"rate": None, "date": None, "basis": "not_required_zero_holdings"}
        )
    rate = float(valuation_fx["rate"]) if valuation_fx["rate"] is not None else 1.0
    if not math.isfinite(
        sum(float(p.walletImpact.currentValue) * rate for p in positions.positions)
        + investment_amount_gbp
    ):
        raise MarketError(
            "Portfolio/investment values exceed the supported numeric range."
        )
    assets = {s: Asset() for s in tickers}
    held: dict[str, Asset] = {}
    full_values: dict[str, float] = {}
    slots = asyncio.Semaphore(service.settings.fetch_concurrency)
    warnings: list[str] = []
    excluded: list[dict[str, str]] = []
    benchmark_asset = Asset()

    async def history(asset: Asset) -> None:
        assert asset.company is not None
        prices, raw, notes = await service.converted_history(
            asset.company, start, end, "GBP"
        )
        asset.warnings.extend(notes)
        asset.provenance = {
            "source": raw.source,
            "retrieved_at": raw.retrieved_at.isoformat(),
            "price_basis": raw.price_basis,
            "original_currency": raw.currency,
            "first_price_date": prices.dropna().index[0].date().isoformat()
            if prices.notna().any()
            else None,
            "last_price_date": prices.dropna().index[-1].date().isoformat()
            if prices.notna().any()
            else None,
            "analysis_currency": "GBP",
        }
        if prices.dropna().empty or prices.dropna().index[-1].date() < end - timedelta(
            days=7
        ):
            raise MarketError("Historical prices are stale or unavailable.")
        if (
            len(
                prices.pct_change(fill_method=None)
                .replace([np.inf, -np.inf], np.nan)
                .dropna()
            )
            < service.settings.min_observations
        ):
            raise MarketError("Insufficient daily adjusted-price/FX history.")
        asset.prices = prices

    async def candidate(symbol: str, asset: Asset, fundamentals: bool = True) -> None:
        async with slots:
            try:
                if loop.time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    asset.company = await service.provider.profile(symbol)
                    if asset.company.is_fund or asset.company.quote_type not in (
                        None,
                        "EQUITY",
                        "ETF",
                    ):
                        raise MarketError(
                            "Candidate security type is unsupported; use a stock or ETF listing."
                        )
                    try:
                        await history(asset)
                    except MarketError as exc:
                        asset.error = str(exc)
                        asset.warnings.append(f"Historical prices unavailable: {exc}")
                    if fundamentals:
                        try:
                            asset.fundamentals = await service.provider.fundamentals(
                                asset.company
                            )
                        except MarketError as exc:
                            asset.warnings.append(f"Fundamentals unavailable: {exc}")
            except TimeoutError:
                asset.error = "Candidate analysis fetch budget exhausted."
            except MarketError as exc:
                asset.error = str(exc)

    async def holding(position: Position) -> None:
        key = f"unresolved:{position.instrument.ticker}"
        value = float(position.walletImpact.currentValue) * rate
        if value <= 0:
            return
        asset = Asset()
        async with slots:
            try:
                if loop.time() >= deadline:
                    raise TimeoutError
                async with asyncio.timeout_at(deadline):
                    asset.company = await service.resolver.resolve(
                        position.instrument,
                        (listing_selections or {}).get(position.instrument.ticker),
                    )
                    key = asset.company.symbol
                    await history(asset)
            except TimeoutError:
                asset.error = "Candidate analysis fetch budget exhausted."
            except MarketError as exc:
                asset.error = str(exc)
        full_values[key] = full_values.get(key, 0.0) + value
        if asset.error:
            excluded.append(
                {"trading212_ticker": position.instrument.ticker, "reason": asset.error}
            )
        # A failed duplicate listing must not replace usable history.
        if key not in held or held[key].prices is None:
            held[key] = asset

    await asyncio.gather(
        *(candidate(s, a, include_fundamentals) for s, a in assets.items()),
        *(
            (candidate(benchmark, benchmark_asset, False),)
            if benchmark is not None
            else ()
        ),
        *(
            holding(p)
            for p in sorted(
                positions.positions,
                key=lambda p: p.walletImpact.currentValue,
                reverse=True,
            )
        ),
    )
    full_values = dict(
        sorted(full_values.items(), key=lambda item: (-item[1], item[0]))
    )
    held = {s: held[s] for s in full_values}
    sectors = {s: a.company.sector or "Unknown" for s, a in held.items() if a.company}
    series = {s: a.prices for s, a in held.items() if a.prices is not None}
    covered_values = {s: v for s, v in full_values.items() if s in series}
    canonical_isins = {
        a.company.isin: s
        for s, a in held.items()
        if a.company and a.company.isin and a.prices is not None
    }
    analysis_symbols: dict[str, str] = {}
    for symbol, asset in assets.items():
        if asset.company:
            known = held.get(symbol)
            if known and known.company:
                if asset.company.isin and known.company.isin != asset.company.isin:
                    asset.error = (
                        "Candidate identifier conflicts with the verified held listing."
                    )
                    asset.prices = None
                elif known.company.isin:
                    asset.company = asset.company.model_copy(
                        update={
                            "isin": known.company.isin,
                            "identifier_source": known.company.identifier_source,
                        }
                    )
            canonical = (
                canonical_isins.get(asset.company.isin, symbol)
                if asset.company.isin
                else symbol
            )
            analysis_symbols[symbol] = canonical
            sectors.setdefault(canonical, asset.company.sector or "Unknown")
            if canonical != symbol:
                asset.warnings.append(
                    f"Same verified ISIN as {canonical}; its GBP history and listing weights are reused, rather than counted as a new security."
                )
            if not asset.company.isin and symbol not in held:
                asset.warnings.append(
                    "Candidate ISIN unavailable; concentration is by listing, not consolidated issuer/share classes or unidentified cross-listings."
                )
            if asset.prices is not None:
                series.setdefault(canonical, asset.prices)
                if asset.company.isin:
                    canonical_isins.setdefault(asset.company.isin, canonical)
        if asset.error:
            asset.warnings.append(asset.error)
    frame = (
        pd.concat(series, axis=1, sort=False).sort_index() if series else pd.DataFrame()
    )
    returns = (
        frame.pct_change(fill_method=None).replace([np.inf, -np.inf], np.nan).dropna()
    )
    benchmark_returns = None
    if benchmark_asset.prices is not None and not frame.empty:
        observed = (
            benchmark_asset.prices.reindex(frame.index)
            .pct_change(fill_method=None)
            .replace([np.inf, -np.inf], np.nan)
            .dropna()
        )
        common = returns.index.intersection(observed.index)
        if len(common) >= service.settings.min_observations:
            returns = returns.loc[common]
            benchmark_returns = observed.loc[common]
        else:
            warnings.append(
                "Insufficient common benchmark observations; beta withheld for every scenario."
            )
    elif benchmark is not None:
        warnings.append(
            f"Benchmark unavailable: {benchmark_asset.error or 'no usable history'}; beta withheld."
        )
    if len(returns) < service.settings.min_observations:
        warnings.append(
            "Insufficient common daily returns across covered holdings and usable candidates; historical comparisons withheld."
        )
        returns = returns.iloc[:0]
    if compare_cash:
        returns[CASH] = (1 + annual_cash_return) ** (1 / 252) - 1
    total = sum(full_values.values())
    covered = sum(covered_values.values())
    if total > 0 and covered <= 0:
        returns = returns.iloc[:0]
        warnings.append(
            "No existing holdings have usable histories; portfolio risk comparisons withheld rather than representing the purchase alone as the portfolio."
        )
    if excluded:
        warnings.append(
            "Unresolved or missing-history holdings may duplicate candidate securities. Listing-level concentration cannot establish complete issuer exposures when identifiers are missing."
        )
    if covered < total:
        warnings.append(
            "Risk comparisons describe the covered subset plus new capital, not the full portfolio. Actual full-portfolio weights/concentration are shown separately from analysed weights."
        )
    if not returns.empty:
        warnings.append(
            f"All risk comparisons use {len(returns)} common daily returns from {returns.index[0].date()} to {returns.index[-1].date()}; the usable window may be shorter than the requested {lookback_years}-year history."
        )
    return ResearchData(
        assets,
        held,
        full_values,
        covered_values,
        sectors,
        analysis_symbols,
        returns,
        benchmark_returns,
        benchmark_asset,
        valuation_fx,
        positions.retrieved_at,
        start,
        end,
        warnings,
        excluded,
    )


async def analyse_candidates(
    service: MarketService,
    trading: Trading212Client,
    tickers: list[str],
    investment_amount_gbp: float,
    lookback_years: int = 3,
    benchmark: str = "SPY",
    compare_cash: bool = False,
    allocations: dict[str, float] | None = None,
    use_historical_mean_returns: bool = False,
    annual_risk_free_rate: float = 0.0,
    annual_cash_return: float = 0.0,
    confidence: float = 0.95,
    listing_selections: dict[str, str] | None = None,
) -> dict[str, Any]:
    tickers = list(dict.fromkeys(tickers))
    if not tickers or len(tickers) > 10 or CASH in tickers:
        raise MarketError(
            "Supply one to ten distinct provider candidate symbols; the internal cash symbol is reserved."
        )
    if (
        isinstance(investment_amount_gbp, bool)
        or not math.isfinite(investment_amount_gbp)
        or investment_amount_gbp <= 0
    ):
        raise MarketError("Investment amount must be finite and positive, in GBP.")
    if (
        not 1 <= lookback_years <= 10
        or not 0.5 < confidence < 1
        or not all(
            math.isfinite(v) and -1 < v <= 1
            for v in (annual_risk_free_rate, annual_cash_return)
        )
    ):
        raise MarketError("Invalid lookback, confidence or annual rate assumptions.")
    if allocations is not None and (
        not allocations
        or set(allocations) - set(tickers)
        or any(
            isinstance(v, bool) or not math.isfinite(v) or v <= 0
            for v in allocations.values()
        )
        or not math.isclose(sum(allocations.values()), 1.0, rel_tol=0, abs_tol=1e-9)
    ):
        raise MarketError(
            "Allocations must use candidate symbols, positive fractions, and sum to one."
        )
    data = await load_research_data(
        service,
        trading,
        tickers,
        investment_amount_gbp,
        lookback_years,
        benchmark,
        compare_cash,
        annual_cash_return,
        listing_selections,
    )
    assets, held = data.assets, data.held
    full_values, covered_values = data.full_values, data.covered_values
    sectors, analysis_symbols = data.sectors, data.analysis_symbols
    returns, benchmark_returns = data.returns, data.benchmark_returns
    benchmark_asset, valuation_fx = data.benchmark_asset, data.valuation_fx
    start, end = data.start, data.end
    warnings, excluded = data.warnings, data.excluded
    total, covered = sum(full_values.values()), sum(covered_values.values())
    if not use_historical_mean_returns:
        warnings.append(
            "Sharpe estimates and their changes are withheld unless use_historical_mean_returns=true. Opting in gives descriptive historical estimates, not expected-return forecasts."
        )
    baseline = statistics(
        returns,
        covered_values,
        benchmark_returns,
        annual_risk_free_rate,
        confidence,
        use_historical_mean_returns,
    )
    results: list[dict[str, Any]] = []
    candidate_results: list[dict[str, Any]] = []
    for symbol, asset in assets.items():
        row: dict[str, Any] = {
            "ticker": symbol,
            "analysis_symbol": analysis_symbols.get(symbol),
            "status": ("partial" if covered < total else "available")
            if asset.prices is not None and not returns.empty
            else "unavailable",
            "company": asset.company.model_dump(mode="json") if asset.company else None,
            "fundamentals": asset.fundamentals.model_dump(mode="json")
            if asset.fundamentals
            else None,
            "missing_fundamentals": [
                name
                for name in type(asset.fundamentals).model_fields
                if getattr(asset.fundamentals, name) is None
            ]
            if asset.fundamentals
            else ["fundamentals"],
            "price_provenance": asset.provenance,
            "warnings": asset.warnings,
            "analysis": None,
        }
        if asset.prices is not None:
            row["analysis"] = scenario(
                symbol,
                {analysis_symbols[symbol]: investment_amount_gbp},
                full_values,
                covered_values,
                returns,
                baseline,
                sectors,
                benchmark_returns,
                annual_risk_free_rate,
                confidence,
                use_historical_mean_returns,
            )
            results.append(row["analysis"])
        candidate_results.append(row)
    allocated = None
    if allocations is not None:
        if all(assets[s].prices is not None for s in allocations):
            additions: dict[str, float] = {}
            # Normalise only accepted floating-point sum error, not missing allocations.
            for symbol, fraction in allocations.items():
                key = analysis_symbols[symbol]
                additions[key] = additions.get(
                    key, 0.0
                ) + investment_amount_gbp * fraction / sum(allocations.values())
            allocated = scenario(
                "allocated",
                additions,
                full_values,
                covered_values,
                returns,
                baseline,
                sectors,
                benchmark_returns,
                annual_risk_free_rate,
                confidence,
                use_historical_mean_returns,
            )
            results.append(allocated)
        else:
            warnings.append(
                "Allocated scenario unavailable: at least one allocated candidate has insufficient/missing prices; no funds redistributed."
            )
    cash = (
        scenario(
            "hold_cash",
            {CASH: investment_amount_gbp},
            full_values,
            covered_values,
            returns,
            baseline,
            sectors,
            benchmark_returns,
            annual_risk_free_rate,
            confidence,
            use_historical_mean_returns,
        )
        if compare_cash
        else None
    )
    eligible = [r for r in results if r["post_investment_metrics"] is not None]
    ranking = [
        {
            "scenario": r["scenario"],
            "diversification": r["historical_diversification_benefit"],
            "volatility": r["post_investment_metrics"]["risk_metrics"][
                "annualised_volatility"
            ],
            "concentration": r["change_in_concentration"]["herfindahl_index"],
        }
        for r in eligible
    ]
    current_weights = {s: v / total for s, v in full_values.items()} if total else {}
    return {
        "source": service.provider.name,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "currency": "GBP",
        "current_portfolio": {
            "holdings_value_gbp": total,
            "weights": current_weights,
            "analysed_weights": {s: v / covered for s, v in covered_values.items()}
            if covered
            else {},
            "metrics": compact(baseline),
            "concentration": concentration(current_weights),
            "sector_exposure": exposures(current_weights, sectors),
            "positions_retrieved_at": data.positions_retrieved_at.isoformat(),
            "valuation_fx": valuation_fx,
        },
        "candidates": candidate_results,
        "allocated_scenario": allocated,
        "cash_comparison": cash,
        "comparison": {
            "scope": "covered_subset_only"
            if covered < total
            else "full_open_positions",
            "largest_historical_diversification_benefit": leaders(
                ranking, "diversification", True
            ),
            "lowest_resulting_volatility": leaders(ranking, "volatility"),
            "lowest_concentration_impact": leaders(ranking, "concentration"),
            "financial_quality_metric_leaders": quality_leaders(assets),
            "composite_score": None,
        },
        "data_quality": {
            "included_holdings_value_fraction": covered / total if total else None,
            "excluded_holdings": excluded,
            "common_observations": len(returns),
            "requested_period": {"start": start.isoformat(), "end": end.isoformat()},
            "period_used": {
                "start": returns.index[0].date().isoformat(),
                "end": returns.index[-1].date().isoformat(),
            }
            if not returns.empty
            else None,
            "holding_price_provenance": {s: a.provenance for s, a in held.items()},
            "holding_sectors": sectors,
            "benchmark": {
                "symbol": benchmark,
                "available": benchmark_returns is not None,
                "price_provenance": benchmark_asset.provenance,
                "warnings": benchmark_asset.warnings,
            },
            "warnings": warnings
            + [f"{s}: {w}" for s, a in held.items() for w in a.warnings],
        },
        "assumptions": {
            "funding": "New external GBP capital; total holdings value increases by the investment. Existing uninvested, reserved and pie cash is excluded from the baseline; no existing positions are sold.",
            "returns": "Historical split/dividend-adjusted daily prices in GBP; fixed current/post-purchase weights rebalanced daily. No forecast returns or optimisation.",
            "alignment": "One common observed-date sample across covered holdings and all usable candidates; no equity price fill. Benchmark uses the same sample when sufficiently available. Missing candidates are not allocated capital in a replacement scenario.",
            "sharpe": "Descriptive historical mean excess returns"
            if use_historical_mean_returns
            else "Withheld; no expected-return assumption supplied",
            "annual_risk_free_rate": annual_risk_free_rate,
            "annual_cash_return": annual_cash_return,
            "cash": "Optional new-capital-only GBP cash scenario with constant assumed daily return; not observed cash performance. Excluded from candidate winner rankings. Cash is a separate concentration/exposure bucket.",
            "units": "Weights, growth, margins, volatility and daily losses are fractions; ratios/beta are dimensionless. Purchases and portfolio values are GBP; financial statements retain their reported currency.",
            "costs": "Zero fees, spread, taxes and FX charges; fractional shares and immediate deployment assumed. No orders are created.",
            "risk": "Existing sample covariance/ddof=1 analytics, annualised by 252; historical Sharpe/Sortino/VaR/CVaR/drawdown are descriptive. Common historical means are not reliable expected-return forecasts.",
            "diversification": "1 minus correlation of the purchase basket with covered current-weight portfolio; separate from resulting volatility and HHI. Undefined for zero/constant-return baseline or basket.",
            "marginal_risk": "Unconstrained derivative d(volatility)/d(asset weight)=annual Cov(asset,portfolio)/portfolio volatility; added-weight Euler contribution and the funded direction d(volatility)/d(additional GBP) are separately labelled. Existing holdings' risk contribution includes their original and new allocation.",
            "concentration": "HHI/largest/top-five by resolved security/listing, not issuer or ETF look-through. Verified shared ISINs reuse the held listing; unknown cross-listings/share classes are not guessed. Sector exposures also have no ETF look-through.",
            "fundamentals": "Whole configured provider block with provenance, reported dates/currencies and null missing metrics. Quality leaders compare individual metrics only within equal source, statement end and currency, requiring two peers. Vendor snapshot/TTM quality metrics are not ranked without verified metric-specific periods; raw values remain available. No opaque recommendation score.",
            "limitations": "Current constituents introduce survivorship/selection bias; closes across venues are asynchronous. Corporate-action quality is provider-dependent, and historical tails omit unobserved losses.",
            "fetch_budget_seconds": service.settings.risk_timeout_seconds,
        },
    }
