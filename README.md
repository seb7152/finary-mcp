# Finary MCP Server

Unofficial MCP (Model Context Protocol) server exposing a Finary account as
read-only tools. Designed to be deployed next to the
[obsidian-headless-mcp](https://github.com/seb7152/obsidian-headless-mcp)
stack on Hostinger, sharing the same Traefik reverse proxy.

Wraps [`finary_uapi`](https://github.com/lasconic/finary_uapi) — the de-facto
community wrapper around Finary's private API. No reverse engineering on our
side, just an MCP shim.

> ⚠️ Finary has no official public API. `finary_uapi` calls the private one.
> It can break at any time. Read-only by design here: no write operations
> are exposed.

## Architecture

```
                            Traefik (existing, from obsidian stack)
                                          │
                                          ▼
        Internet ─── https://finary-mcp.${DOMAIN} ───► finary-mcp (Python 3.12)
                                                            │
                                                            ▼
                                                       finary_uapi
                                                            │
                                                            ▼
                                                         Finary
```

- **finary-mcp** runs FastMCP in *streamable HTTP* mode on port 3002, exposed
  inside the Docker network only.
- The **existing Traefik** (from the obsidian-headless-mcp stack) discovers
  this container via the Docker socket and routes `finary-mcp.${DOMAIN}` to it.
- Access is protected either by a **legacy `API_TOKEN`** (accepted as a URL
  path prefix `/{token}/mcp` or an `Authorization: Bearer <token>` header) or
  by **OAuth 2.1 via Zitadel** (`Authorization: Bearer <access_token>`,
  validated against the Zitadel userinfo endpoint and gated on the
  `OAUTH_REQUIRED_ROLE` role). Same dual-mode auth as `obsidian-mcp`.
- A named Docker volume (`finary-session`) persists `credentials.json` and
  `cookies.json` across restarts.

## Available tools

Aggregate views:

- `finary_get_net_worth` — current net patrimoine + weekly variation
- `finary_get_gross_wealth` — current gross patrimoine + weekly variation
- `finary_get_timeseries` — historical series (1w, 1m, ytd, 1y, all) for
  gross / net / finance metric
- `finary_get_value_timeseries` — value evolution over a parameterizable horizon
  for the **total** or a single **asset class** (investments, cryptos,
  real_estates, fonds_euro, savings, checkings, commodities, loans). Markdown
  gives a summary (start/end/delta/min/max) + a downsampled series (~12 points);
  JSON gives the full `{date, value}` series. Params: `category`, `period`,
  `metric`, `response_format`. _(Finary only exposes history at the asset-class
  level — no per-account/per-position value history.)_
- `finary_get_evolution_value` — market-price evolution / **backtest** of a single
  listed holding via Yahoo Finance (yfinance). Resolves a Finary `query`
  (ticker/ISIN/name → Yahoo ticker, using the position's current quantity) or
  takes an explicit `ticker`; charts price × constant quantity over `period`
  (1mo…max). Only for listed instruments (stocks, ETFs, listed funds, crypto) —
  unlisted bonds / money-market / structured funds return a clear "not on Yahoo"
  message instead of a wrong number. Constant-quantity (no past buys/sells), value
  in the Yahoo quote currency (no FX conversion).
- `finary_get_overview` — totals per asset class

Position lookup:

- `finary_get_position` — find one holding by **ticker / ISIN / name**, across
  every account, without downloading the whole portfolio. Returns only the
  matching position(s); an instrument held in several accounts (e.g. SPOT on
  both IBKR and Trade Republic) is grouped with a per-account breakdown
  (institution, account, quantity, value, average buying price, performance)
  and aggregated totals. Params: `query` (required), `match_by`
  (`auto` | `ticker` | `isin` | `name`), `response_format`.
- `finary_get_account` — fetch a whole **portfolio** (every holding of one
  account or broker) without downloading everything. Filter by institution name
  (`Interactive Brokers`), account name (`IBKR`, `PEA`) or account id; a broker
  with several accounts yields one section per account, each with its own total.
  Params: `query` (required), `response_format`.

Per-asset-class lists:

- `finary_list_investments` (PEA, CTO, PER…)
- `finary_list_cryptos`
- `finary_list_scpis`
- `finary_list_real_estate`
- `finary_list_fonds_euro`
- `finary_list_crowdlendings`
- `finary_list_precious_metals`
- `finary_list_generic_assets`
- `finary_list_startups`
- `finary_list_holdings_accounts`

Other:

- `finary_get_upcoming_dividends`
- `finary_get_user_info` (works as healthcheck)

All tools support `response_format='markdown'` (default) or
`response_format='json'`.

## Files

```
finary-mcp/
├── docker-compose.yml
├── .env.example          → rename to .env
├── finary_mcp.py         → the actual MCP server (fetched at container startup)
└── README.md
```

## Deployment

### 1. Clone & configure

On your Hostinger VPS:

```bash
mkdir -p /docker/finary-mcp && cd /docker/finary-mcp
git clone https://github.com/seb7152/finary-mcp.git .
cp .env.example .env
# Edit .env: DOMAIN, FINARY_EMAIL, FINARY_PASSWORD, API_TOKEN
# Optional (OAuth 2.1 via Zitadel instead of/alongside API_TOKEN):
#   ZITADEL_ISSUER, OAUTH_REQUIRED_ROLE
```

Generate a strong API token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

### 2. DNS

Point `finary-mcp.${DOMAIN}` to the same IP as your other services. Traefik
will obtain a Let's Encrypt cert automatically on first request.

### 3. First-time signin (handles MFA)

Bring the container up just enough to run signin interactively:

```bash
docker compose up -d
# Wait ~30s for pip install + script download, then:
docker compose exec finary-mcp python -m finary_uapi signin
# (Enter MFA code if prompted. cookies.json gets written to /data, which is
# the persistent volume — subsequent runs are seamless.)
docker compose restart finary-mcp
```

### 4. Verify

```bash
curl -s https://finary-mcp.${DOMAIN}/mcp \
     -H "Authorization: Bearer ${API_TOKEN}" \
     -X POST \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

You should see the 16 tools listed.

### 5. Add to Claude

In claude.ai → Connectors → Add custom connector:

- **MCP URL** — either form works:
  - `https://finary-mcp.${DOMAIN}/{API_TOKEN}/mcp` (token in URL, no headers needed)
  - `https://finary-mcp.${DOMAIN}/mcp` (with HTTP header `Authorization: Bearer ${API_TOKEN}`)

Same pattern as the obsidian-mcp connector.

## Updating

The container fetches `finary_mcp.py` from this repo at every startup. To
deploy a new version, just push to `main` then:

```bash
docker compose restart finary-mcp
```

No rebuild, no image registry to maintain.

## Re-auth when session expires

Finary session cookies expire periodically. When you see auth errors:

```bash
docker compose exec finary-mcp python -m finary_uapi signin
docker compose restart finary-mcp
```

## Local dev (optional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install mcp finary_uapi pydantic uvicorn
export API_TOKEN=dev-token
export FINARY_SESSION_DIR=./.session
python finary_uapi  # for signin
python finary_mcp.py
```

To inspect the tools without Claude:

```bash
npx @modelcontextprotocol/inspector python finary_mcp.py
```

## Security notes

- `API_TOKEN` (or, when unset, OAuth via Zitadel) is the only thing between
  the open internet and your full patrimoine — make the token long and
  rotate it if leaked, or rely on OAuth by leaving `API_TOKEN` empty.
- Read-only by design: no add/update/delete tools are exposed even though
  `finary_uapi` supports them. If you want write operations later, add them
  deliberately and reconsider auth.
- Finary credentials live in the `finary-session` Docker volume on the
  server — never commit `credentials.json` or `cookies.json`.

## Credits

- [lasconic/finary_uapi](https://github.com/lasconic/finary_uapi) — unofficial
  Finary API wrapper (the brains)
- [seb7152/obsidian-headless-mcp](https://github.com/seb7152/obsidian-headless-mcp)
  — pattern this repo follows (Traefik + GitHub-bootstrapped Python MCP)

## License

MIT.
