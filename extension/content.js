/* Maskroom for Claude — content script.

   What it does:
   - Adds a small bar to claude.ai. "Mask" replaces the text in the composer
     with pseudonymized text from the Maskroom server. YOU still press send.
   - Guard mode: pressing Enter or the send button with unmasked text first
     masks it and stops the send, so you can read what will leave the
     browser, then press Enter again.
   - Restores real values in replies on screen only (the DOM you see); the
     conversation on Anthropic's side keeps the tokens.
   What it does not do: files/attachments, other tabs, mobile. Use the
   Maskroom staging page for files. This is unsupported by Anthropic and
   depends on claude.ai's markup — every selector lives in SEL below. */
(() => {
  if (window.__maskroomLoaded) return;
  window.__maskroomLoaded = true;

  const SEL = {
    composer: ['div[contenteditable="true"].ProseMirror', 'div[contenteditable="true"][data-placeholder]',
               'fieldset div[contenteditable="true"]', 'div[contenteditable="true"]'],
    sendButton: ['button[aria-label="Send message"]', 'button[aria-label="Send Message"]',
                 'button[aria-label*="Send" i]', 'button[data-testid="send-button"]'],
  };
  const T = self.MaskroomTokens;

  let settings = { guard: true, unmask: true, preamble: true };
  let sessionId = null;
  let idx = T.buildIndex({});
  let entries = 0;
  let lastMasked = "";      // composer text as we left it after masking
  let busy = false;

  // ------------------------------------------------------------ helpers
  const q = (list, root = document) => { for (const s of list) { const el = root.querySelector(s); if (el) return el; } return null; };
  const call = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, res));
  const api = (path, method, body) => call({ type: "api", path, method, body });

  let toastTimer = null;
  function toast(text, bad = false) {
    let el = document.getElementById("maskroom-toast");
    if (!el) { el = document.createElement("div"); el.id = "maskroom-toast"; document.body.appendChild(el); }
    el.textContent = text; el.classList.toggle("mr-bad", bad); el.classList.add("mr-show");
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("mr-show"), bad ? 6000 : 3200);
  }

  // Conversation key: one Maskroom session per claude.ai chat. A brand-new
  // chat starts as "new" and is re-keyed once the URL gains its id.
  const convKey = () => (location.pathname.match(/\/chat\/([0-9a-f-]{8,})/i) || [])[1] || "new";
  let currentKey = convKey();

  async function storedSessions() { return (await chrome.storage.local.get({ sessions: {} })).sessions; }
  async function rememberSession(key, id) {
    const sessions = await storedSessions();
    sessions[key] = id;
    await chrome.storage.local.set({ sessions });
  }

  async function loadVault() {
    if (!sessionId) { idx = T.buildIndex({}); entries = 0; return; }
    const r = await api(`/api/session/${sessionId}/vault`);
    if (!r.ok) { if (r.status === 404) { sessionId = null; idx = T.buildIndex({}); entries = 0; } return; }
    const mappings = (r.data && r.data.mappings) || {};
    idx = T.buildIndex(mappings); entries = Object.keys(mappings).length;
    if (settings.unmask) restoreAll(document.body);
    renderBar();
  }

  async function ensureSession() {
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

  // ---------------------------------------------------------- composer
  function composerText(el) { return (el.innerText || "").replace(/\u00a0/g, " ").replace(/\n{3,}/g, "\n\n"); }

  function setComposerText(el, text) {
    el.focus();
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
    if (!el) { toast("Composer not found — claude.ai may have changed; see SEL in content.js.", true); return false; }
    const text = composerText(el);
    if (!text.trim()) return false;
    if (busy) return false;
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
  document.addEventListener("keydown", (e) => {
    if (!settings.guard || e.key !== "Enter" || e.shiftKey || e.isComposing) return;
    const el = q(SEL.composer);
    if (!el || !el.contains(e.target) || !needsMask()) return;
    e.preventDefault(); e.stopImmediatePropagation();
    maskComposer().then((r) => { if (r.done && r.changed) toast("Masked — press Enter again to send."); else if (r.done) resend(); });
  }, true);
  document.addEventListener("click", (e) => {
    if (!settings.guard) return;
    const btn = e.target.closest && e.target.closest("button");
    if (!btn || !SEL.sendButton.some((s) => btn.matches(s)) || !needsMask()) return;
    e.preventDefault(); e.stopImmediatePropagation();
    maskComposer().then((r) => { if (r.done && r.changed) toast("Masked — click send again."); else if (r.done) resend(); });
  }, true);

  // ------------------------------------------------------- unmask view
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
      if (text !== n.nodeValue) { n.nodeValue = text; n.parentElement && n.parentElement.classList.add("maskroom-restored"); }
    }
  }
  let pending = null;
  const observer = new MutationObserver((muts) => {
    if (!settings.unmask || !entries) return;
    if (pending) return;
    pending = setTimeout(() => {
      pending = null;
      const roots = new Set();
      for (const m of muts) {
        if (m.type === "characterData") roots.add(m.target.parentElement || document.body);
        for (const n of m.addedNodes) roots.add(n.nodeType === 1 ? n : (n.parentElement || document.body));
      }
      for (const r of roots) if (r && r.isConnected) restoreAll(r);
    }, 120);
  });
  observer.observe(document.body, { childList: true, subtree: true, characterData: true });

  // --------------------------------------------------------------- bar
  function renderBar() {
    let bar = document.getElementById("maskroom-bar");
    if (!bar) {
      bar = document.createElement("div"); bar.id = "maskroom-bar";
      bar.innerHTML = `<span class="mr-brand">MASKROOM</span><span class="mr-meta"></span>
        <button class="mr-primary" data-act="mask" title="Pseudonymize the composer text (Ctrl/Cmd+Shift+M)">Mask</button>
        <button data-act="guard" title="Guard: Enter/send first masks unmasked text">guard</button>
        <button data-act="unmask" title="Show real values in replies (on screen only)">unmask view</button>
        <button data-act="new" title="Start a new vault for this chat">new session</button>
        <button data-act="opts" title="Settings">⚙</button>`;
      bar.addEventListener("click", async (e) => {
        const act = e.target.dataset && e.target.dataset.act;
        if (act === "mask") maskComposer();
        else if (act === "new") newSession();
        else if (act === "guard") { settings.guard = !settings.guard; await chrome.storage.local.set({ guard: settings.guard }); renderBar(); }
        else if (act === "unmask") { settings.unmask = !settings.unmask; await chrome.storage.local.set({ unmask: settings.unmask }); if (settings.unmask) restoreAll(document.body); renderBar(); }
        else if (act === "opts") chrome.runtime.sendMessage({ type: "openOptions" });
      });
      document.body.appendChild(bar);
    }
    bar.querySelector(".mr-meta").innerHTML = sessionId
      ? `session <b>${sessionId.slice(0, 6)}</b> · <b>${entries}</b> pseudonyms`
      : `no session yet`;
    bar.querySelector('[data-act="mask"]').disabled = busy;
    bar.querySelector('[data-act="mask"]').textContent = busy ? "masking…" : "Mask";
    bar.querySelector('[data-act="guard"]').classList.toggle("mr-off", !settings.guard);
    bar.querySelector('[data-act="unmask"]').classList.toggle("mr-off", !settings.unmask);
  }
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === "m") { e.preventDefault(); maskComposer(); }
  });

  // ------------------------------------------------------- navigation
  setInterval(async () => {
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
    if (area !== "local") return;
    for (const k of ["guard", "unmask", "preamble"]) if (k in changes) settings[k] = changes[k].newValue;
    renderBar();
  });

  // ---------------------------------------------------------------- boot
  (async () => {
    settings = Object.assign(settings, await call({ type: "settings" }));
    renderBar();
    const sessions = await storedSessions();
    sessionId = sessions[currentKey] || null;
    await loadVault();
    renderBar();
  })();
})();
