# Research validation — 7 November 2026

This report distinguishes real requests from fixture tests. No trade, account
mutation, paid subscription or paid endpoint was used. Credentials and portfolio
values are omitted. The installed versions were yfinance 1.8.0 and MCP Inspector
2.13.0. Settings default to Yahoo prices and SEC annual fundamentals.

## Live market data

The registered `get_market_data` tool successfully retrieved three-year histories
and converted daily adjusted prices to GBP. Latest price dates were 6 November
2026 (the preceding trading day).

| Listing | Quote currency | Price observations | SEC annual period end |
| --- | --- | ---: | --- |
| NVDA | USD | 753 | 25 January 2026 |
| GOOGL | USD | 753 | 31 December 2025 |
| APH | USD | 753 | 31 December 2025 |
| ASML.AS (Amsterdam) | EUR | 766 | Unsupported in SEC mode |
| SPY (S&P 500 ETF) | USD | 753 | Not company annual fundamentals |

The repeatable opt-in test independently checks returned GBP prices against the
raw SDK `Adj Close` and daily FX `Close` frames, positive/finite prices, daily and
cumulative returns, date freshness, exact SEC CIKs and financial statement
contexts. It must obtain actual provider data to pass; errors or empty financial
results do not count as successful integration.

US CIKs verified against SEC responses: Nvidia 1045810, Alphabet 1652044 and
Amphenol 820313. Annual revenue, net income, assets and operating cash flow were
available. Values are selected from the same accession, currency and exact annual
period; balance-sheet facts use the matching period end. SEC values were checked
against raw companyfacts contexts, not an independent commercial dataset.

Nvidia free cash flow remained null: the required supported PP&E capex concept
was unavailable in the selected annual context. No different concept or missing
capex-as-zero assumption was substituted. Alphabet and Amphenol free cash flow
were available. Current valuation multiples, forward estimates, ROIC and unsupported
debt definitions remain null in SEC mode.

These are annual statements, not TTM. Amended filings, conflicting facts, future
filings, incompatible periods, units and missing data have fixture regression
coverage; no live amended filing was used to claim amendment verification.
Non-US/IFRS/custom-taxonomy and ETF fundamentals are not supplied by this SEC
adapter. A separate explicit Yahoo-only annual fundamentals request for ASML.AS
also succeeded, returning EUR statements with revenue and assets. Select
`MARKET_FUNDAMENTALS_SOURCE=provider` explicitly to use a whole
Yahoo fundamentals block instead; fields are never silently filled from Yahoo
while reporting SEC as their source.

## Live portfolio risk

A GET-only run against the configured live Trading 212 account returned 58
positions. With a validation-only 480-second budget, it completed in 447.79 seconds,
covered 91.19% of holdings value in GBP and explicitly excluded two ambiguous
instrument mappings. Risk metrics and benchmark beta were available. Original
portfolio weights and included-subset variance contributions each summed to one.
No account IDs, account values or holding lists are included in this report.

This was partial coverage, not a verified whole-portfolio risk estimate. The
remaining instrument choices must be made from verified candidates using
`listing_selections`; automatic guessing remains disabled. The benchmark is SPY,
converted to GBP and aligned to common observed dates. Cash is excluded, and
current weights are applied to a hypothetical daily-rebalanced historical portfolio.

Scalar numerical reference tests independently verify covariance, correlation,
annualised volatility, Sharpe, Sortino, drawdown, linear-quantile VaR, CVaR, beta,
unequal weights and negative hedging contributions. These numerical tests use
known examples, not independently sourced historical portfolio valuations.

An initial account request was rate limited. Pacing now starts from response
completion with a small margin and respects successful last-quota reset headers.
Account limits remain shared with Inspector, other MCP processes and other API
clients. A different process can still consume the account's allowance.

## Live stock discovery and screening

Yahoo custom EquityQuery returned 105 unique equity listings over two pages,
from 291 provider-reported US matches with market capitalisation at least 100
billion in provider-native currency. This was actual provider discovery, not a
portfolio-only list. Pagination, count metadata and truncation worked live.
The adapter fetches up to 100 rows per page, respects the remaining candidate
cap and imposes a configured maximum of 200 evaluated companies.

A capped five-company screen with `exclude_owned=false`, and explicitly selected
growth/quality factors, returned five ranked companies with no provider failures.
The opt-in regression checks repeatable scores on cached snapshots and verifies
the market-cap filter. Categorical filters use exact, case-insensitive matches;
`keyword` is the separate text-search filter.

**Known blocker:** the same screen with `exclude_owned=true` returned zero
candidates because all five Yahoo profiles lacked ISINs. The tool reported the
five failures and refused to assert that candidates were unowned. Fixture tests
verify correct exclusion when identifiers are present, but this does not establish
broad live Yahoo ownership-exclusion coverage. Turning exclusion off is explicit
and does not establish that returned candidates are unowned.

Discovery is limited to configured `MARKET_YAHOO_REGIONS` (default `['us']`),
Yahoo-supported EquityQuery fields/venues and active provider results. It is not
a complete global or historical universe. Ranking a capped large-cap sample is
not equivalent to screening the whole market. SEC mode lacks valuation factors;
unsupported metrics are disclosed and receive no invented neutral score.

Yahoo documents a maximum of 250 rows per custom-query request; the adapter uses
100. Provider counts can change between pages and completeness is not guaranteed.
See [Yahoo screen](https://ranaroussi.github.io/yfinance/reference/api/yfinance.screen.html)
and [EquityQuery](https://ranaroussi.github.io/yfinance/reference/api/yfinance.EquityQuery.html).

## Fixes and regression checks

- Enabled Pyright strict mode across all source modules and corrected external
  JSON/container and pandas/NumPy typing without disabling strict diagnostics.
- Fixed CVaR exclusion of an exact quantile-boundary loss due to floating-point
  rounding. A genuinely different quantile still gets its correct tail set.
- Fixed final-page over-fetch and incorrect screening truncation metadata.
- Made risk matrices and contribution ordering independent of network completion.
- Corrected categorical substring matches (for example NYSE matching NYSEARCA).
- Hardened Trading 212 pacing and successful quota-reset handling.
- Replaced deprecated yfinance `raise_errors` with the 1.8 SDK exception setting;
  the adapter still observes upstream failures and enforces cooldowns.

Fixture checks cover outages, network timeouts, 403/429/500 responses, missing
symbols and currencies, malformed indexes, missing statements, unknown ISINs,
ambiguous listings, conflicting prices, missing dates, stale/expired caches,
worker limits, cancellation, partial coverage and 54-instrument concurrency.
They do not simulate an actual Yahoo or SEC outage being induced remotely.

All ten tools and their read-only annotations were confirmed through local MCP
Inspector CLI over stdio. Live `get_market_data` returned structured results for
NVDA, ASML.AS and SPY. Live `screen_stocks` also returned five ranked companies
with no provider failures under an explicit 180000 ms request timeout. Its first
attempt with the default one-minute client limit did not complete and was not
counted as a pass.

An Inspector `analyse_portfolio_risk` call with a deliberately short 40-second
server budget returned structured partial results for the same 58 positions:
32.04% value coverage, 52 exclusions, risk metrics available and beta withheld.
This separately verifies the protocol/partial-response path; it is not the
447.79-second coverage run described above. CLI validation is not a claim that
the browser UI was manually exercised.

Final checks: Ruff lint and formatting passed; Pyright strict mode analysed all
32 source modules with zero errors or warnings; the full offline suite passed
136 tests with two opt-in integration tests skipped. The Yahoo/SEC opt-in test
was then run separately against real providers and passed (one test, FMP
deselected). The optional paid/quota-consuming FMP smoke test was not run.

## Reproduce and configuration

Offline checks (no provider requests):

```sh
uv run ruff check .
uv run pyright
uv run pytest -q
```

Explicit public live test (network access and a real SEC contact required; no
Trading 212 requests):

```sh
RUN_YAHOO_SEC_INTEGRATION=1 uv run pytest -q tests/test_integration.py -k yahoo_sec
```

Aggregate live portfolio validation (uses configured Trading 212 credentials):

```sh
uv run python scripts/validate_live.py --portfolio --risk-only
```

The validation script uses a 480-second risk budget in its own process; it does
not edit `.env`. For large personal portfolios, set the server and client budgets
together, for example `MARKET_RISK_TIMEOUT_SECONDS=480` and an Inspector Request
Timeout of `540000` ms. Keep `MARKET_FETCH_CONCURRENCY=4` initially. Concurrency
overlaps network waits but retains Yahoo's conservative 30-starts/minute pacing
and OpenFIGI's own limit. Valid identifier mappings persist; price/FX caches remain
in memory and expire after their configured TTL. No stale-on-error reuse occurs.

SEC requires `MARKET_SEC_USER_AGENT` containing an application name and real
contact email. The adapter uses at most two requests per second, below the SEC's
published 10 requests/second ceiling; 403/429 restrictions are never bypassed.
See [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
and [fair access](https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data).
FMP remains optional and was not live-validated; its quota-consuming smoke test
is a separate opt-in test. No claim about paid FMP entitlements was established.

## Readiness

The public price/FX and supported US annual-statement paths work with real data,
and partial portfolio risk works. Resolve the remaining listing ambiguities and
establish a usable verified ownership-exclusion source before relying on default
screening or introducing further investment tools. This is suitable for continued
personal experimentation, not a declaration of complete coverage or production
readiness. No cloud/data redistribution licensing conclusion is implied by these
connectivity checks; existing provider licensing limitations still apply.


Later candidate-tool validation found that Inspector **CLI** 2.13.0 did not apply
its catalog `requestTimeout` to long tool calls and still timed out at 60 seconds.
The earlier configured screening success was within that limit; it does not
verify CLI timeout extension. Use an explicit Python MCP client timeout for long
CLI-driven validation. See [candidate validation](candidate-investment.md).
