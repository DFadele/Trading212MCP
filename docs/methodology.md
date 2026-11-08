# Calculation methodology

## Daily data and currencies

All risk inputs are daily provider split/dividend-adjusted prices. Returns are simple fractions `P[t]/P[t-1]-1`. Prices must be positive and finite, dates are sorted, identical duplicates are deduplicated and conflicting duplicates fail validation. Unit conversion handles GBp/GBX as 0.01 GBP and ZAc as 0.01 ZAR, without confusing uppercase GBP with pence.

Historical FX converts each observation to the account's major currency unit before returns. FX can carry the last observed value backward-looking for at most four calendar days; future rates are never used. Equity prices are never filled. Missing FX values remain missing. No spot-today conversion is applied to an entire historical series.

Asset prices use a union of observed exchange dates. A common risk row needs every included asset's price on that date and the immediately preceding union-calendar date. Missing rows are dropped, not assigned zero return. This avoids comparing a two-day move in one asset against a one-day move in another when another asset supplies the intervening date. The union uses observed calendars: if all assets lack a day, it cannot distinguish a holiday from a missing vendor observation. No licensed exchange-calendar dataset is installed. International closes and FX fixings are not synchronous; results are historical daily approximations, not intraday risk estimates.

Histories with fewer than `MARKET_MIN_OBSERVATIONS` returns, unresolved identifiers, missing FX or prices over seven calendar days stale are excluded. The covered value fraction and reasons are returned. Assets starting later than the requested period cause warnings; all included assets use the common available period. If common overlap is insufficient, current concentration/exposures are still returned but risk metrics are withheld. A missing benchmark does not invalidate asset risk estimates; beta is withheld.

## Portfolio construction

These are **hypothetical current-weight backtests**, not actual portfolio performance. Current wallet values determine weights over open holdings, excluding cash. Uncovered holdings are removed and remaining weights sum to one. Daily returns use fixed weights, equivalent to daily rebalancing. There is no reconstruction of past trades, deposits, withdrawals, taxes or fees, and current constituents introduce survivorship bias.

Every response labels these assumptions and reports exclusions, original holding weights, effective included weights, account currency and data freshness. Value coverage can differ from observation/time coverage. Concentration and sector/industry exposures describe all current holdings, with unclassified holdings assigned `Unknown`; fund look-through is not available.

## Risk statistics

- Annualisation: 252 trading observations/year; sample standard deviation and covariance (`ddof=1`). Annual covariance is daily covariance × 252.
- Volatility: standard deviation of portfolio daily returns × sqrt(252).
- Risk-free rate: configurable annual fraction converted to daily via `(1+rf)^(1/252)-1`; default zero is explicitly stated.
- Sharpe: mean daily excess return / sample daily return standard deviation × sqrt(252).
- Sortino: mean daily excess return / sqrt(mean(min(excess,0)^2)) × sqrt(252). The denominator uses **all** observations, including zero downside for positive excess returns.
- Drawdown: minimum of wealth / previous peak − 1, including initial wealth of one. Returned as a nonpositive fraction.
- VaR: negative lower-tail simple-return quantile at `1-confidence`, linear interpolation, floored at zero; one-day loss fraction.
- CVaR: negative mean return of observations at or below the quantile, floored at zero. Empirical tail counts are included; small samples have coarse tail estimates.
- Correlation: Pearson correlation on the same complete-case daily observations used for covariance. Constant-series correlations are null.
- Beta: sample covariance of portfolio and benchmark daily returns / sample benchmark variance on matched observations; requires the configured minimum matched sample. Benchmark prices are converted to account currency.
- Variance contributions: `w_i * (Sigma @ w)_i`. Absolute contributions sum to annual portfolio variance; normalized fractions sum to one for nonzero variance. Negative contributions can be real hedging effects.
- Diversification ratio: weighted sum of asset volatilities / portfolio volatility. Undefined ratios are null, never infinity.
- Concentration: largest holding weight, top-five weight sum and Herfindahl index (sum of squared current holding weights).

## Screening and ranking

See [stock discovery and factor ranking](stock-screening.md) for methodology version 2: provider-backed listing universes; country/exchange/industry/sector/market-cap/liquidity filters; six transparent factor families; dated annual-history features; adjusted-price momentum; sector-relative winsorised percentiles; configurable factor/metric coverage thresholds; and per-factor explanations.

Existing presets and API fields remain. Scores now use expanded metric sets and weights renormalised over observed factors; missing factors remain null rather than receiving zero score contribution. Version 1 and version 2 scores must not be compared as the same model. The documentation includes numerical definitions, universe/period limitations, live verification evidence and example MCP calls.


## Explicit alternate sources

The descriptions above specify the financial definitions of the optional FMP mode. The current default is Yahoo prices/discovery plus SEC annual US statements. Yahoo uses explicit split/dividend-adjusted `Adj Close` with auto/back-adjust and repair disabled; FX uses raw spot `Close`. Missing adjusted prices fail rather than falling back to raw prices. Yahoo yearly revenue/earnings growth is kept separate from its vendor snapshot valuation/margins. Yahoo debtToEquity is a percentage converted to a fraction; ROE is never substituted for unavailable ROIC. Annual balance/cash rows must share the income fiscal end and financial currency must be known.

Optional SEC mode replaces the full fundamentals block, explicitly labeled `sec_edgar_annual`. It selects annual standard US-GAAP 10-K/10-K/A contexts, preserves reporting units and accessions, and does not combine values from different accessions when calculating margins or free cash flow. Latest filed restatements are preferred for each year; this is not point-in-time historical fundamental data. It withholds valuations, ROIC and unsupported debt consolidation. Annual margins must not be interpreted as vendor TTM margins. Screening output now points to the exact per-result source/provenance rather than describing every provider as TTM.


## Bounded risk fetches

`MARKET_RISK_TIMEOUT_SECONDS` defaults to 50 seconds for the full risk data-fetch phase, including Trading 212 account/position retrieval and benchmark prices. Current holdings are fetched in descending wallet-value order to improve value coverage under a short budget. Unfinished holdings remain in concentration/exposure accounting and are explicitly excluded from risk calculations; included weights are renormalized as documented above. The benchmark shares the same deadline and beta is withheld when it cannot be fetched in time. If the account snapshot cannot be retrieved, no partial portfolio is invented.

This deadline bounds async I/O, not a guarantee that CPU calculations and serialization finish at exactly that second. Progress notifications are best-effort client feedback, not a request to bypass a client's maximum timeout. For large cold runs, increase both server budget and client timeout. Timed-out Yahoo SDK threads retain the existing bounded-worker guard.

CVaR includes losses on the linear VaR quantile boundary. Quantile positions within 1e-12 of an integer order statistic are snapped to that statistic to avoid floating-point exclusion of an exact boundary loss. Risk matrix and contribution ordering follows descending holding value, independent of request completion order.
