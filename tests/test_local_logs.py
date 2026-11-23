"""Tokens from coding tools' local logs, balance-drop spend, and how both feed the deduplicated totals."""
import json
import os
import time

import pytest

from conftest import cli

NOW = cli.now_utc()
HR = NOW.strftime("%Y-%m-%dT%H")
TS = NOW.strftime("%Y-%m-%dT%H:%M:%S.000Z")
MONTH = NOW.strftime("%Y-%m")


@pytest.fixture(autouse=True)
def clear_cache():
    cli._LOG_CACHE.clear()
    yield
    cli._LOG_CACHE.clear()


def write_jsonl(path, rows, junk=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in rows]
    if junk:
        lines[1:1] = ["", "not json at all", "{broken", "[1,2]"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reply(mid, sid="s1", model="claude-opus-5-5", i=10, o=20, cr=300, cw=40, ts=TS, thinking=5):
    return {"type": "assistant", "sessionId": sid, "timestamp": ts, "requestId": "req-" + mid,
            "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": "text", "text": "secret words"}],
                        "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
                                  "cache_creation_input_tokens": cw, "output_tokens_details": {"thinking_tokens": thinking}}}}


def cost_state(sid, total, models):
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": total, "startTime": int(time.time() * 1000),
            "modelUsage": models}


def mu(i=0, o=0, cr=0, cw=0, th=0, cost=0.0):
    return {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr, "cacheCreationInputTokens": cw,
            "thinkingTokens": th, "costUSD": cost}


def codex_session(path, records=True, provider="openai", model="gpt-6.1-sol", replies=((100, 40, 10, 5),)):
    rows = [{"type": "session_meta", "timestamp": TS, "payload": {"model_provider": provider, "id": "x"}},
            {"type": "turn_context", "timestamp": TS, "payload": {"turn_id": "t1", "model": model}}]
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0,
             "total_tokens": 0, "cache_write_input_tokens": 0}
    for n, (i, cached, o, r) in enumerate(replies):
        u = {"input_tokens": i, "cached_input_tokens": cached, "cache_write_input_tokens": 0, "output_tokens": o,
             "reasoning_output_tokens": r, "total_tokens": i + o}
        for k in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            total[k] += u[k]
        if records:
            rows.append({"type": "token_usage_record", "timestamp": TS,
                         "payload": {"turn_id": "t1", "response_id": f"resp-{n}", "usage": u}})
        ev = {"type": "event_msg", "timestamp": TS,
              "payload": {"type": "token_count", "info": {"total_token_usage": dict(total), "last_token_usage": u}}}
        rows.append(ev)
        rows.append(ev)  # Codex can repeat the same total; must not double count
    write_jsonl(path, rows)


def collect(tmp_path, **acct):
    return cli.collect_local_logs({"id": "logs", "claude_dir": str(tmp_path / "claude"),
                                   "codex_dir": str(tmp_path / "codex"), **acct})


def month_tokens(m, model_part=None):
    rows = m["token_usage"]["by_model"]
    if model_part:
        rows = [r for r in rows if model_part in r["model"]]
    return sum(r["input"] + r["output"] + r["cache"] for r in rows)


# ---------------------------------------------------------------- Claude Code

class TestClaudeCode:
    def test_each_reply_counted_once_even_when_logged_per_block(self, tmp_path):
        r = reply("m1")
        write_jsonl(tmp_path / "claude/p/a.jsonl", [r, r, r, reply("m2")])
        m = collect(tmp_path)
        t = m["token_usage"]["totals"]
        assert (t["input"], t["output"], t["cache"], t["requests"]) == (20, 40, 680, 2)

    def test_same_reply_in_two_files_counted_once(self, tmp_path):
        write_jsonl(tmp_path / "claude/p1/a.jsonl", [reply("m1")])
        write_jsonl(tmp_path / "claude/p2/b.jsonl", [reply("m1")])
        assert month_tokens(collect(tmp_path)) == 370

    def test_bad_lines_and_other_types_are_skipped(self, tmp_path):
        rows = [{"type": "user", "message": {"content": "hi"}}, reply("m1"),
                {"type": "assistant", "message": {"id": "nousage", "model": "x"}},
                {"type": "assistant", "timestamp": TS, "message": {"id": "syn", "model": "<synthetic>",
                                                                   "usage": {"input_tokens": 999}}}]
        write_jsonl(tmp_path / "claude/p/a.jsonl", rows, junk=True)
        assert month_tokens(collect(tmp_path)) == 370

    def test_cost_comes_from_cost_state(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1"), reply("m2"),
                                                    cost_state("s1", 0.5, {"claude-opus-5-5": mu(20, 40, 600, 80, cost=0.5)})])
        m = collect(tmp_path)
        assert m["token_usage"]["totals"]["cost"] == pytest.approx(0.5)
        assert m["est_cost_mtd"] == pytest.approx(0.5)

    def test_latest_cost_state_wins(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [
            reply("m1"), cost_state("s1", 0.2, {"claude-opus-5-5": mu(10, 20, 300, 40, cost=0.2)}),
            cost_state("s1", 0.9, {"claude-opus-5-5": mu(10, 20, 300, 40, cost=0.9)})])
        assert collect(tmp_path)["token_usage"]["totals"]["cost"] == pytest.approx(0.9)

    def test_side_calls_only_in_cost_state_are_included(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1"), cost_state("s1", 1.1, {
            "claude-opus-5-5": mu(10, 20, 300, 40, cost=1.0), "claude-haiku-5-5": mu(5000, 700, cost=0.1)})])
        m = collect(tmp_path)
        assert month_tokens(m, "haiku") == 5700
        assert month_tokens(m, "opus") == 370
        assert m["token_usage"]["totals"]["cost"] == pytest.approx(1.1)

    def test_cost_state_larger_than_replies_raises_the_count(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1"), cost_state("s1", 1, {
            "claude-opus-5-5": mu(100, 200, 3000, 400, cost=1)})])
        assert month_tokens(collect(tmp_path)) == 3700

    def test_cost_split_across_days_by_tokens(self, tmp_path):
        yesterday = (NOW - cli.dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write_jsonl(tmp_path / "claude/p/a.jsonl", [
            reply("m1", i=0, o=0, cr=100, cw=0, ts=yesterday), reply("m2", i=0, o=0, cr=300, cw=0),
            cost_state("s1", 4.0, {"claude-opus-5-5": mu(0, 0, 400, 0, cost=4.0)})])
        daily = {d["date"]: d for d in collect(tmp_path)["token_usage"]["daily"]}
        assert daily[yesterday[:10]]["cost"] == pytest.approx(1.0)
        assert daily[TS[:10]]["cost"] == pytest.approx(3.0)

    def test_sessions_without_cost_state_have_tokens_but_no_cost(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        m = collect(tmp_path)
        assert month_tokens(m) == 370 and m["token_usage"]["totals"]["cost"] is None

    @pytest.mark.parametrize("model,provider", [("deepseek-v4-pro", "deepseek"), ("kimi-k3", "moonshot"),
                                                ("glm-5", "zhipu"), ("claude-sonnet-5-5", "anthropic"),
                                                ("some-local-model", "anthropic")])
    def test_other_models_through_claude_code_go_to_their_provider(self, tmp_path, model, provider):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1", model=model)])
        assert collect(tmp_path)["token_usage"]["by_provider"][0]["provider"] == provider

    def test_old_months_are_not_in_this_month(self, tmp_path):
        old = (NOW - cli.dt.timedelta(days=70)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1", ts=old), reply("m2")])
        m = collect(tmp_path)
        assert m["token_usage"]["totals"]["input"] == 10
        assert all(h[0] >= (NOW - cli.dt.timedelta(days=32)).strftime("%Y-%m-%dT%H") for h in m["hourly"])

    def test_message_text_never_leaves_the_collector(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        assert "secret words" not in json.dumps(collect(tmp_path))


# ---------------------------------------------------------------- Codex

class TestCodex:
    def test_records_counted_once_with_cached_input_split_out(self, tmp_path):
        codex_session(tmp_path / "codex/2026/11/19/rollout-a.jsonl", replies=[(100, 40, 10, 5), (200, 150, 20, 0)])
        m = collect(tmp_path)
        t = m["token_usage"]["totals"]
        # input excludes cached: (100-40)+(200-150); cache = 40+150
        assert (t["input"], t["output"], t["cache"], t["reasoning"], t["requests"]) == (110, 30, 190, 5, 2)
        assert m["token_usage"]["by_provider"] == [{"provider": "codex", "tokens": 330, "cost": 0.0}]

    def test_older_logs_use_running_totals_without_double_counting(self, tmp_path):
        codex_session(tmp_path / "codex/old.jsonl", records=False, replies=[(100, 40, 10, 0), (200, 150, 20, 0)])
        assert collect(tmp_path)["token_usage"]["totals"]["input"] == 110

    def test_model_comes_from_turn_context(self, tmp_path):
        codex_session(tmp_path / "codex/a.jsonl", model="gpt-6.1-sol")
        assert collect(tmp_path)["token_usage"]["by_model"][0]["model"].startswith("gpt-6.1-sol")

    def test_codex_pointed_at_another_provider(self, tmp_path):
        codex_session(tmp_path / "codex/a.jsonl", provider="deepseek", model="deepseek-v4-pro")
        assert collect(tmp_path)["token_usage"]["by_provider"][0]["provider"] == "deepseek"

    def test_compressed_files_are_skipped_with_a_note_when_zstd_is_missing(self, tmp_path, monkeypatch):
        codex_session(tmp_path / "codex/a.jsonl")
        (tmp_path / "codex/b.jsonl.zst").write_bytes(b"\x28\xb5\x2f\xfd not really zstd")
        import builtins
        real_import = builtins.__import__

        def no_zstd(name, *a, **kw):
            if name in ("zstandard", "compression"):
                raise ImportError(name)
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", no_zstd)
        m = collect(tmp_path)
        assert m["logs"]["codex"] == {"files": 1, "skipped_zst": 1, "unreadable": 0}
        assert "zstandard" in m["note"]

    def test_compressed_files_are_read_when_zstd_is_available(self, tmp_path):
        zstandard = pytest.importorskip("zstandard")
        codex_session(tmp_path / "plain/a.jsonl")
        raw = (tmp_path / "plain/a.jsonl").read_bytes()
        (tmp_path / "codex").mkdir()
        (tmp_path / "codex/a.jsonl.zst").write_bytes(zstandard.ZstdCompressor().compress(raw))
        assert collect(tmp_path)["token_usage"]["totals"]["input"] == 60


# ---------------------------------------------------------------- folders, errors and caching

class TestFoldersAndCache:
    def test_no_folders_is_a_clear_error(self, tmp_path):
        with pytest.raises(RuntimeError, match="No coding tool records found"):
            collect(tmp_path)

    def test_empty_folders_say_no_sessions_yet(self, tmp_path):
        (tmp_path / "claude").mkdir()
        with pytest.raises(RuntimeError, match="No coding tool sessions found yet"):
            collect(tmp_path)

    def test_only_one_tool_present_is_fine(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        assert collect(tmp_path)["logs"]["codex"]["files"] == 0

    def test_default_folders_follow_the_tools_env_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
        write_jsonl(tmp_path / "cc/projects/p/a.jsonl", [reply("m1")])
        codex_session(tmp_path / "cx/sessions/a.jsonl")
        m = cli.collect_local_logs({"id": "logs"})
        assert m["logs"]["claude_code"]["files"] == 1 and m["logs"]["codex"]["files"] == 1

    def test_unchanged_files_are_not_parsed_again(self, tmp_path, monkeypatch):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        collect(tmp_path)
        calls = []
        real = cli._parse_claude_file

        def spy(p):
            calls.append(p)
            return real(p)
        spy.__name__ = real.__name__  # the cache is keyed by reader name
        monkeypatch.setattr(cli, "_parse_claude_file", spy)
        collect(tmp_path)
        assert calls == []

    def test_changed_files_are_parsed_again(self, tmp_path):
        f = tmp_path / "claude/p/a.jsonl"
        write_jsonl(f, [reply("m1")])
        assert month_tokens(collect(tmp_path)) == 370
        write_jsonl(f, [reply("m1"), reply("m2")])
        os.utime(f, (time.time() + 5, time.time() + 5))
        assert month_tokens(collect(tmp_path)) == 740


# ---------------------------------------------------------------- balance-drop spend

def snap(con, aid, ts, balance, ok=1):
    con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,?,?)",
                (ts, aid, ok, json.dumps({"balance": balance})))
    con.commit()


class TestBalanceSpend:
    def iso(self, days_ago=0, hours=0):
        return cli.iso(NOW - cli.dt.timedelta(days=days_ago, hours=hours))

    def test_drops_are_spend_and_top_ups_are_ignored(self, con):
        for h, b in [(5, 10.0), (4, 9.0), (3, 8.5), (2, 20.0), (1, 19.0)]:  # top-up of 11.5 at hour 2
            snap(con, "ds", self.iso(hours=h), b)
        bs = cli.balance_spend(con, "ds", {"USD": 1}, "USD")
        assert sum(bs["daily"].values()) == pytest.approx(2.5) and bs["checks"] == 5

    def test_failed_checks_are_ignored(self, con):
        snap(con, "ds", self.iso(hours=3), 10.0)
        snap(con, "ds", self.iso(hours=2), 0.0, ok=0)
        snap(con, "ds", self.iso(hours=1), 9.0)
        assert cli.balance_spend(con, "ds", {"USD": 1}, "USD")["month"] == pytest.approx(1.0)

    def test_other_currency_is_converted(self, con):
        snap(con, "ds", self.iso(hours=2), 100.0)
        snap(con, "ds", self.iso(hours=1), 93.0)
        assert cli.balance_spend(con, "ds", {"USD": 1, "CNY": 0.14}, "CNY")["month"] == pytest.approx(0.98)

    def test_unknown_currency_is_not_guessed(self, con):
        snap(con, "ds", self.iso(hours=2), 100.0)
        snap(con, "ds", self.iso(hours=1), 93.0)
        assert cli.balance_spend(con, "ds", {"USD": 1}, "CNY") is None

    def test_one_check_means_no_spend_yet(self, con):
        snap(con, "ds", self.iso(hours=1), 5.0)
        bs = cli.balance_spend(con, "ds", {"USD": 1}, "USD")
        assert bs["month"] == 0 and bs["checks"] == 1

    def test_single_reading_leaves_spend_unknown(self, cfg, con):
        cfg["api_accounts"].append({"id": "ds", "label": "DeepSeek", "provider": "deepseek", "api_key_env": "X"})
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (self.iso(hours=1), "ds", json.dumps({"balance": -0.32, "balance_currency": "USD"})))
        con.commit()
        card = next(a for a in cli.build_state(cfg)["apis"] if a["id"] == "ds")
        assert "spend_mtd" not in card["metrics"] and card["metrics"]["balance_spend"]["checks"] == 1

    def test_spend_shows_on_the_card_and_in_money_totals(self, cfg, con, http):
        cfg["api_accounts"].append({"id": "ds", "label": "DeepSeek", "provider": "deepseek", "api_key_env": "X"})
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (self.iso(hours=2), "ds", json.dumps({"balance": 10.0, "balance_currency": "USD"})))
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (self.iso(hours=1), "ds", json.dumps({"balance": 7.5, "balance_currency": "USD"})))
        con.commit()
        st = cli.build_state(cfg)
        card = next(a for a in st["apis"] if a["id"] == "ds")
        assert card["metrics"]["spend_mtd"] == pytest.approx(2.5)
        assert st["totals"]["api_mtd_usd"] == pytest.approx(2.5)
        row = next(r for r in st["token_total"]["by_provider"] if r["name"] == "DeepSeek")
        assert row["cost"] == pytest.approx(2.5) and "balance changes" in row["from"]


# ---------------------------------------------------------------- how it all adds up

def api(provider, hourly=None, tu=None, label=None, **metrics):
    m = dict(metrics)
    if hourly is not None:
        m["hourly"] = hourly
    if tu is not None:
        m["token_usage"] = tu
    return {"id": provider, "label": label or provider, "provider": provider, "metrics": m}


def own_report(tokens, cost):
    day = NOW.strftime("%Y-%m-%d")
    z = {"input": tokens, "output": 0, "cache": 0, "cost": cost}
    return {"totals": z, "today": z, "daily": [{"date": day, "tokens": tokens, "cost": cost}]}


class TestTotals:
    def test_hermes_and_local_logs_add_up_per_provider(self):
        t = cli.token_total([api("hermes", [[HR, "deepseek", "deepseek-v4", 1000, 1.0]]),
                             api("local_logs", [[HR, "deepseek", "deepseek-v4", 500, 0.5]])])
        row = t["by_provider"][0]
        assert (row["name"], row["tokens"], row["cost"]) == ("DeepSeek", 1500, 1.5)
        assert row["from"] == "Hermes sessions and Local logs"

    def test_providers_own_report_wins_when_larger(self):
        t = cli.token_total([api("local_logs", [[HR, "anthropic", "claude-opus-5-5", 500, 2.0]]),
                             api("anthropic", tu=own_report(800, 3.0), label="Anthropic API")])
        assert t["tokens"] == 800 and t["by_provider"][0]["from"] == "Anthropic API report"

    def test_records_win_when_they_saw_more(self):
        t = cli.token_total([api("local_logs", [[HR, "anthropic", "claude-opus-5-5", 900, 2.0]]),
                             api("anthropic", tu=own_report(800, 3.0))])
        assert t["tokens"] == 900 and t["by_provider"][0]["from"] == "Local logs"

    def test_codex_tokens_do_not_land_on_the_openai_api_card(self):
        t = cli.token_total([api("local_logs", [[HR, "codex", "gpt-6.1-sol", 700, 0]]),
                             api("openai", tu=own_report(100, 1.0), label="OpenAI API")])
        names = {r["name"]: r["tokens"] for r in t["by_provider"]}
        assert names == {"ChatGPT / Codex": 700, "OpenAI API": 100}

    def test_balance_spend_alone_makes_a_row(self):
        t = cli.token_total([api("deepseek", balance_spend={"month": 4.0, "today": 1.0, "daily": {NOW.strftime("%Y-%m-%d"): 1.0}})])
        row = {k: v for k, v in t["by_provider"][0].items() if k not in ("trend", "week", "prev_week")}
        assert len(t["by_provider"]) == 1
        assert row == {"name": "DeepSeek", "tokens": 0, "cost": 4.0, "today": 0, "today_cost": 1.0, "from": "balance changes"}

    def test_each_row_has_a_14_day_trend_ending_today(self):
        t = cli.token_total([api("local_logs", [[HR, "deepseek", "deepseek-chat", 1500, 1.5]])])
        row = t["by_provider"][0]
        assert len(row["trend"]) == 14 and row["trend"][-1] == 1500 and sum(row["trend"]) == 1500
        assert (row["week"], row["prev_week"]) == (1500, 0)

    def test_nothing_to_count(self):
        assert cli.token_total([api("deepseek", balance=3)]) is None

    def test_balance_that_never_dropped_adds_no_empty_row(self):
        unchanged = {"month": 0, "today": 0, "daily": {}, "checks": 4}
        assert cli.token_total([api("deepseek", balance=3, balance_spend=unchanged)]) is None
        t = cli.token_total([api("deepseek", balance=3, balance_spend=unchanged),
                             api("local_logs", [[HR, "anthropic", "claude-opus-5-5", 10, 0]])])
        assert [r["name"] for r in t["by_provider"]] == ["Anthropic"]

    def test_plan_card_windows_get_tokens_from_local_logs(self, cfg):
        reset = (NOW + cli.dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        apis = [api("local_logs", [[HR, "anthropic", "claude-opus-5-5", 1234, 0.7]]),
                api("claude_sub", windows=[cli.window("five_hour", "5-hour session", 40, resets_at=reset, window_seconds=18000)])]
        cli.enrich_windows(cfg, apis)
        w = apis[1]["metrics"]["windows"][0]
        assert (w["tokens"], w["cost"], w["tokens_src"]) == (1234, 0.7, "records")
        assert "hourly" not in apis[0]["metrics"]  # internal rows are not sent to the browser

    def test_window_with_no_records_claims_nothing(self, cfg):
        reset = (NOW + cli.dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        old = (NOW - cli.dt.timedelta(days=3)).strftime("%Y-%m-%dT%H")
        apis = [api("local_logs", [[old, "codex", "gpt-6.1-sol", 500, None]]),
                api("codex_sub", windows=[cli.window("p", "5-hour", 53, resets_at=reset, window_seconds=18000)])]
        cli.enrich_windows(cfg, apis)
        w = apis[1]["metrics"]["windows"][0]
        assert "tokens" not in w and "cost" not in w and "token_limit" not in w

    def test_unknown_cost_is_shown_as_unknown_not_zero(self, cfg):
        reset = (NOW + cli.dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        apis = [api("local_logs", [[HR, "codex", "gpt-6.1-sol", 500, None]]),
                api("codex_sub", windows=[cli.window("p", "5-hour", 10, resets_at=reset, window_seconds=18000)])]
        cli.enrich_windows(cfg, apis)
        w = apis[1]["metrics"]["windows"][0]
        assert w["tokens"] == 500 and w["cost"] is None
        assert cli.token_total([api("local_logs", [[HR, "codex", "gpt-6.1-sol", 500, None]])])["tokens"] == 500

    def test_estimates_are_not_counted_as_money_spent(self, cfg, con):
        cfg["api_accounts"].append({"id": "logs", "provider": "local_logs"})
        con.execute("INSERT INTO snapshots(ts, account_id, ok, metrics) VALUES (?,?,1,?)",
                    (cli.iso(NOW), "logs", json.dumps({"est_cost_mtd": 110.0, "token_usage": own_report(5, 110.0)})))
        con.commit()
        assert cli.build_state(cfg)["totals"]["api_mtd_usd"] == 0
