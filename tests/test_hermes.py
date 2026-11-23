"""Hermes Agent's session database: read-only, per model and provider, old and new column sets."""
import sqlite3

import pytest

from conftest import cli

NOW = cli.now_utc()


def make_db(path, columns_full=True, rows=()):
    con = sqlite3.connect(path)
    if columns_full:
        con.execute("""CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, message_count INTEGER,
            api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
            cache_write_tokens INTEGER, reasoning_tokens INTEGER, billing_provider TEXT,
            estimated_cost_usd REAL, actual_cost_usd REAL)""")
    else:  # an older Hermes without provider, cache or actual-cost columns
        con.execute("CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, message_count INTEGER, "
                    "input_tokens INTEGER, output_tokens INTEGER, estimated_cost_usd REAL)")
    for r in rows:
        con.execute(f"INSERT INTO sessions VALUES ({','.join('?' * len(r))})", r)
    con.commit()
    con.close()


def test_reads_tokens_cost_and_providers(tmp_path):
    t = NOW.timestamp()
    make_db(tmp_path / "state.db", rows=[
        ("a", "deepseek-v4", t, 4, 3, 1000, 100, 50, 0, 10, "deepseek", 0.5, None),
        ("b", "kimi-k3", t, 2, 2, 400, 40, 0, 0, 0, "kimi-coding", 0.2, 0.25)])
    m = cli.collect_hermes({"id": "h", "state_db": str(tmp_path / "state.db")})
    t = m["token_usage"]["totals"]
    assert (t["input"], t["output"], t["cache"], t["requests"]) == (1400, 140, 50, 5)
    assert t["cost"] == pytest.approx(0.75)  # actual cost wins over the estimate
    assert {p["provider"] for p in m["token_usage"]["by_provider"]} == {"deepseek", "kimi-coding"}
    assert {r[1] for r in m["hourly"]} == {"deepseek", "kimi-coding"}


def test_older_hermes_without_newer_columns(tmp_path):
    make_db(tmp_path / "state.db", columns_full=False, rows=[("a", "m", NOW.timestamp(), 3, 100, 10, 0.1)])
    m = cli.collect_hermes({"id": "h", "state_db": str(tmp_path / "state.db")})
    assert m["token_usage"]["totals"]["input"] == 100 and m["token_usage"]["totals"]["requests"] == 3


def test_database_is_opened_read_only(tmp_path):
    db = tmp_path / "state.db"
    make_db(db, rows=[("a", "m", NOW.timestamp(), 1, 1, 1, 1, 0, 0, 0, "x", 0, None)])
    before = db.read_bytes()
    cli.collect_hermes({"id": "h", "state_db": str(db)})
    assert db.read_bytes() == before


def test_missing_database_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="Hermes database not found"):
        cli.collect_hermes({"id": "h", "state_db": str(tmp_path / "nope.db")})
