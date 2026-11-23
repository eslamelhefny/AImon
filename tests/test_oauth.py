"""Sign-in (OAuth PKCE, paste-back) for Claude and ChatGPT: every branch of start, finish, refresh and use."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import Resp, cli

CLAUDE = {"id": "claude-plan", "label": "Claude", "provider": "claude_sub"}
CODEX = {"id": "chatgpt-plan", "label": "ChatGPT", "provider": "codex_sub"}

# What Claude Code 2.1.325 sends (read from its binary on 2026-11-19). If Claude Code changes,
# update OAUTH["claude_sub"] and these constants together.
CLAUDE_CODE_PARAM_ORDER = ["code", "client_id", "response_type", "redirect_uri", "scope",
                           "code_challenge", "code_challenge_method", "state"]
CLAUDE_CODE_SCOPES = ["org:create_api_key", "user:profile", "user:inference", "user:sessions:claude_code",
                      "user:mcp_servers", "user:file_upload", "user:plugins", "user:privacy_consents:write"]


def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def jwt(claims):
    return "h." + b64url(json.dumps(claims).encode()) + ".s"


def start(con, acct):
    res = cli.oauth_start(con, acct)
    u = urlparse(res["url"])
    q = parse_qs(u.query, keep_blank_values=True)
    return res, u, {k: v[0] for k, v in q.items()}, [p.split("=", 1)[0] for p in u.query.split("&")]


# ---------------------------------------------------------------- oauth_start

class TestStartClaude:
    def test_uses_claude_code_authorize_page(self, con):
        _, u, _, _ = start(con, CLAUDE)
        assert f"{u.scheme}://{u.netloc}{u.path}" == "https://claude.com/cai/oauth/authorize"

    def test_parameter_order_matches_claude_code(self, con):
        _, _, _, order = start(con, CLAUDE)
        assert order == CLAUDE_CODE_PARAM_ORDER

    def test_fixed_values(self, con):
        _, _, q, _ = start(con, CLAUDE)
        assert q["code"] == "true"
        assert q["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
        assert q["response_type"] == "code"
        assert q["redirect_uri"] == "https://platform.claude.com/oauth/code/callback"
        assert q["code_challenge_method"] == "S256"

    def test_scopes_match_claude_code(self, con):
        _, _, q, _ = start(con, CLAUDE)
        assert q["scope"].split(" ") == CLAUDE_CODE_SCOPES

    def test_state_is_43_chars_like_claude_code(self, con):
        # The 22-char state AI Meter used before was rejected with "Invalid request format".
        _, _, q, _ = start(con, CLAUDE)
        assert len(q["state"]) == 43
        assert set(q["state"]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def test_pkce_challenge_is_sha256_of_saved_verifier(self, con):
        _, _, q, _ = start(con, CLAUDE)
        pend = cli.kv_get(con, "oauth_pending:claude-plan")
        assert len(pend["verifier"]) == 43
        assert q["code_challenge"] == b64url(hashlib.sha256(pend["verifier"].encode()).digest())
        assert pend["state"] == q["state"]

    def test_each_start_is_fresh_and_replaces_the_pending_login(self, con):
        _, _, q1, _ = start(con, CLAUDE)
        _, _, q2, _ = start(con, CLAUDE)
        assert q1["state"] != q2["state"] and q1["code_challenge"] != q2["code_challenge"]
        assert cli.kv_get(con, "oauth_pending:claude-plan")["state"] == q2["state"]

    def test_hint_tells_user_what_to_paste(self, con):
        res, _, _, _ = start(con, CLAUDE)
        assert "code" in res["hint"].lower()

    def test_pending_logins_are_per_account(self, con):
        _, _, q1, _ = start(con, CLAUDE)
        _, _, q2, _ = start(con, dict(CLAUDE, id="claude-2"))
        assert cli.kv_get(con, "oauth_pending:claude-plan")["state"] == q1["state"]
        assert cli.kv_get(con, "oauth_pending:claude-2")["state"] == q2["state"]


class TestStartCodex:
    def test_authorize_page_and_values(self, con):
        _, u, q, _ = start(con, CODEX)
        assert f"{u.scheme}://{u.netloc}{u.path}" == "https://auth.openai.com/oauth/authorize"
        assert q["client_id"] == "app_EMoamEEZ73f0CkXaXp7hrann"
        assert q["redirect_uri"] == "http://localhost:1455/auth/callback"
        assert q["scope"] == "openid profile email offline_access"
        assert q["id_token_add_organizations"] == "true"
        assert q["codex_cli_simplified_flow"] == "true"
        assert "code" not in q  # code=true is Claude only

    def test_parameter_order_is_the_one_that_worked(self, con):
        _, _, _, order = start(con, CODEX)
        assert order == ["response_type", "client_id", "redirect_uri", "scope", "code_challenge",
                         "code_challenge_method", "state", "id_token_add_organizations", "codex_cli_simplified_flow"]

    def test_hint_explains_the_failing_localhost_page(self, con):
        res, _, _, _ = start(con, CODEX)
        assert "localhost:1455" in res["hint"] and "address" in res["hint"]


# ---------------------------------------------------------------- oauth_finish

def claude_token_ok(http, **extra):
    http.on("POST", "oauth/token", Resp({"access_token": "acc", "refresh_token": "ref", "expires_in": 3600,
                                         "account": {"email_address": "me@example.com"}, **extra}))


class TestFinishClaude:
    def test_code_hash_state_paste_succeeds(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        claude_token_ok(http)
        rec = cli.oauth_finish(con, CLAUDE, f"THECODE#{q['state']}")
        assert rec["access_token"] == "acc" and rec["refresh_token"] == "ref" and rec["email"] == "me@example.com"
        assert cli.kv_get(con, "oauth:claude-plan")["access_token"] == "acc"
        assert cli.kv_get(con, "oauth_pending:claude-plan") is None

    def test_token_request_body_is_json_with_state_and_verifier(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        verifier = cli.kv_get(con, "oauth_pending:claude-plan")["verifier"]
        claude_token_ok(http)
        cli.oauth_finish(con, CLAUDE, f"THECODE#{q['state']}")
        method, url, kw = http.posts("oauth/token")[0]
        assert url == "https://platform.claude.com/v1/oauth/token"
        assert "json" in kw and "data" not in kw
        assert kw["json"] == {"grant_type": "authorization_code", "code": "THECODE",
                              "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
                              "redirect_uri": "https://platform.claude.com/oauth/code/callback",
                              "code_verifier": verifier, "state": q["state"]}

    def test_bare_code_without_state_uses_pending_state(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        claude_token_ok(http)
        cli.oauth_finish(con, CLAUDE, "THECODE")
        assert http.posts("oauth/token")[0][2]["json"]["state"] == q["state"]

    def test_spaces_and_newlines_around_paste_are_ignored(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        claude_token_ok(http)
        cli.oauth_finish(con, CLAUDE, f"  \n THECODE#{q['state']} \n")
        assert http.posts("oauth/token")[0][2]["json"]["code"] == "THECODE"

    def test_state_from_older_attempt_is_rejected(self, con, http):
        _, _, old, _ = start(con, CLAUDE)
        start(con, CLAUDE)
        with pytest.raises(RuntimeError, match="older login attempt"):
            cli.oauth_finish(con, CLAUDE, f"THECODE#{old['state']}")
        assert not http.posts()

    def test_wrong_state_is_rejected(self, con, http):
        start(con, CLAUDE)
        with pytest.raises(RuntimeError, match="older login attempt"):
            cli.oauth_finish(con, CLAUDE, "THECODE#not-the-state")

    def test_empty_paste_is_rejected(self, con, http):
        start(con, CLAUDE)
        for pasted in ("", "   ", "#somestate"):
            with pytest.raises(RuntimeError, match="Could not find a code"):
                cli.oauth_finish(con, CLAUDE, pasted)
        assert not http.posts()

    def test_finish_without_start_is_rejected(self, con, http):
        with pytest.raises(RuntimeError, match="No login in progress"):
            cli.oauth_finish(con, CLAUDE, "THECODE#x")

    def test_finish_twice_is_rejected(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        claude_token_ok(http)
        cli.oauth_finish(con, CLAUDE, f"THECODE#{q['state']}")
        with pytest.raises(RuntimeError, match="No login in progress"):
            cli.oauth_finish(con, CLAUDE, f"THECODE#{q['state']}")

    def test_falls_back_to_second_token_url(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        http.on("POST", "platform.claude.com/v1/oauth/token", Resp({"error": "down"}, status=503))
        http.on("POST", "console.anthropic.com/v1/oauth/token", Resp({"access_token": "acc2", "expires_in": 60}))
        rec = cli.oauth_finish(con, CLAUDE, f"C#{q['state']}")
        assert rec["access_token"] == "acc2"
        assert [c[1] for c in http.posts()] == ["https://platform.claude.com/v1/oauth/token",
                                                  "https://console.anthropic.com/v1/oauth/token"]

    def test_all_token_urls_fail_reports_status_and_keeps_login_open(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        http.on("POST", "oauth/token", Resp({"error": "invalid_grant"}, status=400))
        with pytest.raises(RuntimeError, match=r"token request failed \(HTTP 400"):
            cli.oauth_finish(con, CLAUDE, f"C#{q['state']}")
        assert cli.kv_get(con, "oauth_pending:claude-plan") is not None  # user can paste again
        assert cli.kv_get(con, "oauth:claude-plan") is None

    def test_network_error_is_reported(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        http.on("POST", "oauth/token", cli.requests.ConnectionError("no route to host"))
        with pytest.raises(RuntimeError, match="no route to host"):
            cli.oauth_finish(con, CLAUDE, f"C#{q['state']}")

    def test_missing_expires_in_defaults_to_one_hour(self, con, http):
        _, _, q, _ = start(con, CLAUDE)
        http.on("POST", "oauth/token", Resp({"access_token": "a"}))
        rec = cli.oauth_finish(con, CLAUDE, f"C#{q['state']}")
        assert 3500 < rec["expires_at"] - time.time() <= 3600


class TestFinishCodex:
    def test_pasted_callback_url_succeeds_with_form_body(self, con, http):
        _, _, q, _ = start(con, CODEX)
        claims = {"email": "me@example.com",
                  "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1", "chatgpt_plan_type": "plus"}}
        http.on("POST", "auth.openai.com/oauth/token",
                Resp({"access_token": "a", "refresh_token": "r", "id_token": jwt(claims), "expires_in": 3600}))
        url = f"http://localhost:1455/auth/callback?code=ac.1.XYZ&scope=openid&state={q['state']}"
        rec = cli.oauth_finish(con, CODEX, url)
        assert (rec["account_id"], rec["plan"], rec["email"]) == ("acct-1", "plus", "me@example.com")
        kw = http.posts("oauth/token")[0][2]
        assert "data" in kw and "json" not in kw
        assert kw["data"]["code"] == "ac.1.XYZ" and "state" not in kw["data"]
        assert kw["data"]["redirect_uri"] == "http://localhost:1455/auth/callback"

    def test_url_without_code_is_rejected(self, con, http):
        _, _, q, _ = start(con, CODEX)
        with pytest.raises(RuntimeError, match="Could not find a code"):
            cli.oauth_finish(con, CODEX, f"http://localhost:1455/auth/callback?state={q['state']}")

    def test_url_with_error_instead_of_code_is_rejected(self, con, http):
        _, _, q, _ = start(con, CODEX)
        with pytest.raises(RuntimeError, match="Could not find a code"):
            cli.oauth_finish(con, CODEX, f"http://localhost:1455/auth/callback?error=access_denied&state={q['state']}")

    def test_url_from_older_attempt_is_rejected(self, con, http):
        _, _, old, _ = start(con, CODEX)
        start(con, CODEX)
        with pytest.raises(RuntimeError, match="older login attempt"):
            cli.oauth_finish(con, CODEX, f"http://localhost:1455/auth/callback?code=c&state={old['state']}")

    def test_https_url_also_accepted(self, con, http):
        _, _, q, _ = start(con, CODEX)
        http.on("POST", "oauth/token", Resp({"access_token": "a"}))
        cli.oauth_finish(con, CODEX, f"https://localhost:1455/auth/callback?code=c&state={q['state']}")
        assert cli.kv_get(con, "oauth:chatgpt-plan")["access_token"] == "a"

    def test_bad_id_token_does_not_crash(self, con, http):
        _, _, q, _ = start(con, CODEX)
        http.on("POST", "oauth/token", Resp({"access_token": "not-a-jwt", "id_token": "garbage"}))
        rec = cli.oauth_finish(con, CODEX, f"http://localhost:1455/auth/callback?code=c&state={q['state']}")
        assert rec["account_id"] is None and rec["email"] is None


# ---------------------------------------------------------------- oauth_token (use and refresh)

def save(con, acct, **rec):
    cli.kv_set(con, f"oauth:{acct['id']}", {"access_token": "old", "refresh_token": "ref",
                                            "expires_at": int(time.time()) + 3600, **rec})


class TestToken:
    def test_not_signed_in(self, con, http):
        with pytest.raises(cli.NotConnected, match="Not connected"):
            cli.oauth_token(con, CLAUDE)

    def test_access_token_from_env_as_fallback(self, con, http, monkeypatch):
        monkeypatch.setenv("TEST_CLAUDE_TOKEN", "envtok")
        tok = cli.oauth_token(con, dict(CLAUDE, access_token_env="TEST_CLAUDE_TOKEN"))
        assert tok["access_token"] == "envtok"

    def test_valid_token_is_used_without_refresh(self, con, http):
        save(con, CLAUDE)
        assert cli.oauth_token(con, CLAUDE)["access_token"] == "old"
        assert not http.posts()

    @pytest.mark.parametrize("seconds_left", [-100, 0, 299])
    def test_token_expiring_within_5_min_is_refreshed(self, con, http, seconds_left):
        save(con, CLAUDE, expires_at=int(time.time()) + seconds_left)
        http.on("POST", "oauth/token", Resp({"access_token": "new", "refresh_token": "ref2", "expires_in": 3600}))
        tok = cli.oauth_token(con, CLAUDE)
        assert tok["access_token"] == "new" and tok["refresh_token"] == "ref2"
        body = http.posts()[0][2]["json"]
        assert body == {"grant_type": "refresh_token", "refresh_token": "ref",
                        "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"}

    def test_refresh_keeps_old_refresh_token_and_email_when_not_returned(self, con, http):
        save(con, CLAUDE, expires_at=0, email="me@example.com")
        http.on("POST", "oauth/token", Resp({"access_token": "new", "expires_in": 3600}))
        tok = cli.oauth_token(con, CLAUDE)
        assert tok["refresh_token"] == "ref" and tok["email"] == "me@example.com"

    def test_codex_refresh_sends_scope_as_form(self, con, http):
        save(con, CODEX, expires_at=0, account_id="acct-1")
        http.on("POST", "oauth/token", Resp({"access_token": "new"}))
        tok = cli.oauth_token(con, CODEX)
        kw = http.posts()[0][2]
        assert kw["data"]["scope"] == "openid profile email" and tok["account_id"] == "acct-1"

    def test_failed_refresh_asks_to_sign_in_again(self, con, http):
        save(con, CLAUDE, expires_at=0)
        http.on("POST", "oauth/token", Resp({"error": "invalid_grant"}, status=400))
        with pytest.raises(cli.NotConnected, match="refresh failed"):
            cli.oauth_token(con, CLAUDE)

    def test_expired_without_refresh_token_is_returned_as_is(self, con, http):
        save(con, CLAUDE, expires_at=0, refresh_token=None)
        assert cli.oauth_token(con, CLAUDE)["access_token"] == "old"
        assert not http.posts()


# ---------------------------------------------------------------- reading the plan with the token

class TestCollectClaude:
    def test_reads_windows_with_the_right_headers(self, con, http):
        save(con, CLAUDE, email="me@example.com")
        http.on("GET", "api/oauth/usage", Resp({
            "five_hour": {"utilization": 63, "resets_at": "2026-11-19T05:00:00Z"},
            "seven_day": {"utilization": 41, "resets_at": "2026-11-22T00:00:00Z"},
            "seven_day_opus": {"utilization": None}}))
        m = cli.collect_claude_sub(CLAUDE, con)
        assert [w["used_pct"] for w in m["windows"]] == [63, 41]
        assert m["account"] == "me@example.com"
        h = http.gets("api/oauth/usage")[0][2]["headers"]
        assert h["Authorization"] == "Bearer old" and h["anthropic-beta"] == "oauth-2025-04-20"

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_login_means_sign_in_again(self, con, http, status):
        save(con, CLAUDE)
        http.on("GET", "api/oauth/usage", Resp({}, status=status))
        with pytest.raises(cli.NotConnected, match="Reconnect"):
            cli.collect_claude_sub(CLAUDE, con)

    def test_server_error_is_a_failed_check(self, con, http):
        save(con, CLAUDE)
        http.on("GET", "api/oauth/usage", Resp({}, status=500, text="boom"))
        with pytest.raises(RuntimeError, match="HTTP 500"):
            cli.collect_claude_sub(CLAUDE, con)


class TestCollectCodex:
    def test_sends_account_header_and_reads_windows(self, con, http):
        save(con, CODEX, account_id="acct-1", plan="plus")
        http.on("GET", "wham/usage", Resp({"plan_type": "plus", "rate_limit": {
            "primary_window": {"used_percent": 20, "limit_window_seconds": 18000, "reset_after_seconds": 600},
            "secondary_window": {"used_percent": 5, "limit_window_seconds": 604800, "reset_at": 1800000000}}}))
        m = cli.collect_codex_sub(CODEX, con)
        assert [w["used_pct"] for w in m["windows"]] == [20, 5] and m["plan"] == "plus"
        assert http.gets("wham/usage")[0][2]["headers"]["ChatGPT-Account-Id"] == "acct-1"

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_login_means_sign_in_again(self, con, http, status):
        save(con, CODEX)
        http.on("GET", "wham/usage", Resp({}, status=status))
        with pytest.raises(cli.NotConnected):
            cli.collect_codex_sub(CODEX, con)
