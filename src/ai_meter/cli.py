#!/usr/bin/env python3
"""
AI Meter: AI plan limits, tokens and cost monitor.

Collects live spend, credits and caps from provider APIs, tracks renewal
dates and manual usage readings for chat subscriptions and other SaaS,
sends alerts (Telegram / webhook) and serves a small web dashboard.

Commands:
  collect            poll every API account, store a snapshot, send alerts
  serve              run the dashboard (default 127.0.0.1:8787)
  status             print a summary in the terminal
  log ID VALUE       record a manual usage reading for a subscription
  test-alert         send a test message to every enabled alert channel
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
import yaml

UTC = dt.timezone.utc
HERE = Path(__file__).resolve().parent
log = logging.getLogger("aimeter")
HTTP_TIMEOUT = 30


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def default_config_path() -> Path:
    """~/.aimeter/config.yaml (or $AIMETER_HOME/config.yaml), created on first run.
    An existing ~/.aimon folder from before the rename keeps being used."""
    env_home = os.environ.get("AIMETER_HOME") or os.environ.get("AIMON_HOME")
    home = Path(env_home or "~/.aimeter").expanduser()
    legacy = Path("~/.aimon").expanduser()
    if not env_home and not home.exists() and (legacy / "config.yaml").exists():
        home = legacy
    p = home / "config.yaml"
    if not p.exists():
        home.mkdir(parents=True, exist_ok=True)
        starter = HERE / "config.starter.yaml"
        p.write_text(starter.read_text(encoding="utf-8") if starter.exists() else "settings: {}\napi_accounts: []\nsubscriptions: []\n",
                     encoding="utf-8")
        log.info("created %s", p)
    return p


def load_config(path: str | None) -> dict:
    cfg_path = Path(path).expanduser() if path else default_config_path()
    load_env_file(cfg_path.parent / ".env")
    with open(cfg_path, encoding="utf-8-sig") as f:
        cfg = yaml.safe_load(f) or {}
    s = cfg.setdefault("settings", {})
    s.setdefault("db_path", "data/monitor.db")
    s.setdefault("alert_thresholds", [0.5, 0.8, 0.95])
    s.setdefault("renewal_reminder_days", [7, 3, 1])
    s.setdefault("fx_to_usd", {"USD": 1})
    s["fx_to_usd"].setdefault("USD", 1)
    s.setdefault("dashboard", {})
    db = Path(s["db_path"])
    s["db_path"] = str(db if db.is_absolute() else cfg_path.parent / db)
    cfg.setdefault("api_accounts", [])
    cfg.setdefault("subscriptions", [])
    cfg.setdefault("alerts", {})
    return cfg


def env(name: str | None) -> str | None:
    """Read an env var; AIMETER_* names also accept the old AIMON_* spelling."""
    if not name:
        return None
    val = os.environ.get(name)
    if val is None and name.startswith("AIMETER_"):
        val = os.environ.get("AIMON_" + name[len("AIMETER_"):])
    return val


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    account_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    metrics TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_snap_acct_ts ON snapshots(account_id, ts);
CREATE TABLE IF NOT EXISTS manual_usage (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    sub_id TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    lim REAL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_manual_sub_ts ON manual_usage(sub_id, ts);
CREATE TABLE IF NOT EXISTS alerts_sent (
    key TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    message TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts TEXT NOT NULL
);
"""


def db_connect(cfg: dict) -> sqlite3.Connection:
    Path(cfg["settings"]["db_path"]).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(cfg["settings"]["db_path"], timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        os.chmod(cfg["settings"]["db_path"], 0o600)  # holds OAuth tokens
    except OSError:
        pass
    return con


def kv_get(con, key: str):
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def kv_set(con, key: str, value) -> None:
    if value is None:
        con.execute("DELETE FROM kv WHERE key=?", (key,))
    else:
        con.execute("INSERT OR REPLACE INTO kv(key, value, ts) VALUES (?,?,?)",
                    (key, json.dumps(value), iso(now_utc())))
    con.commit()


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def iso(t: dt.datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def latest_snapshot(con, account_id: str, ok_only=False):
    q = "SELECT * FROM snapshots WHERE account_id=?"
    if ok_only:
        q += " AND ok=1"
    row = con.execute(q + " ORDER BY ts DESC LIMIT 1", (account_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else {}
    return d


def latest_manual(con, sub_id: str):
    row = con.execute(
        "SELECT * FROM manual_usage WHERE sub_id=? ORDER BY ts DESC LIMIT 1", (sub_id,)
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# Collectors. Each returns a metrics dict:
#   spend_mtd, spend_today        USD spent this calendar month (UTC) / today
#   daily: [{date, usd}]          per-day spend this month
#   by_model: {name: usd}         spend breakdown
#   tokens_by_model: {name: n}    input+output tokens this month
#   balance                       prepaid credit left (USD), if known
#   cap, cap_remaining            provider-side spending cap, if known
# --------------------------------------------------------------------------

def month_start(t: dt.datetime) -> dt.datetime:
    return t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _get(url, headers, params=None):
    r = requests.get(url, headers=headers, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} from {urlparse(url).path}: {r.text[:300]}")
    return r.json()


def _paginate(url, headers, params, next_key="next_page", page_param="page", max_pages=20):
    params = dict(params)
    for _ in range(max_pages):
        data = _get(url, headers, params)
        yield data
        if not data.get("has_more") or not data.get(next_key):
            return
        params[page_param] = data[next_key]


def _finish(daily: dict, by_model: dict, today: str) -> dict:
    return {
        "spend_mtd": round(sum(daily.values()), 4),
        "spend_today": round(daily.get(today, 0.0), 4),
        "daily": [{"date": d, "usd": round(v, 4)} for d, v in sorted(daily.items())],
        "by_model": {k: round(v, 4) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
    }


# A "window" is one limit a provider enforces, shown as one gauge in a widget:
#   {key, label, used_pct (0-100), resets_at (ISO UTC), window_seconds,
#    limit, remaining, unit}

def window(key, label, used_pct, resets_at=None, window_seconds=None, limit=None, remaining=None, unit=None):
    if isinstance(resets_at, (int, float)):
        resets_at = iso(dt.datetime.fromtimestamp(resets_at, UTC))
    elif isinstance(resets_at, dt.datetime):
        resets_at = iso(resets_at)
    elif isinstance(resets_at, str) and resets_at:
        try:
            resets_at = iso(dt.datetime.fromisoformat(resets_at.replace("Z", "+00:00")))
        except ValueError:
            pass
    return {
        "key": key, "label": label,
        "used_pct": None if used_pct is None else round(max(0.0, min(999.0, float(used_pct))), 1),
        "resets_at": resets_at or None, "window_seconds": window_seconds,
        "limit": limit, "remaining": remaining, "unit": unit,
    }


def next_month_start(t: dt.datetime) -> dt.datetime:
    m = month_start(t)
    return (m + dt.timedelta(days=32)).replace(day=1)


def openrouter_reset(kind):
    now = now_utc()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return midnight + dt.timedelta(days=1)
    if kind == "weekly":
        return midnight + dt.timedelta(days=7 - now.weekday())
    if kind == "monthly":
        return next_month_start(now)
    return None


def budget_windows(acct: dict, m: dict) -> list:
    out = []
    if acct.get("monthly_budget") and m.get("spend_mtd") is not None:
        b = float(acct["monthly_budget"])
        out.append(window("budget", "Monthly budget", m["spend_mtd"] / b * 100, resets_at=next_month_start(now_utc()),
                          limit=b, remaining=round(b - m["spend_mtd"], 2), unit="USD"))
    if acct.get("daily_budget") and m.get("spend_today") is not None:
        b = float(acct["daily_budget"])
        nxt = now_utc().replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        out.append(window("daily", "Daily budget", m["spend_today"] / b * 100, resets_at=nxt,
                          limit=b, remaining=round(b - m["spend_today"], 2), unit="USD"))
    return out


class TokenAgg:
    """Collects token counts and cost per model and per day into one shape:
    token_usage = {totals, today, by_model[], daily[]} (totals = this calendar month, UTC)."""
    FIELDS = ("input", "output", "cache", "reasoning", "cost", "requests")

    def __init__(self, now=None):
        now = now or now_utc()
        self.today = now.strftime("%Y-%m-%d")
        self.month = now.strftime("%Y-%m")
        self.models, self.days = {}, {}
        self.tot = dict.fromkeys(self.FIELDS, 0.0)
        self.td = dict.fromkeys(self.FIELDS, 0.0)
        self.has_cost = False

    def add(self, day, model, input=0, output=0, cache=0, reasoning=0, cost=None, requests=0):
        vals = {"input": input or 0, "output": output or 0, "cache": cache or 0, "reasoning": reasoning or 0,
                "cost": cost or 0, "requests": requests or 0}
        if cost is not None:
            self.has_cost = True
        d = self.days.setdefault(day, {"tokens": 0, "cost": 0.0})
        d["tokens"] += vals["input"] + vals["output"] + vals["cache"]
        d["cost"] += vals["cost"]
        if not day.startswith(self.month):
            return
        m = self.models.setdefault(model or "other", dict.fromkeys(self.FIELDS, 0.0))
        for k, v in vals.items():
            m[k] += v
            self.tot[k] += v
            if day == self.today:
                self.td[k] += v

    def set_model_cost(self, model, cost):
        if model in self.models:
            self.models[model]["cost"] = cost
            self.has_cost = True

    def result(self, source=None):
        def clean(x):
            out = {k: (round(v, 4) if k == "cost" else int(v)) for k, v in x.items()}
            if not self.has_cost:
                out["cost"] = None
            return out
        rows = [{"model": k, **clean(v)} for k, v in self.models.items()]
        rows.sort(key=lambda r: -((r["input"] + r["output"] + r["cache"]) or 0))
        return {"source": source, "totals": clean(self.tot), "today": clean(self.td), "by_model": rows,
                "daily": [{"date": d, "tokens": int(v["tokens"]), "cost": round(v["cost"], 4)}
                          for d, v in sorted(self.days.items())]}


def collect_anthropic(acct: dict, con=None) -> dict:
    """Anthropic Admin API (needs an sk-ant-admin... key)."""
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "aimeter/1.0"}
    base = acct.get("base_url", "https://api.anthropic.com")
    now = now_utc()
    start = month_start(now)
    today = now.strftime("%Y-%m-%d")

    daily, by_model = {}, {}
    params = {
        "starting_at": iso(start),
        "ending_at": iso(now + dt.timedelta(days=1)),
        "group_by[]": "description",
        "limit": 31,
    }
    for page in _paginate(f"{base}/v1/organizations/cost_report", h, params):
        for bucket in page.get("data", []):
            day = str(bucket.get("starting_at", ""))[:10]
            for res in bucket.get("results", []):
                usd = float(res.get("amount") or 0) / 100.0  # amounts are in cents
                daily[day] = daily.get(day, 0.0) + usd
                name = res.get("model") or res.get("description") or res.get("cost_type") or "other"
                by_model[name] = by_model.get(name, 0.0) + usd
    out = _finish(daily, by_model, today)

    agg = TokenAgg(now)
    try:
        uparams = {
            "starting_at": iso(start),
            "ending_at": iso(now + dt.timedelta(days=1)),
            "group_by[]": "model",
            "bucket_width": "1d",
            "limit": 31,
        }
        for page in _paginate(f"{base}/v1/organizations/usage_report/messages", h, uparams):
            for bucket in page.get("data", []):
                day = str(bucket.get("starting_at", ""))[:10]
                for res in bucket.get("results", []):
                    cc = res.get("cache_creation") or {}
                    agg.add(day, res.get("model") or "other",
                            input=res.get("uncached_input_tokens") or 0,
                            output=res.get("output_tokens") or 0,
                            cache=(res.get("cache_read_input_tokens") or 0)
                            + sum(v or 0 for v in cc.values() if isinstance(v, (int, float))))
        for m, usd in out["by_model"].items():
            agg.set_model_cost(m, usd)
        out["token_usage"] = agg.result("Anthropic usage report")
    except Exception as e:  # tokens are a nice-to-have
        log.warning("anthropic usage_report failed: %s", e)
    out["windows"] = budget_windows(acct, out)
    return out


def collect_openai(acct: dict, con=None) -> dict:
    """OpenAI Platform org Usage/Costs API (needs an admin key)."""
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if acct.get("org_id"):
        h["OpenAI-Organization"] = acct["org_id"]
    base = acct.get("base_url", "https://api.openai.com")
    now = now_utc()
    start = int(month_start(now).timestamp())
    today = now.strftime("%Y-%m-%d")

    daily, by_line = {}, {}
    params = {"start_time": start, "bucket_width": "1d", "limit": 31, "group_by": "line_item"}
    for page in _paginate(f"{base}/v1/organization/costs", h, params):
        for bucket in page.get("data", []):
            day = dt.datetime.fromtimestamp(bucket.get("start_time", 0), UTC).strftime("%Y-%m-%d")
            for res in bucket.get("results", []):
                amt = res.get("amount") or {}
                usd = float(amt.get("value") or 0)
                daily[day] = daily.get(day, 0.0) + usd
                name = res.get("line_item") or "other"
                by_line[name] = by_line.get(name, 0.0) + usd
    out = _finish(daily, by_line, today)

    try:
        agg = TokenAgg(now)
        uparams = {"start_time": start, "bucket_width": "1d", "limit": 31, "group_by": "model"}
        for page in _paginate(f"{base}/v1/organization/usage/completions", h, uparams):
            for bucket in page.get("data", []):
                day = dt.datetime.fromtimestamp(bucket.get("start_time", 0), UTC).strftime("%Y-%m-%d")
                for res in bucket.get("results", []):
                    cached = res.get("input_cached_tokens") or 0
                    agg.add(day, res.get("model") or "other",
                            input=(res.get("input_tokens") or 0) - cached, output=res.get("output_tokens") or 0,
                            cache=cached, requests=res.get("num_model_requests") or 0)
        tu = agg.result("OpenAI usage API")
        tu["totals"]["cost"] = out["spend_mtd"]  # cost comes per line item, not per model
        tu["today"]["cost"] = out["spend_today"]
        day_cost = {d["date"]: d["usd"] for d in out["daily"]}
        for d in tu["daily"]:
            d["cost"] = day_cost.get(d["date"], 0.0)
        out["token_usage"] = tu
    except Exception as e:
        log.warning("openai usage/completions failed: %s", e)
    out["windows"] = budget_windows(acct, out)
    return out


def collect_openrouter(acct: dict, con=None) -> dict:
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    h = {"Authorization": f"Bearer {key}"}
    k = _get("https://openrouter.ai/api/v1/key", h).get("data", {})
    today = now_utc().strftime("%Y-%m-%d")
    out = {
        "spend_mtd": round(float(k.get("usage_monthly") or 0), 4),
        "spend_today": round(float(k.get("usage_daily") or 0), 4),
        "spend_all_time": round(float(k.get("usage") or 0), 4),
        "daily": [{"date": today, "usd": round(float(k.get("usage_daily") or 0), 4)}],
        "by_model": {},
        "cap": k.get("limit"),
        "cap_remaining": k.get("limit_remaining"),
        "cap_reset": k.get("limit_reset"),
        "free_tier": k.get("is_free_tier"),
    }
    try:
        c = _get("https://openrouter.ai/api/v1/credits", h).get("data", {})
        out["balance"] = round(float(c.get("total_credits", 0)) - float(c.get("total_usage", 0)), 4)
    except Exception as e:
        log.info("openrouter /credits unavailable for this key: %s", e)
    mkey = env(acct.get("management_key_env"))
    if mkey:  # per-model tokens + cost for the last 30 days (management keys only)
        try:
            act = _get("https://openrouter.ai/api/v1/activity", {"Authorization": f"Bearer {mkey}"}).get("data") or []
            agg = TokenAgg()
            for r in act:
                agg.add(r.get("date", ""), r.get("model") or r.get("model_permaslug"),
                        input=r.get("prompt_tokens") or 0, output=r.get("completion_tokens") or 0,
                        reasoning=r.get("reasoning_tokens") or 0,
                        cost=float(r.get("usage") or 0) + float(r.get("byok_usage_inference") or 0),
                        requests=r.get("requests") or 0)
            tu = agg.result("OpenRouter activity (completed days)")
            tu["today"]["cost"] = out["spend_today"]  # activity covers completed days only
            out["token_usage"] = tu
        except Exception as e:
            out["token_note"] = f"activity API: {str(e)[:120]}"
    w = []
    if k.get("limit"):
        lim, rem = float(k["limit"]), float(k.get("limit_remaining") or 0)
        w.append(window("cap", f"Key cap ({k.get('limit_reset') or 'total'})", (1 - rem / lim) * 100 if lim else None,
                        resets_at=openrouter_reset(k.get("limit_reset")), limit=lim, remaining=rem, unit="USD"))
    w += budget_windows(acct, out)
    rl = k.get("rate_limit") or {}
    if rl.get("requests"):
        out["rate_limit"] = f"{rl['requests']} requests / {rl.get('interval', '')}"
    out["windows"] = w
    return out


def collect_deepseek(acct: dict, con=None) -> dict:
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    d = _get("https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {key}"})
    infos = d.get("balance_infos") or []
    usd = next((i for i in infos if i.get("currency") == "USD"), infos[0] if infos else {})
    return {
        "balance": float(usd.get("total_balance") or 0),
        "balance_currency": usd.get("currency", "USD"),
        "available": d.get("is_available"),
    }


def collect_manual(acct: dict, con=None) -> dict:
    """For providers with no usage API: numbers come from `aimeter log`."""
    return {"manual": True}


# --------------------------------------------------------------------------
# Subscription plan limits (Claude Pro/Max, ChatGPT/Codex).
# These use the same private endpoints the official CLIs use for /usage and
# /status. They are not public APIs and may change. AI Meter gets its own login
# through the dashboard's Connect button (OAuth with PKCE), so it never
# shares, or logs out, the tokens of your laptop's CLI.
# --------------------------------------------------------------------------

class NotConnected(RuntimeError):
    pass


OAUTH = {
    "claude_sub": {
        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        # Values from the Claude Code CLI 2.1.325 (checked 2026-11-19): it moved the claude.ai
        # authorize page to claude.com/cai; the old claude.ai URL now answers "Invalid request format".
        "authorize": "https://claude.com/cai/oauth/authorize",
        "token_urls": ["https://platform.claude.com/v1/oauth/token", "https://console.anthropic.com/v1/oauth/token"],
        "redirect": "https://platform.claude.com/oauth/code/callback",
        "scope": "org:create_api_key user:profile user:inference user:sessions:claude_code user:mcp_servers "
                 "user:file_upload user:plugins user:privacy_consents:write",
        "extra": {"code": "true"},  # nosec B105: OAuth parameter, not a password
        "json_body": True,
        "hint": "After you approve, the page shows a code. Copy all of it and paste it here.",
    },
    "codex_sub": {
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "authorize": "https://auth.openai.com/oauth/authorize",
        "token_urls": ["https://auth.openai.com/oauth/token"],
        "redirect": "http://localhost:1455/auth/callback",
        "scope": "openid profile email offline_access",
        "extra": {"id_token_add_organizations": "true", "codex_cli_simplified_flow": "true"},  # nosec B105: OAuth parameters, not passwords
        "json_body": False,
        "hint": "After you sign in, the browser opens a page that fails to load (localhost:1455). "
                "That is expected: copy the full address from the address bar and paste it here.",
    },
}


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def oauth_start(con, acct: dict) -> dict:
    import hashlib
    import secrets
    from urllib.parse import urlencode
    o = OAUTH[acct["provider"]]
    verifier = _b64url(secrets.token_bytes(32))
    state = _b64url(secrets.token_bytes(32))  # 43 chars, like Claude Code; a 22-char state is rejected as "Invalid request format"
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    kv_set(con, f"oauth_pending:{acct['id']}", {"verifier": verifier, "state": state})
    if acct["provider"] == "claude_sub":
        # Claude Code's order: code=true first, then the standard parameters.
        q = {**o["extra"], "client_id": o["client_id"], "response_type": "code", "redirect_uri": o["redirect"],
             "scope": o["scope"], "code_challenge": challenge, "code_challenge_method": "S256", "state": state}
    else:
        # ChatGPT: the order that is known to reach the localhost:1455 callback with a code.
        q = {"response_type": "code", "client_id": o["client_id"], "redirect_uri": o["redirect"], "scope": o["scope"],
             "code_challenge": challenge, "code_challenge_method": "S256", "state": state, **o["extra"]}
    return {"url": o["authorize"] + "?" + urlencode(q), "hint": o["hint"]}


def _token_post(o: dict, body: dict) -> dict:
    last = None
    for url in o["token_urls"]:
        try:
            if o["json_body"]:
                r = requests.post(url, json=body, timeout=HTTP_TIMEOUT, headers={"User-Agent": "aimeter/1.0"})
            else:
                r = requests.post(url, data=body, timeout=HTTP_TIMEOUT, headers={"User-Agent": "aimeter/1.0"})
            if r.ok:
                return r.json()
            last = f"HTTP {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            last = str(e)
    raise RuntimeError(f"token request failed ({last})")


def _jwt_claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def _save_tokens(con, acct, tok: dict, old: dict | None = None) -> dict:
    old = old or {}
    rec = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token") or old.get("refresh_token"),
        "expires_at": int(now_utc().timestamp()) + int(tok.get("expires_in") or 3600),
        "account_id": old.get("account_id"),
        "email": old.get("email"),
        "saved_at": iso(now_utc()),
    }
    for t in (tok.get("id_token"), tok.get("access_token")):
        c = _jwt_claims(t or "")
        auth = c.get("https://api.openai.com/auth") or {}
        rec["account_id"] = auth.get("chatgpt_account_id") or rec["account_id"]
        rec["plan"] = auth.get("chatgpt_plan_type") or rec.get("plan") or old.get("plan")
        rec["email"] = c.get("email") or rec["email"]
    if isinstance(tok.get("account"), dict):
        rec["email"] = tok["account"].get("email_address") or rec["email"]
    kv_set(con, f"oauth:{acct['id']}", rec)
    return rec


def oauth_finish(con, acct: dict, pasted: str) -> dict:
    from urllib.parse import parse_qs as _pq
    o = OAUTH[acct["provider"]]
    pend = kv_get(con, f"oauth_pending:{acct['id']}")
    if not pend:
        raise RuntimeError("No login in progress. Click Connect again.")
    pasted = pasted.strip()
    code, state = pasted, pend["state"]
    if pasted.startswith("http"):
        q = _pq(urlparse(pasted).query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [state])[0]
    elif "#" in pasted:
        code, state = pasted.split("#", 1)
    if not code:
        raise RuntimeError("Could not find a code in what you pasted.")
    if state != pend["state"]:
        raise RuntimeError("This code belongs to an older login attempt. Click Connect again.")
    body = {"grant_type": "authorization_code", "code": code, "client_id": o["client_id"],
            "redirect_uri": o["redirect"], "code_verifier": pend["verifier"]}
    if acct["provider"] == "claude_sub":
        body["state"] = state
    tok = _token_post(o, body)
    kv_set(con, f"oauth_pending:{acct['id']}", None)
    return _save_tokens(con, acct, tok)


CODEX_CALLBACK = ("127.0.0.1", 1455)  # where ChatGPT's sign-in sends the browser back (Codex's redirect)
CALLBACK_TIMEOUT = 600
_callback_lock = __import__("threading").Lock()
_callback = {"server": None}


def _callback_page(title: str, text: str, ok: bool) -> bytes:
    import html
    color = "#2f8f6a" if ok else "#cf4436"
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>AI Meter</title><body style='font:16px/1.5 system-ui,sans-serif;max-width:520px;margin:12vh auto;"
            f"padding:0 20px;color:#1c2321'><h1 style='font-size:24px;color:{color}'>{html.escape(title)}</h1>"
            f"<p>{html.escape(text)}</p></body>").encode()


def stop_codex_callback(wait: bool = False) -> None:
    """Stop listening on localhost:1455. wait=True blocks until the port is released; the listener's
    own request handler must not wait (it would wait for itself)."""
    with _callback_lock:
        srv, _callback["server"] = _callback["server"], None
    if not srv:
        return
    if wait:
        srv.shutdown()
        srv.server_close()
    else:
        import threading
        threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


def start_codex_callback(cfg, acct) -> bool:
    """Listen once on localhost:1455 for ChatGPT's redirect and finish the sign-in, so nothing has to be
    pasted. Only works when the browser runs on the same computer as AI Meter. Returns False when the port
    is taken (the Codex app may be signing in); the paste box still works then."""
    import threading
    from http.server import HTTPServer
    stop_codex_callback(wait=True)
    aid = acct["id"]

    class CB(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            log.debug(fmt, *a)

        def _reply(self, code, page):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(page)

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0].lower()
            u = urlparse(self.path)
            if host not in ("localhost", "127.0.0.1") or u.path != "/auth/callback":
                return self._reply(404, _callback_page("Not found", "This address only finishes an AI Meter sign-in.", False))
            q = parse_qs(u.query)
            if q.get("error"):
                stop_codex_callback()
                return self._reply(400, _callback_page("Sign-in was not completed",
                                                       redact(q.get("error_description", q["error"])[0])[:200], False))
            con = db_connect(cfg)
            try:
                pend = kv_get(con, f"oauth_pending:{aid}")
                if not pend or (q.get("state") or [""])[0] != pend["state"]:
                    # Not ours (or an old tab): keep listening for the real one.
                    return self._reply(400, _callback_page("This sign-in link is out of date",
                                                           "Start the sign-in again from AI Meter.", False))
                oauth_finish(con, acct, f"http://localhost:{CODEX_CALLBACK[1]}{self.path}")
                stop_codex_callback()
                self._reply(200, _callback_page("Signed in to AI Meter",
                                                "You can close this tab and go back to AI Meter.", True))
            except Exception as e:  # noqa: BLE001 - shown to the user, with secrets removed
                stop_codex_callback()
                return self._reply(400, _callback_page("Sign-in failed", redact(str(e))[:300], False))
            finally:
                con.close()
            try:
                cmd_collect(cfg, argparse.Namespace(only=[aid], no_alerts=True, strict=False, force=True))
            except Exception as e:  # noqa: BLE001
                log.error("%s: first check after sign-in failed: %s", aid, redact(str(e)))

    try:
        srv = HTTPServer(CODEX_CALLBACK, CB)
    except OSError:
        return False
    with _callback_lock:
        _callback["server"] = srv
    threading.Thread(target=srv.serve_forever, daemon=True, name="aimeter-oauth-callback").start()
    t = threading.Timer(CALLBACK_TIMEOUT, lambda: _callback["server"] is srv and stop_codex_callback())
    t.daemon = True
    t.start()
    return True


def oauth_token(con, acct: dict) -> dict:
    rec = kv_get(con, f"oauth:{acct['id']}") if con is not None else None
    if not rec:
        tok = env(acct.get("access_token_env"))  # manual fallback: paste an access token
        if tok:
            return {"access_token": tok, "account_id": env(acct.get("account_id_env"))}
        raise NotConnected("Not connected yet. Click Connect in this widget.")
    if rec.get("expires_at", 0) - 300 < now_utc().timestamp() and rec.get("refresh_token"):
        o = OAUTH[acct["provider"]]
        body = {"grant_type": "refresh_token", "refresh_token": rec["refresh_token"], "client_id": o["client_id"]}
        if acct["provider"] == "codex_sub":
            body["scope"] = "openid profile email"
        try:
            rec = _save_tokens(con, acct, _token_post(o, body), rec)
        except RuntimeError as e:
            raise NotConnected(f"Login expired and refresh failed, click Reconnect. ({e})")
    return rec


def _window_label(seconds, fallback):
    s = int(seconds or 0)
    if s == 18000:
        return "5-hour session"
    if s == 604800:
        return "Weekly"
    if s == 86400:
        return "Daily"
    if s:
        return f"{round(s / 3600)}h window" if s < 172800 else f"{round(s / 86400)}-day window"
    return fallback


CLAUDE_WINDOWS = {
    "five_hour": ("5-hour session", 18000), "seven_day": ("Weekly (all models)", 604800),
    "seven_day_opus": ("Weekly Opus", 604800), "seven_day_sonnet": ("Weekly Sonnet", 604800),
    "seven_day_oauth_apps": ("Weekly (apps)", 604800),
}


def collect_claude_sub(acct: dict, con=None) -> dict:
    tok = oauth_token(con, acct)
    r = requests.get("https://api.anthropic.com/api/oauth/usage", timeout=HTTP_TIMEOUT, headers={
        "Authorization": f"Bearer {tok['access_token']}", "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json", "User-Agent": "aimeter/1.0"})
    if r.status_code in (401, 403):
        raise NotConnected(f"Claude rejected the login (HTTP {r.status_code}), click Reconnect.")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    wins = []
    for k, v in d.items():
        if isinstance(v, dict) and "utilization" in v and v.get("utilization") is not None:
            label, secs = CLAUDE_WINDOWS.get(k, (k.replace("_", " ").capitalize(), None))
            wins.append(window(k, label, v["utilization"], resets_at=v.get("resets_at"), window_seconds=secs))
    ex = d.get("extra_usage") or {}
    if ex.get("is_enabled") and ex.get("monthly_limit"):
        used, lim = float(ex.get("used_credits") or 0), float(ex["monthly_limit"])
        wins.append(window("extra", "Extra usage", ex.get("utilization", used / lim * 100 if lim else 0),
                           resets_at=next_month_start(now_utc()), limit=lim / 100, remaining=(lim - used) / 100,
                           unit="USD"))
    order = list(CLAUDE_WINDOWS)
    wins.sort(key=lambda w: order.index(w["key"]) if w["key"] in order else 99)
    return {"windows": wins, "account": tok.get("email"), "plan": acct.get("plan")}


def _codex_windows(rl: dict, prefix: str, name: str | None = None) -> list:
    out = []
    for slot in ("primary_window", "secondary_window"):
        w = (rl or {}).get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        secs = w.get("limit_window_seconds")
        reset = w.get("reset_at")
        if not reset and w.get("reset_after_seconds") is not None:
            reset = now_utc().timestamp() + float(w["reset_after_seconds"])
        label = _window_label(secs, slot.split("_")[0].capitalize())
        out.append(window(f"{prefix}_{slot}", f"{name}: {label}" if name else label, w["used_percent"],
                          resets_at=reset, window_seconds=secs))
    return out


def collect_codex_sub(acct: dict, con=None) -> dict:
    tok = oauth_token(con, acct)
    h = {"Authorization": f"Bearer {tok['access_token']}", "User-Agent": "codex-cli", "Accept": "application/json"}
    if tok.get("account_id"):
        h["ChatGPT-Account-Id"] = tok["account_id"]
    r = requests.get("https://chatgpt.com/backend-api/wham/usage", headers=h, timeout=HTTP_TIMEOUT)
    if r.status_code in (401, 403):
        raise NotConnected(f"ChatGPT rejected the login (HTTP {r.status_code}), click Reconnect.")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    wins = _codex_windows(d.get("rate_limit"), "codex")
    for extra in d.get("additional_rate_limits") or []:
        name = extra.get("limit_name") or extra.get("metered_feature") or "Model"
        wins += _codex_windows(extra.get("rate_limit"), re.sub(r"\W+", "_", name.lower()), name)
    if d.get("code_review_rate_limit"):
        wins += _codex_windows(d["code_review_rate_limit"], "review", "Code review")
    out = {"windows": wins, "plan": d.get("plan_type") or tok.get("plan"), "account": tok.get("email")}
    cr = d.get("credits") or {}
    if cr.get("has_credits") and cr.get("balance") is not None:
        out["credits"] = cr.get("balance")
    return out


# --------------------------------------------------------------------------
# API rate limits (tokens/requests per minute) read from response headers.
# Each check sends one tiny request (1 output token), a fraction of a cent.
# --------------------------------------------------------------------------

def _dur_seconds(s: str | None) -> float | None:
    """OpenAI reset values look like '1s', '6m0s', '20ms', '1h2m3.5s'."""
    if not s:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", s):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def collect_anthropic_limits(acct: dict, con=None) -> dict:
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    r = requests.post(f"{acct.get('base_url', 'https://api.anthropic.com')}/v1/messages", timeout=HTTP_TIMEOUT,
                      headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                      json={"model": acct.get("probe_model", "claude-haiku-5-5"), "max_tokens": 1,
                            "messages": [{"role": "user", "content": "1"}]})
    h = r.headers
    if r.status_code >= 400 and r.status_code != 429 and not h.get("anthropic-ratelimit-requests-limit"):
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    wins = []
    for k, label, unit in (("requests", "Requests / min", "requests"), ("input-tokens", "Input tokens / min", "tokens"),
                           ("output-tokens", "Output tokens / min", "tokens"), ("tokens", "Tokens / min", "tokens")):
        lim, rem = _int(h.get(f"anthropic-ratelimit-{k}-limit")), _int(h.get(f"anthropic-ratelimit-{k}-remaining"))
        if lim:
            wins.append(window(k, label, (1 - (rem or 0) / lim) * 100, resets_at=h.get(f"anthropic-ratelimit-{k}-reset"),
                               window_seconds=60, limit=lim, remaining=rem, unit=unit))
    if any(w["key"] in ("input-tokens", "output-tokens") for w in wins):
        wins = [w for w in wins if w["key"] != "tokens"]
    return {"windows": wins, "model": acct.get("probe_model", "claude-haiku-5-5"),
            "throttled": r.status_code == 429}


def collect_openai_limits(acct: dict, con=None) -> dict:
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    model = acct.get("probe_model")
    if not model:
        raise RuntimeError("set probe_model in config.yaml (any cheap chat model your key can use)")
    r = requests.post(f"{acct.get('base_url', 'https://api.openai.com')}/v1/chat/completions", timeout=HTTP_TIMEOUT,
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "max_completion_tokens": 1, "messages": [{"role": "user", "content": "1"}]})
    h = r.headers
    if r.status_code >= 400 and r.status_code != 429 and not h.get("x-ratelimit-limit-requests"):
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    wins = []
    for k, label, unit in (("requests", "Requests / min", "requests"), ("tokens", "Tokens / min", "tokens")):
        lim, rem = _int(h.get(f"x-ratelimit-limit-{k}")), _int(h.get(f"x-ratelimit-remaining-{k}"))
        if lim:
            secs = _dur_seconds(h.get(f"x-ratelimit-reset-{k}"))
            reset = now_utc() + dt.timedelta(seconds=secs) if secs is not None else None
            wins.append(window(k, label, (1 - (rem or 0) / lim) * 100, resets_at=reset, window_seconds=60,
                               limit=lim, remaining=rem, unit=unit))
    return {"windows": wins, "model": model, "throttled": r.status_code == 429}


def collect_hermes(acct: dict, con=None) -> dict:
    """Token counts and cost per model from Hermes Agent's own session database.
    Covers every provider Hermes routes to (Nous, DeepSeek, Kimi, OpenCode Go, ...)."""
    path = acct.get("state_db", "/hermes/state.db")
    if not Path(path).exists():
        raise RuntimeError(f"Hermes database not found at {path} (is the Hermes volume mounted?)")
    h = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    h.row_factory = sqlite3.Row
    cols = {r[1] for r in h.execute("PRAGMA table_info(sessions)")}
    def c(name, default="0"):
        return f"COALESCE({name},0)" if name in cols else default
    cost = ("COALESCE(actual_cost_usd, estimated_cost_usd, 0)" if "actual_cost_usd" in cols
            else c("estimated_cost_usd"))
    prov = "billing_provider" if "billing_provider" in cols else "NULL"
    since = (month_start(now_utc()) - dt.timedelta(days=31)).timestamp()
    rows = h.execute(f"""
        SELECT date(started_at, 'unixepoch') AS day, COALESCE(model, 'unknown') AS model, {prov} AS provider,
               SUM({c('input_tokens')}) AS i, SUM({c('output_tokens')}) AS o,
               SUM({c('cache_read_tokens')} + {c('cache_write_tokens')}) AS ca,
               SUM({c('reasoning_tokens')}) AS r, SUM({cost}) AS cost,
               SUM({c('api_call_count', c('message_count'))}) AS req, COUNT(*) AS sessions
        FROM sessions WHERE started_at >= ? GROUP BY day, model, provider""", (since,)).fetchall()  # nosec B608: only fixed column names are interpolated; values are bound
    hourly = h.execute(f"""
        SELECT strftime('%Y-%m-%dT%H', started_at, 'unixepoch') AS hr, COALESCE({prov}, '') AS provider,
               COALESCE(model, '') AS model,
               SUM({c('input_tokens')} + {c('output_tokens')} + {c('cache_read_tokens')} + {c('cache_write_tokens')}) AS tok,
               SUM({cost}) AS cost
        FROM sessions WHERE started_at >= ? GROUP BY hr, provider, model""",  # nosec B608: only fixed column names are interpolated; values are bound
        ((now_utc() - dt.timedelta(days=32)).timestamp(),)).fetchall()
    h.close()
    agg = TokenAgg()
    providers = {}
    for r in rows:
        label = f"{r['model']} ({r['provider']})" if r["provider"] else r["model"]
        agg.add(r["day"], label, input=r["i"], output=r["o"], cache=r["ca"], reasoning=r["r"],
                cost=r["cost"], requests=r["req"])
        if r["day"].startswith(agg.month):
            p = providers.setdefault(r["provider"] or "unknown", {"tokens": 0, "cost": 0.0})
            p["tokens"] += (r["i"] or 0) + (r["o"] or 0) + (r["ca"] or 0)
            p["cost"] += r["cost"] or 0
    tu = agg.result("Hermes sessions")
    tu["by_provider"] = [{"provider": k, "tokens": int(v["tokens"]), "cost": round(v["cost"], 4)}
                         for k, v in sorted(providers.items(), key=lambda x: -x[1]["tokens"])]
    t = tu["totals"]
    return {"token_usage": tu, "spend_mtd": t["cost"], "spend_today": tu["today"]["cost"],
            "hourly": [[r["hr"], r["provider"], r["model"], int(r["tok"] or 0), round(r["cost"] or 0, 5)] for r in hourly],
            "windows": budget_windows(acct, {"spend_mtd": t["cost"], "spend_today": tu["today"]["cost"]})}


# --------------------------------------------------------------------------
# Coding tools on this computer: token counts from the session logs that
# Claude Code and Codex write locally. Read-only. These log formats are not a
# public API; checked against Claude Code 2.1.325 and Codex 0.167 (2026-11-19).
#
# Claude Code (~/.claude/projects/**/*.jsonl): one line per content block, so a
#   reply's message.id repeats with identical usage; count each id once.
#   "cost-state" lines hold a running per-session total with costUSD per model,
#   including side calls (for example Haiku) that have no reply line of their own.
# Codex (~/.codex/sessions/**/*.jsonl[.zst]): "token_usage_record" per reply with
#   a response_id; input_tokens includes cached_input_tokens. The model comes from
#   the "turn_context" line of the same turn_id. Older logs only have
#   "event_msg/token_count" with a running total_token_usage.
# --------------------------------------------------------------------------

_LOG_CACHE: dict = {}  # path -> (size, mtime, parsed); parsing is skipped for unchanged files


def _read_log_lines(path: Path):
    """Yield parsed JSON objects from a .jsonl or .jsonl.zst file; bad lines are skipped."""
    if path.name.endswith((".zst", ".zstd")):
        try:
            import zstandard  # optional dependency
            with open(path, "rb") as fh:
                data = zstandard.ZstdDecompressor().stream_reader(fh).read()
        except ImportError:
            try:
                from compression import zstd  # Python 3.14+
                data = zstd.decompress(path.read_bytes())
            except ImportError:
                raise _NeedsZstd()
        lines = data.decode("utf-8", errors="replace").splitlines()
    else:
        lines = path.open(encoding="utf-8", errors="replace")
    try:
        for line in lines:
            line = line.strip()
            if not line or line[0] != "{":
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue
    finally:
        if hasattr(lines, "close"):
            lines.close()


class _NeedsZstd(Exception):
    pass


def _hour(ts) -> str | None:
    """'2026-11-19T03:14:55.479Z' or epoch seconds/ms -> '2026-11-19T03' (UTC)."""
    try:
        if isinstance(ts, (int, float)):
            t = dt.datetime.fromtimestamp(ts / 1000 if ts > 1e12 else ts, UTC)
        else:
            t = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC)
        return t.strftime("%Y-%m-%dT%H")
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _model_provider(model: str, default: str) -> str:
    """Which provider a model name belongs to, for tools that can point at other providers."""
    m = (model or "").lower()
    for needle, prov in (("claude", "anthropic"), ("deepseek", "deepseek"), ("kimi", "moonshot"),
                         ("moonshot", "moonshot"), ("gemini", "google"), ("glm", "zhipu"), ("qwen", "qwen"),
                         ("grok", "xai"), ("mistral", "mistral"), ("hermes", "nous"),
                         ("gpt", "codex"), ("codex", "codex")):
        if needle in m:
            return default if default != "anthropic" and prov == "codex" else prov
    return default


def _parse_claude_file(path: Path) -> dict:
    msgs, costs = {}, {}
    for d in _read_log_lines(path):
        t = d.get("type")
        if t == "cost-state":
            sid = d.get("sessionId")
            if sid and (d.get("totalCostUSD") or 0) >= (costs.get(sid, {}).get("totalCostUSD") or 0):
                costs[sid] = {"totalCostUSD": d.get("totalCostUSD") or 0, "startTime": d.get("startTime"),
                              "modelUsage": d.get("modelUsage") or {}}
            continue
        m = d.get("message")
        if t != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
            continue
        model = m.get("model") or "unknown"
        if model.startswith("<"):  # "<synthetic>": local placeholder, not an API call
            continue
        mid = m.get("id") or d.get("requestId") or d.get("uuid")
        hr = _hour(d.get("timestamp"))
        if not mid or not hr:
            continue
        u = m["usage"]
        msgs[mid] = (d.get("sessionId") or "", hr, model, int(u.get("input_tokens") or 0),
                     int(u.get("output_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
                     int(u.get("cache_creation_input_tokens") or 0),
                     int((u.get("output_tokens_details") or {}).get("thinking_tokens") or 0))
    return {"msgs": msgs, "costs": costs}


def _parse_qwen_file(path: Path) -> dict:
    """Qwen Code chat log (~/.qwen/projects/<project>/chats/*.jsonl). Field names follow the open-source
    readers ccusage and CodeBurn (not checked on a live install here): promptTokenCount includes the
    cached tokens; thoughts are billed as output. Each reply is keyed by sessionId + uuid."""
    rows = {}
    for d in _read_log_lines(path):
        u = d.get("usageMetadata")
        if d.get("type") != "assistant" or not isinstance(u, dict):
            continue
        hr = _hour(d.get("timestamp"))
        if not hr:
            continue
        prompt = max(0, int(u.get("promptTokenCount") or 0))
        cached = min(max(0, int(u.get("cachedContentTokenCount") or 0)), prompt)
        thoughts = max(0, int(u.get("thoughtsTokenCount") or 0))
        key = f"{d.get('sessionId')}:{d.get('uuid')}" if d.get("uuid") else f"{path.name}:{len(rows)}"
        rows[key] = (hr, d.get("model") or "qwen-auto", prompt - cached,
                     max(0, int(u.get("candidatesTokenCount") or 0)) + thoughts, cached, thoughts)
    return {"rows": rows}


def _codex_limits_from(rl: dict, ts: str) -> dict | None:
    """ChatGPT plan limits as Codex logs them in each token_count event."""
    if not isinstance(rl, dict):
        return None
    try:
        ts = iso(dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
    except ValueError:
        return None
    out = {"ts": ts, "plan": rl.get("plan_type"), "credits": rl.get("credits"), "rate_limit": {}}
    for src, dst in (("primary", "primary_window"), ("secondary", "secondary_window")):
        w = rl.get(src)
        if isinstance(w, dict) and w.get("used_percent") is not None:
            out["rate_limit"][dst] = {"used_percent": w["used_percent"],
                                      "limit_window_seconds": int(w["window_minutes"]) * 60 if w.get("window_minutes") else None,
                                      "reset_at": w.get("resets_at")}
    return out if out["rate_limit"] else None


def _parse_codex_file(path: Path) -> dict:
    provider, turn_model, cur_model = "openai", {}, None
    records, counts, prev_total, limits = {}, [], None, None
    for d in _read_log_lines(path):
        t, p = d.get("type"), d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if t == "session_meta":
            provider = p.get("model_provider") or provider
        elif t == "turn_context":
            cur_model = p.get("model") or cur_model
            if p.get("turn_id"):
                turn_model[p["turn_id"]] = cur_model
        elif t == "token_usage_record":
            u, rid, hr = p.get("usage") or {}, p.get("response_id"), _hour(d.get("timestamp"))
            if rid and hr and u:
                records[rid] = (hr, turn_model.get(p.get("turn_id")) or cur_model or "unknown", u)
        elif t == "event_msg" and p.get("type") == "token_count":
            lim = _codex_limits_from(p.get("rate_limits"), d.get("timestamp") or "")
            if lim and (p["rate_limits"].get("limit_id") in (None, "codex")) and (not limits or lim["ts"] >= limits["ts"]):
                limits = lim
            info = p.get("info") or {}
            tot, hr = info.get("total_token_usage"), _hour(d.get("timestamp"))
            if isinstance(tot, dict) and hr:
                if prev_total is None or tot.get("total_tokens", 0) > prev_total.get("total_tokens", 0):
                    base = prev_total or {}
                    counts.append((hr, cur_model or "unknown", {k: (tot.get(k) or 0) - (base.get(k) or 0) for k in tot}))
                    prev_total = tot
    rows = list(records.values()) if records else counts  # records are per reply; counts only for older logs
    out = []
    for hr, model, u in rows:
        cached = int(u.get("cached_input_tokens") or 0)
        out.append((hr, model, max(0, int(u.get("input_tokens") or 0) - cached), int(u.get("output_tokens") or 0),
                    cached + int(u.get("cache_write_input_tokens") or 0), int(u.get("reasoning_output_tokens") or 0)))
    return {"provider": "codex" if provider == "openai" else provider, "rows": out, "limits": limits}


def _scan_logs(root: Path, patterns, parser, stats: dict) -> list:
    out = []
    if not root.is_dir():
        return out
    for pat in patterns:
        for f in root.rglob(pat):
            try:
                st = f.stat()
            except OSError:
                continue
            key = (parser.__name__, str(f))  # the same file can be read by both parsers
            hit = _LOG_CACHE.get(key)
            if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
                out.append(hit[2])
                stats["files"] += 1
                continue
            try:
                parsed = parser(f)
            except _NeedsZstd:
                stats["skipped_zst"] += 1
                continue
            except OSError:
                stats["unreadable"] += 1
                continue
            _LOG_CACHE[key] = (st.st_size, st.st_mtime, parsed)
            out.append(parsed)
            stats["files"] += 1
    return out


def _default_claude_dir() -> str:
    return str(Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "projects")


def _default_codex_dir() -> str:
    return str(Path(os.environ.get("CODEX_HOME") or "~/.codex").expanduser() / "sessions")


def _default_qwen_dir() -> str:
    first = (os.environ.get("QWEN_DATA_DIR") or "").split(",")[0].strip()
    return str(Path(first).expanduser()) if first else str(Path("~/.qwen/projects").expanduser())


def _parse_dsh_file(path: Path) -> dict:
    """DeepSeek Harness session (~/.dsh/sessions/**/session.v4.jsonl.zstd), checked on a live install
    (2026-11-19): "assistant/message" events carry data.usage {inputTokens, outputTokens, cacheReadTokens,
    cacheWriteTokens}; inputTokens excludes the cache (totalTokens = all four). One event per message id."""
    rows = {}
    for d in _read_log_lines(path):
        if d.get("type") != "assistant/message":
            continue
        data = d.get("data") or {}
        u, msg = data.get("usage"), data.get("message") or {}
        hr = _hour(d.get("time"))
        if not isinstance(u, dict) or not hr:
            continue
        src = msg.get("source") or {}
        key = msg.get("id") or f"{path.name}:{d.get('seq')}"
        rows[key] = (hr, src.get("model") or "deepseek", src.get("provider") or "deepseek",
                     int(u.get("inputTokens") or 0), int(u.get("outputTokens") or 0),
                     int(u.get("cacheReadTokens") or 0) + int(u.get("cacheWriteTokens") or 0), 0)
    return {"rows": rows}


def _default_dsh_dir() -> str:
    return str(Path("~/.dsh/sessions").expanduser())


def _default_continue_db() -> str:
    return str(Path(os.environ.get("CONTINUE_GLOBAL_DIR") or "~/.continue").expanduser() / "dev_data" / "devdata.sqlite")


def _read_continue(db: Path, stats: dict) -> list:
    """Continue's own token records (devdata.sqlite, table tokens_generated: model, provider,
    tokens_prompt, tokens_generated, timestamp in UTC). Checked on a live install, 2026-11-19."""
    if not db.is_file():
        return []
    try:
        st = db.stat()
    except OSError:
        return []
    key = ("continue", str(db))
    hit = _LOG_CACHE.get(key)
    if hit and hit[0] == st.st_size and hit[1] == st.st_mtime:
        stats["files"] += 1
        return hit[2]
    try:
        c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        rows = c.execute("SELECT model, provider, tokens_prompt, tokens_generated, timestamp FROM tokens_generated "
                         "WHERE timestamp >= ?", ((now_utc() - dt.timedelta(days=40)).strftime("%Y-%m-%d"),)).fetchall()
        c.close()
    except sqlite3.Error:
        stats["unreadable"] += 1
        return []
    out = []
    for model, provider, prompt, gen, ts in rows:
        hr = _hour(str(ts).replace(" ", "T") + ("Z" if "Z" not in str(ts) and "+" not in str(ts) else ""))
        if hr:
            out.append((hr, model or "unknown", provider or "", int(prompt or 0), int(gen or 0), 0, 0))
    _LOG_CACHE[key] = (st.st_size, st.st_mtime, out)
    stats["files"] += 1
    return out


def _provider_from_hint(hint: str, model: str, fallback: str) -> str:
    """Provider for a tool that names its own provider (Continue, DeepSeek Harness)."""
    h = (hint or "").lower()
    for needle, prov in (("anthropic", "anthropic"), ("deepseek", "deepseek"), ("openrouter", "openrouter"),
                         ("moonshot", "moonshot"), ("kimi", "moonshot"), ("gemini", "google"), ("vertex", "google"),
                         ("mistral", "mistral"), ("ollama", "ollama"), ("lmstudio", "lmstudio"), ("lm-studio", "lmstudio"),
                         ("openai", "openai"), ("azure", "openai"), ("qwen", "qwen"), ("dashscope", "qwen")):
        if needle in h:
            return prov
    return _model_provider(model, h or fallback)


# Coding tools read from this computer: key -> name shown on the dashboard.
TOOLS = {"claude_code": "Claude Code", "codex": "Codex", "qwen_code": "Qwen Code",
         "deepseek_harness": "DeepSeek Harness", "continue": "Continue"}


def collect_local_logs(acct: dict, con=None) -> dict:
    """Tokens (and Claude Code's estimated cost) from coding tools' local records, plus the ChatGPT plan
    limits that Codex writes into its logs. Every row is tagged with the tool it came from."""
    dirs = {"claude_code": Path(acct.get("claude_dir") or _default_claude_dir()).expanduser(),
            "codex": Path(acct.get("codex_dir") or _default_codex_dir()).expanduser(),
            "qwen_code": Path(acct.get("qwen_dir") or _default_qwen_dir()).expanduser(),
            "deepseek_harness": Path(acct.get("dsh_dir") or _default_dsh_dir()).expanduser()}
    continue_db = Path(acct.get("continue_db") or _default_continue_db()).expanduser()
    if not any(d.is_dir() for d in dirs.values()) and not continue_db.is_file():
        raise RuntimeError("No coding tool records found (Claude Code, Codex, Qwen Code, DeepSeek Harness or "
                           "Continue). This reads files on the computer AI Meter runs on.")
    stats = {k: {"files": 0, "skipped_zst": 0, "unreadable": 0} for k in TOOLS}
    cl = _scan_logs(dirs["claude_code"], ["*.jsonl"], _parse_claude_file, stats["claude_code"])
    cx = _scan_logs(dirs["codex"], ["*.jsonl", "*.jsonl.zst"], _parse_codex_file, stats["codex"])
    qw = _scan_logs(dirs["qwen_code"], ["*.jsonl"], _parse_qwen_file, stats["qwen_code"])
    ds = _scan_logs(dirs["deepseek_harness"], ["session*.jsonl", "session*.jsonl.zstd"], _parse_dsh_file,
                    stats["deepseek_harness"])
    co = _read_continue(continue_db, stats["continue"])

    # (hour, provider, model, tool) -> [input, output, cache, reasoning, cost or None, requests]
    buckets: dict = {}

    def put(hr, prov, model, tool, i=0, o=0, c=0, r=0, cost=None, req=0):
        b = buckets.setdefault((hr, prov, model, tool), [0, 0, 0, 0, None, 0])
        b[0] += i
        b[1] += o
        b[2] += c
        b[3] += r
        if cost is not None:
            b[4] = (b[4] or 0) + cost
        b[5] += req

    # Claude Code: unique replies, then reconcile each session with its cost-state.
    msgs, costs = {}, {}
    for p in cl:
        msgs.update(p["msgs"])
        for sid, c in p["costs"].items():
            if c["totalCostUSD"] >= costs.get(sid, {}).get("totalCostUSD", -1):
                costs[sid] = c
    by_sess: dict = {}
    for sid, hr, model, i, o, cr, cw, th in msgs.values():
        s_ = by_sess.setdefault(sid, {})
        per = s_.setdefault(model, {})
        v = per.setdefault(hr, [0, 0, 0, 0, 0, 0])
        for k, x in enumerate((i, o, cr, cw, th, 1)):
            v[k] += x
    for sid in set(by_sess) | set(costs):
        models = by_sess.get(sid, {})
        mu = (costs.get(sid) or {}).get("modelUsage") or {}
        hours = [h for per in models.values() for h in per]
        last = max(hours) if hours else _hour((costs.get(sid) or {}).get("startTime"))
        if not last:
            continue
        for model in set(models) | set(mu):
            per = models.get(model) or {}
            sums = [sum(v[k] for v in per.values()) for k in range(6)]
            c = mu.get(model) or {}
            final = [max(sums[0], int(c.get("inputTokens") or 0)), max(sums[1], int(c.get("outputTokens") or 0)),
                     max(sums[2], int(c.get("cacheReadInputTokens") or 0)),
                     max(sums[3], int(c.get("cacheCreationInputTokens") or 0)),
                     max(sums[4], int(c.get("thinkingTokens") or 0))]
            cost = c.get("costUSD") if mu else None
            weight_total = sum(v[0] + v[1] + v[2] + v[3] for v in per.values())
            shares = ({h: (v[0] + v[1] + v[2] + v[3]) / weight_total for h, v in per.items()} if weight_total
                      else {last: 1.0})
            prov = _model_provider(model, "anthropic")
            for h, share in shares.items():
                put(h, prov, model, "claude_code", i=final[0] * share, o=final[1] * share,
                    c=(final[2] + final[3]) * share, r=final[4] * share,
                    cost=None if cost is None else cost * share, req=per[h][5] if h in per else 0)

    codex_limits = None
    for p in cx:
        for hr, model, i, o, c, r in p["rows"]:
            put(hr, _model_provider(model, p["provider"]), model, "codex", i=i, o=o, c=c, r=r, req=1)
        lim = p.get("limits")
        if lim and (not codex_limits or lim["ts"] > codex_limits["ts"]):
            codex_limits = lim

    qrows = {}
    for p in qw:
        qrows.update(p["rows"])  # same session + uuid in two files counts once
    for hr, model, i, o, c, r in qrows.values():
        put(hr, _model_provider(model, "qwen"), model, "qwen_code", i=i, o=o, c=c, r=r, req=1)

    drows = {}
    for p in ds:
        drows.update(p["rows"])
    for hr, model, hint, i, o, c, r in drows.values():
        put(hr, _provider_from_hint(hint, model, "deepseek"), model, "deepseek_harness", i=i, o=o, c=c, r=r, req=1)

    for hr, model, hint, i, o, c, r in co:
        put(hr, _provider_from_hint(hint, model, "unknown"), model, "continue", i=i, o=o, c=c, r=r, req=1)

    agg = TokenAgg()
    providers: dict = {}
    tools = {k: {"tool": k, "name": v, "tokens": 0, "today": 0, "cost": None, "requests": 0, "files": 0}
             for k, v in TOOLS.items()}
    hourly = []
    since = (now_utc() - dt.timedelta(days=32)).strftime("%Y-%m-%dT%H")
    for (hr, prov, model, tool), (i, o, c, r, cost, req) in sorted(buckets.items()):
        agg.add(hr[:10], f"{model} ({PRETTY.get(prov, prov)})", input=i, output=o, cache=c, reasoning=r,
                cost=cost, requests=req)
        if hr[:7] == agg.month:
            pv = providers.setdefault(prov, {"tokens": 0, "cost": 0.0})
            pv["tokens"] += i + o + c
            pv["cost"] += cost or 0
            t = tools[tool]
            t["tokens"] += i + o + c
            t["requests"] += req
            if hr[:10] == agg.today:
                t["today"] += i + o + c
            if cost is not None:
                t["cost"] = (t["cost"] or 0) + cost
        if hr >= since:
            hourly.append([hr, prov, model, int(i + o + c), None if cost is None else round(cost, 5)])
    found = {k: v["files"] for k, v in stats.items()}
    if not buckets and not any(found.values()):
        raise RuntimeError("No coding tool sessions found yet.")
    tu = agg.result("Coding tools on this computer")
    tu["by_provider"] = [{"provider": k, "tokens": int(v["tokens"]), "cost": round(v["cost"], 4)}
                         for k, v in sorted(providers.items(), key=lambda x: -x[1]["tokens"])]
    for t in tools.values():
        t["files"] = found[t["tool"]]
        t["tokens"], t["today"] = int(t["tokens"]), int(t["today"])
        t["cost"] = None if t["cost"] is None else round(t["cost"], 4)
    by_tool = sorted((t for t in tools.values() if t["files"] or t["tokens"]), key=lambda t: -t["tokens"])
    read = [f"{TOOLS[k]} ({n})" for k, n in found.items() if n]
    notes = [("Read " + ", ".join(read) + " on this computer.") if read else "No tool records found yet."]
    skipped = sum(v["skipped_zst"] for v in stats.values())
    if skipped:
        notes.append(f"Skipped {skipped} compressed files: install the zstandard package to include them.")
    notes.append("Cost is Claude Code's estimate at API prices, not what a plan charges. The other tools "
                 "record tokens only.")
    out = {"token_usage": tu, "hourly": hourly, "est_cost_mtd": tu["totals"]["cost"], "by_tool": by_tool,
           "logs": stats, "note": " ".join(notes)}
    if codex_limits:
        out["codex_limits"] = codex_limits
    return out


def codex_windows_from_logs(lim: dict, now: dt.datetime | None = None) -> list:
    """Turn the limits Codex logged into plan windows. A window whose reset time has passed since
    the log was written has refilled, and Codex has not run since (or a newer log would exist)."""
    now = now or now_utc()
    wins = _codex_windows(lim.get("rate_limit"), "codex")
    for w in wins:
        if w.get("resets_at") and dt.datetime.fromisoformat(w["resets_at"].replace("Z", "+00:00")) <= now:
            w["used_pct"], w["resets_at"] = 0.0, None
    return wins


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


KIMI_UNITS = {"TIME_UNIT_SECOND": 1, "TIME_UNIT_MINUTE": 60, "TIME_UNIT_HOUR": 3600, "TIME_UNIT_DAY": 86400}


def collect_kimi_code(acct: dict, con=None) -> dict:
    """Kimi Code plan: 5-hour window + plan quota. Uses a Kimi Code API key (console)."""
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    d = _get(acct.get("usage_url_api", "https://api.kimi.com/coding/v1/usages"),
             {"Authorization": f"Bearer {key}", "User-Agent": "aimeter/1.0"})
    wins = []
    for i, item in enumerate(d.get("limits") or []):
        w, det = item.get("window") or {}, item.get("detail") or {}
        secs = (_num(w.get("duration")) or 0) * KIMI_UNITS.get(w.get("timeUnit"), 60)
        lim, rem = _num(det.get("limit")), _num(det.get("remaining"))
        if lim:
            wins.append(window(f"limit{i}", _window_label(secs, "Window"), (lim - (rem or 0)) / lim * 100,
                               resets_at=det.get("resetTime"), window_seconds=int(secs) or None,
                               limit=lim, remaining=rem, unit="%" if lim == 100 else "units"))
    u = d.get("usage") or {}
    lim, rem = _num(u.get("limit")), _num(u.get("remaining"))
    if lim:
        wins.append(window("quota", acct.get("quota_label", "Plan quota"), (lim - (rem or 0)) / lim * 100,
                           resets_at=u.get("resetTime"), window_seconds=None,
                           limit=lim, remaining=rem, unit="%" if lim == 100 else "units"))
    for w in wins:  # values are percentages of the plan, so the "x of 100 left" line adds nothing
        if w["unit"] == "%":
            w["limit"] = w["remaining"] = None
    return {"windows": wins, "plan": acct.get("plan")}


def collect_kimi_api(acct: dict, con=None) -> dict:
    """Moonshot / Kimi Open Platform prepaid balance."""
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    base = acct.get("base_url", "https://api.moonshot.ai")
    d = _get(f"{base}/v1/users/me/balance", {"Authorization": f"Bearer {key}"}).get("data") or {}
    cur = acct.get("currency", "CNY" if ".cn" in base else "USD")
    out = {"balance": _num(d.get("available_balance")), "balance_currency": cur,
           "voucher": _num(d.get("voucher_balance")), "cash": _num(d.get("cash_balance"))}
    return out


def _pct(v):
    v = _num(v)
    if v is None:
        return None
    return v * 100 if 0 < v <= 1 and not float(v).is_integer() else v


def collect_opencode_go(acct: dict, con=None) -> dict:
    """OpenCode Go plan: rolling 5-hour, weekly and monthly dollar allowances."""
    key = env(acct.get("api_key_env"))
    if not key:
        raise RuntimeError(f"env var {acct.get('api_key_env')} is not set")
    d = _get("https://opencode.ai/zen/go/v1/usage", {"Authorization": f"Bearer {key}", "User-Agent": "aimeter/1.0"})
    u = d.get("usage") or d
    labels = [("rolling", "5-hour", 18000), ("weekly", "Weekly", 604800), ("monthly", "Monthly", 2592000)]
    caps = acct.get("caps_usd") or {}
    wins = []
    for k, label, secs in labels:
        w = u.get(k) or u.get(f"{k}Usage")
        if not isinstance(w, dict):
            continue
        pct = _pct(w.get("percent", w.get("usagePercent")))
        reset = w.get("resetsAt") or w.get("resetAt")
        if not reset and w.get("resetInSec") is not None:
            reset = now_utc().timestamp() + float(w["resetInSec"])
        if isinstance(reset, (int, float)) and reset > 1e12:  # milliseconds
            reset = reset / 1000
        cap = caps.get(k)
        wins.append(window(k, label, pct, resets_at=reset, window_seconds=secs,
                           limit=cap, remaining=None if cap is None or pct is None else round(cap * (1 - pct / 100), 2),
                           unit="USD" if cap else None))
    if not wins:
        raise RuntimeError(f"unexpected response: {json.dumps(d)[:200]}")
    return {"windows": wins, "plan": acct.get("plan", "Go")}


# Premium requests per month by plan, used only when GitHub does not report the allowance itself.
# From GitHub's Copilot plans page (2026); set monthly_allowance on the account if yours differs.
COPILOT_ALLOWANCE = {"free": 50, "pro": 300, "pro_plus": 1500, "pro+": 1500, "business": 300, "enterprise": 1000}


def _copilot_reset(value) -> str | None:
    if not value:
        return None
    v = str(value)
    return v + "T00:00:00Z" if len(v) == 10 else v


def collect_copilot(acct: dict, con=None) -> dict:
    """GitHub Copilot premium requests. First the endpoint Copilot's own editors use
    (api.github.com/copilot_internal/user, unofficial: quota_snapshots with entitlement, remaining,
    percent_remaining, unlimited, plus quota_reset_date and copilot_plan). If GitHub refuses it,
    the documented billing report (/users/{login}/settings/billing/premium_request/usage), which
    gives premium requests used this month; the allowance then comes from the plan."""
    tok = env(acct.get("api_key_env"))
    if not tok:
        raise RuntimeError("Add a GitHub token for Copilot.")
    ua = {"User-Agent": "aimeter/1.0", "X-GitHub-Api-Version": "2022-11-28"}
    why = None
    try:
        r = requests.get("https://api.github.com/copilot_internal/user", timeout=HTTP_TIMEOUT,
                         headers={**ua, "Authorization": f"token {tok}", "Accept": "application/json"})
        if r.status_code == 200 and isinstance(r.json().get("quota_snapshots"), dict):
            d = r.json()
            reset = _copilot_reset(d.get("quota_reset_date") or d.get("quota_reset_date_utc"))
            wins = []
            for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat"),
                               ("completions", "Code completions")):
                q = d["quota_snapshots"].get(key)
                if not isinstance(q, dict) or q.get("unlimited"):
                    continue
                ent, rem, pr = q.get("entitlement"), q.get("remaining"), q.get("percent_remaining")
                if pr is not None:
                    pct = 100 - float(pr)
                elif ent:
                    pct = (float(ent) - float(rem or 0)) / float(ent) * 100
                else:
                    continue
                if q.get("overage_count"):
                    pct = max(pct, 100.0)
                wins.append(window(f"copilot_{key}", label, pct, resets_at=reset, window_seconds=2592000,
                                   limit=ent, remaining=rem, unit="requests"))
            return {"windows": wins, "plan": d.get("copilot_plan") or acct.get("plan"), "account": d.get("login"),
                    "source": "copilot", "note": None if wins else "Copilot reports no limited quotas for this plan."}
        if r.status_code in (401,):
            raise NotConnected("GitHub rejected the token (HTTP 401). Create a new token and add it again.")
        why = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        why = str(e)[:120]
    # Documented route: premium requests used this calendar month.
    hb = {**ua, "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}
    u = requests.get("https://api.github.com/user", headers=hb, timeout=HTTP_TIMEOUT)
    if u.status_code == 401:
        raise NotConnected("GitHub rejected the token (HTTP 401). Create a new token and add it again.")
    if u.status_code >= 400:
        raise RuntimeError(f"HTTP {u.status_code} from GitHub: {u.text[:200]}")
    login = u.json().get("login")
    now = now_utc()
    b = requests.get(f"https://api.github.com/users/{login}/settings/billing/premium_request/usage",
                     params={"year": now.year, "month": now.month}, headers=hb, timeout=HTTP_TIMEOUT)
    if b.status_code in (403, 404):
        raise RuntimeError("GitHub did not share Copilot usage for this token. Use a classic token, or a "
                           "fine-grained token with the Plan permission set to read.")
    if b.status_code >= 400:
        raise RuntimeError(f"HTTP {b.status_code} from GitHub billing: {b.text[:200]}")
    items = b.json().get("usageItems") or []
    used = sum(float(i.get("grossQuantity") or 0) for i in items
               if "copilot" in str(i.get("product", "copilot")).lower())
    plan = (acct.get("plan") or "").lower().replace(" ", "_")
    allowance = acct.get("monthly_allowance") or COPILOT_ALLOWANCE.get(plan)
    pct = used / float(allowance) * 100 if allowance else None
    w = window("copilot_premium_interactions", "Premium requests", pct, resets_at=next_month_start(now),
               window_seconds=2592000, limit=allowance, remaining=None if not allowance else max(0.0, allowance - used),
               unit="requests")
    note = "From GitHub's billing report"
    note += f" (the Copilot quota endpoint answered {why})." if why else "."
    if not allowance:
        note += " Set your plan or monthly allowance to see the percentage."
    return {"windows": [w] if pct is not None else [], "premium_used": used, "plan": acct.get("plan"),
            "account": login, "source": "billing", "note": note}


def _local_url(value: str | None, default: str) -> str:
    url = (value or default).rstrip("/")
    if not re.match(r"^https?://", url):
        raise RuntimeError(f"{url} is not an http address.")
    return url


def collect_local_models(acct: dict, con=None) -> dict:
    """Models running on this computer: Ollama (/api/version, /api/tags, /api/ps; checked on Ollama 0.40)
    and LM Studio (/api/v0/models, falling back to the OpenAI-style /v1/models). Free, so no cost."""
    servers = []
    o_url = _local_url(acct.get("ollama_url"), "http://127.0.0.1:11434")
    try:
        ver = requests.get(f"{o_url}/api/version", timeout=3).json().get("version")
        tags = requests.get(f"{o_url}/api/tags", timeout=5).json().get("models") or []
        ps = requests.get(f"{o_url}/api/ps", timeout=5).json().get("models") or []
        servers.append({"name": "Ollama", "running": True, "version": ver, "installed": len(tags),
                        "loaded": [{"model": m.get("name") or m.get("model"),
                                    "size_gb": round((m.get("size") or 0) / 1e9, 1),
                                    "gpu_share": round((m.get("size_vram") or 0) / m["size"] * 100) if m.get("size") else None,
                                    "until": m.get("expires_at")} for m in ps]})
    except (requests.RequestException, ValueError, AttributeError):
        servers.append({"name": "Ollama", "running": False})
    l_url = _local_url(acct.get("lmstudio_url"), "http://127.0.0.1:1234")
    try:
        r = requests.get(f"{l_url}/api/v0/models", timeout=3)
        if r.status_code == 404:
            r = requests.get(f"{l_url}/v1/models", timeout=3)
        data = r.json().get("data") or []
        servers.append({"name": "LM Studio", "running": True, "installed": len(data),
                        "loaded": [{"model": m.get("id")} for m in data
                                   if m.get("state") == "loaded" or "state" not in m and "/v1/" in r.url]})
    except (requests.RequestException, ValueError, AttributeError):
        servers.append({"name": "LM Studio", "running": False})
    if not any(x["running"] for x in servers):
        raise RuntimeError("Neither Ollama nor LM Studio is running on this computer.")
    loaded = sum(len(x.get("loaded") or []) for x in servers)
    return {"local_servers": servers, "loaded_models": loaded,
            "note": "Local models are free: no cost or limits to track, just what is installed and running."}


def collect_nous(acct: dict, con=None) -> dict:
    """Nous Portal has no public usage API; read rate-limit headers from one tiny request."""
    acct = {"base_url": "https://inference-api.nousresearch.com", **acct}
    try:
        out = collect_openai_limits(acct, con)
    except RuntimeError as e:
        if "probe_model" in str(e):
            raise
        raise RuntimeError(f"Nous request failed: {e}")
    if not out["windows"]:
        out["note"] = "Nous did not report rate limits on this request. Credits are only visible in the Portal."
    return out


COLLECTORS = {
    "anthropic": collect_anthropic,
    "openai": collect_openai,
    "openrouter": collect_openrouter,
    "deepseek": collect_deepseek,
    "manual": collect_manual,
    "claude_sub": collect_claude_sub,
    "codex_sub": collect_codex_sub,
    "anthropic_limits": collect_anthropic_limits,
    "openai_limits": collect_openai_limits,
    "kimi_code": collect_kimi_code,
    "kimi_api": collect_kimi_api,
    "opencode_go": collect_opencode_go,
    "nous": collect_nous,
    "hermes": collect_hermes,
    "local_logs": collect_local_logs,
    "copilot": collect_copilot,
    "local_models": collect_local_models,
}

# How often each provider is polled by default (seconds); override with `interval:`.
DEFAULT_INTERVAL = {
    "anthropic": 1800, "openai": 1800, "openrouter": 300, "deepseek": 1800, "manual": 3600,
    "claude_sub": 300, "codex_sub": 300, "anthropic_limits": 600, "openai_limits": 600,
    "kimi_code": 300, "kimi_api": 1800, "opencode_go": 300, "nous": 900, "hermes": 300, "local_logs": 300, "copilot": 900, "local_models": 120,
}

PROVIDER_META = {
    "anthropic": ("Anthropic API", "spend"), "openai": ("OpenAI API", "spend"),
    "openrouter": ("OpenRouter", "spend"), "deepseek": ("DeepSeek", "spend"), "manual": ("Manual", "manual"),
    "claude_sub": ("Claude plan", "plan"), "codex_sub": ("ChatGPT / Codex plan", "plan"),
    "anthropic_limits": ("Anthropic rate limits", "ratelimit"), "openai_limits": ("OpenAI rate limits", "ratelimit"),
    "kimi_code": ("Kimi Code plan", "plan"), "kimi_api": ("Kimi API (Moonshot)", "spend"),
    "opencode_go": ("OpenCode Go plan", "plan"), "nous": ("Nous Portal", "ratelimit"), "hermes": ("Hermes Agent usage", "spend"),
    "local_logs": ("Coding tools on this computer", "spend"),
    "copilot": ("GitHub Copilot", "plan"),
    "local_models": ("Local models", "spend"),
}


# --------------------------------------------------------------------------
# Subscriptions
# --------------------------------------------------------------------------

CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannual": 6, "yearly": 12, "annual": 12}


def add_months(d: dt.date, n: int) -> dt.date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(y, m, min(day, 31))
        except ValueError:
            continue
    raise ValueError(d)


def next_renewal(sub: dict, today: dt.date | None = None) -> dt.date | None:
    r = sub.get("renews_on")
    if not r:
        return None
    d = r if isinstance(r, dt.date) else dt.date.fromisoformat(str(r))
    today = today or dt.date.today()
    cycle = str(sub.get("cycle", "monthly")).lower()
    if cycle == "weekly":
        while d < today:
            d += dt.timedelta(days=7)
        return d
    step = CYCLE_MONTHS.get(cycle)
    if not step:
        return d
    while d < today:
        d = add_months(d, step)
    return d


def monthly_cost_usd(sub: dict, fx: dict) -> float | None:
    cost = sub.get("cost")
    if cost is None:
        return None
    cur = str(sub.get("currency", "USD")).upper()
    rate = fx.get(cur)
    if rate is None:
        return None
    cycle = str(sub.get("cycle", "monthly")).lower()
    per_month = float(cost) * (52 / 12 if cycle == "weekly" else 1 / CYCLE_MONTHS.get(cycle, 1))
    return round(per_month * float(rate), 2)


def usage_period_key(sub: dict, t: dt.datetime) -> str:
    p = str(sub.get("limit_period", "monthly")).lower()
    if p == "weekly":
        y, w, _ = t.isocalendar()
        return f"{y}-W{w:02d}"
    if p == "daily":
        return t.strftime("%Y-%m-%d")
    return t.strftime("%Y-%m")


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

def send_alert(cfg: dict, text: str) -> bool:
    sent = False
    a = cfg.get("alerts", {})
    tg = a.get("telegram") or {}
    if tg.get("enabled"):
        tok, chat = env(tg.get("bot_token_env")), env(tg.get("chat_id_env"))
        if tok and chat:
            try:
                r = requests.post(
                    f"https://api.telegram.org/bot{tok}/sendMessage",
                    json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                    timeout=HTTP_TIMEOUT,
                )
                sent = sent or r.ok
                if not r.ok:
                    log.error("telegram: HTTP %s %s", r.status_code, r.text[:200])
            except Exception as e:
                log.error("telegram: %s", e)
        else:
            log.error("telegram enabled but token/chat id env vars are missing")
    wh = a.get("webhook") or {}
    if wh.get("enabled"):
        url = env(wh.get("url_env"))
        if url:
            try:
                r = requests.post(url, json={"text": text, "content": text, "source": "aimeter"}, timeout=HTTP_TIMEOUT)
                sent = sent or r.ok
            except Exception as e:
                log.error("webhook: %s", e)
    if not sent:
        log.warning("ALERT (not delivered): %s", text)
    return sent


def alert_once(con, cfg: dict, key: str, text: str) -> None:
    if con.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
        return
    log.info("alert: %s", text)
    send_alert(cfg, text)
    con.execute("INSERT OR REPLACE INTO alerts_sent(key, ts, message) VALUES (?,?,?)", (key, iso(now_utc()), text))
    con.commit()


def crossed(used_pct: float, thresholds: list) -> float | None:
    hit = [t for t in sorted(thresholds) if used_pct >= t]
    return hit[-1] if hit else None


def run_checks(con, cfg: dict) -> None:
    s = cfg["settings"]
    th = s["alert_thresholds"]
    now = now_utc()
    month = now.strftime("%Y-%m")
    today = dt.date.today()

    for acct in cfg["api_accounts"]:
        aid, label = acct["id"], acct.get("label", acct["id"])
        snap = latest_snapshot(con, aid)
        if not snap:
            continue
        if not snap["ok"]:
            if snap["error"].startswith("NOT_CONNECTED"):
                if kv_get(con, f"oauth:{aid}"):  # was connected before, so the login expired
                    alert_once(con, cfg, f"{aid}:disconnected:{today}",
                               f"🔌 {label}: login expired, open the dashboard and click Reconnect")
                continue
            alert_once(con, cfg, f"{aid}:error:{today}", f"⚠️ {label}: collector failed\n{snap['error'][:300]}")
            continue
        m = snap["metrics"]
        for w in m.get("windows") or []:
            # plan windows (5-hour, weekly...). Budget/cap have their own alerts below,
            # per-minute API buckets reset too fast to be worth a message.
            if w["key"] in ("budget", "daily", "cap") or (w.get("window_seconds") or 0) <= 60:
                continue
            if w.get("used_pct") is None:
                continue
            t = crossed(w["used_pct"] / 100.0, th)
            if t is not None:
                when = ""
                if w.get("resets_at"):
                    left = dt.datetime.fromisoformat(w["resets_at"].replace("Z", "+00:00")) - now
                    secs = max(0, int(left.total_seconds()))
                    d_, h_, m_ = secs // 86400, secs % 86400 // 3600, secs % 3600 // 60
                    when = f", resets in {d_}d {h_}h" if d_ else f", resets in {h_}h {m_}m"
                alert_once(con, cfg, f"{aid}:win:{w['key']}:{t}:{(w.get('resets_at') or month)[:13]}",
                           f"⏳ {label}: {w['label']} at {w['used_pct']:.0f}%{when}")
        budget = acct.get("monthly_budget")
        if budget and m.get("spend_mtd") is not None:
            pct = m["spend_mtd"] / float(budget)
            t = crossed(pct, th)
            if t is not None:
                alert_once(con, cfg, f"{aid}:budget:{t}:{month}",
                           f"💸 {label}: ${m['spend_mtd']:.2f} of ${budget} monthly budget ({pct:.0%})")
        if m.get("cap") and m.get("cap_remaining") is not None:
            pct = 1 - float(m["cap_remaining"]) / float(m["cap"])
            t = crossed(pct, th)
            if t is not None:
                alert_once(con, cfg, f"{aid}:cap:{t}:{month}",
                           f"🧢 {label}: key cap {pct:.0%} used (${m['cap_remaining']:.2f} of ${m['cap']} left)")
        low = acct.get("low_balance")
        if low is not None and m.get("balance") is not None and m["balance"] < float(low):
            alert_once(con, cfg, f"{aid}:lowbal:{today}",
                       f"🪫 {label}: balance {m['balance']:.2f} {m.get('balance_currency', 'USD')} is below {low}")
        daily_budget = acct.get("daily_budget")
        if daily_budget and m.get("spend_today", 0) >= float(daily_budget):
            alert_once(con, cfg, f"{aid}:daily:{today}",
                       f"📈 {label}: ${m['spend_today']:.2f} spent today (daily limit ${daily_budget})")

    for sub in cfg["subscriptions"]:
        sid, name = sub["id"], sub.get("name", sub["id"])
        nr = next_renewal(sub, today)
        if nr:
            days = (nr - today).days
            for d in s["renewal_reminder_days"]:
                if days == d or (d == min(s["renewal_reminder_days"]) and 0 <= days <= d):
                    cost = f"{sub.get('cost')} {sub.get('currency', 'USD')}" if sub.get("cost") is not None else ""
                    alert_once(con, cfg, f"{sid}:renew:{nr}:{d}",
                               f"🔁 {name} renews in {days} day(s) on {nr} {cost}".strip())
                    break
        mu = latest_manual(con, sid)
        if mu:
            lim = mu["lim"] or sub.get("usage_limit")
            pct = (mu["value"] / 100.0) if mu["unit"] == "%" else (mu["value"] / float(lim) if lim else None)
            if pct is not None:
                t = crossed(pct, th)
                if t is not None:
                    period = usage_period_key(sub, dt.datetime.fromisoformat(mu["ts"].replace("Z", "+00:00")))
                    alert_once(con, cfg, f"{sid}:manual:{t}:{period}",
                               f"📊 {name}: {pct:.0%} of {sub.get('limit_period', 'monthly')} limit used")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

_SECRET_PATTERNS = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\b(sk|rk|pk)-[A-Za-z0-9_-]{8,}"), "[redacted]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',;&]{8,}"), r"\1\2[redacted]"),
]


def redact(text: str, acct: dict | None = None) -> str:
    """Remove this account's secrets, and anything shaped like a key or bearer token, from a message."""
    text = str(text)
    for field in ("api_key_env", "management_key_env", "access_token_env"):
        val = env((acct or {}).get(field))
        if val and len(val) >= 6:
            text = text.replace(val, "[redacted]")
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def cmd_collect(cfg, args) -> int:
    con = db_connect(cfg)
    failures = 0
    force = getattr(args, "force", False) or bool(args.only)
    for acct in cfg["api_accounts"]:
        if args.only and acct["id"] not in args.only:
            continue
        prov = acct.get("provider", "manual")
        fn = COLLECTORS.get(prov)
        ts = iso(now_utc())
        if not fn:
            log.error("%s: unknown provider %r", acct["id"], prov)
            continue
        if not force:
            last = latest_snapshot(con, acct["id"])
            every = int(acct.get("interval") or DEFAULT_INTERVAL.get(prov, 1800))
            if last:
                age = (now_utc() - dt.datetime.fromisoformat(last["ts"].replace("Z", "+00:00"))).total_seconds()
                if age < every - 5:
                    continue
        try:
            m = fn(acct, con)
            con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                        (ts, acct["id"], json.dumps(m)))
            log.info("%s: ok %s", acct["id"], {k: m[k] for k in ("spend_mtd", "balance", "cap_remaining") if k in m}
                     or [f"{w['label']} {w['used_pct']}%" for w in m.get("windows", [])])
        except NotConnected as e:
            msg = redact(str(e), acct)
            con.execute("INSERT INTO snapshots(ts, account_id, ok, error) VALUES (?,?,0,?)",
                        (ts, acct["id"], f"NOT_CONNECTED: {msg}"))
            log.info("%s: %s", acct["id"], msg)
        except Exception as e:
            failures += 1
            msg = redact(str(e), acct)  # providers sometimes echo the key back in their error text
            con.execute("INSERT INTO snapshots(ts, account_id, ok, error) VALUES (?,?,0,?)",
                        (ts, acct["id"], msg))
            log.error("%s: %s", acct["id"], msg)
        con.commit()
    # keep the DB small: one snapshot per account per hour is plenty after 7 days
    con.execute("""DELETE FROM snapshots WHERE ts < ? AND id NOT IN (
                     SELECT MAX(id) FROM snapshots GROUP BY account_id, substr(ts,1,13))""",
                (iso(now_utc() - dt.timedelta(days=7)),))
    con.commit()
    if not args.no_alerts:
        run_checks(con, cfg)
    return 1 if failures and args.strict else 0


def cmd_log(cfg, args) -> int:
    ids = {s["id"] for s in cfg["subscriptions"]} | {a["id"] for a in cfg["api_accounts"]}
    if args.id not in ids:
        print(f"Unknown id {args.id!r}. Known: {', '.join(sorted(ids))}")
        return 2
    con = db_connect(cfg)
    con.execute("INSERT INTO manual_usage(ts, sub_id, value, unit, lim, note) VALUES (?,?,?,?,?,?)",
                (iso(now_utc()), args.id, args.value, args.unit, args.limit, args.note))
    con.commit()
    print(f"Logged {args.value}{args.unit or ''} for {args.id}")
    run_checks(con, cfg)
    return 0


# Which Hermes billing providers / model names belong to each widget (substring match,
# case-insensitive). Override per account with `hermes_match: [...]`.
HERMES_MATCH = {
    "claude_sub": ["anthropic", "claude"], "anthropic": ["anthropic", "claude"], "anthropic_limits": ["anthropic", "claude"],
    "codex_sub": ["codex", "chatgpt"], "openai": ["openai"], "openai_limits": ["openai"],
    "kimi_code": ["kimi", "moonshot"], "kimi_api": ["moonshot", "kimi"], "opencode_go": ["opencode"],
    "nous": ["nous", "hermes"], "deepseek": ["deepseek"], "openrouter": ["openrouter"], "copilot": ["copilot"],
}


USAGE_RECORD_PROVIDERS = ("hermes", "local_logs")


def _usage_hourly(apis) -> list:
    """Hour rows [hour, provider, model, tokens, cost] from every usage-record source, tagged with the source."""
    out = []
    for a in apis:
        if a["provider"] in USAGE_RECORD_PROVIDERS and a["metrics"].get("hourly"):
            out += [r[:5] + [a["provider"]] for r in a["metrics"]["hourly"]]
    return out


def _hermes_usage_since(hourly, keys, start: dt.datetime):
    lo = start.astimezone(UTC).strftime("%Y-%m-%dT%H")
    tok = cost = 0
    hit = known = False
    for hr, provider, model, t, c, *_ in hourly:
        if hr < lo:
            continue
        hay = f"{provider} {model if not provider else ''}".lower()
        if any(k in hay for k in keys):
            tok += t
            if c is not None:  # None: the source has no cost for that hour (yet)
                cost += c
                known = True
            hit = True
    if not hit:
        return 0, 0.0
    return tok, round(cost, 4) if known else None


def enrich_windows(cfg, apis):
    """Attach tokens + cost used inside each window, so every % has tokens and $ beside it."""
    hourly = _usage_hourly(apis)
    now = now_utc()
    for a in apis:
        acct = next((x for x in cfg["api_accounts"] if x["id"] == a["id"]), {})
        keys = [k.lower() for k in (acct.get("hermes_match") or HERMES_MATCH.get(a["provider"], []))]
        tu = a["metrics"].get("token_usage")
        for w in a["metrics"].get("windows") or []:
            if w["key"] == "budget" and tu:
                w["tokens"], w["cost"], w["tokens_src"] = tu["totals"]["input"] + tu["totals"]["output"] + tu["totals"]["cache"], tu["totals"]["cost"], "provider"
                continue
            if w["key"] == "daily" and tu:
                t = tu["today"]
                w["tokens"], w["cost"], w["tokens_src"] = t["input"] + t["output"] + t["cache"], t["cost"], "provider"
                continue
            if not hourly or not keys or (w.get("window_seconds") or 0) <= 60:
                continue
            if w.get("resets_at") and w.get("window_seconds"):
                start = dt.datetime.fromisoformat(w["resets_at"].replace("Z", "+00:00")) - dt.timedelta(seconds=w["window_seconds"])
            elif w["key"] in ("budget", "cap"):
                start = month_start(now)
            elif w["key"] == "daily":
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                continue
            tok, cost = _hermes_usage_since(hourly, keys, start)
            if not tok:
                continue  # nothing recorded here: the usage came from elsewhere, so don't claim "0 tokens"
            w["tokens"], w["cost"] = tok, cost
            w["tokens_src"] = "records"
        # token limits: exact if configured (token_limits: {five_hour: 2000000}), else estimated
        exact = acct.get("token_limits") or {}
        for w in a["metrics"].get("windows") or []:
            if w.get("unit") in ("tokens", "USD") or w["key"] in ("budget", "daily", "cap"):
                continue
            if exact.get(w["key"]):
                w["token_limit"], w["token_limit_est"] = int(exact[w["key"]]), False
            elif w.get("tokens") and (w.get("used_pct") or 0) >= 3:
                w["token_limit"], w["token_limit_est"] = int(w["tokens"] / (w["used_pct"] / 100)), True
        # your own token budgets: token_budgets: {daily: 2000000, weekly: 10000000, monthly: ...}
        tb = acct.get("token_budgets") or {}
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        periods = {"daily": (day0, day0 + dt.timedelta(days=1), 86400, "Daily tokens"),
                   "weekly": (day0 - dt.timedelta(days=now.weekday()), day0 + dt.timedelta(days=7 - now.weekday()), 604800, "Weekly tokens"),
                   "monthly": (month_start(now), next_month_start(now), None, "Monthly tokens")}
        for per, (start, reset, secs, label) in periods.items():
            lim = tb.get(per)
            if not lim:
                continue
            if tu:
                d0 = start.strftime("%Y-%m-%d")
                rows = [d for d in tu["daily"] if d["date"] >= d0]
                used, cost = sum(d["tokens"] for d in rows), round(sum(d["cost"] for d in rows), 4)
            elif hourly and keys:
                used, cost = _hermes_usage_since(hourly, keys, start)
            else:
                continue
            w = window(f"tok_{per}", label, used / float(lim) * 100, resets_at=reset, window_seconds=secs,
                       limit=int(lim), remaining=int(lim) - used, unit="tokens")
            w["tokens"], w["cost"], w["tokens_src"] = used, cost, "provider" if tu else "records"
            a["metrics"].setdefault("windows", []).append(w)
        # month / today token totals for widgets without their own token report
        if not tu and hourly and keys and a["provider"] not in USAGE_RECORD_PROVIDERS:
            mt, mc = _hermes_usage_since(hourly, keys, month_start(now))
            dtk, dc = _hermes_usage_since(hourly, keys, now.replace(hour=0, minute=0, second=0, microsecond=0))
            if mt:
                a["metrics"]["hermes_tokens"] = {"month": mt, "month_cost": mc, "today": dtk, "today_cost": dc}
    for a in apis:
        if a["provider"] in USAGE_RECORD_PROVIDERS:
            a["metrics"].pop("hourly", None)


PRETTY = {"nous": "Nous Portal", "kimi-coding": "Kimi Code", "kimi": "Kimi", "moonshot": "Kimi API (Moonshot)",
          "opencode-go": "OpenCode Go", "opencode": "OpenCode", "deepseek": "DeepSeek", "openrouter": "OpenRouter",
          "anthropic": "Anthropic", "openai": "OpenAI", "openai-codex": "ChatGPT / Codex", "codex": "ChatGPT / Codex",
          "gemini": "Google Gemini", "google": "Google Gemini", "zhipu": "Zhipu (GLM)", "qwen": "Qwen",
          "xai": "xAI", "mistral": "Mistral", "ollama": "Ollama (local)", "lmstudio": "LM Studio (local)",
          "copilot": "GitHub Copilot", "unknown": "Unknown provider"}
SOURCE_LABEL = {"hermes": "Hermes sessions", "local_logs": "Local logs"}
# Prepaid accounts whose spend is worked out from balance drops, and the record-source key they belong to.
BALANCE_SPEND_KEY = {"deepseek": "deepseek", "kimi_api": "moonshot"}


def balance_spend(con, account_id: str, fx: dict, currency: str | None) -> dict | None:
    """Money spent on a prepaid account, from drops in its balance between checks.
    A rise is a top-up and is not counted. Converted to USD with settings.fx_to_usd."""
    rate = fx.get((currency or "USD").upper())
    if rate is None:
        return None
    rows = con.execute("SELECT ts, metrics FROM snapshots WHERE account_id=? AND ok=1 ORDER BY ts",
                       (account_id,)).fetchall()
    now = now_utc()
    month, today = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    prev, daily = None, {}
    for r in rows:
        try:
            bal = json.loads(r["metrics"] or "{}").get("balance")
        except ValueError:
            continue
        if bal is None:
            continue
        bal = float(bal)
        if prev is not None and bal < prev:
            day = r["ts"][:10]
            daily[day] = daily.get(day, 0.0) + (prev - bal) * float(rate)
        prev = bal
    return {"month": round(sum(v for d, v in daily.items() if d.startswith(month)), 4),
            "today": round(daily.get(today, 0.0), 4),
            "daily": {d: round(v, 4) for d, v in sorted(daily.items())}, "checks": len(rows)}


def token_total(apis) -> dict | None:
    """One deduplicated total across every source. Hermes and local logs record different tools, so
    they add up; each provider is then counted once, using whichever saw more of it: those records or
    the provider's own report (which already includes those calls). Prepaid balance drops set a floor
    under the cost."""
    hourly = _usage_hourly(apis)
    own = {a["provider"]: a for a in apis if a["provider"] in ("anthropic", "openai", "openrouter")
           and a["metrics"].get("token_usage")}
    spend = {BALANCE_SPEND_KEY[a["provider"]]: a for a in apis
             if a["provider"] in BALANCE_SPEND_KEY and (a["metrics"].get("balance_spend") or {}).get("daily")}
    if not hourly and not own and not spend:
        return None
    now = now_utc()
    month, today = now.strftime("%Y-%m"), now.strftime("%Y-%m-%d")
    groups = {}

    def g(name, label):
        return groups.setdefault(name, {"name": label, "own": None, "spend": None, "srcs": set(),
                                        "rec": {"month": 0, "month_cost": 0.0, "today": 0, "today_cost": 0.0,
                                                "daily": {}}})
    for prov, a in own.items():
        tu = a["metrics"]["token_usage"]
        t, d = tu["totals"], tu["today"]
        g(prov, a["label"])["own"] = {
            "month": t["input"] + t["output"] + t["cache"], "month_cost": t["cost"] or 0.0,
            "today": d["input"] + d["output"] + d["cache"], "today_cost": d["cost"] or 0.0,
            "daily": {x["date"]: (x["tokens"], x["cost"] or 0.0) for x in tu["daily"]}, "label": a["label"]}
    for hr, provider, model, tok, cost, src in hourly:
        cost = cost or 0.0
        hay = f"{provider} {model if not provider else ''}".lower()
        key = next((p for p in own if any(k in hay for k in HERMES_MATCH.get(p, [p]))), None) or (provider or "unknown")
        grp = g(key, own[key]["label"] if key in own else PRETTY.get(key.lower(), key))
        grp["srcs"].add(src)
        h, day = grp["rec"], hr[:10]
        dd = h["daily"].setdefault(day, [0, 0.0])
        dd[0] += tok
        dd[1] += cost
        if day.startswith(month):
            h["month"] += tok
            h["month_cost"] += cost
        if day == today:
            h["today"] += tok
            h["today_cost"] += cost
    for key, a in spend.items():
        g(key, PRETTY.get(key, a["label"]))["spend"] = a["metrics"]["balance_spend"]
    rows, daily = [], {}
    tot = {"tokens": 0, "cost": 0.0, "today": 0, "today_cost": 0.0}
    for key, grp in groups.items():
        o, h, sp = grp["own"], grp["rec"], grp["spend"]
        use_own = o is not None and o["month"] >= h["month"]
        src = dict(o if use_own else h)
        froms = [o["label"] + " report"] if use_own else [SOURCE_LABEL[x] for x in sorted(grp["srcs"])]
        if sp and (sp["month"] > src["month_cost"] or sp["today"] > src["today_cost"]):
            src["month_cost"] = max(src["month_cost"], sp["month"])
            src["today_cost"] = max(src["today_cost"], sp["today"])
            froms.append("balance changes")
        row = {"name": grp["name"], "tokens": int(src["month"]), "cost": round(src["month_cost"], 4),
               "today": int(src["today"]), "today_cost": round(src["today_cost"], 4),
               "from": " and ".join(froms) or "balance changes"}
        rows.append(row)
        tot["tokens"] += src["month"]
        tot["cost"] += src["month_cost"]
        tot["today"] += src["today"]
        tot["today_cost"] += src["today_cost"]
        sp_daily = (sp or {}).get("daily", {})
        mine = {}
        for day in set(h["daily"]) | set((o or {}).get("daily", {})) | set(sp_daily):
            ht, hc = h["daily"].get(day, (0, 0.0))
            ot, oc = (o or {}).get("daily", {}).get(day, (0, 0.0))
            mine[day] = (max(ht, ot), max(hc, oc, sp_daily.get(day, 0.0)))
            dt_ = daily.setdefault(day, [0, 0.0])
            dt_[0] += mine[day][0]
            dt_[1] += mine[day][1]
        # Last 14 days, oldest first, for the trend line; and this week against the week before.
        days14 = [(now - dt.timedelta(days=13 - i)).strftime("%Y-%m-%d") for i in range(14)]
        row["trend"] = [int(mine.get(d, (0, 0))[0]) for d in days14]
        row["week"] = sum(row["trend"][7:])
        row["prev_week"] = sum(row["trend"][:7])
    rows.sort(key=lambda r: (-r["tokens"], -r["cost"]))
    return {"tokens": int(tot["tokens"]), "cost": round(tot["cost"], 4), "today": int(tot["today"]),
            "today_cost": round(tot["today_cost"], 4), "by_provider": rows,
            "daily": [{"date": d, "tokens": int(v[0]), "cost": round(v[1], 4)} for d, v in sorted(daily.items())][-31:]}


# Providers that can be connected from the dashboard. "fields" are asked for in the form;
# secret fields are stored in the server's database and never sent back to the browser.
CATALOG = {
    "claude_sub":       {"name": "Claude plan", "group": "Plans", "blurb": "5-hour and weekly limits of a Claude Pro or Max plan.", "signin": True, "fields": []},
    "codex_sub":        {"name": "ChatGPT / Codex plan", "group": "Plans", "blurb": "5-hour and weekly Codex limits of a ChatGPT plan.", "signin": True, "fields": []},
    "kimi_code":        {"name": "Kimi Code", "group": "Plans", "blurb": "5-hour window and plan quota.", "fields": [["api_key", "Kimi Code API key", True]]},
    "opencode_go":      {"name": "OpenCode Go", "group": "Plans", "blurb": "Rolling 5-hour, weekly and monthly allowance.", "fields": [["api_key", "OpenCode Go API key", True]]},
    "copilot":          {"name": "GitHub Copilot", "group": "Plans",
                         "blurb": "Premium requests used out of your monthly allowance, and when it resets.",
                         "fields": [["api_key", "GitHub token", True],
                                    ["plan", "Plan: free, pro, pro_plus, business or enterprise (optional)", False],
                                    ["monthly_allowance", "Premium requests per month (optional)", False]]},
    "openrouter":       {"name": "OpenRouter", "group": "API credits", "blurb": "Spend, key cap, credits. Add a management key for tokens per model.",
                         "fields": [["api_key", "API key", True], ["management_key", "Management key (optional)", True], ["monthly_budget", "Monthly budget in USD (optional)", False]]},
    "deepseek":         {"name": "DeepSeek", "group": "API credits", "blurb": "Prepaid balance.", "fields": [["api_key", "API key", True], ["low_balance", "Alert below this balance (optional)", False]]},
    "kimi_api":         {"name": "Kimi API (Moonshot)", "group": "API credits", "blurb": "Prepaid balance on the Kimi Open Platform.", "fields": [["api_key", "API key", True], ["low_balance", "Alert below this balance (optional)", False]]},
    "anthropic":        {"name": "Anthropic API spend", "group": "API credits", "blurb": "Monthly spend and tokens per model. Needs an admin key (sk-ant-admin...).",
                         "fields": [["api_key", "Admin key", True], ["monthly_budget", "Monthly budget in USD (optional)", False]]},
    "openai":           {"name": "OpenAI API spend", "group": "API credits", "blurb": "Monthly spend and tokens per model. Needs an admin key.",
                         "fields": [["api_key", "Admin key", True], ["monthly_budget", "Monthly budget in USD (optional)", False]]},
    "anthropic_limits": {"name": "Anthropic rate limits", "group": "Rate limits", "blurb": "Tokens and requests per minute. One 1-token request per check.",
                         "fields": [["api_key", "API key", True], ["probe_model", "Model to check with", False, "claude-haiku-5-5"]]},
    "openai_limits":    {"name": "OpenAI rate limits", "group": "Rate limits", "blurb": "Tokens and requests per minute. One 1-token request per check.",
                         "fields": [["api_key", "API key", True], ["probe_model", "Model to check with", False, "gpt-5-nano"]]},
    "nous":             {"name": "Nous Portal", "group": "Rate limits", "blurb": "Rate limits, when Nous reports them. Credits are only shown in the Portal.",
                         "fields": [["api_key", "API key", True], ["probe_model", "Model to check with", False, "Hermes-4-70B"]]},
    "hermes":           {"name": "Hermes Agent", "group": "Usage records", "blurb": "Tokens and cost of everything Hermes runs, from its own records. No key needed.",
                         "fields": [["state_db", "Path to state.db", False, "/hermes/state.db"]]},
    "local_logs":       {"name": "Coding tools on this computer", "group": "Usage records",
                         "blurb": "Tokens from Claude Code, Codex, Qwen Code, DeepSeek Harness and Continue on the "
                                  "computer AI Meter runs on, plus ChatGPT limits from Codex. No key needed.",
                         "fields": [["claude_dir", "Claude Code logs folder (optional)", False],
                                    ["codex_dir", "Codex logs folder (optional)", False],
                                    ["qwen_dir", "Qwen Code logs folder (optional)", False],
                                    ["dsh_dir", "DeepSeek Harness sessions folder (optional)", False],
                                    ["continue_db", "Continue devdata.sqlite (optional)", False]]},
    "local_models":     {"name": "Local models", "group": "Usage records",
                         "blurb": "Ollama and LM Studio on this computer: what is installed and loaded. Free, no key.",
                         "fields": [["ollama_url", "Ollama address (optional)", False, "http://127.0.0.1:11434"],
                                    ["lmstudio_url", "LM Studio address (optional)", False, "http://127.0.0.1:1234"]]},
    "manual":           {"name": "Other service", "group": "Usage records", "blurb": "Anything without an API. You add readings by hand.", "fields": [["usage_url", "Usage page link (optional)", False]]},
}
SECRET_FIELDS = {"api_key": "api_key_env", "management_key": "management_key_env"}


def merge_db_accounts(cfg, con=None) -> None:
    """Add the providers connected from the dashboard to cfg["api_accounts"]."""
    con = con or db_connect(cfg)
    known = {a["id"] for a in cfg["api_accounts"]}
    for acct in kv_get(con, "ui_accounts") or []:
        if acct["id"] in known:
            continue
        acct = dict(acct, _ui=True)
        for field, env_field in SECRET_FIELDS.items():
            val = kv_get(con, f"secret:{acct['id']}:{field}")
            if val:
                name = f"AIMETER_SECRET_{re.sub(r'[^A-Z0-9]', '_', acct['id'].upper())}_{field.upper()}"
                os.environ[name] = val
                acct[env_field] = name
        cfg["api_accounts"].append(acct)


def add_ui_account(cfg, con, body: dict) -> dict:
    prov = body.get("provider")
    if prov not in CATALOG:
        raise RuntimeError("Unknown provider.")
    meta = CATALOG[prov]
    label = (str(body.get("label") or "").strip() or meta["name"])[:60]
    base = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or prov
    ids = {a["id"] for a in cfg["api_accounts"]}
    aid, n = base, 2
    while aid in ids:
        aid, n = f"{base}-{n}", n + 1
    acct = {"id": aid, "label": label, "provider": prov}
    for f in meta["fields"]:
        key, secret = f[0], f[2]
        val = str(body.get(key) or "").strip()
        if not val:
            if secret and key == "api_key":
                raise RuntimeError("Paste the API key first.")
            if len(f) > 3:
                acct[key] = f[3]
            continue
        if secret:
            kv_set(con, f"secret:{aid}:{key}", val)
        elif key in ("monthly_budget", "low_balance", "monthly_allowance"):
            try:
                acct[key] = float(val)
            except ValueError:
                raise RuntimeError(f"{f[1]} must be a number.")
        else:
            acct[key] = val
    if body.get("plan"):
        acct["plan"] = str(body["plan"])[:40]
    saved = kv_get(con, "ui_accounts") or []
    saved.append(acct)
    kv_set(con, "ui_accounts", saved)
    merge_db_accounts(cfg, con)
    return acct


def remove_ui_account(cfg, con, aid: str) -> None:
    saved = [a for a in (kv_get(con, "ui_accounts") or []) if a["id"] != aid]
    kv_set(con, "ui_accounts", saved)
    for field in SECRET_FIELDS:
        kv_set(con, f"secret:{aid}:{field}", None)
    kv_set(con, f"oauth:{aid}", None)
    con.execute("DELETE FROM snapshots WHERE account_id=?", (aid,))
    con.commit()
    cfg["api_accounts"][:] = [a for a in cfg["api_accounts"] if a["id"] != aid]


# AI tools AI Meter looks for on the computer it runs on: what each needs to be fully covered.
# "via" is the provider that covers it; "plan" an optional second provider for the tool's plan limits.
DETECT = [
    {"key": "claude_code", "name": "Claude Code", "paths": ["{claude}"], "via": "local_logs", "plan": "claude_sub"},
    {"key": "codex", "name": "Codex", "paths": ["{codex}"], "via": "local_logs", "plan": "codex_sub"},
    {"key": "qwen_code", "name": "Qwen Code", "paths": ["~/.qwen"], "via": "local_logs"},
    {"key": "deepseek_harness", "name": "DeepSeek Harness", "paths": ["~/.dsh"], "via": "local_logs"},
    {"key": "continue", "name": "Continue", "paths": ["~/.continue"], "via": "local_logs"},
    {"key": "copilot", "name": "GitHub Copilot", "paths": ["~/.copilot", "~/.config/github-copilot"], "via": "copilot"},
    {"key": "ollama", "name": "Ollama", "paths": ["~/.ollama"], "via": "local_models"},
    {"key": "lmstudio", "name": "LM Studio", "paths": ["~/.lmstudio", "~/.cache/lm-studio"], "via": "local_models"},
    {"key": "antigravity", "name": "Antigravity", "paths": ["~/.gemini/antigravity"], "via": None,
     "note": "No reliable way to read its limits yet. Check them in the Antigravity app."},
]


def detect_tools(cfg) -> list:
    """Which known AI tools are installed here, and whether AI Meter already covers them."""
    have = {a.get("provider") for a in cfg["api_accounts"]}
    claude = str(Path(_default_claude_dir()).parent)
    codex = str(Path(_default_codex_dir()).parent)
    out = []
    for t in DETECT:
        paths = [Path(x.format(claude=claude, codex=codex)).expanduser() for x in t["paths"]]
        if not any(x.exists() for x in paths):
            continue
        covered = t["via"] in have if t["via"] else False
        plan = t.get("plan")
        out.append({"key": t["key"], "name": t["name"], "via": t["via"], "covered": covered,
                    "plan": plan, "plan_connected": plan in have if plan else None, "note": t.get("note")})
    return out


def _ts(v: str) -> dt.datetime:
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def forecast_runout(w: dict, points: list, now: dt.datetime | None = None) -> dict:
    """When a limit runs out at the recent pace. Uses this window's readings from the last 6 hours (a
    weekly window's average since Monday hides today's pace). Returns {} when there is too little to go on:
    fewer than two readings 10 minutes apart, no reset time, or use not rising."""
    p, resets, size = w.get("used_pct"), w.get("resets_at"), w.get("window_seconds") or 0
    if p is None or not resets or size <= 60:
        return {}
    if p >= 100:
        return {"runout": "now"}
    now = now or now_utc()
    try:
        reset_t = _ts(resets)
        start = max(reset_t - dt.timedelta(seconds=size), now - dt.timedelta(hours=6))
        pts = [(_ts(t), v) for t, v in points if _ts(t) >= start]
    except ValueError:
        return {}
    if len(pts) < 2:
        return {}
    (t0, v0), (t1, v1) = pts[0], pts[-1]
    span = (t1 - t0).total_seconds()
    if span < 600:
        return {}
    rate = (v1 - v0) / span  # percent per second
    if rate <= 0:
        return {"runout": None, "pace_pct_per_hour": 0.0}
    at = t1 + dt.timedelta(seconds=(100 - v1) / rate)
    return {"runout": iso(at) if at < reset_t else None, "pace_pct_per_hour": round(rate * 3600, 2)}


def build_state(cfg) -> dict:
    con = db_connect(cfg)
    fx = cfg["settings"]["fx_to_usd"]
    today = dt.date.today()
    apis = []
    since = iso(now_utc() - dt.timedelta(hours=24))
    for a in cfg["api_accounts"]:
        prov = a.get("provider", "manual")
        snap = latest_snapshot(con, a["id"])
        good = snap if snap and snap["ok"] else latest_snapshot(con, a["id"], ok_only=True)
        mu = latest_manual(con, a["id"])
        err = snap["error"] if snap and not snap["ok"] else None
        # 24h history of each window's used % (for the sparklines), max ~96 points
        hist = {}
        rows = con.execute("SELECT ts, metrics FROM snapshots WHERE account_id=? AND ok=1 AND ts>=? ORDER BY ts",
                           (a["id"], since)).fetchall()
        step = max(1, len(rows) // 96)
        full = {}
        for i, r in enumerate(rows):
            for w in (json.loads(r["metrics"]).get("windows") or []):
                if w.get("used_pct") is not None:
                    full.setdefault(w["key"], []).append((r["ts"], w["used_pct"]))
                    if i % step == 0:
                        hist.setdefault(w["key"], []).append([r["ts"], w["used_pct"]])
        for w in ((good or {}).get("metrics") or {}).get("windows") or []:
            w.update(forecast_runout(w, full.get(w["key"], [])))
        oauth = kv_get(con, f"oauth:{a['id']}") if prov in OAUTH else None
        name, kind = PROVIDER_META.get(prov, (prov, "spend"))
        apis.append({
            "id": a["id"], "label": a.get("label", a["id"]), "provider": prov, "provider_name": name, "kind": kind,
            "removable": bool(a.get("_ui")),
            "budget": a.get("monthly_budget"), "console_url": a.get("console_url") or a.get("usage_url"),
            "notes": a.get("notes"), "plan": a.get("plan"), "low_balance": a.get("low_balance"),
            "interval": int(a.get("interval") or DEFAULT_INTERVAL.get(prov, 1800)),
            "metrics": good["metrics"] if good else {},
            "updated": good["ts"] if good else None,
            "checked": snap["ts"] if snap else None,
            "error": err.replace("NOT_CONNECTED: ", "") if err else None,
            "not_connected": bool(err and err.startswith("NOT_CONNECTED")),
            "oauth": prov in OAUTH, "connected": bool(oauth) if prov in OAUTH else None,
            "account": (oauth or {}).get("email"),
            "signed_in_at": (oauth or {}).get("saved_at"),
            "history": hist,
            "manual": mu,
        })
    subs = []
    for sb in cfg["subscriptions"]:
        nr = next_renewal(sb, today)
        mu = latest_manual(con, sb["id"])
        if mu and not mu["lim"] and mu["unit"] != "%":
            mu["lim"] = sb.get("usage_limit")
        subs.append({
            "id": sb["id"], "name": sb.get("name", sb["id"]), "vendor": sb.get("vendor", ""),
            "category": sb.get("category", "other"), "plan": sb.get("plan", ""),
            "cost": sb.get("cost"), "currency": sb.get("currency", "USD"),
            "cycle": sb.get("cycle", "monthly"),
            "monthly_usd": monthly_cost_usd(sb, fx),
            "next_renewal": nr.isoformat() if nr else None,
            "days_left": (nr - today).days if nr else None,
            "limits": sb.get("limits", ""), "usage_url": sb.get("usage_url"),
            "limit_period": sb.get("limit_period"),
            "manual": mu,
        })
    # ChatGPT plan limits that Codex wrote into its logs: used when there is no live sign-in
    # reading, or when the logs are newer than it.
    logged = [x["metrics"]["codex_limits"] for x in apis
              if x["provider"] == "local_logs" and x["metrics"].get("codex_limits")]
    lim = max(logged, key=lambda x: x["ts"]) if logged else None
    for a in apis:
        if a["provider"] != "codex_sub" or not lim:
            continue
        live = a["updated"] if a["metrics"].get("windows") and not a["error"] else None
        if live and live >= lim["ts"]:
            continue
        a["metrics"] = {**a["metrics"], "windows": codex_windows_from_logs(lim),
                        "plan": lim.get("plan") or a["metrics"].get("plan")}
        a.update(updated=lim["ts"], checked=lim["ts"], error=None, not_connected=False, limits_from_logs=lim["ts"])
    for a in apis:
        if a["provider"] in BALANCE_SPEND_KEY and a["metrics"].get("balance") is not None:
            bs = balance_spend(con, a["id"], fx, a["metrics"].get("balance_currency"))
            if bs:
                a["metrics"]["balance_spend"] = bs
                if bs["checks"] >= 2:  # one reading cannot show spend yet: leave it unknown, not $0
                    a["metrics"].setdefault("spend_mtd", bs["month"])
                    a["metrics"].setdefault("spend_today", bs["today"])
    total = token_total(apis)
    enrich_windows(cfg, apis)
    token_sources = [{"id": x["id"], "label": x["label"], "provider": x["provider"],
                      "usage": x["metrics"]["token_usage"], "updated": x["updated"]}
                     for x in apis if x["metrics"].get("token_usage")]
    token_sources.sort(key=lambda t: (t["provider"] != "hermes", -(t["usage"]["totals"].get("input", 0) + t["usage"]["totals"].get("output", 0))))
    api_mtd = sum((x["metrics"].get("spend_mtd") or 0) for x in apis if x["provider"] not in USAGE_RECORD_PROVIDERS)
    fixed = sum((x["monthly_usd"] or 0) for x in subs)
    recent = [dict(r) for r in con.execute("SELECT * FROM alerts_sent ORDER BY ts DESC LIMIT 15")]
    return {
        "generated": iso(now_utc()), "apis": apis, "subscriptions": subs, "token_sources": token_sources,
        "token_total": total,
        "detected": detect_tools(cfg),
        "catalog": {k: {"name": v["name"], "group": v["group"], "blurb": v["blurb"], "signin": v.get("signin", False),
                        "fields": [{"key": f[0], "label": f[1], "secret": f[2], "default": f[3] if len(f) > 3 else ""} for f in v["fields"]]}
                    for k, v in CATALOG.items()},
        "totals": {"api_mtd_usd": round(api_mtd, 2), "subscriptions_monthly_usd": round(fixed, 2),
                   "all_usd": round(api_mtd + fixed, 2)},
        "alerts": recent,
    }


def cmd_status(cfg, args) -> int:
    st = build_state(cfg)
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"\nAPI ACCOUNTS (month to date, generated {st['generated']})")
    for a in st["apis"]:
        m = a["metrics"]
        parts = []
        if "spend_mtd" in m:
            parts.append(f"${m['spend_mtd']:.2f}" + (f" / ${a['budget']}" if a["budget"] else ""))
        if m.get("balance") is not None:
            parts.append(f"balance {m['balance']:.2f}")
        if m.get("cap"):
            parts.append(f"cap left {m.get('cap_remaining')}/{m['cap']}")
        if a["error"]:
            parts.append(f"ERROR: {a['error'][:80]}")
        print(f"  {a['label']:<28} {'  '.join(parts) or '(no data yet)'}")
    print("\nSUBSCRIPTIONS")
    for s in sorted(st["subscriptions"], key=lambda x: (x["days_left"] is None, x["days_left"] or 0)):
        cost = f"{s['cost']} {s['currency']}/{s['cycle']}" if s["cost"] is not None else ""
        ren = f"renews {s['next_renewal']} ({s['days_left']}d)" if s["next_renewal"] else ""
        mu = s["manual"]
        use = f"  usage {mu['value']}{mu['unit'] or ''}" if mu else ""
        print(f"  {s['name']:<28} {cost:<22} {ren}{use}")
    t = st["totals"]
    print(f"\nTOTAL  API this month ${t['api_mtd_usd']}  +  subscriptions ${t['subscriptions_monthly_usd']}/mo"
          f"  =  ${t['all_usd']}\n")
    return 0


def cmd_test_alert(cfg, args) -> int:
    ok = send_alert(cfg, "✅ AI Meter test alert: your usage monitor can reach you.")
    print("sent" if ok else "not sent (check alerts config and env vars)")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def cmd_serve(cfg, args) -> int:
    d = cfg["settings"]["dashboard"]
    host = args.host or d.get("host", "127.0.0.1")
    port = args.port or int(d.get("port", 8787))
    token = env(d.get("token_env"))
    page = (HERE / "dashboard.html").read_text(encoding="utf-8")
    # Without a token the dashboard must only answer to local names, or a web page using
    # DNS rebinding could read it. Extra names (a LAN name, say) go in settings.dashboard.allowed_hosts.
    local_names = {"127.0.0.1", "localhost", "[::1]", "::1"} | {str(h).lower() for h in d.get("allowed_hosts") or []}
    if host not in ("0.0.0.0", "::", ""):  # nosec B104: a check, not a bind
        local_names.add(host.lower())
    max_body = 1024 * 1024
    headers_all = {
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                    "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                                    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    }

    class H(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            log.debug(fmt, *a)

        def _host_ok(self) -> bool:
            if token:
                return True  # behind a proxy on a real domain the token is the protection
            h = (self.headers.get("Host") or "").strip().lower()
            name = h.rsplit(":", 1)[0] if not h.startswith("[") else h.split("]")[0] + "]"
            return bool(name) and name in local_names

        def _same_origin(self) -> bool:
            """Refuse requests a browser sends on behalf of another site."""
            if (self.headers.get("Sec-Fetch-Site") or "").lower() in ("cross-site", "same-site"):
                return False
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # not a browser cross-site request (curl, scripts, same-origin GET)
            o = urlparse(origin)
            return o.scheme in ("http", "https") and o.netloc.lower() == (self.headers.get("Host") or "").lower()

        def _authed(self) -> bool:
            if not token:
                return True
            q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            if q and hmac.compare_digest(q, token):
                return True
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    pw = base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                    return hmac.compare_digest(pw, token)
                except Exception:
                    return False
            if auth.startswith("Bearer "):
                return hmac.compare_digest(auth[7:], token)
            return False

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            for k, v in headers_all.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _deny(self, code, text):
            return self._send(code, json.dumps({"error": text}).encode(), "application/json")

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if not self._host_ok():
                return self._deny(403, "Unknown host name. Open the dashboard at localhost, "
                                       "or add this name to settings.dashboard.allowed_hosts.")
            if not self._authed():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="AI Meter"')
                for k, v in headers_all.items():
                    self.send_header(k, v)
                self.end_headers()
                return
            if path == "/api/state":
                return self._send(200, json.dumps(build_state(cfg)).encode(), "application/json")
            if path in ("/", "/index.html"):
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return self._deny(403, "Unknown host name.")
            if not self._authed():
                return self._send(401, b"unauthorized", "text/plain")
            if not self._same_origin():
                return self._deny(403, "Requests from other sites are not allowed.")
            # JSON only: browsers cannot send it cross-site without asking first (CSRF protection).
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._deny(415, "Send JSON (Content-Type: application/json).")
            path = urlparse(self.path).path
            raw_len = (self.headers.get("Content-Length") or "0").strip()
            if not raw_len.isdigit():
                return self._deny(400, "Bad Content-Length.")
            n = int(raw_len)
            if n > max_body:
                return self._deny(413, "Request too large.")
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, RecursionError):
                return self._send(400, b"bad json", "text/plain")
            if not isinstance(body, dict):
                return self._send(400, b"bad json", "text/plain")
            if path == "/api/log":
                try:
                    value = float(body.get("value"))
                    limit = float(body["limit"]) if body.get("limit") not in (None, "") else None
                except (TypeError, ValueError):
                    return self._send(400, json.dumps({"error": "The value and limit must be numbers."}).encode(),
                                      "application/json")
                ns = argparse.Namespace(id=str(body.get("id", "")), value=value,
                                        unit=body.get("unit") or None, limit=limit,
                                        note=body.get("note"))
                rc = cmd_log(cfg, ns)
                return self._send(200 if rc == 0 else 400, json.dumps({"ok": rc == 0}).encode(), "application/json")
            if path == "/api/collect":
                only = body.get("only") or None
                cmd_collect(cfg, argparse.Namespace(only=only, no_alerts=False, strict=False, force=True))
                return self._send(200, b'{"ok":true}', "application/json")
            if path in ("/api/providers/add", "/api/providers/remove"):
                con = db_connect(cfg)
                try:
                    if path.endswith("add"):
                        acct = add_ui_account(cfg, con, body)
                        if acct["provider"] in OAUTH:
                            return self._send(200, json.dumps({"ok": True, "id": acct["id"], "signin": True}).encode(), "application/json")
                        cmd_collect(cfg, argparse.Namespace(only=[acct["id"]], no_alerts=True, strict=False, force=True))
                        snap = latest_snapshot(con, acct["id"])
                        res = {"ok": True, "id": acct["id"], "checked": bool(snap and snap["ok"]),
                               "error": (snap or {}).get("error")}
                    else:
                        acct = next((a for a in cfg["api_accounts"] if a["id"] == body.get("id")), None)
                        if not acct or not acct.get("_ui"):
                            raise RuntimeError("Only providers connected from this page can be removed here. Edit config.yaml for the others.")
                        remove_ui_account(cfg, con, acct["id"])
                        res = {"ok": True}
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
                return self._send(200, json.dumps(res).encode(), "application/json")
            if path.startswith("/api/oauth/"):
                acct = next((a for a in cfg["api_accounts"] if a["id"] == body.get("id")), None)
                if not acct or acct.get("provider") not in OAUTH:
                    return self._send(400, b'{"error":"unknown account"}', "application/json")
                con = db_connect(cfg)
                try:
                    if path == "/api/oauth/start":
                        res = oauth_start(con, acct)
                        if acct["provider"] == "codex_sub":
                            res["auto"] = start_codex_callback(cfg, acct)
                    elif path == "/api/oauth/finish":
                        rec = oauth_finish(con, acct, str(body.get("code", "")))
                        if acct["provider"] == "codex_sub":
                            stop_codex_callback(wait=True)
                        cmd_collect(cfg, argparse.Namespace(only=[acct["id"]], no_alerts=True, strict=False, force=True))
                        res = {"ok": True, "account": rec.get("email")}
                    elif path == "/api/oauth/disconnect":
                        kv_set(con, f"oauth:{acct['id']}", None)
                        res = {"ok": True}
                    else:
                        return self._send(404, b"not found", "text/plain")
                except Exception as e:
                    return self._send(400, json.dumps({"error": str(e)[:300]}).encode(), "application/json")
                return self._send(200, json.dumps(res).encode(), "application/json")
            self._send(404, b"not found", "text/plain")

    if host not in ("127.0.0.1", "localhost") and not token:
        log.warning("Dashboard is listening on %s without a token. Set settings.dashboard.token_env.", host)
    log.info("dashboard on http://%s:%s", host, port)
    if not getattr(args, "no_collect", False):
        import threading
        import time as _time

        def loop():
            while True:
                try:
                    c = load_config(args.config)
                    merge_db_accounts(c)
                    cmd_collect(c, argparse.Namespace(only=None, no_alerts=False, strict=False, force=False))
                except Exception as e:  # keep the loop alive
                    log.error("collector loop: %s", e)
                _time.sleep(60)
        threading.Thread(target=loop, daemon=True, name="aimeter-collector").start()
        log.info("background checks every 60 s (each provider on its own interval)")
    print(f"\n  AI Meter is running: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}\n"  # nosec B104: only chooses the name to print
          f"  config: {getattr(args, 'config', None) or default_config_path()}\n", flush=True)
    ThreadingHTTPServer((host, port), H).serve_forever()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aimeter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-c", "--config", help="path to config.yaml (default: ~/.aimeter/config.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sp = p.add_subparsers(dest="cmd")

    c = sp.add_parser("collect", help="poll APIs, store snapshot, send alerts")
    c.add_argument("--only", nargs="*", help="only these account ids")
    c.add_argument("--no-alerts", action="store_true")
    c.add_argument("--strict", action="store_true", help="exit 1 if any collector fails")
    c.add_argument("--force", action="store_true", help="ignore per-account intervals and poll everything now")

    s = sp.add_parser("serve", help="run the dashboard and the background checks")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--no-collect", action="store_true", help="dashboard only; run checks elsewhere")

    st = sp.add_parser("status", help="terminal summary")
    st.add_argument("--json", action="store_true")

    lg = sp.add_parser("log", help="record a manual usage reading")
    lg.add_argument("id")
    lg.add_argument("value", type=float)
    lg.add_argument("--unit", help='e.g. "%" for percent of limit, "msgs", "credits"')
    lg.add_argument("--limit", type=float, help="the limit the value is measured against")
    lg.add_argument("--note")

    sp.add_parser("test-alert", help="send a test alert")

    args = p.parse_args(argv)
    if not args.cmd:  # plain `aimeter` = start the dashboard
        args = p.parse_args(list(argv or sys.argv[1:]) + ["serve"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    merge_db_accounts(cfg)
    return {"collect": cmd_collect, "serve": cmd_serve, "status": cmd_status,
            "log": cmd_log, "test-alert": cmd_test_alert}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
