"""AI Meter desktop: runs AI Meter and opens it in its own window.

Double-click "AI Meter.exe" (Windows) or run `aimeter-desktop`. The dashboard
opens in an app window (Chrome, or Edge if Chrome is not installed, without the
browser bars). Closing the
window stops AI Meter. If no such browser is found, it opens in your normal
browser and a small control window lets you stop it.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess  # nosec B404: used only to open the browser window
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import cli

PORTS = range(8787, 8800)


def _answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1.5) as r:  # nosec B310: fixed http://127.0.0.1 URL
            return r.read().strip() == b"ok"
    except Exception:
        return False


def _free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> tuple[int, bool]:
    """(port, already_running). Reuses a running AI Meter instead of starting a second one."""
    for p in PORTS:
        if _answers(p):
            return p, True
        if _free(p):
            return p, False
    raise RuntimeError("ports 8787 to 8799 are all in use")


def find_app_browser() -> str | None:
    """Chrome first, then Edge (every Windows PC has Edge)."""
    cands = []
    if sys.platform == "win32":
        roots = [os.environ.get(e) for e in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        roots = [r for r in roots if r]
        cands += [Path(r, "Google/Chrome/Application/chrome.exe") for r in roots]
        cands += [Path(r, "Microsoft/Edge/Application/msedge.exe") for r in roots]
    elif sys.platform == "darwin":
        cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                  Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        cands += [Path(p) for p in filter(None, (shutil.which(n) for n in
                  ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")))]
    return next((str(c) for c in cands if c.exists()), None)


def _message(title: str, text: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, text)
        root.destroy()
    except Exception:
        logging.error("%s: %s", title, text)


def _control_window(url: str) -> None:
    """Fallback when there is no app-mode browser: a small window to reopen or stop."""
    webbrowser.open(url)
    try:
        import tkinter
        from tkinter import ttk
    except Exception:
        while True:
            time.sleep(3600)
    root = tkinter.Tk()
    root.title("AI Meter")
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=18)
    frm.grid()
    ttk.Label(frm, text="AI Meter is running", font=("Segoe UI", 12, "bold")).grid(column=0, row=0, columnspan=2, sticky="w")
    ttk.Label(frm, text=url).grid(column=0, row=1, columnspan=2, sticky="w", pady=(2, 12))
    ttk.Button(frm, text="Open dashboard", command=lambda: webbrowser.open(url)).grid(column=0, row=2, sticky="w")
    ttk.Button(frm, text="Stop AI Meter", command=root.destroy).grid(column=1, row=2, sticky="e", padx=(8, 0))
    root.mainloop()


def main() -> int:
    home = cli.default_config_path().parent
    log_path = home / "aimeter.log"
    if sys.stdout is None or sys.stderr is None:  # pythonw: no console
        f = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_path, encoding="utf-8"), logging.StreamHandler()])

    try:
        port, running = pick_port()
    except RuntimeError as e:
        _message("AI Meter could not start", str(e))
        return 1

    if not running:
        threading.Thread(target=cli.main, args=(["serve", "--host", "127.0.0.1", "--port", str(port)],),
                         name="aimeter-server", daemon=True).start()
        for _ in range(100):
            if _answers(port):
                break
            time.sleep(0.2)
        else:
            _message("AI Meter could not start", f"The dashboard did not come up. Details are in {log_path}")
            return 1

    url = f"http://localhost:{port}"
    browser = find_app_browser()
    if browser:
        started = time.monotonic()
        proc = subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={home / 'window'}",  # nosec B603: argument list, local browser and URL
                                 "--window-size=1440,960", "--no-first-run", "--no-default-browser-check"])
        if running:
            return 0  # another AI Meter owns the server; we only opened a window
        proc.wait()
        if time.monotonic() - started > 4:
            return 0  # window was closed: stop AI Meter
        logging.info("app window handed off or failed; showing the control window instead")
    if not running:
        _control_window(url)
    else:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
