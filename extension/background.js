/* Service worker: the only place that talks to the Maskroom server, so the
   content script needs no CORS and the API key never enters the page. */
const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true,
                   unmask: true, preamble: true, excelAttach: "xlsx" };
const MAX_UPLOAD = 25 * 1024 * 1024;

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

async function upload(name, b64, fields) {
  const bytes = b64ToBytes(b64);
  if (bytes.length > MAX_UPLOAD) return { ok: false, status: 413, error: "File is larger than 25 MB." };
  const fd = new FormData();
  fd.append("file", new Blob([bytes]), name);
  for (const [k, v] of Object.entries(fields || {})) fd.append(k, v);
  const { res, error } = await request("/api/process", { method: "POST", body: fd });
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

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;
  switch (msg.type) {
    case "api": api(msg.path, msg.method, msg.body).then(sendResponse); return true;
    case "upload": upload(msg.name, msg.b64, msg.fields).then(sendResponse); return true;
    case "fetchBinary": fetchBinary(msg.path).then(sendResponse); return true;
    case "settings": settings().then(sendResponse); return true;
    case "openOptions": chrome.runtime.openOptionsPage(); return false;
    default: return false;
  }
});
