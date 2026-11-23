"""Shared fixtures. Every test runs offline: requests.get/post inside ai_meter.cli go to FakeHTTP."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_meter import cli  # noqa: E402


class Resp:
    def __init__(self, data=None, status=200, headers=None, text=None):
        self._data = data if data is not None else {}
        self.status_code = status
        self.ok = status < 300
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._data)

    def json(self):
        return self._data


class FakeHTTP:
    """Routes by URL substring. A route value is a Resp, an Exception to raise, or a callable(url, kw) -> Resp.
    Every call is recorded in .calls as (method, url, kwargs)."""

    def __init__(self):
        self.routes = []
        self.calls = []

    def on(self, method, needle, result):
        self.routes.insert(0, (method, needle, result))  # later registrations win

    def _handle(self, method, url, kw):
        self.calls.append((method, url, kw))
        for m, needle, result in self.routes:
            if m == method and needle in url:
                if isinstance(result, Exception):
                    raise result
                return result(url, kw) if callable(result) else result
        return Resp({"error": "no fake route"}, status=404)

    def get(self, url, **kw):
        return self._handle("GET", url, kw)

    def post(self, url, **kw):
        return self._handle("POST", url, kw)

    def posts(self, needle=""):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]

    def gets(self, needle=""):
        return [c for c in self.calls if c[0] == "GET" and needle in c[1]]


@pytest.fixture(autouse=True)
def no_real_user_data(tmp_path_factory, monkeypatch):
    """Tests never read the real user's tool records: every default location points into an empty folder.
    A test that wants a default location sets the tool's own env var (CLAUDE_CONFIG_DIR, CODEX_HOME, ...)."""
    empty = tmp_path_factory.mktemp("empty-home")
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "QWEN_DATA_DIR", "CONTINUE_GLOBAL_DIR"):
        monkeypatch.setenv(var, str(empty / var.lower()))
    monkeypatch.setattr(cli, "_default_dsh_dir", lambda: str(empty / "dsh" / "sessions"))


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(cli.requests, "get", fake.get)
    monkeypatch.setattr(cli.requests, "post", fake.post)
    return fake


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A fresh config with its own database, and no AIMETER_SECRET_* leftovers between tests."""
    import os
    for k in list(os.environ):
        if k.startswith("AIMETER_SECRET_"):
            monkeypatch.delenv(k)
    p = tmp_path / "config.yaml"
    p.write_text("settings:\n  db_path: data/test.db\napi_accounts: []\nsubscriptions: []\n", encoding="utf-8")
    return cli.load_config(str(p))


@pytest.fixture
def con(cfg):
    c = cli.db_connect(cfg)
    yield c
    c.close()
