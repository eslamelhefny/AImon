# AI Meter

Self-hosted monitor for AI plan limits, reset times, tokens and cost. One Python app serves a dashboard;
the same code ships as a PyPI package (`ai-meter`), a Docker service on the owner's VPS, a Windows app
(`AI Meter.exe`) and a VS Code extension that reads from a running AI Meter. Formerly called "aimon".

## Layout
- `src/ai_meter/cli.py` is the whole backend (single file, stdlib + requests + PyYAML): config, SQLite
  storage, one collector per provider, aggregation (`build_state`), alerts, OAuth sign-in, HTTP server.
- `src/ai_meter/dashboard.html` is the whole frontend (vanilla JS, no build step). It reads `/api/state`.
- `src/ai_meter/desktop.py` opens the dashboard in an app window and stops the server when it closes.
- `docker/`, `deploy/` (systemd), `windows/` (C launcher), `vscode-extension/` (plain JS, no build), `brand/`.
- `scripts/build_*.sh` produce every release file into `dist/`.

## Run and test
- Run: `pip install -e .` then `aimeter` (dashboard at http://localhost:8787, data in `~/.aimeter`).
- Automated tests: `pip install pytest` then `python -m pytest` (offline, about 25 s). `test_oauth.py` covers
  sign-in (it pins the Claude Code authorize URL, parameter order, scopes and state length),
  `test_providers.py` connect/remove for every catalog entry, `test_server.py` the HTTP API the page uses,
  `test_encoding_and_page.py` UTF-8 handling and static checks on the dashboard, `test_local_logs.py` local
  log tokens, balance spend and totals, `test_security.py` access control, CSRF, DNS rebinding, limits, secrets,
  headers and script injection (uses Playwright if installed). `requests` is faked in `conftest.py`.
- Before any release: build, then `python -m pytest` (`test_release_privacy.py` scans dist/ and the .vsix for the
  builder's user name or home path, key/token shapes, private files, pip's `direct_url.json`, stray `.pyc`, and
  bundled pip), `python -m bandit -r src/ai_meter` (expect 0 issues; reviewed lines carry `# nosec` with a reason)
  and `python -m pip_audit -r requirements.txt`. The Windows bundle ships without pip (it had known CVEs).
- Demo with every provider and tool connected (fake data only, for screenshots and product images):
  `python tests/serve_demo.py`, then http://localhost:8800/?token=demo.
- Mock everything: `python tests/mock2.py` (fake providers + fake Hermes DB, prints collector results);
  `python tests/serve_mock.py` serves the dashboard with mocks on port 8799, token `t`.
- UI checks: `python tests/shots.py`, `tests/flow.py`, `tests/interact.py` (Playwright; screenshots go to
  `tests/screenshots/`). After any dashboard change, take desktop light, desktop dark and phone screenshots
  and check for horizontal overflow and console errors.

## Providers and how each is read
API keys come from env vars named in config (`api_key_env`) or are stored in the DB when added from the page.
- Official APIs: Anthropic Admin `cost_report`/`usage_report`; OpenAI org costs/usage; OpenRouter `/key`,
  `/credits`, `/activity` (needs a management key); DeepSeek `/user/balance`; Moonshot `/v1/users/me/balance`;
  rate-limit headers from Anthropic (`anthropic-ratelimit-*`) and OpenAI (`x-ratelimit-*`).
- Unofficial (the endpoints the vendors' own apps use; may change without notice, label them as such):
  Claude plan `api.anthropic.com/api/oauth/usage` (header `anthropic-beta: oauth-2025-04-20`), Codex
  `chatgpt.com/backend-api/wham/usage`, Kimi Code `api.kimi.com/coding/v1/usages`, OpenCode Go
  `opencode.ai/zen/go/v1/usage`.
- Sign-in (PKCE, paste-back): Claude uses Claude Code's client id and `platform.claude.com/oauth/code/callback`;
  ChatGPT uses Codex's client id and `localhost:1455/auth/callback` (the user pastes the failed page's URL).
- Hermes Agent: reads `sessions` from its `state.db` read-only.
- Coding tools on this computer (`local_logs`): reads Claude Code (`~/.claude/projects/**/*.jsonl`, or
  `$CLAUDE_CONFIG_DIR`) and Codex (`~/.codex/sessions/**/*.jsonl[.zst]`, or `$CODEX_HOME`) logs read-only.
  Claude Code repeats a reply's `message.id` once per content block: count each id once. Its `cost-state` lines
  are a running per-session total with `costUSD` per model (API-price estimate, also covers side calls).
  Codex `token_usage_record` has a `response_id`; its `input_tokens` includes `cached_input_tokens`. Not public
  formats (checked 2026-11-19 on Claude Code 2.1.325, Codex 0.167). `.zst` needs the optional `zstandard`.
  Codex `event_msg/token_count` also carries `rate_limits` (ChatGPT plan %, `window_minutes`, `resets_at`): the
  ChatGPT card uses them when there is no newer live reading (`limits_from_logs`).
  Qwen Code: `~/.qwen/projects/*/chats/*.jsonl` (`$QWEN_DATA_DIR`), `type: assistant` lines with `usageMetadata`
  (`promptTokenCount` includes `cachedContentTokenCount`; thoughts billed as output). Format taken from ccusage
  and CodeBurn, not yet checked on a live install. Qwen Code's free OAuth tier ended 2026-04-15.
- ChatGPT sign-in: while the dialog is open AI Meter listens on 127.0.0.1:1455 (Codex's redirect) for up to
  10 minutes, accepts only `/auth/callback` with the pending `state` and a localhost Host, then stops.
  Port busy (Codex CLI signing in) or AI Meter remote: the paste-back box still works.
- DeepSeek has no token-usage API (only `/user/balance`). Prepaid spend (DeepSeek, Kimi API) is worked out from
  balance drops between checks; rises are top-ups.
- Totals deduplicate overlapping sources: Hermes and local logs add up (different tools); per provider, take the
  larger of that sum and the provider's own report. Estimated costs (Hermes, local logs) never count as money spent.

## Security model (tests in `tests/test_security.py`)
- No token: only `localhost`/`127.0.0.1`/`[::1]` (plus `settings.dashboard.allowed_hosts`) Host names are
  answered, against DNS rebinding. With a token, any host (reverse proxy) but the token is required.
- POST must be `Content-Type: application/json` and same-origin (Origin / Sec-Fetch-Site), against CSRF.
  Any client that POSTs (page, VS Code extension) must send JSON.
- Bodies over 1 MB are refused; errors are passed through `redact()` before being stored, logged or returned.
- Page headers: CSP, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`, `no-store`.

## Known issues
- Claude sign-in "Authorization failed / Invalid request format": fixed 2026-11-19. Claude Code 2.1.325 moved
  the authorize page to `https://claude.com/cai/oauth/authorize` and requests `code=true` first plus a wider scope
  list; `OAUTH["claude_sub"]` now matches it. `state` is 32 random bytes (43 chars) like Claude Code; the old
  16-byte state still failed after log-in. The new URL reaches the claude.ai log-in page; a full sign-in with a
  real account has not been completed yet. If it breaks again, read the values from the installed Claude Code
  binary (search it for the client id `9d1c250a-...`).
- The Windows app has not yet been run on real Windows (it was built and checked on Linux; Wine crashed there).

## Conventions
- Verify every provider fact against a current source before relying on it; these endpoints move.
- Never log or return API keys to the browser; secrets stay in the DB (chmod 600) or env.
- Writing style for docs and UI copy: plain sentence case; no em dashes (use commas, colons or parentheses);
  "program", not "programme".
- Keep backward compatibility with `AIMON_*` env vars and `~/.aimon`.

## Deployment
See `docs/vps.md`, with the real server details in the git-ignored `docs/vps.local.md` (Hostinger VPS, Docker Manager project `aimon`, code pulled from GitHub on each start).
