# Candidate investment analysis

`analyse_candidate_investment` is an eleventh read-only MCP tool. It retrieves a
fresh Trading 212 snapshot and uses the configured market-data provider and
existing `portfolio_statistics` engine. It never places orders, changes positions
or chooses an allocation for the user.

## Inputs

| Input | Default | Meaning |
| --- | --- | --- |
| `tickers` | required | One to ten provider listing symbols, including stocks/ETFs; duplicate requests are deduplicated |
| `investment_amount_gbp` | required | Finite positive amount of new external GBP capital |
| `lookback_years` | 3 | One to ten historical years |
| `benchmark` | `SPY` | Provider symbol; SPY is an S&P 500 ETF, not a global-equity ETF |
| `compare_cash` | false | Add a scenario holding the new investment as GBP cash |
| `allocations` | null | Optional positive fractions by candidate symbol, summing to one; adds a joint scenario |
| `use_historical_mean_returns` | false | Opt into descriptive historical Sharpe estimates and changes |
| `annual_risk_free_rate` | 0 | Annual effective GBP rate for Sharpe/Sortino excess returns; greater than -1, at most 1 |
| `annual_cash_return` | 0 | Assumed annual effective constant cash return; greater than -1, at most 1 |
| `confidence` | 0.95 | Historical VaR/CVaR confidence, strictly between 0.5 and 1 |
| `listing_selections` | null | Explicit choices among independently verified held-instrument listings, as in the existing risk tool |

Each candidate independently receives the full investment amount. Passing an
allocation does not replace these independent comparisons. The joint scenario
spends exactly the same total new capital. Missing allocated assets do not cause
funds to be silently redistributed.

Example: a £1,000 Nvidia scenario with a cash alternative:

```json
{"tickers":["NVDA"],"investment_amount_gbp":1000,"compare_cash":true}
```

Example: compare alternatives and also an explicitly supplied split:

```json
{
  "tickers": ["GOOGL", "NVDA", "SPY"],
  "investment_amount_gbp": 2000,
  "allocations": {"GOOGL": 0.5, "NVDA": 0.5},
  "lookback_years": 3,
  "benchmark": "SPY",
  "use_historical_mean_returns": true
}
```

For the user's global ETF, supply its actual provider listing symbol instead of
SPY; no global exposure is inferred from an ETF's name.

## Values, weights and funding

The baseline consists of current positive open-position wallet values. All
existing uninvested, pie and reserved cash is excluded, independently of the
optional new-capital cash scenario. New money is external funding, not a sale or
a drawdown of existing cash. Account values in another currency are converted
using the most recent available daily GBP FX rate, no more than seven calendar
days old. The rate, date and retrieval time are reported. This is a valuation
estimate, not an executable FX quote.

For existing holdings value V and investment A, post-purchase value is V+A.
Each original position contributes its original value divided by V+A; purchased
amounts are added to the matching security before dividing. A purchase of an
already-held security is not an extra artificial position. Verified identical
ISINs reuse the existing analysis listing and disclose that reuse. New candidate
aliases with an identical reported ISIN likewise share a joint-scenario bucket.
Unverified identifiers, different share classes/ADRs and unknown cross-listings
are never guessed to be equivalent.

Concentration is HHI (sum of squared weights), largest weight and top-five
weight by known listing/security bucket. Sectors come from provider profiles;
unknowns remain `Unknown`. Neither ETF holdings nor issuer groups are inferred.
Cash is a separate concentration/sector bucket. These are allocation weights,
not a claim about issuer or ETF look-through diversification.

Zero-value portfolios have no baseline risk metrics. A valid investment then
forms a 100% candidate portfolio; baseline correlation and metric changes are
undefined rather than zero. If a positive portfolio has *no* covered holdings,
portfolio risk comparisons are withheld rather than labelling the new purchase
alone as the user's portfolio.

## Historical risk and comparison

Held securities use the existing verified ISIN/currency resolver. Historical
prices use the existing adjusted-price layer and daily FX conversion into GBP,
including minor quote units such as GBp. No raw-close substitution, future FX or
equity-price filling is introduced. All usable candidates and covered holdings
share one observed-date sample. Benchmark observations restrict that same sample
when enough are available; otherwise beta is withheld consistently. Insufficient
candidates are reported individually and do not silently become cash.

Baseline and post-purchase scenarios reuse the existing covariance, volatility,
Sharpe/Sortino, drawdown, historical VaR/CVaR, correlation, beta and variance-risk
contribution implementation. No daily time series or full covariance matrices
are returned, keeping the response smaller. Matrices are used internally.

The backtest assumes daily rebalancing to current/post-purchase fixed weights;
it is not actual account performance or a buy-and-hold historical account replay.
Sharpe and its change are null by default. Opting into historical mean returns
produces descriptive sample Sharpe ratios, not predicted Sharpe or reliable
expected returns. Sortino, VaR/CVaR and drawdown are also historical sample
statistics. Risk-free and cash rates use 252-day effective daily equivalents.
Constant assumed cash has no modeled volatility; its Sharpe is undefined.

Diversification benefit is **1 minus correlation** of the funded purchase basket
with the existing covered current-weight portfolio. This is distinct from the
resulting volatility or HHI: a low-correlation purchase may itself be volatile.
Undefined/zero-variance correlations remain null.

For annual covariance C, post-purchase weights w and volatility sigma:

- Unconstrained marginal volatility per unit asset weight is `(Cw)_i / sigma`.
- Added-capital Euler contribution is `added_weight_i * (Cw)_i / sigma`.
- The full holding's reported variance contribution includes original and added
  capital and comes directly from the existing analytics engine.
- The derivative of volatility along the funded direction is
  `(Cov(purchase basket, post portfolio) - sigma²) / ((covered V + A) * sigma)`
  per additional GBP, accounting for dilution of the old weights.

Zero-volatility marginal derivatives are undefined. Results can include negative
risk contributions for hedging effects; these are not forced to be positive.

Comparison leaders are reported separately for largest historical diversification
benefit, lowest resulting volatility and lowest change in HHI. Cash is excluded
from these candidate rankings. An allocated scenario participates as its own
explicit scenario. Ties are returned; no recommendation score is calculated.

## Partial coverage and fundamentals

`current_portfolio.weights` and `post_investment_weights` use the full observed
holdings value. Unresolved broker instruments retain explicit `unresolved:`
buckets. Historical `analysed_weights` use only covered holdings plus the new
capital. Coverage fractions, exclusions and a `covered_subset_only` comparison
scope and candidate `status=partial` expose this distinction; weights of unresolved holdings are never treated
as zero-risk cash. Unresolved positions may conceal duplicate candidate exposure.
A small covered subset is not a dependable estimate of the whole portfolio.

A short history shared by every valid scenario shortens the common comparison
sample. Disjoint calendars or insufficient common observations withhold all
historical comparisons. Price provenance, actual first/last price dates, common
period, observations and data warnings are returned. Benchmark, candidate and
holding data retain source/retrieval metadata. The existing
`MARKET_MIN_OBSERVATIONS` applies.

Financial fundamentals use the existing configured whole source block. Missing
fundamentals do not prevent price-based comparison, and insufficient price
history does not prevent reporting available statements. Annual/TTM definitions,
reporting dates, currencies, warnings and null metrics remain explicit. SEC mode
does not invent valuation multiples, forward estimates or unsupported ROIC.

Quality leaders compare each metric separately (net margin, ROIC, lower
nonnegative debt/equity) only when at least two candidates share source, statement
end and currency. Yahoo/FMP snapshot/TTM quality metrics are not automatically
ranked against annual statement dates because metric-specific periods are not
verified; their raw metrics remain visible. Different annual ends are separate
peer groups. Valuation and growth metrics are returned directly with provenance,
not combined into a score or silently substituted between sources.

## Reliability and assumptions

Independent candidate, benchmark and holding loads use the existing configured
fetch concurrency and provider caches/pacing. The overall fetch budget is
`MARKET_RISK_TIMEOUT_SECONDS` (default 50 seconds), shared with account setup,
valuation FX, prices and fundamentals. Successful price data remains usable if
fundamentals time out. Cached identifier choices can be reused; no account
position or balance changes occur. Cold large portfolios still require a longer
client timeout and fetch budget, as documented for the existing risk tool.

Assumed fees, spreads, taxes and FX charges are zero; fractional shares and
immediate deployment are allowed. Cash returns are user assumptions, not live
bank-rate data. No future performance, optimisation, trade suitability or
execution availability is asserted.


## Validation

Run the deterministic suite, lint and strict source typing with:

```sh
uv run pytest -q
uv run ruff check .
uv run pyright
```

The fixtures exercise new and already-held securities, known-ISIN aliases,
independent and allocated scenarios, cash, account and daily historical FX,
zero-value portfolios, missing statements and prices, common calendars,
validation errors, partial coverage, timeouts and the existing engine's metric
consistency. The funded marginal-volatility derivative is independently checked
against a numerical change in the investment amount.

An explicit real-portfolio smoke check over MCP stdio is available:

```sh
uv run python scripts/validate_candidate_live.py
```

It uses the configured read-only Trading 212 account, explicitly selects Yahoo
and SEC, compares NVDA/APH/GOOGL/SPY, and checks joint allocation, cash, matching
sample sizes and post-investment totals/weights. It prints only public candidate
statuses and aggregate coverage checks. It uses a 180-second server fetch budget
and an explicit 300-second Python client timeout in its own process, without
editing `.env`. No live pass is implied by fixture tests.

The installed Inspector CLI 2.13.0 was observed timing out at 60 seconds even when
a longer `requestTimeout` was present in its server config; its client options
omit forwarding that setting. Long live validation therefore uses the Python
MCP client. Do not assume that editing the CLI catalog changes its call timeout.
The browser UI settings and other clients have their own timeout handling.


On 7 November 2026 the live MCP stdio smoke check succeeded for NVDA, APH, GOOGL
and SPY, with all eleven tools discoverable. The independent scenarios, joint
NVDA/APH allocation and cash scenario passed total-value, weight-sum and
common-sample checks. All four candidates had risk metrics and benchmark beta.
The three stocks returned SEC annual fundamentals; SPY's company fundamentals
were unavailable and left null. The 180-second fetch budget covered **58.40%** of
existing holdings value, with 31 holdings excluded and **97 common daily returns**.
This validates a partial comparison, not the full portfolio or a three-year
complete sample. All market/account requests were read-only. No account values
or holdings lists are recorded here.

The final deterministic suite passed 172 tests; the two existing opt-in provider
tests were skipped during that offline run. Ruff and strict Pyright passed.
The live MCP check was performed separately, not inferred from those fixtures.
