import sys, os, json, datetime as dt, shutil
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ai_meter import cli as aimon
import requests
D="/tmp/aimon-test"; shutil.rmtree(D, ignore_errors=True); os.makedirs(D)
cfg_txt=open(ROOT / "src/ai_meter/config.example.yaml").read().replace("  # - id: openai-limits\n  #   label: OpenAI API limits\n  #   provider: openai_limits\n  #   api_key_env: OPENAI_API_KEY\n  #   probe_model: gpt-5-nano               # any cheap chat model your key can use\n  #   interval: 600",
"  - id: openai-limits\n    label: OpenAI API limits\n    provider: openai_limits\n    api_key_env: OPENAI_API_KEY\n    probe_model: gpt-5-nano\n    interval: 600")
cfg_txt=cfg_txt.replace("state_db: /hermes/state.db","state_db: /tmp/aimon-test/hermes_state.db")
open(D+"/config.yaml","w").write(cfg_txt)
import sqlite3, random
random.seed(4)
hd=sqlite3.connect(D+"/hermes_state.db")
hd.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, model TEXT, started_at REAL, message_count INTEGER, api_call_count INTEGER,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
  billing_provider TEXT, estimated_cost_usd REAL, actual_cost_usd REAL)""")
_now=dt.datetime.now(dt.timezone.utc)
combos=[("claude-opus-5-5","anthropic",15e-6,75e-6),("claude-sonnet-5-5","anthropic",3e-6,15e-6),("deepseek-v4-pro","deepseek",0.4e-6,1.6e-6),
        ("kimi-k3","kimi-coding",0,0),("qwen3.7-max","opencode-go",1.2e-6,6e-6),("hermes-4-405b","nous",1e-6,3e-6),("openai/gpt-5","openrouter",1.25e-6,10e-6)]
n=0
for h_ in range(0, 24*20, 2):
    t=_now-dt.timedelta(hours=h_, minutes=random.randint(0,59))
    for m_,pv,ci,co in random.sample(combos,3):
        i_=random.randint(20000,400000); o_=random.randint(2000,40000); cr=random.randint(0,800000)
        n+=1; hd.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(f"s{n}","cli",m_,t.timestamp(),10,random.randint(3,30),i_,o_,cr,0,random.randint(0,5000),pv,i_*ci+o_*co+cr*ci*0.1,None))
hd.commit(); hd.close()
os.environ.update(KIMI_CODE_API_KEY="k", OPENCODE_GO_API_KEY="o", DEEPSEEK_API_KEY="d", NOUS_API_KEY="n", ANTHROPIC_ADMIN_KEY="x", OPENAI_ADMIN_KEY="x", OPENROUTER_API_KEY="x", ANTHROPIC_API_KEY="x", OPENAI_API_KEY="x")
now=dt.datetime.now(dt.timezone.utc); today=now.strftime("%Y-%m-%d")
class R:
    def __init__(s,d,code=200,h=None): s._d=d; s.status_code=code; s.ok=code<300; s.text=json.dumps(d); s.headers=h or {}
    def json(s): return s._d
def fake_get(url, headers=None, params=None, timeout=None):
    if "cost_report" in url:
        return R({"data":[{"starting_at":f"{now:%Y-%m}-{d:02d}T00:00:00Z","results":[{"amount":str(300+d*137%900),"model":"claude-opus-5-5"},{"amount":"150","model":"claude-haiku-5-5"}]} for d in range(1,now.day+1)],"has_more":False})
    if "usage_report" in url: return R({"data":[{"starting_at":f"{now:%Y-%m-%d}T00:00:00Z","results":[{"model":"claude-opus-5-5","uncached_input_tokens":812000,"output_tokens":95000}]}],"has_more":False})
    if "organization/costs" in url: return R({"data":[{"start_time":int(now.timestamp())-86400*i,"results":[{"amount":{"value":1.2+i%3},"line_item":"gpt-5, input"}]} for i in range(8)],"has_more":False})
    if "usage/completions" in url: return R({"data":[],"has_more":False})
    if url.endswith("/key"): return R({"data":{"limit":30,"limit_remaining":7.4,"usage":80,"usage_daily":1.2,"usage_monthly":22.6,"limit_reset":"monthly","rate_limit":{"requests":200,"interval":"10s"}}})
    if url.endswith("/credits"): return R({"data":{"total_credits":50,"total_usage":38.2}})
    if "oauth/usage" in url:
        return R({"five_hour":{"utilization":63.0,"resets_at":(now+dt.timedelta(hours=2,minutes=14)).isoformat()},
                  "seven_day":{"utilization":41.0,"resets_at":(now+dt.timedelta(days=3,hours=5)).isoformat()},
                  "seven_day_opus":{"utilization":92.0,"resets_at":(now+dt.timedelta(days=3,hours=5)).isoformat()},"seven_day_sonnet":None})
    if "wham/usage" in url:
        return R({"plan_type":"plus","rate_limit":{"primary_window":{"used_percent":18,"limit_window_seconds":18000,"reset_after_seconds":9000},
                  "secondary_window":{"used_percent":77,"limit_window_seconds":604800,"reset_at":int(now.timestamp())+86400*4}}})
    if "kimi.com/coding/v1/usages" in url:
        return R({"usage":{"limit":"100","remaining":"38","resetTime":(now+dt.timedelta(days=2,hours=6)).isoformat()},
                  "limits":[{"window":{"duration":300,"timeUnit":"TIME_UNIT_MINUTE"},"detail":{"limit":"100","remaining":"71","resetTime":(now+dt.timedelta(hours=3,minutes=40)).isoformat()}}]})
    if "zen/go/v1/usage" in url:
        return R({"usage":{"rolling":{"status":"ok","percent":34,"resetsAt":(now+dt.timedelta(hours=1,minutes=5)).isoformat()},
                  "weekly":{"status":"ok","percent":58,"resetsAt":(now+dt.timedelta(days=5)).isoformat()},
                  "monthly":{"status":"ok","percent":22,"resetsAt":(now+dt.timedelta(days=19)).isoformat()}}})
    if "deepseek.com/user/balance" in url:
        return R({"is_available":True,"balance_infos":[{"currency":"USD","total_balance":"8.42"}]})
    raise AssertionError(url)
def fake_post(url, json=None, data=None, timeout=None, headers=None):
    if "v1/messages" in url:
        return R({}, 200, {"anthropic-ratelimit-requests-limit":"50","anthropic-ratelimit-requests-remaining":"49","anthropic-ratelimit-requests-reset":(now+dt.timedelta(seconds=40)).isoformat(),
          "anthropic-ratelimit-input-tokens-limit":"30000","anthropic-ratelimit-input-tokens-remaining":"21000","anthropic-ratelimit-input-tokens-reset":(now+dt.timedelta(seconds=25)).isoformat(),
          "anthropic-ratelimit-output-tokens-limit":"8000","anthropic-ratelimit-output-tokens-remaining":"7900","anthropic-ratelimit-output-tokens-reset":(now+dt.timedelta(seconds=12)).isoformat()})
    if "chat/completions" in url and "nousresearch" in url:
        return R({}, 200, {})
    if "chat/completions" in url:
        return R({}, 200, {"x-ratelimit-limit-requests":"500","x-ratelimit-remaining-requests":"499","x-ratelimit-reset-requests":"120ms","x-ratelimit-limit-tokens":"200000","x-ratelimit-remaining-tokens":"150000","x-ratelimit-reset-tokens":"15s"})
    if "oauth/token" in url:
        return R({"access_token":"tokA","refresh_token":"refB","expires_in":3600,"account":{"email_address":"me@example.com"}})
    return R({})
requests.get=fake_get; requests.post=fake_post
cfgp=D+"/config.yaml"
# connect claude via oauth flow
cfg=aimon.load_config(cfgp); con=aimon.db_connect(cfg)
acct=[a for a in cfg["api_accounts"] if a["id"]=="claude-plan"][0]
st=aimon.oauth_start(con,acct); print("auth url ok:", st["url"][:60])
state=aimon.kv_get(con,"oauth_pending:claude-plan")["state"]
print("finish:", aimon.oauth_finish(con,acct,"CODE123#"+state)["email"])
# seed history: insert fake past snapshots for claude
for i in range(48,0,-1):
    ts=aimon.iso(now-dt.timedelta(minutes=30*i))
    m={"windows":[aimon.window("five_hour","5-hour session",(i*7)%70),aimon.window("seven_day","Weekly (all models)",20+ (48-i)*0.4),aimon.window("seven_day_opus","Weekly Opus",50+(48-i)*0.9)]}
    con.execute("INSERT INTO snapshots(ts,account_id,ok,metrics) VALUES (?,?,1,?)",(ts,"claude-plan",json.dumps(m)))
con.commit()
aimon.main(["-c",cfgp,"collect","--force"])
# codex not connected? -> leave chatgpt-plan not connected to show that state: it was collected via oauth_token -> NotConnected
s=aimon.build_state(aimon.load_config(cfgp))
for a in s["apis"]:
    print(a["id"], a["kind"], "conn",a["connected"], "err",(a["error"] or "")[:50], [ (w["label"],w["used_pct"],w["resets_at"]) for w in a["metrics"].get("windows",[])][:3])
print("alerts:", [x["message"] for x in s["alerts"]][:6])
# test refresh path
con.execute("UPDATE kv SET value=json_set(value,'$.expires_at',0) WHERE key='oauth:claude-plan'"); con.commit()
print("refresh ->", aimon.oauth_token(con,acct)["access_token"])
print("dur", aimon._dur_seconds("6m0s"), aimon._dur_seconds("120ms"), aimon._dur_seconds("1h2m3.5s"))
