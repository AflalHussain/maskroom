/* Maskroom for Claude — content script.

   What it does:
   - Adds a small bar to claude.ai. "Mask" replaces the text in the composer
     with pseudonymized text from the Maskroom server. YOU still press send.
   - Guard mode: pressing Enter or the send button with unmasked text first
     masks it and stops the send, so you can read what will leave the
     browser, then press Enter again.
   - Restores real values in replies on screen only (the DOM you see); the
     conversation on Anthropic's side keeps the tokens.
   - "Mask file" sends a .xlsx/.pdf to the server and attaches the masked
     version (workbook or Markdown text) to the chat; guard mode routes
     files dropped or picked in claude.ai through the same path.
   What it does not do: other tabs, mobile, other Claude surfaces. This is
   unsupported by Anthropic and depends on claude.ai's markup — every
   selector lives in SEL below. */
(() => {
  if (window.__maskroomLoaded) return;
  window.__maskroomLoaded = true;
  const isTop = window.top === window.self;  // only the top frame draws UI / owns the session

  const SEL = {
    composer: ['div[contenteditable="true"].ProseMirror', 'div[contenteditable="true"][data-placeholder]',
               'fieldset div[contenteditable="true"]', 'div[contenteditable="true"]',
               'textarea[data-testid], textarea[placeholder], textarea'],
    sendButton: ['button[aria-label="Send message"]', 'button[aria-label="Send Message"]',
                 'button[aria-label*="Send" i]', 'button[data-testid="send-button"]',
                 'button[type="submit"]', 'button:has(svg[aria-label*="Send" i])'],
    // claude.ai's own hidden file input (attach button) and the drop zone
    fileInput: ['input[type="file"][multiple]', 'input[type="file"]'],
    dropTarget: ['fieldset', 'form'],
  };
  const FILE_RE = /\.(xlsx|xlsm|pdf|docx|pptx|csv|tsv|txt|json)$/i;
  const RESTORE_RE = /\.(md|txt|csv|tsv|json|html?|xml|ya?ml|xlsx|xlsm|docx|pptx)$/i;
  const MAX_UPLOAD = 25 * 1024 * 1024;
  const T = self.MaskroomTokens;

  let settings = { guard: true, unmask: true, preamble: true, excelAttach: "xlsx", interceptDownloads: true, keepMasked: false };
  let sessionId = null;
  let idx = T.buildIndex({});
  let entries = 0;
  let lastMasked = "";      // composer text as we left it after masking
  let busy = false;
  let guardLocked = false;  // set true by admin managed policy (chrome.storage.managed)

  // ------------------------------------------------------------ helpers
  const q = (list, root = document) => { for (const s of list) { const el = root.querySelector(s); if (el) return el; } return null; };
  // Tolerate an invalidated extension context (happens to the old content
  // script in an open tab after the extension is reloaded): fail quietly
  // instead of throwing "Extension context invalidated" on every call.
  let contextDead = false;
  const call = (msg) => new Promise((res) => {
    if (contextDead) return res(undefined);
    try {
      chrome.runtime.sendMessage(msg, (r) => { void chrome.runtime.lastError; res(r); });
    } catch (e) {
      contextDead = true;
      if (isTop) { try { const el = document.getElementById("maskroom-bar"); if (el) { const m = el.querySelector(".mr-meta"); if (m) m.textContent = "reload this tab (extension was updated)"; } } catch (_) {} }
      res(undefined);
    }
  });
  const api = (path, method, body) => call({ type: "api", path, method, body });
  const safeSet = async (obj) => { try { await chrome.storage.local.set(obj); } catch (e) { contextDead = true; renderBar(); } };
  const safeGet = async (def) => { try { return await chrome.storage.local.get(def); } catch (e) { contextDead = true; return def; } };
  // Admin lock: chrome.storage.managed is read-only and set only by enterprise policy.
  // When guardLocked is set there, force the guard on and make the toggle inert.
  async function loadManaged() {
    try {
      const m = await chrome.storage.managed.get({ guardLocked: false });
      guardLocked = !!(m && m.guardLocked);
    } catch (e) { guardLocked = false; }  // no managed policy / not supported
    if (guardLocked) settings.guard = true;
  }

  let toastTimer = null;
  function toast(text, bad = false) {
    if (!isTop) return;
    let el = document.getElementById("maskroom-toast");
    if (!el) { el = document.createElement("div"); el.id = "maskroom-toast"; document.body.appendChild(el); }
    el.textContent = text; el.classList.toggle("mr-bad", bad); el.classList.add("mr-show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("mr-show"), bad ? 6000 : 3200);
  }

  // Conversation key: one Maskroom session per claude.ai chat. A brand-new
  // chat starts as "new" and is re-keyed once the URL gains its id.
  const convKey = () => (location.pathname.match(/\/chat\/([0-9a-f-]{8,})/i) || [])[1] || "new";
  let currentKey = convKey();

  async function storedSessions() { return (await safeGet({ sessions: {} })).sessions; }
  async function rememberSession(key, id) {
    const sessions = await storedSessions();
    sessions[key] = id;
    await safeSet({ sessions });
  }

  async function loadVault() {
    if (!sessionId) { idx = T.buildIndex({}); entries = 0; return; }
    const r = await api(`/api/session/${sessionId}/vault`);
    if (!r.ok) { if (r.status === 404) { sessionId = null; idx = T.buildIndex({}); entries = 0; } return; }
    const mappings = (r.data && r.data.mappings) || {};
    idx = T.buildIndex(mappings); entries = Object.keys(mappings).length;
    call({ type: "setTabVault", vault: mappings });  // let preview subframes restore too
    if (settings.unmask) restoreAll(document.body);
    renderBar();
  }

  // Subframes (artifact / file previews) restore only: they fetch the chat's
  // vault from the top frame via the worker, and never touch the session.
  async function refreshFrameVault() {
    const r = await call({ type: "getTabVault" });
    const mappings = (r && r.vault) || {};
    idx = T.buildIndex(mappings); entries = Object.keys(mappings).length;
    if (settings.unmask) restoreAll(document.body);
  }

  async function ensureSession() {
    if (!isTop) return sessionId;  // subframes never create or own a session
    if (sessionId) return sessionId;
    const sessions = await storedSessions();
    const known = sessions[currentKey];
    if (known) {
      const r = await api(`/api/session/${known}`);
      if (r.ok) { sessionId = known; await loadVault(); return sessionId; }
    }
    const r = await api("/api/session", "POST", {});
    if (!r.ok) throw new Error(r.error);
    sessionId = r.data.session_id;
    await rememberSession(currentKey, sessionId);
    renderBar();
    return sessionId;
  }

  async function newSession() {
    sessionId = null; lastMasked = "";
    await ensureSession();
    await loadVault();
    toast("New Maskroom session for this chat.");
  }

  // Adopt a session created elsewhere (the /staging page shows its id), so
  // files masked there and text masked here share one vault.
  async function adoptSession(id) {
    id = (id || "").trim();
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(id)) { toast("That does not look like a session id.", true); return false; }
    const r = await api(`/api/session/${id}`);
    if (!r.ok) { toast(r.status === 404 ? "Unknown or expired session id." : r.error, true); return false; }
    sessionId = id; lastMasked = "";
    await rememberSession(currentKey, id);
    await loadVault(); renderBar();
    toast(`Using session ${id.slice(0, 6)} (${entries} pseudonyms).`);
    return true;
  }

  // ---------------------------------------------------------- composer
  const isTextarea = (el) => el && el.tagName === "TEXTAREA";
  function composerText(el) {
    if (isTextarea(el)) return (el.value || "").replace(/\n{3,}/g, "\n\n");
    return (el.innerText || "").replace(/\u00a0/g, " ").replace(/\n{3,}/g, "\n\n");
  }

  function setComposerText(el, text) {
    el.focus();
    if (isTextarea(el)) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(el, text);  // React-friendly value set
      el.dispatchEvent(new InputEvent("input", { bubbles: true }));
      return;
    }
    const sel = window.getSelection();
    const range = document.createRange(); range.selectNodeContents(el); sel.removeAllRanges(); sel.addRange(range);
    let ok = false;
    try { ok = document.execCommand("insertText", false, text); } catch (e) { ok = false; }
    if (!ok || composerText(el).trim() !== text.trim()) {
      // Fallback for editors that ignore execCommand: rebuild paragraphs.
      el.innerHTML = "";
      for (const line of text.split("\n")) {
        const p = document.createElement("p");
        if (line) p.textContent = line; else p.appendChild(document.createElement("br"));
        el.appendChild(p);
      }
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
  }

  async function maskComposer() {
    const el = q(SEL.composer);
    if (!el) { toast("Composer not found — claude.ai may have changed; see SEL in content.js.", true); return { done: false, changed: false }; }
    const text = composerText(el);
    if (!text.trim()) return { done: false, changed: false };
    if (busy) { toast("Still masking the previous text…"); return { done: false, changed: false }; }
    busy = true; renderBar();
    try {
      await ensureSession();
      let r = await api("/api/mask", "POST", { text, session_id: sessionId });
      if (!r.ok && r.status === 404) { sessionId = null; await ensureSession(); r = await api("/api/mask", "POST", { text, session_id: sessionId }); }
      if (!r.ok) throw new Error(r.error);
      const d = r.data;
      let out = d.masked;
      const sessions = await storedSessions();
      const preambleKey = `preamble:${sessionId}`;
      if (settings.preamble && d.changed && !sessions[preambleKey]) {
        out = d.preamble + "\n\n" + out;
        sessions[preambleKey] = true; await chrome.storage.local.set({ sessions });
      }
      if (d.changed) setComposerText(el, out);
      lastMasked = composerText(el);
      entries = d.vault_entries;
      const n = d.findings.length;
      toast(n ? `${n} value${n === 1 ? "" : "s"} masked. Review, then press send.` : "No PII detected — safe to send.");
      await loadVault();
      return { done: true, changed: !!d.changed };
    } catch (e) {
      toast(e.message || String(e), true);
      return { done: false, changed: false };
    } finally { busy = false; renderBar(); }
  }

  // Replay the send the user already asked for, when the check found
  // nothing to mask: the text is exactly what they typed and submitted.
  function resend() {
    const btn = q(SEL.sendButton);
    if (btn) { btn.click(); return; }
    const el = q(SEL.composer);
    if (el) el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
  }

  // Guard: intercept Enter / send-click while the composer holds text we
  // have not checked yet. If masking changed anything the user must send
  // again after reading it; if nothing needed masking the original send is
  // replayed as-is.
  function needsMask() {
    const el = q(SEL.composer);
    if (!el) return false;
    const t = composerText(el).trim();
    return !!t && t !== lastMasked.trim();
  }
  const inComposer = (el, target) => !!el && (el === target || el.contains(target) || (target && target.isContentEditable));
  const onEnter = (e) => {
    if (!settings.guard || e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    const el = q(SEL.composer);
    if (!el || !inComposer(el, e.target) || !needsMask()) return;
    e.preventDefault(); e.stopImmediatePropagation();
    maskComposer().then((r) => { if (r.done && r.changed) toast("Masked — press Enter again to send."); else if (r.done) resend(); });
  };
  const onSendClick = (e) => {
    if (!settings.guard) return;
    const btn = e.target.closest && e.target.closest("button");
    const isSend = btn && SEL.sendButton.some((sel) => { try { return btn.matches(sel); } catch (x) { return false; } });
    if (!isSend || !needsMask()) return;
    e.preventDefault(); e.stopImmediatePropagation();
    maskComposer().then((r) => { if (r.done && r.changed) toast("Masked — click send again."); else if (r.done) resend(); });
  };
  // Register on window in the capture phase (the earliest point in dispatch) so the
  // guard sees Enter / the send click before claude.ai's own handlers can submit.
  window.addEventListener("keydown", onEnter, true);
  window.addEventListener("click", onSendClick, true);

  // --------------------------------------------------------------- files
  const readB64 = (file) => new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",")[1] || "");
    fr.onerror = () => rej(fr.error || new Error("read failed"));
    fr.readAsDataURL(file);
  });
  const isOurs = (el) => !!(el && el.closest && el.closest("#maskroom-bar, #maskroom-toast"));

  // Hand a File to claude.ai the way a user would: through its hidden file
  // input if there is one, else a synthetic drop on the composer area. The
  // page must show the file name within a few seconds to count as accepted.
  async function attachFile(file) {
    const dt = new DataTransfer();
    dt.items.add(file);
    const input = [...document.querySelectorAll(SEL.fileInput.join(","))].find((i) => !isOurs(i));
    const composer = q(SEL.composer);
    const target = (composer && composer.closest(SEL.dropTarget.join(","))) || composer || document.body;
    if (input) {
      try { input.files = dt.files; input.dispatchEvent(new Event("change", { bubbles: true })); } catch (e) { /* fall through */ }
    }
    if (!input || !(await fileVisible(file.name, 1200))) {
      for (const type of ["dragenter", "dragover", "drop"]) {
        target.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }));
      }
    }
    return fileVisible(file.name, 3000);
  }
  function fileVisible(name, ms) {
    return new Promise((res) => {
      const t0 = Date.now();
      const check = () => {
        for (const el of document.body.querySelectorAll("*")) {
          if (isOurs(el) || el.closest('[contenteditable="true"]')) continue;
          if (el.childElementCount === 0 && (el.textContent || "").includes(name)) return res(true);
          if (el.value && String(el.value).includes(name) && el.tagName === "INPUT" && el.type === "file") return res(true);
        }
        if (Date.now() - t0 > ms) return res(false);
        setTimeout(check, 150);
      };
      check();
    });
  }

  async function maskFile(file) {
    if (!FILE_RE.test(file.name)) { toast(`${file.name}: only .xlsx, .xlsm and .pdf can be masked.`, true); return false; }
    if (file.size > MAX_UPLOAD) { toast(`${file.name}: larger than 25 MB.`, true); return false; }
    while (busy) await new Promise((r) => setTimeout(r, 150));  // a text mask may be running
    busy = true; renderBar();
    try {
      await ensureSession();
      toast(`Masking ${file.name}…`);
      const b64 = await readB64(file);
      const fields = { session_id: sessionId, pdf_mode: "text", preview: "false" };
      let r = await call({ type: "upload", name: file.name, b64, fields });
      if (!r.ok && r.status === 404) { sessionId = null; await ensureSession(); fields.session_id = sessionId; r = await call({ type: "upload", name: file.name, b64, fields }); }
      if (!r.ok) throw new Error(r.error);
      const d = r.data;
      const isPdf = /\.pdf$/i.test(file.name);
      const wantMd = isPdf || settings.excelAttach === "md";
      const artefact = wantMd ? (d.downloads.text || d.downloads.output) : d.downloads.output;
      const path = `/api/download/${d.run_id}/${artefact}`;
      const bin = await call({ type: "fetchBinary", path });
      if (!bin.ok) throw new Error(bin.error);
      const bytes = Uint8Array.from(atob(bin.b64), (c) => c.charCodeAt(0));
      const stem = file.name.replace(/\.[^.]+$/, "");
      const ext = artefact.slice(artefact.lastIndexOf("."));
      const type = ext === ".md" ? "text/markdown" : (ext === ".xlsx" ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : "application/octet-stream");
      const masked = new File([bytes], `${stem}_masked${ext}`, { type });
      entries = d.vault_entries;
      const n = d.findings.length;
      const ok = await attachFile(masked);
      await loadVault();
      if (ok) toast(`${file.name}: ${n} value${n === 1 ? "" : "s"} masked · attached as ${masked.name}. Review, then send.`);
      else {
        toast(`${file.name} was masked (${n} values) but claude.ai did not accept the attachment. Opening the masked file so you can attach it yourself.`, true);
        window.open(await downloadUrl(path), "_blank");
      }
      return ok;
    } catch (e) {
      toast(`${file.name}: ${e.message || e}`, true);
      return false;
    } finally { busy = false; renderBar(); }
  }
  async function downloadUrl(path) {
    const s = await call({ type: "settings" });
    return s.serverUrl.replace(/\/+$/, "") + path + (s.apiKey ? `?key=${encodeURIComponent(s.apiKey)}` : "");
  }
  // Files are queued, not dropped, when one is already in flight.
  let fileQueue = Promise.resolve();
  function maskFiles(files) {
    for (const f of files) fileQueue = fileQueue.then(() => maskFile(f)).catch(() => {});
    return fileQueue;
  }

  // ------------------------------------------------ restore downloads
  function bufToB64(buf) {
    const bytes = new Uint8Array(buf); let str = "";
    for (let i = 0; i < bytes.length; i += 0x8000) str += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(str);
  }
  // A download link on claude.ai usually points at a blob: URL that the page
  // revokes right after the click. Start reading it at click time so the
  // worker can still get the bytes when the download shows up.
  const blobStore = new Map();  // url -> {p: Promise<b64|null>, name}
  document.addEventListener("click", (e) => {
    if (!isTop || !settings.interceptDownloads) return;
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || isOurs(a) || !(a.href.startsWith("blob:") || a.hasAttribute("download"))) return;
    const name = a.getAttribute("download") || a.href.split("?")[0].split("/").pop();
    if (!RESTORE_RE.test(name)) return;
    const p = fetch(a.href).then((r) => r.arrayBuffer()).then(bufToB64).catch(() => null);
    blobStore.set(a.href, { p, name });
    setTimeout(() => blobStore.delete(a.href), 60000);

    // Fast path: for a plain-click blob download in default mode, block the
    // browser download and restore it ourselves, so the token copy never
    // touches disk. Everything else (demo mode, https links, modified or
    // programmatic clicks) falls through to the worker's downloads intercept.
    if (settings.keepMasked || !a.href.startsWith("blob:")) return;
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault(); e.stopImmediatePropagation();
    fastRestoreDownload(name, p);
  }, true);

  async function fastRestoreDownload(name, bytesPromise) {
    while (busy) await new Promise((r) => setTimeout(r, 150));
    busy = true; renderBar();
    try {
      await ensureSession();
      const b64 = await bytesPromise;
      if (!b64) throw new Error("could not read the download");
      const r = await call({ type: "restoreFile", name, b64, sessionId });
      if (!r.ok) throw new Error(r.error);
      await call({ type: "saveFile", name: r.name, b64: r.b64 });
      const n = r.report.unresolved.length;
      toast(`${name}: ${r.report.restored} value${r.report.restored === 1 ? "" : "s"} restored → ${r.name}`
        + (n ? ` · ${n} token${n === 1 ? "" : "s"} not in this session's vault (listed inside the file)` : ""), n > 0);
    } catch (e) {
      // We blocked the browser's download; hand the file back so nothing is lost.
      try { const b64 = await bytesPromise; if (b64) await call({ type: "saveFile", name, b64 }); } catch (_) { /* ignore */ }
      toast(`${name}: ${e.message || e} — saved with tokens.`, true);
    } finally { busy = false; renderBar(); }
  }

  async function unmaskFile(file) {
    if (!RESTORE_RE.test(file.name)) { toast(`${file.name}: not a type Maskroom can restore.`, true); return false; }
    if (file.size > MAX_UPLOAD) { toast(`${file.name}: larger than 25 MB.`, true); return false; }
    while (busy) await new Promise((r) => setTimeout(r, 150));  // a text mask may be running
    busy = true; renderBar();
    try {
      await ensureSession();
      toast(`Restoring ${file.name}…`);
      const r = await call({ type: "restoreFile", name: file.name, b64: await readB64(file), sessionId });
      if (!r.ok) throw new Error(r.error);
      await call({ type: "saveFile", name: r.name, b64: r.b64 });
      const n = r.report.unresolved.length;
      toast(`${file.name}: ${r.report.restored} value${r.report.restored === 1 ? "" : "s"} restored → ${r.name}`
        + (n ? ` · ${n} token${n === 1 ? "" : "s"} not in this session's vault (listed inside the file)` : ""), n > 0);
      return true;
    } catch (e) {
      toast(`${file.name}: ${e.message || e}`, true);
      return false;
    } finally { busy = false; renderBar(); }
  }

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg) return false;
    if (msg.type === "fetchBytes") {
      (async () => {
        const entry = blobStore.get(msg.url);
        let b64 = entry ? await entry.p : null;
        if (!b64) { try { b64 = bufToB64(await (await fetch(msg.url, { credentials: "include" })).arrayBuffer()); } catch (e) { b64 = null; } }
        sendResponse(b64 ? { ok: true, b64, name: entry ? entry.name : undefined } : { ok: false });
      })();
      return true;
    }
    if (msg.type === "sessionId") {
      if (!isTop) { sendResponse({ ok: false }); return false; }
      ensureSession().then((id) => sendResponse({ ok: !!id, sessionId: id })).catch(() => sendResponse({ ok: false }));
      return true;
    }
    if (msg.type === "tabVaultUpdated") { if (!isTop) refreshFrameVault(); sendResponse({ ok: true }); return false; }
    if (msg.type === "toast") { toast(msg.text, !!msg.bad); sendResponse({ ok: true }); return false; }
    return false;
  });

  // Guard for raw uploads: a real (trusted) drop or file-picker selection in
  // claude.ai is taken over and routed through maskFile instead. We intercept
  // the whole drag sequence on WINDOW capture (the earliest phase) so
  // claude.ai's own drag overlay never appears — otherwise, because we swallow
  // the drop, claude's drop handler never runs to dismiss the overlay and its
  // UI freezes at the "drop here" state. Our own synthetic attach drops are
  // untrusted (isTrusted === false), so they pass straight through.
  const isFileDrag = (e) => {
    const t = e.dataTransfer && e.dataTransfer.types;
    return !!t && Array.prototype.indexOf.call(t, "Files") !== -1;
  };
  const swallowDrag = (e) => {
    if (e.isTrusted && settings.guard && isTop && !isOurs(e.target) && isFileDrag(e)) {
      e.preventDefault(); e.stopImmediatePropagation();
      return true;
    }
    return false;
  };
  let dragHinted = false;
  window.addEventListener("dragenter", (e) => { if (swallowDrag(e) && !dragHinted) { dragHinted = true; toast("Drop the file to mask it with Maskroom."); } }, true);
  window.addEventListener("dragover", swallowDrag, true);
  window.addEventListener("dragleave", swallowDrag, true);
  window.addEventListener("dragend", () => { dragHinted = false; }, true);
  window.addEventListener("drop", (e) => {
    if (!settings.guard || !isTop || !e.isTrusted || isOurs(e.target)) return;
    const files = [...((e.dataTransfer && e.dataTransfer.files) || [])];
    if (!files.length) return;
    e.preventDefault(); e.stopImmediatePropagation();
    dragHinted = false;
    // Belt and suspenders: if claude did manage to show an overlay, clear it.
    for (const t of ["dragleave", "dragend"]) { try { window.dispatchEvent(new DragEvent(t, { bubbles: true })); } catch (_) { /* ignore */ } }
    const maskable = files.filter((f) => FILE_RE.test(f.name));
    if (!maskable.length) { toast(`${files.map((f) => f.name).join(", ")}: not a type Maskroom can mask — not attached.`, true); return; }
    maskFiles(maskable);
    const rest = files.filter((f) => !FILE_RE.test(f.name));
    if (rest.length) toast(`${rest.map((f) => f.name).join(", ")}: not maskable, not attached.`, true);
  }, true);
  document.addEventListener("change", (e) => {
    const input = e.target;
    if (!settings.guard || !e.isTrusted || !input || input.type !== "file" || isOurs(input)) return;
    const files = [...(input.files || [])];
    if (!files.some((f) => FILE_RE.test(f.name))) return;
    e.stopImmediatePropagation();
    try { input.value = ""; } catch (err) { /* ignore */ }
    maskFiles(files.filter((f) => FILE_RE.test(f.name)));
  }, true);

  // ------------------------------------------------------- unmask view
  // Original token text per restored node, so the view can be switched
  // back to tokens without a reload.
  const restoredNodes = new Map();  // text node -> { original, restored }
  function restoreAll(root) {
    if (!settings.unmask || !entries) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        const v = n.nodeValue;
        if (!v || !/tok/i.test(v)) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (!p || p.closest('[contenteditable="true"], script, style, textarea, #maskroom-bar, #maskroom-toast')) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    for (let n = walker.nextNode(); n; n = walker.nextNode()) nodes.push(n);
    for (const n of nodes) {
      const { text } = T.restore(n.nodeValue, idx);
      if (text === n.nodeValue) continue;
      const prev = restoredNodes.get(n);
      restoredNodes.set(n, { original: prev ? prev.original : n.nodeValue, restored: text });
      n.nodeValue = text;
      n.parentElement && n.parentElement.classList.add("maskroom-restored");
    }
  }
  function revertAll() {
    for (const [n, { original, restored }] of restoredNodes) {
      // Skip nodes the page has since replaced or rewritten.
      if (n.isConnected && n.nodeValue === restored) {
        n.nodeValue = original;
        const p = n.parentElement;
        if (p && ![...p.childNodes].some((c) => c !== n && restoredNodes.has(c))) p.classList.remove("maskroom-restored");
      }
    }
    restoredNodes.clear();
  }
  function setUnmask(on) {
    settings.unmask = on;
    if (on) restoreAll(document.body); else revertAll();
    renderBar();
  }
  // Mutations are accumulated (not dropped) while a run is pending, so a
  // reply that streams in during the debounce window is still restored.
  const pendingRoots = new Set();
  let pendingTimer = null;
  const observer = new MutationObserver((muts) => {
    if (!settings.unmask || !entries) return;
    for (const m of muts) {
      if (isOurs(m.target)) continue;
      if (m.type === "characterData") pendingRoots.add(m.target.parentElement || document.body);
      for (const n of m.addedNodes) pendingRoots.add(n.nodeType === 1 ? n : (n.parentElement || document.body));
    }
    if (pendingTimer || !pendingRoots.size) return;
    pendingTimer = setTimeout(() => {
      pendingTimer = null;
      const roots = [...pendingRoots]; pendingRoots.clear();
      for (const r of roots) if (r && r.isConnected) restoreAll(r);
    }, 120);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // --------------------------------------------------------------- bar
  function renderBar() {
    if (!isTop) return;
    let bar = document.getElementById("maskroom-bar");
    if (contextDead && bar) { const m = bar.querySelector(".mr-meta"); if (m) m.textContent = "reload this tab — extension was updated"; return; }
    if (!bar) {
      bar = document.createElement("div"); bar.id = "maskroom-bar";
      bar.innerHTML = `<span class="mr-brand">MASKROOM</span><span class="mr-meta"></span>
        <button class="mr-primary" data-act="mask" title="Pseudonymize the composer text (Ctrl/Cmd+Shift+M)">Mask</button>
        <button data-act="file" title="Mask a .xlsx/.pdf/.docx and attach the masked version">Mask file</button>
        <input type="file" id="maskroom-file" accept=".xlsx,.xlsm,.pdf,.docx,.pptx,.csv,.tsv,.txt,.json" multiple hidden>
        <button data-act="unmaskfile" title="Restore the real values inside a file Claude produced (saved as *_restored)">Unmask file</button>
        <input type="file" id="maskroom-unmask-file" accept=".md,.txt,.csv,.tsv,.json,.html,.htm,.xml,.yaml,.yml,.xlsx,.xlsm,.docx,.pptx" multiple hidden>
        <button data-act="guard" title="Guard: Enter/send and file drops go through Maskroom first — click to turn on/off">guard: on</button>
        <button data-act="unmask" title="Show real values in replies (on screen only) — click to turn on/off">unmask: on</button>
        <button data-act="new" title="Start a new vault for this chat">new session</button>
        <button data-act="adopt" title="Use a session id from the Maskroom staging page">use id…</button>
        <button data-act="opts" title="Settings">⚙</button>`;
      bar.addEventListener("click", async (e) => {
        const hit = e.target.closest && e.target.closest("[data-act]");
        const act = hit && hit.dataset.act;
        if (contextDead) { renderBar(); return; }
        try {
          if (act === "mask") maskComposer();
          else if (act === "file") bar.querySelector("#maskroom-file").click();
          else if (act === "unmaskfile") bar.querySelector("#maskroom-unmask-file").click();
          else if (act === "new") newSession();
          else if (act === "adopt") adoptSession(window.prompt("Maskroom session id (shown on the staging page):", ""));
          else if (act === "guard") { if (guardLocked) { toast("Guard is locked on by your administrator.", true); } else { settings.guard = !settings.guard; await safeSet({ guard: settings.guard }); renderBar(); } }
          else if (act === "unmask") { setUnmask(!settings.unmask); await safeSet({ unmask: settings.unmask }); }
          else if (act === "opts") call({ type: "openOptions" });
        } catch (err) { contextDead = true; renderBar(); }
      });
      bar.querySelector("#maskroom-file").addEventListener("change", (e) => {
        const files = [...e.target.files]; e.target.value = "";
        maskFiles(files);
      });
      bar.querySelector("#maskroom-unmask-file").addEventListener("change", (e) => {
        const files = [...e.target.files]; e.target.value = "";
        for (const f of files) fileQueue = fileQueue.then(() => unmaskFile(f)).catch(() => {});
      });
      document.body.appendChild(bar);
    }
    bar.querySelector(".mr-meta").innerHTML = sessionId
      ? `session <b>${sessionId.slice(0, 6)}</b> · <b>${entries}</b> pseudonyms`
      : `no session yet`;
    bar.dataset.session = sessionId || ""; bar.dataset.entries = String(entries);
    bar.dataset.vault = String(Object.keys(idx.vault).length);  // state for tests/debugging
    bar.querySelector('[data-act="mask"]').disabled = busy;
    bar.querySelector('[data-act="mask"]').textContent = busy ? "masking…" : "Mask";
    bar.querySelector('[data-act="file"]').disabled = busy;
    bar.querySelector('[data-act="unmaskfile"]').disabled = busy;
    const gb = bar.querySelector('[data-act="guard"]');
    gb.textContent = guardLocked ? "guard: on 🔒" : "guard: " + (settings.guard ? "on" : "off");
    gb.title = guardLocked ? "Guard is locked on by your administrator" : "Guard: Enter/send and file drops go through Maskroom first — click to turn on/off";
    gb.classList.toggle("mr-off", !settings.guard && !guardLocked);
    const ub = bar.querySelector('[data-act="unmask"]');
    ub.textContent = "unmask: " + (settings.unmask ? "on" : "off");
    ub.classList.toggle("mr-off", !settings.unmask);
  }
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "m") { e.preventDefault(); maskComposer(); }
  });

  // ------------------------------------------------------- navigation
  if (isTop) setInterval(async () => {
    const key = convKey();
    if (key === currentKey) return;
    const sessions = await storedSessions();
    if (currentKey === "new" && sessionId && !sessions[key]) {
      // the chat we started in just got its id: carry the session over
      sessions[key] = sessionId; delete sessions["new"]; await chrome.storage.local.set({ sessions });
    } else {
      sessionId = sessions[key] || null; lastMasked = "";
    }
    currentKey = key;
    await loadVault(); renderBar();
  }, 1000);

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "managed") {
      if ("guardLocked" in changes) { guardLocked = !!changes.guardLocked.newValue; if (guardLocked) settings.guard = true; renderBar(); }
      return;
    }
    if (area !== "local") return;
    for (const k of ["guard", "preamble", "excelAttach", "interceptDownloads", "keepMasked"]) if (k in changes) settings[k] = changes[k].newValue;
    if (guardLocked) settings.guard = true;  // managed policy always wins over a local write
    if ("unmask" in changes && changes.unmask.newValue !== settings.unmask) setUnmask(!!changes.unmask.newValue);
    renderBar();
  });

  // ---------------------------------------------------------------- boot
  (async () => {
    settings = Object.assign(settings, await call({ type: "settings" }));
    if (!isTop) { await refreshFrameVault(); return; }  // preview subframe: restore only
    await loadManaged();
    renderBar();
    const sessions = await storedSessions();
    sessionId = sessions[currentKey] || null;
    await loadVault();
    renderBar();
  })();
})();
