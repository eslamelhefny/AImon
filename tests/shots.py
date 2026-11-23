import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "tests" / "screenshots"; SHOTS.mkdir(exist_ok=True)
from playwright.sync_api import sync_playwright
tag = sys.argv[1] if len(sys.argv) > 1 else "v1"
with sync_playwright() as p:
    b = p.chromium.launch()
    errs = []
    for scheme, w, h, name in (("dark", 1400, 1000, "desk-dark"), ("light", 1400, 1000, "desk-light"), ("dark", 390, 844, "phone")):
        pg = b.new_page(viewport={"width": w, "height": h}, color_scheme=scheme, device_scale_factor=1)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("console", lambda m: errs.append("console:" + m.text) if m.type == "error" else None)
        pg.goto("http://localhost:8799/?token=t"); pg.wait_for_timeout(1500)
        pg.screenshot(path=str(SHOTS / f"{tag}-{name}.png"), full_page=True)
        # overflow check: the page must not scroll sideways. Elements inside their own
        # scroll box (the section bar, wide tables) are allowed past the edge.
        ov = pg.evaluate("""() => {
          const inScroller = e => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) {
            const o = getComputedStyle(p).overflowX; if (o === 'auto' || o === 'scroll' || o === 'hidden') return true; } return false; };
          const wide = [...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right > innerWidth + 1 && !inScroller(e));
          return {pageScrollsSideways: document.documentElement.scrollWidth > innerWidth,
                  offenders: wide.map(e => e.tagName + '.' + e.className).slice(0, 8)}; }""")
        print(name, "overflow:", ov)
        pg.close()
    print("errors:", errs)
    b.close()
