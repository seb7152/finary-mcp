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

    def _call(self, fn, *args, **kwargs):
        """Call a finary_uapi function, retrying once on empty/non-JSON response.

        Finary JWTs have a short TTL. finary_uapi's helpers call `.json()` blindly,
        so a silent auth expiry surfaces as a `JSONDecodeError` on an empty body.
        On that signal we invalidate the cached session, force `prepare_session()`
        to refresh the JWT via Clerk (using the persisted cookies), and retry once.
        """
        try:
            return fn(self._ensure_session(), *args, **kwargs)
        except json.JSONDecodeError:
            log.info("Finary returned non-JSON; refreshing session and retrying")
            with self._lock:
                self._session = None
            return fn(self._ensure_session(), *args, **kwargs)

    # User / account ----------------------------------------------------

    def get_me(self) -> dict:
        from finary_uapi.user_me import get_user_me
        return self._call(get_user_me)

    def get_holdings_accounts(self) -> dict:
        from finary_uapi.user_holdings_accounts import get_holdings_accounts
        return self._call(get_holdings_accounts, "")

    def get_checking_accounts_transactions(
        self,
        page: int = 1,
        per_page: int = 50,
        query: str = "",
        account_id: str = "",
        institution_id: str = "",
    ) -> dict:
        from finary_uapi.user_portfolio import get_portfolio_checking_accounts_transactions
        return self._call(
            get_portfolio_checking_accounts_transactions,
            page,
            per_page,
            query,
            account_id,
            institution_id,
        )

    # Portfolio aggregates ----------------------------------------------

    def get_timeseries(self, period: str, metric: str) -> dict:
        from finary_uapi.user_portfolio import get_portfolio_timeseries
        if period not in VALID_PERIODS:
            raise ValueError(f"Invalid period '{period}'. Must be one of {sorted(VALID_PERIODS)}.")
        if metric not in VALID_METRICS:
            raise ValueError(f"Invalid metric '{metric}'. Must be one of {sorted(VALID_METRICS)}.")
        api_metric = "finary" if metric == "finance" else metric
        return self._call(get_portfolio_timeseries, period, api_metric)

    def get_dividends(self) -> dict:
        from finary_uapi.user_portfolio import get_portfolio_investments_dividends
        return self._call(get_portfolio_investments_dividends)

    # Asset classes -----------------------------------------------------

    def get_investments(self) -> dict:
        from finary_uapi.user_securities import get_user_securities
        return self._call(get_user_securities)

    def get_cryptos(self) -> dict:
        from finary_uapi.user_cryptos import get_user_cryptos
        return self._call(get_user_cryptos)

    def get_scpis(self) -> dict:
        from finary_uapi.user_scpis import get_user_scpis
        return self._call(get_user_scpis)

    def get_fonds_euro(self) -> dict:
        from finary_uapi.user_fonds_euro import get_user_fonds_euro
        return self._call(get_user_fonds_euro)

    def get_real_estates(self) -> dict:
        from finary_uapi.user_real_estates import get_user_real_estates
        return self._call(get_user_real_estates)

    def get_crowdlendings(self) -> dict:
        from finary_uapi.user_crowdlendings import get_user_crowdlendings
        return self._call(get_user_crowdlendings)

    def get_precious_metals(self) -> dict:
        from finary_uapi.user_precious_metals import get_user_precious_metals
        return self._call(get_user_precious_metals)

    def get_generic_assets(self) -> dict:
        from finary_uapi.user_generic_assets import get_user_generic_assets
        return self._call(get_user_generic_assets)

    def get_startups(self) -> dict:
        from finary_uapi.user_startups import get_user_startups
        return self._call(get_user_startups)


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
        for key in ("data", "items", "results", "assets", "timeseries"):
            if key in data and isinstance(data[key], list):
                return data[key]
    return []


def _normalize_timeseries(payload: Any) -> list[dict]:
    """Normalize a Finary timeseries payload to a list of {date, value} dicts.

    Finary returns `{"result": {"timeseries": [[iso_date, breakdown], ...]}}` where
    `breakdown` is a dict with one sub-dict per asset class plus a "total" sub-dict
    holding the aggregate `amount` for the requested metric.
    """
    raw = _to_list(payload)
    points: list[dict] = []
    for entry in raw:
        if isinstance(entry, (list, tuple)) and len(entry) == 2:
            date, breakdown = entry
            if isinstance(breakdown, dict):
                total = breakdown.get("total")
                value = total.get("amount") if isinstance(total, dict) else None
                points.append({"date": date, "value": value})
        elif isinstance(entry, dict):
            points.append({
                "date": _pick(entry, "date", "timestamp", "x"),
                "value": _pick(entry, "value", "amount", "y"),
            })
    return points


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
            "Call the finary_signin tool to refresh it (it will ask for an MFA "
            "code if 2FA is enabled on the account)."
        )
    if isinstance(e, json.JSONDecodeError):
        # finary_uapi calls .json() blindly; if the API returns an empty body
        # (typically a silent auth failure) we surface a clear hint.
        return (
            "Error: Finary returned an empty/non-JSON response. The session likely "
            "expired silently. Call the finary_signin tool to refresh it."
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


def _render_transactions(payload: Any) -> str:
    items = _to_list(payload)
    if not items:
        return "# Transactions\n\n_Aucune transaction._"
    lines = [
        "# Transactions comptes courants\n",
        "| Date | Libellé | Compte | Catégorie | Montant |",
        "| --- | --- | --- | --- | ---: |",
    ]
    total = 0.0
    for t in items:
        date = _pick(t, "date", "transaction_date", "value_date") or ""
        label = _pick(t, "name", "label", "description", "wording") or "?"
        account = t.get("account") if isinstance(t.get("account"), dict) else {}
        account_name = account.get("name") if account else _pick(t, "account_name") or ""
        cat = t.get("category") if isinstance(t.get("category"), dict) else {}
        cat_name = cat.get("name") if cat else _pick(t, "category_name") or ""
        amount = _pick(t, "amount", "display_amount", "value")
        try:
            total += float(amount)
        except (TypeError, ValueError):
            pass
        lines.append(f"| {date} | {label} | {account_name} | {cat_name} | {_fmt_eur(amount)} |")
    lines.append(f"\n**Total** ({len(items)} transactions) : {_fmt_eur(total)}")
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
    series = _normalize_timeseries(payload)
    if not series:
        return "# Historique\n\n_Aucune donnée._"
    first, last = series[0], series[-1]
    first_val, last_val = first["value"], last["value"]
    delta = None
    if isinstance(first_val, (int, float)) and isinstance(last_val, (int, float)) and first_val:
        delta = (last_val - first_val) / first_val * 100
    return "\n".join([
        "# Historique de patrimoine\n",
        f"- **Du** : {first['date']}",
        f"- **Au** : {last['date']}",
        f"- **Valeur initiale** : {_fmt_eur(first_val)}",
        f"- **Valeur finale** : {_fmt_eur(last_val)}",
        f"- **Évolution** : {_fmt_pct(delta) if delta is not None else 'n/a'}",
        f"- **Points** : {len(series)}",
    ])


def _render_net_worth(payload: Any, label: str) -> str:
    series = _normalize_timeseries(payload)
    if not series:
        return f"# {label}\n\n_Aucune donnée._"
    last = series[-1]
    first_val, last_val = series[0]["value"], last["value"]
    delta = None
    if isinstance(first_val, (int, float)) and isinstance(last_val, (int, float)) and first_val:
        delta = (last_val - first_val) / first_val * 100
    return "\n".join([
        f"# {label}\n",
        f"**Valeur actuelle** : {_fmt_eur(last_val)}",
        f"**Variation sur la période** : {_fmt_pct(delta) if delta is not None else 'n/a'}",
        f"**Dernier point** : {last['date']}",
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


class TransactionsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(
        default=1,
        description="Page number (1-based). Use -1 to fetch all pages.",
    )
    perpage: int = Field(
        default=50,
        description="Items per page (default 50).",
    )
    account_ids: str = Field(
        default="",
        description="Filter by holding account id(s). Comma-separated for multiple.",
    )
    institution_ids: str = Field(
        default="",
        description="Filter by institution id(s). Comma-separated for multiple.",
    )
    query: str = Field(
        default="",
        description="Free-text search on transaction labels.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


class SigninInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    otp_code: str = Field(
        default="",
        description="6-digit TOTP code from your authenticator app. Leave empty on the first call to discover whether MFA is required.",
    )


# ===========================================================================
# TOOLS — Auth
# ===========================================================================


@mcp.tool(
    name="finary_signin",
    annotations={
        "title": "Sign in to Finary (refresh expired session)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def finary_signin(params: SigninInput) -> str:
    """Refresh the Finary session by signing in with the configured credentials.

    Call without otp_code first. If the response asks for an OTP, ask the user
    for their 6-digit TOTP code (from their authenticator app) and call again
    with otp_code set. On success, cookies are persisted to /data/cookies.json
    and the in-memory session is reset so subsequent tools use the new auth.
    """
    from finary_uapi.signin import signin as _signin

    os.makedirs(FINARY_SESSION_DIR, exist_ok=True)
    os.chdir(FINARY_SESSION_DIR)
    try:
        _signin(params.otp_code)
    except RuntimeError as e:
        msg = str(e)
        if "OTP code is required" in msg:
            return (
                "MFA required. Ask the user for their 6-digit TOTP code from "
                "their authenticator app, then call finary_signin again with "
                "otp_code set."
            )
        return f"Sign-in failed: {msg}"
    except Exception as e:  # noqa: BLE001
        return f"Sign-in failed: {type(e).__name__}: {e}"

    _client._session = None
    return "Signed in. Session cookies refreshed; all other tools are now usable."


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
        if not isinstance(u, dict) or not u:
            return (
                "Error: Finary returned an empty profile for /users/me. "
                "The session likely expired silently. Call the finary_signin tool "
                "to refresh it."
            )
        plan = u.get("plan")
        plan_name = plan.get("name") if isinstance(plan, dict) else plan or "n/a"
        full_name = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip() or "n/a"
        return (
            "# Profil Finary\n\n"
            f"- **Nom** : {full_name}\n"
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
# TOOLS — Transactions
# ===========================================================================


@mcp.tool(
    name="finary_get_transactions",
    annotations={
        "title": "Get checking account transactions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_transactions(params: TransactionsInput) -> str:
    """List transactions across connected checking (current) accounts.

    Supports pagination and filtering by account, institution or free-text query.

    Args:
        params:
            - page: page number (1-based), or -1 to fetch all pages
            - perpage: items per page (default 50)
            - account_ids: filter by holding account id(s), comma-separated
            - institution_ids: filter by institution id(s), comma-separated
            - query: free-text search on transaction labels
            - response_format: 'markdown' | 'json'

    Returns:
        Markdown table of transactions or full JSON payload.
    """
    try:
        data = _client.get_checking_accounts_transactions(
            page=params.page,
            per_page=params.perpage,
            query=params.query,
            account_id=params.account_ids,
            institution_id=params.institution_ids,
        )
        return _format_response(data, params.response_format, _render_transactions)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# TOOLS — Position lookup (find a holding by ticker / ISIN / name)
# ===========================================================================


class MatchBy(str, Enum):
    AUTO = "auto"
    TICKER = "ticker"
    ISIN = "isin"
    NAME = "name"


class PositionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        description="Ticker/symbol, ISIN, or (part of) a name to look up. "
        "Examples: 'SPOT', 'US85207U1043', 'Spotify'.",
    )
    match_by: MatchBy = Field(
        default=MatchBy.AUTO,
        description=(
            "How to interpret `query`. 'auto' (default) matches an exact ticker or "
            "ISIN, or a substring of the ticker/name. 'ticker'/'isin' force an exact "
            "identifier match; 'name' forces a case-insensitive substring on the name."
        ),
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


# Sub-objects that, when present on a position, carry the instrument identity.
_INSTRUMENT_SUBKEYS = ("security", "crypto", "fonds_euro", "scpi", "precious_metal", "asset")


def _amount(value: Any) -> Any:
    """Finary sometimes wraps a monetary value in a dict ({eur, display, ...})."""
    if isinstance(value, dict):
        return _pick(value, "eur", "value", "amount")
    return value


def _fmt_qty(value: Any) -> str:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value) if value is not None else "—"
    if f == int(f):
        return f"{int(f):,}".replace(",", " ")
    return f"{f:,.6f}".replace(",", " ").replace(".", ",").rstrip("0").rstrip(",")


def _position_identity(pos: dict) -> tuple:
    """Return (symbol, isin, name, instrument_dict) for a position.

    A position usually nests its instrument under one of `_INSTRUMENT_SUBKEYS`;
    some flat endpoints expose the identity fields directly on the position.
    """
    instrument = None
    for k in _INSTRUMENT_SUBKEYS:
        v = pos.get(k)
        if isinstance(v, dict):
            instrument = v
            break
    src = instrument if instrument is not None else pos
    symbol = _pick(src, "symbol", "ticker", "code")
    isin = _pick(src, "isin")
    name = _pick(src, "name", "display_name", "fullname")
    return symbol, isin, name, instrument


def _account_meta(acc: dict) -> dict:
    inst = acc.get("institution") if isinstance(acc.get("institution"), dict) else {}
    return {
        "institution": (inst.get("name") if inst else None) or _pick(acc, "institution_name"),
        "account": _pick(acc, "name", "display_name") or "?",
        "account_id": acc.get("id"),
    }


def _iter_account_positions(holdings_payload: Any):
    """Yield position records (with institution/account context) from holdings_accounts.

    Walks every list-valued field of each account and keeps items that expose an
    instrument identity (a nested security/crypto/... sub-object, or a direct
    symbol/ISIN). This stays resilient to Finary renaming its per-class arrays.
    """
    for acc in _to_list(holdings_payload):
        if not isinstance(acc, dict):
            continue
        meta = _account_meta(acc)
        for key, val in acc.items():
            if not isinstance(val, list):
                continue
            for item in val:
                if not isinstance(item, dict):
                    continue
                symbol, isin, name, instrument = _position_identity(item)
                if instrument is None and not symbol and not isin:
                    continue
                yield {
                    **meta, "kind": key, "position": item,
                    "symbol": symbol, "isin": isin, "name": name,
                }


def _iter_flat_positions():
    """Fallback: walk the flat securities/cryptos endpoints when holdings_accounts
    does not nest positions. Institution/account context is taken from the item
    itself when available."""
    sources = [("securities", _client.get_investments), ("cryptos", _client.get_cryptos)]
    for kind, fn in sources:
        try:
            items = _to_list(fn())
        except Exception:  # noqa: BLE001
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            symbol, isin, name, instrument = _position_identity(item)
            if instrument is None and not symbol and not isin:
                continue
            acc = item.get("account") if isinstance(item.get("account"), dict) else {}
            if not acc and isinstance(item.get("holding_account"), dict):
                acc = item["holding_account"]
            meta = _account_meta(acc) if acc else {"institution": None, "account": "?", "account_id": None}
            yield {**meta, "kind": kind, "position": item,
                   "symbol": symbol, "isin": isin, "name": name}


def _position_matches(query: str, match_by: MatchBy, symbol, isin, name) -> bool:
    q = query.strip().lower()
    if not q:
        return False
    sym = (symbol or "").lower()
    isn = (isin or "").lower()
    nm = (name or "").lower()
    if match_by == MatchBy.TICKER:
        return q == sym
    if match_by == MatchBy.ISIN:
        return q == isn
    if match_by == MatchBy.NAME:
        return q in nm
    # auto: exact ticker/ISIN, else substring on ticker or name
    return q == sym or q == isn or q in sym or q in nm


def _group_matches(records, query: str, match_by: MatchBy) -> list:
    """Filter records by the query and group them per instrument (so the same
    ticker held in several accounts is collapsed into one entry)."""
    groups: dict = {}
    order: list = []
    for rec in records:
        if not _position_matches(query, match_by, rec["symbol"], rec["isin"], rec["name"]):
            continue
        key = (rec["isin"] or "").lower() or (rec["symbol"] or "").lower() or (rec["name"] or "").lower()
        if key not in groups:
            groups[key] = {"name": rec["name"], "symbol": rec["symbol"], "isin": rec["isin"], "rows": []}
            order.append(key)
        pos = rec["position"]
        groups[key]["rows"].append({
            "institution": rec["institution"],
            "account": rec["account"],
            "account_id": rec["account_id"],
            "kind": rec["kind"],
            "quantity": _pick(pos, "quantity", "shares", "units"),
            "value": _amount(_pick(
                pos, "display_current_value", "current_value", "display_value",
                "value", "amount", "display_balance", "balance",
            )),
            "buying": _amount(_pick(pos, "display_buying_price", "buying_price")),
            "perf": _amount(_pick(
                pos, "display_unrealized_performance", "display_diff_gain",
                "performance", "variation_percentage",
            )),
        })
    return [groups[k] for k in order]


def _render_positions(groups: list, query: str) -> str:
    if not groups:
        return (
            f"# Position « {query} »\n\n"
            "_Aucune position trouvée._ Vérifiez le ticker/ISIN/nom, ou réessayez "
            "avec `match_by='name'` pour une recherche partielle sur le nom."
        )
    out: list[str] = []
    for g in groups:
        header = g["name"] or g["symbol"] or g["isin"] or "?"
        ident_bits = []
        if g["symbol"]:
            ident_bits.append(f"ticker `{g['symbol']}`")
        if g["isin"]:
            ident_bits.append(f"ISIN `{g['isin']}`")
        out.append(f"## {header}" + (f" — {', '.join(ident_bits)}" if ident_bits else ""))
        out.append("")
        out.append("| Institution | Compte | Quantité | Valeur | PRU | Perf. |")
        out.append("| --- | --- | ---: | ---: | ---: | ---: |")
        total_qty = 0.0
        total_val = 0.0
        for r in g["rows"]:
            try:
                total_qty += float(r["quantity"])
            except (TypeError, ValueError):
                pass
            try:
                total_val += float(r["value"])
            except (TypeError, ValueError):
                pass
            buying = _fmt_eur(r["buying"]) if r["buying"] is not None else "—"
            perf = _fmt_pct(r["perf"]) if r["perf"] is not None else "—"
            out.append(
                f"| {r['institution'] or '—'} | {r['account']} | {_fmt_qty(r['quantity'])} | "
                f"{_fmt_eur(r['value'])} | {buying} | {perf} |"
            )
        if len(g["rows"]) > 1:
            out.append(
                f"| **Total** | {len(g['rows'])} compte(s) | **{_fmt_qty(total_qty)}** | "
                f"**{_fmt_eur(total_val)}** | | |"
            )
        out.append("")
    return "\n".join(out).rstrip()


@mcp.tool(
    name="finary_get_position",
    annotations={
        "title": "Find a position by ticker / ISIN / name",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_position(params: PositionInput) -> str:
    """Find a single holding (a "position") by ticker, ISIN or name, across every account.

    This avoids downloading the whole portfolio: it fetches the holdings once,
    filters server-side, and returns only the matching position(s) — so it stays
    cheap in tokens. An instrument held in several accounts (e.g. SPOT on both
    IBKR and Trade Republic) is grouped together, with a per-account breakdown
    (institution, account, quantity, value, average buying price, performance)
    and aggregated totals.

    Args:
        params:
            - query: ticker (e.g. 'SPOT'), ISIN (e.g. 'US85207U1043') or name ('Spotify').
            - match_by: 'auto' (default), 'ticker', 'isin' or 'name'.
            - response_format: 'markdown' (default) or 'json'.

    Returns:
        Markdown breakdown grouped per instrument, or the matching positions as JSON.
    """
    try:
        records = list(_iter_account_positions(_client.get_holdings_accounts()))
        # Fallback for portfolios where holdings_accounts does not nest positions.
        if not records:
            records = list(_iter_flat_positions())
        groups = _group_matches(records, params.query, params.match_by)
        if params.response_format == ResponseFormat.JSON:
            payload = [
                {
                    "name": g["name"], "symbol": g["symbol"], "isin": g["isin"],
                    "accounts_count": len(g["rows"]),
                    "total_quantity": sum(
                        float(r["quantity"]) for r in g["rows"]
                        if isinstance(r["quantity"], (int, float))
                    ),
                    "total_value": sum(
                        float(r["value"]) for r in g["rows"]
                        if isinstance(r["value"], (int, float))
                    ),
                    "positions": [
                        {
                            "institution": r["institution"], "account": r["account"],
                            "account_id": r["account_id"], "kind": r["kind"],
                            "quantity": r["quantity"], "value": r["value"],
                            "buying_price": r["buying"], "performance": r["perf"],
                        }
                        for r in g["rows"]
                    ],
                }
                for g in groups
            ]
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        return _render_positions(groups, params.query)
    except Exception as e:  # noqa: BLE001
        return _handle_error(e)


# ===========================================================================
# TOOLS — Account / portfolio lookup (all holdings of one account or broker)
# ===========================================================================


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(
        description="Account name, account id, or institution name to fetch. "
        "Examples: 'IBKR', 'Interactive Brokers', 'Trade Republic', 'PEA'.",
    )
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN)


def _account_query_matches(query: str, meta: dict) -> bool:
    q = query.strip().lower()
    if not q:
        return False
    inst = (meta.get("institution") or "").lower()
    acc = (meta.get("account") or "").lower()
    acc_id = str(meta.get("account_id") or "").lower()
    return q == acc_id or q in inst or q in acc


def _group_by_account(records, query: str) -> list:
    """Keep records whose account/institution matches the query and group them
    per account (so 'IBKR' returns every line held in the IBKR account(s))."""
    groups: dict = {}
    order: list = []
    for rec in records:
        if not _account_query_matches(query, rec):
            continue
        key = str(rec.get("account_id") or "") or f"{rec['institution']}|{rec['account']}"
        if key not in groups:
            groups[key] = {
                "institution": rec["institution"], "account": rec["account"],
                "account_id": rec["account_id"], "rows": [],
            }
            order.append(key)
        pos = rec["position"]
        groups[key]["rows"].append({
            "name": rec["name"], "symbol": rec["symbol"], "isin": rec["isin"],
            "kind": rec["kind"],
            "quantity": _pick(pos, "quantity", "shares", "units"),
            "value": _amount(_pick(
                pos, "display_current_value", "current_value", "display_value",
                "value", "amount", "display_balance", "balance",
            )),
            "buying": _amount(_pick(pos, "display_buying_price", "buying_price")),
            "perf": _amount(_pick(
                pos, "display_unrealized_performance", "display_diff_gain",
                "performance", "variation_percentage",
            )),
        })
    return [groups[k] for k in order]


def _render_accounts(groups: list, query: str) -> str:
    if not groups:
        return (
            f"# Portefeuille « {query} »\n\n"
            "_Aucun compte correspondant._ Essayez le nom de l'institution "
            "(ex. 'Interactive Brokers') ou utilisez `finary_list_holdings_accounts` "
            "pour voir les comptes disponibles."
        )
    out: list[str] = []
    for g in groups:
        title = " — ".join(x for x in (g["institution"], g["account"]) if x) or "?"
        out.append(f"## {title}")
        out.append("")
        out.append("| Instrument | Ticker | ISIN | Quantité | Valeur | PRU | Perf. |")
        out.append("| --- | --- | --- | ---: | ---: | ---: | ---: |")
        total_val = 0.0
        for r in g["rows"]:
            try:
                total_val += float(r["value"])
            except (TypeError, ValueError):
                pass
            name = r["name"] or r["symbol"] or r["isin"] or "?"
            buying = _fmt_eur(r["buying"]) if r["buying"] is not None else "—"
            perf = _fmt_pct(r["perf"]) if r["perf"] is not None else "—"
            out.append(
                f"| {name} | {r['symbol'] or '—'} | {r['isin'] or '—'} | "
                f"{_fmt_qty(r['quantity'])} | {_fmt_eur(r['value'])} | {buying} | {perf} |"
            )
        out.append(
            f"| **Total** | | | | **{_fmt_eur(total_val)}** | | "
            f"_{len(g['rows'])} ligne(s)_ |"
        )
        out.append("")
    return "\n".join(out).rstrip()


@mcp.tool(
    name="finary_get_account",
    annotations={
        "title": "Get all holdings of an account / broker",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def finary_get_account(params: AccountInput) -> str:
    """Get every holding of one account or broker (a whole "portfolio").

    Like finary_get_position but filtered by account instead of by instrument:
    it fetches the holdings once, keeps only the lines held in the matching
    account(s), and returns them with a per-account total — so dumping the IBKR
    portfolio no longer means downloading everything. The query matches an
    institution name (e.g. 'Interactive Brokers'), an account name (e.g. 'IBKR',
    'PEA') or an account id; a broker with several accounts yields one section
    per account.

    Args:
        params:
            - query: institution name, account name, or account id.
            - response_format: 'markdown' (default) or 'json'.

    Returns:
        Markdown holdings grouped per account, or the holdings as JSON.
    """
    try:
        records = list(_iter_account_positions(_client.get_holdings_accounts()))
        if not records:
            records = list(_iter_flat_positions())
        groups = _group_by_account(records, params.query)
        if params.response_format == ResponseFormat.JSON:
            payload = [
                {
                    "institution": g["institution"], "account": g["account"],
                    "account_id": g["account_id"], "lines_count": len(g["rows"]),
                    "total_value": sum(
                        float(r["value"]) for r in g["rows"]
                        if isinstance(r["value"], (int, float))
                    ),
                    "positions": [
                        {
                            "name": r["name"], "symbol": r["symbol"], "isin": r["isin"],
                            "kind": r["kind"], "quantity": r["quantity"],
                            "value": r["value"], "buying_price": r["buying"],
                            "performance": r["perf"],
                        }
                        for r in g["rows"]
                    ],
                }
                for g in groups
            ]
            return json.dumps(payload, indent=2, ensure_ascii=False, default=str)
        return _render_accounts(groups, params.query)
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
