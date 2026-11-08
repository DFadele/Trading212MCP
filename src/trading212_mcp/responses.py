"""Deterministic presentation of complete analytical results; never recalculates risk."""

import copy
import json
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.normalization import is_list, is_mapping
from trading212_mcp.quantitative.factors import ADVANCED_METRICS

ResponseMode = Literal["summary", "detailed", "full"]
RiskComponent = Literal[
    "metrics",
    "concentration",
    "exposures",
    "contributors",
    "correlations",
    "covariance",
    "benchmark",
    "diversification",
]


def mapping(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value) if is_mapping(value) else {}


def rows(value: object) -> list[dict[str, Any]]:
    return [mapping(v) for v in value if is_mapping(v)] if is_list(value) else []


def pick(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {k: value[k] for k in names if k in value}


@dataclass(frozen=True)
class ResponseOptions:
    mode: ResponseMode = "summary"
    symbols: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()
    top_n: int = 5
    components: tuple[str, ...] = ()


def warning_groups(
    messages: object, symbols: set[str], component: str = "data_quality"
) -> list[dict[str, Any]]:
    """Group exact messages only; preserve scopes, occurrences and all unique text."""
    groups: dict[str, dict[str, Any]] = {}
    ordered_symbols = sorted(symbols, key=lambda s: (-len(s), s))
    if not is_list(messages):
        return []
    for value in messages:
        if not isinstance(value, str):
            key = "structured:" + json.dumps(value, sort_keys=True, default=str)
            group = groups.setdefault(
                key,
                {
                    "component": component,
                    "message": None,
                    "details": value,
                    "symbols": [],
                    "occurrences": 0,
                    "occurrences_by_symbol": {},
                    "unscoped_occurrences": 0,
                },
            )
            group["occurrences"] += 1
            group["unscoped_occurrences"] += 1
            continue
        symbol = next((s for s in ordered_symbols if value.startswith(s + ": ")), None)
        message = value[len(symbol) + 2 :] if symbol else value
        group = groups.setdefault(
            message,
            {
                "component": component,
                "message": message,
                "symbols": [],
                "occurrences": 0,
                "occurrences_by_symbol": {},
                "unscoped_occurrences": 0,
            },
        )
        group["occurrences"] += 1
        if symbol is None:
            group["unscoped_occurrences"] += 1
        else:
            group["occurrences_by_symbol"][symbol] = (
                group["occurrences_by_symbol"].get(symbol, 0) + 1
            )
        if symbol is not None and symbol not in group["symbols"]:
            group["symbols"].append(symbol)
    for group in groups.values():
        group["symbols"].sort()
    return list(groups.values())


class Presenter:
    def __init__(
        self, tool: str, raw: dict[str, Any], options: ResponseOptions
    ) -> None:
        self.tool, self.raw, self.options = tool, raw, options
        self.counts: dict[str, dict[str, int]] = {}
        quality = mapping(raw.get("data_quality"))
        self.sector_map: dict[str, str] = mapping(quality.get("holding_sectors"))
        self.known_symbols = set(self.sector_map)
        self.focus = set(options.symbols)
        for row in rows(mapping(raw.get("portfolio_summary")).get("holdings")):
            symbol, broker = row.get("market_symbol"), row.get("trading212_ticker")
            self.known_symbols.update(s for s in (symbol, broker) if isinstance(s, str))
            if symbol:
                self.sector_map[symbol] = row.get("sector", "Unknown")
                if broker in self.focus:
                    self.focus.add(symbol)
        for key in ("candidates", "assets"):
            for row in rows(raw.get(key)):
                company = mapping(row.get("company"))
                symbol = (
                    company.get("symbol")
                    or row.get("ticker")
                    or row.get("requested_symbol")
                )
                if symbol:
                    self.known_symbols.add(symbol)
                    self.sector_map[symbol] = company.get("sector") or "Unknown"
                    if symbol in self.focus and row.get("analysis_symbol"):
                        self.focus.add(row["analysis_symbol"])
        self.known_symbols.update(mapping(quality.get("provenance")))
        self.known_symbols.update(mapping(quality.get("holding_price_provenance")))
        self.known_symbols.update(
            mapping(mapping(raw.get("current_portfolio")).get("weights"))
        )

    def matches(self, row: dict[str, Any]) -> bool:
        company = mapping(row.get("company"))
        names = {
            v
            for v in (
                row.get("symbol"),
                row.get("market_symbol"),
                row.get("trading212_ticker"),
                row.get("ticker"),
                row.get("requested_symbol"),
                company.get("symbol"),
            )
            if isinstance(v, str)
        }
        if self.focus and not names & self.focus:
            return False
        sector = (
            row.get("sector")
            or company.get("sector")
            or next(
                (self.sector_map[s] for s in sorted(names) if s in self.sector_map),
                "Unknown",
            )
        )
        return not self.options.sectors or str(sector).casefold() in {
            s.strip().casefold() for s in self.options.sectors
        }

    def select(
        self, values: list[dict[str, Any]], label: str, *, limit: bool = False
    ) -> list[dict[str, Any]]:
        matched = [r for r in values if self.matches(r)]
        result = (
            matched[: self.options.top_n]
            if limit and self.options.mode == "summary"
            else matched
        )
        self.counts[label] = {
            "total": len(values),
            "matching": len(matched),
            "returned": len(result),
        }
        return result

    def weights(self, value: object, label: str) -> dict[str, Any]:
        weights = mapping(value)
        ordered = sorted(
            ({"symbol": s, "weight": w} for s, w in weights.items()),
            key=lambda r: (-float(r["weight"]), r["symbol"]),
        )
        selected = self.select(ordered, label, limit=True)
        shown = {r["symbol"]: r["weight"] for r in selected}
        return {
            "shown": shown,
            "other_weight": math.fsum(
                float(v) for k, v in weights.items() if k not in shown
            ),
            "total_weight": math.fsum(float(v) for v in weights.values()),
        }

    def contributions(self, value: object, label: str) -> dict[str, Any]:
        values = sorted(
            rows(value),
            key=lambda r: (
                -abs(float(r.get("annualised_variance_contribution") or 0)),
                str(r.get("symbol")),
            ),
        )
        selected = self.select(values, label, limit=True)
        available = [r for r in values if r.get("fraction_of_variance") is not None]
        shown = math.fsum(float(r.get("fraction_of_variance") or 0) for r in selected)
        total = math.fsum(float(r["fraction_of_variance"]) for r in available)
        hedges = [
            r
            for r in values
            if float(r.get("annualised_variance_contribution") or 0) < 0
            and self.matches(r)
        ]
        return {
            "largest_absolute_contributors": selected,
            "largest_hedges": hedges[: self.options.top_n],
            "shown_fraction_of_variance": shown if available else None,
            "remaining_fraction_of_variance": total - shown if available else None,
        }

    def provenance(self, value: object) -> dict[str, Any]:
        values = mapping(value)
        times = sorted(
            str(r["retrieved_at"])
            for r in values.values()
            if is_mapping(r) and r.get("retrieved_at")
        )
        return {
            "asset_count": len(values),
            "oldest_retrieved_at": times[0] if times else None,
            "newest_retrieved_at": times[-1] if times else None,
            "price_bases": sorted(
                {
                    str(r["price_basis"])
                    for r in values.values()
                    if is_mapping(r) and r.get("price_basis")
                }
            ),
            "currencies": sorted(
                {
                    str(r["currency"])
                    for r in values.values()
                    if is_mapping(r) and r.get("currency")
                }
            ),
        }

    def quality(self, value: object) -> dict[str, Any]:
        original = mapping(value)
        result = {k: v for k, v in original.items() if k != "holding_sectors"}
        if "warnings" in result:
            result["warnings"] = warning_groups(result["warnings"], self.known_symbols)
        if self.options.mode == "summary":
            for key in ("provenance", "holding_price_provenance"):
                if key in result:
                    result[key + "_summary"] = self.provenance(result.pop(key))
        if self.focus or self.options.sectors:
            for key in ("provenance", "holding_price_provenance", "holding_sectors"):
                if key in result:
                    result[key] = {
                        s: v
                        for s, v in mapping(result[key]).items()
                        if self.matches({"symbol": s})
                    }
        return result

    def fundamentals(self, value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        full = mapping(value)
        return pick(
            full,
            (
                "source",
                "retrieved_at",
                "statement_date",
                "statement_currency",
                "provenance",
                "revenue_growth",
                "earnings_growth",
                "pe",
                "forward_pe",
                "price_to_book",
                "price_to_sales",
                "net_margin",
                "operating_margin",
                "roic",
                "debt_to_equity",
                "free_cash_flow",
            ),
        )

    def company(self, value: object) -> dict[str, Any] | None:
        if value is None:
            return None
        return pick(
            mapping(value),
            (
                "symbol",
                "name",
                "currency",
                "isin",
                "sector",
                "industry",
                "country",
                "is_etf",
                "market_cap",
                "exchange",
                "quote_type",
                "volume",
                "average_volume",
                "average_volume_period",
            ),
        )

    def model_metrics(self, value: object, label: str) -> dict[str, Any] | None:
        if value is None:
            return None
        result = dict(mapping(value))
        if "risk_contributions" in result:
            result["risk_contributions"] = self.contributions(
                result["risk_contributions"], label
            )
        return result

    def correlations(self) -> dict[str, Any]:
        matrix = {
            s: mapping(row) for s, row in mapping(self.raw.get("correlations")).items()
        }
        pairs: list[dict[str, Any]] = []
        weights: dict[str, float] = {}
        for holding in rows(mapping(self.raw.get("portfolio_summary")).get("holdings")):
            symbol = holding.get("market_symbol")
            if symbol:
                weights[symbol] = weights.get(symbol, 0) + float(
                    holding.get("weight", 0)
                )
        undefined = 0
        symbols = sorted(matrix)
        for i, left in enumerate(symbols):
            for right in symbols[i + 1 :]:
                value = matrix[left].get(right)
                if value is None:
                    undefined += 1
                    continue
                if (self.focus or self.options.sectors) and not (
                    self.matches({"symbol": left}) or self.matches({"symbol": right})
                ):
                    continue
                pairs.append(
                    {
                        "left": left,
                        "right": right,
                        "correlation": value,
                        "combined_full_portfolio_weight": weights.get(left, 0)
                        + weights.get(right, 0),
                    }
                )
        self.counts["correlation_pairs"] = {
            "total": len(symbols) * (len(symbols) - 1) // 2,
            "matching": len(pairs),
            "returned": min(
                self.options.top_n, sum(p["correlation"] > 0 for p in pairs)
            )
            + min(self.options.top_n, sum(p["correlation"] < 0 for p in pairs)),
        }
        return {
            "highest_positive": sorted(
                (p for p in pairs if p["correlation"] > 0),
                key=lambda p: (-p["correlation"], p["left"], p["right"]),
            )[: self.options.top_n],
            "most_negative": sorted(
                (p for p in pairs if p["correlation"] < 0),
                key=lambda p: (p["correlation"], p["left"], p["right"]),
            )[: self.options.top_n],
            "undefined_pairs_in_full_universe": undefined,
        }

    def exposures(self, values: object) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for category, table in mapping(values).items():
            all_rows: list[dict[str, Any]] = sorted(
                ({"name": s, "weight": v} for s, v in mapping(table).items()),
                key=lambda r: (-float(r["weight"]), str(r["name"])),
            )
            eligible = [
                r
                for r in all_rows
                if category != "sector"
                or not self.options.sectors
                or r["name"].casefold()
                in {s.strip().casefold() for s in self.options.sectors}
            ]
            selected = eligible[: self.options.top_n]
            output[category] = {
                "largest_exposures": selected,
                "other_weight": math.fsum(float(r["weight"]) for r in all_rows)
                - math.fsum(float(r["weight"]) for r in selected),
            }
            self.counts[category + "_exposures"] = {
                "total": len(all_rows),
                "matching": len(eligible),
                "returned": len(selected),
            }
        return output

    def risk(self) -> dict[str, Any]:
        raw = self.raw
        result = pick(
            raw,
            (
                "source",
                "retrieved_at",
                "risk_metrics",
                "benchmark_comparison",
                "concentration_analysis",
                "diversification_ratio",
                "methodology",
            ),
        )
        portfolio = dict(mapping(raw.get("portfolio_summary")))
        holdings = sorted(
            rows(portfolio.get("holdings")),
            key=lambda r: (-r["weight"], r["trading212_ticker"]),
        )
        portfolio["holdings_count"] = len(holdings)
        portfolio["holdings"] = self.select(holdings, "holdings", limit=True)
        result["portfolio_summary"] = portfolio
        result["data_quality"] = self.quality(raw.get("data_quality"))
        result["asset_risk_contributions"] = self.contributions(
            raw.get("asset_risk_contributions"), "risk_contributors"
        )
        result["diversification_analysis"] = self.exposures(
            raw.get("diversification_analysis")
        )
        result["correlations"] = self.correlations()
        return self.risk_components(result)

    def risk_components(self, result: dict[str, Any]) -> dict[str, Any]:
        if not self.options.components:
            return result
        names = {
            "concentration_analysis": "concentration",
            "diversification_analysis": "exposures",
            "asset_risk_contributions": "contributors",
            "correlations": "correlations",
            "annualised_covariance": "covariance",
            "benchmark_comparison": "benchmark",
            "diversification_ratio": "diversification",
        }
        return {
            k: v
            for k, v in result.items()
            if k not in names or names[k] in self.options.components
        }

    def scenario(self, value: object, label: str) -> dict[str, Any] | None:
        if value is None:
            return None
        source = mapping(value)
        result = {
            k: v
            for k, v in source.items()
            if k not in {"post_investment_weights", "analysed_weights"}
        }
        for field in ("post_investment_weights", "analysed_weights"):
            if field in source:
                result[field] = self.weights(source[field], label + "." + field)
        return result

    def candidate(self) -> dict[str, Any]:
        raw = self.raw
        result = pick(
            raw, ("source", "retrieved_at", "currency", "comparison", "assumptions")
        )
        result["data_quality"] = self.quality(raw.get("data_quality"))
        current = dict(mapping(raw.get("current_portfolio")))
        for key in ("weights", "analysed_weights"):
            if key in current:
                current[key] = self.weights(current[key], "current." + key)
        result["current_portfolio"] = current
        candidates: list[dict[str, Any]] = []
        for row in self.select(rows(raw.get("candidates")), "candidates"):
            entry = pick(
                row,
                (
                    "ticker",
                    "analysis_symbol",
                    "status",
                    "missing_fundamentals",
                    "price_provenance",
                ),
            )
            entry["company"] = self.company(row.get("company"))
            entry["fundamentals"] = self.fundamentals(row.get("fundamentals"))
            entry["analysis"] = self.scenario(
                row.get("analysis"), str(row.get("ticker"))
            )
            candidates.append(entry)
        result["candidates"] = candidates
        result["allocated_scenario"] = self.scenario(
            raw.get("allocated_scenario"), "allocated"
        )
        result["cash_comparison"] = self.scenario(raw.get("cash_comparison"), "cash")
        return result

    def allocation(
        self, value: object, label: str, *, card: bool = False
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        original = mapping(value)
        omitted = {"resulting_portfolio_weights", "modelled_metrics", "risk_budgeting"}
        result = {k: v for k, v in original.items() if k not in omitted}
        model = mapping(original.get("modelled_metrics"))
        result["modelled_metrics"] = (
            {k: v for k, v in model.items() if k != "risk_contributions"}
            if card and model
            else self.model_metrics(
                original.get("modelled_metrics"), label + ".contributors"
            )
        )
        if "resulting_portfolio_weights" in original and not card:
            result["resulting_portfolio_weights"] = self.weights(
                original["resulting_portfolio_weights"], label + ".weights"
            )
        budget = mapping(original.get("risk_budgeting"))
        if budget:
            result["risk_budgeting"] = {
                k: v for k, v in budget.items() if k != "targets"
            }
            contributions = rows(
                mapping(original.get("modelled_metrics")).get("risk_contributions")
            )
            achieved = {
                r["symbol"]: r.get("fraction_of_variance") for r in contributions
            }
            deviations: list[dict[str, Any]] = sorted(
                (
                    {
                        "symbol": s,
                        "target": v,
                        "achieved": achieved.get(s),
                        "absolute_deviation": abs((achieved.get(s) or 0) - v),
                    }
                    for s, v in mapping(budget.get("targets")).items()
                ),
                key=lambda r: (-float(r["absolute_deviation"]), str(r["symbol"])),
            )
            result["risk_budgeting"]["target_count"] = len(deviations)
            result["risk_budgeting"]["largest_deviations"] = self.select(
                deviations, label + ".budget_deviations", limit=True
            )
        return result

    def optimisation(self) -> dict[str, Any]:
        raw = self.raw
        result = {
            k: v
            for k, v in raw.items()
            if k
            not in {
                "current_portfolio",
                "recommended",
                "method_comparisons",
                "comparisons",
                "data_quality",
            }
        }
        result["data_quality"] = self.quality(raw.get("data_quality"))
        current = dict(mapping(raw.get("current_portfolio")))
        if "weights" in current:
            current["weights"] = self.weights(current["weights"], "current.weights")
        current["modelled_metrics"] = self.model_metrics(
            current.get("modelled_metrics"), "current.contributors"
        )
        result["current_portfolio"] = current
        result["recommended"] = self.allocation(raw.get("recommended"), "recommended")
        methods = {}
        for name, row in mapping(raw.get("method_comparisons")).items():
            methods[name] = (
                {"status": mapping(row).get("status"), "same_as_recommended": True}
                if raw.get("recommended") and name == raw.get("objective")
                else self.allocation(row, name, card=True)
            )
        result["method_comparisons"] = methods
        comparisons = mapping(raw.get("comparisons"))
        result["comparisons"] = {
            k: self.allocation(v, k, card=True)
            for k, v in comparisons.items()
            if k != "individual_candidates"
        }
        if "individual_candidates" in comparisons:
            result["comparisons"]["individual_candidates"] = {
                k: self.allocation(v, k, card=True)
                for k, v in mapping(comparisons["individual_candidates"]).items()
                if self.matches({"symbol": k})
            }
        return result

    def market(self) -> dict[str, Any]:
        result = pick(self.raw, ("source", "requested_at", "requested_period"))
        selected: list[dict[str, Any]] = []
        for row in self.select(rows(self.raw.get("assets")), "assets"):
            entry = {
                k: v
                for k, v in row.items()
                if k not in {"company", "fundamentals", "warnings"}
            }
            entry["company"] = self.company(row.get("company"))
            if "fundamentals" in row:
                entry["fundamentals"] = self.fundamentals(row["fundamentals"])
            selected.append(entry)
        result["assets"] = selected
        return result

    def screening(self) -> dict[str, Any]:
        raw = self.raw
        result = pick(raw, ("source", "retrieved_at", "coverage", "methodology"))
        result["warnings"] = warning_groups(
            raw.get("warnings"), self.known_symbols, "screening"
        )
        candidates: list[dict[str, Any]] = []
        metric_names = (
            "source",
            "retrieved_at",
            "statement_date",
            "statement_currency",
            "provenance",
            "revenue_growth",
            "earnings_growth",
            "pe",
            "forward_pe",
            "price_to_book",
            "price_to_sales",
            "net_margin",
            "roic",
            "debt_to_equity",
            "free_cash_flow",
            "momentum_12_1",
            "diversification_benefit",
            "momentum_6m",
            "relative_strength",
            *ADVANCED_METRICS,
        )
        for row in self.select(rows(raw.get("candidates")), "candidates"):
            entry = pick(
                row,
                (
                    "factor_scores",
                    "composite_score",
                    "available_factor_weight",
                    "metric_coverage",
                    "inclusion_reasons",
                    "missing_factors",
                    "missing_metrics",
                    "source",
                    "fundamentals_retrieved_at",
                    "prices_retrieved_at",
                ),
            )
            entry["company"] = self.company(row.get("company"))
            entry["metrics"] = pick(mapping(row.get("metrics")), metric_names)
            entry["factor_explanations"] = {}
            for factor, detail in mapping(row.get("factor_explanations")).items():
                detail = mapping(detail)
                observed = [
                    mapping(v)
                    for v in mapping(detail.get("metric_scores")).values()
                    if mapping(v).get("score") is not None
                ]
                entry["factor_explanations"][factor] = {
                    **pick(detail, ("effective_weight", "composite_contribution")),
                    "observed_metric_count": len(observed),
                    "metric_count": len(mapping(detail.get("metric_scores"))),
                    "normalisation_scopes": sorted(
                        {str(v["normalisation"]) for v in observed}
                    ),
                    "peer_count_range": [
                        min(v["peer_count"] for v in observed),
                        max(v["peer_count"] for v in observed),
                    ]
                    if observed
                    else None,
                }
            # Missing names remain explicit without repeating null advanced values.
            entry["metrics"] = {
                name: value
                for name, value in entry["metrics"].items()
                if name not in ADVANCED_METRICS or value is not None
            }
            period_groups: dict[str, dict[str, Any]] = {}
            for metric, metadata in mapping(row.get("metric_metadata")).items():
                if mapping(metadata).get("available") is False:
                    continue
                key = json.dumps(metadata, sort_keys=True)
                group = period_groups.setdefault(
                    key, {**mapping(metadata), "metrics": []}
                )
                group["metrics"].append(metric)
            entry["metric_metadata_groups"] = list(period_groups.values())
            candidates.append(entry)
        result["candidates"] = candidates
        return result

    def record_diagnostics(self) -> dict[str, Any]:
        messages: dict[str, list[str]] = {"records": [], "fundamentals": []}
        issues: list[dict[str, Any]] = []
        for key in ("assets", "candidates"):
            for row in rows(self.raw.get(key)):
                company = mapping(row.get("company"))
                symbol = (
                    row.get("ticker")
                    or row.get("requested_symbol")
                    or company.get("symbol")
                    or "Unknown"
                )
                self.known_symbols.add(symbol)
                for component, warnings in (
                    ("records", row.get("warnings")),
                    ("fundamentals", mapping(row.get("fundamentals")).get("warnings")),
                ):
                    if is_list(warnings):
                        messages[component].extend(f"{symbol}: {w}" for w in warnings)
                problem = pick(
                    row,
                    (
                        "status",
                        "missing_data",
                        "missing_fundamentals",
                        "missing_factors",
                        "missing_metrics",
                    ),
                )
                if any(v for k, v in problem.items() if k != "status") or problem.get(
                    "status"
                ) not in (None, "available"):
                    issues.append({"symbol": symbol, **problem})
        failures: dict[str, Any] = {}
        comparisons = mapping(self.raw.get("comparisons"))
        all_comparisons = {
            **mapping(self.raw.get("method_comparisons")),
            **{k: v for k, v in comparisons.items() if k != "individual_candidates"},
            **mapping(comparisons.get("individual_candidates")),
        }
        for name, original in all_comparisons.items():
            row = mapping(original)
            if row.get("constraint_compliant") is False or row.get("status") not in (
                None,
                "optimal",
            ):
                failures[name] = pick(
                    row, ("status", "solver_status", "constraint_violations")
                )
        return {
            "issues": issues,
            "warnings": [
                group
                for component, values in messages.items()
                for group in warning_groups(values, self.known_symbols, component)
            ],
            "comparison_failures": failures,
        }

    def filter_nested(self, value: Any, path: str = "") -> Any:
        if is_mapping(value):
            output: dict[str, Any] = {}
            for key, child in value.items():
                label = path + "." + key
                if key in {"correlations", "annualised_covariance"}:
                    output[key] = child
                elif key in {
                    "weights",
                    "analysed_weights",
                    "post_investment_weights",
                    "resulting_portfolio_weights",
                    "targets",
                } and is_mapping(child):
                    entries = [{"symbol": s, "weight": v} for s, v in child.items()]
                    output[key] = {
                        r["symbol"]: r["weight"] for r in self.select(entries, label)
                    }
                elif key == "risk_contributions" and is_list(child):
                    output[key] = self.select(rows(child), label)
                else:
                    output[key] = self.filter_nested(child, label)
            return output
        if is_list(value):
            return [self.filter_nested(v, path + f"[{i}]") for i, v in enumerate(value)]
        return value

    def expanded(self) -> dict[str, Any]:
        result = copy.deepcopy(
            {
                k: v
                for k, v in self.raw.items()
                if self.options.mode != "detailed"
                or k not in {"correlations", "annualised_covariance"}
            }
        )
        if self.tool == "analyse_portfolio_risk":
            if self.options.mode == "detailed":
                result.pop("annualised_covariance", None)
                result.pop("correlations", None)
            portfolio = mapping(result.get("portfolio_summary"))
            portfolio["holdings"] = self.select(
                rows(portfolio.get("holdings")), "holdings"
            )
            result["asset_risk_contributions"] = self.select(
                rows(result.get("asset_risk_contributions")), "risk_contributors"
            )
            for key in ("correlations", "annualised_covariance"):
                explicitly_requested = (
                    key == "annualised_covariance"
                    and "covariance" in self.options.components
                )
                focused = bool(self.focus or self.options.sectors)
                if key in self.raw and (
                    self.options.mode == "full"
                    or explicitly_requested
                    or (focused and key == "correlations")
                ):
                    result[key] = {
                        s: v
                        for s, v in mapping(self.raw[key]).items()
                        if not focused or self.matches({"symbol": s})
                    }
            result = self.risk_components(result)
        for key in ("assets", "candidates"):
            if key in result:
                result[key] = self.select(rows(result[key]), key)
        if self.options.mode == "detailed":
            if "data_quality" in result:
                result["data_quality"] = self.quality(result["data_quality"])
            if "warnings" in result:
                result["warnings"] = warning_groups(
                    result["warnings"], self.known_symbols
                )
        if self.focus or self.options.sectors:
            result = self.filter_nested(result)
        return result

    def render(self) -> dict[str, Any]:
        if self.options.mode != "summary":
            return self.expanded()
        return {
            "analyse_portfolio_risk": self.risk,
            "analyse_candidate_investment": self.candidate,
            "optimise_new_contribution": self.optimisation,
            "get_market_data": self.market,
            "screen_stocks": self.screening,
        }[self.tool]()


@dataclass
class Report:
    tool: str
    signature: str
    created: float
    data: dict[str, Any]


class ResearchResponses:
    """Eight ten-minute reports, scoped to this single-account server instance.

    No disk persistence or cross-server sharing. Cache IDs never trigger refresh.
    """

    def __init__(self, ttl: float = 600, max_entries: int = 8) -> None:
        self.ttl, self.max_entries = ttl, max_entries
        self.reports: OrderedDict[str, Report] = OrderedDict()

    async def respond(
        self,
        tool: str,
        arguments: dict[str, Any],
        compute: Callable[[], Awaitable[dict[str, Any]]],
        *,
        response_mode: ResponseMode = "summary",
        detail_symbols: list[str] | None = None,
        detail_sectors: list[str] | None = None,
        top_n: int = 5,
        risk_components: Sequence[str] | None = None,
        report_id: str | None = None,
    ) -> dict[str, Any]:
        if response_mode not in ("summary", "detailed", "full") or not 1 <= top_n <= 20:
            raise MarketError(
                "Invalid response mode or top_n; use summary/detailed/full and 1–20."
            )
        if (
            response_mode == "summary"
            and risk_components
            and "covariance" in risk_components
        ):
            raise MarketError(
                "Covariance matrices require response_mode=detailed or full; use detail_symbols for selected rows."
            )
        signature = json.dumps(
            arguments, sort_keys=True, allow_nan=False, separators=(",", ":")
        )
        now = time.monotonic()
        for key in list(self.reports):
            if now - self.reports[key].created >= self.ttl:
                del self.reports[key]
        if report_id is not None:
            report = self.reports.get(report_id)
            if report is None or report.tool != tool:
                raise MarketError(
                    "Report unavailable, expired or from another tool; rerun without report_id."
                )
            if report.signature != signature:
                raise MarketError(
                    "Report inputs differ; repeat the original analysis inputs or omit report_id to calculate a new report."
                )
            self.reports.move_to_end(report_id)
        else:
            raw = await compute()
            report = Report(tool, signature, time.monotonic(), raw)
            report_id = secrets.token_urlsafe(12)
            self.reports[report_id] = report
            while len(self.reports) > self.max_entries:
                self.reports.popitem(last=False)
        options = ResponseOptions(
            response_mode,
            tuple(detail_symbols or []),
            tuple(detail_sectors or []),
            top_n,
            tuple(risk_components or []),
        )
        if response_mode == "full" and not (
            detail_symbols or detail_sectors or risk_components
        ):
            return report.data
        presenter = Presenter(tool, report.data, options)
        result = presenter.render()
        result["record_diagnostics"] = presenter.record_diagnostics()
        currency = report.data.get("currency") or mapping(
            report.data.get("portfolio_summary")
        ).get("currency")
        result["response"] = {
            "mode": response_mode,
            "schema_version": 1,
            "report_id": report_id,
            "expires_in_seconds": max(
                0, math.floor(self.ttl - (time.monotonic() - report.created))
            ),
            "detail_symbols": detail_symbols or [],
            "detail_sectors": detail_sectors or [],
            "risk_components": risk_components or [],
            "counts": presenter.counts,
            "scope": "Metrics/rankings describe the original complete calculation and its stated coverage. Filters select displayed details only; weights/contributions are not renormalised.",
            "full_result_available": True,
        }
        result["units"] = {
            "portfolio_currency": currency,
            "weights_growth_margins_volatility_and_daily_losses": "fractions; 0.10 means 10%",
            "ratios_and_beta": "dimensionless",
            "variance_contributions": "annual return variance",
            "money": "Named portfolio currencies; fundamental amounts retain statement_currency; market cap uses company.currency (major units).",
            "missing": "null means unavailable/undefined, never zero; omitted rows/fields are display choices, with full results available.",
        }
        return result
