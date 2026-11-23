from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "tests" / "screenshots"; SHOTS.mkdir(exist_ok=True)
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); errs = []
    pg = b.new_page(viewport={"width": 1360, "height": 900}, color_scheme="light")
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8799/?token=t"); pg.wait_for_timeout(1500)
    # the dialog closes with x, Cancel and a backdrop click
    for how in ("x", "cancel", "backdrop"):
        pg.click("#addBtn"); pg.wait_for_timeout(200)
        if how == "x": pg.click("#add .dlg-x")
        elif how == "cancel": pg.click("#aActs [data-dismiss]")
        else: pg.mouse.click(5, 5)
        pg.wait_for_timeout(200); print(f"closes with {how}:", not pg.is_visible("#add"))
    # every provider form opens, keeps its buttons on screen, and Back returns to the list
    pg.click("#addBtn"); pg.wait_for_timeout(300)
    bad = []
    for k in pg.eval_on_selector_all("[data-pick]", "els => els.map(e => e.dataset.pick)"):
        pg.click(f"[data-pick={k}]"); pg.wait_for_timeout(60)
        if not pg.is_visible("[data-save]") or not pg.is_visible("#f_label"): bad.append(k)
        pg.click("[data-back]"); pg.wait_for_timeout(60)
    print("broken provider forms:", bad)
    pg.screenshot(path=str(SHOTS / "f1-pick.png"))
    pg.click("[data-pick=deepseek]"); pg.wait_for_timeout(300)
    pg.fill("#f_label", "DeepSeek (work)"); pg.fill("#f_api_key", "sk-test-123"); pg.fill("#f_low_balance", "5")
    pg.screenshot(path=str(SHOTS / "f2-form.png"))
    pg.click("[data-save]"); pg.wait_for_timeout(2500)
    print("added widget:", pg.is_visible("#w-deepseek-work"), "dialog closed:", not pg.is_visible("#add"), "toast:", pg.inner_text("#toast"))
    pg.locator("#w-deepseek-work").screenshot(path=str(SHOTS / "f3-added.png"))
    # add claude plan -> signin dialog
    pg.click("#addBtn"); pg.wait_for_timeout(300); pg.click("[data-pick=claude_sub]"); pg.wait_for_timeout(200)
    pg.click("[data-save]"); pg.wait_for_timeout(1500)
    print("signin dialog open:", pg.is_visible("#connect"))
    pg.click("#cSave"); pg.wait_for_timeout(200)
    print("empty paste error:", pg.inner_text("#cErr"))
    pg.click("#cCancel"); pg.wait_for_timeout(800)
    # remove deepseek work
    pg.on("dialog", lambda d: d.accept())
    pg.click("#w-deepseek-work [data-remove]"); pg.wait_for_timeout(1500)
    print("removed:", not pg.is_visible("#w-deepseek-work"))
    # empty key error
    pg.click("#addBtn"); pg.wait_for_timeout(300); pg.click("[data-pick=kimi_code]"); pg.click("[data-save]"); pg.wait_for_timeout(800)
    print("error text:", pg.inner_text("#aErr"))
    print("errors:", errs); b.close()
