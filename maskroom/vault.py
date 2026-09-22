"""The token store: the mapping from pseudonym tokens back to the original
values, plus the bookkeeping that records which tokens (and which spreadsheet
cells) were numeric so a masked workbook restores to the right types.

One object so the pieces move — and serialize — together, behind a small
interface, instead of three loose structures every collaborator reaches into.
The values are sensitive material; treat a Vault, and any JSON it writes, as
you would the original PII.
"""
from xml.sax.saxutils import escape

_MISSING = object()


class Vault:
    def __init__(self):
        self._map = {}              # token -> original value
        self._numeric_tokens = set()  # tokens whose source cell was numeric (fallback)
        self._numeric_cells = set()   # "Sheet!A1" cells that were numeric (per run)
        self._dirty = set()           # tokens added / promoted since the last mark_clean()

    # ----------------------------------------------------------- storage
    def add(self, token, value):
        if self._map.get(token, _MISSING) != value:
            self._dirty.add(token)
        self._map[token] = value

    def hydrate(self, token, value, numeric=False):
        """Load an entry that is already persisted: set it without marking it
        dirty. Stores use this when reading a vault back."""
        self._map[token] = value
        if numeric:
            self._numeric_tokens.add(token)

    def get(self, token, default=None):
        return self._map.get(token, default)

    def token_for(self, value):
        """The token a value maps to, or None — the inverse lookup callers
        previously rebuilt by hand."""
        for tok, val in self._map.items():
            if val == value:
                return tok
        return None

    def __contains__(self, token):
        return token in self._map

    def __len__(self):
        return len(self._map)

    def __iter__(self):
        return iter(self._map)

    def items(self):
        return self._map.items()

    # ------------------------------------------------- numeric bookkeeping
    def mark_numeric(self, token, cell=None):
        """Record that `token` came from a numeric cell (and, if given, the
        "Sheet!A1" coordinate of that cell)."""
        if token not in self._numeric_tokens:
            self._numeric_tokens.add(token)
            self._dirty.add(token)
        if cell:
            self._numeric_cells.add(cell)

    def was_numeric(self, cell=None, token=None):
        """Whether a cell should restore to a number. The per-cell record wins
        (the same value can be numeric in one sheet and text in another);
        per-token is the fallback when the vault came from a different
        workbook and carries no cell coordinates."""
        if self._numeric_cells:
            return cell in self._numeric_cells
        return token in self._numeric_tokens

    def begin_run(self):
        """Reset the run-scoped state. Cell coordinates are only meaningful
        within a single workbook, so they must not leak across the files of a
        shared session — SessionStore.bind calls this at the start of each run.
        Numeric *tokens* persist (they are session-scoped fallbacks)."""
        self._numeric_cells = set()

    # ------------------------------------------------------- persistence
    def dirty_rows(self):
        """[(token, value, is_numeric)] for entries changed since mark_clean().
        A store writes these, then calls mark_clean(); a failed write leaves
        the set intact for the next attempt."""
        return [(t, self._map[t], t in self._numeric_tokens)
                for t in sorted(self._dirty) if t in self._map]

    def mark_clean(self):
        self._dirty = set()

    # ------------------------------------------------------------- views
    def escaped(self):
        """A copy whose values are XML-escaped, for restoring tokens back into
        Office XML parts without producing invalid markup."""
        v = Vault()
        v._map = {k: escape(val) for k, val in self._map.items()}
        v._numeric_tokens = set(self._numeric_tokens)
        v._numeric_cells = set(self._numeric_cells)
        return v

    # ----------------------------------------------------- serialization
    def to_dict(self):
        return {"mappings": dict(self._map),
                "numeric_tokens": sorted(self._numeric_tokens),
                "numeric_cells": sorted(self._numeric_cells)}

    def update_from_dict(self, data):
        """Merge a serialized vault in. Accepts the current
        {mappings, numeric_tokens, numeric_cells} shape and the legacy flat
        {token: value} form."""
        if "mappings" in data:
            self._map.update(data["mappings"])
            self._dirty.update(data["mappings"])
            self._numeric_tokens.update(data.get("numeric_tokens", []))
            self._dirty.update(data.get("numeric_tokens", []))
            self._numeric_cells.update(data.get("numeric_cells", []))
        else:  # legacy flat {token: value}
            self._map.update(data)
            self._dirty.update(data)

    @classmethod
    def from_dict(cls, data):
        v = cls()
        v.update_from_dict(data)
        return v
