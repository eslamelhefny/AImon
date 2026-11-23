# Changelog

## Unreleased
- Overview as widgets: six KPI tiles with mini charts (tokens, value at API prices, today, money spent, limits above 80%, balances), a wall of arc gauges for every limit, a provider table with 14-day sparklines and week-on-week change, and donuts for each tool's share.
- Run-out forecast: each limit says when it runs out at the pace of the last 6 hours, or that it lasts until the reset. Limits under 50% are shown quieter; warnings start at 80% and turn red at 90%.
- Redesigned dashboard: one overview screen with a limits board (most urgent first, live countdowns), tokens by tool and money; a "Found on this computer" strip; providers as one expandable row each. Archivo type, cool slate colors, one meter style.
- New sources: DeepSeek Harness and Continue tokens, GitHub Copilot premium requests, local models (Ollama, LM Studio). AI Meter detects installed AI tools and offers to connect them.
- `tests/serve_demo.py`: a fake-data demo with everything connected.
- ChatGPT limits without signing in: read from the Codex logs on this computer (used when there is no live reading, or the logs are newer).
- ChatGPT sign-in finishes by itself: AI Meter listens on localhost:1455 while the sign-in dialog is open, checks the sign-in belongs to it, and closes the dialog. Pasting the address still works when the port is busy or AI Meter runs elsewhere.
- Qwen Code: tokens from its chat logs (`~/.qwen/projects/*/chats`, or `$QWEN_DATA_DIR`).
- Windows with no recorded tokens no longer claim "0 tokens".
- New source "Coding tools on this computer": tokens per provider, model and hour from Claude Code and Codex logs, with Claude Code's cost estimate. Plan cards show the tokens used in each window.
- DeepSeek and Kimi API: money spent is worked out from drops in the prepaid balance.
- Security: answers only local host names when no token is set (DNS rebinding), accepts only same-origin JSON for changes (CSRF), refuses bodies over 1 MB and malformed lengths, removes keys from stored and logged errors, and sends CSP, frame and referrer headers. The VS Code extension sends JSON for "check now".
- ChatGPT sign-in uses the parameter order that worked before the Claude fix.
- Fixed Claude sign-in ("Authorization failed / Invalid request format"): uses Claude Code's current sign-in page (claude.com/cai) and scopes.
- ChatGPT sign-in: the paste box shows an example of the address to copy.
- Automated test suite (`python -m pytest`, 159 tests, offline) for sign-in, connecting and removing providers, the page's API and encoding.
- Fixed: a provider named with only spaces got an empty title; it now uses the provider's name.
- Fixed: a hand-entered reading that is not a number dropped the connection; it now returns a clear error.
- Fixed garbled characters (for example "â€"" in place of "–") on Windows: config, `.env` and the page are read as UTF-8.
- Section bar at the top of the page that stays visible while scrolling, with a count of limits above 80%.
- Dialogs: close button, Cancel always visible, close on backdrop click; fit on phone screens.
- Short messages after checks, connects, removals and saved readings; failures are shown instead of ignored.
- Connecting a provider whose first check fails now says so, with the reason.
- Readings over 100% are labelled "Over limit" in words, not only in red.
- Opened "Cost by model" sections and keyboard focus survive the 15-second refresh.
- Modules keep their config order across columns.
- Daily chart has token (left) and cost (right) scales.
- "Add a reading" button on manual providers jumps to the form with that service selected.
- Larger touch targets on touch screens.

## 0.3.2
- Windows app: `AI Meter.exe` opens AI Meter in its own window (Chrome, or Edge if Chrome is not installed); closing the window stops it.
- `aimeter-desktop` command for the same window on any system.
- Claude sign-in uses the current Claude Code values (platform.claude.com callback, wider scopes).

## 0.3.1
- Renamed from aimon to AI Meter, with a logo. `AIMON_*` settings and `~/.aimon` still work.
- VS Code extension: status bar and side panel for plan limits.

## 0.3.0
- Python package: `pipx install ai-meter`, then `aimeter`.
- Providers can be connected from the page; background checks run inside `aimeter serve`.
