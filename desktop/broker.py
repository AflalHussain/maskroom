"""SafePII file broker: an MCP server that serves a folder to Claude, masked.

Why this exists. A folder attached to Claude Desktop goes around every guard the
helper has: the agent reads it inside a sandbox VM, and nothing of ours is in
that path. It cannot be intercepted (`docs/DESKTOP_APP_RESEARCH.md` §5.6), so
instead the raw folder is made unattachable by policy and this becomes the only
way a file reaches the model. Every read is masked as it is served: nothing is
pre-masked, nothing masked is written to disk, and the server audits all of it.

Why HTTP on the loopback interface rather than a stdio extension. A managed MCP
server -- one an administrator pushes, that a user cannot remove -- may speak
only `http` or `sse`; stdio is for servers the user adds themselves. Loopback is
also the one plain-HTTP endpoint Claude Desktop's URL check accepts without
warning. Verified from the app's own configuration schema; §5.6.3(b).

    python desktop/broker.py --root ~/work --server https://safepii.example.com

Prototype, and deliberately narrow: it masks text-shaped files and refuses
everything else rather than passing it through. A file it cannot mask is a file
that must not reach the model, which is the same choice the guard makes about a
message it cannot check (ADR 0006). Spreadsheets and PDFs are the next step and
go through the server's /api/process, not /api/mask.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.1.0"
NAME = "safepii-files"

# The CSRF value the server checks (webui/auth.py CSRF_VALUE) and the same wire
# constant desktop/helper.py sends. A protocol value, not a product name.
CSRF_VALUE = "maskroom"
USER_AGENT = f"SafePII-broker/{__version__}"

# The newest MCP revision this was written against. A client that asks for
# another version gets its own back: the subset used here -- initialize,
# tools/list, tools/call -- has not changed across revisions, and refusing a
# newer client would break the one thing this has to do.
PROTOCOL_VERSION = "2025-06-18"

DEFAULT_PORT = 47821            # fixed, because a managed policy needs a stable URL

# What may be masked, and therefore what may be read. Extensions rather than
# sniffing: a wrong guess here is a disclosure, and the set is the customer's to
# widen deliberately.
TEXT_TYPES = {".txt", ".md", ".markdown", ".rst", ".json", ".log", ".yaml",
              ".yml", ".xml", ".html", ".htm", ".ini", ".cfg", ".conf",
              ".toml", ".tex"}
# A table is not prose, and masking it as prose leaves the names in place: a row
# like `Nimal Perera,912345678V,...` gives a name recognizer no context to work
# with, so the NIC and the phone number were masked and the person was not.
# Observed against a live server; these go through the server's tabular pipeline
# (/api/process), which masks by column instead.
TABULAR_TYPES = {".csv", ".tsv"}
# Masking these corrupts them, and identifiers, keys and hostnames are
# false-positive bait, so they are refused rather than mangled or leaked.
CODE_TYPES = {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".java", ".c", ".h",
              ".cc", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".pl",
              ".sh", ".bash", ".ps1", ".bat", ".sql", ".tf", ".gradle", ".swift",
              ".kt", ".scala", ".r", ".m", ".vb", ".asm"}
# The server masks these properly through /api/process; this prototype does not.
DOCUMENT_TYPES = {".xlsx", ".xlsm", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf"}

MAX_READ_BYTES = 512_000        # one file, before masking
MAX_LIST = 500                  # entries in one listing
MAX_HITS = 200                  # search hits
CACHE_MAX = 64                  # masked files held in memory


def log(msg: str) -> None:
    sys.stderr.write(f"{time.strftime('%H:%M:%S')} broker: {msg}\n")
    sys.stderr.flush()


# ----------------------------------------------------------------- SafePII server
class Client:
    """The same contract extension/background.js and desktop/helper.py use."""

    def __init__(self, base: str, token: str = "", api_key: str = "", timeout: float = 120.0):
        self.base = base.rstrip("/")
        self.token, self.api_key, self.timeout = token, api_key, timeout

    def api(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        headers = {"X-Requested-With": CSRF_VALUE, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        elif self.api_key.startswith("mr_"):
            headers["Authorization"] = "Bearer " + self.api_key
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return {"ok": True, "data": json.loads(res.read() or b"{}")}
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw or b"{}")
            except ValueError:
                payload = {}
            return {"ok": False, "error": payload.get("error") or f"{e.code} {e.reason}"}
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            return {"ok": False, "error": f"cannot reach SafePII at {self.base} "
                                         f"({getattr(e, 'reason', e)})"}


    def upload(self, path: str, filename: str, blob: bytes, fields: dict) -> dict:
        """Multipart by hand, as extension/background.js and the helper do: one
        dependency-free POST is easier to keep than a library on a frozen build."""
        boundary = "----SafePIIBroker" + os.urandom(8).hex()
        out = []
        for key, value in fields.items():
            out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode())
        out.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                   f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n".encode())
        out.append(blob)
        out.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(out)
        headers = {"X-Requested-With": CSRF_VALUE, "Accept": "application/json",
                   "User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        elif self.api_key.startswith("mr_"):
            headers["Authorization"] = "Bearer " + self.api_key
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return {"ok": True, "data": json.loads(res.read() or b"{}")}
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except ValueError:
                payload = {}
            return {"ok": False, "error": payload.get("error") or f"{e.code} {e.reason}"}
        except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
            return {"ok": False, "error": f"cannot reach SafePII at {self.base} "
                                         f"({getattr(e, 'reason', e)})"}

    def download(self, path: str) -> tuple[bytes | None, str | None]:
        headers = {"X-Requested-With": CSRF_VALUE, "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        elif self.api_key.startswith("mr_"):
            headers["Authorization"] = "Bearer " + self.api_key
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(self.base + path, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.read(), None
        except urllib.error.HTTPError as e:
            return None, f"{e.code} {e.reason}"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return None, str(getattr(e, "reason", e))


class BrokerError(Exception):
    """Something the model should be told plainly, not a crash."""


# ----------------------------------------------------------------- the workspace
class Names:
    """What a file is called once SafePII is between the folder and the model.

    `Nimal Perera - loan.csv` discloses before a byte is read, so a name cannot
    be passed through. Masking one is harder than it looks, and measured against
    the real engine rather than assumed: `Nimal Perera - loan.csv` masks, but
    `kyc/Nimal Perera - loan.csv` does not, because the path separator glues the
    name to the folder and the detector is reading prose; `Nimal_Perera_loan.csv`
    does not either. Both were reproduced against a live server.

    So there are three policies, and the safe one is the default.

    - `handles`: every name becomes `f003.csv` in `d01/`, and a map remembers
      which is which. Nothing about a name can leak, whatever the detector does
      or does not notice. The cost is that the model loses the meaning a folder
      tree carries.
    - `mask`: each path component is masked separately, and a component the
      detector passes over is probed again with its separators turned into
      spaces, with whatever values that finds substituted back into the original
      spelling. Better than passing names through, and still best-effort: it
      cannot be promised.
    - `real`: names as they are. For a folder whose names are known to be safe.
    """

    POLICIES = ("handles", "mask", "real")

    def __init__(self, workspace, policy: str = "handles"):
        if policy not in self.POLICIES:
            raise BrokerError(f"name policy must be one of {', '.join(self.POLICIES)}")
        self.ws = workspace
        self.policy = policy
        self.lock = threading.Lock()
        self.to_real: dict[str, str] = {}       # what the model sees -> the real relative path
        self.shown: dict[str, str] = {}         # the real relative path -> what the model sees
        self.dirs: dict[str, str] = {}          # real directory -> its handle

    # ---- handles
    def _handle(self, rel: str) -> str:
        parent = str(Path(rel).parent.as_posix())
        if parent in (".", ""):
            folder = ""
        else:
            with self.lock:
                if parent not in self.dirs:
                    self.dirs[parent] = f"d{len(self.dirs) + 1:02d}"
                folder = self.dirs[parent] + "/"
        with self.lock:
            n = len(self.shown) + 1
        return f"{folder}f{n:03d}{Path(rel).suffix.lower()}"

    # ---- best-effort masking
    def _mask_component(self, comp: str, last: bool) -> str:
        """One path component. The extension is never part of it.

        `notes.md` probed as prose came back as `notes.TOK_PERSON_…`: the
        detector read `md` as a surname, and substituting it destroyed the
        extension the model needs to know what the file is. Observed against a
        live server, and the reason the extension is now held back from every
        pass rather than trusted to survive one.
        """
        stem, suffix = (comp, "")
        if last:
            dot = comp.rfind(".")
            if 0 < dot < len(comp) - 1:
                stem, suffix = comp[:dot], comp[dot:]
        masked, _ = self.ws.mask(stem)
        if masked != stem:
            return masked + suffix
        # A separator defeats prose detection. Probe the same characters with the
        # separators turned into spaces, then put whatever it found back into the
        # spelling the file actually has.
        probe = re.sub(r"[_.\-]+", " ", stem).strip()
        if probe == stem or not probe:
            return stem + suffix
        _m, data = self.ws.mask(probe)
        out = stem
        for finding in data.get("findings") or []:
            value = (finding.get("text") or "").strip()
            token = finding.get("token")
            if not value or not token:
                continue
            # Joined rather than escaped-then-patched: whether re.escape
            # escapes a space has changed between Python versions.
            pattern = r"[\s_.\-]+".join(re.escape(word) for word in value.split())
            out = re.sub(pattern, token, out, flags=re.IGNORECASE)
        return out + suffix

    def _masked_path(self, rel: str) -> str:
        parts = [p for p in rel.split("/") if p]
        return "/".join(self._mask_component(p, i == len(parts) - 1)
                        for i, p in enumerate(parts))

    # ---- the one entry point
    def show(self, rels: list[str]) -> list[str]:
        out = []
        for rel in rels:
            with self.lock:
                seen = self.shown.get(rel)
            if seen is not None:
                out.append(seen)
                continue
            if self.policy == "real":
                name = rel
            elif self.policy == "handles":
                name = self._handle(rel)
            else:
                name = self._masked_path(rel)
            with self.lock:
                self.shown[rel] = name
                self.to_real[name] = rel
            out.append(name)
        return out

    def real(self, given: str) -> str | None:
        with self.lock:
            return self.to_real.get(given)

    def note(self) -> str:
        if self.policy == "handles":
            return ("File names are replaced with handles such as d01/f003.csv, because a name "
                    "can disclose as much as the file does. Use the handle exactly as given; "
                    "ask the person if you need to know what a file is called.\n")
        if self.policy == "mask":
            return ("Personal data in file names is masked where SafePII recognises it. A name is "
                    "prose to the detector, so this is best-effort; use names exactly as given.\n")
        return ""


class Workspace:
    """One real folder, served masked, through one SafePII session.

    Everything that decides what leaves the machine is here: which paths are
    reachable, which types may be masked, and what a name is called once it has
    been masked.
    """

    def __init__(self, root: Path, client: Client, session: str = "",
                 staging: Path | None = None, allow_code: bool = False,
                 names: str = "handles"):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise BrokerError(f"{self.root} is not a folder")
        self.client = client
        self.session = session
        self.staging = Path(staging).expanduser().resolve() if staging else self.root.parent / (self.root.name + "-safepii-out")
        self.allow_code = allow_code
        self.lock = threading.Lock()
        self.cache: dict[tuple, str] = {}       # (realpath, mtime_ns, size) -> masked text
        self.names_policy = Names(self, names)
        self.preamble_sent = False

    # ---- paths
    def resolve(self, given: str) -> Path:
        """A path inside the root, or an error.

        Resolved before the check, so a symlink out of the tree and `..` are both
        refused rather than followed -- the same rule Claude Desktop applies to
        the folders it will attach, and for the same reason.
        """
        raw = (given or "").strip().replace("\\", "/").lstrip("/")
        # The model can only quote the name we gave it, so that is the name that
        # has to work. Anything else is looked up as a real path.
        raw = self.names_policy.real(raw) or raw
        target = (self.root / raw).expanduser()
        try:
            real = target.resolve()
        except OSError as e:
            raise BrokerError(f"cannot resolve {given!r}: {e}") from None
        if real != self.root and self.root not in real.parents:
            raise BrokerError(f"{given!r} is outside the folder SafePII serves")
        return real

    def classify(self, path: Path) -> tuple[str, str]:
        """(state, why). Only "text" may be read."""
        ext = path.suffix.lower()
        if ext in TEXT_TYPES:
            return "text", ""
        if ext in TABULAR_TYPES:
            return "tabular", ""
        if ext in CODE_TYPES:
            return ("text", "") if self.allow_code else (
                "refused", "source code: masking it would corrupt it, so SafePII does not serve it")
        if ext in DOCUMENT_TYPES:
            return "refused", f"{ext} needs SafePII's document pipeline, which this broker does not have yet"
        return "refused", f"SafePII cannot check {ext or 'a file with no extension'}, so it is not served"

    # ---- masking
    def ensure_session(self) -> str:
        with self.lock:
            if self.session:
                return self.session
            res = self.client.api("/api/session", "POST", {})
            if not res["ok"]:
                raise BrokerError(f"could not start a SafePII session: {res['error']}")
            self.session = res["data"].get("session_id") or res["data"].get("id") or ""
            if not self.session:
                raise BrokerError("SafePII did not return a session id")
            log(f"session {self.session} for {self.root}")
            return self.session

    def mask(self, text: str) -> tuple[str, dict]:
        """Text through the server. The session is the vault, so the same value
        gets the same token everywhere in this workspace -- which is what makes
        searching for a masked term work at all."""
        if not text.strip():
            return text, {}
        res = self.client.api("/api/mask", "POST",
                              {"text": text, "session_id": self.ensure_session()})
        if not res["ok"]:
            raise BrokerError(f"SafePII could not mask this: {res['error']}")
        data = res["data"]
        return data.get("masked", text), data

    def unmask(self, text: str) -> tuple[str, dict]:
        res = self.client.api("/api/unmask", "POST",
                              {"text": text, "session_id": self.ensure_session()})
        if not res["ok"]:
            raise BrokerError(f"SafePII could not restore this: {res['error']}")
        data = res["data"]
        return data.get("text", text), data

    def show_names(self, rels: list[str]) -> list[str]:
        return self.names_policy.show(rels)

    def masked_tabular(self, real: Path) -> str:
        """Through /api/process, so the column rules apply. The masked copy comes
        back as a file, which is read as text -- the same two-step the helper
        uses for an attachment."""
        blob = real.read_bytes()
        res = self.client.upload("/api/process", real.name, blob,
                                 {"session_id": self.ensure_session(), "preview": "false"})
        if not res["ok"]:
            raise BrokerError(f"SafePII could not mask {real.name}: {res['error']}")
        data = res["data"]
        out = (data.get("downloads") or {}).get("output")
        run = data.get("run_id")
        if not out or not run:
            raise BrokerError(f"SafePII masked {real.name} but returned no file to read")
        body, err = self.client.download(f"/api/download/{run}/{urllib.parse.quote(out)}")
        if err or body is None:
            raise BrokerError(f"SafePII masked {real.name} but the masked copy could not be read ({err})")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise BrokerError(f"the masked copy of {real.name} is not text") from None

    def masked_text(self, real: Path) -> str:
        st = real.stat()
        if st.st_size > MAX_READ_BYTES:
            raise BrokerError(f"{real.name} is {st.st_size // 1024} kB; the limit is "
                              f"{MAX_READ_BYTES // 1024} kB per read")
        key = (str(real), st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self.cache.get(key)
        if hit is not None:
            return hit
        if self.classify(real)[0] == "tabular":
            masked = self.masked_tabular(real)
        else:
            try:
                raw = real.read_text("utf-8")
            except UnicodeDecodeError:
                raise BrokerError(f"{real.name} is not text SafePII can read, so it is not served") from None
            except OSError as e:
                raise BrokerError(f"cannot read {real.name}: {e}") from None
            masked, _ = self.mask(raw)
        with self.lock:
            if len(self.cache) >= CACHE_MAX:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = masked
        return masked

    def note(self) -> str:
        """Said once per session: masked text has to be announced, which is what
        the chat preamble does for a typed message."""
        with self.lock:
            if self.preamble_sent:
                return ""
            self.preamble_sent = True
        return (self.names_policy.note()
                + "SafePII has replaced the personal data in this folder with tokens of the form "
                "TOK_<TYPE>_<ID>. Each token stands for one real value and the same value always "
                "has the same token. Treat them as opaque identifiers: quote them exactly, never "
                "guess what is behind one, and never rewrite one to look like a name. The person "
                "reading your reply sees the real values in place of the tokens.\n\n")


# ----------------------------------------------------------------- the tools
def tool_list_files(ws: Workspace, args: dict) -> str:
    where = ws.resolve(args.get("path") or ".")
    pattern = (args.get("glob") or "").strip()
    if not where.is_dir():
        raise BrokerError(f"{args.get('path')!r} is not a folder")
    entries, truncated = [], False
    for dirpath, dirnames, filenames in os.walk(where):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(ws.root).as_posix()
            if pattern and not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(fn, pattern):
                continue
            if len(entries) >= MAX_LIST:
                truncated = True
                break
            state, why = ws.classify(full)
            try:
                size = full.stat().st_size
            except OSError:
                continue
            entries.append({"rel": rel, "state": state, "why": why, "size": size})
        if truncated:
            break
    shown = ws.show_names([e["rel"] for e in entries])
    lines = [ws.note() + f"{len(entries)} file(s) in {args.get('path') or '.'}:"]
    for e, name in zip(entries, shown):
        if e["state"] != "refused":
            lines.append(f"  {name}  ({e['size']} bytes)")
        else:
            lines.append(f"  {name}  -- NOT SERVED: {e['why']}")
    if truncated:
        lines.append(f"  ... more than {MAX_LIST} files; narrow it with path or glob.")
    return "\n".join(lines)


def tool_read_file(ws: Workspace, args: dict) -> str:
    real = ws.resolve(args.get("path") or "")
    if not real.is_file():
        raise BrokerError(f"{args.get('path')!r} is not a file")
    state, why = ws.classify(real)
    if state == "refused":
        raise BrokerError(why)
    masked = ws.masked_text(real)
    lines = masked.splitlines()
    # Sliced after masking, never before: a token is longer than the value it
    # replaced, so any offset taken from the real bytes points somewhere else.
    start = max(1, int(args.get("offset") or 1))
    limit = int(args.get("limit") or 0)
    end = start + limit - 1 if limit > 0 else len(lines)
    body = "\n".join(f"{i}\t{line}" for i, line in enumerate(lines[start - 1:end], start))
    head = ws.note() + f"{args['path']} (masked by SafePII, lines {start}-{min(end, len(lines))} of {len(lines)}):\n"
    return head + body


def tool_search_files(ws: Workspace, args: dict) -> str:
    """The query is masked too, which is the whole trick: tokens are
    deterministic, so a search for a real name becomes a search for that name's
    token and matches the masked text. An exact value only -- a partial or
    fuzzy search cannot work against masked content, and saying so is better
    than quietly returning nothing."""
    query = (args.get("query") or "").strip()
    if not query:
        raise BrokerError("nothing to search for")
    where = ws.resolve(args.get("path") or ".")
    masked_query, _ = ws.mask(query)
    try:
        needle = re.compile(re.escape(masked_query), re.IGNORECASE)
    except re.error as e:
        raise BrokerError(f"bad search term: {e}") from None
    hits, scanned, skipped = [], 0, 0
    for dirpath, dirnames, filenames in os.walk(where):
        dirnames[:] = [d for d in sorted(dirnames) if not d.startswith(".")]
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if fn.startswith(".") or ws.classify(full)[0] == "refused":
                skipped += 1
                continue
            try:
                text = ws.masked_text(full)
            except BrokerError:
                skipped += 1
                continue
            scanned += 1
            rel = full.relative_to(ws.root).as_posix()
            shown = ws.show_names([rel])[0]
            for n, line in enumerate(text.splitlines(), 1):
                if needle.search(line):
                    hits.append(f"  {shown}:{n}: {line.strip()[:300]}")
                    if len(hits) >= MAX_HITS:
                        break
            if len(hits) >= MAX_HITS:
                break
        if len(hits) >= MAX_HITS:
            break
    told = ""
    if masked_query != query:
        told = ("The term you searched for is personal data, so SafePII searched for its token "
                f"({masked_query}) instead. Matches are the same ones.\n")
    if not hits:
        return (ws.note() + told + f"No matches in {scanned} file(s)"
                + (f"; {skipped} not served." if skipped else ".")
                + " Masked content only matches a whole value, not part of one.")
    return ws.note() + told + f"{len(hits)} match(es) in {scanned} file(s):\n" + "\n".join(hits)


def tool_write_file(ws: Workspace, args: dict) -> str:
    """Tokens restored on the way out, and written beside the folder rather than
    into it. A file the model wrote is a file somebody should look at before it
    replaces the original, and `mode: ro` on the workspace means the original
    was never writable anyway."""
    rel = (args.get("path") or "").strip().replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("..") or ".." in Path(rel).parts:
        raise BrokerError("give a plain relative path to write")
    content = args.get("content")
    if not isinstance(content, str):
        raise BrokerError("content must be text")
    restored, report = ws.unmask(content)
    out = (ws.staging / rel).resolve()
    if ws.staging not in out.parents and out != ws.staging:
        raise BrokerError("that path would land outside the SafePII output folder")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(restored, "utf-8")
    said = [f"Written to {out} with {report.get('restored', 0)} value(s) restored.",
            "This is SafePII's output folder, not the folder you were given: somebody has to "
            "look at it before it replaces anything."]
    if report.get("unresolved"):
        said.append("These tokens are not in this workspace's vault and were left as they are: "
                    + ", ".join(sorted(set(report["unresolved"]))[:20])
                    + ". They were probably masked in another session.")
    return "\n".join(said)


TOOLS = [
    {"name": "list_files",
     "description": "List the files SafePII serves from the connected folder. Personal data in "
                    "file names is replaced with tokens, and files SafePII cannot check are "
                    "listed but marked as not served. Use the names exactly as given.",
     "handler": tool_list_files,
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "Folder to list, relative to the root. Default: the root."},
         "glob": {"type": "string", "description": "Optional pattern, e.g. *.csv"}}}},
    {"name": "read_file",
     "description": "Read one file with its personal data replaced by TOK_<TYPE>_<ID> tokens. "
                    "Line numbers refer to the masked text. Files SafePII cannot check are refused.",
     "handler": tool_read_file,
     "inputSchema": {"type": "object", "required": ["path"], "properties": {
         "path": {"type": "string", "description": "File path as list_files gave it."},
         "offset": {"type": "integer", "description": "First line to return (1-based)."},
         "limit": {"type": "integer", "description": "How many lines."}}}},
    {"name": "search_files",
     "description": "Search the served files. A search term that is itself personal data is "
                    "masked first, so searching for a real name finds that name's token. Only "
                    "whole values match, never part of one.",
     "handler": tool_search_files,
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string"},
         "path": {"type": "string", "description": "Limit the search to this subfolder."}}}},
    {"name": "write_file",
     "description": "Write a file back. Tokens are restored to the real values first, and the "
                    "file is written to SafePII's output folder beside the connected folder, "
                    "never over the original.",
     "handler": tool_write_file,
     "inputSchema": {"type": "object", "required": ["path", "content"], "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}}},
]
BY_NAME = {t["name"]: t for t in TOOLS}


def wire_tools() -> list[dict]:
    return [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]


# ----------------------------------------------------------------- before sharing
def preview(root, allow_code: bool = False) -> dict:
    """What would be served, counted without asking the server anything.

    The person about to share a folder is entitled to know what leaves it before
    it starts leaving, and the answer has to come back instantly: this walks the
    tree and classifies by extension, with no network and no masking.
    """
    root = Path(root).expanduser()
    out = {"root": str(root), "served": 0, "refused": 0, "bytes": 0,
           "refusals": {}, "examples": []}
    if not root.is_dir():
        out["error"] = f"{root} is not a folder"
        return out
    ext_of = lambda p: p.suffix.lower()      # noqa: E731
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            path = Path(dirpath) / fn
            ext = ext_of(path)
            if ext in TEXT_TYPES or ext in TABULAR_TYPES or (allow_code and ext in CODE_TYPES):
                out["served"] += 1
                try:
                    out["bytes"] += path.stat().st_size
                except OSError:
                    pass
                if len(out["examples"]) < 3:
                    out["examples"].append(path.relative_to(root).as_posix())
            else:
                out["refused"] += 1
                if ext in CODE_TYPES:
                    why = "source code"
                elif ext in DOCUMENT_TYPES:
                    why = "needs the document pipeline"
                else:
                    why = f"cannot be checked ({ext or 'no extension'})"
                out["refusals"][why] = out["refusals"].get(why, 0) + 1
    return out


def describe(p: dict) -> str:
    """The preview as one short paragraph for the panel."""
    if p.get("error"):
        return p["error"]
    if not p["served"] and not p["refused"]:
        return "That folder is empty."
    kb = p["bytes"] // 1024
    said = [f"Claude would see {p['served']} file(s)"
            + (f" ({kb} kB)" if kb else "") + ", masked."]
    if p["refused"]:
        parts = ", ".join(f"{n} {why}" for why, n in sorted(p["refusals"].items()))
        said.append(f"{p['refused']} would not be served: {parts}.")
    said.append("File names are replaced with handles.")
    return " ".join(said)


# ----------------------------------------------------------------- MCP over HTTP
def handle_rpc(ws: Workspace, msg: dict) -> dict | None:
    """One JSON-RPC message in, one response out (None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        asked = params.get("protocolVersion")
        return ok({"protocolVersion": asked if isinstance(asked, str) and asked else PROTOCOL_VERSION,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": NAME, "version": __version__},
                   "instructions": "Files from this folder arrive with personal data replaced by "
                                   "TOK_<TYPE>_<ID> tokens. Quote them exactly and never guess "
                                   "what is behind one."})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": wire_tools()})
    if method == "tools/call":
        tool = BY_NAME.get(params.get("name"))
        if tool is None:
            return err(-32602, f"no tool called {params.get('name')!r}")
        try:
            text = tool["handler"](ws, params.get("arguments") or {})
        except BrokerError as e:
            # A refusal the model should read and act on, not a transport fault.
            return ok({"content": [{"type": "text", "text": f"SafePII did not do that: {e}"}],
                       "isError": True})
        except Exception as e:  # noqa: BLE001
            log(f"{params.get('name')} failed: {e!r}")
            return ok({"content": [{"type": "text", "text": f"SafePII hit an internal error: {e}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": text}]})
    if mid is None:
        return None
    return err(-32601, f"method {method!r} is not supported")


class Handler(BaseHTTPRequestHandler):
    server_version = f"SafePII-broker/{__version__}"
    protocol_version = "HTTP/1.1"
    workspace: Workspace
    auth_token: str = ""

    def log_message(self, fmt, *a):            # quiet; the broker logs what matters
        pass

    # ---- guards
    def origin_ok(self) -> bool:
        """A browser on this machine must not be able to drive a local MCP
        server, which is what the spec's Origin rule is for: a page on any site
        can POST to 127.0.0.1, and this one reads files."""
        origin = self.headers.get("Origin")
        if not origin:
            return True                         # a real MCP client sends none
        host = urllib.parse.urlparse(origin).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1")

    def authorised(self) -> bool:
        if not self.auth_token:
            return True
        given = (self.headers.get("Authorization") or "").strip()
        return given == "Bearer " + self.auth_token

    def send_json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # No server-initiated stream: the spec allows refusing the SSE channel.
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        if not self.origin_ok():
            self.send_json(403, {"error": "cross-origin requests are refused"})
            return
        if not self.authorised():
            self.send_json(401, {"error": "bad or missing bearer token"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b""
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            self.send_json(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "not JSON"}})
            return
        if isinstance(msg, list):               # batches: older revisions allow them
            out = [r for r in (handle_rpc(self.workspace, m) for m in msg) if r is not None]
            if not out:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_json(200, out)
            return
        if not isinstance(msg, dict):
            self.send_json(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32600, "message": "not a request"}})
            return
        reply = handle_rpc(self.workspace, msg)
        if reply is None:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_json(200, reply)


def make_server(workspace: Workspace, port: int = DEFAULT_PORT, token: str = "") -> ThreadingHTTPServer:
    """Bound to the loopback address only. Nothing off this machine can reach it."""
    handler = type("BoundHandler", (Handler,), {"workspace": workspace, "auth_token": token})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd


def policy_snippet(port: int, token: str = "") -> str:
    """What an administrator puts in Claude Desktop's managed configuration."""
    entry = {"name": NAME, "url": f"http://127.0.0.1:{port}/mcp", "transport": "http"}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return json.dumps({"managedMcpServers": [entry]}, indent=2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve a folder to Claude, masked by SafePII.")
    ap.add_argument("--root", required=True, help="the real folder to serve")
    ap.add_argument("--server", default=os.environ.get("SAFEPII_SERVER", "http://127.0.0.1:5170"))
    ap.add_argument("--token", default=os.environ.get("SAFEPII_TOKEN", ""), help="SafePII login token")
    ap.add_argument("--api-key", default=os.environ.get("SAFEPII_API_KEY", ""), help="SafePII service key")
    ap.add_argument("--session", default="", help="an existing SafePII session to reuse")
    ap.add_argument("--staging", default="", help="where write_file puts its output")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--auth", default="", help="require this bearer token from the MCP client")
    ap.add_argument("--names", default="handles", choices=Names.POLICIES,
                    help="what the model sees a file called: opaque handles (default), "
                         "best-effort masking, or the real name")
    ap.add_argument("--allow-code", action="store_true",
                    help="serve source files too (masking them can corrupt them)")
    args = ap.parse_args(argv)

    client = Client(args.server, token=args.token, api_key=args.api_key)
    try:
        ws = Workspace(Path(args.root), client, session=args.session,
                       staging=Path(args.staging) if args.staging else None,
                       allow_code=args.allow_code, names=args.names)
    except BrokerError as e:
        log(str(e))
        return 1
    httpd = make_server(ws, args.port, args.auth)
    log(f"serving {ws.root} masked, through {args.server}")
    log(f"output folder for write_file: {ws.staging}")
    log(f"listening on http://127.0.0.1:{args.port}/mcp")
    if not args.auth:
        log("no --auth token: any process on this machine can call this broker")
    sys.stderr.write("\nClaude Desktop managed configuration:\n"
                     + policy_snippet(args.port, args.auth) + "\n\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
