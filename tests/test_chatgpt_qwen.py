"""ChatGPT limits from Codex logs, the automatic ChatGPT sign-in on localhost:1455, and Qwen Code tokens."""
import argparse
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import Resp, cli
from test_local_logs import TS, NOW, write_jsonl

CODEX = {"id": "chatgpt-plan", "label": "ChatGPT", "provider": "codex_sub"}


@pytest.fixture(autouse=True)
def clean():
    cli._LOG_CACHE.clear()
    cli.stop_codex_callback(wait=True)
    yield
    cli.stop_codex_callback(wait=True)
    cli._LOG_CACHE.clear()


def collect(tmp_path):
    return cli.collect_local_logs({"id": "logs", "claude_dir": str(tmp_path / "claude"),
                                   "codex_dir": str(tmp_path / "codex"), "qwen_dir": str(tmp_path / "qwen")})


# ---------------------------------------------------------------- ChatGPT limits from Codex logs

def limits_event(ts, primary_pct=40.0, secondary_pct=70.0, p_reset=None, s_reset=None, limit_id="codex", plan="plus"):
    now = time.time()
    return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": None, "rate_limits": {
        "limit_id": limit_id, "plan_type": plan, "credits": {"has_credits": False, "balance": "0"},
        "primary": {"used_percent": primary_pct, "window_minutes": 300, "resets_at": p_reset or int(now + 3600)},
        "secondary": {"used_percent": secondary_pct, "window_minutes": 10080, "resets_at": s_reset or int(now + 86400)}}}}


def iso_ago(seconds):
    return cli.iso(NOW - cli.dt.timedelta(seconds=seconds)).replace("Z", ".000Z")


class TestCodexLimits:
    def test_latest_limits_are_read(self, tmp_path):
        write_jsonl(tmp_path / "codex/a.jsonl", [limits_event(iso_ago(600), 10, 20), limits_event(iso_ago(60), 40, 70)])
        lim = collect(tmp_path)["codex_limits"]
        assert lim["plan"] == "plus" and lim["rate_limit"]["primary_window"]["used_percent"] == 40
        assert lim["rate_limit"]["primary_window"]["limit_window_seconds"] == 18000
        assert lim["rate_limit"]["secondary_window"]["limit_window_seconds"] == 604800

    def test_newest_file_wins(self, tmp_path):
        write_jsonl(tmp_path / "codex/old.jsonl", [limits_event(iso_ago(3000), 99, 99)])
        write_jsonl(tmp_path / "codex/new.jsonl", [limits_event(iso_ago(30), 5, 6)])
        assert collect(tmp_path)["codex_limits"]["rate_limit"]["primary_window"]["used_percent"] == 5

    def test_other_limit_ids_do_not_replace_the_plan_limits(self, tmp_path):
        write_jsonl(tmp_path / "codex/a.jsonl", [limits_event(iso_ago(60), 40, 70),
                                                  limits_event(iso_ago(30), 1, 1, limit_id="some_model")])
        assert collect(tmp_path)["codex_limits"]["rate_limit"]["primary_window"]["used_percent"] == 40

    def test_no_limits_in_logs(self, tmp_path):
        write_jsonl(tmp_path / "codex/a.jsonl", [{"type": "session_meta", "timestamp": TS, "payload": {}}])
        assert "codex_limits" not in collect(tmp_path)

    def test_window_past_its_reset_shows_as_refilled(self):
        lim = cli._codex_limits_from({"primary": {"used_percent": 80, "window_minutes": 300, "resets_at": int(time.time()) - 60},
                                      "secondary": {"used_percent": 30, "window_minutes": 10080, "resets_at": int(time.time()) + 999}},
                                     TS)
        p, s = cli.codex_windows_from_logs(lim)
        assert (p["used_pct"], p["resets_at"]) == (0.0, None)
        assert s["used_pct"] == 30 and s["resets_at"]

    def test_bad_timestamp_is_ignored(self):
        assert cli._codex_limits_from({"primary": {"used_percent": 1, "window_minutes": 300}}, "not a time") is None


def add_logs_snapshot(con, lim_ts, primary=40):
    lim = cli._codex_limits_from({"plan_type": "plus", "primary": {"used_percent": primary, "window_minutes": 300,
                                                                   "resets_at": int(time.time()) + 3600}}, lim_ts)
    con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                (cli.iso(NOW), "logs", json.dumps({"codex_limits": lim, "token_usage": None})))
    con.commit()


class TestChatGPTCard:
    def setup_accounts(self, cfg):
        cfg["api_accounts"] += [{"id": "logs", "provider": "local_logs"}, dict(CODEX)]

    def card(self, cfg):
        return next(a for a in cli.build_state(cfg)["apis"] if a["id"] == "chatgpt-plan")

    def test_not_signed_in_uses_the_logs(self, cfg, con):
        self.setup_accounts(cfg)
        add_logs_snapshot(con, iso_ago(120))
        c = self.card(cfg)
        assert c["limits_from_logs"] and c["metrics"]["windows"][0]["used_pct"] == 40
        assert c["metrics"]["plan"] == "plus" and c["error"] is None and c["connected"] is False

    def test_newer_live_reading_wins(self, cfg, con):
        self.setup_accounts(cfg)
        add_logs_snapshot(con, iso_ago(3600))
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (cli.iso(NOW), "chatgpt-plan", json.dumps({"windows": [cli.window("p", "5-hour", 12)]})))
        con.commit()
        c = self.card(cfg)
        assert "limits_from_logs" not in c and c["metrics"]["windows"][0]["used_pct"] == 12

    def test_newer_logs_win_over_an_old_live_reading(self, cfg, con):
        self.setup_accounts(cfg)
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (iso_ago(7200)[:19] + "Z", "chatgpt-plan", json.dumps({"windows": [cli.window("p", "5-hour", 12)]})))
        con.commit()
        add_logs_snapshot(con, iso_ago(60), primary=55)
        assert self.card(cfg)["metrics"]["windows"][0]["used_pct"] == 55

    def test_failed_live_check_falls_back_to_the_logs(self, cfg, con):
        self.setup_accounts(cfg)
        add_logs_snapshot(con, iso_ago(600))
        con.execute("INSERT INTO snapshots(ts, account_id, ok, error) VALUES (?,?,0,?)",
                    (cli.iso(NOW), "chatgpt-plan", "NOT_CONNECTED: Not connected yet."))
        con.commit()
        c = self.card(cfg)
        assert c["limits_from_logs"] and c["error"] is None and not c["not_connected"]

    def test_no_logs_source_leaves_the_card_alone(self, cfg, con):
        cfg["api_accounts"].append(dict(CODEX))
        c = self.card(cfg)
        assert "limits_from_logs" not in c and not c["metrics"].get("windows")


# ---------------------------------------------------------------- automatic sign-in on localhost:1455

def port_free(port):
    """True when nothing is listening (closed connections waiting in TIME_WAIT don't count)."""
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def get(path, host="localhost"):
    req = urllib.request.Request(f"http://127.0.0.1:{cli.CODEX_CALLBACK[1]}{path}", headers={"Host": f"{host}:1455"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def callback(cfg, con, http, monkeypatch):
    if not port_free(cli.CODEX_CALLBACK[1]):
        pytest.skip("port 1455 is in use on this computer")
    cfg["api_accounts"].append(dict(CODEX))
    res = cli.oauth_start(con, CODEX)
    state = res["url"].split("state=")[1].split("&")[0]
    assert cli.start_codex_callback(cfg, CODEX) is True
    return state


class TestAutoSignin:
    def test_redirect_finishes_the_signin_and_stops_listening(self, callback, con, http):
        http.on("POST", "auth.openai.com/oauth/token", Resp({"access_token": "a", "refresh_token": "r", "expires_in": 3600}))
        http.on("GET", "wham/usage", Resp({"rate_limit": {"primary_window": {"used_percent": 9, "limit_window_seconds": 18000}}}))
        code, page = get(f"/auth/callback?code=ac.1.XYZ&scope=openid&state={callback}")
        assert code == 200 and "Signed in to AI Meter" in page
        assert cli.kv_get(con, "oauth:chatgpt-plan")["access_token"] == "a"
        assert http.gets("wham/usage"), "first check runs right away"
        time.sleep(0.5)
        assert port_free(cli.CODEX_CALLBACK[1])

    def test_out_of_date_state_is_refused_and_keeps_listening(self, callback, con, http):
        code, page = get("/auth/callback?code=c&state=forged")
        assert code == 400 and "out of date" in page
        assert not http.posts() and cli.kv_get(con, "oauth:chatgpt-plan") is None
        http.on("POST", "oauth/token", Resp({"access_token": "a"}))
        assert get(f"/auth/callback?code=c&state={callback}")[0] == 200

    def test_cancelled_signin_shows_the_reason(self, callback):
        code, page = get("/auth/callback?error=access_denied&error_description=User%20cancelled%20%3Cb%3E")
        assert code == 400 and "User cancelled" in page and "<b>" not in page  # escaped

    def test_token_failure_shows_a_clean_message(self, callback, http):
        http.on("POST", "oauth/token", Resp({"error": "invalid_grant", "leak": "Bearer sk-abcdef123456789"}, status=400))
        code, page = get(f"/auth/callback?code=c&state={callback}")
        assert code == 400 and "Sign-in failed" in page and "sk-abcdef123456789" not in page

    @pytest.mark.parametrize("path", ["/", "/auth/other", "/api/state", "/auth/callback/../../x"])
    def test_other_paths_are_404(self, callback, path):
        assert get(path)[0] == 404

    def test_other_host_names_are_refused(self, callback, http):
        http.on("POST", "oauth/token", Resp({"access_token": "a"}))
        assert get(f"/auth/callback?code=c&state={callback}", host="evil.example")[0] == 404
        assert not http.posts()

    def test_listens_on_this_computer_only(self, callback):
        assert cli._callback["server"].server_address[0] == "127.0.0.1"

    def test_busy_port_falls_back_to_pasting(self, cfg, con, http):
        if not port_free(cli.CODEX_CALLBACK[1]):
            pytest.skip("port 1455 is in use on this computer")
        blocker = socket.socket()
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: really hold the port, like another app would
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(cli.CODEX_CALLBACK)
        blocker.listen(1)
        try:
            assert cli.start_codex_callback(cfg, CODEX) is False
        finally:
            blocker.close()

    def test_starting_again_replaces_the_old_listener(self, callback, cfg):
        first = cli._callback["server"]
        assert cli.start_codex_callback(cfg, CODEX) is True
        assert cli._callback["server"] is not first

    def test_gives_up_after_the_timeout(self, cfg, con, http, monkeypatch):
        if not port_free(cli.CODEX_CALLBACK[1]):
            pytest.skip("port 1455 is in use on this computer")
        monkeypatch.setattr(cli, "CALLBACK_TIMEOUT", 0.3)
        assert cli.start_codex_callback(cfg, CODEX) is True
        time.sleep(1.0)
        assert cli._callback["server"] is None and port_free(cli.CODEX_CALLBACK[1])


def test_start_endpoint_reports_auto_and_paste_still_works(cfg, http, monkeypatch):
    if not port_free(cli.CODEX_CALLBACK[1]):
        pytest.skip("port 1455 is in use on this computer")
    from test_server import free_port
    cfg["settings"]["dashboard"] = {}
    port = free_port()
    threading.Thread(target=cli.cmd_serve, daemon=True,
                     args=(cfg, argparse.Namespace(host="127.0.0.1", port=port, no_collect=True, config=None))).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.05)

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    aid = post("/api/providers/add", {"provider": "codex_sub"})["id"]
    start = post("/api/oauth/start", {"id": aid})
    assert start["auto"] is True
    state = start["url"].split("state=")[1].split("&")[0]
    http.on("POST", "oauth/token", Resp({"access_token": "a"}))
    post("/api/oauth/finish", {"id": aid, "code": f"http://localhost:1455/auth/callback?code=c&state={state}"})
    time.sleep(0.5)
    assert port_free(cli.CODEX_CALLBACK[1]), "pasting stops the listener"
    st = json.loads(urllib.request.urlopen(base + "/api/state", timeout=10).read())
    assert next(a for a in st["apis"] if a["id"] == aid)["signed_in_at"]


# ---------------------------------------------------------------- Qwen Code

def qwen_reply(uid, sid="q1", model="qwen3-coder-plus", prompt=1000, cached=400, cand=200, thoughts=50, ts=TS):
    return {"type": "assistant", "uuid": uid, "sessionId": sid, "timestamp": ts, "model": model,
            "message": {"role": "model", "parts": [{"text": "private text"}]},
            "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": cand, "thoughtsTokenCount": thoughts,
                              "cachedContentTokenCount": cached, "totalTokenCount": prompt + cand + thoughts}}


class TestQwen:
    def test_tokens_with_cache_and_thoughts(self, tmp_path):
        write_jsonl(tmp_path / "qwen/proj/chats/a.jsonl", [{"type": "user", "uuid": "u0"}, qwen_reply("u1")], junk=True)
        m = collect(tmp_path)
        t = m["token_usage"]["totals"]
        assert (t["input"], t["output"], t["cache"], t["reasoning"], t["requests"]) == (600, 250, 400, 50, 1)
        assert m["token_usage"]["by_provider"] == [{"provider": "qwen", "tokens": 1250, "cost": 0.0}]
        assert m["logs"]["qwen_code"]["files"] == 1

    def test_same_reply_in_two_files_counted_once(self, tmp_path):
        write_jsonl(tmp_path / "qwen/p1/chats/a.jsonl", [qwen_reply("u1")])
        write_jsonl(tmp_path / "qwen/p2/chats/b.jsonl", [qwen_reply("u1")])
        assert collect(tmp_path)["token_usage"]["totals"]["requests"] == 1

    def test_cached_larger_than_prompt_is_capped(self, tmp_path):
        write_jsonl(tmp_path / "qwen/p/chats/a.jsonl", [qwen_reply("u1", prompt=100, cached=500)])
        t = collect(tmp_path)["token_usage"]["totals"]
        assert (t["input"], t["cache"]) == (0, 100)

    def test_other_models_through_qwen_code_go_to_their_provider(self, tmp_path):
        write_jsonl(tmp_path / "qwen/p/chats/a.jsonl", [qwen_reply("u1", model="deepseek-v4-pro")])
        assert collect(tmp_path)["token_usage"]["by_provider"][0]["provider"] == "deepseek"

    def test_qwen_data_dir_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("QWEN_DATA_DIR", f"{tmp_path / 'custom'},{tmp_path / 'other'}")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "none1"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "none2"))
        write_jsonl(tmp_path / "custom/p/chats/a.jsonl", [qwen_reply("u1")])
        assert cli.collect_local_logs({"id": "x"})["logs"]["qwen_code"]["files"] == 1

    def test_message_text_never_leaves_the_collector(self, tmp_path):
        write_jsonl(tmp_path / "qwen/p/chats/a.jsonl", [qwen_reply("u1")])
        assert "private text" not in json.dumps(collect(tmp_path))

    def test_qwen_shows_as_its_own_row_in_totals(self, tmp_path):
        write_jsonl(tmp_path / "qwen/p/chats/a.jsonl", [qwen_reply("u1")])
        m = collect(tmp_path)
        t = cli.token_total([{"id": "logs", "label": "Logs", "provider": "local_logs", "metrics": m}])
        assert [(r["name"], r["tokens"]) for r in t["by_provider"]] == [("Qwen", 1250)]
