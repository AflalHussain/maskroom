const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true, unmask: true, preamble: true, excelAttach: "xlsx", interceptDownloads: true, keepMasked: false };
const $ = (id) => document.getElementById(id);
const status = (t, cls = "") => { $("status").textContent = t; $("status").className = cls; };

chrome.storage.local.get(DEFAULTS).then((s) => {
  $("serverUrl").value = s.serverUrl; $("apiKey").value = s.apiKey;
  $("guard").checked = s.guard; $("unmask").checked = s.unmask; $("preamble").checked = s.preamble;
  $("excelAttach").value = s.excelAttach; $("interceptDownloads").checked = s.interceptDownloads;
  $("keepMasked").checked = s.keepMasked;
});

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
    await chrome.storage.local.set({ serverUrl, apiKey: $("apiKey").value.trim(),
      guard: $("guard").checked, unmask: $("unmask").checked, preamble: $("preamble").checked,
      excelAttach: $("excelAttach").value, interceptDownloads: $("interceptDownloads").checked,
      keepMasked: $("keepMasked").checked });
    status("Saved.", "ok");
  } catch (e) { status(e.message, "bad"); }
});

$("test").addEventListener("click", async () => {
  status("Testing…");
  const r = await chrome.runtime.sendMessage({ type: "api", path: "/api/config" });
  if (r && r.ok) status(`Connected · locale ${r.data.default_locale} · auth ${r.data.auth_required ? "required" : "off"}`, "ok");
  else status((r && r.error) || "No response", "bad");
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
