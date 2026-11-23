"""UTF-8 everywhere (the Windows garbled-text bug) and static checks on dashboard.html."""
import re
import shutil
import subprocess

import pytest

from conftest import cli

PAGE = (cli.HERE / "dashboard.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- reading files as UTF-8

@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_config_with_non_ascii_and_optional_bom(tmp_path, bom):
    p = tmp_path / "config.yaml"
    p.write_bytes(bom + "subscriptions:\n  - id: s\n    name: \"Café – plan…\"\n".encode("utf-8"))
    assert cli.load_config(str(p))["subscriptions"][0]["name"] == "Café – plan…"


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_env_file_with_bom_and_quotes(tmp_path, monkeypatch, bom):
    monkeypatch.delenv("AIM_TEST_A", raising=False)
    monkeypatch.delenv("AIM_TEST_B", raising=False)
    (tmp_path / ".env").write_bytes(bom + "AIM_TEST_A=\"väl–ue\"\n# comment\n\nAIM_TEST_B='x=y'\nnot a line\n".encode("utf-8"))
    cli.load_env_file(tmp_path / ".env")
    import os
    assert os.environ["AIM_TEST_A"] == "väl–ue" and os.environ["AIM_TEST_B"] == "x=y"


def test_env_file_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIM_TEST_C", "real")
    (tmp_path / ".env").write_text("AIM_TEST_C=fromfile\n", encoding="utf-8")
    cli.load_env_file(tmp_path / ".env")
    import os
    assert os.environ["AIM_TEST_C"] == "real"


def test_first_run_creates_starter_config_as_utf8(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMETER_HOME", str(tmp_path / "home"))
    p = cli.default_config_path()
    assert p.exists()
    assert p.read_text(encoding="utf-8") == (cli.HERE / "config.starter.yaml").read_text(encoding="utf-8")
    cli.load_config(str(p))  # and it loads


def test_old_aimon_home_still_works(tmp_path, monkeypatch):
    monkeypatch.delenv("AIMETER_HOME", raising=False)
    monkeypatch.setenv("AIMON_HOME", str(tmp_path / "old"))
    assert cli.default_config_path().parent == tmp_path / "old"


# ---------------------------------------------------------------- dashboard.html

def test_page_is_clean_utf8():
    raw = (cli.HERE / "dashboard.html").read_bytes()
    raw.decode("utf-8")  # raises if not valid UTF-8
    assert b"\xc3\xa2\xc2\x80" not in raw and "â€" not in PAGE  # no double-encoded text
    assert '<meta charset="utf-8">' in PAGE


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_page_script_parses(tmp_path):
    script = PAGE[PAGE.index("<script>") + 8:PAGE.rindex("</script>")]
    f = tmp_path / "page.js"
    f.write_text(script, encoding="utf-8")
    r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_browser_alert_popups():
    assert not re.search(r"\balert\(", PAGE.split("<script>")[1])


def test_dialogs_can_always_be_closed():
    for dlg in re.findall(r'<dialog id="(\w+)".*?</dialog>', PAGE, re.S):
        block = PAGE[PAGE.index(f'<dialog id="{dlg}"'):]
        block = block[:block.index("</dialog>")]
        assert 'class="dlg-x"' in block and "data-dismiss" in block, dlg


def test_every_jump_link_has_a_target():
    for target in re.findall(r'<nav class="jump".*?</nav>', PAGE, re.S)[0].split('href="#')[1:]:
        assert f'id="{target.split(chr(34))[0]}"' in PAGE


def test_ids_used_by_the_ui_tests_exist():
    for sel in ("addBtn", "refreshAll", "add", "connect", "cCancel", "cSave", "cCode", "cErr", "aErr", "aActs",
                "toast", "logform", "logid", "alerts", "subs", "hotn"):
        assert f'id="{sel}"' in PAGE, sel


def test_every_catalog_provider_has_a_color():
    colors = re.search(r"const COLORS = \{(.*?)\};", PAGE, re.S).group(1)
    missing = [p for p in cli.CATALOG if p not in ("manual",) and not re.search(rf"\b{p}:", colors)]
    assert not missing, f"no card color for {missing}"


def test_writing_style_no_em_dash_in_ui_copy():
    assert "—" not in PAGE  # house style: no em dashes
