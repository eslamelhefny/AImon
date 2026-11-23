"""DeepSeek Harness and Continue records, and the by-tool totals."""
import json
import sqlite3
import time

import pytest

from conftest import cli
from test_local_logs import NOW, TS, reply, write_jsonl

MS = int(time.time() * 1000)


@pytest.fixture(autouse=True)
def clear():
    cli._LOG_CACHE.clear()
    yield
    cli._LOG_CACHE.clear()


def collect(tmp_path, **extra):
    return cli.collect_local_logs({"id": "logs", "claude_dir": str(tmp_path / "claude"), "codex_dir": str(tmp_path / "codex"),
                                   "qwen_dir": str(tmp_path / "qwen"), "dsh_dir": str(tmp_path / "dsh"),
                                   "continue_db": str(tmp_path / "continue" / "devdata.sqlite"), **extra})


def dsh_message(mid, provider="deepseek-official", model="deepseek-flash", i=100, o=20, cr=600, cw=0, t=MS):
    return {"type": "assistant/message", "seq": 17, "time": t,
            "data": {"turn": 1, "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": "hidden reply"}],
                                            "source": {"kind": "llm", "provider": provider, "model": model}},
                     "usage": {"inputTokens": i, "outputTokens": o, "cacheReadTokens": cr, "cacheWriteTokens": cw,
                               "totalTokens": i + o + cr + cw}}}


def write_dsh(path, rows, compress=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
    if compress:
        zstandard = pytest.importorskip("zstandard")
        data = zstandard.ZstdCompressor().compress(data)
    path.write_bytes(data)


def make_continue(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE tokens_generated (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL,
                 provider TEXT NOT NULL, tokens_generated INTEGER NOT NULL, tokens_prompt INTEGER NOT NULL DEFAULT 0,
                 timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    for model, provider, gen, prompt, ts in rows:
        c.execute("INSERT INTO tokens_generated(model, provider, tokens_generated, tokens_prompt, timestamp) VALUES (?,?,?,?,?)",
                  (model, provider, gen, prompt, ts))
    c.commit()
    c.close()


NOW_SQL = NOW.strftime("%Y-%m-%d %H:%M:%S")


class TestDeepSeekHarness:
    def test_tokens_with_cache_from_compressed_sessions(self, tmp_path):
        write_dsh(tmp_path / "dsh/proj/session-a/session.v4.jsonl.zstd",
                  [{"type": "user/message"}, dsh_message("m1"), dsh_message("m2", i=50, o=5, cr=0)], compress=True)
        m = collect(tmp_path)
        t = m["token_usage"]["totals"]
        assert (t["input"], t["output"], t["cache"], t["requests"]) == (150, 25, 600, 2)
        assert m["token_usage"]["by_provider"] == [{"provider": "deepseek", "tokens": 775, "cost": 0.0}]
        assert m["by_tool"][0]["name"] == "DeepSeek Harness" and m["by_tool"][0]["tokens"] == 775

    def test_plain_files_and_message_ids_counted_once(self, tmp_path):
        write_dsh(tmp_path / "dsh/p/s1/session.v4.jsonl", [dsh_message("m1"), dsh_message("m1")])
        write_dsh(tmp_path / "dsh/p/s2/session.v4.jsonl", [dsh_message("m1")])
        assert collect(tmp_path)["token_usage"]["totals"]["requests"] == 1

    @pytest.mark.parametrize("provider,model,expect", [("deepseek-account", "deepseek-flash", "deepseek"),
                                                       ("anthropic", "claude-sonnet-5-5", "anthropic"),
                                                       ("custom", "kimi-k3", "moonshot")])
    def test_provider_names(self, tmp_path, provider, model, expect):
        write_dsh(tmp_path / "dsh/p/s/session.v4.jsonl", [dsh_message("m1", provider=provider, model=model)])
        assert collect(tmp_path)["token_usage"]["by_provider"][0]["provider"] == expect

    def test_reply_text_never_leaves_the_collector(self, tmp_path):
        write_dsh(tmp_path / "dsh/p/s/session.v4.jsonl", [dsh_message("m1")])
        assert "hidden reply" not in json.dumps(collect(tmp_path))


class TestContinue:
    def test_tokens_per_model_and_provider(self, tmp_path):
        make_continue(tmp_path / "continue/devdata.sqlite", [
            ("claude-sonnet-5-5", "anthropic", 200, 1000, NOW_SQL), ("qwen2.5-coder:7b", "ollama", 30, 300, NOW_SQL)])
        m = collect(tmp_path)
        provs = {p["provider"]: p["tokens"] for p in m["token_usage"]["by_provider"]}
        assert provs == {"anthropic": 1200, "ollama": 330}
        tool = next(t for t in m["by_tool"] if t["tool"] == "continue")
        assert (tool["tokens"], tool["requests"], tool["files"]) == (1530, 2, 1)

    def test_old_rows_are_left_out(self, tmp_path):
        make_continue(tmp_path / "continue/devdata.sqlite", [("m", "openai", 5, 5, "2020-01-01 00:00:00")])
        assert collect(tmp_path)["token_usage"]["totals"]["input"] == 0

    def test_broken_database_is_reported_not_fatal(self, tmp_path):
        (tmp_path / "continue").mkdir()
        (tmp_path / "continue/devdata.sqlite").write_bytes(b"not a database")
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        m = collect(tmp_path)
        assert m["logs"]["continue"]["unreadable"] == 1 and m["token_usage"]["totals"]["requests"] == 1

    def test_database_opened_read_only(self, tmp_path):
        db = tmp_path / "continue/devdata.sqlite"
        make_continue(db, [("m", "openai", 5, 5, NOW_SQL)])
        before = db.read_bytes()
        collect(tmp_path)
        assert db.read_bytes() == before

    def test_continue_global_dir_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(tmp_path / "cg"))
        make_continue(tmp_path / "cg/dev_data/devdata.sqlite", [("m", "openai", 5, 5, NOW_SQL)])
        assert cli.collect_local_logs({"id": "x"})["logs"]["continue"]["files"] == 1


class TestByTool:
    def test_each_tool_gets_its_own_totals(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        write_dsh(tmp_path / "dsh/p/s/session.v4.jsonl", [dsh_message("d1")])
        m = collect(tmp_path)
        names = {t["name"]: (t["tokens"], t["today"]) for t in m["by_tool"]}
        assert names == {"Claude Code": (370, 370), "DeepSeek Harness": (720, 720)}

    def test_tools_with_nothing_found_are_not_listed(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        assert [t["tool"] for t in collect(tmp_path)["by_tool"]] == ["claude_code"]

    def test_note_names_what_was_read(self, tmp_path):
        write_jsonl(tmp_path / "claude/p/a.jsonl", [reply("m1")])
        write_dsh(tmp_path / "dsh/p/s/session.v4.jsonl", [dsh_message("d1")])
        assert "Claude Code (1), DeepSeek Harness (1)" in collect(tmp_path)["note"]
