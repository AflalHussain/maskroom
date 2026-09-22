/* Shared by every Maskroom page: who is signed in, how to call the API, how to
   download a file. With single sign-on (auth_mode "oidc") the session cookie
   authenticates and a 401 sends the browser to /auth/login; with auth off the
   legacy keys (if the server sets them) are asked for once per tab and sent
   as headers only, never in a URL. */
window.MaskroomAuth = (() => {
  let cfg = { auth_mode: "off" }, me = null;
  const RANK = { staff: 0, auditor: 1, admin: 2 };
  const STORE = { api: "maskroom-key", admin: "maskroom-admin-key" };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const here = () => location.pathname + location.search;
  const loginUrl = () => "/auth/login?next=" + encodeURIComponent(here());

  function legacy(kind) { try { return sessionStorage.getItem(STORE[kind]) || ""; } catch (e) { return ""; } }
  function setLegacy(kind, v) { try { v ? sessionStorage.setItem(STORE[kind], v) : sessionStorage.removeItem(STORE[kind]); } catch (e) {} }

  function headers(extra = {}) {
    const h = { "X-Requested-With": "maskroom", ...extra };
    if (cfg.auth_mode !== "oidc") {
      const k = legacy("api"); if (k) h["X-API-Key"] = k;
      const a = legacy("admin"); if (a) h["X-Admin-Key"] = a;
    }
    return h;
  }

  async function fetchJson(path, opts = {}) {
    const r = await fetch(path, { credentials: "same-origin", ...opts, headers: headers(opts.headers || {}) });
    if (r.status === 401 && cfg.auth_mode === "oidc") {
      location.href = loginUrl();
      throw Object.assign(new Error("Sign-in required."), { code: 401 });
    }
    let d = null; try { d = await r.json(); } catch (e) {}
    if (!r.ok) throw Object.assign(new Error((d && d.error) || `${r.status} ${r.statusText}`), { code: r.status });
    return d;
  }

  async function download(path, filename) {
    const r = await fetch(path, { credentials: "same-origin", headers: headers() });
    if (r.status === 401 && cfg.auth_mode === "oidc") { location.href = loginUrl(); return; }
    if (!r.ok) { let d = null; try { d = await r.json(); } catch (e) {} throw new Error((d && d.error) || `${r.status} ${r.statusText}`); }
    const url = URL.createObjectURL(await r.blob());
    const a = document.createElement("a");
    a.href = url; a.download = filename || path.split("/").pop();
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  // An anchor that downloads through fetch (so the credentials travel as
  // headers/cookies, never in the URL).
  function link(path, filename, label) {
    return `<a href="#" data-dl="${esc(path)}" data-name="${esc(filename || "")}">${label}</a>`;
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a[data-dl]");
    if (!a || !a.dataset.dl) return;
    e.preventDefault();
    download(a.dataset.dl, a.dataset.name).catch((err) => { a.textContent = "✕ " + err.message; });
  });

  // Ends the Maskroom session, then follows the server's instruction: the
  // identity provider's logout (so SSO does not sign us straight back in) or
  // the signed-out page.
  async function signOut() {
    let target = "/auth/signed-out";
    try {
      const r = await fetch("/auth/logout", { method: "POST", credentials: "same-origin", headers: headers() });
      const d = await r.json(); if (d && d.redirect) target = d.redirect;
    } catch (e) {}
    location.href = target;
  }

  async function boot() {
    try { cfg = await (await fetch("/api/config", { credentials: "same-origin" })).json(); } catch (e) {}
    me = null;
    if (cfg.auth_mode === "oidc") {
      try { const r = await fetch("/api/me", { credentials: "same-origin" }); if (r.ok) me = (await r.json()).principal; } catch (e) {}
    }
    renderHeader();
    return { cfg, me };
  }

  function renderHeader() {
    const el = document.getElementById("mr-auth");
    if (!el) return;
    if (cfg.auth_mode === "oidc") {
      el.innerHTML = me
        ? `<span title="${esc(me.email || "")}">${esc(me.name || me.email)} · ${esc(me.role)}</span> <a href="#" id="mr-signout">sign out</a>`
        : `<a href="${loginUrl()}">sign in</a>`;
      const so = el.querySelector("#mr-signout");
      if (so) so.addEventListener("click", (e) => { e.preventDefault(); signOut(); });
      return;
    }
    const parts = [];
    if (cfg.auth_required) parts.push(`<a href="#" data-legacy="api">${legacy("api") ? "api key ✓" : "api key…"}</a>`);
    if (cfg.admin_auth_required && /^\/admin/.test(location.pathname)) parts.push(`<a href="#" data-legacy="admin">${legacy("admin") ? "admin key ✓" : "admin key…"}</a>`);
    el.innerHTML = parts.join(" · ");
    el.querySelectorAll("[data-legacy]").forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      const kind = a.dataset.legacy;
      const v = window.prompt(kind === "api" ? "API key (the server's MASKROOM_API_KEY)" : "Admin key (the server's MASKROOM_ADMIN_KEY)", legacy(kind));
      if (v === null) return;
      setLegacy(kind, v.trim()); renderHeader();
      document.dispatchEvent(new CustomEvent("maskroom-auth-changed"));
    }));
  }

  // {ok} or {ok:false, reason}; with SSO an anonymous visitor is sent to sign in.
  function requireRole(role) {
    if (cfg.auth_mode !== "oidc") return { ok: true };
    if (!me) { location.href = loginUrl(); return { ok: false, reason: "Sign-in required." }; }
    if ((RANK[me.role] ?? -1) < (RANK[role] ?? 99)) return { ok: false, reason: `You need the ${role} role. Ask an administrator (Admin → Users).` };
    return { ok: true };
  }

  return { fetchJson, download, link, boot, requireRole, headers, legacy, setLegacy, esc, signOut,
           get cfg() { return cfg; }, get me() { return me; } };
})();
