// AI Meter for VS Code: reads /api/state from a running AI Meter and shows it.
const vscode = require("vscode");

const MIN_WINDOW = 3600; // seconds; shorter windows are per-minute rate limits
let state = null, lastError = null, timer = null, statusItem, provider, ctx;

const cfg = () => vscode.workspace.getConfiguration("aimeter");
const base = () => String(cfg().get("serverUrl") || "http://localhost:8787").replace(/\/+$/, "");

async function api(path, method = "GET") {
  const headers = { Accept: "application/json" };
  const token = await ctx.secrets.get("aimeter.token");
  if (token) headers.Authorization = "Bearer " + token;
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), 15000);
  try {
    const opts = { method, headers, signal: ctrl.signal };
    if (method !== "GET") {  // AI Meter only accepts JSON for changes (protects against cross-site requests)
      headers["Content-Type"] = "application/json";
      opts.body = "{}";
    }
    const r = await fetch(base() + path, opts);
    if (r.status === 401) throw Object.assign(new Error("AI Meter asked for a token."), { code: "auth" });
    if (!r.ok) throw new Error(`AI Meter answered ${r.status}.`);
    return await r.json();
  } catch (e) {
    if (e.code === "auth") throw e;
    if (e.name === "AbortError") throw new Error(`No answer from ${base()} within 15 seconds.`);
    if (/fetch failed|ECONNREFUSED/i.test(String(e.message) + String(e.cause))) throw Object.assign(new Error(`Can't reach AI Meter at ${base()}.`), { code: "down" });
    throw e;
  } finally { clearTimeout(t); }
}

// Every limit with a reset time, flattened and sorted tightest first.
function limits(st, withRate) {
  const out = [];
  for (const a of st?.apis || []) {
    if (a.error || a.not_connected) continue;
    for (const w of a.metrics?.windows || []) {
      if (w.used_pct == null) continue;
      const short = (w.window_seconds || 0) && w.window_seconds < MIN_WINDOW;
      const perMin = /\/ ?min/i.test(w.label || "");
      if (!withRate && (short || perMin || a.kind === "ratelimit")) continue;
      out.push({ provider: a.label, plan: a.plan, kind: a.kind, label: w.label, pct: w.used_pct, resets: w.resets_at,
        tokens: w.tokens, cost: w.cost, unit: w.unit, url: a.console_url, key: `${a.id}:${w.key || w.label}` });
    }
  }
  return out.sort((x, y) => y.pct - x.pct);
}

const fmtTok = n => n == null ? "" : n >= 1e9 ? (n / 1e9).toFixed(2) + "B" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? Math.round(n / 1e3) + "k" : String(n);
const fmtUsd = n => n == null ? "" : "$" + (n >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
function until(iso) {
  if (!iso) return "";
  let s = Math.max(0, (Date.parse(iso) - Date.now()) / 1000);
  if (s < 60) return "under a minute";
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

function renderStatus() {
  if (cfg().get("statusBar") === "off") { statusItem.hide(); return; }
  statusItem.backgroundColor = undefined;
  if (lastError) {
    statusItem.text = "$(dashboard) AI Meter offline";
    statusItem.tooltip = lastError.message + (lastError.code === "auth" ? " Run “AI Meter: Set dashboard token”." : " Click to change the address.");
    statusItem.command = lastError.code === "auth" ? "aimeter.setToken" : "aimeter.setServer";
    statusItem.show(); return;
  }
  const all = limits(state, false), plans = all.filter(l => l.kind === "plan");
  const top = (plans.length ? plans : all)[0];
  if (!top) {
    statusItem.text = "$(dashboard) AI Meter";
    statusItem.tooltip = "No limits with reset times yet. Connect Claude, ChatGPT, Kimi Code or OpenCode Go in the dashboard.";
  } else {
    statusItem.text = `$(dashboard) ${top.provider} ${Math.round(top.pct)}%` + (top.resets ? ` · ${until(top.resets)}` : "");
    if (top.pct >= 95) statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    else if (top.pct >= 80) statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    const md = new vscode.MarkdownString(undefined, true);
    md.appendMarkdown(`**AI Meter**\n\n| Limit | Used | Back in |\n|:--|--:|--:|\n`);
    for (const l of [...plans, ...all.filter(l => l.kind !== "plan")].slice(0, 8))
      md.appendMarkdown(`| ${l.provider}: ${l.label} | ${Math.round(l.pct)}% | ${l.resets ? until(l.resets) : ""} |\n`);
    const tt = state.token_total;
    if (tt) md.appendMarkdown(`\nToday ${fmtTok(tt.today)} tokens, ${fmtUsd(tt.today_cost)} · Month ${fmtTok(tt.tokens)} tokens, ${fmtUsd(tt.cost)}`);
    statusItem.tooltip = md;
  }
  statusItem.command = "aimeter.focus";
  statusItem.show();
}

async function maybeNotify() {
  const at = cfg().get("notifyAt") ?? 90;
  const seen = ctx.globalState.get("aimeter.notified", {});
  for (const l of limits(state, false)) {
    if (l.pct < at || l.kind !== "plan") continue;
    const id = `${l.key}@${l.resets || ""}`;
    if (seen[id]) continue;
    seen[id] = Date.now();
    const back = l.resets ? ` It comes back in ${until(l.resets)}.` : "";
    vscode.window.showWarningMessage(`${l.provider}: ${l.label} is at ${Math.round(l.pct)}%.${back}`, "Open panel")
      .then(c => c && vscode.commands.executeCommand("aimeter.focus"));
  }
  const week = Date.now() - 8 * 86400e3;
  for (const k of Object.keys(seen)) if (seen[k] < week) delete seen[k];
  await ctx.globalState.update("aimeter.notified", seen);
}

async function refresh() {
  try { state = await api("/api/state"); lastError = null; await maybeNotify(); }
  catch (e) { lastError = e; }
  renderStatus(); provider?.post();
}

function schedule() {
  clearInterval(timer);
  timer = setInterval(refresh, Math.max(15, cfg().get("refreshSeconds") || 60) * 1000);
}

class Panel {
  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = html(view.webview);
    view.webview.onDidReceiveMessage(m => {
      if (m.cmd === "open") vscode.env.openExternal(vscode.Uri.parse(m.url || base()));
      else if (m.cmd) vscode.commands.executeCommand("aimeter." + m.cmd);
    });
    view.onDidChangeVisibility(() => view.visible && this.post());
    this.post();
  }
  post() {
    if (!this.view) return;
    this.view.webview.postMessage({
      error: lastError && { message: lastError.message, code: lastError.code || "" }, server: base(),
      limits: state ? limits(state, cfg().get("includeRateLimits")) : [], total: state?.token_total || null,
      spend: state?.totals || null, updated: state?.generated || null,
    });
  }
}

function html(webview) {
  const nonce = Math.random().toString(36).slice(2);
  return `<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';">
<style>
  body { padding: 4px 14px 16px; color: var(--vscode-foreground); font: var(--vscode-font-size)/1.45 var(--vscode-font-family); }
  .totals { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--vscode-widget-border, transparent); border: 1px solid var(--vscode-widget-border, var(--vscode-panel-border)); border-radius: 5px; overflow: hidden; margin: 8px 0 14px; }
  .totals div { background: var(--vscode-sideBar-background); padding: 8px 10px; }
  .big { font-size: 1.45em; font-weight: 600; font-variant-numeric: tabular-nums; line-height: 1.15; }
  .sub { color: var(--vscode-descriptionForeground); font-size: .88em; }
  h3 { font-size: .92em; font-weight: 600; margin: 14px 0 6px; color: var(--vscode-descriptionForeground); }
  .row { padding: 7px 0 9px; border-top: 1px solid var(--vscode-widget-border, var(--vscode-panel-border)); }
  .row:first-of-type { border-top: 0; }
  .head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name b { font-weight: 600; }
  .pct { font-variant-numeric: tabular-nums; font-weight: 600; }
  .bar { display: grid; grid-template-columns: repeat(20, 1fr); gap: 2px; margin: 5px 0 4px; }
  .bar i { height: 7px; border-radius: 1px; background: var(--vscode-input-background, rgba(128,128,128,.18)); outline: 1px solid rgba(128,128,128,.12); }
  .bar i.on { background: var(--vscode-charts-green); outline: 0; }
  .bar.warn i.on { background: var(--vscode-charts-yellow); }
  .bar.hot i.on { background: var(--vscode-charts-red); }
  .meta { display: flex; justify-content: space-between; gap: 8px; color: var(--vscode-descriptionForeground); font-size: .88em; font-variant-numeric: tabular-nums; }
  .empty { margin-top: 12px; }
  .empty p { margin: 0 0 10px; }
  button { font: inherit; padding: 5px 11px; border: 0; border-radius: 3px; cursor: pointer; color: var(--vscode-button-foreground); background: var(--vscode-button-background); margin: 0 6px 6px 0; }
  button:hover { background: var(--vscode-button-hoverBackground); }
  button.sec { color: var(--vscode-button-secondaryForeground); background: var(--vscode-button-secondaryBackground); }
  a { color: var(--vscode-textLink-foreground); cursor: pointer; text-decoration: none; }
  .foot { margin-top: 14px; color: var(--vscode-descriptionForeground); font-size: .85em; }
</style></head><body><div id="root"><p class="sub">Connecting to AI Meter…</p></div>
<script nonce="${nonce}">
const vscode = acquireVsCodeApi(); let data = null;
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const tok = n => n == null ? "–" : n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? Math.round(n/1e3)+"k" : String(n);
const usd = n => n == null ? "–" : "$" + (n >= 100 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
function until(iso, verb) { verb = verb || "back in"; if (!iso) return ""; let s = Math.max(0,(Date.parse(iso)-Date.now())/1000);
  if (s < 60) return verb + " under a minute"; const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60),sec=Math.floor(s%60);
  return verb + " " + (d ? d+"d "+h+"h" : h ? h+"h "+m+"m" : m+"m "+String(sec).padStart(2,"0")+"s"); }
function bar(p) { const lit = Math.min(20, Math.round(p/5)); const c = p >= 95 ? "hot" : p >= 80 ? "warn" : "";
  return '<div class="bar '+c+'" role="img" aria-label="'+Math.round(p)+' percent used">' + Array.from({length:20},(_,i)=>'<i class="'+(i<lit?"on":"")+'"></i>').join("") + "</div>"; }
function render() {
  const r = document.getElementById("root"); if (!data) return;
  if (data.error) {
    const auth = data.error.code === "auth";
    r.innerHTML = '<div class="empty"><p><b>'+esc(data.error.message)+'</b></p><p class="sub">'+
      (auth ? "Your AI Meter has a dashboard token set. Paste it here once and VS Code keeps it in its secret storage."
            : "Start it with <code>aimeter</code>, or open an SSH tunnel to your server, then try again.")+'</p>'+
      (auth ? '<button data-cmd="setToken">Set token</button>' : '<button data-cmd="refresh">Try again</button><button class="sec" data-cmd="setServer">Change address</button>')+'</div>';
    return; }
  const t = data.total; let h = "";
  h += '<div class="totals"><div><div class="big">'+tok(t?.today)+'</div><div class="sub">tokens today · '+usd(t?.today_cost)+'</div></div>'+
       '<div><div class="big">'+tok(t?.tokens)+'</div><div class="sub">tokens this month · '+usd(t?.cost)+'</div></div></div>';
  if (!data.limits.length) h += '<div class="empty"><p>No limits with reset times yet.</p><p class="sub">Connect Claude, ChatGPT, Kimi Code or OpenCode Go in the dashboard to see them here.</p><button data-open="1">Connect a provider</button></div>';
  else {
    const groups = [["Plan limits", data.limits.filter(l => l.kind === "plan")],
                    ["Your budgets", data.limits.filter(l => l.kind !== "plan" && l.kind !== "ratelimit")],
                    ["Rate limits", data.limits.filter(l => l.kind === "ratelimit")]];
    for (const [title, list] of groups) { if (!list.length) continue; h += "<h3>"+title+"</h3>";
    for (const l of list) {
      const used = l.tokens ? tok(l.tokens)+" tokens"+(l.cost ? " · "+usd(l.cost) : "") : "";
      h += '<div class="row"><div class="head"><span class="name" title="'+esc(l.provider+": "+l.label)+'"><b>'+esc(l.provider)+'</b> '+esc(l.label)+'</span><span class="pct">'+Math.round(l.pct)+'%</span></div>'+
           bar(l.pct)+'<div class="meta"><span data-reset="'+esc(l.resets||"")+'" data-verb="'+(l.kind === "plan" ? "back in" : "resets in")+'">'+until(l.resets, l.kind === "plan" ? "back in" : "resets in")+'</span><span>'+used+'</span></div></div>'; } } }
  h += '<div class="foot">'+(data.spend ? "Spend this month "+usd(data.spend.all_usd)+" incl. subscriptions · " : "")+'<a data-open="1">Open full dashboard</a></div>';
  r.innerHTML = h;
}
addEventListener("message", e => { data = e.data; render(); });
addEventListener("click", e => { const b = e.target.closest("[data-cmd],[data-open]"); if (!b) return;
  vscode.postMessage(b.dataset.cmd ? { cmd: b.dataset.cmd } : { cmd: "open" }); });
setInterval(() => document.querySelectorAll("[data-reset]").forEach(el => el.textContent = until(el.dataset.reset, el.dataset.verb)), 1000);
</script></body></html>`;
}

function activate(context) {
  ctx = context;
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  provider = new Panel();
  context.subscriptions.push(
    statusItem,
    vscode.window.registerWebviewViewProvider("aimeter.panel", provider),
    vscode.commands.registerCommand("aimeter.focus", () => vscode.commands.executeCommand("aimeter.panel.focus")),
    vscode.commands.registerCommand("aimeter.refresh", refresh),
    vscode.commands.registerCommand("aimeter.checkNow", async () => {
      await vscode.window.withProgress({ location: vscode.ProgressLocation.Window, title: "AI Meter: checking every provider" }, async () => {
        try { await api("/api/collect", "POST"); } catch (e) { vscode.window.showErrorMessage(e.message); }
        await refresh();
      });
    }),
    vscode.commands.registerCommand("aimeter.openDashboard", () => vscode.env.openExternal(vscode.Uri.parse(base()))),
    vscode.commands.registerCommand("aimeter.setToken", async () => {
      const v = await vscode.window.showInputBox({ title: "AI Meter dashboard token", prompt: "The value of AIMETER_DASHBOARD_TOKEN on the machine running AI Meter. Stored in VS Code's secret storage.", password: true, ignoreFocusOut: true });
      if (v) { await ctx.secrets.store("aimeter.token", v.trim()); await refresh(); }
    }),
    vscode.commands.registerCommand("aimeter.clearToken", async () => { await ctx.secrets.delete("aimeter.token"); await refresh(); }),
    vscode.commands.registerCommand("aimeter.setServer", async () => {
      const v = await vscode.window.showInputBox({ title: "AI Meter address", value: base(), prompt: "For example http://localhost:8787", ignoreFocusOut: true,
        validateInput: s => /^https?:\/\/.+/.test(s) ? null : "Start with http:// or https://" });
      if (v) await cfg().update("serverUrl", v, vscode.ConfigurationTarget.Global);
    }),
    vscode.workspace.onDidChangeConfiguration(e => { if (e.affectsConfiguration("aimeter")) { schedule(); refresh(); } }),
    { dispose: () => clearInterval(timer) },
  );
  // Keep the countdown in the status bar fresh between reads.
  const tick = setInterval(() => state && !lastError && renderStatus(), 30000);
  context.subscriptions.push({ dispose: () => clearInterval(tick) });
  refresh(); schedule();
}

module.exports = { activate, deactivate() {} };
