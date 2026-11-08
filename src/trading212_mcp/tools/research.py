import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from trading212_mcp.market.models import MarketError
from trading212_mcp.market.service import MarketService
from trading212_mcp.quantitative.candidates import analyse_candidates
from trading212_mcp.quantitative.optimisation import Objective, optimise_contribution
from trading212_mcp.quantitative.screening import screen
from trading212_mcp.responses import ResearchResponses, ResponseMode, RiskComponent
from trading212_mcp.trading212.client import Trading212Client
from trading212_mcp.trading212.errors import Trading212Error

Symbol = Annotated[
    str, Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9.^=_:/-]+$")
]
Lookback = Annotated[int, Field(ge=1, le=10)]
FilterList = Annotated[
    list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=10)
]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
AllocationFraction = Annotated[
    float, Field(gt=0, le=1, allow_inf_nan=False, strict=True)
]

BoundFraction = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]


@asynccontextmanager
async def risk_progress(ctx: Context[Any, Any]) -> AsyncGenerator[None, None]:
    try:
        meta = ctx.request_context.meta
    except ValueError:
        meta = None  # Direct in-process calls have no protocol request context.
    if meta is None or meta.progressToken is None:
        yield
        return

    async def heartbeat() -> None:
        updates = 1
        while True:
            await ctx.report_progress(
                updates,
                message="Fetching portfolio holdings and market histories for risk analysis.",
            )
            updates += 1
            await asyncio.sleep(5)

    task = asyncio.create_task(heartbeat())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def register_research_tools(
    server: FastMCP,
    get_client: Callable[[], Trading212Client],
    get_market: Callable[[], MarketService],
) -> None:
    responses = ResearchResponses()

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_market_data(
        tickers: Annotated[list[Symbol], Field(min_length=1, max_length=20)],
        lookback_years: Lookback = 3,
        include_fundamentals: bool = True,
        include_series: bool = False,
        base_currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")] | None = None,
        response_mode: ResponseMode = "summary",
        detail_symbols: Annotated[list[Symbol], Field(max_length=100)] | None = None,
        detail_sectors: FilterList | None = None,
        top_n: Annotated[int, Field(ge=1, le=20)] = 5,
        report_id: Annotated[
            str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Get real adjusted market histories and fundamentals for provider symbols.

        Works for companies outside the Trading 212 portfolio and benchmark ETFs.
        Use provider listing symbols (e.g. AAPL, SPY), not Trading 212 identifiers.
        Defaults to compact summaries; include_series explicitly returns daily
        adjusted prices and simple returns. base_currency converts price history
        via daily FX; statement amounts retain their reported currencies.
        Missing/unsupported data is identified, never fabricated.
        Response modes: summary (default), detailed (individual records without
        default dense matrices), full (legacy complete result). detail_symbols/
        detail_sectors filter displayed records only, not calculations or rankings.
        top_n bounds explanatory top lists, not explicitly requested tickers/limit.
        Reuse report_id with original analysis inputs for the same cached snapshot
        (8 reports/10 minutes per server). Full without filters restores old fields.
        """
        try:
            return await responses.respond(
                "get_market_data",
                {
                    "tickers": tickers,
                    "lookback_years": lookback_years,
                    "include_fundamentals": include_fundamentals,
                    "include_series": include_series,
                    "base_currency": base_currency,
                },
                lambda: get_market().market_data(
                    tickers,
                    lookback_years,
                    include_fundamentals,
                    include_series,
                    base_currency,
                ),
                response_mode=response_mode,
                detail_symbols=detail_symbols,
                detail_sectors=detail_sectors,
                top_n=top_n,
                report_id=report_id,
            )
        except MarketError as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def analyse_portfolio_risk(
        ctx: Context[Any, Any],
        lookback_years: Lookback = 3,
        benchmark: Symbol = "SPY",
        annual_risk_free_rate: Annotated[
            float, Field(gt=-1, le=1, allow_inf_nan=False)
        ] = 0.0,
        confidence: Annotated[float, Field(gt=0.5, lt=1, allow_inf_nan=False)] = 0.95,
        listing_selections: dict[str, Symbol] | None = None,
        response_mode: ResponseMode = "summary",
        detail_symbols: Annotated[list[Symbol], Field(max_length=100)] | None = None,
        detail_sectors: FilterList | None = None,
        top_n: Annotated[int, Field(ge=1, le=20)] = 5,
        report_id: Annotated[
            str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
        ]
        | None = None,
        risk_components: Annotated[list[RiskComponent], Field(max_length=8)]
        | None = None,
    ) -> dict[str, Any]:
        """Analyse current Trading 212 holdings using deterministic daily risk metrics.

        Resolves ISIN/currency, converts prices to account currency and computes
        volatility, Sharpe, Sortino, drawdown, historical VaR/CVaR, correlations,
        annual covariance, benchmark beta and asset variance contributions.
        This is a hypothetical current-weight, daily-rebalanced backtest excluding
        cash, NOT actual historical portfolio performance. Reports exclusions,
        coverage and methodology. benchmark is a provider symbol.
        For ambiguous mappings, ask the user which verified candidate listing to
        use, then pass listing_selections={Trading212_ticker: Yahoo_symbol}.
        Only verified matches are accepted; choices are cached for later calls.
        Response modes: summary (default), detailed (individual records without
        default dense matrices), full (legacy complete result). detail_symbols/
        detail_sectors filter displayed records only, not calculations or rankings.
        top_n bounds explanatory top lists, not explicitly requested tickers/limit.
        Reuse report_id with original analysis inputs for the same cached snapshot
        (8 reports/10 minutes per server). Full without filters restores old fields.
        """
        try:
            async with risk_progress(ctx):
                return await responses.respond(
                    "analyse_portfolio_risk",
                    {
                        "lookback_years": lookback_years,
                        "benchmark": benchmark,
                        "annual_risk_free_rate": annual_risk_free_rate,
                        "confidence": confidence,
                        "listing_selections": listing_selections,
                    },
                    lambda: get_market().portfolio_risk(
                        get_client(),
                        lookback_years,
                        benchmark,
                        annual_risk_free_rate,
                        confidence,
                        listing_selections,
                    ),
                    response_mode=response_mode,
                    detail_symbols=detail_symbols,
                    detail_sectors=detail_sectors,
                    top_n=top_n,
                    report_id=report_id,
                    risk_components=risk_components,
                )
        except (MarketError, Trading212Error) as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def analyse_candidate_investment(
        ctx: Context[Any, Any],
        tickers: Annotated[list[Symbol], Field(min_length=1, max_length=10)],
        investment_amount_gbp: Annotated[
            float, Field(gt=0, allow_inf_nan=False, strict=True)
        ],
        lookback_years: Lookback = 3,
        benchmark: Symbol = "SPY",
        compare_cash: bool = False,
        allocations: dict[Symbol, AllocationFraction] | None = None,
        use_historical_mean_returns: bool = False,
        annual_risk_free_rate: Annotated[
            float, Field(gt=-1, le=1, allow_inf_nan=False)
        ] = 0.0,
        annual_cash_return: Annotated[
            float, Field(gt=-1, le=1, allow_inf_nan=False)
        ] = 0.0,
        confidence: Annotated[float, Field(gt=0.5, lt=1, allow_inf_nan=False)] = 0.95,
        listing_selections: dict[str, Symbol] | None = None,
        response_mode: ResponseMode = "summary",
        detail_symbols: Annotated[list[Symbol], Field(max_length=100)] | None = None,
        detail_sectors: FilterList | None = None,
        top_n: Annotated[int, Field(ge=1, le=20)] = 5,
        report_id: Annotated[
            str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Compare hypothetical externally funded GBP investments with current holdings.

        Each provider ticker independently receives the full investment amount.
        Optional allocations are positive fractions summing to one and add a
        joint scenario. compare_cash adds an assumed GBP cash alternative.
        Uses common observed historical dates, adjusted prices, daily GBP FX and
        existing risk analytics. Reports full versus covered-subset weights,
        concentration, exposures, volatility, beta, downside risk and marginal
        contributions. Partial coverage is explicit. Sharpe/change is withheld
        unless use_historical_mean_returns=true; historical means are not forecasts.
        No orders, optimisation, forecast returns or opaque recommendation score.
        listing_selections accepts only verified held-security choices, as in
        analyse_portfolio_risk. Existing cash and transaction costs are excluded.
        Response modes: summary (default), detailed (individual records without
        default dense matrices), full (legacy complete result). detail_symbols/
        detail_sectors filter displayed records only, not calculations or rankings.
        top_n bounds explanatory top lists, not explicitly requested tickers/limit.
        Reuse report_id with original analysis inputs for the same cached snapshot
        (8 reports/10 minutes per server). Full without filters restores old fields.
        """
        try:
            async with risk_progress(ctx):
                return await responses.respond(
                    "analyse_candidate_investment",
                    {
                        "tickers": tickers,
                        "investment_amount_gbp": investment_amount_gbp,
                        "lookback_years": lookback_years,
                        "benchmark": benchmark,
                        "compare_cash": compare_cash,
                        "allocations": allocations,
                        "use_historical_mean_returns": use_historical_mean_returns,
                        "annual_risk_free_rate": annual_risk_free_rate,
                        "annual_cash_return": annual_cash_return,
                        "confidence": confidence,
                        "listing_selections": listing_selections,
                    },
                    lambda: analyse_candidates(
                        get_market(),
                        get_client(),
                        tickers,
                        investment_amount_gbp,
                        lookback_years,
                        benchmark,
                        compare_cash,
                        allocations,
                        use_historical_mean_returns,
                        annual_risk_free_rate,
                        annual_cash_return,
                        confidence,
                        listing_selections,
                    ),
                    response_mode=response_mode,
                    detail_symbols=detail_symbols,
                    detail_sectors=detail_sectors,
                    top_n=top_n,
                    report_id=report_id,
                )
        except (MarketError, Trading212Error) as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def optimise_new_contribution(
        ctx: Context[Any, Any],
        investment_amount_gbp: Annotated[
            float, Field(gt=0, allow_inf_nan=False, strict=True)
        ],
        tickers: Annotated[list[Symbol], Field(min_length=1, max_length=10)],
        objective: Objective = "minimum_volatility",
        lookback_years: Lookback = 3,
        max_position_weight: AllocationFraction = 1.0,
        sector_limits: dict[
            Annotated[str, Field(min_length=1, max_length=100)], BoundFraction
        ]
        | None = None,
        min_purchase_allocations: dict[Symbol, BoundFraction] | None = None,
        max_purchase_allocations: dict[Symbol, BoundFraction] | None = None,
        allow_cash: bool = False,
        min_cash_allocation: BoundFraction = 0.0,
        max_cash_allocation: BoundFraction = 1.0,
        risk_budgets: dict[Symbol, BoundFraction] | None = None,
        listing_selections: dict[str, Symbol] | None = None,
        response_mode: ResponseMode = "summary",
        detail_symbols: Annotated[list[Symbol], Field(max_length=100)] | None = None,
        detail_sectors: FilterList | None = None,
        top_n: Annotated[int, Field(ge=1, le=20)] = 5,
        report_id: Annotated[
            str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Optimise only new GBP capital while retaining all current positions.

        Objectives: minimum_volatility, risk_parity/risk_budgeting, or
        maximum_diversification. CVXPY solves the resulting WHOLE portfolio
        using robust GBP covariance, not the candidate sub-portfolio alone.
        Position and sector caps apply after funding; purchase min/max bounds
        are fractions of new money. Cash is optional with separate bounds.
        Default risk budgets are equal across feasible risky securities;
        custom budgets use resolved symbols and sum to one (cash budget zero).
        Complete held-asset history is required; missing candidates can receive
        zero. Infeasible limits or failed solver validation return no recommendation.
        Comparisons include all objectives, equal weight, single purchases and
        optional cash; infeasible comparison baselines are explicitly marked.
        No sales, trade execution, forecast returns or maximum-Sharpe guesses.
        Response modes: summary (default), detailed (individual records without
        default dense matrices), full (legacy complete result). detail_symbols/
        detail_sectors filter displayed records only, not calculations or rankings.
        top_n bounds explanatory top lists, not explicitly requested tickers/limit.
        Reuse report_id with original analysis inputs for the same cached snapshot
        (8 reports/10 minutes per server). Full without filters restores old fields.
        """
        try:
            async with risk_progress(ctx):
                return await responses.respond(
                    "optimise_new_contribution",
                    {
                        "investment_amount_gbp": investment_amount_gbp,
                        "tickers": tickers,
                        "objective": objective,
                        "lookback_years": lookback_years,
                        "max_position_weight": max_position_weight,
                        "sector_limits": sector_limits,
                        "min_purchase_allocations": min_purchase_allocations,
                        "max_purchase_allocations": max_purchase_allocations,
                        "allow_cash": allow_cash,
                        "min_cash_allocation": min_cash_allocation,
                        "max_cash_allocation": max_cash_allocation,
                        "risk_budgets": risk_budgets,
                        "listing_selections": listing_selections,
                    },
                    lambda: optimise_contribution(
                        get_market(),
                        get_client(),
                        investment_amount_gbp,
                        tickers,
                        objective,
                        lookback_years,
                        max_position_weight,
                        sector_limits,
                        min_purchase_allocations,
                        max_purchase_allocations,
                        allow_cash,
                        min_cash_allocation,
                        max_cash_allocation,
                        risk_budgets,
                        listing_selections,
                    ),
                    response_mode=response_mode,
                    detail_symbols=detail_symbols,
                    detail_sectors=detail_sectors,
                    top_n=top_n,
                    report_id=report_id,
                )
        except (MarketError, Trading212Error) as exc:
            raise ToolError(str(exc)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def screen_stocks(
        sectors: FilterList | None = None,
        min_market_cap: Positive | None = None,
        exclude_owned: bool = True,
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        strategy: Literal["quality_growth", "balanced", "value"] = "quality_growth",
        industries: FilterList | None = None,
        countries: FilterList | None = None,
        exchanges: FilterList | None = None,
        keyword: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
        min_revenue_growth: Finite | None = None,
        min_earnings_growth: Finite | None = None,
        max_pe: Positive | None = None,
        max_forward_pe: Positive | None = None,
        min_net_margin: Finite | None = None,
        min_roic: Finite | None = None,
        min_free_cash_flow: Finite | None = None,
        max_debt_to_equity: Annotated[float, Field(ge=0, allow_inf_nan=False)]
        | None = None,
        min_momentum: Finite | None = None,
        factor_weights: dict[str, Finite] | None = None,
        regions: FilterList | None = None,
        max_market_cap: Positive | None = None,
        min_volume: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = None,
        min_average_volume: Annotated[float, Field(ge=0, allow_inf_nan=False)]
        | None = None,
        min_average_daily_value: Annotated[float, Field(ge=0, allow_inf_nan=False)]
        | None = None,
        security_types: Annotated[
            list[Literal["equity", "etf", "fund"]], Field(min_length=1, max_length=3)
        ]
        | None = None,
        benchmark: Symbol = "SPY",
        min_available_factor_weight: Annotated[
            float, Field(ge=0, le=1, allow_inf_nan=False)
        ] = 0.5,
        min_metric_coverage: Annotated[
            float, Field(ge=0, le=1, allow_inf_nan=False)
        ] = 0.25,
        min_sector_peers: Annotated[int, Field(ge=2, le=100)] = 5,
        response_mode: ResponseMode = "summary",
        detail_symbols: Annotated[list[Symbol], Field(max_length=100)] | None = None,
        detail_sectors: FilterList | None = None,
        top_n: Annotated[int, Field(ge=1, le=20)] = 5,
        report_id: Annotated[
            str, Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
        ]
        | None = None,
    ) -> dict[str, Any]:
        """Discover and rank stocks from the provider's real company-screener universe.

        Excludes owned ISINs by default. Filters growth, valuation, profitability,
        leverage, free cash flow and momentum; keyword matches company name,
        sector and industry text. Rates/growth/margins use fractions, not percent.
        min_market_cap and min_free_cash_flow use native provider currencies.
        Optional factor_weights keys: growth, valuation, quality, momentum,
        diversification, financial_improvement. Scores are relative ranks,
        not return predictions. Annual history supplies CAGR, cash-flow growth,
        margin/profitability/cash-flow changes, ROE and leverage when supported.
        Benchmark-relative strength uses common adjusted-price dates. Country
        filters use ISO codes or supported country names; exchanges use provider
        identifiers. Yahoo regions explicitly choose listing markets separately
        from profile country (issuer domicile). Equity discovery only; ETF/fund requests fail explicitly.
        min_average_daily_value is an average-volume times current-price proxy
        in major quote currency, not measured daily turnover or universal USD.
        Missing factors stay null; observed weights are renormalised only after
        configured factor/metric coverage thresholds. Full mode includes per-
        metric peer counts, percentiles and availability explanations.
        Coverage, candidate caps, missing factors and freshness are disclosed.
        Response modes: summary (default), detailed (individual records without
        default dense matrices), full (legacy complete result). detail_symbols/
        detail_sectors filter displayed records only, not calculations or rankings.
        top_n bounds explanatory top lists, not explicitly requested tickers/limit.
        Reuse report_id with original analysis inputs for the same cached snapshot
        (8 reports/10 minutes per server). Full without filters restores old fields.
        """
        filters = {
            "sectors": sectors,
            "min_market_cap": min_market_cap,
            "industries": industries,
            "countries": countries,
            "exchanges": exchanges,
            "keyword": keyword,
            "min_revenue_growth": min_revenue_growth,
            "min_earnings_growth": min_earnings_growth,
            "max_pe": max_pe,
            "max_forward_pe": max_forward_pe,
            "min_net_margin": min_net_margin,
            "min_roic": min_roic,
            "min_free_cash_flow": min_free_cash_flow,
            "max_debt_to_equity": max_debt_to_equity,
            "min_momentum": min_momentum,
            "regions": regions,
            "max_market_cap": max_market_cap,
            "min_volume": min_volume,
            "min_average_volume": min_average_volume,
            "min_average_daily_value": min_average_daily_value,
            "security_types": security_types,
            "benchmark": benchmark,
            "min_available_factor_weight": min_available_factor_weight,
            "min_metric_coverage": min_metric_coverage,
            "min_sector_peers": min_sector_peers,
        }
        try:
            return await responses.respond(
                "screen_stocks",
                {
                    "filters": filters,
                    "exclude_owned": exclude_owned,
                    "limit": limit,
                    "strategy": strategy,
                    "factor_weights": factor_weights,
                },
                lambda: screen(
                    get_market(),
                    get_client(),
                    filters,
                    exclude_owned,
                    limit,
                    strategy,
                    factor_weights,
                ),
                response_mode=response_mode,
                detail_symbols=detail_symbols,
                detail_sectors=detail_sectors,
                top_n=top_n,
                report_id=report_id,
            )
        except (MarketError, Trading212Error) as exc:
            raise ToolError(str(exc)) from None
