/* Token restore logic shared by the content script and the node tests.
   Mirrors maskroom/engine.py unmask_text(): exact tokens first, then loose
   matches (any case, spaces/hyphens/escaped underscores, truncated or
   extended id) resolved through a normalized index with unique-prefix
   near-matching. Unknown tokens are left alone, never guessed. */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.MaskroomTokens = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const EXACT_RE = /TOK_[A-Z0-9_]+_[0-9A-F]{8,}/g;
  const LOOSE_RE = /\bTOK(?:[\s_\-\\]{1,3}[A-Z]{2,})+?[\s_\-\\]{1,3}([0-9A-F]{6,})/gi;
  const LEFTOVER_RE = /\bTOK[\s_\-\\]{1,3}[A-Z]{2,}[\w\\\-]*/gi;
  const MIN_ID = 6;
  const key = (s) => s.toUpperCase().replace(/[^A-Z0-9]/g, "");

  function buildIndex(vault) {
    const exact = {}, byEntity = {};
    for (const tok of Object.keys(vault)) {
      exact[key(tok)] = tok;
      const i = tok.lastIndexOf("_");
      const entity = key(tok.slice(4, i)), hex = tok.slice(i + 1);
      (byEntity[entity] = byEntity[entity] || []).push([hex, tok]);
    }
    return { vault, exact, byEntity };
  }

  function restore(text, idx) {
    const report = { restored: 0, fuzzy: [], unresolved: [] };
    if (!/tok/i.test(text)) return { text, report };
    let out = text.replace(EXACT_RE, (m) => {
      if (!(m in idx.vault)) return m;
      report.restored++;
      return idx.vault[m];
    });
    out = out.replace(LOOSE_RE, (m, hex) => {
      const k = key(m);
      let tok = idx.exact[k];
      if (!tok) {
        const h = hex.toUpperCase();
        const entity = k.slice(3, k.length - h.length);
        if (h.length >= MIN_ID) {
          const hits = (idx.byEntity[entity] || []).filter(([vh]) => vh.startsWith(h) || h.startsWith(vh));
          if (hits.length === 1) tok = hits[0][1];
        }
      }
      if (!tok) return m;
      if (m !== tok) report.fuzzy.push({ seen: m, token: tok });
      report.restored++;
      return idx.vault[tok];
    });
    report.unresolved = (out.match(LEFTOVER_RE) || []);
    return { text: out, report };
  }

  return { buildIndex, restore, EXACT_RE, LOOSE_RE, LEFTOVER_RE };
});
