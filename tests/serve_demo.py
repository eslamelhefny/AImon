"""A realistic demo of AI Meter with every provider and tool connected, for screenshots and product images.
All data is generated here and fake: no network calls, no real accounts, only example.com addresses.
Run:  python tests/serve_demo.py   then open http://localhost:8800/?token=demo"""
import datetime as dt
import json
import os
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ai_meter import cli  # noqa: E402

random.seed(7)
D = Path(tempfile.mkdtemp(prefix="aimeter-demo-"))
NOW = dt.datetime.now(dt.timezone.utc)
iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")  # noqa: E731
later = lambda **kw: (NOW + dt.timedelta(**kw)).isoformat()  # noqa: E731

# ---------------------------------------------------------------- coding tool records on "this computer"
claude = D / "claude" / "projects" / "demo"
claude.mkdir(parents=True)
lines, n = [], 0
for day in range(20, -1, -1):
    for k in range(random.randint(6, 14)):
        t = NOW - dt.timedelta(days=day, hours=random.randint(0, 9), minutes=random.randint(0, 59))
        n += 1
        model = "claude-opus-5-5" if k % 4 else "claude-sonnet-5-5"
        lines.append({"type": "assistant", "sessionId": f"s{day}", "timestamp": iso(t), "requestId": f"r{n}",
                      "message": {"id": f"msg{n}", "model": model, "role": "assistant", "content": [],
                                  "usage": {"input_tokens": random.randint(5, 900), "output_tokens": random.randint(400, 9000),
                                            "cache_read_input_tokens": random.randint(200_000, 900_000),
                                            "cache_creation_input_tokens": random.randint(5_000, 60_000)}}})
    lines.append({"type": "cost-state", "sessionId": f"s{day}", "totalCostUSD": round(random.uniform(3, 9), 2),
                  "modelUsage": {"claude-opus-5-5": {"costUSD": round(random.uniform(3, 9), 2)}}})
(claude / "a.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")

codex = D / "codex" / "sessions" / "2026"
codex.mkdir(parents=True)
rows = [{"type": "session_meta", "timestamp": iso(NOW), "payload": {"model_provider": "openai"}},
        {"type": "turn_context", "timestamp": iso(NOW), "payload": {"turn_id": "t1", "model": "gpt-6.1-sol"}}]
for i in range(140):
    t = NOW - dt.timedelta(hours=random.randint(0, 24 * 18))
    rows.append({"type": "token_usage_record", "timestamp": iso(t), "payload": {"turn_id": "t1", "response_id": f"x{i}", "usage": {
        "input_tokens": random.randint(20_000, 90_000), "cached_input_tokens": random.randint(5_000, 20_000),
        "output_tokens": random.randint(300, 4000), "reasoning_output_tokens": random.randint(0, 800)}}})
(codex / "rollout-demo.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")

dsh = D / "dsh" / "sessions" / "demo" / "session-1"
dsh.mkdir(parents=True)
rows = []
for i in range(90):
    t = NOW - dt.timedelta(hours=random.randint(0, 24 * 15))
    rows.append({"type": "assistant/message", "time": int(t.timestamp() * 1000), "data": {
        "message": {"id": f"d{i}", "source": {"provider": "deepseek-official", "model": "deepseek-v4-pro"}},
        "usage": {"inputTokens": random.randint(400, 3000), "outputTokens": random.randint(100, 1500),
                  "cacheReadTokens": random.randint(3000, 20000), "cacheWriteTokens": 0}}})
(dsh / "session.v4.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")

qwen = D / "qwen" / "demo" / "chats"
qwen.mkdir(parents=True)
rows = [{"type": "assistant", "uuid": f"q{i}", "sessionId": "q", "model": "qwen3-coder-plus",
         "timestamp": iso(NOW - dt.timedelta(hours=random.randint(0, 24 * 10))),
         "usageMetadata": {"promptTokenCount": random.randint(8000, 30000), "cachedContentTokenCount": random.randint(1000, 6000),
                           "candidatesTokenCount": random.randint(300, 2000), "thoughtsTokenCount": random.randint(0, 400)}}
        for i in range(60)]
(qwen / "a.jsonl").write_text("\n".join(json.dumps(x) for x in rows), encoding="utf-8")

cont = D / "continue" / "dev_data"
cont.mkdir(parents=True)
c = sqlite3.connect(cont / "devdata.sqlite")
c.execute("CREATE TABLE tokens_generated (id INTEGER PRIMARY KEY, model TEXT, provider TEXT, tokens_generated INTEGER, "
          "tokens_prompt INTEGER, timestamp DATETIME)")
for i in range(80):
    t = NOW - dt.timedelta(hours=random.randint(0, 24 * 12))
    c.execute("INSERT INTO tokens_generated(model, provider, tokens_generated, tokens_prompt, timestamp) VALUES (?,?,?,?,?)",
              ("qwen2.5-coder:7b", "ollama", random.randint(20, 200), random.randint(800, 4000), t.strftime("%Y-%m-%d %H:%M:%S")))
c.commit()
c.close()

# Hermes Agent's database
h = sqlite3.connect(D / "hermes.db")
h.execute("""CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, message_count INTEGER, api_call_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  billing_provider TEXT, estimated_cost_usd REAL, actual_cost_usd REAL)""")
for i in range(120):
    t = NOW - dt.timedelta(hours=random.randint(0, 24 * 18))
    model, prov, price = random.choice([("deepseek-v4-pro", "deepseek", 0.4e-6), ("kimi-k3", "kimi-coding", 0.6e-6),
                                        ("qwen3.7-max", "opencode-go", 1.2e-6), ("openai/gpt-5", "openrouter", 1.25e-6)])
    i_, o_ = random.randint(20_000, 300_000), random.randint(2_000, 30_000)
    h.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (f"h{i}", model, t.timestamp(), 8, random.randint(3, 20), i_, o_, random.randint(0, 200_000), 0, 0, prov, (i_ + o_ * 4) * price, None))
h.commit()
h.close()

# ---------------------------------------------------------------- config
cfg_text = f"""
settings:
  db_path: data/demo.db
  dashboard: {{token_env: AIMETER_DASHBOARD_TOKEN}}
api_accounts:
  - {{id: claude-plan, label: Claude, provider: claude_sub, plan: max}}
  - {{id: chatgpt-plan, label: ChatGPT, provider: codex_sub}}
  - {{id: copilot, label: GitHub Copilot, provider: copilot, api_key_env: DEMO_KEY, plan: pro}}
  - {{id: kimi-code, label: Kimi Code, provider: kimi_code, api_key_env: DEMO_KEY, plan: allegro}}
  - {{id: opencode-go, label: OpenCode Go, provider: opencode_go, api_key_env: DEMO_KEY, caps_usd: {{rolling: 12, weekly: 30, monthly: 60}}}}
  - {{id: deepseek, label: DeepSeek, provider: deepseek, api_key_env: DEMO_KEY, low_balance: 3}}
  - {{id: openrouter, label: OpenRouter, provider: openrouter, api_key_env: DEMO_KEY, monthly_budget: 30}}
  - {{id: anthropic, label: Anthropic API, provider: anthropic, api_key_env: DEMO_KEY, monthly_budget: 200}}
  - {{id: hermes, label: Hermes Agent, provider: hermes, state_db: "{(D / 'hermes.db').as_posix()}"}}
  - {{id: coding-tools, label: Coding tools on this computer, provider: local_logs,
      claude_dir: "{(D / 'claude' / 'projects').as_posix()}", codex_dir: "{(D / 'codex' / 'sessions').as_posix()}",
      qwen_dir: "{(D / 'qwen').as_posix()}", dsh_dir: "{(D / 'dsh' / 'sessions').as_posix()}",
      continue_db: "{(cont / 'devdata.sqlite').as_posix()}"}}
  - {{id: local-models, label: Local models, provider: local_models}}
subscriptions:
  - {{id: sub-claude, name: Claude, vendor: Anthropic, plan: Max 5x, cost: 100, currency: USD, cycle: monthly, renews_on: "2026-01-05", limits: "5-hour session window + weekly caps"}}
  - {{id: sub-chatgpt, name: ChatGPT, vendor: OpenAI, plan: Plus, cost: 20, currency: USD, cycle: monthly, renews_on: "2026-01-12", limits: "Codex 5-hour and weekly limits"}}
  - {{id: sub-copilot, name: GitHub Copilot, vendor: GitHub, plan: Pro, cost: 10, currency: USD, cycle: monthly, renews_on: "2026-02-01", limits: "300 premium requests a month"}}
  - {{id: sub-kimi, name: Kimi Code, vendor: Moonshot, plan: Allegro, cost: 19, currency: USD, cycle: monthly, renews_on: "2026-03-08", limits: "5-hour window and plan quota"}}
  - {{id: sub-opencode, name: OpenCode Go, vendor: OpenCode, plan: Go, cost: 10, currency: USD, cycle: monthly, renews_on: "2026-03-20", limits: "5-hour, weekly and monthly allowance"}}
"""
(D / "config.yaml").write_text(cfg_text, encoding="utf-8")
os.environ.update(DEMO_KEY="demo-key", AIMETER_DASHBOARD_TOKEN="demo")


# ---------------------------------------------------------------- fake provider answers
class R:
    def __init__(self, d, code=200, h=None, url=""):
        self._d, self.status_code, self.ok, self.headers, self.url = d, code, code < 300, h or {}, url
        self.text = json.dumps(d)

    def json(self):
        return self._d


def fake_get(url, headers=None, params=None, timeout=None):
    if "oauth/usage" in url:
        return R({"five_hour": {"utilization": 47.0, "resets_at": later(hours=2, minutes=10)},
                  "seven_day": {"utilization": 64.0, "resets_at": later(days=3, hours=5)},
                  "seven_day_opus": {"utilization": 91.0, "resets_at": later(days=3, hours=5)}})
    if "wham/usage" in url:
        return R({"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 31, "limit_window_seconds": 18000, "reset_after_seconds": 3 * 3600 + 1200},
            "secondary_window": {"used_percent": 72, "limit_window_seconds": 604800, "reset_at": int((NOW + dt.timedelta(days=2, hours=4)).timestamp())}}})
    if "copilot_internal/user" in url:
        nxt = cli.next_month_start(NOW).strftime("%Y-%m-%d")
        return R({"login": "demo", "copilot_plan": "pro", "quota_reset_date": nxt, "quota_snapshots": {
            "premium_interactions": {"entitlement": 300, "remaining": 86, "percent_remaining": 28.7, "unlimited": False},
            "chat": {"unlimited": True}, "completions": {"unlimited": True}}})
    if "kimi.com/coding/v1/usages" in url:
        return R({"usage": {"limit": "100", "remaining": "45", "resetTime": later(days=2, hours=6)},
                  "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                              "detail": {"limit": "100", "remaining": "71", "resetTime": later(hours=3, minutes=40)}}]})
    if "zen/go/v1/usage" in url:
        return R({"usage": {"rolling": {"percent": 34, "resetsAt": later(hours=1, minutes=5)},
                            "weekly": {"percent": 58, "resetsAt": later(days=4, hours=20)},
                            "monthly": {"percent": 22, "resetsAt": later(days=18)}}})
    if "deepseek.com/user/balance" in url:
        return R({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "8.42"}]})
    if url.endswith("/key"):
        return R({"data": {"limit": 30, "limit_remaining": 7.4, "usage": 80, "usage_daily": 1.2, "usage_monthly": 22.6,
                           "limit_reset": "monthly", "rate_limit": {"requests": 200, "interval": "10s"}}})
    if url.endswith("/credits"):
        return R({"data": {"total_credits": 50, "total_usage": 38.2}})
    if "cost_report" in url:
        return R({"data": [{"starting_at": f"{NOW:%Y-%m}-{d:02d}T00:00:00Z", "results": [{"amount": str(180 + d * 37 % 260), "model": "claude-sonnet-5-5"}]}
                           for d in range(1, NOW.day + 1)], "has_more": False})
    if "usage_report" in url:
        return R({"data": [], "has_more": False})
    if "11434/api/version" in url:
        return R({"version": "0.40.16"})
    if "11434/api/tags" in url:
        return R({"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3.3:70b"}, {"name": "deepseek-r1:14b"}]})
    if "11434/api/ps" in url:
        return R({"models": [{"name": "qwen2.5-coder:7b", "size": 6_000_000_000, "size_vram": 6_000_000_000}]})
    if "1234/" in url:
        raise cli.requests.ConnectionError("not running")
    raise cli.requests.ConnectionError(f"demo: no fake for {url}")


def fake_post(url, json=None, data=None, timeout=None, headers=None):
    if "oauth/token" in url:
        return R({"access_token": "demo-access", "refresh_token": "demo-refresh", "expires_in": 3600 * 24 * 365,
                  "account": {"email_address": "you@example.com"}})
    return R({})


cli.requests.get, cli.requests.post = fake_get, fake_post
# "Found on this computer" comes from fixed demo values, never from the machine running the demo.
cli.detect_tools = lambda cfg: [
    {"key": "claude_code", "name": "Claude Code", "via": "local_logs", "covered": True, "plan": "claude_sub", "plan_connected": True, "note": None},
    {"key": "lmstudio", "name": "LM Studio", "via": "local_models", "covered": True, "plan": None, "plan_connected": None, "note": None},
    {"key": "gemini_cli", "name": "Gemini CLI", "via": None, "covered": False, "plan": None, "plan_connected": None,
     "note": "Not supported yet."}]

cfg = cli.load_config(str(D / "config.yaml"))
con = cli.db_connect(cfg)
for aid in ("claude-plan", "chatgpt-plan"):
    acct = next(a for a in cfg["api_accounts"] if a["id"] == aid)
    url = cli.oauth_start(con, acct)["url"]
    state = cli.kv_get(con, f"oauth_pending:{aid}")["state"]
    cli.oauth_finish(con, acct, f"CODE#{state}" if aid == "claude-plan" else f"http://localhost:1455/auth/callback?code=c&state={state}")
# DeepSeek balance history, so spend shows from balance drops
for i, bal in enumerate([14.90, 13.75, 12.10, 11.30, 10.05, 9.20]):
    ts = cli.iso(NOW - dt.timedelta(days=5 - i, hours=2))
    con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                (ts, "deepseek", json.dumps({"balance": bal, "balance_currency": "USD"})))
con.commit()
cli.main(["-c", str(D / "config.yaml"), "collect", "--force", "--no-alerts"])
# Earlier readings of each limit, rising toward now, so trends and run-out forecasts show
for r in con.execute("SELECT account_id, ts, metrics FROM snapshots WHERE ok=1 AND ts>=?",
                     (cli.iso(NOW - dt.timedelta(minutes=10)),)).fetchall():
    m = json.loads(r["metrics"])
    if not m.get("windows"):
        continue
    for k in range(1, 13):
        old = json.loads(r["metrics"])
        for w in old["windows"]:
            if w.get("used_pct") is not None:
                w["used_pct"] = max(0.0, round(w["used_pct"] * (1 - 0.06 * k * 20 / 60), 1))
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (cli.iso(cli._ts(r["ts"]) - dt.timedelta(minutes=20 * k)), r["account_id"], json.dumps(old)))
con.commit()
print(f"\nDemo data in {D}\nOpen http://localhost:8800/?token=demo\n", flush=True)
cli.main(["-c", str(D / "config.yaml"), "serve", "--port", "8800", "--no-collect"])
