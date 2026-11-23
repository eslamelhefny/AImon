from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "tests" / "screenshots"; SHOTS.mkdir(exist_ok=True)
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); errs=[]
    pg = b.new_page(viewport={"width": 1300, "height": 900}, color_scheme="dark")
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://localhost:8799/?token=t"); pg.wait_for_timeout(1200)
    pg.click("#w-chatgpt-plan [data-connect]"); pg.wait_for_timeout(600)
    print("dialog open:", pg.is_visible("#connect"))
    pg.fill("#cCode", "http://localhost:1455/auth/callback?code=abc&state=wrong")
    pg.click("#cSave"); pg.wait_for_timeout(800)
    print("error shown:", pg.inner_text("#cErr"))
    pg.screenshot(path=str(SHOTS / "v6-dialog.png"))
    pg.click("#cCancel"); pg.wait_for_timeout(300)
    pg.click("#w-kimi-code summary")  # rows open to show their details and actions
    pg.click("#w-kimi-code [data-refresh]"); pg.wait_for_timeout(1500)
    print("kimi still rendered:", pg.is_visible("#w-kimi-code .win"))
    print("check button restored:", pg.inner_text("#w-kimi-code [data-refresh]") == "Check now")
    print("errors:", errs); b.close()
