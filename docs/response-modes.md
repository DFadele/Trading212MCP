# Research response modes

All 12 tools remain registered and read-only. The five research tools now default to `response_mode="summary"`: `get_market_data`, `analyse_portfolio_risk`, `screen_stocks`, `analyse_candidate_investment`, and `optimise_new_contribution`.

## Migration

**The research default response shapes have changed.** Existing clients that parse the previous JSON must add `"response_mode": "full"`. With no display filters or `risk_components`, full returns the original complete result shape, without a presentation envelope. Sector/industry fields on resolved risk holdings and the candidate/optimiser `data_quality.holding_sectors` map are additive metadata; calculations and scientific inputs are unchanged. Existing validation scripts and legacy contract tests explicitly request full mode.

The seven brokerage tools keep their existing schemas and defaults. Account summary is already small. Positions, allocation and pending orders describe complete account records; dropping them implicitly could misrepresent holdings or orders. The three history tools already offer `limit` and cursors: request a smaller page when appropriate and do not treat it as the entire history. The audit below includes all seven.

## Modes and controls

| Parameter | Behaviour |
| --- | --- |
| `response_mode="summary"` | Deterministic Python projection of the complete calculation; bounded holdings/contributors/exposures and compact comparisons. No dense matrices. |
| `response_mode="detailed"` | Complete individual records and weights, with risk matrices omitted unless requested as described below. |
| `response_mode="full"` | Complete original data, including matrices. Leave display filters/components unset for the legacy shape. |
| `top_n=5` | Summary bound for holdings, weight maps, risk contributors, hedges, correlation extremes, exposure categories and risk-budget deviations; range 1–20. Does **not** truncate explicitly requested candidate/market rows or the screening result limit. |
| `detail_symbols=[...]` | Select displayed holdings/candidates/records and their weight/contribution details. Risk accepts resolved provider symbols or Trading 212 tickers. |
| `detail_sectors=[...]` | Select displayed records by case-insensitive sector, including `Unknown`; combine with symbols to intersect the filters. |
| `risk_components=[...]` | Risk only: select concentration, exposures, contributors, correlations, covariance, benchmark or diversification sections; core portfolio/coverage/methodology and available risk metrics remain visible. `metrics` names the always-visible metrics. |
| `report_id="..."` | Retrieve another view of the same stored calculation without new provider/account requests. Repeat the **original scientific inputs**, including non-default values. Only presentation controls may change. |

Filters do not change the universe, recalculate statistics, normalise weights, or change rankings and recommendations. Global metrics continue to describe the original calculation and its explicitly stated coverage. `response.counts` distinguishes total, matching and returned rows. Summary weight maps contain `shown`, `other_weight` and `total_weight`; omitted weights are never redistributed.

A detailed risk request with a symbol/sector filter returns the selected correlation **rows against all original columns**, so relationships with other holdings remain visible. A detailed request for `risk_components=["correlations"]` without a filter omits the matrix; add a filter or use full mode. Covariance is returned only in full mode or an explicit detailed `risk_components=["covariance"]` request. Without a filter that explicit request returns the entire covariance matrix. Summary covariance requests fail with an informative error instead of returning a large matrix unexpectedly.

Summary and detailed responses add `response` (presentation schema version 1) and `units`. Full with filters/components is a scoped view and also adds them. Full without filters/components preserves the legacy envelope.

### Content and semantics

- Risk: unchanged portfolio metrics, benchmark, concentration and diversification ratio; largest holdings/exposures; largest absolute variance contributions and negative hedges; strongest positive and most negative unique off-diagonal correlations, with combined portfolio weights. These correlation lists describe extremes, not an investment ranking. Remaining exposure/weight/variance shares are explicit.
- Candidate analysis: every candidate's status, before/after risk metrics, metric changes, exposure changes, assumptions, comparison findings and selected fundamentals; repeated portfolio weight maps are bounded.
- Optimisation: complete purchase decisions, solver status, feasibility evidence, constraint violations/binding constraints, before/after metrics and assumptions. Comparison cards omit repeated resulting weights and contributor lists; detailed/full restore them. The chosen method card references `recommended` rather than repeating it. Risk-budget summaries show target count and the largest deviations; full retains all targets.
- Screening: every returned candidate, original factor scores, composite score, available-factor weight, missing factors, screening coverage and methodology. Profile/fundamental fields are selected for research interpretation, with complete records in detailed/full.
- Market data: every requested asset, price/return summary, missing-data status, selected company/fundamental fields and original period/adjustment metadata. An explicit `include_series=true` is honoured in **every** mode and can still produce large responses.

Presentation never rounds or re-estimates financial metrics. Numbers remain at engine precision. Weights, growth, margins, volatility and daily loss measures are fractions (0.10 means 10%), ratios/beta/correlations are dimensionless, and variance contributions are annual return variance. Fundamental money retains `statement_currency`; market cap is in company currency major units. `null` means unavailable/undefined, never zero. Fields omitted by a mode are display choices, recoverable in full mode. The original methodology and observation start/end/count fields remain available in summaries. Annual/TTM/snapshot distinctions retain provider provenance and warnings rather than being inferred from statement dates.

Coverage/exclusions and feasibility warnings are never top-N limited or scoped away. Repeated warning strings are grouped by exact text, component and affected symbols, preserving occurrence counts (including unscoped occurrences). `record_diagnostics` inventories missing data and per-record warnings from the **unfiltered original result**, plus failed comparison statuses/constraint violations. Summary provenance for risk price histories aggregates retrieval times, currencies and price bases; detailed/full retain per-symbol records. Summary fundamentals retain reporting-period provenance; all underlying raw metrics are available in full mode.

### Stable drill-down example

First call `analyse_portfolio_risk`:

```json
{"lookback_years": 3, "benchmark": "SPY"}
```

Use its `response.report_id` to retrieve NVDA's correlation row and risk contribution from that exact calculation:

```json
{
  "lookback_years": 3,
  "benchmark": "SPY",
  "report_id": "COPY_RETURNED_ID",
  "response_mode": "detailed",
  "detail_symbols": ["NVDA"],
  "risk_components": ["correlations", "contributors"]
}
```

For all original matrices and records:

```json
{
  "lookback_years": 3,
  "benchmark": "SPY",
  "report_id": "COPY_RETURNED_ID",
  "response_mode": "full"
}
```

For a fresh complete candidate analysis:

```json
{
  "tickers": ["NVDA", "APH", "GOOGL"],
  "investment_amount_gbp": 2000,
  "response_mode": "full"
}
```

Reports are held in memory for ten minutes, at most eight across the five tools per server instance. They are tied to a tool and exact scientific-input values. IDs expire on restart, expiry or eviction; unknown/mismatched IDs fail and **never silently recompute**. Omit the ID for a fresh calculation. A view does not refresh data freshness or extend its expiry. This is a single-account instance cache, not a multi-tenant authorisation mechanism; hosted wrappers must maintain account isolation and affinity to the same server instance. Reports never persist to disk.

## Measurement method

`tests/response_fixture.py` supplies a reproducible, seeded synthetic portfolio: 58 holdings worth £100,000 plus £2,000 cash, six sectors, USD/EUR/GBp prices, varying daily FX, 756 business sessions, 24 outside-portfolio discovery candidates, and a benchmark. Twenty market/screen results and five candidate/optimisation choices are requested. Brokerage history pages contain 50 realistic model records including order fills/taxes. This exercises the real registered MCP functions, analytics and numerical solver, with no network or credentials. It is **not live-market validation** and cannot measure Yahoo/SEC/Trading 212 response times.

Token counts use the clearly labelled approximation **ceil(UTF-8 bytes / 4)**, not a model-specific tokenizer. Numeric JSON may differ substantially from this estimate; these are not billing counts. Compare byte reductions directly. `CallToolResult` sizes include both SDK text `content` and `structuredContent`, excluding JSON-RPC framing. Clients may consume one or both representations. Payload-only bytes/tokens and indented line counts are also retained in the JSON reports.

The archived [before report](benchmarks/response-baseline-58.json) was captured before introducing modes. The [after report](benchmarks/response-modes-58.json) includes current full, summary, detailed, cached full and targeted risk views; all 12 input/output schemas were measured. Research tools have dynamic dictionary output schemas; mode-specific shapes are defined above and tested. No output validation or SDK text representation was removed to achieve the reductions.

Reproduce the current benchmark (offline):

```sh
uv run --locked python scripts/benchmark_responses.py --output /tmp/response-modes-58.json
```

The archived pre-change report is an historical baseline; rerunning the current script measures current full versus summary, not an old checkout. The script prints both projection call timing and timing including the audit's compact/pretty payload and MCP serializations. Cached timings are seven paired repetitions on the same report. Fresh calls also execute calculations, but the second call benefits from provider/CPU caches, so their difference must not be attributed solely to presentation. There is no claim that summary mode reduces provider fetching or solver time.


### Measured sizes

All bytes below are serialized MCP result bytes (text plus structured content); tokens are approximations as defined above. Baseline and default brokerage values are unchanged.

| Tool | Before bytes | Before ≈tokens | Default bytes | Default ≈tokens | Reduction | Detailed bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| get_account_summary | 891 | 223 | 891 | 223 | 0.0% | — |
| get_positions | 67,635 | 16,909 | 67,635 | 16,909 | 0.0% | — |
| get_portfolio_allocation | 26,342 | 6,586 | 26,342 | 6,586 | 0.0% | — |
| get_pending_orders | 9,385 | 2,347 | 9,385 | 2,347 | 0.0% | — |
| get_order_history | 110,770 | 27,693 | 110,770 | 27,693 | 0.0% | — |
| get_dividend_history | 43,040 | 10,760 | 43,040 | 10,760 | 0.0% | — |
| get_transaction_history | 15,138 | 3,785 | 15,138 | 3,785 | 0.0% | — |
| get_market_data | 99,930 | 24,983 | 86,982 | 21,746 | 13.0% | 112,645 |
| analyse_portfolio_risk | 551,409 | 137,853 | 31,729 | 7,933 | 94.2% | 91,817 |
| screen_stocks | 105,499 | 26,375 | 85,329 | 21,333 | 19.1% | 119,707 |
| analyse_candidate_investment | 166,910 | 41,728 | 78,154 | 19,539 | 53.2% | 157,846 |
| optimise_new_contribution | 375,893 | 93,974 | 61,715 | 15,429 | 83.6% | 379,843 |

Risk payloads fall from 8,266 indented JSON lines to 670. Its default compact structured payload is 11,602 bytes versus the original 230,454. A one-holding correlation/contributor drill-down is 25,885 MCP bytes. More records, unique warnings, explicit time series, and different missing-data patterns can change these sizes; these measurements are not universal bounds. Screening and market-data reductions are deliberately modest because all requested rows, factor inputs, financial periods and diagnostic messages are retained.

### Measured latency (milliseconds)

| Research tool | Before fresh full | After fresh full | After fresh summary | Cached full projection | Cached summary projection | Cached full + size serializations | Cached summary + size serializations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| get_market_data | 55.37 | 52.22 | 9.353 | 0.208 | 0.486 | 0.798 | 1.039 |
| analyse_portfolio_risk | 150.418 | 142.93 | 99.71 | 1.112 | 2.03 | 7.083 | 2.238 |
| screen_stocks | 328.333 | 315.02 | 225.86 | 0.222 | 0.48 | 0.917 | 1.076 |
| analyse_candidate_investment | 148.732 | 145.189 | 184.883 | 0.317 | 1.312 | 1.516 | 1.892 |
| optimise_new_contribution | 1631.523 | 980.708 | 169.016 | 0.687 | 0.808 | 3.892 | 1.317 |

Projection can cost slightly more CPU than returning an existing dictionary. Smaller serialization and transport volumes are the benefit; actual provider latency is unchanged. Fresh before/after measurements are illustrative single offline calls, not controlled network benchmarks. Cached views prove no account/provider fetch is needed, while repeated median timings quantify presentation/serialization overhead. No claim is made about live end-to-end timeout resolution.

## Validation

Regression tests compare exact underlying values across summary/full, demonstrate full matrix recovery, verify scoped row drill-downs without recalculation, retain negative correlation/hedging evidence, preserve coverage/infeasibility warnings, honour explicit series requests and test report scoping, expiry and eviction. Legacy contracts are exercised explicitly in full mode; all seven brokerage contracts and all 12 MCP registrations remain covered. Run `uv run --locked pytest`, `uv run --locked ruff check src tests scripts`, and `uv run --locked pyright` (strict mode is configured in `pyproject.toml`).
