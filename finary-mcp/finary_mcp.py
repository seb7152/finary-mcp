#!/usr/bin/env python3
"""Finary MCP server.

Read-only MCP server exposing a Finary account as a set of tools, via the
unofficial `finary_uapi` wrapper. Designed to be deployed alongside the
obsidian-headless-mcp stack on Hostinger, behind Traefik.

Auth model identical to obsidian_mcp.py: a single `API_TOKEN` env var, accepted
either as a `/{token}/` URL path prefix or as an `Authorization: Bearer <token>`
header. DNS rebinding protection is disabled because the auth middleware
forces the Host header to localhost before passing the request to FastMCP.
"""

import json
import logging
import os
import threading
from enum import Enum
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.getenv("PORT", 3002))
API_TOKEN = os.getenv("API_TOKEN", "")
FINARY_SESSION_DIR = os.getenv("FINARY_SESSION_DIR", "/data")

logging.basicConfig(
    level=os.environ.get("FINARY_MCP_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("finary_mcp")

# ---------------------------------------------------------------------------
# Create MCP server
# ---------------------------------------------------------------------------
# DNS rebinding protection disabled — TokenAuthMiddleware enforces auth and
# rewrites the Host header to localhost before passing to FastMCP. Same trick
# as obsidian_mcp.py.

mcp = FastMCP(
    "Finary",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ---------------------------------------------------------------------------
# finary_uapi wrapper — lazy session, persisted cookies
# ---------------------------------------------------------------------------


class FinaryAuthError(Exception):
    """Raised when Finary signin fails or session is invalid."""


VALID_PERIODS = {"all", "1w", "1m", "ytd", "1y"}
VALID_METRICS = {"gross", "net", "finance"}


class _FinaryClient:
    """Lazy-initialized client around finary_uapi.

    Session is created on first use and cached for the lifetime of the
    process. Cookies live in FINARY_SESSION_DIR (a Docker volume) so they
    survive restarts.
    """

    def __init__(self) -> None:
        self._session = None
        self._lock = threading.Lock()

    def _ensure_session(self):
        if self._session is not None:
            return self._session
        with self._lock:
            if self._session is not None:
                return self._session

            os.makedirs(FINARY_SESSION_DIR, exist_ok=True)
            # finary_uapi reads `credentials.json` and writes `cookies.json` in cwd
            os.chdir(FINARY_SESSION_DIR)

            # Bootstrap credentials.json from env on first run if missing
            creds_path = os.path.join(FINARY_SESSION_DIR, "credentials.json")
            if not os.path.exists(creds_path):
                email = os.environ.get("FINARY_EMAIL")
                password = os.environ.get("FINARY_PASSWORD")
                if email and password:
                    with open(creds_path, "w") as f:
                        json.dump({"email": email, "password": password}, f, indent=2)
                    log.info("credentials.json bootstrapped from environment")
                else:
                    log.warning(
                        "No credentials.json and no FINARY_EMAIL/FINARY_PASSWORD; "
                        "manual signin will be required."
                    )

            try:
                from finary_uapi.auth import prepare_session
            except ImportError as e:
                raise FinaryAuthError("finary_uapi not installed") from e

            try:
                session = prepare_session()
            except Exception as e:  # noqa: BLE001
                raise FinaryAuthError(f"Could not prepare Finary session: {e}") from e

            if session is None:
                raise FinaryAuthError(
                    "Finary session could not be initialized. Check credentials and MFA."
                )
            self._session = session
            return self._session

    # User / account ----------------------------------------------------

    def get_me(self) -> dict:
        from finary_uapi.user_me import get_user_me
        return get_user_me(self._ensure_session())

    def get_holdings_accounts(self) -> dict:
        from finary_uapi.user_holdings_accounts import get_holdings_accounts
        return get_holdings_accounts(self._ensure_session(), "")

    # Portfolio aggregates ----------------------------------------------

    def get_timeseries(self, period: str, metric: str) -> dict:
        from finary_uapi.user_portfolio import get_portfolio_timeseries
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period '{period}'. Must be one of {sorted(VALID_PERIODS)}.")
        if metric not in VALID_METRICS:
            raise ValueError(f"Invalid metric '{metric}'. Must be one of {sorted(VALID_METRICS)}.")
        api_metric = "finary" if metric == "finance" else metric
        return get_portfolio_timeseries(self._ensure_session(), period, api_metric)

    def get_dividends(self) -> dict:
        from finary_uapi.user_portfolio import get_portfolio_investments_dividends
        return get_portfolio_investments_dividends(self._ensure_session())

    # Asset classes -----------------------------------------------------

    def get_investments(self) -> dict:
        from finary_uapi.user_securities import get_user_securities
        return get_user_securities(self._ensure_session())

    def get_cryptos(self) -> dict:
        from finary_uapi.user_cryptos import get_user_cryptos
        return get_user_cryptos(self._ensure_session())

    def get_scpis(self) -> dict:
        from finary_uapi.user_scpis import get_user_scpis
        return get_user_scpis(self._ensure_session())

    def get_fonds_euro(self) -> dict:
        from finary_uapi.user_fonds_euro import get_user_fonds_euro
        return get_user_fonds_euro(self._ensure_session())

    def get_real_estates(self) -> dict:
        from finary_uapi.user_real_estates import get_user_real_estates
        return get_user_real_estates(self._ensure_session())

    def get_crowdlendings(self) -> dict:
        from finary_uapi.user_crowdlendings import get_user_crowdlendings
        return get_user_crowdlendings(self._ensure_session())

    def get_precious_metals(self) -> dict:
        from finary_uapi.user_precious_metals import get_user_precious_metals
        return get_user_precious_metals(self._ensure_session())

    def get_generic_assets(self) -> dict:
        from finary_uapi.user_generic_assets import get_user_generic_assets
        return get_user_generic_assets(self._ensure_session())

    def get_startups(self) -> dict:
        from finary_uapi.user_startups import get_user_startups
        return get_user_startups(self._ensure_session())


_client = _FinaryClient()

# ---------------------------------------------------------------------------
# Shared input models / enums
# ---------------------------------------------------------------------------


class ResponseFormat(str, Enum):
    MARKDOWN = "markdown"
    JSON = "json"


class EmptyInput(BaseModel):
    """No parameters except response format."""

    model_config = ConfigDict(extra="forbid")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' (human-readable) or 'json' (machine-readable).",
    )


class TimeseriesPeriod(str, Enum):
    WEEK = "1w"
    MONTH = "1m"
    YTD = "ytd"
    YEAR = "1y"
    ALL = "all"


class TimeseriesMetric(str, Enum):
    GROSS = "gross"
    NET = "net"
    FINANCE = "finance"


class TimeseriesInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period: TimeseriesPeriod = Field(
        default=TimeseriesPeriod.MONTH,
        description="Period: 1w, 1m, ytd, 1y, or all.",
    )
    metric: TimeseriesMetric = Field(
        default=TimeseriesMetric.GROSS,
        description="Metric: gross/net wealth, or 'finance' for financial assets only.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_eur(amount: Any) -> str:
    try:
        v = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _fmt_pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    return payload


def _to_list(payload: Any) -> list:
    data = _unwrap(payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "results", "assets"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def _pick(item: dict, *keys: str) -> Any:
    for k in keys:
        if k in item and item[k] is not None:
            return item[k]
    return None


def _format_response(data: Any, fmt: ResponseFormat, markdown_renderer: Callable[[Any], str]) -> str:
    if fmt == ResponseFormat.JSON:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    return markdown_renderer(data)


def _handle_error(e: Exception) -> str:
    if isinstance(e, FinaryAuthError):
        return (
            "Error: Finary authentication failed. The session cookie may have expired. "
            "Re-run signin inside the container."
        )
    log.exception("Tool execution failed")
    return f"Error: {type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Markdown renderers
# ---------------------------------------------------------------------------


def _render_account_like(items: list, title: str) -> str:
    if not items:
        return f"# {title}\n\n_Aucune ligne._"
    lines = [f"# {title}\n"]
    total = 0.0
    for item in items:
        name = _pick(item, "name", "display_name", "code", "description", "address") or "?"
        value = _pick(
            item,
            "display_current_value", "current_value",
            "display_balance", "balance", "amount", "user_estimated_value",
        )
        perf = _pick(item, "display_diff_gain", "performance", "variation_percentage")
        try:
            total += float(value)
        except (TypeError, ValueError):
            pass
        suffix = f" ({_fmt_pct(perf)})" if perf is not None else ""
        lines.append(f"- **{name}** — {_fmt_eur(value)}{suffix}")
    if total:
        lines.append(f"\n**Total** : {_fmt_eur(total)}")
    return "\n".join(lines)


def _render_dividends(payload: Any) -> str:
    items = _to_list(payload)
    if not items:
        return "# Dividendes à venir\n\n_Aucun dividende planifié._"
    lines = [
        "# Dividendes à venir\n",
        "| Ex-date | Paiement | Titre | Montant | Quantité |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for d in items:
        ex_date = _pick(d, "ex_dividend_date", "ex_date") or ""
        pay_date = _pick(d, "payment_date", "pay_date") or ""
        sec = d.get("security") if isinstance(d.get("security"), dict) else {}
        name = sec.get("name") if sec else _pick(d, "name", "code") or "?"
        amount = _pick(d, "amount", "net_amount", "total_amount")
        qty = _pick(d, "quantity", "shares") or ""
        lines.append(f"| {ex_date} | {pay_date} | {name} | {_fmt_eur(amount)} | {qty} |")
    return "\n".join(lines)


def _render_timeseries(payload: Any) -> str:
    series = _to_list(payload)
    if not series:
        return "# Historique\n\n_Aucune donnée._"
    first, last = series[0], series[-1]
    first_val = _pick(first, "value", "amount", "y")
    last_val = _pick(last, "value", "amount", "y")
    delta = None
    if isinstance(first_val, (int, float)) and isinstance(last_val, (int, float)) and first_val:
        delta = (last_val - first_val) / first_val * 100
    return "\n".join([
        "# Historique de patrimoine\n",
        f"- **Du** : {_pick(first, 'date', 'timestamp', 'x')}",
        f"- **Au** : {_pick(last, 'date', 'timestamp', 'x')}",
        f"- **Valeur initiale** : {_fmt_eur(first_val)}",
        f"- **Valeur finale** : {_fmt_eur(last_val)}",
        f"- **Évolution** : {_fmt_pct(delta) if delta is not None else 'n/a'}",
        f"- **Points** : {len(series)}",
    ])


def _render_net_worth(payload: Any, label: str) -> str:
    series = _to_list(payload)
    if not series:
        return f"# {label}\n\n_Aucune donnée._"
    last = series[-1]
    last_val = _pick(last, "value", "amount", "y")
    first_val = _pick(series[0], "value", "amount", "y")
    delta = None
    if isinstance(first_val, (int, float)) and isinstance(last_val, (int, float)) and first_val:
        delta = (last_val - first_val) / first_val * 100
    return "\n".join([
        f"# {label}\n",
        f"**Valeur actuelle** : {_fmt_eur(last_val)}",
        f"**Variation sur la période** : {_fmt_pct(delta) if delta is not None else 'n/a'}",
        f"**Dernier point** : {_pick(last, 'date', 'timestamp', 'x')}",
    ])


def _render_overview(summary: dict) -> str:
    lines = ["# Patrimoine — synthèse par classe d'actifs\n",
             "| Catégorie | Total | Lignes |",
             "| --- | ---: | ---: |"]
    grand_total = 0.0
    for cat, info in summary["categories"].items():
        grand_total += info["total"]
        lines.append(f"| {cat} | {_fmt_eur(info['total'])} | {info['count']} |")
    lines.append(f"\n**Total agrégé** : {_fmt_eur(grand_total)}")
    if summary.get("errors"):
        lines.append("\n## Erreurs partielles\n")
        for err in summary["errors"]:
            lines.append(f"- {err}")
    return "\n".join(lines)


# ===========================================================================
# TOOLS — Aggregates
# ===========================================================================


@mcp.tool(
    name="finary_get_net_worth",
    annotations={
        "title": "Get current net worth",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_net_worth(params: EmptyInput) -> str:
    """Get the current net patrimoine (assets minus liabilities) with weekly variation.

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        Markdown summary or full JSON payload.
    """
    try:
        data = _client.get_timeseries("1w", "net")
        return _format_response(
            data, params.response_format, lambda d: _render_net_worth(d, "Patrimoine net")
        )
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="finary_get_gross_wealth",
    annotations={
        "title": "Get current gross wealth",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_gross_wealth(params: EmptyInput) -> str:
    """Get the current gross patrimoine (all assets, no liabilities deducted).

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        Markdown summary or full JSON payload.
    """
    try:
        data = _client.get_timeseries("1w", "gross")
        return _format_response(
            data, params.response_format, lambda d: _render_net_worth(d, "Patrimoine brut")
        )
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="finary_get_timeseries",
    annotations={
        "title": "Get patrimoine timeseries",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_timeseries(params: TimeseriesInput) -> str:
    """Get the historical patrimoine timeseries.

    Useful for variations like 'last month / YTD performance'.

    Args:
        params:
            - period: '1w' | '1m' | 'ytd' | '1y' | 'all'
            - metric: 'gross' | 'net' | 'finance' (financial assets only)
            - response_format: 'markdown' | 'json'

    Returns:
        Markdown summary with start/end/delta, or full series in JSON.
    """
    try:
        data = _client.get_timeseries(params.period.value, params.metric.value)
        return _format_response(data, params.response_format, _render_timeseries)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


@mcp.tool(
    name="finary_get_overview",
    annotations={
        "title": "Get aggregated patrimoine overview",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_overview(params: EmptyInput) -> str:
    """Get an aggregated overview across all asset classes.

    Calls each asset-class endpoint and returns totals per category. Errors on
    individual classes do not fail the whole call — they are reported.

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        Markdown table or JSON payload.
    """
    summary: dict = {"categories": {}, "errors": []}
    sources: list[tuple[str, Callable[[], dict]]] = [
        ("Investments", _client.get_investments),
        ("Cryptos", _client.get_cryptos),
        ("SCPIs", _client.get_scpis),
        ("Immobilier", _client.get_real_estates),
        ("Fonds euro", _client.get_fonds_euro),
        ("Crowdlending", _client.get_crowdlendings),
        ("Métaux précieux", _client.get_precious_metals),
        ("Autres actifs", _client.get_generic_assets),
        ("Startups", _client.get_startups),
    ]
    for label, fn in sources:
        try:
            items = _to_list(fn())
            total = 0.0
            for item in items:
                value = _pick(
                    item,
                    "display_current_value", "current_value",
                    "display_balance", "balance", "amount", "user_estimated_value",
                )
                try:
                    total += float(value)
                except (TypeError, ValueError):
                    pass
            summary["categories"][label] = {"total": total, "count": len(items)}
        except Exception as e:  # noqa: BLE001
            summary["categories"][label] = {"total": 0.0, "count": 0}
            summary["errors"].append(f"{label}: {type(e).__name__}: {e}")
    return _format_response(summary, params.response_format, _render_overview)


# ===========================================================================
# TOOLS — User info
# ===========================================================================


@mcp.tool(
    name="finary_get_user_info",
    annotations={
        "title": "Get Finary account info",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_user_info(params: EmptyInput) -> str:
    """Get the connected user's Finary profile (name, email, plan).

    Useful as a healthcheck: a successful response means auth is working.

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        JSON or markdown summary.
    """
    try:
        data = _client.get_me()
        if params.response_format == ResponseFormat.JSON:
            return json.dumps(data, indent=2, ensure_ascii=False, default=str)
        u = _unwrap(data)
        plan = u.get("plan") if isinstance(u, dict) else None
        plan_name = plan.get("name") if isinstance(plan, dict) else plan or "n/a"
        return (
            "# Profil Finary\n\n"
            f"- **Nom** : {u.get('first_name', '')} {u.get('last_name', '')}\n"
            f"- **Email** : {u.get('email', 'n/a')}\n"
            f"- **Plan** : {plan_name}\n"
        )
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# TOOLS — Asset classes (read-only lists, registered via factory)
# ===========================================================================


def _register_list_tool(name: str, title: str, client_method: str) -> None:
    description = (
        f"List all {title.lower()} held on Finary. "
        "Returns either a markdown summary with per-line value and computed total, "
        "or the full JSON payload from the Finary API. "
        "Use response_format='json' for raw structure when chaining with another tool."
    )

    @mcp.tool(
        name=name,
        description=description,
        annotations={
            "title": title,
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def _tool(params: EmptyInput) -> str:
        try:
            data = getattr(_client, client_method)()
            items = _to_list(data)
            return _format_response(
                data,
                params.response_format,
                lambda _: _render_account_like(items, title),
            )
        except Exception as e:  # noqa: BLE001
            return _handle_error(e)

    _tool.__name__ = name


_register_list_tool("finary_list_investments", "Investments (actions, ETF, PEA, CTO, PER)", "get_investments")
_register_list_tool("finary_list_cryptos", "Cryptos", "get_cryptos")
_register_list_tool("finary_list_scpis", "SCPIs", "get_scpis")
_register_list_tool("finary_list_fonds_euro", "Fonds euro (assurance-vie)", "get_fonds_euro")
_register_list_tool("finary_list_real_estate", "Immobilier", "get_real_estates")
_register_list_tool("finary_list_crowdlendings", "Crowdlending", "get_crowdlendings")
_register_list_tool("finary_list_precious_metals", "Métaux précieux", "get_precious_metals")
_register_list_tool("finary_list_generic_assets", "Autres actifs", "get_generic_assets")
_register_list_tool("finary_list_startups", "Investissements en startups", "get_startups")


@mcp.tool(
    name="finary_list_holdings_accounts",
    annotations={
        "title": "List holding accounts (banks + brokers)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_list_holdings_accounts(params: EmptyInput) -> str:
    """List all bank, broker and investment accounts connected to Finary.

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        Markdown summary or full JSON payload.
    """
    try:
        data = _client.get_holdings_accounts()
        items = _to_list(data)
        return _format_response(
            data,
            params.response_format,
            lambda _: _render_account_like(items, "Comptes (banques & brokers)"),
        )
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# TOOLS — Dividends
# ===========================================================================


@mcp.tool(
    name="finary_get_upcoming_dividends",
    annotations={
        "title": "Get upcoming dividends",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_upcoming_dividends(params: EmptyInput) -> str:
    """Get the list of upcoming dividends across all investment accounts.

    Returns ex-date, payment date, security name, amount and quantity for each
    upcoming payment.

    Args:
        params: response_format ('markdown' | 'json').

    Returns:
        Markdown table or JSON payload.
    """
    try:
        data = _client.get_dividends()
        return _format_response(data, params.response_format, _render_dividends)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# AUTH MIDDLEWARE — copied verbatim from obsidian_mcp.py
# Same dual-mode: /{token}/ URL path prefix OR Authorization: Bearer header.
# ===========================================================================


class TokenAuthMiddleware:
    """Authenticate requests via either a URL path prefix /{token}/... or
    an `Authorization: Bearer <token>` header. Both schemes are accepted so
    clients that can't customize the request URL (path-only) and clients that
    prefer header-based auth (Bearer) both work."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            headers = scope.get("headers", [])

            # Look for Authorization: Bearer <token>
            auth_header = next(
                (v for k, v in headers if k.lower() == b"authorization"),
                None,
            )
            header_token = None
            if auth_header:
                try:
                    decoded = auth_header.decode("latin-1").strip()
                except Exception:
                    decoded = ""
                if decoded.lower().startswith("bearer "):
                    header_token = decoded[7:].strip()

            path_has_token = bool(API_TOKEN) and path.startswith(f"/{API_TOKEN}")
            header_ok = bool(API_TOKEN) and header_token == API_TOKEN

            if API_TOKEN and not path_has_token and not header_ok:
                async def send_401(send):
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"www-authenticate", b'Bearer realm="mcp"')],
                    })
                    await send({"type": "http.response.body", "body": b"Unauthorized"})
                await send_401(send)
                return

            scope = dict(scope)
            # Strip the token prefix from path only if it's actually there
            if API_TOKEN and path_has_token:
                new_path = path[len(f"/{API_TOKEN}"):] or "/"
                scope["path"] = new_path
                raw_path = scope.get("raw_path", path.encode())
                scope["raw_path"] = raw_path[len(f"/{API_TOKEN}".encode()):] or b"/"
            # Replace Host header with localhost to bypass FastMCP DNS rebinding protection
            new_headers = [(k, v) for k, v in headers if k.lower() != b"host"]
            new_headers.append((b"host", b"localhost"))
            scope["headers"] = new_headers
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    import uvicorn

    print(f"Starting Finary MCP Server on port {PORT}")
    print(f"Session data directory: {FINARY_SESSION_DIR}")
    if not API_TOKEN:
        print("⚠️  WARNING: API_TOKEN is empty — server is unauthenticated!")

    mcp_app = mcp.streamable_http_app()
    app = TokenAuthMiddleware(mcp_app)

    uvicorn.run(app, host="0.0.0.0", port=PORT)
