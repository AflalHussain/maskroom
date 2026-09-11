const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true, unmask: true, preamble: true, excelAttach: "xlsx", interceptDownloads: true };
const $ = (id) => document.getElementById(id);
const status = (t, cls = "") => { $("status").textContent = t; $("status").className = cls; };

chrome.storage.local.get(DEFAULTS).then((s) => {
  $("serverUrl").value = s.serverUrl; $("apiKey").value = s.apiKey;
  $("guard").checked = s.guard; $("unmask").checked = s.unmask; $("preamble").checked = s.preamble;
  $("excelAttach").value = s.excelAttach; $("interceptDownloads").checked = s.interceptDownloads;
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
      excelAttach: $("excelAttach").value, interceptDownloads: $("interceptDownloads").checked });
    status("Saved.", "ok");
  } catch (e) { status(e.message, "bad"); }
});

$("test").addEventListener("click", async () => {
  status("Testing…");
  const r = await chrome.runtime.sendMessage({ type: "api", path: "/api/config" });
  if (r && r.ok) status(`Connected · locale ${r.data.default_locale} · auth ${r.data.auth_required ? "required" : "off"}`, "ok");
  else status((r && r.error) || "No response", "bad");
});
