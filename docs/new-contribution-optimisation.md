# Buy-only optimisation of new contributions

`optimise_new_contribution` is the twelfth read-only MCP tool. It uses fresh
Trading 212 position values, verified instrument resolution and existing adjusted
historical GBP price conversion. It never sells, rebalances the actual account,
places orders or edits positions. CVXPY/CLARABEL performs the numerical solves;
no numerical optimiser is implemented from scratch.

## Inputs and examples

| Input | Default | Interpretation |
| --- | --- | --- |
| `investment_amount_gbp` | required | Positive finite NEW external GBP capital |
| `tickers` | required | One to ten provider symbols; existing holdings need not be candidates |
| `objective` | `minimum_volatility` | `minimum_volatility`, `risk_parity`, `risk_budgeting`, `maximum_diversification` |
| `lookback_years` | 3 | Historical request, 1–10 years; actual common sample disclosed |
| `max_position_weight` | 1 | Cap on each resulting risky security/listing bucket, not its purchase share |
| `sector_limits` | null | Optional resulting-portfolio sector caps, fractions 0–1; exact provider names, case insensitive |
| `min_purchase_allocations` | null | Per-candidate minimum fractions of the NEW contribution, default zero |
| `max_purchase_allocations` | null | Per-candidate maximum fractions of the NEW contribution, default one |
| `allow_cash` | false | Allow contributed money to remain GBP cash |
| `min_cash_allocation` | 0 | Minimum fraction of new capital left as cash |
| `max_cash_allocation` | 1 | Maximum fraction of new capital left as cash |
| `risk_budgets` | null | Optional nonnegative variance-risk targets summing to one, keyed by resolved risky security symbols |
| `listing_selections` | null | Verified choices for ambiguous existing instruments, as in the existing risk/candidate tools |

Each purchase can be zero. Positive minima are mandatory, not hints. Cash is
excluded from the individual security cap and has its own purchase bounds. Sector
`Cash`, when requested, instead caps cash as a fraction of the total portfolio.
No 20% or other implicit investment preference is imposed: position caps default
to one until the user supplies a tighter limit.

Example: allocate a new contribution and compare all three methods:

```json
{
  "investment_amount_gbp": 2000,
  "tickers": ["NVDA", "GOOGL", "APH", "SPY"],
  "objective": "minimum_volatility",
  "max_position_weight": 0.25,
  "lookback_years": 3
}
```

Example: constrain direct technology exposure without selling:

```json
{
  "investment_amount_gbp": 2000,
  "tickers": ["NVDA", "GOOGL", "APH"],
  "objective": "risk_parity",
  "max_position_weight": 0.25,
  "sector_limits": {"Technology": 0.40},
  "max_purchase_allocations": {"NVDA": 0.30},
  "allow_cash": true,
  "max_cash_allocation": 0.20
}
```

These inputs may be infeasible for a concentrated existing portfolio. The tool
returns that result rather than automatically relaxing limits. Sector limits
cannot be certified when existing ETF/other exposures have unknown classification;
no ETF sector look-through is invented. A lower cash allocation can be fixed by
setting its minimum and maximum to the same fraction.

## Whole-portfolio constraints

Let v be the existing position values in GBP, A the contribution, V their sum,
x the fractions of new capital purchased, and P map purchases to securities
(including repeated verified ISINs). Then

```
w = v / (V + A) + (A / (V + A)) * P * x
sum(x) = 1
0 <= purchase_min <= x <= purchase_max
w[security] <= max_position_weight
sector_matrix * w <= sector_limits
```

Only x is optimised. Non-candidate holdings remain in covariance and risk
calculations; their original GBP values cannot be sold. Their percentage weights
can decline through dilution by new cash. Existing free, reserved and pie cash is
excluded from the baseline, and all funding is external new money.

For a holding worth v_i, its lowest possible resulting weight is v_i/(V+A), even
if none of the new money buys it. If this already exceeds the cap, the tool reports
an unavoidable breach. The necessary contribution lower bound
`max(0, v_i / cap - V)` is a dilution bound, not a guarantee that the other
constraints would become feasible. Sector values have an analogous dilution check.

Known identical ISINs among old and new listings share a position limit. Different
share classes, ADRs, issuers and unverified cross-listings are not guessed to be
identical. Caps are on the reported security/listing buckets, not an issuer or
underlying ETF holding limit. Identity provenance and limitations are included.

Unknown candidate sectors are assigned zero purchasing capacity when sector
limits are active; a mandatory positive purchase then blocks the analysis. Unknown
existing sectors block sector-constrained recommendations entirely. An absent
sector label such as `Tech` is not silently mapped to `Technology`: zero matching
exposure is disclosed in feasibility warnings.

## Risk estimation and objectives

One common observed-date sample is used for all covered securities, methods and
comparisons. Prices are split/dividend-adjusted and converted to GBP using the
existing daily FX/minor-unit layer; there is no equity-price filling or future FX.
The request can be three years while the common sample is much shorter. First/
last dates, observation counts, source and retrieval metadata remain visible.

The covariance model uses scikit-learn's **Ledoit–Wolf** shrinkage estimator,
annualised by 252. It is useful when the sample is short relative to the number
of securities or the sample covariance is singular. Its shrinkage coefficient is
reported. Cash is removed before fitting and restored as an exact zero covariance
row/column; shrinkage does not manufacture cash volatility.

A disclosed numerical eigenvalue floor of
`max(mean annual variance * 1e-8, 1e-12)` protects risky-asset conditioning. The
maximum floor adjustment is reported. An entirely constant/no-variation sample is
rejected instead of presented as reliable zero-risk evidence. This model is an
estimate, not observed future risk or synthetic market prices.

### Minimum volatility

Minimise `w' C w` subject to the buy-only constraints. This is a convex quadratic
program. Because w includes fixed existing values, it can allocate differently
from an optimiser run only on the candidate basket.

### Risk parity / constrained risk budgeting

Default budgets are equal across existing risky assets and feasible new risky
assets, not just the candidate stocks. LP support checks omit new securities
forced to zero by the actual constraints. Explicit budgets can assign zero but
must total one and cannot assign positive risk to cash.

The solver uses scaled variables z=t*w and y=t*x, t>0, and minimises

```
0.5 * z' C z - sum(b_i * log(z_i))
```

with all buy-only limits scaled by t. This is a convex risk-budgeting formulation.
Without binding restrictions, its first-order conditions match the target risk
contributions. With retained positions and binding caps, exact equality can be
impossible: the response reports targets, achieved contributions, maximum absolute
deviation and whether targets were attained within 1e-4. It does not claim to
minimise squared risk-budget error. `exact_risk_parity_achieved` applies to default
equal budgets; custom budgets use `risk_budget_targets_achieved` instead.

### Maximum diversification

Maximise the model diversification ratio `(asset_volatilities' w)/sqrt(w' C w)`.
Using z=t*w and y=t*x, solve the equivalent second-order cone programme:

```
maximise asset_volatilities' z
subject to ||L*z||_2 <= 1       (L' L = C)
           scaled buy-only constraints
```

The returned purchases are y/t. This uses CVXPY's standard cone formulation,
not a custom fractional/nonlinear search algorithm.

No expected returns are inferred from historical averages. **Maximum Sharpe is
not offered** because this interface does not supply a defensible forward-return
model. Cash has assumed zero nominal return and variance. Risk-budgeting and
diversification can leave cash weights non-unique; the user can narrow cash bounds
to express a preference. These methods do not optimise an undisclosed cash-return
forecast.

See [CVXPY quadratic programming](https://www.cvxpy.org/examples/basic/quadratic_program.html),
[solver interfaces](https://www.cvxpy.org/tutorial/solvers/index.html) and
[Ledoit–Wolf](https://scikit-learn.org/stable/modules/generated/sklearn.covariance.LedoitWolf.html).

## Results and comparisons

A successful response has `status=optimal` and a `recommended` allocation for the
requested objective. It includes:

- Unrounded contribution fractions/GBP amounts, full resulting weights/value.
- Model before/after annualised volatility and GBP volatility, diversification
  ratio/change, correlation with the current portfolio and risk contributions.
- Concentration, sector exposure, binding constraints and independent checks.
- All three method comparisons, plus equal-weight, each full-contribution single
  purchase, and optionally holding the contribution as cash.
- Source, common sample, robust covariance settings, assumptions and warnings.

Raw historical sample risk metrics use the existing analytics engine and are
labelled separately from shrunk model volatility/contributions. Sample Sharpe
is withheld; historical sample means are not used to choose allocations.
Risk contributions may be negative for hedging effects and sum to model variance;
fractions are undefined for a zero-variance all-cash portfolio.

Equal/single/cash comparisons are literal alternatives, not projected back onto
constraints. A comparison breaching a cap or purchase minimum is explicitly
`constraint_compliant=false`. It is not substituted as a recommendation. Only
the requested, independently validated solver result can be recommended.

Zero-value portfolios have no before risk metrics; the new portfolio is still
optimisable when candidate data are sufficient. All-cash-only constraints have
no diversification/risk-budgeting objective, but minimum volatility can remain
well-defined. Identical/singular risky series are handled by shrinkage/flooring.

## Failures and numerical validation

The tool requires **complete existing-holding value coverage** and sufficient
common observations before recommending an allocation. Missing new candidates can
receive zero, but existing holdings cannot disappear from the model. This differs
intentionally from descriptive candidate comparisons, which can show a labelled
covered subset. No partial-portfolio optimum is advertised as an account plan.

Important statuses include `insufficient_data`, `infeasible`,
`infeasible_risk_budgets`, `undefined_no_risky_exposure`, `invalid_covariance`,
`solver_failed`, `invalid_solver_result` and `constraint_validation_failed`.
`recommended` is null unless the selected method succeeds. An alternative method
is never silently selected when the requested one fails.

CLARABEL must report `optimal`; inaccurate/timeout outcomes are not promoted to
success. Each solve is limited to ten seconds, within a twenty-second aggregate
solver budget, with the selected method prioritised. Solver work runs outside the
async event loop. Cancellation cannot instantly stop the synchronous worker, but
its numerical work is bounded. The data fetch uses the existing
`MARKET_RISK_TIMEOUT_SECONDS` budget. Allow client time for both fetch and solving.

After tiny negative-number cleanup, disabled purchases are forced to exact zero
and funding is renormalised. Independent NumPy arithmetic rechecks finite amounts,
purchase budget/bounds, no sales, resulting weight sum, position caps and sector
caps with disclosed fractional tolerance **1e-7**. A solver success status alone
is insufficient. Any material breach returns no recommendation. Model amounts
are not rounded to pennies or executable share quantities; rounding can change
binding constraints and is not represented as a trade-ready order list.

## Setup and validation

Install the updated locked local numerical dependencies:

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run pyright
```

CVXPY, CLARABEL and scikit-learn require no subscription or API key. Existing
market-data licensing/access restrictions remain unchanged. Restart the MCP server
and refresh discovery to see twelve tools. No account permissions need expanding.

An explicit real-account check is available:

```sh
uv run python scripts/validate_optimisation_live.py --fetch-budget 50
```

This reads actual holdings and Yahoo histories, attempts a normal optimisation,
and verifies that an intentionally tiny position cap is rejected. It prints only
aggregate coverage/status checks. `live_optimal_allocation_verified=false` means
that a complete live allocation was **not** verified, even if data/infeasibility
guards worked correctly. Increase the fetch budget and supply verified listing
choices where required; the script never bypasses those guards. The Python MCP
client uses an explicit timeout because the installed Inspector CLI's one-minute
limit was observed ignoring its catalog request-timeout field.

The deterministic tests cover buy-only and total-portfolio behaviour, position/
sector/purchase/cash bounds, infeasibility, missing assets, risk budgets, currency
and pence conversion, singular/zero-variation covariance, numerical precision,
malformed solver output, solver failures and MCP registration/serialization. Known
closed-form and independent-grid checks validate the three mathematical objectives.

All results are conditional historical model estimates. Current holdings introduce
selection/survivor bias; exchange closes are asynchronous; covariance shrinkage
does not guarantee future outcomes or prevent losses. Fees, spreads, taxes, FX
charges, broker lot restrictions, inflation and actual trade execution are outside
this model.

## Validation results — 7 November 2026

- Full offline suite: **218 passed, 2 existing opt-in tests skipped**. Ruff and
  strict Pyright passed. The original eleven tools' discovery and response
  regression checks remain in the suite.
- Closed-form diagonal examples verified minimum-variance weights of 80%/20%
  and equal-risk/maximum-diversification weights of 2/3 and 1/3. Independent dense
  grids verified the correlated, funded whole-portfolio objectives.
- Live Python MCP stdio discovery returned **twelve read-only tools**. Against the
  actual account and Yahoo prices, a deliberately bounded 40-second fetch covered
  29.43% of holdings value (97 common daily returns) and correctly returned
  `insufficient_data` with no allocation. A subsequent tiny-cap call returned
  `infeasible`, also with no allocation, using real position-value dilution bounds.
- **A complete live optimal allocation was not verified.** The live checks prove
  the data/feasibility guards and protocol integration, not a full-account solve.
  Complete histories and verified listing choices remain prerequisites; no missing
  holding is treated as cash or dropped merely to produce an allocation.
