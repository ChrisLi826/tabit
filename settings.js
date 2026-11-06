/* tabit's Settings sheet: AI defaults, Claude and Codex accounts, update, restart.
   The terminal view (remote.html) and Tabit Chat (chat.html) both load it and
   call openTabitSettings(sheet, close) with their own way to show and close a
   sheet; each page styles the markup.
   Only with typing from the phone on; Remote's own switches stay at the
   computer, so the phone can neither open more to itself nor lock itself out. */
"use strict";
(() => {
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const SET_LABELS = {
  ai_use_tmux: "New AI tabs run in tmux",
  ai_claude_bypass: "Claude starts in bypass (--dangerously-skip-permissions)",
  ai_fresh_on_restore: "New AI tabs start fresh after tabit reopens",
};
let setTimer = 0;
async function admin(op, body) {
  const res = await fetch(`api/admin/${op}`, {
    method: "POST", headers: { "Content-Type": "application/json", "X-Tabit": "1" },
    body: JSON.stringify(body || {}),
  });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out.error || `HTTP ${res.status}`);
  return out;
}
function settingsMsg(text, err) {
  const m = $("smsg");
  if (m) { m.textContent = text || ""; m.className = err ? "err" : "smsg"; }
}
window.openTabitSettings = (sheet, close) => {
  sheet(`<h3>Settings</h3><div id="sbody" class="dim">Loading…</div><div id="smsg" class="smsg"></div>
    <div class="row"><button id="sclose" type="button">Close</button></div>`);
  $("sclose").onclick = () => { clearTimeout(setTimer); close(); };
  drawSettings();
};
async function drawSettings() {
  clearTimeout(setTimer);
  if (!$("sbody") || !$("sbody").offsetParent) return;   // closed, hidden, or another sheet now
  let st;
  try {
    const res = await fetch("api/admin/state", { cache: "no-store" });
    // a tabit older than this page answers 404 in plain text
    if (res.status === 404) {
      $("sbody").textContent = "";
      settingsMsg("The tabit running now is older than this page. Restart tabit at the computer first.", true);
      return;
    }
    st = await res.json();
    if (!res.ok) throw new Error(st.error || `HTTP ${res.status}`);
  } catch (e) { settingsMsg(e.message || String(e), true); setTimer = setTimeout(drawSettings, 3000); return; }
  if (!$("sbody")) return;
  const lg = st.login, up = st.update;
  const busy = (lg && (lg.state === "starting" || lg.state === "waiting")) || (up && up.state === "running");
  const again = () => { if (busy) setTimer = setTimeout(drawSettings, 2000); };
  // a code half typed in survives the refresh: only the line under it moves
  const code = $("scode");
  if (code && lg && lg.state === "waiting" && (code.value || document.activeElement === code)) {
    $("slmsg").textContent = lg.msg || "";
    again();
    return;
  }
  const acctRow = (a) => `<div class="acct"><span>${esc(a.name || "Default")} — ${a.loggedIn
    ? esc(a.email || "logged in") + (a.plan ? ` · ${esc(a.plan)}` : "") : "not logged in"}</span>${a.loggedIn
    ? "" : `<button class="slogin" data-c="${esc(a.cli || "claude")}" data-n="${esc(a.name)}">Log in</button>`}</div>`;
  const PROG = { claude: "Claude", codex: "Codex" };
  const progs = [...new Set(st.accounts.map((a) => a.cli || "claude"))];
  const lgName = lg ? (lg.cli === "codex" ? "Codex " : "") + (lg.account || "Default") : "";
  let login = "";
  if (lg && (lg.state === "starting" || lg.state === "waiting") && lg.cli === "codex") {
    // a device code: typed on the login page, and nothing comes back here
    login = `<div class="box"><b>Log in to ${esc(lgName)}</b>${lg.url && lg.code
      ? `<a class="btnlink" href="${esc(lg.url)}" target="_blank" rel="noopener">1. Open the login page</a>
        <label>2. Type this code there</label><div class="scodeshow"><b>${esc(lg.code)}</b></div>
        <div class="dim">This turns to ✓ by itself when you are done.</div>`
      : `<div class="dim">Starting…</div>`}</div>`;
  } else if (lg && (lg.state === "starting" || lg.state === "waiting")) {
    login = `<div class="box"><b>Log in to ${esc(lgName)}</b>${lg.url
      ? `<a class="btnlink" href="${esc(lg.url)}" target="_blank" rel="noopener">1. Open the login page</a>
        <label for="scode">2. Paste the code it shows</label>
        <input id="scode" autocapitalize="off" autocorrect="off" spellcheck="false">
        <div class="row"><button id="scodego" class="go">Send code</button></div>`
      : `<div class="dim">Starting…</div>`}<div id="slmsg" class="dim">${esc(lg.msg || "")}</div></div>`;
  } else if (lg && lg.state === "done") {
    login = `<div class="dim">✓ ${esc(lgName)} logged in.</div>`;
  } else if (lg && lg.state === "failed") {
    login = `<div class="err">Login stopped: ${esc(lg.msg || "")}</div>`;
  }
  let upd = `<div class="row"><button id="supcheck">Check for updates</button></div><div id="supinfo"></div>`;
  if (up && up.state === "running") {
    upd = `<div class="dim">Updating to ${esc(up.latest)}… tabit restarts by itself when done.</div>
      <pre class="slog">${esc((up.log || []).slice(-8).join("\n"))}</pre>`;
  } else if (up && up.state === "done") {
    upd = `<div class="dim">✓ Installed ${esc(up.latest)}. Restarting…</div>`;
  } else if (up && up.state === "failed") {
    upd = `<div class="err">Update failed: ${esc(up.error)}</div>${upd}`;
  }
  $("sbody").className = "";
  $("sbody").innerHTML = `
    <label>AI defaults (new tabs)</label>
    ${Object.keys(SET_LABELS).map((k) => `<label class="check"><input type="checkbox" data-k="${k}"${
      st.settings[k] ? " checked" : ""}> ${esc(SET_LABELS[k])}</label>`).join("")}
    ${progs.map((p) => `<label>${PROG[p] || p} accounts</label>
    ${st.accounts.filter((a) => (a.cli || "claude") === p).map(acctRow).join("")}`).join("")}
    ${login}
    <div class="pathline">${progs.length > 1 ? `<select id="snewcli">${progs.map((p) => `<option value="${p}">${PROG[p] || p}</option>`).join("")}</select>` : ""}<input id="snewacct" placeholder="new account name, e.g. work" autocapitalize="off"
      autocorrect="off" spellcheck="false"><button id="saddacct">Add</button></div>
    <label>tabit ${esc(st.version)}</label>
    ${upd}
    <div class="row"><button id="srestart">Restart tabit</button></div>`;
  const run = async (fn) => {
    try { await fn(); settingsMsg(""); } catch (e) { settingsMsg(e.message || String(e), true); }
    drawSettings();
  };
  for (const c of $("sbody").querySelectorAll("input[data-k]")) c.onchange = async () => {
    try { await admin("set", { [c.dataset.k]: c.checked }); settingsMsg("saved"); }
    catch (e) { c.checked = !c.checked; settingsMsg(e.message || String(e), true); }
  };
  for (const b of $("sbody").querySelectorAll(".slogin")) b.onclick = () => run(() => admin("login-start", { cli: b.dataset.c, name: b.dataset.n }));
  $("saddacct").onclick = () => run(() => admin("account-add", { cli: $("snewcli") ? $("snewcli").value : "claude", name: $("snewacct").value.trim() }));
  if ($("scodego")) $("scodego").onclick = () => run(() => admin("login-code", { code: $("scode").value.trim() }));
  if ($("supcheck")) $("supcheck").onclick = async () => {
    $("supinfo").innerHTML = `<div class="dim">Checking…</div>`;
    try {
      const r = await admin("update-check");
      $("supinfo").innerHTML = r.newer
        ? `<div class="dim">New: <b>${esc(r.latest)}</b></div><div class="row"><button id="supgo" class="go">Update now</button></div>`
        : `<div class="dim">✓ ${esc(r.current)} is the newest.</div>`;
      if ($("supgo")) $("supgo").onclick = () => {
        if (confirm(`Update to ${r.latest}?\n\ntabit restarts by itself when done: tabs outside tmux close.`)) run(() => admin("update"));
      };
    } catch (e) { $("supinfo").innerHTML = ""; settingsMsg(e.message || String(e), true); }
  };
  $("srestart").onclick = async () => {
    if (!confirm("Restart tabit?\n\nTabs outside tmux close and their programs stop. tmux tabs keep running.")) return;
    try { await admin("restart"); settingsMsg("restarting… this page comes back by itself"); }
    catch (e) { settingsMsg(e.message || String(e), true); }
  };
  again();
}
})();
