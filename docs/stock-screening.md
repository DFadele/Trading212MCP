# Stock discovery and factor ranking

`screen_stocks` remains the single discovery tool. All 12 MCP tools remain read-only, with existing names, arguments, response modes and top-level fields retained. Additional optional inputs and result fields extend the existing tool; no broker operations or recommendation agent were added.

## Universe and coverage

Discovery uses Yahoo's custom EquityQuery or FMP's company-screener, followed by profile enrichment and local verification. No manual stock list is used. Yahoo sorts discovery by descending market capitalisation; a capped scan consequently favours larger companies. FMP's provider ordering also affects a capped scan. Neither provider's subset is the global equity market or an unbiased sample of it.

- `regions`: optional Yahoo **listing-market** codes, e.g. `["us", "gb"]`; otherwise `MARKET_YAHOO_REGIONS` (default `["us"]`). This is distinct from company domicile. FMP rejects this Yahoo-specific control; use its country/exchange filters.
- `countries`: issuer/profile country, ISO codes or supported country names. Yahoo applies this locally, because its region query is not a domicile filter. A UK-domiciled company listed in the US can belong to region `us` and country `GB` simultaneously. For UK listings use `regions=["gb"]`; setting country alone does not change listing regions.
- `exchanges`, `sectors`, `industries`: exact case-insensitive local matches, using provider identifiers. A single requested value is pushed upstream where supported. Yahoo recognises the existing NASDAQ/NYSE exchange aliases. Country names are canonicalised for matching, but sector/industry vocabularies remain provider-specific.
- `min_market_cap`, `max_market_cap`: native provider major quote-currency amounts; these are not globally comparable USD thresholds.
- `min_volume`: provider-reported current/latest-session shares. `min_average_volume`: average shares over the reported averaging window (Yahoo three months; FMP provider-defined).
- `min_average_daily_value`: **liquidity proxy**, average shares × current share price in major quote currency (GBp prices divided by 100). This is neither measured historical turnover nor a universally USD-denominated liquidity statistic. Missing volume/price fails a requested filter.
- `security_types`: currently supports `["equity"]`, including provider-classified equity listings. ETF/fund discovery is not supported by this stock-universe path; explicit ETF/fund requests fail informatively rather than returning an incomplete stock subset. Preferred shares, ADRs and REITs are not reliably classified separately by these adapters; use available industry/identity data without assuming subtype completeness.
- Existing keyword and financial filters remain available and fail closed when their required metric is unavailable.

`MARKET_SCREEN_MAX_CANDIDATES` defaults to 40 (allowed 1–200). Yahoo fetches pages up to 100 records and discloses the provider's reported match count; FMP uses its existing bounded pagination but does not supply a trustworthy total. `limit` controls the returned ranked results, not the universe fetched. Filters applied after the provider cap can produce few/no matches even when matching companies exist elsewhere. Multi-valued industry/sector/exchange filters are local; narrow the listing universe to improve coverage.

`coverage` retains the previous fields and adds `provider_reported_total` (null when unknown), `unique_discovered`, `provider_filters`, `requested_filters`, `supported_security_types`, `stages`, `excluded_candidates`, and `not_returned_due_to_limit`. Stages count valid profiles, universe matches, ownership checks passed, returned fundamentals blocks, usable fresh price histories, financial filters passed, quality-qualified ranks and final results. Fundamentals availability counts returned blocks, not completeness of every metric. Price/fundamental availability are side-stage counts, not mandatory sequential gates for every strategy.

Exclusions identify the listing symbol, stage and reason/filter names. A requested financial/volume filter fails on absent data. Ranking-quality exclusions additionally report available-factor weight and active-metric coverage. Provider/identity failures remain in `coverage.failures`. Lists of excluded candidates, provider truncation and warnings are never top-N limited by response presentation.

Owned positions are excluded by verified ISIN across listings using the existing Trading 212 integration. Missing candidate ISINs fail ownership verification when holdings exist; `stages.ownership_unverified` and a global warning report the resulting exclusions, which can leave an empty Yahoo result. The experimental yfinance ISIN scraper is not used as verified identity evidence. A portfolio with missing holding ISINs cannot claim verified exclusion. Different share classes/ADRs with different ISINs are not consolidated by issuer. Discovery does not establish that every returned listing is tradable on Trading 212.

## Financial features

Adapters add up to four dated annual observations to the existing `Fundamentals` model. SEC uses annual 10-K/10-K/A facts with the same accession and currency within a report, preferring the latest filed report per fiscal end. Yahoo joins income/balance/cash fields on exact fiscal ends. FMP joins exact income end/currency and withholds mismatching amounts. Known future fiscal ends/filing dates are excluded. Vendor records with unknown filing dates are disclosed and are not historical point-in-time data.

| Factor | Metrics and definitions | Availability rules |
| --- | --- | --- |
| Growth | Existing annual revenue and earnings growth; approximately three-year revenue CAGR; annual FCF growth; operating-margin expansion | CAGR needs consecutive same-currency annual records spanning 2.5–3.5 years and positive endpoints; actual elapsed years disclosed. Growth needs a positive prior denominator; loss-to-profit changes are not invented percentage growth. Margin expansion is current operating income/revenue minus prior. |
| Valuation | Positive P/E, forward P/E, price/book, price/sales, EV/EBITDA; earnings yield; annual FCF yield | Earnings yield = 1/positive vendor P/E. EV/EBITDA is an explicit vendor snapshot multiple. FCF yield = latest annual FCF/current profile market cap only if currencies match in major units. Negative FCF yield is retained and ranks poorly; nonpositive multiples are not treated as cheap valuations. |
| Quality | Net margin, ROIC, ROE, operating profitability, cash conversion, debt/equity, debt/assets | ROE = annual net income/average positive equity; operating profitability = annual operating income/average positive assets. Both need comparable current/prior records. Cash conversion = OCF/positive net income. Debt ratios require reliable reported debt/equity/assets; missing components stay null. ROE is a separate field, never labelled ROIC. |
| Momentum | Six-month momentum, twelve-month momentum excluding the most recent month, benchmark-relative strength | Adjusted prices in a common base currency. Definitions below. |
| Financial improvement | Change in ROA; change in OCF/revenue; deleveraging; financial stability | Profitability improvement = change in annual net income/assets. Cash-flow improvement = change in annual OCF/revenue. Deleveraging = prior debt/assets minus current. Stability = fraction of three consecutive years with positive net income **and** OCF; all six observations must be present. These are descriptive proxies, not solvency guarantees. |
| Diversification | Existing 1 − historical daily correlation with covered current-weight holdings | Requires sufficient common returns and nonzero volatility; partial portfolio value coverage is explicit. This is not a forecast benefit or optimisation. |

Annual comparisons require the selected statement end/currency to match the history, and adjacent annual ends 300–430 days apart. Conflicting duplicate fiscal ends withhold history features. Missing/nonfinite values and zero/negative required denominators are null, never synthetic zeros. Cash conversion can be negative with negative OCF; negative earnings/ROE/margins are preserved where their definitions remain meaningful. Annual statements older than 550 days generate warnings.

Default Yahoo+SEC uses SEC annual fundamentals as a whole block. SEC cannot supply vendor P/E/forward P/E/EV multiples or a reliable consolidated debt definition in this adapter; these remain null. Its annual equity/assets/earnings/OCF may support ROE and profitability features. Annual FCF may be absent even for a large company when supported concepts or same-accession contexts are incomplete. No Yahoo/FMP per-metric financial fallback is introduced. The explicit annual-FCF/current-market-cap calculation identifies its annual periods and price-provider market-cap input; differing statement/quote currencies withhold it.

Provider-financial mode can supply Yahoo snapshot multiples and aligned yearly statements. FMP remains optional, with its existing credentials/entitlements; four statement years are requested but only supplied years are used. Free/personal Yahoo data is not a promise of licensed cloud redistribution or production availability. See [provider costs and licensing](market-data.md) and [provider comparison](provider-comparison.md).

## Momentum

For observed adjusted prices ending at index `t`:

- `momentum_6m = P[t] / P[t−126] − 1`, requiring 127 prices.
- `momentum_12_1 = P[t−21] / P[t−252] − 1`, requiring 253 prices. Changes in the last 21 prices do not affect this feature.
- `relative_strength = (P[t]/P[t−126]) / (B[t]/B[t−126]) − 1` on the last 127 **common observed dates** with `benchmark` (default `SPY`). No price or benchmark forward-filling is used to create observations.

These are trading-observation approximations, not fixed calendar-month intervals; actual endpoints/counts are supplied. Price histories use provider split/dividend-adjusted prices and existing daily FX conversion. A history more than seven calendar days stale is withheld; insufficient histories leave the affected metric null. Benchmark failure withholds relative strength, preserving other factors.

Base currency is the account currency when diversification uses real positions; otherwise USD. The response states it. Quote currencies, reported statement currencies and liquidity/filter currencies retain their separate meanings.

## Ranking methodology version 2

1. Start with candidates passing profile, universe, ownership and requested financial filters. This pool defines peers before final ranking-quality exclusions. Repeated listing symbols are deduplicated, and asynchronous enrichment order does not affect ranks.
2. Within each metric definition/basis, winsorise at the peer group's 5th/95th percentiles. Use sector-relative peers for financial factors if at least `min_sector_peers` (default 5, configurable 2–100) valid observations exist; otherwise use the evaluated same-definition universe. Unknown sectors fall back. Momentum/diversification use the evaluated same-definition universe. No annual feature is pooled with a differently labelled snapshot/TTM definition.
3. Midpoint percentile score = `(average_rank − 0.5) / valid_peer_count × 100`. Expensive multiples and leverage reverse direction. Ties receive equal scores, and a lone peer gets 50; small groups remain statistically weak. Percentile scores are not return forecasts.
4. Factor score = arithmetic mean of available metric percentiles. Every metric has equal weight **inside its factor**; configurable `factor_weights` control the factors. Some metrics share information (e.g. P/E and earnings yield); this is disclosed, not an independent-signal claim.
5. Require at least two populated active factors (or the sole factor in an explicitly single-factor model), `min_available_factor_weight` (default 0.5) and `min_metric_coverage` (default 0.25). Metric coverage is available valid observations divided by all metric slots in active factors. These are independent coverage thresholds.
6. Composite score = sum(configured weight × observed factor score) / sum(observed configured weights). Missing factors remain null and do not become zero-valued observations. Effective weights and available weight are explicit. Partial-coverage scores are less comparable; increase thresholds when completeness matters.
7. Sort by descending composite, then descending available-factor weight, descending active-metric coverage, and ascending symbol. No random or LLM scoring is involved. The result is reproducible for identical data/settings, not immutable across refreshed/restated provider data.

Existing presets and their requested factor weights remain; expanded metric sets change the factor scores. `financial_improvement` is an additional configurable weight, default zero in the existing presets. Full `factor_explanations` records each metric's valid value, percentile, peer count, normalisation scope, definition/basis and missing reason, plus factor/effective weights and composite contributions. Summary mode retains effective weights, score contributions, observed/total metric counts, normalisation scopes and peer-count ranges, omitting the per-metric detail table. Grouped metadata describes available metrics’ periods without repeating identical period records. Unavailable advanced fields are listed in `missing_metrics` rather than repeating null values; legacy metric fields retain their existing null representation. Detailed/full expose the complete explanations. Coverage and warnings remain unchanged in every mode.

**Methodology migration:** version 1 gave absent factors zero composite contribution. Version 2 renormalises over observed factors, adds the metric-coverage threshold and the advanced features. Existing calls and JSON field types remain valid, but scores/ranks are intentionally different. Do not compare v1 and v2 scores as a time series or treat scores as probabilities/predicted returns. The top-level presentation schema version remains separate from `methodology.version`.

This is **current-date screening**, not a historical backtest. SEC facts must have been filed by retrieval date, but latest restatements can revise history. Yahoo/FMP annual filing availability can be unknown, forward multiples are vendor estimates, and the universe contains today's discoveries. The tool does not accept a historical as-of date or claim point-in-time reconstruction; using current ratios/universes to simulate old investment decisions would introduce look-ahead/survivorship bias.

## Example MCP queries

UK-listed industrial equities, minimum average volume, excluding held securities:

```json
{
  "regions": ["gb"],
  "countries": ["GB"],
  "sectors": ["Industrials"],
  "min_average_volume": 100000,
  "exclude_owned": true,
  "limit": 10
}
```

The default SEC fundamentals source does not cover most non-US issuers. For that experiment explicitly configure `MARKET_FUNDAMENTALS_SOURCE=provider` to use Yahoo's financial block, then restart the server. No source is switched automatically.

A configurable growth/quality/improvement model:

```json
{
  "regions": ["us"],
  "min_market_cap": 1000000000,
  "min_average_volume": 500000,
  "benchmark": "SPY",
  "factor_weights": {
    "growth": 0.25,
    "valuation": 0.15,
    "quality": 0.25,
    "financial_improvement": 0.20,
    "momentum": 0.15
  },
  "min_available_factor_weight": 0.75,
  "min_metric_coverage": 0.4,
  "limit": 10
}
```

For complete factor explanations repeat the same scientific inputs and add `"response_mode":"full"`; optionally pass the returned `response.report_id` to reuse that snapshot. Symbol/sector display filters still select presentation details rather than recalculate rankings. See [response modes](response-modes.md). Candidate symbols can then be supplied to `analyse_candidate_investment` or `optimise_new_contribution` for portfolio-specific quantitative comparison.

## Validation and remaining limits

On 2026-11-07 two real public-data smoke checks succeeded: Yahoo discovery/profiles/yearly statements/adjusted prices/ranking, and Yahoo discovery/prices plus SEC annual fundamentals/ranking. Each evaluated only three provider-selected US listings (NVDA, MSFT, AAPL); the provider reported 19,742 matches and both runs disclosed truncation. These identifiers are validation evidence, not hardcoded candidates or recommendations. SEC and Yahoo reported different fiscal ends for some issuers; no attempt was made to merge them. Missing SEC valuation/debt/FCF metrics remained unavailable.

Validation result: **269 pytest tests passed, 2 existing tests skipped**; Ruff check/format and strict Pyright passed (zero errors/warnings).

Automated tests exercise numerical financial examples, denominator failures, future filings, mismatched currencies/periods, short histories, momentum endpoints, sector fallback, negative earnings, outliers, reproducibility, ownership across listings, liquidity, caps/exclusions, input bounds and summary/full consistency. Existing cache/rate-limit/failure tests continue to run. Yahoo uses its bounded worker pool, request pacing, raw-response TTL cache and coalesced identical requests; candidates are enriched with bounded concurrency. FMP/SEC reuse their existing caches/pacing; no new paid dependency is added. Whole-report drill-down caching is unchanged. Cold scans can take several minutes under vendor pacing; lower `MARKET_SCREEN_MAX_CANDIDATES` and configure a sufficient client timeout. Reducing `limit` alone does not reduce the fetched universe.

Unverified live in this change: FMP entitlements/endpoints, non-US discovery/FX/financial coverage, a complete paginated universe, live portfolio ownership exclusion and large cold scans. The US three-stock checks are not evidence of global completeness, provider production reliability, or historical point-in-time correctness.

Run the bounded public-data checks (no Trading 212 requests):

```sh
uv run --locked python scripts/validate_screening_live.py
uv run --locked python scripts/validate_screening_live.py --fundamentals-source sec
```

The SEC option requires the configured real `MARKET_SEC_USER_AGENT` contact. The script explicitly selects the requested source, limits discovery to three US listings, disables ownership/diversification account access, isolates temporary caches and fails if usable live data is not obtained. Network access and vendor availability are required.

Primary interface references: [yfinance EquityQuery](https://ranaroussi.github.io/yfinance/reference/api/yfinance.EquityQuery.html), [yfinance screen and pagination controls](https://ranaroussi.github.io/yfinance/reference/api/yfinance.screen.html), and [SEC XBRL API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
