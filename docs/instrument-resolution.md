# Automatic instrument resolution

Consumers do not need to enter each ticker/ISIN mapping into environment variables. The default Yahoo adapter uses OpenFIGI automatically, with an on-disk cache of verified identifier evidence. Existing explicit maps/overrides remain optional escape hatches.

## Identity checks

1. Take the ISIN and quote currency from Trading 212. No Yahoo symbol is invented by removing a Trading 212 suffix.
2. Send an `ID_ISIN` mapping job to OpenFIGI, filtered to equity-sector instruments and the currency where known. A result's association with the submitted job binds the returned FIGIs to that ISIN; OpenFIGI does not return proprietary ISIN values in the response.
3. Generate candidate Yahoo symbols using a bounded set of exchange suffix conventions. These are proposals, never identity evidence. Name similarity is not an identity check.
4. Retrieve the candidate's actual Yahoo profile and verify currency, exchange and cash-security type. Funds/options are not mistaken for common shares. London GBp/GBX quote units normalize to GBP for identity matching; existing price conversion still handles the unit scale separately.
5. Map the candidate's local exchange ticker back through OpenFIGI using its actual venue MIC and currency. Require an intersecting FIGI or composite FIGI with the original ISIN lookup and compatible security type. A shared name or a share-class FIGI alone is not enough.
6. Return only verified listings. For a Trading 212 identifier explicitly ending `_US_EQ`, known US venues are preferred for automatic selection **after** identity/currency proof; the ticker prefix is never used as the Yahoo symbol. Other ambiguous cases remain unresolved until a verified listing is selected.

The Nasdaq group uses its segment MICs (`XNGS`, `XNMS`, `XNCM`) because the broad `XNAS` operating MIC did not resolve the public Apple test. UK exchange-traded funds map as OpenFIGI ETP securities and Yahoo ETF quote types. Unknown venues/types are not guessed. Supported exchange hints include major US, London, Xetra/Frankfurt, Paris, Amsterdam, Swiss, Italian, Spanish, Canadian, Australian, Hong Kong, Japanese and selected Nordic markets; this is not complete worldwide coverage.

The Yahoo-native exact-ISIN path remains available, and explicit operator ISIN maps are still honored with conflict checks. FMP keeps its native identifier lookup. The new optional currency argument to the internal provider lookup improves filtering without changing existing MCP tool names.

## Persistence and freshness

The standard-library SQLite cache lives at `.cache/instrument-mappings.sqlite3` by default and is excluded from version control. It stores public identifier evidence, Yahoo listing fingerprints, verification time and remembered choices. It stores no account balances, position quantities, financial statements, provider credentials or historical price series.

Identity evidence expires after 30 days by default. Cache reuse still fetches the current Yahoo profile through its normal response cache. Changes in name, country, currency, exchange, quote type or a conflicting native ISIN invalidate stored evidence and require OpenFIGI revalidation. Thus the disk cache does not freeze quotes/fundamentals, and a changed issuer name is a revalidation trigger, never positive identity proof.

Remembered selections are keyed by the price provider, Trading 212 identifier, ISIN and canonical currency. Selecting another financial source (SEC/FMP) does not lose a Yahoo listing choice. Explicit per-call selections and configured overrides take precedence; a remembered symbol must still be among the currently verified matches. Stale choices are not blindly trusted.

The cache is bounded to 5,000 records, uses parameterized transactions, and treats corrupt/unreadable data as a cache miss. If writing fails, the server logs a warning and continues with fresh verification; persistence then cannot be promised. Mount a writable persistent volume for this cache when running in an ephemeral container. It is a local cache, not a multi-user account database.

## Handling ambiguity through chat or Inspector

`analyse_portfolio_risk` now accepts an optional `listing_selections` object. If an exclusion reports several verified candidates, the assistant can ask the user which listing to use, then rerun the same tool:

```json
{
  "lookback_years": 3,
  "benchmark": "SPY",
  "listing_selections": {"EXACT_TRADING212_TICKER": "CHOSEN_YAHOO_SYMBOL"}
}
```

The selected symbol must match the holding's ISIN/currency through the resolver. Unknown holding keys and unverified symbols are rejected. The verified choice is remembered for later risk/diversification calls; no `.env` change is needed. The LLM can explain or propose choices but cannot turn an unverified symbol into a valid match. All ten MCP tools remain; no extra tool or account mutation is introduced.

`portfolio_summary.holdings[].identifier_source` reports native, operator-provided, fresh OpenFIGI or cached OpenFIGI identity evidence when available. Market-data `company` objects also expose `quote_type` and `identifier_source` as additive optional metadata. Ambiguous/unavailable mappings remain explicit risk exclusions and affect reported value coverage.

This improves portfolio identity resolution. It does not magically populate ISINs for every unrelated Yahoo screener candidate. `exclude_owned=True` still fails closed on candidates whose identity cannot be established; reverse ownership classification for the entire market universe is separate work.

## Configuration, limits and sources

| Setting | Default | Purpose |
| --- | --- | --- |
| `MARKET_OPENFIGI_ENABLED` | `true` | Automatic Yahoo identity lookup |
| `MARKET_OPENFIGI_API_KEY` | unset | Optional OpenFIGI key, sent in a header; no key required for public mapping |
| `MARKET_RESOLUTION_CACHE_PATH` | `.cache/instrument-mappings.sqlite3` | Persistent public identifier cache |
| `MARKET_RESOLUTION_CACHE_TTL_DAYS` | `30` | Evidence/selection expiration |

The client sends at most ten mapping jobs per request and paces HTTP requests to no more than 20/minute per process. OpenFIGI documents 25 requests/minute without a key, so replicas sharing an IP must coordinate or lower aggregate traffic. HTTP attempts time out after at most ten seconds; transient failures have bounded retries. A 429 honors the server's cooldown rather than bypassing it. The existing overall risk budget also bounds lookup work. Cold first runs can still be partial; successful cached identities speed subsequent runs.

Primary sources: [OpenFIGI API, job association, identifiers and limits](https://www.openfigi.com/api/documentation), [equity exchange codes](https://www.openfigi.com/about/exchange-codes), and [OpenFIGI FAQ on storage, reuse and proprietary identifiers](https://www.openfigi.com/about/faq). The FIGI symbology/metadata terms do not grant any additional rights to Yahoo market data.

Public API checks verified Apple ISIN and Vanguard S&P 500 ETF mappings and the reverse US/UK venue lookups. Those checks used public sample identifiers, not a user's holdings. Yahoo profile integration and cache/ambiguity behavior are tested with deterministic fixtures; a live Trading 212/Yahoo portfolio run remains to be verified.
