"""The dashboard server over real HTTP, the way the page calls it: auth, page encoding, connect, sign-in, remove."""
import argparse
import base64
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import Resp, cli

TOKEN = "s3cret-token"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(cfg, http, monkeypatch):
    monkeypatch.setenv("AIMETER_TEST_TOKEN", TOKEN)
    cfg["settings"]["dashboard"] = {"token_env": "AIMETER_TEST_TOKEN"}
    cfg["api_accounts"].append({"id": "cfg-deepseek", "label": "From config", "provider": "deepseek",
                                "api_key_env": "CFG_DEEPSEEK_KEY"})
    cfg["subscriptions"].append({"id": "sub-1", "name": "Some plan", "cost": 20, "currency": "USD", "cycle": "monthly"})
    port = free_port()
    args = argparse.Namespace(host="127.0.0.1", port=port, no_collect=True, config=None)
    threading.Thread(target=cli.cmd_serve, args=(cfg, args), daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    return base


def call(base, path, body=None, auth="query", raw=None):
    url = base + path
    headers = {}
    if auth == "query":
        url += ("&" if "?" in url else "?") + "token=" + TOKEN
    elif auth == "bearer":
        headers["Authorization"] = "Bearer " + TOKEN
    elif auth == "basic":
        headers["Authorization"] = "Basic " + base64.b64encode(b"any:" + TOKEN.encode()).decode()
    elif auth == "wrong":
        url += "?token=nope"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def js(resp):
    return json.loads(resp[2] or b"null")


def deepseek_ok(http, balance="8.42"):
    http.on("GET", "api.deepseek.com/user/balance",
            Resp({"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": balance}]}))


# ---------------------------------------------------------------- access

class TestAccess:
    def test_health_check_needs_no_token(self, server):
        assert call(server, "/healthz", auth=None)[0] == 200

    @pytest.mark.parametrize("path", ["/", "/api/state"])
    def test_page_and_state_need_the_token(self, server, path):
        code, headers, _ = call(server, path, auth=None)
        assert code == 401 and "Basic" in headers.get("WWW-Authenticate", "")
        assert call(server, path, auth="wrong")[0] == 401

    @pytest.mark.parametrize("auth", ["query", "bearer", "basic"])
    def test_every_token_style_works(self, server, auth):
        assert call(server, "/api/state", auth=auth)[0] == 200

    def test_post_needs_the_token(self, server, http):
        assert call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "k"}, auth=None)[0] == 401
        assert not http.calls

    def test_unknown_paths_are_404(self, server):
        assert call(server, "/nope")[0] == 404
        assert call(server, "/api/nope", {})[0] == 404


# ---------------------------------------------------------------- the page itself

class TestPage:
    def test_served_as_utf8_without_garbled_characters(self, server):
        code, headers, raw = call(server, "/")
        assert code == 200 and "charset=utf-8" in headers["Content-Type"]
        page = raw.decode("utf-8")
        assert page == (cli.HERE / "dashboard.html").read_text(encoding="utf-8")
        assert "–" in page and "…" in page  # en dash and ellipsis survive
        for garbled in ("â€", "Ã"):  # what cp1252 decoding turned them into
            assert garbled not in page

    def test_security_headers(self, server):
        _, headers, _ = call(server, "/")
        assert headers["X-Content-Type-Options"] == "nosniff" and headers["Cache-Control"] == "no-store"

    def test_state_has_what_the_page_reads(self, server):
        st = js(call(server, "/api/state"))
        for key in ("apis", "subscriptions", "alerts", "catalog", "totals"):
            assert key in st
        assert set(st["catalog"]) == set(cli.CATALOG)


# ---------------------------------------------------------------- connect a provider

class TestConnect:
    def test_success_checks_right_away(self, server, http):
        deepseek_ok(http)
        r = js(call(server, "/api/providers/add", {"provider": "deepseek", "label": "DeepSeek (work)", "api_key": "sk-1"}))
        assert r == {"ok": True, "id": "deepseek-work", "checked": True, "error": None}
        assert http.gets("deepseek")[0][2]["headers"]["Authorization"] == "Bearer sk-1"
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == "deepseek-work")
        assert card["metrics"]["balance"] == 8.42 and card["removable"]

    def test_wrong_key_is_added_but_reports_the_failed_check(self, server, http):
        http.on("GET", "api.deepseek.com", Resp({"error": "Authentication Fails"}, status=401))
        code, _, raw = call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "bad"})
        r = json.loads(raw)
        assert code == 200 and r["ok"] and r["checked"] is False and "HTTP 401" in r["error"]
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == r["id"])
        assert card["error"]

    def test_provider_down_reports_the_failed_check(self, server, http):
        http.on("GET", "api.deepseek.com", cli.requests.ConnectionError("timed out"))
        r = js(call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "k"}))
        assert r["checked"] is False and "timed out" in r["error"]

    def test_missing_key_is_a_400_with_a_message(self, server, http):
        code, _, raw = call(server, "/api/providers/add", {"provider": "deepseek", "api_key": ""})
        assert code == 400 and json.loads(raw)["error"] == "Paste the API key first."
        assert not http.calls

    def test_unknown_provider_is_a_400(self, server):
        code, _, raw = call(server, "/api/providers/add", {"provider": "made-up"})
        assert code == 400 and "Unknown provider" in json.loads(raw)["error"]

    def test_bad_number_is_a_400(self, server):
        code, _, raw = call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "k", "low_balance": "lots"})
        assert code == 400 and "must be a number" in json.loads(raw)["error"]

    @pytest.mark.parametrize("raw", [b"{not json", b"[1, 2]", b'"text"', b"null"])
    def test_bad_json_is_a_400(self, server, raw):
        for path in ("/api/providers/add", "/api/log", "/api/oauth/start", "/api/collect"):
            assert call(server, path, raw=raw)[0] == 400

    @pytest.mark.parametrize("prov", ["claude_sub", "codex_sub"])
    def test_signin_providers_go_to_the_signin_step_without_a_check(self, server, http, prov):
        r = js(call(server, "/api/providers/add", {"provider": prov}))
        assert r["ok"] and r["signin"] is True
        assert not http.calls
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == r["id"])
        assert card["oauth"] and card["connected"] is False

    def test_key_never_comes_back_to_the_browser(self, server, http):
        deepseek_ok(http)
        call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "sk-NEVER-SHOWN"})
        assert b"sk-NEVER-SHOWN" not in call(server, "/api/state")[2]


# ---------------------------------------------------------------- sign in

class TestSignIn:
    def add_claude(self, server):
        return js(call(server, "/api/providers/add", {"provider": "claude_sub", "label": "My Claude"}))["id"]

    def test_full_claude_signin(self, server, http):
        aid = self.add_claude(server)
        start = js(call(server, "/api/oauth/start", {"id": aid}))
        assert start["url"].startswith("https://claude.com/cai/oauth/authorize?code=true&")
        state = start["url"].split("state=")[1]
        http.on("POST", "oauth/token", Resp({"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                             "account": {"email_address": "me@example.com"}}))
        http.on("GET", "api/oauth/usage", Resp({"five_hour": {"utilization": 12, "resets_at": "2030-01-01T00:00:00Z"}}))
        r = js(call(server, "/api/oauth/finish", {"id": aid, "code": f"CODE#{state}"}))
        assert r == {"ok": True, "account": "me@example.com"}
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == aid)
        assert card["connected"] and card["metrics"]["windows"][0]["used_pct"] == 12

    def test_wrong_code_shows_the_reason(self, server, http):
        aid = self.add_claude(server)
        call(server, "/api/oauth/start", {"id": aid})
        code, _, raw = call(server, "/api/oauth/finish", {"id": aid, "code": "CODE#stale"})
        assert code == 400 and "older login attempt" in json.loads(raw)["error"]

    def test_rejected_by_claude_shows_the_reason(self, server, http):
        aid = self.add_claude(server)
        state = js(call(server, "/api/oauth/start", {"id": aid}))["url"].split("state=")[1]
        http.on("POST", "oauth/token", Resp({"error": "invalid_grant"}, status=400))
        code, _, raw = call(server, "/api/oauth/finish", {"id": aid, "code": f"CODE#{state}"})
        assert code == 400 and "token request failed" in json.loads(raw)["error"]

    def test_finish_before_start(self, server):
        aid = self.add_claude(server)
        code, _, raw = call(server, "/api/oauth/finish", {"id": aid, "code": "x#y"})
        assert code == 400 and "No login in progress" in json.loads(raw)["error"]

    @pytest.mark.parametrize("aid", ["does-not-exist", "cfg-deepseek", None])
    def test_signin_only_for_signin_accounts(self, server, aid):
        for step in ("start", "finish", "disconnect"):
            code, _, raw = call(server, f"/api/oauth/{step}", {"id": aid})
            assert code == 400 and json.loads(raw)["error"] == "unknown account"

    def test_disconnect_forgets_the_login(self, server, http):
        aid = self.add_claude(server)
        state = js(call(server, "/api/oauth/start", {"id": aid}))["url"].split("state=")[1]
        http.on("POST", "oauth/token", Resp({"access_token": "a"}))
        call(server, "/api/oauth/finish", {"id": aid, "code": f"C#{state}"})
        assert js(call(server, "/api/oauth/disconnect", {"id": aid})) == {"ok": True}
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == aid)
        assert card["connected"] is False

    def test_unknown_oauth_step_is_404(self, server):
        aid = self.add_claude(server)
        assert call(server, "/api/oauth/other", {"id": aid})[0] == 404


# ---------------------------------------------------------------- remove, check, log

class TestOtherActions:
    def test_remove_added_provider(self, server, http):
        deepseek_ok(http)
        aid = js(call(server, "/api/providers/add", {"provider": "deepseek", "api_key": "k"}))["id"]
        assert js(call(server, "/api/providers/remove", {"id": aid})) == {"ok": True}
        assert all(a["id"] != aid for a in js(call(server, "/api/state"))["apis"])

    @pytest.mark.parametrize("aid", ["cfg-deepseek", "does-not-exist"])
    def test_cannot_remove_config_or_unknown_accounts(self, server, aid):
        code, _, raw = call(server, "/api/providers/remove", {"id": aid})
        assert code == 400 and "config.yaml" in json.loads(raw)["error"]

    def test_check_now_runs_only_that_provider(self, server, http, monkeypatch):
        monkeypatch.setenv("CFG_DEEPSEEK_KEY", "cfgkey")
        deepseek_ok(http, balance="3")
        assert js(call(server, "/api/collect", {"only": ["cfg-deepseek"]})) == {"ok": True}
        assert len(http.gets("deepseek")) == 1
        card = next(a for a in js(call(server, "/api/state"))["apis"] if a["id"] == "cfg-deepseek")
        assert card["metrics"]["balance"] == 3

    def test_manual_reading_saved(self, server):
        assert js(call(server, "/api/log", {"id": "sub-1", "value": 40, "unit": "%"})) == {"ok": True}
        sub = next(s for s in js(call(server, "/api/state"))["subscriptions"] if s["id"] == "sub-1")
        assert sub["manual"]["value"] == 40

    def test_manual_reading_for_unknown_id_is_rejected(self, server):
        code, _, raw = call(server, "/api/log", {"id": "nope", "value": 1})
        assert code == 400 and json.loads(raw) == {"ok": False}

    @pytest.mark.parametrize("value", ["abc", None, [], {}])
    def test_manual_reading_with_bad_value_is_a_400(self, server, value):
        code, _, raw = call(server, "/api/log", {"id": "sub-1", "value": value})
        assert code == 400 and "must be numbers" in json.loads(raw)["error"]

    def test_manual_reading_with_bad_limit_is_a_400(self, server):
        code, _, raw = call(server, "/api/log", {"id": "sub-1", "value": 5, "limit": "lots"})
        assert code == 400 and "must be numbers" in json.loads(raw)["error"]

    def test_manual_reading_count_with_limit(self, server):
        assert js(call(server, "/api/log", {"id": "sub-1", "value": "12", "unit": "", "limit": "50"})) == {"ok": True}
        sub = next(s for s in js(call(server, "/api/state"))["subscriptions"] if s["id"] == "sub-1")
        assert (sub["manual"]["value"], sub["manual"]["lim"]) == (12, 50)
