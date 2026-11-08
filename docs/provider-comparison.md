# Provider comparison and personal experiment configuration

Reviewed 2026-11-07. This project now defaults to the user's preferred personal experiment: **yfinance for prices/profiles/discovery, SEC EDGAR for US annual statements, and FMP as an explicit optional alternative**. One typed market-data interface feeds all three existing research tools.

This is a technical configuration choice, not a new license grant. The prior Yahoo activation flag has been removed at the user's request for personal experimentation. Existing terms/coverage findings below still apply; no live Yahoo/SEC data request or cloud licensing certification was made by changing the defaults.

## Configuration

A standard `uv sync --locked` now installs yfinance. No optional extra, FMP key or Yahoo activation flag is required for the default composition:

```dotenv
MARKET_PROVIDER=yfinance
MARKET_FUNDAMENTALS_SOURCE=sec
MARKET_SEC_USER_AGENT=Trading212Research your-real-contact@example.com
MARKET_YAHOO_REGIONS=["us"]
MARKET_YAHOO_ISIN_MAP={}
```

Set the SEC contact to a real application name and email locally. It is checked before any SEC request. Missing contact details do not block prices, discovery or risk calculations, but financial-data requests report missing SEC configuration and default SEC-based screening cannot rank candidates without statements.

Explicit alternatives retain the same output models:

| Configuration | Prices, FX, profiles and discovery | Financial metrics |
| --- | --- | --- |
| `yfinance` + `sec` (default) | Yahoo | SEC annual US-GAAP statements |
| `yfinance` + `provider` | Yahoo | Yahoo vendor fundamentals |
| `yfinance` + `fmp` | Yahoo | Licensed FMP financial block; requires FMP key |
| `fmp` + `provider` | FMP | FMP financial block; requires FMP key |
| `fmp` + `sec` | FMP | SEC annual US-GAAP statements |

The first value selects `MARKET_PROVIDER`; the second selects `MARKET_FUNDAMENTALS_SOURCE`. FMP can be selected as an explicit fallback when the experimental sources lack coverage. Failures do not silently switch vendors or merge SEC annual values with vendor TTM ratios.

Shared financial fields include reported `revenue`, `net_income`, `total_assets`, annual growth, cash and cash flow when available. Every block retains its source, fiscal date, currency and provenance. SEC mode leaves unsupported valuation/ROIC metrics null. To use vendor-normalized ratios instead, explicitly select the vendor financial block.

## Capability comparison

| Source | Prices and adjusted returns | Fundamentals and FX | Identifiers and real discovery | Free access and limitations |
| --- | --- | --- | --- | --- |
| yfinance/Yahoo | Broad international daily stocks/ETFs/benchmarks. The adapter requires `Adj Close`, with auto/back-adjust and repair disabled. | Vendor ratios/margins, limited annual statements and FX pairs; metric availability varies. No substitute of ROE for ROIC. | Real paginated custom `EquityQuery` screener, configured regions and provider total counts. Exact verified ISINs can be sparse; not a global security master. | No paid subscription/key required by the library, but no published guaranteed request quota or SLA. Yahoo's terms restrict automated collection without permission; personal-use library wording does not establish cloud/LLM rights. |
| SEC EDGAR | No prices, benchmarks, adjusted returns or FX. | Free primary-source filings and XBRL. Annual reported revenue, earnings, assets and cash flows implemented. No consensus forecasts or price-based valuations. | CIK and ticker/exchange index; a real SEC-filer universe, not all global tradable equities or an ISIN master. Cannot screen market cap/momentum alone. | Read APIs need no key. Identify the client and obey aggregate fair access. Practical supplementary statement source, not a complete market-data supplier. |
| Finnhub | Stock/FX candles are premium in its schema; daily stock candles are split-adjusted, not a complete dividend-adjusted-return contract. | Limited free profiles/basic financials; deeper statements/estimates and history depend on plan. | Real exchange stock-symbol lists permit outside-portfolio discovery. Broad enrichment costs quota; lists are not a complete free factor screener. | Advertised 60 free calls/minute plus a 30/second ceiling. Terms restrict third-party sharing of data and derived results without approval. Not an assumed hosted/LLM license. |
| Tiingo | Documented split/dividend-adjusted EOD data; coverage varies by product. | API fundamentals are an add-on; FX is a distinct product. Website financial/screener access does not imply equivalent free API access. | Supported-ticker universe is real, but broad enrichment is quota-limited. Not a verified ISIN resolver. | Starter advertises 500 symbols/month, 50 requests/hour and 1,000/day. Current free/trial terms restrict retained data and derived outputs; saved chats matter. AI submission has specific inference/training conditions. No adapter added. |
| FMP | Implemented adjusted daily histories/FX, subject to endpoint and exchange entitlement. | Closest fit for existing normalized ratios, statements and estimates. | Verified ISIN/profile resolution and a real paginated company screener, with enrichment cap disclosed. | Basic advertises 250 calls/day with restricted symbols/endpoints. The complete workflow is not guaranteed free. Hosting/display/sharing have separate restrictions. Integration retained. |

## Sources and deployment assessment

- [yfinance disclaimer](https://ranaroussi.github.io/yfinance/), [Yahoo terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html?ncid=mbr_idnedulnk00000001), [screen API](https://ranaroussi.github.io/yfinance/reference/api/yfinance.screen.html), [EquityQuery](https://ranaroussi.github.io/yfinance/reference/api/yfinance.EquityQuery.html). The software license and accessible endpoints are not market-data rights; applicable regional and supplemental terms still matter.
- [SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) and [fair-access guidance](https://www.sec.gov/about/developer-resources). Public APIs expose entity-wide standard concepts and explicit units/periods. SEC's stated ceiling is 10 requests/second across a user's machines; our client uses at most two and any lower configured pacing.
- [Finnhub pricing](https://finnhub.io/pricing), [API schema](https://finnhub.io/static/swagger.json), [terms](https://finnhub.io/terms-of-service). The stock/FX candle endpoints carry premium flags. Personal plans are not redistribution permission.
- [Tiingo pricing](https://www.tiingo.com/about/pricing), [EOD documentation](https://www.tiingo.com/documentation/end-of-day), [FX documentation](https://www.tiingo.com/documentation/forex), [terms](https://app.tiingo.com/tos/). The reviewed terms were updated November 5, 2026; retention, derived-output and AI/inference provisions need checking against the actual host and chat settings.
- [FMP pricing](https://site.financialmodelingprep.com/pricing-plans) and [terms](https://site.financialmodelingprep.com/terms-of-service). Verify endpoint entitlements and hosted/LLM use before treating a paid plan as sufficient permission.

For the present personal experiment, the default is the lowest-subscription-cost technical composition. It is not verified as a comprehensive, reliable, licensed global production feed. SEC is the strongest free authoritative US statement source. Yahoo can supply prices and genuine discovery, but its reliability and identifiers limit coverage. If reliable production/cloud coverage later becomes necessary, keep explicit licensed provider configurations available and verify their rights/entitlements rather than assuming a subscription solves every restriction.

## Implementation and known limits

`market/factory.py` composes the selected price/discovery provider with the selected financial source. `FundamentalsProvider` delegates financial requests as a whole block. The MCP tool signatures and existing fields stay intact; three optional reported-statement fields were added to the shared fundamentals schema. FMP and Yahoo populate them when available.

Yahoo SDK operations use a bounded single worker, caller timeout, cooldown handling and timestamp-preserving in-memory cache. A timed-out synchronous call cannot be killed by asyncio; replacement operations wait until it finishes. Logical-operation pacing is not a guaranteed Yahoo HTTP quota. No proxy rotation or denial bypass is added. The SDK may maintain its own cookie cache.

Yahoo discovery reports its regions, provider match count and evaluated cap. It is not a static/held-only sample or complete global listing claim. Portfolio ISIN matching now uses automatic OpenFIGI round-trip verification and a persistent cache; see [instrument resolution](instrument-resolution.md). Missing identifiers still restrict `exclude_owned=True` for unrelated screener candidates. Explicit ISIN maps remain optional. The experimental SDK ISIN helper calls Business Insider, so this adapter does not use it. Fuzzy matches never supply an assumed ISIN. Different share classes/ADRs are not consolidated by issuer.

SEC scope is annual standard US-GAAP 10-K/10-K/A facts. Derived values require matching accession, currency and reporting context. Growth requires a positive comparable prior denominator; missing capex is not zero. Assets/cash require matching year-end and accession. Latest filed restatements may change earlier figures; this is not point-in-time history. IFRS/custom tags, complete debt consolidation, ROIC and quarterly-to-TTM reconstruction remain unsupported. Valuation filters and value-heavy strategies can have no qualifying results with SEC-only financials.

Neither Yahoo nor SEC live financial-data access has been verified from the intended deployment. Tests use deterministic fixtures and mocked HTTP; no contact identity was fabricated for a live SEC request. No optimization, new MCP tools or account mutations were introduced.
