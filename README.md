# (Unofficial) Trading212MCP

A single-user Python MCP server for read-only Trading 212 portfolio access and deterministic investment research. It supports local **stdio** and **OAuth-protected Streamable HTTP** at `/mcp`.

No trades or account changes are implemented. Historical estimates and factor rankings are research inputs, not predictions or guaranteed outcomes. Yahoo/SEC are the personal-experiment defaults; FMP is optional. Data usage rights are separate from the software's licence.

## Tools

| Tool | Purpose |
| --- | --- |
| `get_account_summary` | Account currency, cash and investment totals |
| `get_positions` | Open holdings and reported account-currency values |
| `get_portfolio_allocation` | Position weights, excluding cash |
| `get_pending_orders` | Read pending orders |
| `get_order_history` | Paginated historical orders/fills |
| `get_dividend_history` | Paginated paid dividends |
| `get_transaction_history` | Paginated cash movements |
| `get_market_data` | Adjusted histories, returns and sourced fundamentals |
| `analyse_portfolio_risk` | Historical risk, concentration, correlations and contributions |
| `screen_stocks` | Provider-universe discovery and transparent factor ranking |
| `analyse_candidate_investment` | Hypothetical contribution comparisons against current holdings |
| `optimise_new_contribution` | Constrained, buy-only allocation of new funds |

The five research tools default to compact `summary` results. `detailed` and `full` modes, symbol/sector filters and report IDs provide access to underlying data. Existing JSON consumers expecting complete research responses should explicitly request `response_mode="full"`. [Response contracts and migration](docs/response-modes.md).

## Local setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Clone your checkout, then:

```sh
uv sync --locked
cp .env.example .env
```

Enter your Trading 212 key/secret locally in the ignored `.env`; use read-only permissions and the matching demo/live environment. Do not overwrite an existing `.env`. Configure `MARKET_SEC_USER_AGENT` with a real application/contact to use SEC financial statements.

Start the original stdio server:

```sh
uv run --locked trading212-mcp
```

It waits for MCP input on stdin. A typical local MCP client configuration is:

```json
{
  "mcpServers": {
    "trading212": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/Trading212MCP", "--locked", "trading212-mcp"]
    }
  }
}
```

## Local HTTP and Inspector

For loopback development only, explicitly select local authentication mode and export the local dotenv into the process environment:

```sh
MCP_AUTH_MODE=local uv run --env-file .env --locked trading212-mcp --transport streamable-http --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/healthz` or `/readyz`. Start Inspector separately:

```sh
npx @modelcontextprotocol/inspector
```

Choose Streamable HTTP and connect to `http://127.0.0.1:8000/mcp`. Expect 12 read-only tools. Inspector/Node are development utilities, not Python/runtime dependencies. Stop either process with Ctrl-C.

**Remote HTTP defaults to OAuth and refuses incomplete configuration.** Local mode refuses non-loopback binds. Never put a public forwarder in front of local mode. [Deployment, OAuth, Secure MCP Tunnel and ChatGPT connection instructions](docs/deployment.md).

## Container deployment

Docker builds from `uv.lock`, installs no development dependencies, runs as UID 10001, and includes readiness checks and SIGTERM shutdown. No dotenv files, credentials, local caches or development environment enter the build context.

```sh
cp .env.http.example .env.http
# Fill the actual OAuth resource/IdP values and inject your own secrets locally.
docker compose up --build
```

Compose exposes only a loopback port and keeps OAuth enabled. Cloud platforms must terminate HTTPS and provide a single instance, managed secrets, and a writable persistent `/data` volume owned by UID 10001. An optional, explicitly billed Render example is in [deploy/render.yaml](deploy/render.yaml); do not deploy it until you have reviewed its configuration and costs.

Instrument mappings persist under `/data`. Market-data persistence is opt-in with `MARKET_CACHE_PATH`; account responses and analytical reports are never written to it. [Storage and readiness definitions](docs/deployment.md).

## Verification

```sh
uv run --locked pytest -q
uv run --locked ruff check src tests scripts
uv run --locked ruff format --check src tests scripts
uv run --locked pyright
uv run --locked python scripts/check_deployment.py
uv run --locked python scripts/smoke_http.py
uv build --out-dir dist
uv run --locked python scripts/check_deployment.py --dist dist
```

Strict Pyright covers both `src/` and `scripts/`, including the live validation and benchmark helpers.

The HTTP smoke test uses a real loopback socket, synthetic credentials and a local mock JWKS; it makes no account/provider requests. CI runs the tests, strict typing, formatting, deployment safeguards, container build and container startup/shutdown checks. Live provider checks remain separate and opt-in.

## Code organisation

Quantitative calculations and research workflows live in `src/trading212_mcp/quantitative/`: risk statistics, portfolio allocation, market-return summaries, financial factors, screening, candidate comparisons and contribution optimisation. `market/` owns provider access, FX conversion, instrument resolution and caching; `tools/` owns MCP registration. Existing `MarketService` methods delegate to the quantitative package.

## Research documentation

- [Market-data configuration, costs and licensing](docs/market-data.md), [provider comparison](docs/provider-comparison.md)
- [Instrument resolution](docs/instrument-resolution.md), [risk methodology](docs/methodology.md)
- [Stock discovery and factor ranking](docs/stock-screening.md)
- [Candidate investment analysis](docs/candidate-investment.md), [new-contribution optimisation](docs/new-contribution-optimisation.md)
- [Live-data validation](docs/live-validation.md), [response modes and benchmarks](docs/response-modes.md)

## Security and publication

Read [SECURITY.md](SECURITY.md) before remote deployment. This instance represents one configured account, not a multi-user service. Its OAuth owner allowlist is mandatory for remote HTTP. Protect account credentials with managed secrets and never share logs, dumps or report IDs as public diagnostics.

The repository is prepared for eventual public release, but the owner must choose and add a software licence before distribution. MIT is the suggested simple permissive option; Apache-2.0 is an alternative when explicit patent terms are wanted. No licence grant is implied by this README. [Release checklist and licence decision](docs/open-source.md).
