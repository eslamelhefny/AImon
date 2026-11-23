"""Connecting and removing providers from the page (add_ui_account / remove_ui_account), every catalog entry."""
import json

import pytest

from conftest import cli

KEYED = sorted(k for k, v in cli.CATALOG.items() if any(f[0] == "api_key" for f in v["fields"]))
SIGNIN = sorted(k for k, v in cli.CATALOG.items() if v.get("signin"))
OTHER = sorted(set(cli.CATALOG) - set(KEYED) - set(SIGNIN))


def body_for(prov, **over):
    b = {"provider": prov}
    for f in cli.CATALOG[prov]["fields"]:
        if f[0] == "api_key":
            b["api_key"] = "sk-test-key-123"
    b.update(over)
    return b


def test_catalog_is_fully_covered():
    assert set(KEYED) | set(SIGNIN) | set(OTHER) == set(cli.CATALOG)
    assert SIGNIN == ["claude_sub", "codex_sub"]
    for prov in cli.CATALOG:
        assert prov in cli.COLLECTORS, f"{prov} can be added but has no collector"


@pytest.mark.parametrize("prov", sorted(cli.CATALOG))
def test_every_provider_can_be_added(cfg, con, prov):
    acct = cli.add_ui_account(cfg, con, body_for(prov))
    assert acct["provider"] == prov and acct["label"] == cli.CATALOG[prov]["name"]
    assert any(a["id"] == acct["id"] and a.get("_ui") for a in cfg["api_accounts"])
    assert [a["id"] for a in cli.kv_get(con, "ui_accounts")] == [acct["id"]]


@pytest.mark.parametrize("prov", KEYED)
def test_key_is_required(cfg, con, prov):
    for empty in (None, "", "   "):
        b = body_for(prov)
        b["api_key"] = empty
        with pytest.raises(RuntimeError, match="Paste the API key first"):
            cli.add_ui_account(cfg, con, b)
    assert cli.kv_get(con, "ui_accounts") is None  # nothing half-saved


@pytest.mark.parametrize("prov", KEYED)
def test_key_is_stored_as_a_secret_not_in_the_account(cfg, con, prov):
    acct = cli.add_ui_account(cfg, con, body_for(prov, api_key="  sk-secret-xyz  "))
    assert cli.kv_get(con, f"secret:{acct['id']}:api_key") == "sk-secret-xyz"  # trimmed
    assert "sk-secret-xyz" not in json.dumps(cli.kv_get(con, "ui_accounts"))
    merged = next(a for a in cfg["api_accounts"] if a["id"] == acct["id"])
    assert cli.env(merged["api_key_env"]) == "sk-secret-xyz"


def test_optional_management_key_is_also_secret(cfg, con):
    acct = cli.add_ui_account(cfg, con, body_for("openrouter", management_key="mgmt-1"))
    assert cli.kv_get(con, f"secret:{acct['id']}:management_key") == "mgmt-1"
    merged = next(a for a in cfg["api_accounts"] if a["id"] == acct["id"])
    assert cli.env(merged["management_key_env"]) == "mgmt-1"


def test_unknown_provider_is_rejected(cfg, con):
    for prov in (None, "", "nope"):
        with pytest.raises(RuntimeError, match="Unknown provider"):
            cli.add_ui_account(cfg, con, {"provider": prov})


@pytest.mark.parametrize("field,prov", [("monthly_budget", "openrouter"), ("low_balance", "deepseek")])
def test_number_fields(cfg, con, field, prov):
    acct = cli.add_ui_account(cfg, con, body_for(prov, **{field: "12.5"}))
    assert acct[field] == 12.5
    with pytest.raises(RuntimeError, match="must be a number"):
        cli.add_ui_account(cfg, con, body_for(prov, **{field: "ten"}))


def test_defaults_fill_empty_optional_fields(cfg, con):
    acct = cli.add_ui_account(cfg, con, body_for("anthropic_limits"))
    assert acct["probe_model"] == "claude-haiku-5-5"
    acct = cli.add_ui_account(cfg, con, body_for("anthropic_limits", probe_model="claude-opus-5-5"))
    assert acct["probe_model"] == "claude-opus-5-5"


class TestNamesAndIds:
    def test_label_becomes_id(self, cfg, con):
        assert cli.add_ui_account(cfg, con, body_for("deepseek", label="DeepSeek (work)"))["id"] == "deepseek-work"

    def test_same_name_twice_gets_numbered_ids(self, cfg, con):
        ids = [cli.add_ui_account(cfg, con, body_for("openrouter", label="OR"))["id"] for _ in range(3)]
        assert ids == ["or", "or-2", "or-3"]

    def test_id_never_clashes_with_config_accounts(self, cfg, con):
        cfg["api_accounts"].append({"id": "deepseek", "provider": "deepseek"})
        assert cli.add_ui_account(cfg, con, body_for("deepseek"))["id"] == "deepseek-2"

    def test_blank_label_uses_catalog_name(self, cfg, con):
        assert cli.add_ui_account(cfg, con, body_for("kimi_code", label="  "))["label"] == "Kimi Code"

    def test_symbols_only_label_falls_back_to_provider(self, cfg, con):
        assert cli.add_ui_account(cfg, con, body_for("deepseek", label="!!!"))["id"] == "deepseek"

    def test_non_ascii_label_kept(self, cfg, con):
        acct = cli.add_ui_account(cfg, con, body_for("deepseek", label="مفتاح DeepSeek"))
        assert acct["label"] == "مفتاح DeepSeek" and acct["id"] == "deepseek"

    def test_long_label_is_cut_to_60(self, cfg, con):
        assert len(cli.add_ui_account(cfg, con, body_for("deepseek", label="x" * 200))["label"]) == 60


class TestRemove:
    def test_remove_deletes_account_secrets_login_and_history(self, cfg, con):
        acct = cli.add_ui_account(cfg, con, body_for("openrouter", management_key="m"))
        aid = acct["id"]
        cli.kv_set(con, f"oauth:{aid}", {"access_token": "t"})
        con.execute("INSERT INTO snapshots(ts, account_id, ok) VALUES ('2026-01-01T00:00:00Z', ?, 1)", (aid,))
        con.commit()
        cli.remove_ui_account(cfg, con, aid)
        assert cli.kv_get(con, "ui_accounts") == []
        assert cli.kv_get(con, f"secret:{aid}:api_key") is None
        assert cli.kv_get(con, f"secret:{aid}:management_key") is None
        assert cli.kv_get(con, f"oauth:{aid}") is None
        assert con.execute("SELECT COUNT(*) FROM snapshots WHERE account_id=?", (aid,)).fetchone()[0] == 0
        assert all(a["id"] != aid for a in cfg["api_accounts"])

    def test_remove_one_keeps_the_others(self, cfg, con):
        a = cli.add_ui_account(cfg, con, body_for("deepseek", label="A"))
        b = cli.add_ui_account(cfg, con, body_for("deepseek", label="B"))
        cli.remove_ui_account(cfg, con, a["id"])
        assert [x["id"] for x in cli.kv_get(con, "ui_accounts")] == [b["id"]]
        assert cli.kv_get(con, f"secret:{b['id']}:api_key") == "sk-test-key-123"

    def test_removed_id_can_be_reused(self, cfg, con):
        a = cli.add_ui_account(cfg, con, body_for("deepseek"))
        cli.remove_ui_account(cfg, con, a["id"])
        assert cli.add_ui_account(cfg, con, body_for("deepseek"))["id"] == a["id"]


def test_accounts_survive_a_restart(cfg, con, tmp_path):
    acct = cli.add_ui_account(cfg, con, body_for("deepseek", label="Kept"))
    fresh = cli.load_config(str(tmp_path / "config.yaml"))
    cli.merge_db_accounts(fresh)
    merged = next(a for a in fresh["api_accounts"] if a["id"] == acct["id"])
    assert merged["_ui"] and cli.env(merged["api_key_env"]) == "sk-test-key-123"


def test_state_never_contains_secrets(cfg, con):
    cli.add_ui_account(cfg, con, body_for("openrouter", api_key="sk-LEAK-1", management_key="sk-LEAK-2"))
    cli.kv_set(con, "oauth:x", {"access_token": "sk-LEAK-3", "refresh_token": "sk-LEAK-4"})
    out = json.dumps(cli.build_state(cfg))
    assert "sk-LEAK" not in out
