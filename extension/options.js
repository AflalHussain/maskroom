const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true, unmask: true, preamble: true, excelAttach: "xlsx", interceptDownloads: true, keepMasked: false };
const $ = (id) => document.getElementById(id);
const status = (t, cls = "") => { $("status").textContent = t; $("status").className = cls; };

chrome.storage.local.get(DEFAULTS).then((s) => {
  $("serverUrl").value = s.serverUrl; $("apiKey").value = s.apiKey;
  renderAccount();
  $("guard").checked = s.guard; $("unmask").checked = s.unmask; $("preamble").checked = s.preamble;
  $("excelAttach").value = s.excelAttach; $("interceptDownloads").checked = s.interceptDownloads;
  $("keepMasked").checked = s.keepMasked;
});

// Admin lock: when guardLocked is set by managed policy, force the Guard checkbox on,
// disable it, and say so. Managed storage is read-only, so the user cannot change it.
chrome.storage.managed.get({ guardLocked: false, serverUrl: "" }).then((m) => {
  if (!m) return;
  if (m.guardLocked) {
    const cb = $("guard"); cb.checked = true; cb.disabled = true;
    const note = document.createElement("span");
    note.textContent = " 🔒 locked by your administrator";
    note.style.cssText = "color:var(--brand);font-size:11.5px";
    const label = document.querySelector('label[for="guard"]');
    if (label) label.appendChild(note);
  }
  if (m.serverUrl) {
    const f = $("serverUrl"); f.value = m.serverUrl; f.disabled = true;
    const lbl = document.querySelector('label[for="serverUrl"]');
    if (lbl) { const n = document.createElement("span"); n.textContent = " 🔒 set by your administrator";
      n.style.cssText = "color:var(--brand);font-size:11px"; lbl.appendChild(n); }
  }
}).catch(() => {});

async function ensurePermission(url) {
  let origin;
  try { origin = new URL(url).origin + "/*"; } catch (e) { throw new Error("Invalid server URL"); }
  if (/^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/\*$/.test(origin)) return;
  const has = await chrome.permissions.contains({ origins: [origin] });
  if (!has && !(await chrome.permissions.request({ origins: [origin] }))) throw new Error("Permission for " + origin + " was not granted");
}

$("save").addEventListener("click", async () => {
  try {
    const serverUrl = $("serverUrl").value.trim().replace(/\/+$/, "") || DEFAULTS.serverUrl;
    await ensurePermission(serverUrl);
    await chrome.storage.local.set({ serverUrl,
      guard: $("guard").checked, unmask: $("unmask").checked, preamble: $("preamble").checked,
      excelAttach: $("excelAttach").value, interceptDownloads: $("interceptDownloads").checked,
      keepMasked: $("keepMasked").checked });
    status("Saved.", "ok");
  } catch (e) { status(e.message, "bad"); }
});

$("test").addEventListener("click", async () => {
  status("Testing…");
  const r = await chrome.runtime.sendMessage({ type: "api", path: "/api/config" });
  if (r && r.ok) status(`Connected · locale ${r.data.default_locale} · sign-on ${r.data.auth_mode === "oidc" ? "on" : "off"}${r.data.auth_mode !== "oidc" && r.data.auth_required ? " (legacy API key required)" : ""}`, "ok");
  else status((r && r.error) || "No response", "bad");
  renderAccount();
});

// ---------- account ----------
async function renderAccount() {
  const el = $("account");
  const r = await chrome.runtime.sendMessage({ type: "me" });
  if (!r) { el.textContent = "no response from the extension"; return; }
  if (r.ok && r.data && r.data.auth_mode === "off") {
    el.textContent = "This server runs without sign-on." + (r.data.principal ? ` Using ${r.data.principal.name}.` : "");
    $("login").hidden = true; $("logout").hidden = true; $("legacy").open = true; return;
  }
  $("legacy").open = false;
  if (r.ok && r.data && r.data.principal) {
    const p = r.data.principal;
    el.innerHTML = `Signed in as <b>${(p.email || p.name || "").replace(/</g, "&lt;")}</b> · ${p.role}`;
    $("login").hidden = true; $("logout").hidden = false;
  } else {
    el.textContent = r.status === 401 ? "Not signed in." : (r.error || "Cannot reach the server.");
    $("login").hidden = false; $("logout").hidden = true;
  }
}
$("login").addEventListener("click", async () => {
  status("Opening sign-in…");
  const r = await chrome.runtime.sendMessage({ type: "login" });
  status(r && r.ok ? "Signed in." : ((r && r.error) || "Sign-in failed"), r && r.ok ? "ok" : "bad");
  renderAccount();
});
$("logout").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "logout" });
  status("Signed out.", "ok"); renderAccount();
});
$("savekey").addEventListener("click", async () => {
  await chrome.storage.local.set({ apiKey: $("apiKey").value.trim() });
  status("Legacy key saved.", "ok"); renderAccount();
});


// ---------- download diagnostics ----------
const COLOR = { restored: "#3fb98d", failed: "#e2574c", "left as-is": "#e0a83a", ignored: "#5a6270" };
async function renderLog() {
  const { interceptLog = [] } = await chrome.storage.local.get("interceptLog");
  const el = $("log");
  el.innerHTML = interceptLog.length ? interceptLog.slice().reverse().map((e) =>
    `<div><span style="color:${COLOR[e.outcome] || "#8d94a1"}">${e.outcome}</span> · ${e.time.replace("T", " ").slice(0, 19)} · ${e.name || "?"} · ${e.url}${e.referrer ? " ← " + e.referrer : ""}<br>&nbsp;&nbsp;${(e.detail || "").replace(/</g, "&lt;")}</div>`
  ).join("") : "no downloads seen yet";
  el.dataset.text = interceptLog.map((e) => `${e.time} ${e.outcome} ${e.name} ${e.url} ${e.referrer || ""} ${e.mime || ""} — ${e.detail || ""}`).join("\n");
}
renderLog();
chrome.storage.onChanged.addListener((c, area) => { if (area === "local" && "interceptLog" in c) renderLog(); });
$("copylog").addEventListener("click", async () => { try { await navigator.clipboard.writeText($("log").dataset.text || ""); status("Log copied.", "ok"); } catch (e) { status("Copy failed", "bad"); } });
$("clearlog").addEventListener("click", () => chrome.storage.local.set({ interceptLog: [] }));
