import pytest

from trading212_mcp.market.models import Company, Fundamentals, MarketError
from trading212_mcp.quantitative.screening import (
    passes_filters,
    rank_candidates,
    screen,
)


def candidate(symbol, growth, pe, margin, sector="Technology"):
    return {
        "company": {"symbol": symbol, "sector": sector},
        "metrics": {
            "revenue_growth": growth,
            "earnings_growth": growth,
            "pe": pe,
            "net_margin": margin,
            "roic": margin,
            "debt_to_equity": 0.5,
            "momentum_12_1": 0.1,
        },
    }


def test_factor_direction_and_missing_values():
    ranked = rank_candidates(
        [candidate("A", 0.3, 10, 0.3), candidate("B", 0.1, 30, 0.1)],
        {"growth": 0.3, "valuation": 0.3, "quality": 0.4},
    )
    assert ranked[0]["company"]["symbol"] == "A"
    assert ranked[0]["composite_score"] > ranked[1]["composite_score"]
    missing = rank_candidates(
        [{"company": {"symbol": "C", "sector": None}, "metrics": {}}], {"growth": 1}
    )
    assert missing == []


def test_negative_earnings_not_cheap():
    ranked = rank_candidates(
        [candidate("LOSS", 0.2, -5, 0.2), candidate("PROFIT", 0.2, 10, 0.2)],
        {"growth": 0.3, "valuation": 0.3, "quality": 0.4},
    )
    loss = next(c for c in ranked if c["company"]["symbol"] == "LOSS")
    assert loss["factor_scores"]["valuation"] is None
    assert ranked[0]["company"]["symbol"] == "PROFIT"


def test_sector_relative_scoring_and_ties():
    candidates = [candidate(str(i), i + 1, 10, 1, "A") for i in range(5)]
    candidates += [candidate(str(i + 5), 100 * (i + 1), 10, 1, "B") for i in range(5)]
    ranked = rank_candidates(candidates, {"growth": 0.5, "quality": 0.5})
    scores = {c["company"]["symbol"]: c["factor_scores"] for c in ranked}
    assert scores["4"]["growth"] == scores["9"]["growth"]
    assert scores["0"]["growth"] == scores["5"]["growth"]
    assert scores["0"]["quality"] == 50


@pytest.mark.parametrize(
    "weights",
    [{}, {"made_up": 1}, {"growth": -1}, {"growth": float("nan")}, {"growth": 0}],
)
def test_invalid_weights(weights):
    with pytest.raises(MarketError):
        rank_candidates([], weights)


def test_filters_fail_closed_on_missing_fundamentals():
    company = Company(symbol="A", name="A", currency="USD", sector="Technology")
    from datetime import datetime, timezone

    fundamentals = Fundamentals(
        source="fixture", retrieved_at=datetime.now(timezone.utc)
    )
    assert not passes_filters(company, fundamentals, None, {"max_pe": 20})
    assert not passes_filters(company, fundamentals, None, {"min_revenue_growth": 0.1})
    assert passes_filters(company, fundamentals, None, {"sectors": ["Technology"]})
    fundamentals.pe = -2
    assert not passes_filters(company, fundamentals, None, {"max_pe": 20})


async def test_discovers_real_provider_universe_excludes_owned(service, trading):
    result = await screen(service, trading, {}, True, 20, "quality_growth", None)
    assert [c["company"]["symbol"] for c in result["candidates"]] == ["BBB"]
    assert result["coverage"]["excluded_owned"] == 1
    assert result["coverage"]["discovered"] == 3
    assert result["candidates"][0]["factor_scores"]["momentum"] is not None


async def test_missing_prices_give_partial_factors(service, provider, trading):
    original = provider.history

    async def history(company, start, end):
        if company.symbol == "BBB":
            raise MarketError("Missing prices")
        return await original(company, start, end)

    provider.history = history
    result = await screen(service, trading, {}, True, 20, "quality_growth", None)
    assert result["candidates"][0]["factor_scores"]["momentum"] is None
    assert result["candidates"][0]["available_factor_weight"] < 1


def test_categorical_filters_do_not_match_substrings():
    from datetime import datetime, timezone

    company = Company(
        symbol="A", name="A", currency="USD", exchange="NYSEARCA", sector="Technology"
    )
    fundamentals = Fundamentals(
        source="fixture", retrieved_at=datetime.now(timezone.utc)
    )
    assert not passes_filters(company, fundamentals, None, {"exchanges": ["NYSE"]})
    assert not passes_filters(company, fundamentals, None, {"sectors": ["Tech"]})
    assert passes_filters(company, fundamentals, None, {"exchanges": ["nysearca"]})
    assert passes_filters(company, fundamentals, None, {"keyword": "tech"})


def test_partial_factor_weights_are_renormalised_not_zero_observations():
    rows = [candidate("PARTIAL", 0.2, None, 0.3)]
    result = rank_candidates(rows, {"growth": 0.25, "quality": 0.25, "valuation": 0.5})[
        0
    ]
    assert result["available_factor_weight"] == 0.5
    assert result["factor_scores"]["valuation"] is None
    assert result["composite_score"] == 50
    assert result["factor_explanations"]["valuation"]["composite_contribution"] is None
    assert result["factor_explanations"]["growth"]["effective_weight"] == 0.5
    excluded = []
    assert (
        rank_candidates(
            rows,
            {"growth": 0.25, "quality": 0.25, "valuation": 0.5},
            min_available_factor_weight=0.8,
            exclusions=excluded,
        )
        == []
    )
    assert excluded[0]["stage"] == "ranking_quality"


def test_outliers_reproducibility_and_explicit_sector_fallback():
    rows = [candidate(f"A{i:02}", i / 10, 10, 0.2) for i in range(20)]
    rows[-1]["metrics"]["revenue_growth"] = 1e10
    first = rank_candidates(rows, {"growth": 1})
    second = rank_candidates(list(reversed(rows)), {"growth": 1})
    assert first == second
    assert all(0 <= c["composite_score"] <= 100 for c in first)
    detail = first[0]["factor_explanations"]["growth"]["metric_scores"][
        "revenue_growth"
    ]
    assert detail["normalisation"] == "sector" and detail["peer_count"] == 20
    tiny = rank_candidates(rows[:2], {"growth": 1})
    assert (
        tiny[0]["factor_explanations"]["growth"]["metric_scores"]["revenue_growth"][
            "normalisation"
        ]
        == "evaluated_universe"
    )


def test_incompatible_metric_bases_do_not_share_peers():
    rows = [candidate("A", 0.1, 10, 0.1), candidate("B", 0.9, 20, 0.1)]
    for row, basis in zip(rows, ["annual", "ttm"], strict=True):
        row["metric_metadata"] = {
            "revenue_growth": {"basis": basis},
            "earnings_growth": {"basis": basis},
        }
    ranked = rank_candidates(rows, {"growth": 1})
    assert [c["composite_score"] for c in ranked] == [50, 50]
    assert (
        ranked[0]["factor_explanations"]["growth"]["metric_scores"]["revenue_growth"][
            "peer_count"
        ]
        == 1
    )


async def test_universe_liquidity_filters_report_exclusion_stages(
    service, provider, trading
):
    provider.companies["BBB"].volume = 100
    provider.companies["BBB"].average_volume = 1000
    result = await screen(
        service,
        trading,
        {"countries": ["US"], "exchanges": ["NASDAQ"], "min_average_volume": 2000},
        False,
        20,
        "quality_growth",
        None,
    )
    assert result["candidates"] == []
    rejected = result["coverage"]["excluded_candidates"]
    assert any(
        r["symbol"] == "BBB" and "min_average_volume" in r["reasons"] for r in rejected
    )
    assert result["coverage"]["stages"]["financial_filters_passed"] == 0


async def test_missing_fundamentals_can_still_support_price_only_strategy(
    service, provider, trading
):
    async def unavailable(company):
        raise MarketError("No statements for listing")

    provider.fundamentals = unavailable
    result = await screen(service, trading, {}, True, 20, "balanced", {"momentum": 1})
    row = result["candidates"][0]
    assert row["company"]["symbol"] == "BBB"
    assert row["metrics"]["net_income"] is None
    assert row["metrics"]["revenue_cagr"] is None
    assert row["fundamentals_retrieved_at"] is None
    assert any("Fundamentals unavailable" in w for w in row["warnings"])
    assert result["coverage"]["stages"]["fundamentals_available"] == 0


async def test_cross_listing_owned_isin_is_excluded(service, provider, trading):
    provider.companies["CLONE"] = provider.companies["AAA"].model_copy(
        update={"symbol": "CLONE"}
    )
    result = await screen(service, trading, {}, True, 20, "quality_growth", None)
    assert result["coverage"]["excluded_owned"] == 2
    assert all(c["company"]["symbol"] != "CLONE" for c in result["candidates"])


async def test_provider_subset_and_ranking_limit_are_explicit(
    service, provider, trading
):
    original = provider.universe

    async def capped(filters, max_candidates):
        result = await original(filters, max_candidates)
        return result.model_copy(update={"truncated": True, "reported_total": 10000})

    provider.universe = capped
    result = await screen(service, trading, {}, False, 1, "balanced", None)
    assert result["coverage"]["provider_reported_total"] == 10000
    assert result["coverage"]["discovered"] == 3
    assert result["coverage"]["truncated"]
    assert result["coverage"]["not_returned_due_to_limit"] == 1
    assert any("capped" in w for w in result["warnings"])


async def test_unsupported_security_types_and_invalid_bounds_fail_early(
    service, trading
):
    with pytest.raises(MarketError, match="equity"):
        await screen(
            service, trading, {"security_types": ["etf"]}, False, 20, "balanced", None
        )
    with pytest.raises(MarketError, match="max_market_cap"):
        await screen(
            service,
            trading,
            {"min_market_cap": 200, "max_market_cap": 100},
            False,
            20,
            "balanced",
            None,
        )


def test_native_currency_liquidity_proxy_handles_minor_units():
    from datetime import datetime, timezone

    company = Company(
        symbol="UK",
        name="UK",
        currency="GBp",
        price=250,
        volume=500,
        average_volume=1000,
    )
    fundamentals = Fundamentals(source="test", retrieved_at=datetime.now(timezone.utc))
    assert passes_filters(
        company, fundamentals, None, {"min_average_daily_value": 2500}
    )
    assert not passes_filters(
        company, fundamentals, None, {"min_average_daily_value": 2501}
    )


async def test_country_and_region_filters_are_distinct_for_foreign_listings(
    service, provider, trading
):
    # A UK issuer listed on NASDAQ must not be rejected merely for being in the US listing universe.
    provider.companies["BBB"].country = "GB"
    original = provider.universe
    seen = []

    async def universe(filters, max_candidates):
        seen.append(filters)
        return await original(filters, max_candidates)

    provider.universe = universe
    provider.name = "yfinance_fixture"
    result = await screen(
        service,
        trading,
        {"countries": ["GB"], "regions": ["us"], "exchanges": ["NASDAQ"]},
        False,
        20,
        "quality_growth",
        None,
    )
    assert [c["company"]["symbol"] for c in result["candidates"]] == ["BBB"]
    assert seen[0]["regions"] == "us"
    assert "country" not in seen[0]


async def test_live_smoke_script_reports_provider_failure_cleanly(monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    spec = importlib.util.spec_from_file_location(
        "screening_live_test",
        Path(__file__).parents[1] / "scripts" / "validate_screening_live.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = SimpleNamespace(
        sdk=SimpleNamespace(set_tz_cache_location=lambda path: None)
    )
    provider = AsyncMock()
    monkeypatch.setattr(module, "YFinanceBackend", lambda: backend)
    monkeypatch.setattr(module, "YFinanceProvider", lambda *args: provider)
    monkeypatch.setattr(
        module, "screen", AsyncMock(side_effect=MarketError("Provider unavailable"))
    )
    with pytest.raises(SystemExit) as error:
        await module.main()
    assert error.value.code == 1
    assert '"status": "live_validation_failed"' in capsys.readouterr().out
    provider.aclose.assert_awaited_once()


async def test_unverified_ownership_is_not_silently_treated_as_unowned(
    service, provider, trading
):
    provider.companies["BBB"].isin = None
    result = await screen(service, trading, {}, True, 20, "balanced", None)
    assert result["candidates"] == []
    assert result["coverage"]["stages"]["ownership_unverified"] == 1
    assert any(
        r["stage"] == "ownership_unverified" and r["symbol"] == "BBB"
        for r in result["coverage"]["excluded_candidates"]
    )
    assert any("Ownership could not be verified" in w for w in result["warnings"])


def test_country_names_and_codes_match_case_insensitively():
    from datetime import datetime, timezone

    company = Company(symbol="UK", name="UK", currency="GBP", country="GB")
    f = Fundamentals(source="test", retrieved_at=datetime.now(timezone.utc))
    assert passes_filters(company, f, None, {"countries": ["united kingdom"]})
    assert passes_filters(company, f, None, {"countries": ["gb"]})
