"""Provider-universe filtering and transparent cross-sectional factor ranking."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, cast

import numpy as np
import pandas as pd

from trading212_mcp.market.models import Company, Fundamentals, MarketError
from trading212_mcp.market.normalization import country_code
from trading212_mcp.market.service import MarketService
from trading212_mcp.observability import timed_sync
from trading212_mcp.quantitative.factors import (
    ADVANCED_METRICS,
    financial_features,
    momentum_features,
)
from trading212_mcp.trading212.client import Trading212Client

STRATEGIES = {
    "quality_growth": {
        "growth": 0.30,
        "valuation": 0.20,
        "quality": 0.30,
        "momentum": 0.15,
        "diversification": 0.05,
    },
    "balanced": {
        "growth": 0.20,
        "valuation": 0.20,
        "quality": 0.20,
        "momentum": 0.20,
        "diversification": 0.20,
    },
    "value": {
        "growth": 0.10,
        "valuation": 0.45,
        "quality": 0.25,
        "momentum": 0.15,
        "diversification": 0.05,
    },
}
FACTOR_METRICS = {
    "growth": [
        ("revenue_growth", True),
        ("revenue_cagr", True),
        ("earnings_growth", True),
        ("free_cash_flow_growth", True),
        ("margin_expansion", True),
    ],
    "valuation": [
        ("pe", False),
        ("forward_pe", False),
        ("price_to_book", False),
        ("price_to_sales", False),
        ("ev_to_ebitda", False),
        ("earnings_yield", True),
        ("free_cash_flow_yield", True),
    ],
    "quality": [
        ("net_margin", True),
        ("roic", True),
        ("roe", True),
        ("operating_profitability", True),
        ("cash_conversion", True),
        ("debt_to_equity", False),
        ("debt_to_assets", False),
    ],
    "financial_improvement": [
        ("profitability_improvement", True),
        ("cash_flow_improvement", True),
        ("deleveraging", True),
        ("financial_stability", True),
    ],
    "momentum": [
        ("momentum_6m", True),
        ("momentum_12_1", True),
        ("relative_strength", True),
    ],
    "diversification": [("diversification_benefit", True)],
}


@timed_sync("quantitative_calculation")
def rank_candidates(
    candidates: list[dict[str, Any]],
    weights: dict[str, float],
    *,
    min_available_factor_weight: float = 0.5,
    min_metric_coverage: float = 0.25,
    min_sector_peers: int = 5,
    exclusions: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if (
        not weights
        or set(weights) - set(FACTOR_METRICS)
        or any(not np.isfinite(v) or v < 0 for v in weights.values())
        or not np.isfinite(sum(weights.values()))
        or sum(weights.values()) <= 0
    ):
        raise MarketError(
            "Factor weights must be finite, non-negative, use known factor names, and have a positive total."
        )
    if (
        not 0 <= min_available_factor_weight <= 1
        or not 0 <= min_metric_coverage <= 1
        or not 2 <= min_sector_peers <= 100
    ):
        raise MarketError("Invalid ranking quality thresholds or sector peer count.")
    normalised = {
        factor: weights.get(factor, 0) / sum(weights.values())
        for factor in FACTOR_METRICS
    }
    if not candidates:
        return []
    frame = pd.DataFrame(
        [{"sector": c["company"].get("sector"), **c["metrics"]} for c in candidates]
    )
    scores: dict[str, pd.Series] = {}
    explanations: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for factor, metrics in FACTOR_METRICS.items():
        metric_scores: list[pd.Series] = []
        explanations[factor] = {}
        for metric, higher_better in metrics:
            values = pd.to_numeric(
                frame.get(metric, pd.Series(np.nan, index=frame.index)), errors="coerce"
            ).replace([np.inf, -np.inf], np.nan)
            if metric in POSITIVE_METRICS:
                values = values.where(values > 0)
            if metric in ("debt_to_equity", "debt_to_assets"):
                values = values.where(values >= 0)
            ranked = pd.Series(np.nan, index=frame.index, dtype=float)
            details: list[dict[str, Any]] = [{} for _ in candidates]
            # Never compare an annual definition against an unrelated vendor snapshot.
            bases = pd.Series(
                [
                    c.get("metric_metadata", {})
                    .get(metric, {})
                    .get("basis", "configured_provider_definition")
                    for c in candidates
                ],
                index=frame.index,
            )
            for basis in sorted(bases.unique()):
                basis_mask = bases == basis
                group_scores: dict[str, pd.Series] = {}
                for label in frame.index[basis_mask]:
                    i = cast(int, label)
                    sector = cast(str | None, candidates[i]["company"].get("sector"))
                    sector_mask = basis_mask & (frame["sector"] == sector)
                    use_sector = (
                        factor not in ("momentum", "diversification")
                        and pd.notna(sector)
                        and sector != "Unknown"
                        and values[sector_mask].count() >= min_sector_peers
                    )
                    mask = sector_mask if use_sector else basis_mask
                    available = values[mask].dropna()
                    detail = {
                        "value": float(values.loc[i])
                        if pd.notna(values.loc[i])
                        else None,
                        "score": None,
                        "peer_count": len(available),
                        "normalisation": "sector"
                        if use_sector
                        else "evaluated_universe",
                        "basis": basis,
                        "direction": "higher" if higher_better else "lower",
                    }
                    if pd.notna(values.loc[i]) and not available.empty:
                        peer_key = str(sector) if use_sector else "__universe__"
                        if peer_key not in group_scores:
                            clipped = values[mask].clip(
                                float(available.quantile(0.05)),
                                float(available.quantile(0.95)),
                            )
                            group_scores[peer_key] = (
                                (
                                    clipped.rank(
                                        method="average", ascending=higher_better
                                    )
                                    - 0.5
                                )
                                / len(available)
                                * 100
                            )
                        percentiles = group_scores[peer_key]
                        ranked.iloc[i] = percentiles.loc[i]
                        detail["score"] = float(percentiles.loc[i])
                    else:
                        detail["missing_reason"] = (
                            "Missing, nonfinite or economically invalid for this metric."
                        )
                    details[i] = detail
            explanations[factor][metric] = details
            metric_scores.append(ranked)
        scores[factor] = pd.concat(metric_scores, axis=1).mean(axis=1, skipna=True)
    output: list[dict[str, Any]] = []
    active = [f for f, w in normalised.items() if w > 0]
    for i, candidate in enumerate(candidates):
        factor_scores = {
            factor: float(score.iloc[i]) if pd.notna(score.iloc[i]) else None
            for factor, score in scores.items()
        }
        available_weight = sum(
            normalised[f] for f, value in factor_scores.items() if value is not None
        )
        populated = sum(factor_scores[f] is not None for f in active)
        metric_count = sum(
            explanations[f][m][i]["score"] is not None
            for f in active
            for m, _ in FACTOR_METRICS[f]
        )
        possible_count = sum(len(FACTOR_METRICS[f]) for f in active)
        metric_coverage = metric_count / possible_count
        if (
            populated < min(2, len(active))
            or available_weight < min_available_factor_weight
            or metric_coverage < min_metric_coverage
            or available_weight <= 0
        ):
            if exclusions is not None:
                exclusions.append(
                    {
                        "symbol": candidate["company"]["symbol"],
                        "stage": "ranking_quality",
                        "reasons": [
                            "Insufficient active-factor count, available factor weight or metric coverage."
                        ],
                        "available_factor_weight": available_weight,
                        "metric_coverage": metric_coverage,
                    }
                )
            continue
        factor_details = {}
        for factor in FACTOR_METRICS:
            details_by_metric = {
                metric: explanations[factor][metric][i]
                for metric, _ in FACTOR_METRICS[factor]
            }
            observed = [
                metric
                for metric, detail in details_by_metric.items()
                if detail["score"] is not None
            ]
            score = factor_scores[factor]
            factor_details[factor] = {
                "score": score,
                "configured_weight": normalised[factor],
                "effective_weight": normalised[factor] / available_weight
                if score is not None
                else 0,
                "observed_metrics": observed,
                "missing_metrics": [m for m in details_by_metric if m not in observed],
                "metric_scores": details_by_metric,
                "composite_contribution": normalised[factor] / available_weight * score
                if score is not None
                else None,
            }
        composite = (
            sum(
                normalised[f] * value
                for f, value in factor_scores.items()
                if value is not None
            )
            / available_weight
        )
        output.append(
            {
                **candidate,
                "factor_scores": factor_scores,
                "composite_score": composite,
                "available_factor_weight": available_weight,
                "metric_coverage": metric_coverage,
                "missing_factors": [
                    f for f, value in factor_scores.items() if value is None
                ],
                "factor_explanations": factor_details,
                "inclusion_reasons": [
                    "Passed requested universe and financial filters.",
                    f"{populated}/{len(active)} active factors; {metric_count}/{possible_count} active metrics available.",
                ],
            }
        )
    return sorted(
        output,
        key=lambda c: (
            -c["composite_score"],
            -c["available_factor_weight"],
            -c["metric_coverage"],
            c["company"]["symbol"],
        ),
    )


POSITIVE_METRICS = {
    "pe",
    "forward_pe",
    "price_to_book",
    "price_to_sales",
    "ev_to_ebitda",
    "earnings_yield",
}


def filter_failures(
    company: Company,
    fundamentals: Fundamentals,
    momentum: float | None,
    filters: dict[str, Any],
) -> list[str]:
    reasons: list[str] = []
    for name in ("sectors", "industries", "countries", "exchanges"):
        requested = filters.get(name)
        if requested:
            field = {
                "sectors": "sector",
                "industries": "industry",
                "countries": "country",
                "exchanges": "exchange",
            }[name]
            actual = (getattr(company, field) or "").casefold()
            if name == "countries":
                actual = country_code(getattr(company, field) or "").casefold()
                requested = [country_code(v) for v in requested]
            if not any(value.casefold() == actual for value in requested):
                reasons.append(name)
    if filters.get("keyword"):
        haystack = (
            f"{company.name} {company.sector or ''} {company.industry or ''}".casefold()
        )
        if not all(word in haystack for word in filters["keyword"].casefold().split()):
            reasons.append("keyword")
    checks = {
        "min_market_cap": (company.market_cap, True),
        "max_market_cap": (company.market_cap, False),
        "min_volume": (company.volume, True),
        "min_average_volume": (company.average_volume, True),
        "min_average_daily_value": (
            company.average_volume
            * company.price
            * (0.01 if company.currency == "GBp" else 1)
            if company.average_volume is not None and company.price is not None
            else None,
            True,
        ),
        "min_revenue_growth": (fundamentals.revenue_growth, True),
        "min_earnings_growth": (fundamentals.earnings_growth, True),
        "max_pe": (
            fundamentals.pe if fundamentals.pe and fundamentals.pe > 0 else None,
            False,
        ),
        "max_forward_pe": (
            fundamentals.forward_pe
            if fundamentals.forward_pe and fundamentals.forward_pe > 0
            else None,
            False,
        ),
        "min_net_margin": (fundamentals.net_margin, True),
        "min_roic": (fundamentals.roic, True),
        "min_free_cash_flow": (fundamentals.free_cash_flow, True),
        "max_debt_to_equity": (
            fundamentals.debt_to_equity
            if fundamentals.debt_to_equity is not None
            and fundamentals.debt_to_equity >= 0
            else None,
            False,
        ),
        "min_momentum": (momentum, True),
    }
    for key, (value, minimum) in checks.items():
        threshold = filters.get(key)
        if threshold is not None and (
            value is None or (value < threshold if minimum else value > threshold)
        ):
            reasons.append(key)
    return reasons


def passes_filters(
    company: Company,
    fundamentals: Fundamentals,
    momentum: float | None,
    filters: dict[str, Any],
) -> bool:
    return not filter_failures(company, fundamentals, momentum, filters)


async def screen(
    service: MarketService,
    trading: Trading212Client,
    filters: dict[str, Any],
    exclude_owned: bool,
    limit: int,
    strategy: str,
    factor_weights: dict[str, float] | None,
) -> dict[str, Any]:
    if strategy not in STRATEGIES:
        raise MarketError("Unknown strategy. Use quality_growth, balanced or value.")
    weights = factor_weights if factor_weights is not None else STRATEGIES[strategy]
    policy = {
        key: filters.get(key, default)
        for key, default in (
            ("min_available_factor_weight", 0.5),
            ("min_metric_coverage", 0.25),
            ("min_sector_peers", 5),
        )
    }
    rank_candidates([], weights, **policy)  # Validate before spending provider calls.
    security_types = filters.get("security_types") or ["equity"]
    if set(security_types) != {"equity"}:
        raise MarketError(
            "This provider-backed stock universe supports security_types=['equity'] only; ETF/fund discovery and stock factor ranking are not interchangeable."
        )
    if (
        filters.get("max_market_cap") is not None
        and filters.get("min_market_cap") is not None
        and filters["max_market_cap"] < filters["min_market_cap"]
    ):
        raise MarketError("max_market_cap must be at least min_market_cap.")
    warnings: list[str] = []
    positions = (
        await trading.get_positions()
        if exclude_owned or weights.get("diversification", 0) > 0
        else None
    )
    owned_isins: set[str] = (
        {p.instrument.isin for p in positions.positions} if positions else set()
    )
    if (
        exclude_owned
        and positions
        and any(not p.instrument.isin for p in positions.positions)
    ):
        raise MarketError(
            "Cannot reliably exclude owned securities because a holding lacks an ISIN."
        )
    provider_filters: dict[str, str | float] = {}
    if filters.get("max_market_cap") is not None:
        provider_filters["marketCapLowerThan"] = filters["max_market_cap"]
    if (
        filters.get("min_average_volume") is not None
        and "yfinance" in service.provider.name
    ):
        provider_filters["minAverageVolume"] = filters["min_average_volume"]
    if filters.get("min_market_cap") is not None:
        provider_filters["marketCapMoreThan"] = filters["min_market_cap"]
    for plural, singular in (
        ("sectors", "sector"),
        ("industries", "industry"),
        ("countries", "country"),
        ("exchanges", "exchange"),
    ):
        if filters.get(plural) and len(filters[plural]) == 1:
            provider_filters[singular] = (
                country_code(filters[plural][0])
                if plural == "countries"
                else filters[plural][0]
            )
    if "yfinance" in service.provider.name:
        # Yahoo region is a listing market, not the profile's issuer country.
        provider_filters.pop("country", None)
        if filters.get("regions"):
            provider_filters["regions"] = ",".join(
                str(r).lower() for r in filters["regions"]
            )
    elif filters.get("regions"):
        raise MarketError(
            "regions is a Yahoo listing-market filter; use countries/exchanges with FMP."
        )
    universe = await service.provider.universe(
        provider_filters, service.settings.screen_max_candidates
    )
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=400)
    portfolio_prices: dict[str, pd.Series] = {}
    portfolio_values: dict[str, float] = {}
    base_currency = "USD"
    if positions and positions.positions and weights.get("diversification", 0) > 0:
        account = await trading.get_account_summary()
        base_currency = account.account.currency
        for holding in positions.positions:
            try:
                if (
                    holding.walletImpact.currency != base_currency
                    or holding.walletImpact.currentValue < 0
                ):
                    raise MarketError(
                        "Holding wallet currency/value is incompatible with portfolio comparison."
                    )
                company = await service.resolver.resolve(holding.instrument)
                prices, _, _ = await service.converted_history(
                    company, start, end, base_currency
                )
                if prices.dropna().empty or cast(
                    pd.Timestamp, prices.last_valid_index()
                ).date() < end - timedelta(days=7):
                    raise MarketError("Holding price history is stale.")
                portfolio_prices[company.symbol] = prices
                portfolio_values[company.symbol] = portfolio_values.get(
                    company.symbol, 0
                ) + float(holding.walletImpact.currentValue)
            except MarketError as exc:
                warnings.append(
                    f"Diversification excludes {holding.instrument.ticker}: {exc}"
                )
    benchmark_prices = None
    benchmark_symbol = filters.get("benchmark", "SPY")
    try:
        benchmark_company = await service.provider.profile(benchmark_symbol)
        benchmark_prices, _, benchmark_notes = await service.converted_history(
            benchmark_company, start, end, base_currency
        )
        warnings.extend(
            f"Benchmark {benchmark_symbol}: {note}" for note in benchmark_notes
        )
        if benchmark_prices.dropna().empty or benchmark_prices.dropna().index[
            -1
        ].date() < end - timedelta(days=7):
            raise MarketError("Benchmark history is stale.")
    except MarketError as exc:
        benchmark_prices = None
        warnings.append(
            f"Benchmark {benchmark_symbol} unavailable; relative strength withheld: {exc}"
        )
    candidates: list[dict[str, Any]] = []
    excluded_owned = 0
    failures: list[dict[str, str]] = []
    exclusions: list[dict[str, Any]] = []
    stages = dict.fromkeys(
        (
            "ownership_unverified",
            "profile_valid",
            "universe_passed",
            "ownership_passed",
            "fundamentals_available",
            "price_history_available",
            "financial_filters_passed",
        ),
        0,
    )
    slots = asyncio.Semaphore(service.settings.fetch_concurrency)

    async def evaluate(discovered: Company) -> None:
        nonlocal excluded_owned
        async with slots:
            try:
                company = await service.provider.profile(discovered.symbol)
                stages["profile_valid"] += 1
                if (
                    company.is_etf
                    or company.is_fund
                    or company.quote_type not in (None, "EQUITY")
                ):
                    exclusions.append(
                        {
                            "symbol": company.symbol,
                            "stage": "universe",
                            "reasons": [
                                "Unsupported security type; equity stocks only."
                            ],
                        }
                    )
                    return
                universe_filters = {
                    k: v
                    for k, v in filters.items()
                    if k
                    in {
                        "sectors",
                        "industries",
                        "countries",
                        "exchanges",
                        "keyword",
                        "min_market_cap",
                        "max_market_cap",
                        "min_volume",
                        "min_average_volume",
                        "min_average_daily_value",
                    }
                }
                empty = Fundamentals(
                    source="unavailable", retrieved_at=datetime.now(timezone.utc)
                )
                rejected = filter_failures(company, empty, None, universe_filters)
                if rejected:
                    exclusions.append(
                        {
                            "symbol": company.symbol,
                            "stage": "universe",
                            "reasons": rejected,
                        }
                    )
                    return
                stages["universe_passed"] += 1
                if exclude_owned and owned_isins:
                    if not company.isin:
                        stages["ownership_unverified"] += 1
                        reason = "Candidate lacks ISIN; ownership exclusion cannot be verified."
                        failures.append({"symbol": company.symbol, "reason": reason})
                        exclusions.append(
                            {
                                "symbol": company.symbol,
                                "stage": "ownership_unverified",
                                "reasons": [reason],
                            }
                        )
                        return
                    if company.isin in owned_isins:
                        excluded_owned += 1
                        exclusions.append(
                            {
                                "symbol": company.symbol,
                                "stage": "ownership",
                                "reasons": ["ISIN matches an existing holding."],
                            }
                        )
                        return
                stages["ownership_passed"] += 1
                try:
                    fundamentals = await service.provider.fundamentals(company)
                    stages["fundamentals_available"] += 1
                except MarketError as exc:
                    fundamentals = empty
                    fundamentals.warnings.append(f"Fundamentals unavailable: {exc}")
                metrics = fundamentals.model_dump(
                    mode="json", exclude={"annual_history"}
                )
                advanced, metric_metadata, notes = financial_features(
                    fundamentals, company, end
                )
                metrics.update(advanced)
                notes.extend(fundamentals.warnings)
                if (
                    fundamentals.statement_date
                    and (end - fundamentals.statement_date).days > 550
                ):
                    notes.append(
                        "Latest financial statement is older than 550 days; current ranks may use stale statements."
                    )
                momentum: dict[str, float | None] = {
                    "momentum_6m": None,
                    "momentum_12_1": None,
                    "relative_strength": None,
                }
                price_timestamp = None
                diversification = None
                try:
                    prices, history, price_notes = await service.converted_history(
                        company, start, end, base_currency
                    )
                    notes.extend(price_notes)
                    price_timestamp = history.retrieved_at.isoformat()
                    valid = prices.dropna()
                    if valid.empty or valid.index[-1].date() < end - timedelta(days=7):
                        raise MarketError("Candidate history is stale.")
                    stages["price_history_available"] += 1
                    momentum, price_metadata = momentum_features(
                        prices, benchmark_prices
                    )
                    metric_metadata.update(
                        {
                            name: {
                                **detail,
                                "source": history.source,
                                "retrieved_at": price_timestamp,
                                "currency": base_currency,
                                **(
                                    {"benchmark": benchmark_symbol}
                                    if name == "relative_strength"
                                    else {}
                                ),
                            }
                            for name, detail in price_metadata.items()
                        }
                    )
                    if portfolio_prices and sum(portfolio_values.values()) > 0:
                        frame = pd.concat(
                            {**portfolio_prices, "__candidate__": prices},
                            axis=1,
                            sort=False,
                        ).sort_index()
                        returns = frame.pct_change(fill_method=None).dropna()
                        if len(returns) >= service.settings.min_observations:
                            portfolio_returns = pd.Series(0.0, index=returns.index)
                            for symbol, value in portfolio_values.items():
                                portfolio_returns = portfolio_returns + returns[
                                    symbol
                                ] * value / sum(portfolio_values.values())
                            if (
                                portfolio_returns.std() > 1e-15
                                and returns["__candidate__"].std() > 1e-15
                            ):
                                diversification = 1 - float(
                                    returns["__candidate__"].corr(portfolio_returns)
                                )
                except MarketError as exc:
                    notes.append(str(exc))
                rejected = filter_failures(
                    company, fundamentals, momentum["momentum_12_1"], filters
                )
                if rejected:
                    exclusions.append(
                        {
                            "symbol": company.symbol,
                            "stage": "financial_filters",
                            "reasons": rejected,
                        }
                    )
                    return
                stages["financial_filters_passed"] += 1
                metrics.update({**momentum, "diversification_benefit": diversification})
                candidates.append(
                    {
                        "company": company.model_dump(mode="json"),
                        "metrics": metrics,
                        "metric_metadata": metric_metadata,
                        "missing_metrics": [
                            name
                            for name in {
                                m
                                for entries in FACTOR_METRICS.values()
                                for m, _ in entries
                            }
                            | set(ADVANCED_METRICS)
                            if metrics.get(name) is None
                        ],
                        "source": service.provider.name,
                        "fundamentals_retrieved_at": fundamentals.retrieved_at.isoformat()
                        if fundamentals.source != "unavailable"
                        else None,
                        "prices_retrieved_at": price_timestamp,
                        "warnings": notes,
                    }
                )
            except MarketError as exc:
                failures.append({"symbol": discovered.symbol, "reason": str(exc)})
                exclusions.append(
                    {
                        "symbol": discovered.symbol,
                        "stage": "provider_or_identity",
                        "reasons": [str(exc)],
                    }
                )

    unique_companies = {c.symbol: c for c in universe.companies}
    await asyncio.gather(
        *(
            evaluate(c)
            for c in sorted(unique_companies.values(), key=lambda c: c.symbol)
        )
    )
    candidates.sort(key=lambda c: c["company"]["symbol"])
    for candidate in candidates:
        candidate["missing_metrics"].sort()
    ranked = rank_candidates(candidates, weights, exclusions=exclusions, **policy)
    exclusions.sort(key=lambda e: (e["stage"], e["symbol"]))
    failures.sort(key=lambda e: e["symbol"])
    if stages["ownership_unverified"]:
        warnings.append(
            f"Ownership could not be verified for {stages['ownership_unverified']} candidates because ISINs are missing; these candidates were excluded, not assumed unowned. Yahoo identifier gaps can leave an empty result even when other data exists."
        )
    if universe.truncated:
        warnings.append(
            "Universe was capped before all provider results were fetched; ranks apply only to the evaluated subset."
        )
    warnings.append(
        f"Sector-relative peers require {policy['min_sector_peers']} available observations per metric and definition; otherwise the evaluated same-definition universe is the peer group. Small peer groups give unstable ranks."
    )
    if any(c["missing_factors"] for c in ranked):
        warnings.append(
            "Missing factors remain null; weights are renormalised over observed factors only after minimum coverage thresholds. Partial-coverage scores are less comparable."
        )
    total_owned_value = (
        sum(float(p.walletImpact.currentValue) for p in positions.positions)
        if positions
        else 0.0
    )
    return {
        "source": service.provider.name,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "candidates": ranked[:limit],
        "coverage": {
            "description": universe.description,
            "universe_retrieved_at": universe.retrieved_at.isoformat(),
            "discovered": len(universe.companies),
            "unique_discovered": len(unique_companies),
            "provider_reported_total": universe.reported_total,
            "provider_filters": provider_filters,
            "requested_filters": filters,
            "supported_security_types": ["equity"],
            "stages": {
                "discovered": len(universe.companies),
                **stages,
                "ranking_quality_passed": len(ranked),
                "returned": min(limit, len(ranked)),
            },
            "excluded_candidates": exclusions,
            "not_returned_due_to_limit": max(0, len(ranked) - limit),
            "evaluated_and_passed_filters": len(candidates),
            "rankable": len(ranked),
            "excluded_owned": excluded_owned,
            "provider_pages": universe.pages_fetched,
            "truncated": universe.truncated,
            "failures": failures,
            "diversification_portfolio_value_fraction": sum(portfolio_values.values())
            / total_owned_value
            if total_owned_value > 0
            else None,
        },
        "methodology": {
            "strategy": strategy,
            "version": 2,
            "quality_thresholds": policy,
            "benchmark": benchmark_symbol,
            "tie_break": "Higher available factor weight, then higher active-metric coverage, then ascending listing symbol.",
            "base_currency": base_currency,
            "factor_weights": {
                key: value / sum(weights.values()) for key, value in weights.items()
            },
            "factor_metrics": FACTOR_METRICS,
            "scoring": "Winsorise metric values at 5th/95th percentiles, then midpoint percentile ranks (0–100); reverse expensive/leverage metrics. Average available metrics per factor. Weighted average over observed factors; missing factors are null, not zero. Require at least two active factors (or the sole active factor) plus configurable available-weight and metric-coverage thresholds.",
            "interpretation": "Relative screening attractiveness within evaluated provider results, not predicted returns or guaranteed outcomes.",
            "momentum": "Adjusted-close common-base-currency returns: 6m=t/t-126 minus one (127 prices); 12-1m=t-21/t-252 minus one (253 prices); relative strength=(asset six-month wealth factor / benchmark wealth factor) minus one on 127 common observed dates.",
            "financial_features": "Annual-history metrics require matching statement currency/end and consecutive annual periods; revenue CAGR needs approximately three elapsed years (actual years disclosed). ROE uses average positive equity; operating profitability uses average positive assets; cash conversion=OCF/positive net income. FCF growth requires positive prior FCF. Margin expansion=change in annual operating margin; profitability improvement=change in ROA; cash-flow improvement=change in OCF/revenue; deleveraging=decline in debt/assets; stability=three-year fraction profitable with positive OCF. Missing denominators are never zero-filled.",
            "availability": "Current-date screening only. SEC periods must be filed by retrieval date; vendor filing dates may be unknown. Restatements/current snapshots and today's universe prohibit historical point-in-time backtest claims.",
            "liquidity": "Volume is shares per provider session; average_volume uses the disclosed provider averaging window. min_average_daily_value is average shares times current share price in major quote currency: a liquidity proxy, not observed historical daily turnover or universal USD liquidity.",
            "diversification": f"1 minus daily correlation with current-weight covered holdings in {base_currency}; no optimisation or estimated future benefit.",
            "fundamentals": "Definitions and periods follow each result's metrics.source and metrics.provenance. SEC mode uses annual statements only; provider mode retains its documented snapshots/periods. No cross-provider metric fallback. Nonpositive valuation multiples and negative-equity leverage are missing.",
            "ownership": "Exclude matching ISINs across listings. Different share classes and ADRs with different ISINs are not consolidated by issuer.",
            "units": "Growth/margins/momentum are fractions. Market cap and cash-flow filters use provider-reported native currency amounts, not a universal USD threshold.",
            "survivorship": "Current provider-discovered universe; listing completeness and active/delisted coverage are provider-specific. Not a historical point-in-time universe.",
        },
        "warnings": warnings,
    }
