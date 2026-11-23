"""Security tests: access control, cross-site requests, DNS rebinding, request limits, secret handling,
clickjacking and script injection. The server runs for real on a spare local port; providers are faked."""
import argparse
import base64
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import Resp, cli

TOKEN = "s3cret-token-0123456789"
SECRET_KEY = "sk-SUPER-SECRET-KEY-42"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(cfg, token=None, monkeypatch=None):
    if token:
        monkeypatch.setenv("AIMETER_SEC_TOKEN", token)
        cfg["settings"]["dashboard"] = {"token_env": "AIMETER_SEC_TOKEN"}
    else:
        cfg["settings"]["dashboard"] = {}
    port = free_port()
    threading.Thread(target=cli.cmd_serve, daemon=True,
                     args=(cfg, argparse.Namespace(host="127.0.0.1", port=port, no_collect=True, config=None))).start()
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except OSError:
            time.sleep(0.05)
    return port


@pytest.fixture
def open_server(cfg, http, monkeypatch):
    """Default local setup: no token, listening on 127.0.0.1."""
    return start(cfg)


@pytest.fixture
def token_server(cfg, http, monkeypatch):
    return start(cfg, TOKEN, monkeypatch)


def raw_request(port, method, path, headers=None, body=b""):
    """A hand-written HTTP request, so headers like Host and Content-Length can be anything."""
    h = {"Host": f"127.0.0.1:{port}", "Connection": "close", **(headers or {})}
    if body and "Content-Length" not in h:
        h["Content-Length"] = str(len(body))
    req = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items()) + "\r\n"
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(req.encode("latin-1") + body)
    data = b""
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        data += chunk
    s.close()
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP/") else 0
    hdrs = {k.lower(): v.strip() for k, v in (ln.split(":", 1) for ln in lines[1:] if ":" in ln)}
    return status, hdrs, rest


def post(port, path, obj, **headers):
    h = {"Content-Type": "application/json", **headers}
    return raw_request(port, "POST", path, h, json.dumps(obj).encode())


# ---------------------------------------------------------------- access control

class TestAccessControl:
    def test_everything_but_health_needs_the_token(self, token_server):
        for method, path in [("GET", "/"), ("GET", "/api/state"), ("POST", "/api/collect"),
                             ("POST", "/api/providers/add"), ("POST", "/api/providers/remove"),
                             ("POST", "/api/oauth/start"), ("POST", "/api/oauth/finish"), ("POST", "/api/log")]:
            code, _, _ = raw_request(token_server, method, path, {"Content-Type": "application/json"},
                                     b"{}" if method == "POST" else b"")
            assert code == 401, (method, path)

    @pytest.mark.parametrize("auth", ["Bearer wrong", "Bearer ", "Basic !!!notbase64", "Basic " + base64.b64encode(b"nocolon").decode(),
                                      "Basic " + base64.b64encode(b"u:wrong").decode(), "Token " + TOKEN, TOKEN])
    def test_wrong_or_malformed_credentials_are_refused(self, token_server, auth):
        assert raw_request(token_server, "GET", "/api/state", {"Authorization": auth})[0] == 401

    @pytest.mark.parametrize("q", ["?token=", "?token=wrong", "?token=" + TOKEN[:-1], "?token=" + TOKEN + "x",
                                   "?Token=" + TOKEN, "?token=%00"])
    def test_wrong_query_tokens_are_refused(self, token_server, q):
        assert raw_request(token_server, "GET", "/api/state" + q)[0] == 401

    def test_token_is_compared_in_constant_time(self):
        import inspect
        src = inspect.getsource(cli.cmd_serve)
        assert src.count("hmac.compare_digest") >= 3 and "== token" not in src

    def test_only_known_paths_are_served(self, token_server):
        for path in ["/../../etc/passwd", "/dashboard.html", "/cli.py", "/%2e%2e/%2e%2e/config.yaml",
                     "/api/state/../../config.yaml", "/.env"]:
            code, _, body = raw_request(token_server, "GET", path + ("&" if "?" in path else "?") + "token=" + TOKEN)
            assert code == 404, path
            assert b"api_key" not in body and b"settings" not in body

    def test_open_server_refuses_to_listen_publicly_without_a_warning(self, cfg, monkeypatch, caplog):
        cfg["settings"]["dashboard"] = {}
        port = free_port()
        threading.Thread(target=cli.cmd_serve, daemon=True,
                         args=(cfg, argparse.Namespace(host="0.0.0.0", port=port, no_collect=True, config=None))).start()
        time.sleep(0.5)
        assert any("without a token" in r.message for r in caplog.records)


# ---------------------------------------------------------------- cross-site requests (CSRF) and DNS rebinding

class TestCrossSite:
    """With no token, any web page you visit could try to call the local dashboard. These attacks must fail."""

    @pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", None])
    def test_simple_cross_site_posts_are_refused(self, open_server, http, ctype):
        # Browsers send these content types cross-site without asking first, so they must not be accepted.
        h = {"Content-Type": ctype} if ctype else {}
        code, _, _ = raw_request(open_server, "POST", "/api/providers/add", h,
                                 json.dumps({"provider": "deepseek", "api_key": "attacker"}).encode())
        assert code == 415
        assert json.loads(raw_request(open_server, "GET", "/api/state")[2])["apis"] == []

    @pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example", "null",
                                        "http://127.0.0.1:1", "http://localhost"])
    def test_posts_from_other_origins_are_refused(self, open_server, http, origin):
        code, _, _ = post(open_server, "/api/providers/add", {"provider": "deepseek", "api_key": "k"}, Origin=origin)
        assert code == 403

    @pytest.mark.parametrize("name", ["127.0.0.1", "localhost"])
    def test_posts_from_the_dashboard_itself_are_allowed(self, open_server, http, name):
        http.on("GET", "deepseek", Resp({"balance_infos": [{"currency": "USD", "total_balance": "1"}]}))
        code, _, _ = post(open_server, "/api/providers/add", {"provider": "deepseek", "api_key": "k"},
                          Origin=f"http://{name}:{open_server}", Host=f"{name}:{open_server}",
                          **{"Sec-Fetch-Site": "same-origin"})
        assert code == 200

    def test_cross_site_fetch_metadata_is_refused(self, open_server, http):
        code, _, _ = post(open_server, "/api/collect", {}, **{"Sec-Fetch-Site": "cross-site"})
        assert code == 403

    @pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "attacker.com:80", ""])
    def test_dns_rebinding_hosts_are_refused(self, open_server, host):
        # A rebinding page reaches 127.0.0.1 with its own host name; it must not read the state.
        for path in ("/", "/api/state"):
            code, _, body = raw_request(open_server, "GET", path, {"Host": host.format(port=open_server)})
            assert code == 403, (host, path)
            assert b"apis" not in body

    @pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"])
    def test_local_host_names_are_allowed(self, open_server, host):
        assert raw_request(open_server, "GET", "/api/state", {"Host": host.format(port=open_server)})[0] == 200

    def test_with_a_token_other_host_names_work(self, token_server):
        # Behind a reverse proxy on a real domain the token is the protection.
        code, _, _ = raw_request(token_server, "GET", "/api/state?token=" + TOKEN, {"Host": "meter.example.com"})
        assert code == 200

    def test_no_cors_headers_are_ever_sent(self, open_server):
        _, h, _ = raw_request(open_server, "GET", "/api/state", {"Origin": "https://evil.example"})
        assert not any(k.startswith("access-control-") for k in h)
        code, h, _ = raw_request(open_server, "OPTIONS", "/api/providers/add",
                                 {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert code in (403, 404, 405, 501) and not any(k.startswith("access-control-") for k in h)


# ---------------------------------------------------------------- malformed and oversized requests

class TestRequestLimits:
    @pytest.mark.parametrize("length", ["abc", "-5", "1.5", "99999999999999999999"])
    def test_bad_content_length_is_a_400_not_a_crash(self, token_server, length):
        code, _, _ = raw_request(token_server, "POST", "/api/collect?token=" + TOKEN,
                                 {"Content-Type": "application/json", "Content-Length": length}, b"{}")
        assert code in (400, 413)
        assert raw_request(token_server, "GET", "/healthz")[0] == 200  # still alive

    def test_oversized_body_is_refused(self, token_server):
        big = b'{"x":"' + b"a" * (2 * 1024 * 1024) + b'"}'
        code, _, _ = raw_request(token_server, "POST", "/api/log?token=" + TOKEN,
                                 {"Content-Type": "application/json", "Content-Length": str(len(big))}, b"{}")
        assert code == 413

    def test_deeply_nested_json_is_refused_cleanly(self, token_server):
        body = b"[" * 50000 + b"]" * 50000
        code, _, _ = raw_request(token_server, "POST", "/api/log?token=" + TOKEN,
                                 {"Content-Type": "application/json"}, body)
        assert code == 400
        assert raw_request(token_server, "GET", "/healthz")[0] == 200


# ---------------------------------------------------------------- secrets

class TestSecrets:
    def add(self, port, http, echo_key=False):
        if echo_key:  # a provider that repeats the key back in its error message
            http.on("GET", "deepseek", lambda url, kw: Resp({"error": f"invalid key {kw['headers']['Authorization']}"}, status=401))
        else:
            http.on("GET", "deepseek", Resp({"balance_infos": [{"currency": "USD", "total_balance": "1"}]}))
        return json.loads(post(port, "/api/providers/add?token=" + TOKEN,
                               {"provider": "deepseek", "label": "DS", "api_key": SECRET_KEY})[2])

    def test_key_never_appears_in_page_or_state(self, token_server, http):
        self.add(token_server, http)
        for path in ("/", "/api/state"):
            assert SECRET_KEY.encode() not in raw_request(token_server, "GET", path + "?token=" + TOKEN)[2]

    def test_key_echoed_by_a_provider_is_redacted_everywhere(self, token_server, http, caplog):
        r = self.add(token_server, http, echo_key=True)
        assert r["checked"] is False and SECRET_KEY not in (r["error"] or "")
        assert SECRET_KEY.encode() not in raw_request(token_server, "GET", "/api/state?token=" + TOKEN)[2]
        assert SECRET_KEY not in caplog.text

    def test_oauth_tokens_never_reach_the_browser(self, token_server, http, cfg):
        aid = json.loads(post(token_server, "/api/providers/add?token=" + TOKEN, {"provider": "claude_sub"})[2])["id"]
        state = json.loads(post(token_server, "/api/oauth/start?token=" + TOKEN, {"id": aid})[2])["url"].split("state=")[1]
        http.on("POST", "oauth/token", Resp({"access_token": "AT-SECRET-1", "refresh_token": "RT-SECRET-2"}))
        post(token_server, "/api/oauth/finish?token=" + TOKEN, {"id": aid, "code": f"c#{state}"})
        body = raw_request(token_server, "GET", "/api/state?token=" + TOKEN)[2]
        assert b"AT-SECRET-1" not in body and b"RT-SECRET-2" not in body

    def test_errors_never_include_tracebacks(self, token_server):
        _, _, body = post(token_server, "/api/providers/add?token=" + TOKEN, {"provider": "deepseek", "low_balance": "x", "api_key": "k"})
        assert b"Traceback" not in body and b'File "' not in body

    @pytest.mark.skipif(sys.platform == "win32", reason="file modes are POSIX only")
    def test_database_is_private_to_the_owner(self, cfg, con):
        mode = stat.S_IMODE(os.stat(cfg["settings"]["db_path"]).st_mode)
        assert mode == 0o600

    def test_local_logs_folder_setting_cannot_leak_file_contents(self, tmp_path, cfg, con):
        secret = tmp_path / "private"
        secret.mkdir()
        (secret / "notes.jsonl").write_text(json.dumps({"type": "assistant", "password": "hunter2"}) + "\n", encoding="utf-8")
        (secret / "passwords.txt").write_text("hunter2", encoding="utf-8")
        cli._LOG_CACHE.clear()
        try:
            out = cli.collect_local_logs({"id": "x", "claude_dir": str(secret), "codex_dir": str(secret)})
        except RuntimeError as e:
            out = str(e)
        assert "hunter2" not in json.dumps(out)


def test_clients_send_json_so_they_pass_the_csrf_rule():
    """The page and the VS Code extension must keep sending JSON, or the server refuses them."""
    page = (cli.HERE / "dashboard.html").read_text(encoding="utf-8")
    assert 'method: "POST", headers: {"Content-Type": "application/json"}' in page
    ext = (cli.HERE.parents[1] / "vscode-extension" / "extension.js").read_text(encoding="utf-8")
    assert 'headers["Content-Type"] = "application/json"' in ext


def test_redact_removes_keys_and_tokens():
    out = cli.redact('HTTP 401: {"error":"bad key sk-abcDEF123456 for Bearer eyJhbGciOi.xyz", "api_key": "plainsecret99"}')
    assert "sk-abcDEF123456" not in out and "eyJhbGciOi" not in out and "plainsecret99" not in out
    assert cli.redact("HTTP 500 from /user/balance: timeout") == "HTTP 500 from /user/balance: timeout"


# ---------------------------------------------------------------- browser-side protections

class TestBrowserProtections:
    def test_page_cannot_be_framed(self, token_server):
        _, h, _ = raw_request(token_server, "GET", "/?token=" + TOKEN)
        assert h.get("x-frame-options") == "DENY"
        assert "frame-ancestors 'none'" in h.get("content-security-policy", "")

    def test_no_referrer_leaks_the_token(self, token_server):
        _, h, _ = raw_request(token_server, "GET", "/?token=" + TOKEN)
        assert h.get("referrer-policy") == "no-referrer"

    def test_api_responses_are_not_sniffed_or_cached(self, token_server):
        _, h, _ = raw_request(token_server, "GET", "/api/state?token=" + TOKEN)
        assert h.get("x-content-type-options") == "nosniff" and h.get("cache-control") == "no-store"

    def test_names_with_html_cannot_run_script_in_the_page(self, token_server, http):
        pw = pytest.importorskip("playwright.sync_api")
        evil = '<img src=x onerror="window.__xss=1">"><script>window.__xss=2</script>'
        http.on("GET", "deepseek", lambda url, kw: Resp({"error": evil}, status=500))
        post(token_server, "/api/providers/add?token=" + TOKEN, {"provider": "deepseek", "label": evil, "api_key": "k"})
        with pw.sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as e:  # browser not installed
                pytest.skip(f"chromium not available: {e}")
            pg = b.new_page()
            pg.goto(f"http://127.0.0.1:{token_server}/?token={TOKEN}")
            pg.wait_for_timeout(1500)
            assert pg.evaluate("window.__xss") is None
            assert pg.locator("text=<img src=x").count() >= 1  # shown as text, not run
            b.close()
