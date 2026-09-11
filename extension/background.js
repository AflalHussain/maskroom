/* Service worker: the only place that talks to the Maskroom server, so the
   content script needs no CORS and the API key never enters the page. */
const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true,
                   unmask: true, preamble: true, excelAttach: "xlsx", interceptDownloads: true };
const MAX_UPLOAD = 25 * 1024 * 1024;
// File types the server can restore (mirrors maskroom/restore.py SUPPORTED_EXTS).
const RESTORE_EXTS = [".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml", ".yml",
                      ".xlsx", ".xlsm", ".docx", ".pptx"];
const MIME = { ".md": "text/markdown", ".txt": "text/plain", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
               ".json": "application/json", ".html": "text/html", ".htm": "text/html", ".xml": "application/xml",
               ".yaml": "application/yaml", ".yml": "application/yaml",
               ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
               ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
               ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation" };
const extOf = (name) => { const m = /\.[a-z0-9]+$/i.exec(name || ""); return m ? m[0].toLowerCase() : ""; };
const ownDownloads = new Set();
self.__savedFiles = [];  // {name, b64} of every file this worker saved (inspected by tests)

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  s.serverUrl = (s.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
  return s;
}

function b64ToBytes(b64) {
  const bin = atob(b64), out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}
function bytesToB64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

async function request(path, init) {
  const s = await settings();
  if (s.apiKey) (init.headers = init.headers || {})["X-API-Key"] = s.apiKey;
  try {
    return { res: await fetch(s.serverUrl + path, init) };
  } catch (e) {
    return { error: `Cannot reach Maskroom at ${s.serverUrl} (${e.message}). Is the server running?` };
  }
}

async function api(path, method = "GET", body = null) {
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  const { res, error } = await request(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (error) return { ok: false, status: 0, error };
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  return { ok: res.ok, status: res.status, data, error: res.ok ? null : ((data && data.error) || `${res.status} ${res.statusText}`) };
}

async function upload(name, b64, fields, path = "/api/process") {
  const bytes = b64ToBytes(b64);
  if (bytes.length > MAX_UPLOAD) return { ok: false, status: 413, error: "File is larger than 25 MB." };
  const fd = new FormData();
  fd.append("file", new Blob([bytes]), name);
  for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
  const { res, error } = await request(path, { method: "POST", body: fd });
  if (error) return { ok: false, status: 0, error };
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  return { ok: res.ok, status: res.status, data, error: res.ok ? null : ((data && data.error) || `${res.status} ${res.statusText}`) };
}

async function fetchBinary(path) {
  const { res, error } = await request(path, { method: "GET" });
  if (error) return { ok: false, status: 0, error };
  if (!res.ok) return { ok: false, status: res.status, error: `${res.status} ${res.statusText}` };
  return { ok: true, status: 200, b64: bytesToB64(await res.arrayBuffer()), contentType: res.headers.get("content-type") || "" };
}

async function saveFile(name, b64, mime) {
  const url = `data:${mime || MIME[extOf(name)] || "application/octet-stream"};base64,${b64}`;
  self.__savedFiles.push({ name, b64 });
  const id = await chrome.downloads.download({ url, filename: name, conflictAction: "uniquify", saveAs: false });
  ownDownloads.add(id);
  return { ok: true, id };
}

// Restore a file through the server with the given session; returns {ok, name, b64, report} or {ok:false, error}.
async function restoreFile(name, b64, sessionId) {
  const up = await upload(name, b64, { session_id: sessionId }, "/api/unmask-file");
  if (!up.ok) return { ok: false, error: up.error };
  const bin = await fetchBinary(`/api/download/${up.data.run_id}/${up.data.downloads.output}`);
  if (!bin.ok) return { ok: false, error: bin.error };
  const ext = extOf(name);
  return { ok: true, name: `${name.replace(/\.[^.]+$/, "")}_restored${ext}`, b64: bin.b64, report: up.data };
}

// ------------------------------------------------ download intercept
const claudeTabs = () => chrome.tabs.query({ url: "https://claude.ai/*" });
async function askTabs(msg, preferTab) {
  const tabs = await claudeTabs();
  if (preferTab != null) tabs.sort((a, b) => (a.id === preferTab ? -1 : b.id === preferTab ? 1 : 0));
  for (const t of tabs) {
    try { const r = await chrome.tabs.sendMessage(t.id, msg); if (r && r.ok) return { ...r, tabId: t.id }; } catch (e) { /* tab without our script */ }
  }
  return null;
}
async function toastTab(tabId, text, bad) {
  if (tabId == null) { const [t] = await claudeTabs(); if (!t) return; tabId = t.id; }
  try { await chrome.tabs.sendMessage(tabId, { type: "toast", text, bad: !!bad }); } catch (e) { /* ignore */ }
}
function fromClaude(item) {
  const u = item.url || "", r = item.referrer || "";
  return u.startsWith("blob:https://claude.ai/") || u.startsWith("https://claude.ai/") || r.startsWith("https://claude.ai/");
}
function summarize(report, savedAs, alreadyDone) {
  const n = report.unresolved.length;
  return `${report.filename}: ${report.restored} value${report.restored === 1 ? "" : "s"} restored → ${savedAs}`
    + (n ? ` · ${n} token${n === 1 ? "" : "s"} not in this session's vault (listed inside the file)` : "")
    + (alreadyDone ? ". The token version had already finished downloading and was kept." : "");
}

// Bytes first, cancel second: a download from claude.ai is only cancelled
// once its content is safely in hand, so the user never loses a file.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const base = (p) => (p || "").split(/[\\/]/).pop();
const MIME_EXT = Object.fromEntries(Object.entries(MIME).map(([e, m]) => [m, e]));
// Chrome fills item.filename a moment after creation; the page's download
// link (captured by the content script) is the fallback.
async function resolveName(item, got) {
  let name = base(item.filename);
  for (let i = 0; i < 6 && !extOf(name); i++) {
    await sleep(150);
    const [cur] = await chrome.downloads.search({ id: item.id });
    name = base(cur && cur.filename);
  }
  if (!extOf(name) && got && got.name) name = got.name;
  if (!extOf(name)) name = base(item.url.split("?")[0]);
  if (!extOf(name) && MIME_EXT[item.mime]) name = (name || "download") + MIME_EXT[item.mime];
  return name || "download";
}

async function intercept(item) {
  const s = await settings();
  if (!s.interceptDownloads || !fromClaude(item)) return;
  const got = await askTabs({ type: "fetchBytes", url: item.url });
  const name = await resolveName(item, got);
  const ext = extOf(name);
  console.debug("maskroom: download", item.id, item.url.slice(0, 60), name, got ? "bytes from tab" : "no bytes from tab");
  if (!RESTORE_EXTS.includes(ext)) return;
  let b64 = got && got.b64, tabId = got ? got.tabId : null;
  if (!b64 && !item.url.startsWith("blob:")) {
    try { const res = await fetch(item.url, { credentials: "include" }); if (res.ok) b64 = bytesToB64(await res.arrayBuffer()); } catch (e) { /* below */ }
  }
  if (!b64) { toastTab(tabId, `${name}: could not read the download, so it was saved with tokens. Use "Unmask file" on the bar.`, true); return; }
  try { await chrome.downloads.cancel(item.id); } catch (e) { /* may have finished */ }
  const [cur] = await chrome.downloads.search({ id: item.id });
  const alreadyDone = !!(cur && cur.state === "complete");
  if (!alreadyDone) { try { await chrome.downloads.erase({ id: item.id }); } catch (e) { /* ignore */ } }
  try {
    const sr = await askTabs({ type: "sessionId" }, tabId);
    if (!sr || !sr.sessionId) throw new Error("no Maskroom session in the claude.ai tab");
    const r = await restoreFile(name, b64, sr.sessionId);
    if (!r.ok) throw new Error(r.error);
    await saveFile(r.name, r.b64);
    toastTab(tabId, summarize(r.report, r.name, alreadyDone), r.report.unresolved.length > 0);
  } catch (e) {
    if (!alreadyDone) await saveFile(name, b64);  // hand the original back untouched
    toastTab(tabId, `${name}: restore failed (${e.message || e}) — saved with tokens.`, true);
  }
}
chrome.downloads.onCreated.addListener((item) => {
  if (item.byExtensionId === chrome.runtime.id || ownDownloads.has(item.id) || (item.url || "").startsWith("data:")) return;
  intercept(item).catch((e) => console.warn("maskroom intercept", e));
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;
  switch (msg.type) {
    case "api": api(msg.path, msg.method, msg.body).then(sendResponse); return true;
    case "upload": upload(msg.name, msg.b64, msg.fields).then(sendResponse); return true;
    case "fetchBinary": fetchBinary(msg.path).then(sendResponse); return true;
    case "restoreFile": restoreFile(msg.name, msg.b64, msg.sessionId).then(sendResponse); return true;
    case "saveFile": saveFile(msg.name, msg.b64, msg.mime).then(sendResponse); return true;
    case "settings": settings().then(sendResponse); return true;
    case "openOptions": chrome.runtime.openOptionsPage(); return false;
    default: return false;
  }
});
