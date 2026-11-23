"""GitHub Copilot premium requests: the quota endpoint first, the billing report as a fallback."""
import pytest

from conftest import Resp, cli

ACCT = {"id": "copilot", "label": "Copilot", "provider": "copilot", "api_key_env": "GH_TEST_TOKEN"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("GH_TEST_TOKEN", "ghp_testtoken")


def internal(premium=None, chat=None, completions=None, reset="2026-12-01", plan="individual_pro"):
    snaps = {}
    for k, v in (("premium_interactions", premium), ("chat", chat), ("completions", completions)):
        if v is not None:
            snaps[k] = v
    return Resp({"login": "octo", "copilot_plan": plan, "quota_reset_date": reset, "quota_snapshots": snaps})


class TestQuotaEndpoint:
    def test_premium_requests_with_limit_and_reset(self, http):
        http.on("GET", "copilot_internal/user", internal(
            premium={"entitlement": 300, "remaining": 180, "percent_remaining": 60.0, "unlimited": False},
            chat={"unlimited": True}, completions={"unlimited": True}))
        m = cli.collect_copilot(ACCT)
        (w,) = m["windows"]
        assert (w["label"], w["used_pct"], w["limit"], w["remaining"], w["unit"]) == ("Premium requests", 40.0, 300, 180, "requests")
        assert w["resets_at"] == "2026-12-01T00:00:00Z"
        assert m["plan"] == "individual_pro" and m["source"] == "copilot" and m["account"] == "octo"
        h = http.gets("copilot_internal")[0][2]["headers"]
        assert h["Authorization"] == "token ghp_testtoken"

    def test_percent_worked_out_when_not_given(self, http):
        http.on("GET", "copilot_internal/user", internal(premium={"entitlement": 50, "remaining": 5, "unlimited": False}))
        assert cli.collect_copilot(ACCT)["windows"][0]["used_pct"] == 90.0

    def test_overage_shows_as_over_limit(self, http):
        http.on("GET", "copilot_internal/user", internal(premium={"entitlement": 300, "remaining": 0, "percent_remaining": 0,
                                                                   "overage_count": 12, "unlimited": False}))
        assert cli.collect_copilot(ACCT)["windows"][0]["used_pct"] >= 100

    def test_free_plan_chat_and_completion_limits(self, http):
        http.on("GET", "copilot_internal/user", internal(
            premium={"entitlement": 50, "remaining": 50, "percent_remaining": 100, "unlimited": False},
            chat={"entitlement": 50, "remaining": 20, "percent_remaining": 40, "unlimited": False},
            completions={"entitlement": 2000, "remaining": 1500, "percent_remaining": 75, "unlimited": False}))
        assert [(w["label"], w["used_pct"]) for w in cli.collect_copilot(ACCT)["windows"]] == [
            ("Premium requests", 0.0), ("Chat", 60.0), ("Code completions", 25.0)]

    def test_all_unlimited(self, http):
        http.on("GET", "copilot_internal/user", internal(premium={"unlimited": True}))
        m = cli.collect_copilot(ACCT)
        assert m["windows"] == [] and "no limited quotas" in m["note"]

    def test_rejected_token_means_add_a_new_one(self, http):
        http.on("GET", "copilot_internal/user", Resp({}, status=401))
        with pytest.raises(cli.NotConnected, match="rejected the token"):
            cli.collect_copilot(ACCT)

    def test_missing_token(self, http, monkeypatch):
        monkeypatch.delenv("GH_TEST_TOKEN")
        with pytest.raises(RuntimeError, match="Add a GitHub token"):
            cli.collect_copilot(ACCT)


def billing(http, used=(30, 12.5), status=200):
    http.on("GET", "copilot_internal/user", Resp({"message": "Forbidden"}, status=403))
    http.on("GET", "api.github.com/user", Resp({"login": "octo"}))
    http.on("GET", "premium_request/usage", Resp({"usageItems": [
        {"product": "Copilot", "model": "Claude Sonnet", "grossQuantity": q} for q in used]}, status=status))


class TestBillingFallback:
    def test_used_from_billing_with_plan_allowance(self, http):
        billing(http)
        m = cli.collect_copilot(dict(ACCT, plan="pro"))
        (w,) = m["windows"]
        assert (w["used_pct"], w["limit"], w["remaining"]) == (round(42.5 / 300 * 100, 1), 300, 257.5)
        assert m["source"] == "billing" and m["premium_used"] == 42.5 and "HTTP 403" in m["note"]
        assert http.gets("premium_request/usage")[0][2]["headers"]["Authorization"] == "Bearer ghp_testtoken"

    def test_custom_allowance_wins(self, http):
        billing(http, used=(100,))
        assert cli.collect_copilot(dict(ACCT, plan="pro", monthly_allowance=1000))["windows"][0]["used_pct"] == 10.0

    def test_unknown_plan_shows_usage_but_no_percentage(self, http):
        billing(http)
        m = cli.collect_copilot(ACCT)
        assert m["windows"] == [] and m["premium_used"] == 42.5 and "Set your plan" in m["note"]

    def test_internal_endpoint_unreachable_also_falls_back(self, http):
        http.on("GET", "copilot_internal/user", cli.requests.ConnectionError("blocked"))
        http.on("GET", "api.github.com/user", Resp({"login": "octo"}))
        http.on("GET", "premium_request/usage", Resp({"usageItems": []}))
        assert cli.collect_copilot(dict(ACCT, plan="pro"))["windows"][0]["used_pct"] == 0.0

    def test_token_without_billing_access_explains_what_to_do(self, http):
        billing(http, status=403)
        with pytest.raises(RuntimeError, match="Plan permission"):
            cli.collect_copilot(dict(ACCT, plan="pro"))


def test_can_be_connected_from_the_page(cfg, con):
    acct = cli.add_ui_account(cfg, con, {"provider": "copilot", "api_key": "ghp_x", "plan": "pro", "monthly_allowance": "300"})
    assert acct["monthly_allowance"] == 300.0 and acct["plan"] == "pro"
    assert cli.kv_get(con, f"secret:{acct['id']}:api_key") == "ghp_x"
