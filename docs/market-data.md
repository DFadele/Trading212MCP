# Market data and providers

The personal-experiment default is Yahoo prices/discovery plus SEC annual US fundamentals. FMP remains an explicit optional provider. `MarketProvider` and the three MCP tools share one output schema; [current configuration/comparison](provider-comparison.md) documents source selection. The FMP evaluation and endpoint details below describe the optional FMP path.

## Evaluation (2026-11-07)

| Option | Assessment |
| --- | --- |
| Direct Financial Modeling Prep | Selected. A documented stable API covers profiles, ISIN lookup, adjusted daily histories, fundamentals, FX and a real paginated company screener. A small HTTPX adapter fits the existing project. |
| OpenBB | Useful when many providers are required. It adds a broader package/framework layer and still requires underlying provider accounts and licenses. Not installed for this single-provider implementation. |
| Alpha Vantage | Useful stock/fundamental/FX APIs and a listing-status universe. The documented adjusted daily history and company overview currently carry premium labels; ordinary free access is 25 requests/day. It would need more aggregation work for our screening workflow. |
| Massive (formerly Polygon) | Strong US reference data and prices. Its standard aggregate bars adjust splits but not dividends, requiring additional total-return handling for this methodology. Could be a later adapter. |

Sources: [FMP quickstart](https://site.financialmodelingprep.com/developer/docs/quickstart), [FMP pricing/endpoint entitlements](https://site.financialmodelingprep.com/pricing-plans), [FMP screener pagination](https://site.financialmodelingprep.com/insights/platform/product-updates/fmp-stock-screener-pagination-makes-largescale-equity-research-easier), [OpenBB data licensing](https://github.com/OpenBB-finance/openbb-docs/blob/main/content-odp/python/faqs/license.mdx), [Alpha Vantage documentation](https://www.alphavantage.co/documentation/), [Alpha Vantage free usage](https://www.alphavantage.co/support/), [Massive adjustment conventions](https://massive.com/knowledge-base/categories/stocks).

FMP's Basic plan advertises 250 calls/day but restricts symbols and endpoints. Do not assume it covers a global portfolio or all screening/fundamental endpoints. The full workflow may need a paid plan. Endpoint access, exchanges and histories depend on the account subscription. This code makes no entitlement guarantees and returns explicit missing-data warnings or errors when access fails.

## Setup

Create an FMP account, check access in its API dashboard, and set `MARKET_FMP_API_KEY` in the local `.env` or deployment secret manager. Never put keys in tool arguments. Market configuration is loaded lazily: absent/invalid market credentials do not prevent any of the seven Trading 212 tools from running.

When selected, FMP authenticates using an API-key query parameter. The adapter sanitizes HTTPX/HTTPcore logs and never includes provider response bodies or request URLs in user-facing errors. Do not enable independent proxy/APM logging of query strings containing credentials.

| Variable | Default | Meaning |
| --- | --- | --- |
| `MARKET_FMP_API_KEY` | unset | Enables market data; no fabricated fallback |
| `MARKET_REQUESTS_PER_MINUTE` | 60 | Per-process outbound pacing; set within your plan limits |
| `MARKET_FETCH_CONCURRENCY` | 4 | Concurrent risk holdings and Yahoo SDK workers (1–8); provider pacing still applies |
| `MARKET_CACHE_TTL_SECONDS` | 3600 | In-memory successful-response cache TTL; 0 disables reuse |
| `MARKET_CACHE_MAX_ENTRIES` | 512 | Maximum response entries |
| `MARKET_TIMEOUT_SECONDS` | 20 | Total timeout per HTTP attempt |
| `MARKET_RETRIES` | 2 | Additional attempts for connection errors, HTTP 429 and 5xx |
| `MARKET_MIN_OBSERVATIONS` | 60 | Minimum daily returns for risk and diversification |
| `MARKET_SCREEN_MAX_CANDIDATES` | 40 | Maximum provider-discovered candidates to enrich |
| `MARKET_INSTRUMENT_OVERRIDES` | `{}` | JSON map from exact Trading 212 ticker to FMP listing symbol |

Retries use exponential backoff and `Retry-After`; long cooldowns are returned as errors rather than held open indefinitely. Access failures (401/402/403) are not retried. Caches are bounded, process-local and non-persistent; cache hits retain original successful retrieval times. Limits are shared across replicas at the provider account level, but this implementation's pacing is process-local. Set conservative per-replica budgets or use a shared limiter before multi-replica deployment.

A new candidate can require a profile, six fundamental endpoints, adjusted prices and an FX series. A 40-candidate screen can exceed a 250-call daily free allowance and take several minutes on a cold cache. Reduce the candidate cap, narrow provider filters and set a suitable client tool timeout. Scores always state the subset evaluated; they never claim complete market coverage.

## Data mapping

- Profile: `profile`; ISIN search: `search-isin`, followed by verified profiles.
- Equity/ETF history: `historical-price-eod/dividend-adjusted`, `adjClose`. No fallback to unadjusted close. [FMP adjustment guide](https://site.financialmodelingprep.com/how-to/fmp-historical-price-apis-from-light-charts-to-dividendadjusted-analysis).
- FX: [`historical-price-eod/full`](https://site.financialmodelingprep.com/developer/docs/stable/forex-historical-price-eod-full), `close`, with a source+target pair (target units per source unit). Unsupported pairs fail explicitly; no inverse/triangulated or invented rates.
- Valuation and margins: `ratios-ttm`; ROIC: `key-metrics-ttm`.
- Revenue/earnings growth: latest two comparable annual `income-statement` reports, same reported currency, roughly one year apart, positive prior denominator. A loss turning profitable has no meaningful percentage growth and is missing.
- Balance sheet/cash flow: latest annual `balance-sheet-statement` and `cash-flow-statement`. These are annual statement amounts, not TTM amounts.
- Forward P/E: latest profile price divided by the next future annual `analyst-estimates.epsAvg` within 550 calendar days, only with positive EPS and matching reporting/quote currency. Missing estimates/currency mismatch produce null. It is not the forward PEG ratio.
- Screener: real `company-screener`, paginated from page 0, excluding ETFs/funds and requesting active companies. Provider ordering, subscription and candidate cap affect the evaluated subset.

Null financial metrics are never replaced with zero. Company-level fundamentals may not apply to ETFs/funds and can be absent. Raw prices, statements and TTM ratios may have different as-of dates; provenance exposes statement dates/currencies and retrieval times. Retrieval time is not a guarantee that a filing or quote was just updated. Reported market cap and statement amounts retain native/reporting currency; no cross-country dollar equivalence is implied by screening amount thresholds.

## Instrument matching

Portfolio matching uses exact ISIN/currency and automatically validates Yahoo listings through OpenFIGI, with a persistent evidence cache. Candidate suffix translations and name similarity never count as identity proof. The `_US_EQ` market hint can prefer a verified US listing; it is not stripped to invent the Yahoo ticker. Ambiguity is returned with verified candidates, and `analyse_portfolio_risk.listing_selections` can save a user's choice without an environment edit. See [instrument resolution](instrument-resolution.md).

ETFs, international listings and different share classes follow the same rules; unsupported or ambiguous instruments are excluded from risk metrics with their current value/weight and reason reported. Screening ownership exclusion compares ISINs across listings, not names. ADRs and different share classes with different ISINs are not consolidated by issuer. Candidates lacking an ISIN are excluded when ownership cannot safely be checked.

## Licensing and hosting

Data access is not redistribution permission. [FMP's terms](https://site.financialmodelingprep.com/terms-of-service), particularly sections 2.2 and 2.6, restrict sharing, display, commercial use and use by others; multi-user display requires a specific agreement. Confirm your plan permits your intended private hosted/LLM use, caching and any redistribution. OpenBB would not remove the underlying supplier's restrictions. No subscription is purchased and no licensing entitlement is assumed by this implementation.

No live market-provider credential or cloud deployment was supplied as part of this change. Production responses use only configured real endpoints. Unit fixtures are limited to `tests/`.


## Current provider selection

`MARKET_PROVIDER` defaults to `yfinance`. `MARKET_FUNDAMENTALS_SOURCE` defaults to `sec`; `provider` uses the selected vendor's financial block, and `fmp` explicitly selects licensed FMP financials with Yahoo prices. yfinance is installed by standard `uv sync --locked`, without an activation flag or optional extra.

SEC contact information is required only when statements are requested. Set `MARKET_SEC_USER_AGENT` to a real application/contact. Missing contact details produce a financial-data warning without blocking Yahoo prices or risk. Current source, annual periods and metric provenance remain visible. FMP does not silently fill missing SEC fields.

See [the comparison](provider-comparison.md) for cost, coverage and licensing findings. The defaults implement the chosen personal experiment; they do not certify cloud use rights or complete global coverage.


### Larger portfolios

Risk analysis queues holdings by descending current value and processes up to
`MARKET_FETCH_CONCURRENCY=4` at once. Set it to `1` for serial operation, or at
most `8`. Yahoo SDK work runs in a bounded thread pool; concurrent requests for
the same cache key share successful cached results. Cache reads do not wait for
the outbound pacing lock. Cancellation and timeouts do not release a worker slot
until its underlying synchronous request actually finishes.

Yahoo requests still start at most 30 times per minute (or the lower configured
`MARKET_REQUESTS_PER_MINUTE`). Concurrency overlaps network waits; it does not
raise this rate or bypass Yahoo cooldowns. FMP and OpenFIGI retain their own
serialized pacing. A cold 54-instrument portfolio may still require several
minutes for identifier checks, profiles, historical prices and FX. Keep client
and server timeouts aligned and inspect the returned coverage fraction.
Market-data caching remains in memory; only identifier mappings persist across
restarts. Prices, adjustment conventions and calculation formulas are unchanged.
