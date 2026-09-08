/* Service worker: the only place that talks to the Maskroom server, so the
   content script needs no CORS and the API key never enters the page. */
const DEFAULTS = { serverUrl: "http://127.0.0.1:5170", apiKey: "", guard: true,
                   unmask: true, preamble: true };

async function settings() {
  const s = await chrome.storage.local.get(DEFAULTS);
  s.serverUrl = (s.serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
  return s;
}

async function api(path, method = "GET", body = null) {
  const s = await settings();
  const headers = {};
  if (body) headers["Content-Type"] = "application/json";
  if (s.apiKey) headers["X-API-Key"] = s.apiKey;
  let res;
  try {
    res = await fetch(s.serverUrl + path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  } catch (e) {
    return { ok: false, status: 0, error: `Cannot reach Maskroom at ${s.serverUrl} (${e.message}). Is the server running?` };
  }
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON body */ }
  return { ok: res.ok, status: res.status, data, error: res.ok ? null : ((data && data.error) || `${res.status} ${res.statusText}`) };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "api") {
    api(msg.path, msg.method, msg.body).then(sendResponse);
    return true;  // async response
  }
  if (msg && msg.type === "settings") {
    settings().then(sendResponse);
    return true;
  }
  if (msg && msg.type === "openOptions") {
    chrome.runtime.openOptionsPage();
    return false;
  }
  return false;
});
