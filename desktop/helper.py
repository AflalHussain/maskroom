"""SafePII desktop helper for Claude Desktop (Windows).

Sits beside Claude Desktop the way the Chrome extension sits inside claude.ai:
when the composer has focus, a small floating bar appears above it with a Mask
button. Mask (button, or Ctrl+Shift+M anywhere) reads the composer through UI
Automation, sends the text to the SafePII server (POST /api/mask), and writes
the pseudonymized text back in place through ValuePattern.SetValue - the write
path verified by scripts/desktop/uia_composer_probe.py. You still press send.

Guard (default on, like the extension): a low-level keyboard hook sees Enter
while Claude Desktop is in front. If the composer holds text that has not been
checked yet, the keypress is swallowed and the text goes through /api/mask
first. If anything was masked the send stays held so you can read what will
leave the machine, then press Enter again; if nothing needed masking, Enter is
replayed and the send goes through as typed. Shift+Enter (newline) and Enter
outside the composer are never held. It fails closed: if the server cannot be
reached the send is held and the bar says why; switch the guard off to send
anyway. The Send button is not guarded (only the keyboard is hooked).

Files (guard on): when Claude's paperclip opens the Windows file dialog, the
helper watches it. Confirming a supported file (.xlsx .xlsm .pdf .docx .pptx
.csv .tsv .txt .json) is intercepted: the file goes to the server
(POST /api/process), the masked copy is written to the SafePII files folder,
its path is typed into the dialog's File name box and the confirm is
replayed, so Claude attaches the masked copy and never sees the original.
A type SafePII cannot mask (an image, an archive) is attached as it is with
a warning on the bar, as the extension does; only a supported type that
*should* have been masked and could not holds the attachment. Explorer drag-and-drop onto
Claude is blocked (the overlay window takes the drop and rejects it) so files
go through the paperclip; Ctrl+V of files is intercepted the same way as the
dialog.

Unmask (default on): the helper keeps the session's vault locally and
restores tokens two ways. Hover the mouse over a token in a reply and a
tooltip shows that line with the real values (UI Automation RangeFromPoint,
about a millisecond). Copy text out of Claude and the clipboard is restored
before you paste it anywhere. Ctrl+Shift+U restores the clipboard on demand.
Overlay (experimental, default on): the real values are painted over the
tokens in replies on a transparent, click-through window that covers Claude.
Tokens are located by character offset in the page text (one move per token,
verified by reading the range back; the text search that the probe showed
mis-aligning is not used), their rectangles are refreshed every tick (one
call per token) on its own thread, so a slow read can never delay the Enter
guard. Tokens are found by walking the *visible lines*: a line's text and
rectangle cost one call each, and a token's offset inside its own line is
small, so positioning it is cheap. Between walks each token's rectangle is
re-read, which is what follows scrolling. Font family, size, weight, italic
and colour come from the range's attributes once per walk (a web font that
is not installed on the PC falls back to Segoe UI). The composer is never
overlaid. Set "overlayHideOnScroll" (or the settings checkbox) to hide the
overlay while scrolling instead of following it.
If it does not look right, switch it off on the bar; hover and copy remain.

Run:   py -m pip install uiautomation
       py desktop\\helper.py

Sessions follow the chat, as the extension's do. The chat is identified by
the page URL that Chromium exposes as the document's value (claude.ai/chat/
<id>), else by the tokens visible on the page (every token id is random, so
a token names its session), else by the chat title. Each chat gets its own
SafePII session (vault); switching chats switches the session used for
masking. Restore (hover, copy, overlay) searches every known vault at once,
so it never depends on which chat is current.

Sign-in: the gear button's "Sign in" opens the server's login page in the
system browser. The server sends the browser through the identity provider and
finally to http://127.0.0.1:<port>/done on this machine with a one-time code,
which the helper exchanges (POST /auth/exchange) for a session token it then
sends as `Authorization: Bearer`. A service key (mr_...) pasted into settings
works too, for servers without sign-on or for shared machines.

Config lives in %APPDATA%\\SafePII\\helper.json (server URL, token, session
id). An administrator can pre-set the server URL for every user of a machine
in %ProgramData%\\SafePII\\helper.json (read first, like the extension's
managed serverUrl key).

Threads: UI Automation and the server calls run on one worker thread (COM is
apartment-bound); the global hotkeys run on their own thread because
RegisterHotKey delivers to the registering thread; tkinter owns the main thread
and only ever reads a queue. Nothing here presses Enter in Claude.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import collections
import glob as globlib
import json
import ntpath
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# The file broker lives beside this file and is imported, not spawned, so it
# shares the sign-in, the settings and the vault the bar already holds. Optional:
# a helper without it keeps every other guard.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import broker as broker_mod
except Exception as _broker_err:  # noqa: BLE001
    broker_mod = None
    _BROKER_IMPORT_ERROR = _broker_err

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    # The stdio bridge is this same executable with --stdio, and it never touches
    # the accessibility layer: it relays JSON-RPC to the helper that does. Making
    # a missing uiautomation fatal there would kill the bridge on any machine
    # where the import is unavailable, for a dependency it does not use.
    if "--stdio" not in sys.argv[1:]:
        print("Missing dependency. Run:  py -m pip install uiautomation")
        sys.exit(1)
    auto = None

APP_NAME = "SafePII"
# Its own number, because the helper ships on its own cadence. Sent to the
# server on every call, so support can answer "which build?" without asking,
# and an update check has something to compare against.
__version__ = "0.3.0"
OLD_APP_NAME = "Maskroom"   # settings written before the product was named
# The CSRF value the server checks (webui/auth.py CSRF_VALUE). A wire constant,
# not a product name: renaming it would need a coordinated server change.
CSRF_VALUE = "maskroom"
USER_AGENT = f"SafePII-helper/{__version__} (Windows)"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "helper.json"
MACHINE_CONFIG = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / APP_NAME / "helper.json"
DEFAULTS = {
    "serverUrl": "http://127.0.0.1:5170",
    "apiKey": "",          # a named service key (mr_...) or the legacy shared key
    "token": "",           # login-session token from /auth/exchange (browser sign-in)
    "sessionId": None,     # the session used last (display only; chats decide)
    "chats": {},           # chat url -> session id
    "sessions": {},        # session id -> {"title", "chat", "last"}
    "preamble": True,
    "guard": True,
    "unmask": True,        # hover tooltip + clipboard restore
    "overlay": True,       # paint real values over tokens in replies (experimental)
    "overlayHideOnScroll": False,   # hide while scrolling instead of tracking the scroll
    "overlayDebug": False,  # log every walked line (to find out why a token was missed)
    "fileGuard": True,     # intercept the file dialog / paste / drop
    "onGuardFailure": "hold",  # "hold" (nothing sends) or "warn" (sends, loudly)
    "forgetAfterIdleMinutes": 15,   # drop the real values after this much idle time
    "blockDrops": False,   # an invisible window that eats clicks; opt in (see the README)
    "filesDir": "",        # where masked copies are written (default: <config>/files)
    "watchDownloads": True,   # restore tokens in files Claude produces
    "downloadsDir": "",    # default: the user's Downloads folder
    "deleteTokenCopy": False,  # keep both by default: deleting a download is the user's call
    "restoreUnreadable": "ask",  # ask | always | never: send Office files whose tokens
                                 # cannot be seen from here to the server to be checked
    "preambleSent": [],
    # Sharing a folder with Claude through the file broker (docs/adr/0009).
    # Off until the user picks a folder: nothing is served by simply installing.
    "sharing": True,           # may the user share a folder at all
    "shareRoot": "",           # the folder being served, remembered across restarts
    "shareNames": "mask",      # mask | handles | real (see broker.Names)
    "shareAllowCode": False,   # serve source files too
    "sharePort": 47821,        # the loopback port the managed policy points at
    "watchClaudeConfig": True,  # report MCP servers in Claude Desktop that are not ours
}
OVERLAY_TICK_S = 0.06      # overlay thread: how often rectangles are refreshed
OVERLAY_WALK_MIN_S = 0.30   # never re-walk the visible lines more often than this
OVERLAY_WALK_MAX_S = 1.50   # but do walk at least this often while tokens are on screen
OVERLAY_MAX_LINES = 250     # safety cap on the line walk
SHARED = {"scroll_at": 0.0, "dialog_open": False, "drag_at": 0.0, "blocking": False,
          "blocking_since": 0.0, "index": None, "composer_rect": None,
          "dialog_confirm_rect": None, "dialog_list_rect": None, "dialog_rect": None,
          "worker_beat": 0.0, "worker_busy": "", "alarm": "",
          # A reason string (or True) while Claude is waiting for a folder, which
          # the bar turns into its picker. Cleared when the person answers.
          "want_folder": None,
          # Screen rectangles of the bar's own windows, published by the bar and
          # read by the mouse hook so it can tell a click on us from a click
          # anywhere else. panel_rect is None whenever the panel is closed, which
          # is what keeps the hook's fast path fast.
          "panel_rect": None, "pill_rect": None}
ASK_FOLDER_S = 120.0       # how long a request_folder call waits for the person
UPDATE_EVERY_S = 6 * 3600   # how often to ask the server what the current build is
WORKER_DEAD_S = 15.0       # no heartbeat for this long, and not busy: not working
BLOCKER_MAX_S = 6.0        # the drop blocker comes down after this, drop or no drop
OVERLAY_MAX_TOKENS = 80
MAX_KNOWN_SESSIONS = 25    # vaults kept locally for restore
MASK_EXTS = (".xlsx", ".xlsm", ".pdf", ".docx", ".pptx", ".csv", ".tsv", ".txt", ".json")
# What the server can restore tokens inside (maskroom/restore.py). No PDF: a
# redacted PDF has had its bytes destroyed, so there is nothing to put back.
RESTORE_EXTS = (".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm", ".xml", ".yaml",
                ".yml", ".xlsx", ".xlsm", ".docx", ".pptx")
ZIP_EXTS = (".xlsx", ".xlsm", ".docx", ".pptx")      # tokens are deflated out of sight
DOWNLOAD_POLL_S = 1.5
MAX_UPLOAD = 25 * 1024 * 1024
DIALOG_POLL_S = 0.4
DIALOG_CLASSES = ("#32770", "Shell Dialog", "OperationStatusWindow")
OPEN_BUTTONS = ("open", "ok", "attach", "select", "choose")   # never "save": that is a download
TIP_MAX_CHARS = 400
SIGNIN_TIMEOUT_S = 300
CLAUDE_EXE = "claude.exe"
COMPOSER_CLASS_HINT = "ProseMirror"
POLL_S = 0.12
HIDE_GRACE_S = 0.8

# ----------------------------------------------------------------------------- config
def adopt_old_settings() -> None:
    """Carry settings over from the folder the helper used before the product
    was named SafePII, so an upgrade does not silently sign the user out and
    lose every chat-to-session binding."""
    if CONFIG_FILE.exists() or not _IS_WIN:
        return
    old = Path(os.environ.get("APPDATA", str(Path.home()))) / OLD_APP_NAME / "helper.json"
    try:
        if not old.is_file():
            return
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_bytes(old.read_bytes())
        log(f"adopted settings from the previous {OLD_APP_NAME} folder")
    except OSError as e:
        log(f"could not adopt the previous settings: {e}")


POLICY_KEY = r"SOFTWARE\Policies\SafePII\Helper"
# What an administrator may set, and how it is stored in the registry. Anything
# outside this list is ignored, so a stray value cannot break the helper.
POLICY_TYPES = {"serverUrl": str, "guard": bool, "unmask": bool, "overlay": bool,
                "fileGuard": bool, "blockDrops": bool, "watchDownloads": bool,
                "deleteTokenCopy": bool, "restoreUnreadable": str, "onGuardFailure": str,
                "preamble": bool, "forgetAfterIdleMinutes": int, "filesDir": str,
                "downloadsDir": str, "overlayDebug": bool,
                # Sharing: whether it is allowed at all, and on what terms.
                "sharing": bool, "shareNames": str, "shareAllowCode": bool,
                "sharePort": int, "watchClaudeConfig": bool}
POLICY: dict = {}      # what the administrator has actually set, this run


def read_policy() -> dict:
    """Administrator settings from HKLM, which only an administrator can write.

    This is the difference between a default and a policy. The machine-wide
    JSON file is read before the user's own and can therefore be overridden by
    them; anything here cannot be, which is the control a fleet's security team
    asks for by name.
    """
    if not _IS_WIN:
        return {}
    try:
        import winreg
    except ImportError:
        return {}
    found = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, POLICY_KEY) as key:
            i = 0
            while True:
                try:
                    name, value, _kind = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                want = POLICY_TYPES.get(name)
                if want is None:
                    log(f"policy: ignoring unknown setting {name!r}")
                    continue
                try:
                    found[name] = bool(value) if want is bool else want(value)
                except (TypeError, ValueError):
                    log(f"policy: ignoring {name!r}, not a {want.__name__}")
    except OSError:
        pass                                   # no policy key: the normal case
    return found


def locked(name: str) -> bool:
    return name in POLICY


def version_tuple(text: str) -> tuple:
    """So 0.10.0 sorts above 0.9.0, which a string comparison gets wrong."""
    return tuple(int(part) for part in text.split("."))


def load_config() -> dict:
    adopt_old_settings()
    cfg = dict(DEFAULTS)
    for path in (MACHINE_CONFIG, CONFIG_FILE):   # machine-wide defaults, then the user's own
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError):
            pass
    POLICY.clear()
    POLICY.update(read_policy())                 # last word, and not the user's
    cfg.update(POLICY)
    if POLICY:
        log("policy: administrator sets " + ", ".join(f"{k}={v!r}" for k, v in sorted(POLICY.items())))
    cfg["chats"] = dict(cfg.get("chats") or {})
    cfg["sessions"] = dict(cfg.get("sessions") or {})
    sid = cfg.get("sessionId")
    if sid and sid not in cfg["sessions"]:          # a session from before chats were tracked
        cfg["sessions"][sid] = {"title": None, "chat": None, "last": time.time()}
    return cfg


LOG_FILE = CONFIG_DIR / "helper.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
_log_lock = threading.Lock()
_config_lock = threading.RLock()


def redact(value, keep: int = 0) -> str:
    """What may be written about a piece of user content: its shape, not itself.

    The log is an ordinary file on an endpoint. It is backed up, roamed, and
    collected by whatever agent the customer runs, so a product whose promise is
    that content does not leave the machine unmasked cannot write that content
    into it. `keep` allows a short prefix where one is genuinely diagnostic, such
    as a file extension.
    """
    text = "" if value is None else str(value)
    if not text:
        return "<empty>"
    head = text[:keep].replace("\n", " ") if keep else ""
    return f"<{len(text)} chars{': ' + head + '…' if head else ''}>"


def _rotate_log() -> None:
    """One previous file is kept, so a long session cannot fill the disk and a
    diagnostic report still has the run before it."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            previous = LOG_FILE.with_suffix(".1.log")
            previous.unlink(missing_ok=True)
            LOG_FILE.rename(previous)
    except OSError:
        pass


def log(msg: str) -> None:
    """Diagnostic trail: guard decisions, hook status, counts and outcomes.

    Never user content. Pass anything derived from a document, a message or a
    file name through `redact` first.
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{threading.current_thread().name}] {msg}\n"
    try:
        with _log_lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_log()
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def save_config(cfg: dict) -> None:
    """Written through a temporary file: three threads call this, and a torn
    write silently reverted every setting, including the server URL, to the
    defaults."""
    with _config_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Policy values are not the user's to keep: leaving them in the file
        # would make a setting look chosen after the policy is withdrawn.
        keep = {k: v for k, v in cfg.items() if k not in POLICY}
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(keep, indent=2), "utf-8")
        os.replace(tmp, CONFIG_FILE)


# ----------------------------------------------------------------------------- server
class Server:
    """The same contract extension/background.js uses, over urllib."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _request(self, path: str, method: str, data, headers: dict):
        url = self.cfg["serverUrl"].rstrip("/") + path
        # "maskroom" is the CSRF value the server checks (webui/auth.py CSRF_VALUE),
        # not a product name. It stays put; renaming it would need both sides.
        headers = {"X-Requested-With": CSRF_VALUE, "Accept": "application/json",
                   "User-Agent": USER_AGENT, **headers}
        token = (self.cfg.get("token") or "").strip()
        key = (self.cfg.get("apiKey") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        elif key.startswith("mr_"):
            headers["Authorization"] = "Bearer " + key
        elif key:
            headers["X-API-Key"] = key
        return urllib.request.Request(url, data=data, method=method, headers=headers)

    def raw(self, path: str, body: bytes, content_type: str) -> dict:
        """POST a prepared body (multipart upload) and read a JSON reply."""
        req = self._request(path, "POST", body, {"Content-Type": content_type})
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                return {"ok": True, "status": res.status, "data": _json_or_none(res.read()), "error": None}
        except urllib.error.HTTPError as e:
            payload = _json_or_none(e.read())
            msg = (payload or {}).get("error") if isinstance(payload, dict) else None
            return {"ok": False, "status": e.code, "data": payload, "error": msg or f"{e.code} {e.reason}"}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return {"ok": False, "status": 0, "data": None,
                    "error": f"Cannot reach SafePII at {self.cfg['serverUrl']} ({getattr(e, 'reason', e)})."}

    def binary(self, path: str) -> tuple[bytes | None, str | None]:
        """GET bytes (a masked or restored file). Returns (bytes, error)."""
        req = self._request(path, "GET", None, {})
        try:
            with urllib.request.urlopen(req, timeout=300) as res:
                return res.read(), None
        except urllib.error.HTTPError as e:
            return None, f"{e.code} {e.reason}"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            return None, str(getattr(e, "reason", e))

    def api(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        url = self.cfg["serverUrl"].rstrip("/") + path
        headers = {"X-Requested-With": CSRF_VALUE, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        token = (self.cfg.get("token") or "").strip()
        key = (self.cfg.get("apiKey") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        elif key.startswith("mr_"):
            headers["Authorization"] = "Bearer " + key
        elif key:
            headers["X-API-Key"] = key
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                payload = _json_or_none(res.read())
                return {"ok": True, "status": res.status, "data": payload, "error": None}
        except urllib.error.HTTPError as e:
            payload = _json_or_none(e.read())
            msg = (payload or {}).get("error") if isinstance(payload, dict) else None
            return {"ok": False, "status": e.code, "data": payload, "error": msg or f"{e.code} {e.reason}"}
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            reason = getattr(e, "reason", e)
            return {"ok": False, "status": 0, "data": None,
                    "error": f"Cannot reach SafePII at {self.cfg['serverUrl']} ({reason}). Is the server running?"}


def _multipart(fields: dict, filename: str, blob: bytes) -> tuple[bytes, str]:
    """A minimal multipart/form-data body: the form fields plus one file part."""
    boundary = "----maskroom" + os.urandom(12).hex()
    out = bytearray()
    for k, v in fields.items():
        if v is None:
            continue
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    out += blob + b"\r\n"
    out += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class FileApi:
    """The file half of the server contract the extension uses: /api/process
    to mask, /api/download/<run>/<name> to fetch the result."""

    def __init__(self, server: "Server"):
        self.server = server

    def process(self, path: str, fields: dict) -> dict:
        blob = open(path, "rb").read()
        if len(blob) > MAX_UPLOAD:
            return {"ok": False, "status": 413, "error": "File is larger than 25 MB."}
        body, ctype = _multipart(fields, os.path.basename(path), blob)
        return self.server.raw("/api/process", body, ctype)

    def unmask(self, path: str, fields: dict) -> dict:
        blob = open(path, "rb").read()
        if len(blob) > MAX_UPLOAD:
            return {"ok": False, "status": 413, "error": "File is larger than 25 MB."}
        body, ctype = _multipart(fields, os.path.basename(path), blob)
        return self.server.raw("/api/unmask-file", body, ctype)

    def download(self, run_id: str, name: str) -> tuple[bytes | None, str | None]:
        return self.server.binary(f"/api/download/{run_id}/{urllib.parse.quote(name)}")


# ----------------------------------------------------------------------------- tokens
EXACT_RE = re.compile(r"TOK_[A-Z0-9_]+_[0-9A-F]{8,}")
LOOSE_RE = re.compile(r"\bTOK(?:[\s_\-\\]{1,3}[A-Z]{2,})+?[\s_\-\\]{1,3}([0-9A-F]{6,})", re.IGNORECASE)
MIN_ID = 6


def _key(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


class TokenIndex:
    """Port of extension/tokens.js: exact tokens first, then loose matches (any
    case, spaces/hyphens/escaped underscores, truncated or extended id) through
    a normalized index with unique-prefix matching. Unknown tokens stay as they are."""

    def __init__(self, vault: dict[str, str]):
        self.vault = vault
        self.exact: dict[str, str] = {}
        self.by_entity: dict[str, list[tuple[str, str]]] = {}
        for tok in vault:
            self.exact[_key(tok)] = tok
            i = tok.rfind("_")
            self.by_entity.setdefault(_key(tok[4:i]), []).append((tok[i + 1:], tok))

    def restore(self, text: str) -> tuple[str, int]:
        if not text or not re.search("tok", text, re.IGNORECASE):
            return text, 0
        n = 0

        def exact(m):
            nonlocal n
            tok = m.group(0)
            if tok in self.vault:
                n += 1
                return self.vault[tok]
            return tok

        def loose(m):
            nonlocal n
            k = _key(m.group(0))
            tok = self.exact.get(k)
            if not tok:
                h = m.group(1).upper()
                entity = k[3:len(k) - len(h)]
                if len(h) >= MIN_ID:
                    hits = [t for vh, t in self.by_entity.get(entity, []) if vh.startswith(h) or h.startswith(vh)]
                    if len(hits) == 1:
                        tok = hits[0]
            if not tok:
                return m.group(0)
            n += 1
            return self.vault[tok]

        out = EXACT_RE.sub(exact, text)
        out = LOOSE_RE.sub(loose, out)
        return out, n


# ----------------------------------------------------------------------------- win32 bits
_IS_WIN = sys.platform.startswith("win")
_kernel32 = ctypes.windll.kernel32 if _IS_WIN else None
_user32 = ctypes.windll.user32 if _IS_WIN else None
# shcore is Windows 8.1 and later. Missing it is not fatal: the DPI falls back.
try:
    _shcore = ctypes.windll.shcore if _IS_WIN else None
except Exception:  # noqa: BLE001
    _shcore = None
if _IS_WIN:
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
    _kernel32.GetModuleHandleW.restype = wt.HMODULE
    _kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetParent.restype = wt.HWND
    _user32.GetParent.argtypes = [wt.HWND]
    _user32.GetAsyncKeyState.restype = ctypes.c_short
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    _user32.GetLastInputInfo.argtypes = [ctypes.c_void_p]
    _kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    _user32.OpenInputDesktop.restype = wt.HANDLE
    _user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _user32.CloseDesktop.argtypes = [wt.HANDLE]
    _user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromWindow.restype = wt.HANDLE
    _user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
    _user32.MonitorFromPoint.restype = wt.HANDLE
    _user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
    _user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
    if _shcore is not None:
        _shcore.GetDpiForMonitor.argtypes = [wt.HANDLE, ctypes.c_int,
                                             ctypes.POINTER(wt.UINT), ctypes.POINTER(wt.UINT)]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.GetClipboardSequenceNumber.restype = wt.DWORD
    _user32.GetDC.restype = wt.HDC
    _user32.GetDC.argtypes = [wt.HWND]
    _user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _shell32 = ctypes.windll.shell32
    _shell32.DragQueryFileW.restype = wt.UINT
    _shell32.DragQueryFileW.argtypes = [wt.HANDLE, wt.UINT, wt.LPWSTR, wt.UINT]
    _user32.IsClipboardFormatAvailable.argtypes = [wt.UINT]
    _user32.OpenClipboard.argtypes = [wt.HWND]
    _user32.GetClipboardData.restype = wt.HANDLE
    _user32.GetClipboardData.argtypes = [wt.UINT]
    _user32.SetClipboardData.restype = wt.HANDLE
    _user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    _kernel32.GlobalAlloc.restype = wt.HANDLE
    _kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [wt.HANDLE]
    _kernel32.GlobalUnlock.argtypes = [wt.HANDLE]
    _kernel32.GlobalFree.restype = wt.HANDLE
    _kernel32.GlobalFree.argtypes = [wt.HANDLE]
    _gdi32 = ctypes.windll.gdi32
    _gdi32.GetPixel.restype = wt.COLORREF
    _gdi32.GetPixel.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def screen_pixel(x: int, y: int) -> str | None:
    """'#rrggbb' of the screen pixel, or None."""
    hdc = _user32.GetDC(None)
    try:
        c = _gdi32.GetPixel(hdc, x, y)
    finally:
        _user32.ReleaseDC(None, hdc)
    if c == 0xFFFFFFFF:   # CLR_INVALID
        return None
    return f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"
_exe_cache: dict[int, tuple[str, float]] = {}
EXE_CACHE_TTL = 2.0      # seconds an answer about a process id may be reused
EXE_CACHE_MAX = 512


def process_exe(pid: int, fresh: bool = False) -> str:
    """Lower-cased executable name for a process id ('' when unreadable).

    Cached, because the hooks ask on every wheel notch and every Enter, but only
    briefly. Windows reuses process ids, and this cache used to keep an answer
    for the life of the helper: a browser that inherited an id last seen as
    Claude was then treated as Claude, so the bar appeared over it. In the other
    direction Claude itself stopped being recognised and the guard silently
    never engaged.
    """
    now = time.monotonic()
    hit = _exe_cache.get(pid)
    if not fresh and hit is not None and now - hit[1] < EXE_CACHE_TTL:
        return hit[0]
    name = ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                # ntpath, not os.path: this is always a Windows path, and saying
                # so lets the identity checks be tested off Windows.
                name = ntpath.basename(buf.value).lower()
        finally:
            _kernel32.CloseHandle(h)
    if len(_exe_cache) > EXE_CACHE_MAX:
        _exe_cache.clear()               # a long session must not accumulate ids
    _exe_cache[pid] = (name, now)
    return name


CF_HDROP = 15


def clipboard_files() -> list[str]:
    """Paths on the clipboard (CF_HDROP), or []."""
    if not _IS_WIN or not _user32.IsClipboardFormatAvailable(CF_HDROP):
        return []
    if not _user32.OpenClipboard(None):
        return []
    try:
        h = _user32.GetClipboardData(CF_HDROP)
        if not h:
            return []
        n = _shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(n):
            need = _shell32.DragQueryFileW(h, i, None, 0) + 1
            buf = ctypes.create_unicode_buffer(need)
            if _shell32.DragQueryFileW(h, i, buf, need):
                out.append(buf.value)
        return out
    finally:
        _user32.CloseClipboard()


def set_clipboard_files(paths: list[str]) -> bool:
    """Put paths on the clipboard as CF_HDROP, so a paste attaches them."""
    if not _IS_WIN or not paths:
        return False

    class DROPFILES(ctypes.Structure):
        _fields_ = [("pFiles", wt.DWORD), ("pt", wt.POINT), ("fNC", wt.BOOL), ("fWide", wt.BOOL)]

    names = "".join(p + "\0" for p in paths) + "\0"
    data = bytes(DROPFILES(ctypes.sizeof(DROPFILES), wt.POINT(0, 0), False, True)) + names.encode("utf-16-le")
    h = _kernel32.GlobalAlloc(0x0042, len(data))     # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not h:
        return False
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        return False
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    if not _user32.OpenClipboard(None):
        _kernel32.GlobalFree(h)
        return False
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(CF_HDROP, h):
            _kernel32.GlobalFree(h)
            return False
        return True                                   # the clipboard owns h now
    finally:
        _user32.CloseClipboard()


def window_class(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def foreground_window():
    """(hwnd, class name, exe) of the foreground window."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None, "", ""
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return hwnd, window_class(hwnd), process_exe(pid.value)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def idle_seconds():
    """Seconds since the last keyboard or mouse input, or None if unknown."""
    if not _IS_WIN:
        return None
    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return max(0.0, (_kernel32.GetTickCount64() - info.dwTime) / 1000.0)


def workstation_locked() -> bool:
    """True when the desktop is locked. OpenInputDesktop fails for the caller
    while the secure desktop is up, which is the documented way to tell."""
    if not _IS_WIN:
        return False
    desk = _user32.OpenInputDesktop(0, False, 0x0100)    # DESKTOP_READOBJECTS
    if not desk:
        return True
    _user32.CloseDesktop(desk)
    return False


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def work_area(window):
    """The usable area of the screen *this window is on*, excluding the taskbar.

    tkinter's winfo_screenwidth and winfo_screenheight describe the primary
    monitor and nothing else. Clamping to them dragged the panel back onto the
    primary screen whenever Claude was on a second one, which is exactly what it
    looked like: the bar on one screen, its panel on another.
    """
    if _IS_WIN:
        try:
            window.update_idletasks()
            hwnd = _user32.GetParent(window.winfo_id()) or window.winfo_id()
            monitor = _user32.MonitorFromWindow(hwnd, 2)   # NEAREST
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if monitor and _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                if r.right > r.left and r.bottom > r.top:
                    return r.left, r.top, r.right, r.bottom
        except Exception as e:  # noqa: BLE001
            log(f"could not read the monitor this window is on: {e}")
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def monitor_dpi(rect, fallback: float = 96.0) -> float:
    """The DPI of the monitor a rectangle is on.

    Everything the overlay measures arrives in physical pixels: the helper
    declares itself per-monitor DPI aware at startup so that UI Automation hands
    back real screen coordinates rather than scaled ones. The font size
    Chromium reports does not follow that convention. It is in points off a
    96-DPI page, and knows nothing about the scaling of the monitor it is being
    displayed on, so converting it needs that monitor's DPI and no other.

    One process-wide number cannot serve, which is what a second screen at a
    different scaling exposes: a 150% laptop panel beside a 100% desktop one
    makes every painted value a third too small on one of them. tkinter is no
    help either, because its screen DPI is read once, from the primary monitor,
    the same assumption that put the panel on the wrong screen.
    """
    if _IS_WIN and _shcore is not None and rect:
        try:
            left, top, right, bottom = rect
            point = wt.POINT((left + right) // 2, (top + bottom) // 2)
            monitor = _user32.MonitorFromPoint(point, 2)      # NEAREST
            across, down = wt.UINT(), wt.UINT()
            if monitor and _shcore.GetDpiForMonitor(          # 0 = MDT_EFFECTIVE_DPI
                    monitor, 0, ctypes.byref(across), ctypes.byref(down)) == 0 and across.value:
                return float(across.value)
        except Exception as e:  # noqa: BLE001
            log(f"could not read the dpi of the monitor at {rect}: {e}")
    return fallback


def min_font_px(dpi: float) -> int:
    """The smallest a painted value may be drawn. Nine pixels is legible on a
    96-DPI screen and a smudge on a 192-DPI one, so the floor scales too."""
    return max(9, int(round(9 * dpi / 96.0)))


def patch_font_px(style: dict, height: int, dpi: float) -> int:
    """How tall, in pixels, to draw the value painted over a token.

    Two sources, and they are not equally trustworthy. Chromium reports the
    run's font size in points, which is exact about the text but silent about
    the display, so it is converted with the DPI of the monitor the text is
    actually on rather than with one number for the whole process. The token's
    own bounding rectangle is measured in the pixels we draw in, so it cannot be
    wrong about scale, but it describes a box around the text rather than the
    letters in it, which is what the 0.68 is for.

    The reported size wins when there is one. The rectangle only overrules a
    number that cannot be a font size for a box that shape, because what the box
    means exactly — the line box, or the tight bounds of the glyphs — differs
    between runs, and a narrow band would throw away sizes that are right.
    """
    floor = min_font_px(dpi)
    from_rect = max(floor, int(height * 0.68))
    size = style.get("size")
    if not size:
        return from_rect
    try:
        px = int(round(float(size) * dpi / 72.0))
    except (TypeError, ValueError):
        return from_rect
    if not 0.3 * height <= px <= 1.6 * height:
        return from_rect
    return max(floor, px)


def exclude_from_capture(hwnd, what: str) -> None:
    """Keep a window out of screen shares, recordings and the Snipping Tool.

    The overlay and the tooltip paint *real* values over the tokens, so without
    this they are exactly the thing that leaks into a Teams share or whatever
    screen-recording agent the customer runs."""
    if not _IS_WIN:
        return
    WDA_EXCLUDEFROMCAPTURE = 0x11
    if not _user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        log(f"{what}: could not exclude it from screen capture "
            f"(error {ctypes.get_last_error()}); restored values will appear in shares")


def foreground_exe() -> str:
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wt.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return process_exe(pid.value)


def make_no_activate(tk_toplevel: tk.Toplevel) -> None:
    """Keep the bar from taking keyboard focus away from Claude when clicked."""
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
    tk_toplevel.update_idletasks()
    hwnd = _user32.GetParent(tk_toplevel.winfo_id()) or tk_toplevel.winfo_id()
    style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


class Box:
    """A plain rectangle, so code that reads .left/.top/.right/.bottom works for
    both a UIA rectangle and one we computed."""

    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def __repr__(self):
        return f"Box({self.left},{self.top},{self.right},{self.bottom})"


COMPOSER_BOX_HINT = "rounded-composer"   # the class Claude gives the visible box


def composer_box(ctrl):
    """The composer's *visible* rectangle, not the text element's.

    The editable element sits inside a rounded, padded container, so anchoring
    to it put the bar on top of that container's border. Claude names the
    container in its class list; failing that, the nearest ancestor that is
    meaningfully taller is the box.
    """
    best = ctrl.BoundingRectangle
    node = ctrl
    for _ in range(5):
        try:
            node = node.GetParentControl()
            if node is None:
                break
            r = node.BoundingRectangle
        except Exception:  # noqa: BLE001
            break
        if r.bottom - r.top > (best.bottom - best.top) + 200:
            break                       # too big to be the composer: a page section
        if COMPOSER_BOX_HINT in (node.ClassName or ""):
            return r
        if r.bottom - r.top > best.bottom - best.top:
            best = r
    return best


def find_page_document():
    """(TextPattern, element, window) for Claude's page: the element carrying the
    most text under a Claude window. Called in each thread's own COM apartment,
    because UIA objects must not cross apartments."""
    best, best_n, best_win, best_ctrl = None, -1, None, None
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ClassName != "Chrome_WidgetWin_1" or process_exe(w.ProcessId) != CLAUDE_EXE:
                continue
        except Exception:  # noqa: BLE001
            continue
        stack = [(w, 0)]
        while stack:
            c, depth = stack.pop()
            if depth > 12:
                continue
            try:
                tp = c.GetPattern(auto.PatternId.TextPattern)
            except Exception:  # noqa: BLE001
                tp = None
            if tp is not None:
                try:
                    n = len(tp.DocumentRange.GetText(-1) or "")
                except Exception:  # noqa: BLE001
                    n = -1
                if n > best_n:
                    best, best_n, best_win, best_ctrl = tp, n, w, c
                continue
            try:
                stack.extend((k, depth + 1) for k in c.GetChildren())
            except Exception:  # noqa: BLE001
                pass
    return best, best_ctrl, best_win


# ----------------------------------------------------------------------------- watching Claude's own config
# Claude Desktop reads MCP servers from claude_desktop_config.json, and that file
# is the user's to edit. On a standard (claude.ai sign-in) deployment there is no
# policy that changes this: managedMcpServers is scoped to third-party
# deployments, verified on a real machine. So the helper cannot prevent a user
# adding an MCP server of their own -- one that reads raw files, say -- and the
# honest answer is that it notices and says so, to the person and to the audit
# trail. Prevention is the endpoint team's, through application allowlisting and
# an ACL on this file; see enterprise/WINDOWS-RUNBOOK.md.
CLAUDE_CONFIG_NAME = "claude_desktop_config.json"
# Package family names read from the shipped app, for a Store/MSIX install, whose
# config is nowhere near %APPDATA%.
CLAUDE_PACKAGES = ("Claude_pzs8sxrjxfjjc", "AnthropicPBC.Claude_fnn82j28hfe8t")


def claude_config_paths() -> list[Path]:
    """Every place Claude Desktop is known to keep that file, in the order worth
    looking. There is no single answer: a packaged install puts it under
    Packages\...\LocalCache, which is how a real machine surprised us."""
    appdata = os.environ.get("APPDATA") or ""
    local = os.environ.get("LOCALAPPDATA") or ""
    out = []
    if appdata:
        out.append(Path(appdata) / "Claude" / CLAUDE_CONFIG_NAME)
    if local:
        out.append(Path(local) / "Claude" / CLAUDE_CONFIG_NAME)
        out.append(Path(local) / "Claude-3p" / CLAUDE_CONFIG_NAME)
        for pkg in CLAUDE_PACKAGES:
            out.append(Path(local) / "Packages" / pkg / "LocalCache" / "Roaming"
                       / "Claude" / CLAUDE_CONFIG_NAME)
    return out


def read_claude_mcp_servers(path: Path) -> set[str] | None:
    """The names of the MCP servers configured there, or None if unreadable."""
    try:
        data = json.loads(path.read_text("utf-8") or "{}")
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers")
    return set(servers) if isinstance(servers, dict) else set()


# ----------------------------------------------------------------------------- sharing
class Sharing:
    """One folder at a time, served to Claude through the file broker.

    The user picks the folder from the bar rather than from Claude's own folder
    picker, because with `allowedWorkspaceFolders` set to `[]` there is nothing
    for that picker to offer: the policy removes every raw folder and this is
    what replaces it. Deciding here also means the person is told what will be
    served before it starts being served, which Claude's picker cannot do.

    One at a time, deliberately. Two folders means two vaults to explain and two
    sets of handles that look alike, for a case nobody has asked for yet.
    """

    def __init__(self, cfg: dict, server, on_change=None, on_resolve=None):
        self.cfg = cfg
        self.server = server            # only for its credentials and address
        self.on_change = on_change or (lambda: None)
        self.on_resolve = on_resolve    # token -> value, across every known vault
        self.httpd = None
        self.thread = None
        self.desk = None
        self.root = ""
        self.session = ""
        self.error = ""
        # Set by the broker's threads when a file it served minted new tokens,
        # read and cleared by the worker. A flag rather than a call, because the
        # vault index is the worker's and the overlay reads it.
        self.minted = False
        # One pending "Claude would like a folder" at a time. The broker thread
        # waits on the event; the bar's picker, or a refusal, sets the answer.
        self.want = threading.Event()
        self.answered = ("", False)
        self.declined = False

    @property
    def active(self) -> bool:
        return self.desk is not None and self.desk.workspace is not None

    @property
    def listening(self) -> bool:
        return self.httpd is not None

    @property
    def workspace(self):
        return self.desk.workspace if self.desk else None

    @property
    def name(self) -> str:
        return ntpath.basename(self.root.rstrip("\\/")) or self.root

    def client(self):
        """A broker client with this helper's credentials, so signing in once
        signs in the folder too."""
        return broker_mod.Client(self.cfg["serverUrl"],
                                 token=(self.cfg.get("token") or "").strip(),
                                 api_key=(self.cfg.get("apiKey") or "").strip())

    def listen(self) -> str:
        """Start answering, before anything is shared.

        The server used to come up when a folder was shared and go down when it
        stopped, which made "nothing shared" indistinguishable from "nothing
        running" -- and left Claude unable to ask for a folder, since asking goes
        through this. One server for the helper's lifetime; the folder is swapped
        in and out underneath it, so Claude's connection survives a change.
        """
        if broker_mod is None:
            return "This build has no file broker."
        if self.httpd is not None:
            return ""
        port = int(self.cfg.get("sharePort") or broker_mod.DEFAULT_PORT)
        try:
            self.desk = broker_mod.Desk(ask=self.ask_for_folder)
            self.httpd = broker_mod.make_server(self.desk, port)
        except OSError as e:
            self.desk = None
            log(f"sharing: could not listen on port {port}: {e}")
            return f"SafePII could not open port {port} ({e})."
        self.thread = threading.Thread(target=self.httpd.serve_forever, args=(0.2,),
                                       name="broker", daemon=True)
        self.thread.start()
        log(f"broker listening on http://127.0.0.1:{port}/mcp (nothing shared yet)")
        return ""

    def ask_for_folder(self, reason: str = "") -> tuple[bool, str]:
        """Claude asked for a folder. Runs on a broker thread and blocks there.

        Blocking is right: the model called a tool and is waiting for its result,
        and the result is what the person decided. The wait is bounded, because a
        tool call that never returns is worse than one that says nobody answered.
        """
        if self.declined:
            return False, ("The person has already declined to share a folder. Do not ask "
                           "again; ask them in conversation instead.")
        if self.want.is_set():
            return False, "SafePII is already asking the person about a folder."
        self.answered = ("", False)
        self.want.clear()
        SHARED["want_folder"] = reason or True
        log(f"Claude asked for a folder{f': {reason}' if reason else ''}")
        if not self.want.wait(ASK_FOLDER_S):
            SHARED["want_folder"] = None
            return False, ("The person did not answer in time. Carry on without the files, or "
                           "ask them in conversation.")
        said, ok = self.answered
        return ok, said

    def answer(self, ok: bool, said: str) -> None:
        """What the person decided, from the worker thread."""
        if not SHARED.get("want_folder") and not self.want.is_set():
            return                      # nobody asked; an ordinary share
        SHARED["want_folder"] = None
        self.declined = not ok
        self.answered = (said, ok)
        self.want.set()

    def start(self, root: str, session: str) -> str:
        """Serve `root` through `session`. Returns "" on success, else why not.

        The session is made by the caller, with the same code that makes one for
        a chat, so there is one place that talks to /api/session, one set of
        credentials and one way a 401 is handled.
        """
        if broker_mod is None:
            return "This build has no file broker."
        if not self.cfg.get("sharing", True):
            return ("Your administrator does not allow folders to be shared."
                    if locked("sharing") else "Sharing folders is switched off.")
        if not session:
            return "SafePII has no session for that folder."
        err = self.listen()
        if err:
            return err
        try:
            workspace = broker_mod.Workspace(
                Path(root), self.client(), session=session,
                allow_code=bool(self.cfg.get("shareAllowCode", False)),
                names=str(self.cfg.get("shareNames") or "mask"),
                on_mint=self.note_mint, resolve=self.resolve_tokens)
        except (broker_mod.BrokerError, OSError, ValueError) as e:
            self.error = str(e)
            log(f"sharing: could not serve {root}: {e}")
            return self.error
        # Swapped in under the running server, so a change of folder does not
        # break the connection Claude Desktop already has.
        self.desk.workspace = workspace
        self.root, self.session, self.error = str(workspace.root), session, ""
        self.declined = False
        self.cfg["shareRoot"] = self.root
        save_config(self.cfg)
        log(f"sharing {self.root} as session {session[:8]}")
        # The line an administrator needs to point Claude Desktop at this, in the
        # log rather than in a wiki that will go stale.
        log("managed configuration: " + " ".join(self.policy_snippet().split()))
        self.on_change()
        return ""

    def resolve_tokens(self, text: str) -> str:
        """A token from any session the helper knows, back to its value.

        The folder's tokens are not the chat's: the same name masked in two
        sessions gets two ids. So a search term the model was handed -- the
        person's own words, masked into the chat's vault -- cannot match
        anything in the folder's files until it is turned back. The helper holds
        the union of every vault, which is the only place that can do it.
        """
        try:
            return self.on_resolve(text) if self.on_resolve else text
        except Exception as e:  # noqa: BLE001
            log(f"sharing: could not resolve a token in a search term: {e}")
            return text

    def note_mint(self) -> None:
        """A file Claude read put new values in the vault."""
        self.minted = True

    def take_mint(self) -> bool:
        """True once per batch of new tokens, for the worker to act on."""
        if not self.minted:
            return False
        self.minted = False
        return True

    def stop(self) -> None:
        """Stop serving the folder. The broker keeps listening, so Claude can
        still be told there is nothing shared, and can still ask for one."""
        if self.active:
            log(f"sharing: stopped serving {self.root}")
        if self.desk is not None:
            self.desk.workspace = None
        self.root = self.session = ""
        if self.cfg.get("shareRoot"):
            self.cfg["shareRoot"] = ""
            save_config(self.cfg)
        self.on_change()

    def policy_snippet(self) -> str:
        port = int(self.cfg.get("sharePort") or 47821)
        return broker_mod.policy_snippet(port) if broker_mod else ""


# ----------------------------------------------------------------------------- UIA worker
class Automation(threading.Thread):
    """Owns every UI Automation and server call. Talks to the UI through `events`."""

    def __init__(self, cfg: dict, commands: "queue.Queue[tuple]", events: "queue.Queue[dict]"):
        super().__init__(name="uia", daemon=True)
        self.cfg = cfg
        self.server = Server(cfg)
        self.commands = commands
        self.events = events
        self.composer = None          # last control that looked like the composer
        self.last_seen = 0.0
        self.last_masked = ""         # composer text as it stood after the last check
        self.mask_done_at = 0.0       # when the last check finished (drops stale Enters)
        self.index = TokenIndex({})   # union of every known session's vault, for local restore
        self.vaults: dict[str, dict] = {}
        self.token_owner: dict[str, str] = {}
        self.doc = None               # Claude's page document (TextPattern) for hover lookups
        self.doc_ctrl = None          # the same element, for its Name (title) and Value (url)
        self.doc_checked = 0.0
        self.tip_text = None          # what the tooltip currently shows
        self.cursor = (0, 0)
        self.cursor_since = 0.0
        self.clip_seq = _user32.GetClipboardSequenceNumber() if _IS_WIN else 0
        self.clip_ignore_until = 0.0
        self.claude_win = None        # top-level Claude window (overlay covers it)
        self.files = FileApi(self.server)
        self.sharing = Sharing(cfg, self.server,
                               on_resolve=lambda text: self.index.restore(text)[0])
        self.forgot = False           # values dropped for an unattended desk, to be re-fetched
        self.claude_cfg = None        # where Claude Desktop's own config turned out to be
        self.claude_cfg_at = 0.0      # its mtime when last read
        self.claude_said = None       # the last set of foreign servers reported, to say it once
        self.dialog = None            # the open file dialog, while Claude has one
        self.dialog_edit = None
        self.dialog_confirm = None
        self.dialog_list = None
        self.dialog_seen = 0.0
        self.dialog_said = ""
        self.dialog_dumped = False
        self.seen_classes: set[str] = set()
        self.downloads_at = 0.0
        self.downloads_done: set[str] = set()
        self.downloads_size: dict[str, int] = {}
        self.masked_paths: set[str] = set()   # our own outputs: never re-masked

    # ---- plumbing
    def emit(self, **ev) -> None:
        self.events.put(ev)

    def toast(self, msg: str, error: bool = False, level: str = "") -> None:
        """A message for the user.

        `error` stays for the 42 existing call sites; `level` is the finer
        grade. Anything above "info" is kept until it has been read, because a
        six-second line that fades is the wrong shape for "that file was not
        masked"."""
        self.emit(type="alert", msg=msg, level=level or ("error" if error else "info"))

    def run(self) -> None:
        """Never exits. The hook on another thread keeps swallowing Enter while
        this loop is alive, so a thread that dies takes the user's Enter key
        with it: one unguarded accessibility error used to be enough."""
        with auto.UIAutomationInitializerInThread():
            try:
                err = self.sharing.listen()
                if err:
                    log(f"sharing: {err}")
                self.resume_share()
            except Exception as e:  # noqa: BLE001
                log(f"sharing: could not start the broker: {e}")
            while True:
                try:
                    self.cycle()
                except Exception as e:  # noqa: BLE001 - nothing may end this loop
                    log(f"worker cycle error {type(e).__name__}: {e}")
                    time.sleep(POLL_S)

    def cycle(self) -> None:
        SHARED["worker_beat"] = time.time()
        if SHARED["alarm"] == "worker":
            SHARED["alarm"] = ""
            self.emit(type="alarm_clear")
        try:
            cmd = self.commands.get(timeout=POLL_S)
        except queue.Empty:
            try:
                self.poll_focus()
            except Exception as e:  # noqa: BLE001
                log(f"focus poll error {type(e).__name__}: {e}")
            if self.cfg.get("unmask", True):
                try:
                    self.poll_hover()
                    self.poll_clipboard()
                except Exception as e:  # noqa: BLE001
                    log(f"unmask poll error {type(e).__name__}: {e}")
            if self.cfg.get("fileGuard", True):
                try:
                    self.poll_drag()
                    self.poll_dialog()
                except Exception as e:  # noqa: BLE001
                    log(f"dialog poll error {type(e).__name__}: {e}")
            if self.cfg.get("watchDownloads", True):
                try:
                    self.poll_downloads()
                except Exception as e:  # noqa: BLE001
                    log(f"downloads poll error {type(e).__name__}: {e}")
            self.poll_idle()
            try:
                self.poll_shared_vault()
            except Exception as e:  # noqa: BLE001
                log(f"shared folder vault error {type(e).__name__}: {e}")
            try:
                self.poll_claude_config()
            except Exception as e:  # noqa: BLE001
                log(f"client config watch error {type(e).__name__}: {e}")
            try:
                self.poll_update()
            except Exception as e:  # noqa: BLE001
                log(f"update check error {type(e).__name__}: {e}")
            return
        SHARED["worker_beat"] = time.time()
        SHARED["worker_busy"] = cmd[0]         # working, not wedged
        try:
            getattr(self, "cmd_" + cmd[0])(*cmd[1:])
        except Exception as e:  # noqa: BLE001 - keep the worker alive
            log(f"command {cmd[0]} failed: {type(e).__name__}: {e}")
            self.toast(f"{type(e).__name__}: {e}", error=True)
        finally:
            SHARED["worker_busy"] = ""
            SHARED["worker_beat"] = time.time()

    # ---- focus tracking
    def looks_like_composer(self, ctrl) -> bool:
        try:
            return (COMPOSER_CLASS_HINT in (ctrl.ClassName or "")
                    and process_exe(ctrl.ProcessId) == CLAUDE_EXE)
        except Exception:  # noqa: BLE001
            return False

    def poll_focus(self) -> None:
        try:
            ctrl = auto.GetFocusedControl()
        except Exception:  # noqa: BLE001
            ctrl = None
        if ctrl is not None and self.looks_like_composer(ctrl):
            try:
                r = composer_box(ctrl)
            except Exception:  # noqa: BLE001
                return
            if self.composer is None:
                log(f"composer focused: class={ctrl.ClassName!r} pid={ctrl.ProcessId}")
            self.composer = ctrl
            self.last_seen = time.time()
            SHARED["composer_rect"] = (r.left, r.top, r.right, r.bottom)
            self.emit(type="composer", visible=True, rect=(r.left, r.top, r.right, r.bottom))
        elif time.time() - self.last_seen > HIDE_GRACE_S:
            if self.composer is not None:
                try:
                    where = f"{ctrl.ControlTypeName} class={ctrl.ClassName!r} pid={ctrl.ProcessId}" if ctrl else "none"
                except Exception:  # noqa: BLE001
                    where = "?"
                log(f"composer lost; focus now: {where}")
            self.composer = None
            SHARED["composer_rect"] = None
            self.emit(type="composer", visible=False)

    def current_composer(self):
        try:
            ctrl = auto.GetFocusedControl()
            if ctrl is not None and self.looks_like_composer(ctrl):
                return ctrl
        except Exception:  # noqa: BLE001
            pass
        if self.composer is not None and self.looks_like_composer(self.composer):
            return self.composer
        return None

    # ---- composer read / write
    @staticmethod
    def read_text(ctrl) -> str:
        for getter in (lambda: ctrl.GetPattern(auto.PatternId.ValuePattern).Value,
                       lambda: ctrl.GetPattern(auto.PatternId.TextPattern).DocumentRange.GetText(-1)):
            try:
                text = getter()
            except Exception:  # noqa: BLE001
                continue
            if text is not None:
                return text
        return ""

    def write_text(self, ctrl, text: str) -> str:
        """Returns which path worked: 'value' or 'paste'."""
        try:
            vp = ctrl.GetPattern(auto.PatternId.ValuePattern)
            if vp is not None and not vp.IsReadOnly:
                vp.SetValue(text)
                time.sleep(0.15)
                if self.read_text(ctrl).strip() == text.strip():
                    return "value"
        except Exception:  # noqa: BLE001
            pass
        # Fallback proven by the probe: the same keystrokes a person would use.
        ctrl.SetFocus()
        self.clip_ignore_until = time.time() + 2.0   # do not "restore" our own masked paste
        auto.SetClipboardText(text)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Ctrl}v", waitTime=0.3)
        return "paste"

    # ---- vaults (local restore across every known session)
    def known_sessions(self) -> list[str]:
        meta = self.cfg.get("sessions") or {}
        return sorted(meta, key=lambda s: meta[s].get("last", 0), reverse=True)[:MAX_KNOWN_SESSIONS]

    def forget_session(self, sid: str) -> None:
        self.cfg["sessions"].pop(sid, None)
        self.cfg["chats"] = {k: v for k, v in self.cfg["chats"].items() if v != sid}
        self.vaults.pop(sid, None)
        if self.cfg.get("sessionId") == sid:
            self.cfg["sessionId"] = None
        save_config(self.cfg)

    def load_vault(self, sid: str | None = None) -> None:
        """Fetch one session's vault (or every known one) and rebuild the index."""
        for s in ([sid] if sid else self.known_sessions()):
            r = self.server.api(f"/api/session/{s}/vault")
            if r["ok"] and isinstance(r["data"], dict):
                self.vaults[s] = r["data"].get("mappings") or {}
            elif r["status"] in (403, 404):
                self.forget_session(s)
        self.rebuild_index()

    def rebuild_index(self) -> None:
        union: dict[str, str] = {}
        owner: dict[str, str] = {}
        for s in self.known_sessions():
            for tok, val in (self.vaults.get(s) or {}).items():
                union.setdefault(tok, val)
                owner.setdefault(tok, s)
        self.index = TokenIndex(union)
        SHARED["index"] = self.index          # the overlay thread reads this
        self.token_owner = owner
        log(f"vault index: {len(union)} tokens across {len(self.vaults)} sessions")

    def poll_claude_config(self) -> None:
        """Notice an MCP server in Claude Desktop that is not ours.

        It cannot be prevented on a standard deployment -- the policy that would,
        managedMcpServers, is scoped to third-party ones -- so it is reported
        instead: to the person on the bar, and to the audit trail, where an
        administrator can see which machines have one. A filesystem server added
        here would read raw files and SafePII would never see them, which is the
        one hole left in the fleet story and deserves to be visible rather than
        argued about.

        Cheap: a stat, and a parse only when the file has changed.
        """
        if not self.cfg.get("watchClaudeConfig", True):
            return
        if self.claude_cfg is None or not self.claude_cfg.is_file():
            self.claude_cfg = next((p for p in claude_config_paths() if p.is_file()), None)
            if self.claude_cfg is None:
                return
            log(f"watching Claude Desktop's config at {self.claude_cfg}")
            self.claude_cfg_at = 0.0
        try:
            at = self.claude_cfg.stat().st_mtime
        except OSError:
            return
        if at == self.claude_cfg_at:
            return
        self.claude_cfg_at = at
        names = read_claude_mcp_servers(self.claude_cfg)
        if names is None:
            log(f"Claude Desktop's config at {self.claude_cfg} could not be read")
            return
        ours = broker_mod.NAME if broker_mod else "safepii-files"
        foreign = sorted(n for n in names if n != ours)
        state = (tuple(foreign), ours in names)
        if state == self.claude_said:
            return
        self.claude_said = state
        if foreign:
            said = ("Claude Desktop is configured with "
                    + ("an MCP server SafePII does not manage: " if len(foreign) == 1
                       else f"{len(foreign)} MCP servers SafePII does not manage: ")
                    + ", ".join(foreign[:5])
                    + ". Files it reads do not pass through SafePII.")
            log("posture: " + said)
            self.emit(type="alert", msg=said, level="warn")
            self.report_event("unmanaged-mcp-server",
                              f"{self.claude_cfg}: {', '.join(foreign)}")
        if self.sharing.listening and ours not in names:
            said = ("SafePII is not registered with Claude Desktop, so Claude cannot read "
                    "your files through it. Your administrator sets this up.")
            log("posture: " + said)
            self.emit(type="alert", msg=said, level="warn")
            self.report_event("safepii-not-registered", str(self.claude_cfg))

    def report_event(self, kind: str, detail: str) -> None:
        """Tell the server something an administrator should see. Best-effort:
        a machine that cannot reach the server has a bigger problem, and failing
        to report must not stop the helper doing its job."""
        r = self.server.api("/api/event", "POST", {"kind": kind, "detail": detail})
        if not r["ok"]:
            log(f"could not report {kind}: {r['error']}")

    def poll_shared_vault(self) -> None:
        """Re-read the shared folder's vault after Claude has read a file.

        The folder's vault is fetched once when the folder is shared, and at that
        moment it is empty: every token in it is minted later, as Claude reads
        files, by the broker calling the server directly. Nothing asked the
        helper, so its copy stayed empty and the overlay had nothing to restore a
        file's values with -- which is exactly how it looked, tokens on screen
        that hover and the overlay ignored.

        The broker is in this process, so it raises a flag and this reads it.
        Once per batch rather than once per file: Claude reads a folder in
        handfuls, and one fetch covers all of them.
        """
        if not self.sharing.active or not self.sharing.take_mint():
            return
        before = len(self.index.vault)
        self.load_vault(self.sharing.session)
        log(f"shared folder vault: {len(self.index.vault) - before} new token(s) "
            f"from {self.sharing.name}")

    def poll_update(self) -> None:
        """Ask the server what the current helper is.

        It reports; it does not install. On a managed fleet updating is the
        endpoint team's job through the MSI, and a program that can replace its
        own binary is a program worth attacking."""
        now = time.time()
        if now - self.update_checked < UPDATE_EVERY_S:
            return
        self.update_checked = now
        r = self.server.api("/desktop/latest.json")
        if not r["ok"] or not isinstance(r["data"], dict):
            return
        latest = (r["data"].get("version") or "").strip()
        if not latest or not re.fullmatch(r"\d+(\.\d+)*", latest):
            return
        if version_tuple(latest) <= version_tuple(__version__):
            return
        log(f"update: the server has {latest}, this is {__version__}")
        self.emit(type="update", version=latest, url=r["data"].get("url") or "")

    def poll_idle(self) -> None:
        """Forget the real values after a spell of no input, and at once when
        the workstation is locked. Held indefinitely, they are a standing
        disclosure on an unattended desk."""
        minutes = float(self.cfg.get("forgetAfterIdleMinutes") or 0)
        if not self.index.vault:
            # The values were dropped while nobody was here. Serving never
            # stopped -- the folder is masked by the server, not by anything
            # held in this process -- so when the person comes back, fetch them
            # again and the tokens on screen can be read once more.
            if self.forgot and not workstation_locked() and (idle_seconds() or 0) < 60:
                self.forgot = False
                if self.sharing.active or self.cfg.get("sessions"):
                    log("back at the desk: fetching the vaults again")
                    self.load_vault()
            return
        if workstation_locked():
            self.forget_vaults("the workstation was locked")
            self.forgot = True
            return
        if minutes <= 0:
            return
        idle = idle_seconds()
        if idle is not None and idle > minutes * 60:
            self.forget_vaults(f"no input for {minutes:g} minutes")
            self.forgot = True

    # ---- which chat is on screen
    def chat_identity(self) -> tuple[str | None, str | None]:
        """(url, title) of the chat on screen, from the page document."""
        if self.page_document() is None or self.doc_ctrl is None:
            return None, None
        url = title = None
        try:
            title = (self.doc_ctrl.Name or "").strip() or None
            if title and title.lower().endswith("- claude"):
                title = title[:-8].strip() or None
        except Exception:  # noqa: BLE001
            pass
        try:
            vp = self.doc_ctrl.GetPattern(auto.PatternId.ValuePattern)
            v = (vp.Value or "").strip() if vp is not None else ""
            if v.startswith("http"):
                url = v.split("#", 1)[0].split("?", 1)[0]
        except Exception:  # noqa: BLE001
            pass
        return url, title

    @staticmethod
    def chat_key(url: str | None) -> str | None:
        """A stable key for an existing chat; None for /new and unknown pages."""
        return url if url and "/chat/" in url else None

    def page_text(self) -> str:
        """The page's text, for recognising which chat is on screen. Called once
        per mask, not in any loop."""
        doc = self.page_document()
        try:
            return doc.DocumentRange.GetText(-1) or "" if doc else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- hover tooltip
    def page_document(self):
        """Claude's page document element, cached (see find_page_document)."""
        now = time.time()
        if self.doc is not None or now - self.doc_checked < 2.0:
            return self.doc
        self.doc_checked = now
        self.doc, self.doc_ctrl, self.claude_win = find_page_document()
        if self.doc is not None:
            url, title = self.chat_identity()
            log(f"page document found, chat {'identified by url' if url else 'url unknown'}"
                f", title {redact(title)}")
        else:
            log("page document not found")
        return self.doc

    def set_tip(self, text, x=0, y=0) -> None:
        if text != self.tip_text:
            self.tip_text = text
            self.emit(type="tip", text=text, x=x, y=y)

    def poll_hover(self) -> None:
        pos = auto.GetCursorPos()
        now = time.time()
        if pos != self.cursor:
            self.cursor, self.cursor_since = pos, now
            self.set_tip(None)
            return
        if now - SHARED["scroll_at"] < 0.5:
            self.set_tip(None)                      # the text is moving under the pointer
            return
        if self.tip_text is not None or now - self.cursor_since < 0.35:
            return                                  # already shown, or still moving
        if foreground_exe() != CLAUDE_EXE or not self.index.vault:
            return
        doc = self.page_document()
        if doc is None:
            return
        try:
            rng = doc.RangeFromPoint(*pos)
        except Exception:  # noqa: BLE001
            self.doc = None                         # stale: re-find next time
            return
        if rng is None:
            return
        line = rng.Clone()
        line.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
        text = (line.GetText(-1) or "").strip()
        restored, n = self.index.restore(text)
        if not n:
            self.cursor_since = now + 3600          # nothing here; do not retry until the mouse moves
            return
        if len(restored) > TIP_MAX_CHARS:
            restored = restored[:TIP_MAX_CHARS] + "…"
        rects = line.GetBoundingRectangles()
        r = rects[0] if rects else None
        self.set_tip(restored, r.left if r else pos[0], (r.bottom + 4) if r else pos[1] + 18)

    # ---- clipboard restore
    def poll_clipboard(self) -> None:
        seq = _user32.GetClipboardSequenceNumber()
        if seq == self.clip_seq:
            return
        self.clip_seq = seq
        if time.time() < self.clip_ignore_until or foreground_exe() != CLAUDE_EXE:
            return                                  # our own paste, or a copy from another app
        try:
            text = auto.GetClipboardText()
        except Exception:  # noqa: BLE001
            return
        restored, n = self.index.restore(text or "")
        if n:
            self.clip_ignore_until = time.time() + 1.0
            auto.SetClipboardText(restored)
            self.clip_seq = _user32.GetClipboardSequenceNumber()
            self.toast(f"Clipboard restored: {n} value{'' if n == 1 else 's'}.")

    def refuse_if_locked(self, key: str, label: str) -> bool:
        """A setting an administrator has fixed cannot be changed here, and the
        user is told so rather than left wondering why the switch sprang back."""
        if locked(key):
            self.toast(f"{label} is set by your administrator and cannot be changed here.",
                       level="warn")
            return True
        return False

    def cmd_toggle_overlay(self) -> None:
        if self.refuse_if_locked("overlay", "The overlay"):
            return
        self.cfg["overlay"] = not self.cfg.get("overlay", True)
        save_config(self.cfg)
        self.emit(type="overlay", items=None)
        self.emit(type="overlay_state", on=self.cfg["overlay"])
        self.toast("Overlay on: real values are painted over tokens." if self.cfg["overlay"]
                   else "Overlay off.")

    def cmd_toggle_unmask(self) -> None:
        if self.refuse_if_locked("unmask", "Unmasking replies"):
            return
        self.cfg["unmask"] = not self.cfg.get("unmask", True)
        save_config(self.cfg)
        self.set_tip(None)
        self.emit(type="unmask", on=self.cfg["unmask"])
        self.toast("Unmask on: hover a token, or copy from Claude." if self.cfg["unmask"]
                   else "Unmask off.")

    # ---- sessions follow the chat
    def resolve_session(self, url, title, text) -> tuple[str | None, str]:
        """(session id, how) for the chat on screen, or (None, reason)."""
        key = self.chat_key(url)
        sid = self.cfg["chats"].get(key) if key else None
        if sid:
            return sid, "chat url"
        hits: dict[str, int] = {}
        for tok in set(EXACT_RE.findall(text or "")):
            owner = self.token_owner.get(tok)
            if owner:
                hits[owner] = hits.get(owner, 0) + 1
        if hits:
            return max(hits, key=hits.get), "tokens on the page"
        if title:
            for s in self.known_sessions():
                if (self.cfg["sessions"][s].get("title") or "") == title:
                    return s, "chat title"
        return None, "new chat"

    def bind_session(self, sid: str, url, title) -> None:
        key = self.chat_key(url)
        if key:
            self.cfg["chats"][key] = sid
        meta = self.cfg["sessions"].setdefault(sid, {})
        if title:
            meta["title"] = title
        if key:
            meta["chat"] = key
        meta["last"] = time.time()
        self.cfg["sessionId"] = sid
        # keep the maps bounded
        keep = set(self.known_sessions())
        self.cfg["sessions"] = {s: m for s, m in self.cfg["sessions"].items() if s in keep}
        self.cfg["chats"] = {k: v for k, v in self.cfg["chats"].items() if v in keep}
        save_config(self.cfg)

    def create_session(self) -> str:
        r = self.server.api("/api/session", "POST", {})
        if not r["ok"]:
            raise RuntimeError(r["error"])
        return r["data"]["session_id"]

    def ensure_session(self) -> str:
        """The session for the chat on screen, created if the chat has none."""
        url, title = self.chat_identity()
        sid, how = self.resolve_session(url, title, self.page_text())
        if sid:
            r = self.server.api(f"/api/session/{sid}")
            if not r["ok"]:
                if r["status"] not in (403, 404):
                    raise RuntimeError(r["error"])
                self.forget_session(sid)
                sid = None
        if not sid:
            sid = self.create_session()
            how = "new session"
            self.vaults[sid] = {}
        if self.cfg.get("sessionId") != sid:
            log(f"session {sid[:8]} for chat ({how}; "
                f"url {'known' if url else 'unknown'}, title {redact(title)})")
            self.emit(type="session", id=sid, title=title)
        self.bind_session(sid, url, title)
        return sid

    # ---- sharing a folder (ADR 0009)
    def remember_share_session(self, sid: str, name: str) -> None:
        """Put the folder's vault among the ones restore searches.

        Deliberately not bind_session: that would also make this the session the
        *chat* masks into, and a folder's vault is not a conversation's.
        """
        meta = self.cfg["sessions"].setdefault(sid, {})
        meta["title"] = f"Folder: {name}"
        meta["folder"] = True
        meta["last"] = time.time()
        keep = set(self.known_sessions())
        self.cfg["sessions"] = {s: m for s, m in self.cfg["sessions"].items() if s in keep}
        save_config(self.cfg)

    def cmd_share_folder(self, path: str) -> None:
        # Checked before a session is made, so a folder that cannot be served
        # does not leave one behind on the server.
        if broker_mod is None or not self.cfg.get("sharing", True):
            self.toast(self.sharing.start(path, "x") or "Sharing is unavailable.", error=True)
            return
        if not Path(path).is_dir():
            self.toast(f"{path} is not a folder.", error=True)
            return
        err = self.sharing.start(path, self.create_session())
        if err:
            self.toast(err, error=True)
            self.emit(type="sharing", root="", name="", error=err)
            self.sharing.answer(False, f"SafePII could not share that folder: {err}")
            return
        name = self.sharing.name
        self.remember_share_session(self.sharing.session, name)
        # Fetch it now so a value read out of a file restores on screen from the
        # first reply, rather than after whatever happens to refresh next.
        self.load_vault(self.sharing.session)
        counts = broker_mod.preview(self.sharing.root,
                                    bool(self.cfg.get("shareAllowCode", False)))
        self.emit(type="sharing", root=self.sharing.root, name=name,
                  session=self.sharing.session, served=counts.get("served", 0))
        self.toast(f"Sharing {name} with Claude — {counts.get('served', 0)} file(s), masked")
        # If Claude was the one who asked, its tool call is still waiting.
        self.sharing.answer(True, f"The person shared a folder. It has {counts.get('served', 0)} "
                                  f"file(s) SafePII can serve. Use list_files to see them; the "
                                  f"names are handles, not the real ones.")

    def cmd_folder_declined(self, said: str = "") -> None:
        """The person closed the picker, or there was nothing in the folder to
        serve, while Claude was waiting."""
        self.sharing.answer(False, said or "The person did not share a folder.")

    def cmd_stop_sharing(self) -> None:
        was = self.sharing.name
        self.sharing.stop()
        self.emit(type="sharing", root="", name="")
        if was:
            self.toast(f"Stopped sharing {was}")

    def resume_share(self) -> None:
        """A folder that was being served when the helper last ran.

        Resumed rather than dropped, because the helper restarts on every update
        and a folder quietly disappearing mid-task is worse than one that comes
        back. Never silent: it says so on the bar.
        """
        root = (self.cfg.get("shareRoot") or "").strip()
        if not root or not self.cfg.get("sharing", True) or broker_mod is None:
            return
        if not Path(root).is_dir():
            log(f"sharing: {root} is gone; not resuming")
            self.cfg["shareRoot"] = ""
            save_config(self.cfg)
            return
        self.commands.put(("share_folder", root))

    def cmd_new_session(self) -> None:
        """A fresh vault for the chat on screen, replacing whatever it was bound to."""
        url, title = self.chat_identity()
        sid = self.create_session()
        self.vaults[sid] = {}
        self.bind_session(sid, url, title)
        self.rebuild_index()
        self.toast(f"New session {sid[:8]}… for {title or 'this chat'}")

    def forget_vaults(self, why: str) -> None:
        """Drop every real value held in this process.

        The vaults are the re-identification key for up to 25 sessions. They
        used to outlive sign-out, so hover, clipboard restore and the overlay
        kept revealing real values until the process was killed."""
        if not self.vaults and not self.index.vault:
            return
        n = len(self.index.vault)
        self.vaults.clear()
        self.token_owner.clear()
        self.index = TokenIndex({})
        SHARED["index"] = self.index
        self.set_tip(None)
        self.emit(type="overlay", items=None)
        log(f"vaults cleared ({n} tokens): {why}")

    def cmd_sign_out(self) -> None:
        r = self.server.api("/auth/logout", "POST", {})
        self.cfg["token"] = ""
        save_config(self.cfg)
        # The broker serves with this sign-in. Once it is gone there is nothing
        # left to mask with, so the folder stops with it.
        if self.sharing.active:
            self.cmd_stop_sharing()
        self.forget_vaults("signed out")
        redirect = (r["data"] or {}).get("redirect") if r["ok"] else None
        if redirect and redirect.startswith("http"):
            webbrowser.open(redirect)    # let the identity provider end its session too
        self.emit(type="auth", ok=True, signed_in=False, msg="Signed out.")

    def cmd_load_vault(self) -> None:
        self.load_vault()

    def cmd_check(self) -> None:
        r = self.server.api("/api/me")
        if r["ok"]:
            d = r["data"] or {}
            p = d.get("principal") or {}
            who = p.get("email") or p.get("name") or ("anonymous, auth off" if d.get("auth_mode") == "off" else "ok")
            self.emit(type="check", ok=True, msg=f"Connected as {who}", auth_mode=d.get("auth_mode"))
        else:
            if r["status"] == 401:
                self.cfg["token"] = ""
                save_config(self.cfg)
            self.emit(type="check", ok=False, msg=(r["error"] or "not signed in") +
                      (" — use Sign in" if r["status"] == 401 else ""))

    # ---- the two actions
    def cmd_mask(self) -> None:
        ctrl = self.current_composer()
        if ctrl is None:
            self.toast("Click into the Claude composer first.", error=True)
            return
        self.mask_composer(ctrl)

    def mask_composer(self, ctrl) -> dict:
        """Check the composer's text with the server and rewrite it if needed.
        Returns {"done": bool, "changed": bool}; done is False when the server
        could not be reached or refused (the guard then holds the send)."""
        text = self.read_text(ctrl)
        if not text.strip():
            self.toast("Nothing to mask.")
            return {"done": False, "changed": False}
        self.emit(type="busy", busy=True)
        result = {"done": False, "changed": False}
        try:
            sid = self.ensure_session()
            r = self.server.api("/api/mask", "POST", {"text": text, "session_id": sid})
            if not r["ok"] and r["status"] == 404:
                self.forget_session(sid)
                sid = self.ensure_session()
                r = self.server.api("/api/mask", "POST", {"text": text, "session_id": sid})
            if not r["ok"]:
                if r["status"] == 401:
                    self.cfg["token"] = ""
                    save_config(self.cfg)
                    self.toast("Sign-in required — open the gear button and sign in.", error=True)
                else:
                    self.toast(r["error"], error=True)
                return result
            d = r["data"]
            out = d["masked"]
            if d["changed"] and self.cfg.get("preamble", True) and sid not in self.cfg["preambleSent"]:
                out = d["preamble"] + "\n\n" + out
                self.cfg["preambleSent"] = (self.cfg["preambleSent"] + [sid])[-50:]
                save_config(self.cfg)
            how = ""
            if d["changed"]:
                how = self.write_text(ctrl, out)
                self.last_masked = self.read_text(ctrl)
            else:
                self.last_masked = text
            n = len(d["findings"])
            if n:
                self.toast(f"{n} value{'' if n == 1 else 's'} masked ({how}). Review, then press send.",
                           level="success")
            else:
                self.toast("No PII detected — safe to send.", level="success")
            self.emit(type="vault", entries=d.get("vault_entries", 0))
            if d["changed"]:
                self.load_vault(sid)
            result = {"done": True, "changed": bool(d["changed"])}
            return result
        finally:
            self.mask_done_at = time.time()
            self.emit(type="busy", busy=False)

    # ---- files
    def files_dir(self) -> Path:
        d = Path(self.cfg.get("filesDir") or (CONFIG_DIR / "files"))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def mask_file(self, path: str) -> tuple[str, str | None, str]:
        """Mask one file through the server. Returns (status, path to attach, message):

          "masked"    the masked copy; the original never leaves the machine
          "unmasked"  SafePII cannot mask this type (an image, an archive…), so
                      the original is attached and the user is warned - the same
                      choice the extension makes
          "skip"      nothing to do (not a file); left to the dialog
          "failed"    a type we should have masked but could not; the send is
                      held, because attaching it would leak what we exist to hide
        """
        src = Path(path)
        if not src.is_file():
            return "skip", str(src), f"{src.name}: not a file; left to the dialog"
        ext = src.suffix.lower()
        if ext not in MASK_EXTS:
            return "unmasked", str(src), f"{src.name}: {ext or 'no extension'} cannot be masked, attached as it is"
        sid = self.ensure_session()
        fields = {"session_id": sid, "pdf_mode": "text", "preview": "false"}
        r = self.files.process(str(src), fields)
        if not r["ok"] and r["status"] == 404:
            self.forget_session(sid)
            fields["session_id"] = self.ensure_session()
            r = self.files.process(str(src), fields)
        if not r["ok"]:
            return "failed", None, f"{src.name}: {r['error']}"
        d = r["data"] or {}
        out_name = ((d.get("downloads") or {}).get("output")) or src.name
        blob, err = self.files.download(d["run_id"], out_name)
        if blob is None:
            return "failed", None, f"{src.name}: could not fetch the masked copy ({err})"
        stem = src.stem
        out = self.files_dir() / f"{stem}_masked{Path(out_name).suffix or ext}"
        n = 1
        while out.exists():
            out = self.files_dir() / f"{stem}_masked_{n}{Path(out_name).suffix or ext}"
            n += 1
        out.write_bytes(blob)
        self.masked_paths.add(str(out).lower())
        self.load_vault(fields["session_id"])
        found = len(d.get("findings") or [])
        return "masked", str(out), f"{src.name}: {found} value{'' if found == 1 else 's'} masked"

    def mask_many(self, paths: list[str]) -> tuple[list[str], list[str], list[str]]:
        """(paths to attach, names going out unmasked, failures that must hold)."""
        attach, unmasked, failed = [], [], []
        for p in paths:
            if str(p).lower() in self.masked_paths:
                attach.append(p)                   # already one of ours
                continue
            status, out, msg = self.mask_file(p)
            # The message names the file, because the user needs to know which
            # one. The log gets the shape of it only.
            log(f"file guard: {Path(p).suffix.lower() or 'no extension'} -> {status}")
            if status == "failed":
                failed.append(msg)
                continue
            attach.append(out)
            if status == "unmasked":
                unmasked.append(Path(p).name)
        return attach, unmasked, failed

    @staticmethod
    def attach_summary(attach: list[str], unmasked: list[str]) -> tuple[str, bool]:
        """(message, warn) for a set of files about to be attached."""
        n = len(attach) - len(unmasked)
        bits = []
        if n:
            bits.append(f"masked {n} file{'' if n == 1 else 's'}")
        if unmasked:
            bits.append(f"attached unmasked: {', '.join(unmasked[:3])}"
                        + (f" and {len(unmasked) - 3} more" if len(unmasked) > 3 else ""))
        return ("; ".join(bits) or "nothing to mask"), bool(unmasked)

    # ---- files Claude produces, coming back down
    def downloads_dir(self) -> Path:
        d = self.cfg.get("downloadsDir")
        return Path(d) if d else Path.home() / "Downloads"

    def poll_downloads(self) -> None:
        """Restore tokens in files Claude produces, as the extension's download
        intercept does. A desktop download cannot be caught mid-flight, so it is
        picked up once it has landed and stopped growing."""
        now = time.time()
        if now - self.downloads_at < DOWNLOAD_POLL_S:
            return
        self.downloads_at = now
        folder = self.downloads_dir()
        try:
            entries = [e for e in folder.iterdir() if e.is_file()]
        except OSError:
            return
        for entry in entries:
            key = str(entry).lower()
            if key in self.downloads_done:
                continue
            if entry.suffix.lower() not in RESTORE_EXTS or "_restored" in entry.stem:
                self.downloads_done.add(key)
                continue
            try:
                size, mtime = entry.stat().st_size, entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime > 120:
                self.downloads_done.add(key)      # already there before we started looking
                continue
            if self.downloads_size.get(key) != size or size == 0:
                self.downloads_size[key] = size   # still being written; look again next tick
                continue
            self.downloads_done.add(key)
            self.downloads_size.pop(key, None)
            seen = self.holds_tokens(entry)
            if seen is True:
                self.restore_download(entry)
            elif seen is None:
                self.offer_restore(entry)

    def cmd_restore_answer(self, path: str, answer: str) -> None:
        """The user's reply to offer_restore: yes, no, always or never."""
        if answer in ("always", "never"):
            self.cfg["restoreUnreadable"] = answer
            save_config(self.cfg)
        if answer in ("yes", "always"):
            self.restore_download(Path(path))

    def restore_download(self, path: Path) -> None:
        if self.holds_tokens(path) is False:
            return
        sid = self.cfg.get("sessionId")
        if not sid:
            self.toast(f"{path.name} holds tokens but there is no session to restore it with.",
                       error=True)
            return
        self.emit(type="busy", busy=True)
        try:
            r = self.files.unmask(str(path), {"session_id": sid})
        finally:
            self.emit(type="busy", busy=False)
        if not r["ok"]:
            log(f"downloads: {redact(path.name, keep=0)}{path.suffix}: {r['error']}")
            self.toast(f"{path.name}: could not restore ({r['error']})", error=True)
            return
        d = r["data"] or {}
        out_name = ((d.get("downloads") or {}).get("output")) or ("restored" + path.suffix)
        blob, err = self.files.download(d["run_id"], out_name)
        if blob is None:
            self.toast(f"{path.name}: could not fetch the restored copy ({err})", error=True)
            return
        out = path.with_name(f"{path.stem}_restored{path.suffix}")
        n = 1
        while out.exists():
            out = path.with_name(f"{path.stem}_restored_{n}{path.suffix}")
            n += 1
        out.write_bytes(blob)
        self.downloads_done.add(str(out).lower())
        restored, unresolved = d.get("restored", 0), len(d.get("unresolved") or [])
        log(f"downloads: {redact(path.name, keep=0)}{path.suffix} restored: "
            f"{restored} value(s), {unresolved} unresolved")
        if self.cfg.get("deleteTokenCopy"):
            try:
                path.unlink()
            except OSError as e:
                log(f"downloads: could not remove the token copy: {e}")
        self.toast(f"{out.name}: {restored} value{'' if restored == 1 else 's'} restored"
                   + (f", {unresolved} token{'' if unresolved == 1 else 's'} unresolved "
                      f"(masked in another chat?)" if unresolved else "") + ".",
                   level="warn" if unresolved else "success")

    @staticmethod
    def holds_tokens(path: Path) -> bool | None:
        """True when the file certainly holds tokens, False when it certainly
        does not, None when it cannot be told without opening it on the server.

        A zipped Office file deflates its text out of sight, so its tokens are
        not visible from here. Uploading every such file that lands in Downloads
        was the wrong answer: a bank statement or a customer list saved from
        email would have gone to the server too. Those come back as None and are
        the user's decision."""
        try:
            with open(path, "rb") as fh:
                head = fh.read(4 * 1024 * 1024)
        except OSError:
            return False
        if b"TOK_" in head.upper():
            return True                      # true even inside a stored zip entry
        return None if path.suffix.lower() in ZIP_EXTS else False

    def offer_restore(self, path: Path) -> None:
        """Ask before sending a file whose tokens cannot be seen from here.

        Silently uploading anything that lands in Downloads is an unannounced
        egress channel from the endpoint, which is what a third-party risk
        review stops a rollout over."""
        remembered = self.cfg.get("restoreUnreadable")
        if remembered == "never":
            return
        if remembered == "always":
            self.restore_download(path)
            return
        self.emit(type="ask_restore", name=path.name, path=str(path))

    # ---- the Windows file dialog Claude opens for its paperclip
    def find_dialog(self):
        """Claude's open-file dialog, or None.

        From the *foreground window*, not by scanning the accessibility tree for
        a class name: the scan never found the dialog on a real machine and
        sometimes threw. A file dialog is always the foreground window while it
        is up, and its window class is read straight from Win32."""
        hwnd, cls, exe = foreground_window()
        if not hwnd or exe != CLAUDE_EXE:
            return None
        if cls not in DIALOG_CLASSES:
            if cls and cls not in self.seen_classes:
                self.seen_classes.add(cls)
                log(f"file guard: Claude foreground window class {cls!r} (not a file dialog)")
            return None
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception as e:  # noqa: BLE001
            log(f"file guard: cannot read the dialog ({type(e).__name__}: {e})")
            return None

    @staticmethod
    def dialog_parts(dlg):
        """(file-name edit, confirm button) of a Windows file dialog.

        Searched breadth-first through the whole dialog, not among its direct
        children: in the modern dialog the File name box sits several levels
        down, which is why an earlier version reported it missing on every
        opening and let every pick through unmasked.
        """
        edit, confirm, listing, edit_score = None, None, None, -1
        queue_, seen = collections.deque([(dlg, 0)]), 0
        while queue_ and seen < 900:
            c, depth = queue_.popleft()
            seen += 1
            try:
                kind, name, aid = c.ControlTypeName, (c.Name or ""), (c.AutomationId or "")
            except Exception:  # noqa: BLE001
                continue
            if kind == "EditControl":
                low = name.lower()
                if aid == "1148":
                    score = 3                     # the classic File name control id
                elif "file name" in low or "filename" in low:
                    score = 2
                else:
                    score = 1 if depth > 0 else 0
                if score > edit_score:
                    edit, edit_score = c, score
            elif kind == "ButtonControl" and confirm is None:
                if name.strip().strip("&").lower() in OPEN_BUTTONS:
                    confirm = c
            elif kind in ("ListControl", "DataGridControl") and listing is None:
                listing = c
            if edit_score >= 2 and confirm is not None and listing is not None:
                break
            if depth < 10:
                try:
                    queue_.extend((k, depth + 1) for k in c.GetChildren())
                except Exception:  # noqa: BLE001
                    pass
        return edit, confirm, listing

    def dialog_paths(self, dlg, edit) -> tuple[list[str], list[str]]:
        """(resolved paths, names that could not be resolved).

        What the dialog hands over is a *display* name: Explorer hides known
        extensions, so "hr_leave_register_aug2026" is what a picked .xlsx looks
        like, and it is relative to a folder the dialog does not spell out
        either. Both have to be recovered before anything can be masked.
        """
        try:
            raw = (edit.GetPattern(auto.PatternId.ValuePattern).Value or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        typed = re.findall(r'"([^"]+)"', raw) or ([raw] if raw else [])
        picked = self.dialog_selection(dlg)
        folder = self.dialog_folder(dlg)
        # A selected item may carry its whole path; a typed name needs the folder.
        found, missing = [], []
        for names in ((picked, "list selection"), (typed, "File name box")):
            hits = [p for p in (self.resolve_pick(n, folder) for n in names[0]) if p]
            if hits and len(hits) == len(names[0]):
                log(f"file guard: {names[1]} gave {len(names[0])} name(s) in "
                    f"{'a known folder' if folder else 'an UNKNOWN folder'}, all resolved")
                return hits, []
        for n in (typed or picked):
            p = self.resolve_pick(n, folder)
            (found if p else missing).append(p or n)
        if missing:
            log(f"file guard: could not locate {len(missing)} pick(s): "
                f"{', '.join(redact(m, keep=0) for m in missing[:3])}; "
                f"folder {'known' if folder else 'UNKNOWN'}")
            self.dump_dialog(dlg)
        return found, missing

    def dump_dialog(self, dlg) -> None:
        """Everything the dialog exposes, once per dialog, when a pick cannot be
        located. Guessing at which control holds the folder has cost several
        rounds; this shows what is actually there."""
        if self.dialog_dumped:
            return
        self.dialog_dumped = True
        log("file guard: dialog contents follow (to find where the folder lives)")
        stack, seen = [(dlg, 0)], 0
        while stack and seen < 150:
            c, depth = stack.pop()
            seen += 1
            try:
                value = self.value_of(c)
                log(f"file guard:   {' ' * depth}{c.ControlTypeName} "
                    f"name={redact(c.Name)} id={(c.AutomationId or '')!r} "
                    f"class={(c.ClassName or '')[:24]!r}"
                    + (f" value={redact(value)}" if value else ""))
                if depth < 8:
                    stack.extend((k, depth + 1) for k in reversed(c.GetChildren()))
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def resolve_pick(name: str, folder: str) -> str | None:
        """A display name to a real path: as given if absolute, else joined to
        the folder, else the same stem with whatever extension it really has."""
        p = Path(name)
        if p.is_absolute():
            return str(p) if p.exists() else None
        if not folder:
            return None
        base = Path(folder)
        direct = base / name
        if direct.is_file():
            return str(direct)
        try:
            hits = sorted(base.glob(globlib.escape(name) + ".*"))
        except OSError:
            hits = []
        return str(hits[0]) if hits else None

    def dialog_selection(self, dlg) -> list[str]:
        """What is selected in the dialog's file list. A shell item often
        carries its full path in its accessible value, which settles both the
        folder and the hidden extension at once, so this is tried first."""
        source = self.dialog_list
        if source is None:
            stack, seen = [(dlg, 0)], 0
            while stack and seen < 400 and source is None:
                c, depth = stack.pop()
                seen += 1
                try:
                    if c.ControlTypeName in ("ListControl", "DataGridControl"):
                        source = c
                        break
                    if depth < 8:
                        stack.extend((k, depth + 1) for k in c.GetChildren())
                except Exception:  # noqa: BLE001
                    continue
        if source is None:
            return []
        names = []
        try:
            sel = source.GetPattern(auto.PatternId.SelectionPattern)
            for item in (sel.GetSelection() if sel is not None else []):
                names.append(self.value_of(item) or (item.Name or "").strip())
        except Exception:  # noqa: BLE001
            return []
        return [n for n in names if n]

    @staticmethod
    def looks_like_path(value: str) -> str:
        """A path out of an accessible value such as "Address: C:\\Users\\x"."""
        v = (value or "").strip()
        if ":" in v[:40] and not (len(v) > 1 and v[1] == ":"):
            v = v.split(":", 1)[1].strip()        # drop an "Address:" style prefix
        return v if (len(v) > 2 and v[1] == ":") or v.startswith("\\\\") else ""

    def value_of(self, ctrl) -> str:
        for pid in (auto.PatternId.ValuePattern, auto.PatternId.LegacyIAccessiblePattern):
            try:
                pat = ctrl.GetPattern(pid)
                if pat is not None and pat.Value:
                    return str(pat.Value)
            except Exception:  # noqa: BLE001
                continue
        return ""

    def dialog_folder(self, dlg) -> str:
        """The folder the dialog is browsing. No single source is dependable, so
        the file list's own value, the breadcrumb's value and its item names are
        all tried; the first that names a real directory wins."""
        candidates = []
        if self.dialog_list is not None:
            candidates.append(self.value_of(self.dialog_list))
        stack, seen = [(dlg, 0)], 0
        while stack and seen < 300:
            c, depth = stack.pop()
            seen += 1
            try:
                if c.ControlTypeName == "ToolBarControl" and "address" in (c.Name or "").lower():
                    candidates.append(self.value_of(c))
                    for k in c.GetChildren():
                        candidates.append(self.value_of(k))
                        candidates.append(k.Name or "")
                    continue
                if depth < 6:
                    stack.extend((k, depth + 1) for k in c.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        # Any control at all whose name or value is a real directory: the
        # address bar is not always a toolbar, and its editable form carries the
        # path even when the breadcrumb shows only display names.
        stack, seen = [(dlg, 0)], 0
        while stack and seen < 250:
            c, depth = stack.pop()
            seen += 1
            try:
                candidates.append(self.value_of(c))
                candidates.append(c.Name or "")
                if depth < 8:
                    stack.extend((k, depth + 1) for k in c.GetChildren())
            except Exception:  # noqa: BLE001
                continue
        for value in candidates:
            path = self.looks_like_path(value)
            if path and Path(path).is_dir():
                return path
        return ""

    def poll_dialog(self) -> None:
        """Note when Claude opens or closes a file dialog (the hook needs to
        know whether a confirm belongs to one)."""
        now = time.time()
        if now - self.dialog_seen < DIALOG_POLL_S:
            return
        self.dialog_seen = now
        dlg = self.find_dialog()
        if dlg is None:
            if self.dialog is not None:
                log("file dialog closed")
            self.dialog = self.dialog_edit = self.dialog_confirm = self.dialog_list = None
            self.dialog_dumped = False
            SHARED["dialog_open"] = False
            SHARED["dialog_confirm_rect"] = SHARED["dialog_list_rect"] = None
            SHARED["dialog_rect"] = None
            return
        self.dialog = dlg
        if self.dialog_edit is None:
            # Retried on every poll: a dialog that has just appeared is not yet
            # built. A missing part must never *disarm* the guard - doing that
            # let the user's original file through, twice, on a real machine.
            edit, confirm, listing = self.dialog_parts(dlg)
            if confirm is None:
                # No Open button: a Save dialog, or one we do not understand.
                # Nothing is being attached, so there is nothing to guard.
                SHARED["dialog_open"] = False
                self.warn_dialog(f"file dialog open: title {redact(dlg.Name)} with no "
                                 f"Open button; left alone")
                return
            self.dialog_confirm, self.dialog_list = confirm, listing
            if edit is None:
                # An open-type dialog we cannot read yet. Stay armed: the confirm
                # is held rather than let through, and the box is looked for again
                # on the next poll.
                self.warn_dialog(f"file dialog open: title {redact(dlg.Name)} but no "
                                 f"File name box yet; holding the dialog")
                self.publish_dialog_rects()
                SHARED["dialog_open"] = True
                return
            self.dialog_edit = edit
            log(f"file dialog ready: title {redact(dlg.Name)} "
                f"box={(edit.Name or edit.AutomationId or 'edit')!r} "
                f"button={(confirm.Name or '')!r} "
                f"list={'found' if listing is not None else 'NOT FOUND'}")
            self.toast("File dialog: the file you pick will be masked before Claude sees it.")
        self.publish_dialog_rects()
        SHARED["dialog_open"] = True

    def publish_dialog_rects(self) -> None:
        """The hook decides with arithmetic, not UIA: a low-level hook must
        return fast, and a cross-process call inside one is asking for it to be
        dropped by Windows."""
        for key, ctrl in (("dialog_confirm_rect", self.dialog_confirm),
                          ("dialog_list_rect", self.dialog_list),
                          ("dialog_rect", self.dialog)):
            rect = None
            if ctrl is not None:
                try:
                    r = ctrl.BoundingRectangle
                    if r.right > r.left and r.bottom > r.top:
                        rect = (r.left, r.top, r.right, r.bottom)
                except Exception:  # noqa: BLE001
                    rect = None
            SHARED[key] = rect

    def warn_dialog(self, message: str) -> None:
        """Say it once per distinct message, not once per poll."""
        if message != self.dialog_said:
            self.dialog_said = message
            log(message)

    def cmd_dialog_confirm(self, pressed_at: float) -> None:
        """The hook swallowed a confirm (Enter, a click on Open, or a double
        click on a file) in Claude's file dialog."""
        dlg = self.find_dialog()
        if dlg is None:
            SHARED["dialog_open"] = False
            log("file guard: confirm arrived but the dialog is gone")
            return
        edit, confirm = self.dialog_edit, self.dialog_confirm
        if edit is None:
            edit, confirm, _listing = self.dialog_parts(dlg)
        if edit is None:
            # Cannot read what was picked, so cannot mask it. Holding is the only
            # honest answer: replaying here attached the original unmasked, which
            # is the disclosure this whole feature exists to prevent.
            log("file guard: no File name box found; holding the dialog")
            self.dump_dialog(dlg)
            self.toast("Cannot read this file dialog, so the file cannot be masked. Copy the "
                       "file in Explorer and paste it into Claude instead, or switch the file "
                       "guard off to attach it as it is.", error=True)
            return
        paths, missing = self.dialog_paths(dlg, edit)
        if not paths and not missing:
            self.dialog_replay(dlg, confirm)         # empty box, or a folder double-click
            return
        if missing:
            # Cannot find the file, so cannot mask it. Holding is the only safe
            # answer: letting it through is exactly the disclosure we exist to stop.
            self.toast(f"Cannot locate {', '.join(missing[:2])} on disk, so it cannot be masked. "
                       f"Copy the file in Explorer and paste it into Claude instead.", error=True)
            return
        if all(Path(p).is_dir() for p in paths):
            self.dialog_replay(dlg, confirm)         # navigating into a folder
            return
        self.emit(type="busy", busy=True)
        try:
            attach, unmasked, failed = self.mask_many([p for p in paths if not Path(p).is_dir()])
        finally:
            self.emit(type="busy", busy=False)
        if failed:
            self.toast("; ".join(failed[:2]) + " — attachment held. Fix this, or switch the "
                       "file guard off to attach as it is.", error=True)
            return                                    # dialog stays open for another try
        if not attach:
            self.dialog_replay(dlg, confirm)
            return
        msg, warn = self.attach_summary(attach, unmasked)
        if len(attach) - len(unmasked) == 0:
            # Nothing was masked (all images or the like): confirm the user's own pick.
            self.toast(msg + ".", level="warn")
            self.dialog_replay(dlg, confirm)
            return
        value = " ".join(f'"{p}"' for p in attach) if len(attach) > 1 else attach[0]
        try:
            edit.GetPattern(auto.PatternId.ValuePattern).SetValue(value)
        except Exception as e:  # noqa: BLE001
            self.toast(f"Masked, but the dialog would not take the path ({e}). "
                       f"Pick it from {self.files_dir()}", error=True)
            log(f"file guard: SetValue failed: {e}")
            return
        self.toast(msg + ".", level="warn" if warn else "success")
        self.dialog_replay(dlg, confirm)

    def dialog_replay(self, dlg, confirm) -> None:
        """Confirm the dialog the way the user did: the button if we found it,
        else Enter into the dialog."""
        try:
            if confirm is not None:
                confirm.GetPattern(auto.PatternId.InvokePattern).Invoke(waitTime=0)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            dlg.SetFocus()
        except Exception:  # noqa: BLE001
            pass
        self.replay_enter()

    def cmd_mask_clipboard_files(self, pressed_at: float) -> None:
        """Ctrl+V in Claude with files on the clipboard: mask, then paste ours."""
        paths = clipboard_files()
        if not paths:
            self.replay_paste()
            return
        self.emit(type="busy", busy=True)
        try:
            attach, unmasked, failed = self.mask_many(paths)
        finally:
            self.emit(type="busy", busy=False)
        if failed:
            self.toast("; ".join(failed[:2]) + " — paste held.", error=True)
            return
        msg, warn = self.attach_summary(attach, unmasked)
        if len(attach) - len(unmasked) > 0:          # something changed: swap the clipboard
            if not set_clipboard_files(attach):
                self.toast(f"Masked, but the clipboard would not take the files. "
                           f"Attach them from {self.files_dir()}", error=True)
                return
            self.clip_ignore_until = time.time() + 2.0
        self.toast(msg + ".", level="warn" if warn else "success")
        self.replay_paste()

    @staticmethod
    def replay_paste() -> None:
        auto.SendKeys("{Ctrl}v", waitTime=0)

    def cmd_mask_files(self, paths) -> None:
        """Mask files named on the command line (the Explorer menu entry)."""
        attach, unmasked, failed = self.mask_many(list(paths))
        made = [p for p in attach if str(p).lower() in self.masked_paths]
        if made:
            self.toast(f"Masked into {self.files_dir()}: " + ", ".join(Path(p).name for p in made[:3]))
        if unmasked:
            self.toast(f"Cannot be masked: {', '.join(unmasked[:3])}", error=True)
        if failed:
            self.toast("; ".join(failed[:2]), error=True)

    def poll_drag(self) -> None:
        """While a drag with files is in flight over Claude, put the blocker up."""
        if not (self.cfg.get("fileGuard", True) and self.cfg.get("blockDrops", True)):
            return
        dragging = SHARED["drag_at"] > 0 and (_user32.GetAsyncKeyState(0x01) & 0x8000)
        if not dragging:
            if SHARED["blocking"]:
                SHARED["blocking"] = False
                self.emit(type="block_drop", rect=None)
                # A quick click-release that merely passed over Claude is not a drop.
                if time.time() - SHARED["blocking_since"] > 0.3:
                    self.toast("A dropped file cannot be masked on its way in — use the paperclip "
                               "(it is guarded), or switch drop blocking off in settings.",
                               level="warn")
            SHARED["drag_at"] = 0.0
            return
        if foreground_exe() != CLAUDE_EXE and self.claude_win is None:
            return
        try:
            r = self.claude_win.BoundingRectangle if self.claude_win is not None else None
        except Exception:  # noqa: BLE001
            r = None
        if r is None:
            self.page_document()
            return
        x, y = auto.GetCursorPos()
        if not (r.left <= x <= r.right and r.top <= y <= r.bottom):
            if SHARED["blocking"]:
                SHARED["blocking"] = False
                self.emit(type="block_drop", rect=None)
            return
        if SHARED["blocking"] and time.time() - SHARED["blocking_since"] > BLOCKER_MAX_S:
            # Nothing invisible may sit over Claude eating clicks indefinitely:
            # the bar that would switch it off is underneath it.
            SHARED["blocking"] = False
            SHARED["drag_at"] = 0.0
            self.emit(type="block_drop", rect=None)
            log(f"drop blocker taken down after {BLOCKER_MAX_S:g}s; no drop arrived")
            return
        if not SHARED["blocking"]:
            SHARED["blocking"] = True
            SHARED["blocking_since"] = time.time()
            self.emit(type="block_drop", rect=(r.left, r.top, r.right, r.bottom))
            log("drop blocker up")

    def cmd_toggle_file_guard(self) -> None:
        if self.refuse_if_locked("fileGuard", "The file guard"):
            return
        self.cfg["fileGuard"] = not self.cfg.get("fileGuard", True)
        save_config(self.cfg)
        self.emit(type="file_guard", on=self.cfg["fileGuard"])
        self.toast("File guard on: files are masked before Claude sees them." if self.cfg["fileGuard"]
                   else "File guard off: files are attached as they are.")

    # ---- the guard
    @staticmethod
    def replay_enter() -> None:
        """Send the Enter the user asked for. Synthetic input carries the
        LLKHF_INJECTED flag, so the hook lets it through."""
        auto.SendKeys("{Enter}", waitTime=0)

    def cmd_guard(self, pressed_at: float) -> None:
        """An Enter the hook swallowed while Claude was in front."""
        log(f"guard: Enter received (queued {time.time() - pressed_at:.2f}s ago)")
        if pressed_at < self.mask_done_at:
            log("guard: stale Enter (pressed before the last check finished) -> dropped")
            self.toast("Send dropped: masking finished after you pressed Enter. Review, then press Enter again.", error=True)
            return
        ctrl = self.current_composer()
        if ctrl is None:                      # Enter somewhere else in Claude: not ours
            log("guard: focus is not the composer -> replay Enter")
            self.replay_enter()
            return
        text = self.read_text(ctrl)
        if not text.strip():
            log("guard: composer read as empty -> replay Enter")
            self.replay_enter()
            return
        if text.strip() == self.last_masked.strip():
            log(f"guard: text already checked ({len(text)} chars) -> replay Enter")
            self.replay_enter()
            return
        log(f"guard: unchecked text ({len(text)} chars) -> masking")
        r = self.mask_composer(ctrl)
        log(f"guard: mask result {r}")
        if r["done"] and r["changed"]:
            self.toast("Masked — press Enter again to send.")
        elif r["done"]:
            self.replay_enter()
        else:
            self.toast("Send held: the text could not be checked. Fix the connection, or switch the guard off.", error=True)

    def cmd_toggle_guard(self) -> None:
        if self.refuse_if_locked("guard", "The send guard"):
            return
        self.cfg["guard"] = not self.cfg.get("guard", True)
        save_config(self.cfg)
        self.emit(type="guard", on=self.cfg["guard"])
        self.toast("Guard on: Enter checks the text first." if self.cfg["guard"]
                   else "Guard off: Enter sends as typed.")

    def cmd_unmask_clipboard(self) -> None:
        try:
            text = auto.GetClipboardText()
        except Exception:  # noqa: BLE001
            text = ""
        if not text or "TOK_" not in text.upper():
            self.toast("Clipboard has no TOK_ tokens.")
            return
        restored, n = self.index.restore(text)
        if n:
            self.clip_ignore_until = time.time() + 1.0
            auto.SetClipboardText(restored)
            self.clip_seq = _user32.GetClipboardSequenceNumber()
            self.toast(f"Clipboard restored: {n} value{'' if n == 1 else 's'}.")
            return
        sid = self.cfg.get("sessionId")
        if not sid:
            self.toast("No session yet — mask something first.", error=True)
            return
        self.clip_ignore_until = time.time() + 1.0
        r = self.server.api("/api/unmask", "POST", {"text": text, "session_id": sid})
        if not r["ok"]:
            self.toast(r["error"], error=True)
            return
        d = r["data"]
        auto.SetClipboardText(d["text"])
        unresolved = len(d.get("unresolved") or [])
        self.toast(f"Clipboard restored: {d.get('restored', 0)} value(s)"
                   + (f", {unresolved} unknown token(s) left" if unresolved else "") + ".")


# ----------------------------------------------------------------------------- sign-in
class SignIn(threading.Thread):
    """Browser sign-in with a loopback redirect (RFC 8252): listen on 127.0.0.1,
    open the server's login page, take the one-time code the server redirects
    back with, exchange it for a session token."""

    def __init__(self, cfg: dict, events: "queue.Queue[dict]"):
        super().__init__(name="signin", daemon=True)
        self.cfg, self.events = cfg, events

    def run(self) -> None:
        result: dict = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                result["code"] = (q.get("code") or [""])[0]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = bool(result["code"])
                self.wfile.write(("<!doctype html><meta charset=utf-8><title>SafePII</title>"
                                  "<body style='font-family:system-ui;margin:3rem'>"
                                  + ("<h2>Signed in to SafePII</h2><p>You can close this tab and go back to Claude.</p>"
                                     if ok else "<h2>Sign-in did not complete</h2><p>Try again from the helper.</p>")
                                  + "</body>").encode("utf-8"))

            def log_message(self, *_):  # quiet
                pass

        try:
            srv = HTTPServer(("127.0.0.1", 0), Handler)
        except OSError as e:
            self.events.put({"type": "auth", "ok": False, "msg": f"Cannot listen on 127.0.0.1: {e}"})
            return
        port = srv.server_address[1]
        srv.timeout = SIGNIN_TIMEOUT_S
        done = f"http://127.0.0.1:{port}/done"
        url = self.cfg["serverUrl"].rstrip("/") + "/auth/login?" + urllib.parse.urlencode({"next": done})
        self.events.put({"type": "auth", "ok": True, "msg": "Waiting for the browser…"})
        webbrowser.open(url)
        srv.handle_request()          # exactly one request, or the timeout
        srv.server_close()
        code = result.get("code")
        if not code:
            self.events.put({"type": "auth", "ok": False, "msg": "Sign-in was cancelled or timed out."})
            return
        r = Server(self.cfg).api("/auth/exchange", "POST", {"code": code})
        if not r["ok"]:
            self.events.put({"type": "auth", "ok": False, "msg": r["error"]})
            return
        self.cfg["token"] = r["data"]["token"]
        save_config(self.cfg)
        p = r["data"].get("principal") or {}
        self.events.put({"type": "auth", "ok": True, "signed_in": True,
                         "msg": f"Signed in as {p.get('email') or p.get('name') or 'user'}"})


# ----------------------------------------------------------------------------- hotkeys
class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)


class Hotkeys(threading.Thread):
    """Global hotkeys and the guard's keyboard hook share one message loop:
    RegisterHotKey delivers to the registering thread, and a WH_KEYBOARD_LL
    hook needs a pumping thread too."""

    MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT, WM_HOTKEY = 0x0002, 0x0004, 0x4000, 0x0312
    BINDINGS = {1: ("M", "mask"), 2: ("U", "unmask_clipboard")}
    WH_KEYBOARD_LL, VK_RETURN, VK_SHIFT, VK_CONTROL = 13, 0x0D, 0x10, 0x11
    WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
    LLKHF_INJECTED, LLKHF_ALTDOWN = 0x10, 0x20

    def __init__(self, cfg: dict, commands: "queue.Queue[tuple]", events: "queue.Queue[dict]"):
        super().__init__(name="hotkeys", daemon=True)
        self.cfg = cfg
        self.commands = commands
        self.events = events
        self.swallow_up = False
        self.swallow_click = False
        self.last_down_at, self.last_down = 0.0, (0, 0)
        self.double_click_s = (_user32.GetDoubleClickTime() / 1000.0) if _IS_WIN else 0.5
        self._proc = None            # keep the callback alive for the hook's lifetime

    def claude_in_front(self) -> bool:
        """Asked before the hook swallows a key, so it never uses a cached
        answer: even a couple of seconds of staleness would mean holding
        someone's Enter in a browser or a mail client."""
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = process_exe(pid.value, fresh=True)
        if exe != CLAUDE_EXE:
            log(f"hook: foreground is {exe or '?'} (pid {pid.value}), not {CLAUDE_EXE}")
        return exe == CLAUDE_EXE

    WH_MOUSE_LL, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 14, 0x020A, 0x020E

    WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202

    def mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        """Notes wheel scrolling, collapses the panel on a click elsewhere, and
        swallows a click on the file dialog's confirm button so the pick can be
        masked first."""
        try:
            if n_code >= 0:
                ms = None
                if w_param == self.WM_LBUTTONDOWN:
                    ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    # LLMHF_INJECTED: our own replayed click, never the user's.
                    if SHARED["panel_rect"] and not (ms.flags & 0x01):
                        self.collapse_if_outside(ms.pt.x, ms.pt.y)
                if w_param in (self.WM_MOUSEWHEEL, self.WM_MOUSEHWHEEL) and foreground_exe() == CLAUDE_EXE:
                    SHARED["scroll_at"] = time.time()
                elif (w_param == self.WM_LBUTTONDOWN and SHARED["dialog_open"]
                      and self.cfg.get("fileGuard", True)):
                    if ms is not None and not (ms.flags & 0x01):
                        if self.dialog_click(ms.pt.x, ms.pt.y):
                            self.swallow_click = True
                            self.commands.put(("dialog_confirm", time.time()))
                            return 1
                elif w_param == self.WM_LBUTTONDOWN and foreground_exe() != CLAUDE_EXE:
                    SHARED["drag_at"] = time.time()     # a drag may have begun elsewhere
                elif w_param == self.WM_LBUTTONUP and self.swallow_click:
                    self.swallow_click = False
                    return 1
        except Exception as e:  # noqa: BLE001
            log(f"mouse hook error {type(e).__name__}: {e}")
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def collapse_if_outside(self, x: int, y: int) -> None:
        """A click anywhere but the bar closes the panel, the way a menu closes.

        It has to be seen here because the bar never takes focus
        (WS_EX_NOACTIVATE, so that clicking it leaves the caret in Claude): there
        is no FocusOut to listen for. The click is passed on untouched -- it was
        meant for whatever it landed on, and swallowing it would make the first
        click after opening the panel vanish, which is worse than a panel that
        stays open.

        The pill is excluded as well as the panel: its own chevron already
        toggles, and collapsing from here too would close and reopen on one
        click.
        """
        for rect in (SHARED["panel_rect"], SHARED["pill_rect"]):
            if rect and rect[0] <= x < rect[2] and rect[1] <= y < rect[3]:
                return
        # Not cleared here: close_panel owns that. A second click before the bar
        # has caught up only queues a second collapse, and closing an already
        # closed panel does nothing -- whereas clearing the rectangle from a
        # hook that may be overruled would leave it stale and stop the next
        # click working.
        self.events.put({"type": "collapse"})

    @staticmethod
    def worker_alive() -> bool:
        """Busy is not dead. Masking a workbook or restoring a download holds
        the worker for as long as the server takes, and treating that as a
        failure locked the keyboard mid-upload."""
        if SHARED["worker_busy"]:
            return True
        beat = SHARED["worker_beat"]
        return beat > 0 and (time.time() - beat) < WORKER_DEAD_S

    def guard_failed(self, n_code, w_param, l_param):
        """The worker has stopped answering, so nothing can be masked.

        Holding every Enter would make Claude unusable and the user would just
        kill the helper, which protects nobody. Which way this goes is the
        customer's decision, not ours: `onGuardFailure` defaults to "hold",
        and either way the bar says so rather than failing quietly.
        """
        if SHARED["alarm"] != "worker":
            SHARED["alarm"] = "worker"
            log(f"hook: the worker has not answered for {WORKER_DEAD_S:g}s; guard cannot mask")
            self.events.put({"type": "alarm", "what": "worker",
                             "msg": "SafePII has stopped protecting this app. Restart it."})
        if self.cfg.get("onGuardFailure", "hold") == "hold":
            self.swallow_up = True
            return 1
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    @staticmethod
    def inside(rect, x: int, y: int) -> bool:
        return rect is not None and rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def dialog_click(self, x: int, y: int) -> bool:
        """Should this click in the file dialog be held so the pick can be
        masked first? True for the Open button, and for the second click of a
        double click on the file list.

        A low-level mouse hook never receives WM_LBUTTONDBLCLK - Windows
        synthesises that later, from the window's own message handling - so the
        double click is timed here instead. That is why double-clicking a file,
        which is how most people pick one, went straight through.
        """
        now = time.time()
        gap, moved = now - self.last_down_at, abs(x - self.last_down[0]) + abs(y - self.last_down[1])
        self.last_down_at, self.last_down = now, (x, y)
        on_confirm = self.inside(SHARED["dialog_confirm_rect"], x, y)
        listing = SHARED["dialog_list_rect"]
        if listing is not None:
            on_list, how = self.inside(listing, x, y), "list"
        else:
            # The file list could not be found in this dialog. A double click
            # anywhere in it, other than on the confirm button, is still a pick,
            # and requiring the list meant Enter worked while double click did
            # nothing at all.
            on_list = self.inside(SHARED["dialog_rect"], x, y) and not on_confirm
            how = "dialog"
        double = gap <= self.double_click_s and moved <= 6
        held = on_confirm or (on_list and double)
        log(f"hook: dialog click at ({x},{y}) on_confirm={on_confirm} "
            f"in_{how}={on_list} double={double} -> {'held' if held else 'through'}")
        return held

    def hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code >= 0:
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                injected = bool(kb.flags & self.LLKHF_INJECTED)
                ctrl_down = bool(_user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
                if (kb.vkCode == ord("V") and ctrl_down and not injected
                        and w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN)
                        and self.cfg.get("fileGuard", True) and self.claude_in_front()
                        and clipboard_files()):
                    self.swallow_up = True
                    self.commands.put(("mask_clipboard_files", time.time()))
                    log("hook: Ctrl+V with files swallowed")
                    return 1
                if kb.vkCode == self.VK_RETURN and w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                    plain = not (kb.flags & self.LLKHF_ALTDOWN) \
                        and not (_user32.GetAsyncKeyState(self.VK_SHIFT) & 0x8000) and not ctrl_down
                    # Whether Claude is in front is asked FIRST and gates everything
                    # below. Checking the guard's own health before this swallowed
                    # Enter in every application on the machine, not just Claude.
                    front = self.claude_in_front()
                    if front and plain and not injected and not self.worker_alive():
                        return self.guard_failed(n_code, w_param, l_param)
                    if (SHARED["dialog_open"] and plain and not injected
                            and self.cfg.get("fileGuard", True) and front):
                        self.swallow_up = True
                        self.commands.put(("dialog_confirm", time.time()))
                        log("hook: dialog Enter swallowed")
                        return 1
                    guard = self.cfg.get("guard", True)
                    log(f"hook: Enter down injected={injected} plain={plain} claude_in_front={front} guard={guard}")
                    if guard and not injected and plain and front and not SHARED["dialog_open"]:
                        self.swallow_up = True
                        self.commands.put(("guard", time.time()))
                        log("hook: swallowed")
                        return 1
                elif kb.vkCode == self.VK_RETURN and not (kb.flags & self.LLKHF_INJECTED):
                    if w_param in (self.WM_KEYUP, self.WM_SYSKEYUP) and self.swallow_up:
                        self.swallow_up = False
                        return 1
        except Exception as e:  # noqa: BLE001 - never let the hook die
            log(f"hook: error {type(e).__name__}: {e}")
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            self.pump()

    def pump(self) -> None:
        mods = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        for hid, (key, _) in self.BINDINGS.items():
            if not _user32.RegisterHotKey(None, hid, mods, ord(key)):
                self.events.put({"type": "toast", "error": True,
                                 "msg": f"Ctrl+Shift+{key} is taken by another app; use the bar instead."})
        _user32.SetWindowsHookExW.restype = wt.HHOOK
        _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, wt.HINSTANCE, wt.DWORD]
        _user32.CallNextHookEx.restype = ctypes.c_ssize_t
        _user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        self._mproc = _HOOKPROC(self.mouse_proc)
        if not _user32.SetWindowsHookExW(self.WH_MOUSE_LL, self._mproc, _kernel32.GetModuleHandleW(None), 0):
            log("hook: mouse hook FAILED; scroll tracking falls back to polling")
        self._proc = _HOOKPROC(self.hook_proc)
        hook = _user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not hook:
            err = ctypes.get_last_error() or ctypes.GetLastError()
            log(f"hook: SetWindowsHookExW FAILED, error {err}")
            self.events.put({"type": "toast", "error": True,
                             "msg": f"Could not install the keyboard hook (error {err}); the guard is unavailable."})
        else:
            log(f"hook: installed (guard={'on' if self.cfg.get('guard', True) else 'off'})")
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY and msg.wParam in self.BINDINGS:
                self.commands.put((self.BINDINGS[msg.wParam][1],))
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))


# ----------------------------------------------------------------------------- overlay worker
class OverlayWorker(threading.Thread):
    """Paints the real values over the tokens in replies.

    Own thread and own COM apartment, so a slow read can never delay the Enter
    guard - which it did: locating each token by moving a range endpoint
    thousands of characters from the start of the document measured 400 ms per
    token, 30 s for a real chat, and the guard queued behind it.

    Tokens are found by walking the *visible lines* instead. A line's text and
    its rectangle cost one call each, and a token's offset inside its own line
    is small, so positioning it is cheap and the offset drift that defeated
    document-wide offsets does not accumulate. Between walks each token's
    rectangle is re-read (one call each, only for what is on screen), which is
    what follows scrolling.
    """


    def __init__(self, cfg: dict, events: "queue.Queue[dict]"):
        super().__init__(name="overlay", daemon=True)
        self.cfg = cfg
        self.events = events
        self.doc = None
        self.win = None
        self.doc_checked = 0.0
        self.items: list[dict] = []   # {"range", "value", "tok", "rect", "bg", "style"}
        self.last_frame = None
        self.visible = False
        self.walked_at = 0.0
        self.was_scrolling = False
        self.warned: set[str] = set()
        self.clip = None              # the band where conversation text may be painted
        self.clip_at = 0.0
        self.composer = None          # the composer element, focused or not
        self.composer_at = 0.0
        self.covered = False          # last placement failed because something covered it
        self.update_checked = 0.0

    # ---- plumbing
    def run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            while True:
                time.sleep(OVERLAY_TICK_S)
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - never let the thread die
                    log(f"overlay error {type(e).__name__}: {e}")
                    self.doc = None
                    self.hide()

    def hide(self) -> None:
        if self.visible or self.last_frame is not None:
            self.visible = False
            self.last_frame = None
            self.events.put({"type": "overlay", "items": None})

    def document(self):
        if self.doc is not None:
            return self.doc
        if time.time() - self.doc_checked < 2.0:
            return None
        self.doc_checked = time.time()
        self.doc, _ctrl, self.win = find_page_document()
        log(f"overlay: page document {'found' if self.doc else 'not found'}")
        self.items = []
        self.warned.clear()
        self.clip = None
        self.composer = None
        return self.doc

    # ---- where painting is allowed
    def conversation_scroller(self, win):
        """The scrollable element holding the conversation, found by hit-testing
        its middle and walking up to the first vertically scrollable ancestor.
        Its rectangle is the viewport: a token scrolled out of the conversation
        still reports a rectangle, and without this it lands on the header."""
        x = (win.left + win.right) // 2
        for frac in (0.45, 0.3, 0.6):
            y = win.top + int((win.bottom - win.top) * frac)
            try:
                ctrl = auto.ControlFromPoint(x, y)
            except Exception:  # noqa: BLE001
                return None
            for _ in range(14):
                if ctrl is None:
                    break
                try:
                    sp = ctrl.GetPattern(auto.PatternId.ScrollPattern)
                    if sp is not None and sp.VerticallyScrollable:
                        r = ctrl.BoundingRectangle
                        if r.bottom - r.top > 200 and r.right - r.left > 200:
                            return r
                    ctrl = ctrl.GetParentControl()
                except Exception:  # noqa: BLE001
                    break
        return None

    def composer_rect(self, win):
        """The composer's rectangle whether or not it has focus. SHARED holds it
        only while focused, and the conversation scrolls *under* the composer,
        so without this a token behind it gets painted over it."""
        focused = SHARED["composer_rect"]
        if focused is not None:
            return focused
        if self.composer is not None:
            try:
                r = self.composer.BoundingRectangle
                if r.bottom > r.top:
                    return (r.left, r.top, r.right, r.bottom)
            except Exception:  # noqa: BLE001
                self.composer = None
        if time.time() - self.composer_at < 5.0:
            return None
        self.composer_at = time.time()
        x = (win.left + win.right) // 2
        for up in (40, 70, 100, 140):
            try:
                ctrl = auto.ControlFromPoint(x, win.bottom - up)
            except Exception:  # noqa: BLE001
                return None
            for _ in range(8):
                if ctrl is None:
                    break
                try:
                    if COMPOSER_CLASS_HINT in (ctrl.ClassName or ""):
                        r = ctrl.BoundingRectangle
                        if r.top < (win.top + win.bottom) // 2:
                            break            # not the composer: it sits low in the window
                        self.composer = ctrl
                        log(f"overlay: composer at {(r.left, r.top, r.right, r.bottom)}")
                        return (r.left, r.top, r.right, r.bottom)
                    ctrl = ctrl.GetParentControl()
                except Exception:  # noqa: BLE001
                    break
        return None

    def update_clip(self, win) -> None:
        """Recompute the band where a patch may be drawn: inside the conversation
        viewport, and above the composer."""
        if time.time() - self.clip_at < 2.0 and self.clip is not None:
            return
        self.clip_at = time.time()
        scroller = self.conversation_scroller(win)
        top = scroller.top if scroller is not None else win.top
        bottom = scroller.bottom if scroller is not None else win.bottom
        left = scroller.left if scroller is not None else win.left
        right = scroller.right if scroller is not None else win.right
        if scroller is None:
            self.warn_once("noscroller", "overlay: no scrollable conversation found; "
                                         "painting is clipped to the window only")
        comp = self.composer_rect(win)
        if comp is not None:
            bottom = min(bottom, comp[1] - 4)
        clip = (left, top, right, bottom)
        if clip != self.clip:
            log(f"overlay: paint band {clip}")
        self.clip = clip

    # ---- the loop
    def tick(self) -> None:
        idx = SHARED["index"]
        if not self.cfg.get("overlay", True) or idx is None or not idx.vault:
            self.hide()
            return
        if foreground_exe() != CLAUDE_EXE:
            self.hide()
            return
        doc = self.document()
        if doc is None:
            self.hide()
            return
        try:
            self.update_clip(self.win.BoundingRectangle)
        except Exception:  # noqa: BLE001
            self.doc = None
            self.hide()
            return
        now = time.time()
        scrolling = now - SHARED["scroll_at"] < 0.35
        if self.was_scrolling and not scrolling:
            self.walked_at = 0.0          # the scroll ended: new lines came into view
        self.was_scrolling = scrolling
        moved = self.refresh_rects()
        since = now - self.walked_at
        if self.cfg.get("overlayHideOnScroll") and scrolling:
            self.hide()
            return
        if not scrolling and (since > OVERLAY_WALK_MAX_S
                              or (since > OVERLAY_WALK_MIN_S and (moved or not self.items))):
            self.walk(doc, idx)
        self.emit_frame()

    def refresh_rects(self) -> bool:
        """Re-read every item's rectangle. True when something moved, which is
        the signal that the page scrolled or reflowed and needs a re-walk.

        When a rectangle moves, the range's text is checked too: after an edit a
        kept range can point at different text, and drawing one person's name
        over another's token would be worse than drawing nothing. A mismatch
        drops the patch until the next walk rebuilds it."""
        moved = False
        for it in self.items:
            try:
                rects = it["range"].GetBoundingRectangles()
            except Exception:  # noqa: BLE001
                it["rect"] = None
                continue
            rect = None
            if rects:
                r = rects[0]
                rect = (r.left, r.top, r.right, r.bottom)
            if rect == it["rect"]:
                continue
            moved = True
            if rect is not None:
                try:
                    if (it["range"].GetText(-1) or "").upper() != it["tok"].upper():
                        rect = None               # this range is no longer that token
                except Exception:  # noqa: BLE001
                    rect = None
            it["rect"] = rect
        return moved

    # ---- finding tokens on the visible lines
    def first_visible_line(self, doc, win):
        """A line range at the top of the visible content. Hit-testing is the
        one primitive measured at ~1 ms, so the walk starts from a hit test in
        the middle of the window and steps back up to the top of the view."""
        mid_x = (win.left + win.right) // 2
        line = None
        for frac in (0.35, 0.5, 0.2, 0.65, 0.8):
            y = win.top + int((win.bottom - win.top) * frac)
            for x in (mid_x, win.left + (win.right - win.left) // 3):
                try:
                    hit = doc.RangeFromPoint(x, y)
                except Exception:  # noqa: BLE001
                    return None
                if hit is None:
                    continue
                cand = hit.Clone()
                cand.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
                try:
                    if (cand.GetText(-1) or "").strip():
                        line = cand
                        break
                except Exception:  # noqa: BLE001
                    continue
            if line is not None:
                break
        if line is None:
            return None
        for _ in range(80):                     # step up to the first visible line
            probe = line.Clone()
            if probe.Move(auto.TextUnit.Line, -1, waitTime=0) != -1:
                break
            probe.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
            rects = probe.GetBoundingRectangles()
            if not rects or rects[0].bottom < win.top:
                break
            line = probe
        return line

    def walk(self, doc, idx) -> None:
        t0 = time.perf_counter()
        self.walked_at = time.time()
        band = self.clip
        if band is None:
            return
        win = Box(*band)
        line = self.first_visible_line(doc, win)
        if line is None:
            if "noline" not in self.warned:
                self.warned.add("noline")
                log("overlay: no visible line found; overlay idle")
            self.items = []
            return
        items, lines, blind = [], 0, 0
        unplaced, unknown, covered, token_lines, by_hit = [], [], [], 0, 0
        debug = self.cfg.get("overlayDebug")
        while lines < OVERLAY_MAX_LINES and len(items) < OVERLAY_MAX_TOKENS and blind < 30:
            lines += 1
            try:
                text = line.GetText(-1) or ""
                rects = line.GetBoundingRectangles()
            except Exception:  # noqa: BLE001
                break
            if not rects:
                blind += 1                  # a stretch with no geometry: do not walk forever
                if debug:
                    log(f"overlay:   line {lines}: no rect, {redact(text)}")
            else:
                blind = 0
                r = rects[0]
                if debug:
                    log(f"overlay:   line {lines}: y={r.top}..{r.bottom} {redact(text)}")
                if r.top > win.bottom:
                    break                       # walked past the bottom of the view
                if r.top >= win.top and "TOK_" in text.upper():
                    token_lines += 1
                    for m in EXACT_RE.finditer(text):
                        tok = m.group(0)
                        value = idx.vault.get(tok)
                        if value is None:
                            unknown.append(tok)
                            continue
                        it = self.place(doc, line, (r.left, r.top, r.right, r.bottom),
                                        text, m, value)
                        if it is None:
                            (covered if self.covered else unplaced).append(tok)
                            continue
                        by_hit += it["how"] == "hit test"
                        items.append(it)
            if line.Move(auto.TextUnit.Line, 1, waitTime=0) != 1:
                break
            line.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
        self.items = items
        self.last_frame = None
        # Every walk says the whole outcome, so any fragment of the log is
        # conclusive: a one-off warning had already scrolled away by the time
        # anyone came to read it.
        log(f"overlay: walked {lines} lines ({token_lines} with tokens), "
            f"placed {len(items)}"
            + (f" ({by_hit} by hit test)" if by_hit else "")
            + (f", {len(covered)} behind a panel" if covered else "")
            + (f", UNPLACED {len(unplaced)}: {', '.join(unplaced[:4])}" if unplaced else "")
            + (f", NOT IN ANY VAULT {len(unknown)}: {', '.join(unknown[:4])}" if unknown else "")
            + f" in {(time.perf_counter() - t0) * 1000:.0f} ms"
            + (f"; style {items[0]['style']}" if items else ""))

    def warn_once(self, key: str, message: str) -> None:
        if key not in self.warned:
            if len(self.warned) > 200:
                self.warned.clear()
            self.warned.add(key)
            log(message)

    @staticmethod
    def text_of(rng) -> str:
        try:
            return rng.GetText(-1) or ""
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def range_at(line, start: int, length: int):
        """`length` characters starting `start` characters into `line`. None when
        the line is shorter than `start`. The end is clamped to the line."""
        try:
            rng = line.Clone()
            if rng.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start,
                                      auto.TextUnit.Character, start, waitTime=0) != start:
                return None
            rng.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, rng,
                                    auto.TextPatternRangeEndpoint.Start, waitTime=0)
            rng.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End,
                                   auto.TextUnit.Character, length, waitTime=0)
            return rng
        except Exception:  # noqa: BLE001
            return None

    def on_top(self, rect, tok: str) -> bool:
        """Is the conversation text the thing actually visible at this point?

        Claude's header, its usage banners and its composer float *over* the
        conversation, which keeps scrolling underneath them, so they are inside
        the scroller's rectangle and no band can exclude them. Asking which
        element is at the point answers it directly, whatever is floating:
        the element under a visible token carries the token in its name, and
        the element under a covered one belongs to whatever covers it.
        """
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        try:
            ctrl = auto.ControlFromPoint(x, y)
        except Exception:  # noqa: BLE001
            return True                       # cannot tell; leave it visible
        needle = tok.upper()
        for _ in range(3):                    # the text may be named on an ancestor
            if ctrl is None:
                break
            try:
                if needle in (ctrl.Name or "").upper():
                    return True
                ctrl = ctrl.GetParentControl()
            except Exception:  # noqa: BLE001
                return True
        return False

    def item(self, rng, tok: str, value: str, how: str) -> dict | None:
        try:
            rects = rng.GetBoundingRectangles()
        except Exception:  # noqa: BLE001
            return None
        if not rects:
            return None
        r = rects[0]              # a token wrapped over two lines gets its first part
        if r.right - r.left < 8 or r.bottom - r.top < 6:
            return None
        if not self.on_top((r.left, r.top, r.right, r.bottom), tok):
            self.covered = True               # something floats over it; not a placement fault
            return None
        return {"range": rng, "value": value, "tok": tok, "bg": None, "how": how,
                "rect": (r.left, r.top, r.right, r.bottom),
                "style": self.range_style(rng)}

    def place(self, doc, line, line_rect, text: str, m, value) -> dict | None:
        """A range over one token inside its line, verified by reading it back.

        Two ways, because one is cheap and the other is dependable.

        Counting characters into the line is cheap, and right most of the time.
        It is wrong when Chromium's character offsets disagree with the string
        GetText returns, which a list marker, an embedded object or a formatting
        run (bold, a link, a code span) can cause; the disagreement is not a
        fixed size, so no arithmetic repairs it. That is what left the bold
        account tokens unpainted.

        The fallback uses the one primitive measured as exact on a real chat:
        the range under a screen point, expanded to a word. The token's rough x
        is estimated from its position in the line, and the search steps
        outwards from there until the word under the point is the token.
        """
        tok = m.group(0)
        self.covered = False
        rng = self.range_at(line, m.start(), len(tok))
        if rng is not None and self.text_of(rng).upper() == tok.upper():
            it = self.item(rng, tok, value, "count")
            if it is not None:
                return it
        word = self.hit_test(doc, line_rect, text, m, tok)
        if word is not None:
            it = self.item(word, tok, value, "hit test")
            if it is not None:
                return it
        if self.cfg.get("overlayDebug"):
            log(f"overlay:   {tok} at offset {m.start()} in a {len(text)}-char line: "
                f"counting reached {redact(self.text_of(rng))}, hit test "
                f"{'found nothing' if word is None else 'gave no usable rectangle'}")
        return None

    def hit_test(self, doc, line_rect, text: str, m, tok: str):
        """The word under the token's estimated screen position, if it is the
        token. Proportional estimation is rough with a proportional font, so the
        search steps outwards from the guess."""
        left, top, right, bottom = line_rect
        y = (top + bottom) // 2
        middle = (m.start() + len(tok) / 2) / max(1, len(text))
        x0 = left + int((right - left) * middle)
        step = max(6, (right - left) // max(1, len(text)))
        for i in (0, -1, 1, -2, 2, -3, 3, -4, 4, -6, 6, -8, 8, -11, 11, -15, 15):
            x = min(max(left, x0 + i * step), right - 1)
            try:
                hit = doc.RangeFromPoint(x, y)
            except Exception:  # noqa: BLE001
                return None
            if hit is None:
                continue
            word = hit.Clone()
            word.ExpandToEnclosingUnit(auto.TextUnit.Word, waitTime=0)
            seen = self.text_of(word).strip()
            if seen.upper() == tok.upper():
                return word
            if tok.upper() in seen.upper() and len(seen) <= len(tok) + 4:
                return word          # the token plus a bracket or full stop
        return None

    @staticmethod
    def range_style(rng) -> dict:
        """Font family / size (pt) / weight / italic / foreground of a range.
        Read per token: a bold token must be painted bold, and headings and
        code spans differ from the prose around them."""
        want = {"family": (auto.TextAttributeId.FontNameAttribute, str),
                "size": (auto.TextAttributeId.FontSizeAttribute, (int, float)),
                "weight": (auto.TextAttributeId.FontWeightAttribute, (int, float)),
                "italic": (auto.TextAttributeId.IsItalicAttribute, bool),
                "fg": (auto.TextAttributeId.ForegroundColorAttribute, (int,))}
        style = {}
        for key, (aid, types) in want.items():
            try:
                v = rng.GetAttributeValue(aid)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(v, bool) and key != "italic":
                continue
            if isinstance(v, types):
                style[key] = v
        if "fg" in style:
            c = int(style["fg"])
            style["fg"] = f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"
        return style

    # ---- drawing
    def emit_frame(self) -> None:
        try:
            w = self.win.BoundingRectangle
        except Exception:  # noqa: BLE001
            self.doc = None
            self.hide()
            return
        band = self.clip or (w.left, w.top, w.right, w.bottom)
        bl, bt, br, bb = band
        frame = []
        for it in self.items:
            rect = it["rect"]
            if rect is None:
                continue
            l, t, r, b = rect
            # Fully inside the band, not merely overlapping it: half a patch over
            # the header or the composer is worse than none.
            if t < bt or b > bb or l < bl or r > br:
                continue
            if it["bg"] is None:                # sampled once, before our own patch is drawn
                it["bg"] = screen_pixel(l - 2, (t + b) // 2) or "#ffffff"
            frame.append((rect, it["value"], it["bg"], it["style"]))
        if frame != self.last_frame:
            self.last_frame = frame
            self.visible = bool(frame)
            self.events.put({"type": "overlay", "items": frame or None,
                             "win": (w.left, w.top, w.right, w.bottom)})


# ----------------------------------------------------------------------------- drop blocker
class DropBlocker:
    """A file dropped from Explorer onto Claude cannot be masked in flight (the
    drop is a shell handshake, not a message we can rewrite), so we refuse it:
    while the left button is held over Claude with files on the drag, an
    invisible window that registers no drop target sits on top. Windows then
    shows "not allowed" and nothing reaches Claude. The user is told to use the
    paperclip, which IS guarded."""

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.01)          # effectively invisible, still hit-tested
        self.win.configure(bg="#000000")
        self.win.withdraw()
        self.win.update_idletasks()
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x80
        hwnd = _user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self.visible = False

    def show(self, rect) -> None:
        if not rect:
            return
        left, top, right, bottom = rect
        self.win.geometry(f"{right - left}x{bottom - top}+{left}+{top}")
        if not self.visible:
            self.win.deiconify()
            self.win.lift()
            self.visible = True

    def hide(self) -> None:
        if self.visible:
            self.win.withdraw()
            self.visible = False


# ----------------------------------------------------------------------------- overlay window
class Overlay:
    """One transparent, click-through, topmost window over Claude; a canvas
    paints a background patch and the real value over each token rectangle."""

    KEY = "#010203"          # the colour that is rendered as fully transparent

    def __init__(self, root: tk.Tk):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.KEY)
        try:
            self.win.attributes("-transparentcolor", self.KEY)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(self.win, bg=self.KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.win.withdraw()
        self.win.update_idletasks()
        # click-through + never activated + no taskbar entry
        GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT, WS_EX_LAYERED = -20, 0x08000000, 0x80, 0x20, 0x80000
        hwnd = _user32.GetParent(self.win.winfo_id()) or self.win.winfo_id()
        style = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TRANSPARENT | WS_EX_LAYERED)
        exclude_from_capture(hwnd, "overlay")
        self.fonts: dict[tuple, tkfont.Font] = {}
        self.families = {f.lower() for f in tkfont.families(root)}
        # tkinter's own screen DPI, which describes the primary monitor. Only
        # the fallback: each frame asks the monitor Claude is actually on.
        self.tk_dpi = root.winfo_fpixels("1i")
        self.dpi = self.tk_dpi
        self.geom = None
        self.visible = False

    def font(self, family: str, px: int, bold: bool, italic: bool) -> tkfont.Font:
        key = (family, px, bold, italic)
        if key not in self.fonts:
            self.fonts[key] = tkfont.Font(family=family, size=-px,
                                          weight="bold" if bold else "normal",
                                          slant="italic" if italic else "roman")
        return self.fonts[key]

    def resolve_family(self, css_family) -> str:
        """First installed family from a CSS font-family list, else Segoe UI."""
        for name in (css_family or "").split(","):
            name = name.strip().strip("'\"")
            if name and name.lower() in self.families:
                return name
        return "Segoe UI"

    @staticmethod
    def text_colour(bg: str) -> str:
        r, g, b = int(bg[1:3], 16), int(bg[3:5], 16), int(bg[5:7], 16)
        return "#111827" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#f3f4f6"

    def render(self, items, win_rect) -> None:
        if not items:
            if self.visible:
                self.win.withdraw()
                self.visible = False
            return
        left, top, right, bottom = win_rect
        geom = f"{right - left}x{bottom - top}+{left}+{top}"
        if geom != self.geom:
            self.win.geometry(geom)
            self.geom = geom
        dpi = monitor_dpi(win_rect, self.tk_dpi)
        if dpi != self.dpi:
            log(f"overlay: painting at {dpi:g} dpi ({dpi / 96:.2f}x) "
                f"on the monitor at {win_rect}")
            self.dpi = dpi
        floor = min_font_px(dpi)
        c = self.canvas
        c.delete("all")
        for (l, t, r, b), value, bg, style in items:
            x, y, w, h = l - left, t - top, r - l, b - t
            bg = bg if bg and bg.lower() != self.KEY else "#ffffff"
            fg = style.get("fg") or self.text_colour(bg)
            family = self.resolve_family(style.get("family"))
            bold = float(style.get("weight") or 400) >= 600
            italic = bool(style.get("italic"))
            px = patch_font_px(style, h, dpi)
            f = self.font(family, px, bold, italic)
            text = value
            while f.measure(text) > w and px > floor:
                px -= 1
                f = self.font(family, px, bold, italic)
            if f.measure(text) > w:
                while text and f.measure(text + "…") > w:
                    text = text[:-1]
                text += "…"
            # the patch hides the token; a dotted line marks a restored value, as the extension does
            c.create_rectangle(x, y, x + w, y + h, fill=bg, outline=bg)
            c.create_text(x, y + h / 2, text=text, anchor="w", fill=fg, font=f)
            c.create_line(x, y + h - 1, x + w, y + h - 1, fill=fg, dash=(2, 3))
        if not self.visible:
            self.win.deiconify()
            self.visible = True


# ----------------------------------------------------------------------------- UI
# The shield mark, the same one the extension draws, rendered from
# webui/static/logo-shield.svg and carried inline so the helper stays a single
# file. 60 px square so Tk, which only scales by whole numbers, gets exactly 30
# or 20 with nothing left over. The old build used extension/icon48.png, which
# is the redaction-mark app tile: a different drawing, so the two halves of the
# product did not match.
LOGO_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAADwAAAA8CAYAAAA6/NlyAAAJMUlEQVR42u2aWWxj1RnH/98519e+tuN1nJB14iwzCRkm"
    "w06BIlqVSt2FCvSpEqISUvvQPvQNqSBV7WPbFwq0qkqrSlSoosuIVhWihQzrUESHYQZIhkniJE4mTuLt+tq+9j3n9MFJ"
    "mCWLkzgZUPNJkX3l+51zf+dbzne+G2Bf9mVf9mVf9mVf/m+EMQaP1+9ijF2d+beryDWNNJdOW9ULx1rbB47d+vtIc1vn"
    "VnU1l05cc9FOgPl2lHSPwQ/2DX0rEmu9JZ9ZPCWlqEsvGImFeq4d/qPPF/y6Pxi+wTJzf7NLxXK9sPGBow+Fos1Dlpk7"
    "K5yq2hNgX1PQ0zM4/Ejsms5feAOBL0opXzUz6QSw8fzepqCnd/DY476m4L2KCC6Xq9vnD7abufQ/qxXb2UiXiKG9u/+z"
    "bV19f/AHI/cb/iZeLOTe3ExvR8BEhFC0JdozOPzLULj5B8SIEZjLFwjeYZeKx4uFfG49Xbfh03oGh38UisS+DxAIteVx"
    "e4yjhtfP8pmlEcepyvX0D7R2dB3sH3qOa1orEcjw+e4KhCJ9drk0Ui5ZxYYDM8bR3NbVHx8c/pPXH/gKrUQRAZxrUa8/"
    "MFDIZ49XyqXK5bou3c26Dx95+ECs/acgWs0ZBIKCguHxfkZzu1P5zNI7Uogr3KQpGPH3Dhz7nW4Yt9WWqqatu40jwUjs"
    "bulUTxQtc0kp1RhgzjXq6Dn8ua7+a/+i6+6hjye9GErv8xg+PZde+Le4yFKca+jsG/xaS0f8N8SYfoXXgAAiZvgD93DG"
    "z+QzS6NKfWxot8fgPYPX/9gfijy41ryca+2haOxexvh7hXx6Qkm5c2DD5/f0Dd34oqa5ukDrJUiC2/Ddxhh9kM8svq+U"
    "BBFD68HeW9rjh57ljAc2iRfu94fukUqOmLn0DJQC5xp19Q/df6C142e0zsREBGI84G0K3pVOzT5VT0yzOmKXiJEB2uw+"
    "sJaO7sdbOuLDRIRYW2dvR/zwM5zzA5vOAYA0FunoPvRMc1vXISJCc3v3dS3t3Y8DxDbTZYyM5a+bZ/vGFhXagfbuQ792"
    "uT2PxVo7HnHp7t66kyIImq7Hu3qvfdrw+n8Sa+t6jNWxWFveyxs6GgG6YdzcGT/0/HaLGrfHe3t7fOB5kNqVUkxr9IC0"
    "snHuYAACWJ0eunel5ae2lt8H3gfeB/5Uy86z9EoNe/lnQ1I+rf151YCX4aSuw441oxqOQHrcUIzvnFUKsLINVyYN90IK"
    "rFKpzbdD6B1buBoIIH3nXSh2xyE8HihNqz2Uws62UilBQoCXy/BOTiDy6gm48vmr7dIK5pGjyB85ugrHSyU0nXkPxsw0"
    "quEIcseuRzUQWJN9xfndCwsInD4Fblko9vbBPDwApWlQnMPx+WAOHYErk0bkjdf3DFitVxZ5ZpOI/esFkJSoRKNQxBA9"
    "8XLNBRkDVStYvPvzUJyvCc3tMiKvvQL/2CigFLxTUxCGAau3D6QUFABFhGo4stHTKWzWcqkXWCmllJQFcLRc8RsRvBPj"
    "8I6fB0mJUmcXnKYmsGoVYAxQCp7ZWZDjQHG+9hGrXIb7woVaGBCBl4pwp1Kw+vovSYSsUln/GaUsqDo7AKwOYCmlNNet"
    "mxkDOIfiHLxUgtT1Whyr2qJXotG1YZdBpO5GNRwGpFy+1mvXFz0/SQl9aXHNHUABkEqa9QLXYWGpHKea0t3ujbMQETQz"
    "j2o4guyNN8OYTtS+33JrbQEuA1XL2VYYBtK33wml6+DFIgp9/bDiPTW45aysWQV4kjPrHlaEU00pWUe7ox5g4TjCqdjj"
    "yte0YdJVRCDHgZGYxPyXv4r07XdAMbZqXboMlpaBFGModXbCbm2tXWva6u8ri+IbG4O+tLTullSplD8SwpENcWkphKrY"
    "9vhmO8zysRBGcgbBd0/VYDWt1s243LJEMBKTCP/n5GpsSk2DdLkuhSWCMTuD4Kl3QEKsLsAVW6NtT0ghGmRh4ahysfB+"
    "PRurIgIJgeA7b0NxhtyxGyC83kvcnoSANzGJ6MhLcGUyoGoVuRtuguPzXTqOlDCmEoiOvAQ9nb7USy6L4nLR+kAIp3Hb"
    "UsnKj0opskRaaKNCh5Yflts2Im+8DiOZROHQYdixGBTX4Mrn4J0Yh+/cOWhWAQAQfuskPLNJWP2HUW65BkrToJl5eBOT"
    "8I+NQjPNDctKIUS2WMh/2NB9uJDPJp1KdUL3aNfX0/FYsbR3/DyMqUQtLkG1crFaXbUigNp9k5MwZmbWvW8jvxLV6oRl"
    "ZpMNPS2VS8Vywcw9v8U2z6oLM9sGt8urEFiGqPe+jcQys8fLpaLdUGDhVFV2af6vCpDYiizDXPG33fvWKDmyS6njW3mx"
    "Vvd5OLs4f7ZctF75xBxsFVAuWiOZxfmzu9IAKFmmnVmce0JBfTKACUgvXniyZJn2rgArpZBKJv5ul0pvfgKMi3KpeDKV"
    "TPxDbbHhsKUWj5XPWgtzU49uOZYbDEuAXJibetTKZ62t6m+5NVG2ClP+YKTFbXhvoqtEbGaXfpUYO/PERu+UGwYsnKqs"
    "VspvBaOxL2ia1rpbbwjWo61U7FOTo6cfKuQzhe2MsK3mk12yikrKN4Ph5vuIMe9e4QpHLk6f/+Cbi3PT49sdY9vdNquQ"
    "mwfhbX8o/A1GzLPbsFKIbDIx9sBs4twbSm4/hWwbWCmFQj6TYIyf9jeFvkSMGbsJOzt1/tsz46MvSOHsaKwd9VOVlDBz"
    "6Y+Ukq/5g6F7iHiQqKH5CcKpTCcnxh6YmRh7cbv/qtQw4BXoQi4zZZdKx32B4LDL5epuSCJTgF2yRhJjZ++7MDPx351a"
    "tmHAK+5tmbmMlc/+WXcbyu01biRiru2CSohibmnh5+Mfvvu9dGp2TjXwbQZvZKzZ5aKdS6deFo444fH6BrlL79iqrct2"
    "8eRs4qMHp86dfbpYyJcbnQ94owcUwlFmNj1lZpeeZYxPuz3e64iz0IYHPaUgnGpyKZV8ZHL09A8X55LnGhGv6x5dd0sY"
    "5whGYrFrOuLfCUZiD2uaHr9kVgU4ojKdSy88OT89+dtceiElhLOrp5M9KZM41ygQPhCNtXXeF4o2f1fTPQOObX+YTc8/"
    "tTg3/Vwus7ggHGdPjmF7Wg4zzuFrCjX5A+F4IZ+ZsMysKYXAvuzLvmxb/gdXBjBuP68WMQAAAABJRU5ErkJggg=="
)


def app_logo(root):
    """The mark at a size that suits the display. Tk scales by whole numbers
    only, so 48 gives a clean 24 or 16.

    Not cached across calls: a PhotoImage belongs to the interpreter that made
    it, and the caller has to hold the reference anyway or Tk collects it and
    the label goes blank."""
    scale = 1.0
    try:
        scale = root.winfo_fpixels("1i") / 96.0
    except Exception:  # noqa: BLE001
        pass
    try:
        return tk.PhotoImage(master=root, data=LOGO_PNG).subsample(
            2 if scale >= 1.4 else 3)   # 30 px on a high-density screen, 20 otherwise
    except Exception as e:  # noqa: BLE001 - a missing mark must not stop the bar
        log(f"could not load the logo: {e}")
        return None


class Bar:
    """A small pill above the composer that opens into a panel.

    The old form was a 620 px strip carrying a label, five buttons, four
    toggles and a status line that faded after six seconds, so it read as
    clutter and a warning nobody happened to be looking at was simply lost.

    Shape follows what shipping products in this category settle on. The pill
    is a split button, primary action on the left and status plus a chevron on
    the right, which is how Windows draws a SplitButton: outer corners rounded,
    no gap and no radius at the seam. Collapsed, the four toggles live in the
    hover tooltip rather than on screen, which is how Netskope's tray icon
    reports per-service state without showing four controls. Numbers come from
    the Windows spacing scale: 16 px from a surface to its text, 12 between
    groups, 8 between siblings, 32 px rows, 1 px strokes, 8 px panel radius.

    Nothing here re-opens the panel on a background event. Zoom's floating
    toolbar re-appearing whenever a participant joins is the most complained
    about behaviour in this whole category; only a blocking alert opens it, and
    only once.
    """

    # geometry (see the docstring for provenance)
    PILL_H, PANEL_W, ROW_H, PAD, GAP, TIGHT = 32, 300, 30, 16, 12, 8
    CLEAR = 10          # space left between the pill and the composer's border
    MARGIN = 8          # space kept between the panel and the edge of the screen
    MIN_PANEL_H = 140   # below this it is not worth showing at all
    RECENT_KEEP = 10

    # A shape for every state, so nothing depends on colour alone: about eight
    # per cent of men cannot separate red from green.
    GLYPH = {"protected": "●", "working": "◐", "off": "⊘",
             "warn": "▲", "error": "✖", "detached": "◌"}

    BG, LINE = "#1b2030", "#2f3747"
    # Square corners, so the outline is the whole shape and is a shade brighter
    # than the internal dividers. Clipping a window to a region cannot
    # antialias, so an approximated curve showed every step; a crisp edge is the
    # honest version of the same intent.
    EDGE = "#46526b"
    FG, DIM = "#e8ebf2", "#97a1b5"
    OK, WARN, ERR, ACCENT = "#5bbf87", "#e0b050", "#e06c6c", "#6f8fd6"

    def __init__(self, cfg: dict, commands: "queue.Queue[tuple]", events: "queue.Queue[dict]"):
        self.cfg, self.commands, self.events = cfg, commands, events
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

        self.problems: list[dict] = []     # unacknowledged warnings and errors
        self.recent: list[tuple] = []      # (time, level, message)
        self.state = "protected"
        self.flash = ""                    # a transient line on the pill
        self.flash_after = None
        self.busy = False
        self.panel = None
        self.panel_at = None
        self.body = self.canvas = self.vbar = None
        self.settings_win = None
        self.blocked = False               # a blocking alert opened the panel once
        self.update = None                 # (version, url) when the server has a newer build
        self.sharing = ("", 0)             # (folder name, files served) while a folder is shared
        self.asking = False               # a folder picker Claude asked for is open
        self.rect = None
        self.visible = False

        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.EDGE)
        self.win.withdraw()
        shell = tk.Frame(self.win, bg=self.BG)
        shell.pack(fill="both", expand=True, padx=1, pady=1)

        self.mask_btn = tk.Label(shell, text="Mask", bg="#2a3346", fg=self.FG, padx=12,
                                 font=("Segoe UI", 9, "bold"), cursor="hand2")
        self.mask_btn.pack(side="left", fill="y")
        self.mask_btn.bind("<Button-1>", lambda e: self.commands.put(("mask",)))
        tk.Frame(shell, bg=self.LINE, width=1).pack(side="left", fill="y")

        face = tk.Frame(shell, bg=self.BG, cursor="hand2")
        face.pack(side="left", fill="both", expand=True)
        self.logo = app_logo(self.root)
        # The mark says what this is; the word says how it is doing. Without a
        # mark the pill is an unlabelled thing hovering over someone's chat.
        self.glyph = tk.Label(face, bg=self.BG, fg=self.OK, font=("Segoe UI", 11), padx=8)
        if self.logo is not None:
            self.glyph.configure(image=self.logo)
        else:
            self.glyph.configure(text=self.GLYPH["protected"])
        self.glyph.pack(side="left")
        self.status = tk.Label(face, text="Protected", bg=self.BG, fg=self.FG,
                               font=("Segoe UI", 9), anchor="w")
        self.status.pack(side="left")
        self.badge = tk.Label(face, text="", bg=self.ERR, fg="#ffffff", padx=5,
                              font=("Segoe UI", 8, "bold"))
        self.chev = tk.Label(face, text="⌄", bg=self.BG, fg=self.DIM,
                             font=("Segoe UI", 10), padx=8)
        self.chev.pack(side="right")
        for w in (face, self.glyph, self.status, self.chev):
            w.bind("<Button-1>", lambda e: self.toggle_panel())
            w.bind("<Enter>", lambda e: self.show_pill_tip())
            w.bind("<Leave>", lambda e: self.hide_pill_tip())
        make_no_activate(self.win)

        self.pilltip = self._floating("#10141f")
        self.pilltip_label = tk.Label(self.pilltip, text="", bg="#10141f", fg=self.FG,
                                      font=("Segoe UI", 8), justify="left", padx=10, pady=6)
        self.pilltip_label.pack()

        self.tip = self._floating("#111827")
        self.tip_label = tk.Label(self.tip, text="", bg="#111827", fg="#f9fafb",
                                  font=("Segoe UI", 10), justify="left", wraplength=520,
                                  padx=10, pady=6)
        self.tip_label.pack()
        if _IS_WIN:
            exclude_from_capture(_user32.GetParent(self.tip.winfo_id()) or self.tip.winfo_id(),
                                 "tooltip")

        self.overlay = Overlay(self.root)
        self.blocker = DropBlocker(self.root)
        self.render_pill()
        self.root.after(100, self.pump)
        if not ((cfg.get("apiKey") or "").strip() or (cfg.get("token") or "").strip()):
            self.root.after(300, self.open_settings)

    def _floating(self, bg: str) -> tk.Toplevel:
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=bg)
        w.withdraw()
        make_no_activate(w)
        return w

    # ---- the pill
    def render_pill(self) -> None:
        """One place decides what the pill says, so two subsystems cannot fight
        over it. Severity wins, in the order errors, warnings, then normal."""
        worst = self.worst_problem()
        if self.busy:
            state, text, colour = "working", "Masking…", self.ACCENT
        elif worst and worst["level"] == "error":
            state, text, colour = "error", "Problem", self.ERR
        elif worst:
            state, text, colour = "warn", "Check this", self.WARN
        elif not self.cfg.get("guard", True):
            state, text, colour = "off", "Guard off", self.WARN
        elif self.flash:
            state, text, colour = "protected", self.flash, self.OK
        else:
            # Nothing to report, so the slot carries the name instead.
            state, text, colour = "protected", "SafePII", self.DIM
        self.state = state
        if self.logo is None:                 # no mark: the shape carries the state
            self.glyph.configure(text=self.GLYPH[state], fg=colour)
        self.status.configure(text=text, fg=colour if state != "protected" else self.DIM)
        n = len(self.problems)
        if n:
            self.badge.configure(text=str(n))
            self.badge.pack(side="right", padx=(0, 4))
        else:
            # Cleared as well as hidden, or a stale count flashes if it returns.
            self.badge.configure(text="")
            self.badge.pack_forget()
        self.win.update_idletasks()
        need = (self.mask_btn.winfo_reqwidth() + self.glyph.winfo_reqwidth()
                + self.status.winfo_reqwidth() + self.chev.winfo_reqwidth()
                + (self.badge.winfo_reqwidth() + 4 if n else 0) + 10)
        self.pill_w = max(176, need)
        if self.rect:
            self.place(self.rect, force=True)

    def pill_tip_text(self) -> str:
        """The four toggles, reported on hover instead of shown as controls."""
        parts = [("Guard", self.cfg.get("guard", True)), ("Unmask", self.cfg.get("unmask", True)),
                 ("Overlay", self.cfg.get("overlay", True)), ("Files", self.cfg.get("fileGuard", True))]
        said = "SafePII\n" + "   ".join(f"{name} {'on' if on else 'off'}" for name, on in parts)
        # A shared folder is a live path out of the machine, so it belongs where
        # the state is read at a glance, not only inside the panel.
        if self.sharing[0]:
            said += f"\nSharing {self.sharing[0]} ({self.sharing[1]} files)"
        return said

    def show_pill_tip(self) -> None:
        if self.panel or not self.visible:
            return
        self.pilltip_label.configure(text=self.pill_tip_text())
        self.pilltip.update_idletasks()
        x = self.win.winfo_x() + self.pill_w - self.pilltip.winfo_reqwidth()
        self.pilltip.geometry(f"+{max(0, x)}+{self.win.winfo_y() + self.PILL_H + 6}")
        self.pilltip.deiconify()

    def hide_pill_tip(self) -> None:
        self.pilltip.withdraw()

    def place(self, rect, force: bool = False) -> None:
        left, top, right, bottom = rect
        x, y = right - self.pill_w, top - self.PILL_H - self.CLEAR
        if y < 0:
            y = bottom + self.CLEAR
        if rect != self.rect or force:
            self.win.geometry(f"{self.pill_w}x{self.PILL_H}+{x}+{y}")
            SHARED["pill_rect"] = (x, y, x + self.pill_w, y + self.PILL_H)
            self.rect = rect
            if self.panel:
                self.place_panel()
        if not self.visible:
            self.win.deiconify()
            self.visible = True

    def hide(self) -> None:
        self.hide_pill_tip()
        if self.visible:
            self.win.withdraw()
            self.visible = False
            self.rect = None
            SHARED["pill_rect"] = None
        if self.panel and not self.problems:
            self.close_panel()

    def show_somewhere(self) -> None:
        """When the composer is not on screen there is nothing to anchor to, so
        a problem still gets shown, bottom right of wherever the bar last was."""
        _l, _t, right, bottom = work_area(self.win)
        self.win.geometry(f"{self.pill_w}x{self.PILL_H}"
                          f"+{right - self.pill_w - 24}+{bottom - 120}")
        self.win.deiconify()

    # ---- alerts
    def worst_problem(self):
        for level in ("error", "warn"):
            for p in self.problems:
                if p["level"] == level:
                    return p
        return None

    def alert(self, msg: str, level: str = "info") -> None:
        self.recent.insert(0, (time.strftime("%H:%M"), level, msg))
        del self.recent[self.RECENT_KEEP:]
        if level in ("warn", "error"):
            # Kept until acknowledged. A line that fades is the wrong shape for
            # "that file was not masked".
            if not any(p["msg"] == msg for p in self.problems):
                self.problems.append({"msg": msg, "level": level, "at": time.strftime("%H:%M")})
            if not self.visible:
                self.show_somewhere()
            if self.panel:
                self.build_panel()
        else:
            self.flash = msg if len(msg) <= 34 else msg[:33] + "…"
            if self.flash_after:
                self.root.after_cancel(self.flash_after)
            self.flash_after = self.root.after(4000, self.clear_flash)
            if self.panel:
                self.build_panel()
        self.render_pill()

    def clear_flash(self) -> None:
        self.flash = ""
        self.flash_after = None
        self.render_pill()

    def clear_problem(self, msg: str) -> None:
        self.problems = [p for p in self.problems if p["msg"] != msg]
        self.render_pill()
        if self.panel:
            self.build_panel()

    def set_alarm(self, msg) -> None:
        """The guard itself has failed. This one opens the panel, once."""
        self.problems = [p for p in self.problems if not p.get("alarm")]
        if msg:
            self.problems.insert(0, {"msg": msg, "level": "error", "alarm": True,
                                     "at": time.strftime("%H:%M")})
            if not self.visible:
                self.show_somewhere()
            if not self.blocked:
                self.blocked = True
                self.open_panel()
        else:
            self.blocked = False
        self.render_pill()
        if self.panel:
            self.build_panel()

    # ---- the panel
    def toggle_panel(self) -> None:
        self.close_panel() if self.panel else self.open_panel()

    def open_panel(self) -> None:
        if self.panel:
            return
        self.hide_pill_tip()
        self.panel = self._floating(self.EDGE)
        self.build_panel()
        self.panel.deiconify()
        # Measured only once it is on screen. A hidden window reports the size
        # tkinter guessed, not the size its contents need, which is why the
        # panel opened short and did not settle until something rebuilt it.
        self.place_panel()
        self.root.after_idle(self.place_panel)
        self.chev.configure(text="⌃")

    def close_panel(self) -> None:
        self.panel_at = None
        SHARED["panel_rect"] = None       # the hook stops looking at clicks
        if self.panel:
            self.panel.destroy()
            self.panel = None
            self.chev.configure(text="⌄")

    def on_panel_wheel(self, event) -> None:
        if not self.panel or not self.panel.winfo_exists():
            return
        step = -1 if getattr(event, "num", 0) == 4 else 1 if getattr(event, "num", 0) == 5 \
            else (-1 if getattr(event, "delta", 0) > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    def panel_placement(self, need: int, area):
        """Where the panel goes and how tall it may be, on the screen the pill
        is actually on.

        Whichever side of the pill has room takes it, preferring above so the
        panel does not cover the composer. When neither side fits the whole
        thing, the larger side is used and the contents scroll, rather than the
        panel running off the screen edge and being cut."""
        _left, area_top, _right, area_bottom = area
        top = self.win.winfo_y()
        bottom = top + self.PILL_H
        above = top - area_top - self.TIGHT - self.MARGIN
        below = area_bottom - bottom - self.TIGHT - self.MARGIN
        if need <= above:
            return need, top - need - self.TIGHT
        if need <= below:
            return need, bottom + self.TIGHT
        if above >= below:
            h = max(above, self.MIN_PANEL_H)
            return h, max(area_top + self.MARGIN, top - h - self.TIGHT)
        return max(below, self.MIN_PANEL_H), bottom + self.TIGHT

    def place_panel(self) -> None:
        if not self.panel or not self.panel.winfo_exists():
            return
        self.panel.update_idletasks()
        need = self.body.winfo_reqheight() + 2
        area = work_area(self.win)           # the screen the pill is on, not the primary one
        h, y = self.panel_placement(need, area)
        x = self.win.winfo_x() + self.pill_w - self.PANEL_W
        x = min(max(area[0] + self.MARGIN, x), area[2] - self.PANEL_W - self.MARGIN)
        scrolls = need > h
        if (self.PANEL_W, h, x, y, scrolls) == self.panel_at:
            return                       # nothing moved: do not redraw and flicker
        self.panel_at = (self.PANEL_W, h, x, y, scrolls)
        # The hook compares a click against this to decide whether it landed on
        # us. Published from here because here is where the geometry is decided.
        SHARED["panel_rect"] = (x, y, x + self.PANEL_W, y + h)
        self.panel.geometry(f"{self.PANEL_W}x{h}+{x}+{y}")
        self.panel.update_idletasks()
        self.canvas.configure(scrollregion=(0, 0, self.PANEL_W, need))
        if scrolls:
            self.vbar.grid()
        else:
            self.vbar.grid_remove()
            self.canvas.yview_moveto(0)

    def build_panel(self) -> None:
        if not self.panel:
            return
        for child in self.panel.winfo_children():
            child.destroy()
        # The canvas and the scrollbar below are new objects, so the remembered
        # geometry no longer describes them: without this the next placement
        # takes its early return and the scrollbar is never packed.
        self.panel_at = None
        outer = tk.Frame(self.panel, bg=self.BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        # A canvas holding the contents, so a panel taller than the space on
        # screen scrolls instead of being cut off by the screen edge.
        self.canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0, bd=0,
                                takefocus=0)
        self.vbar = tk.Scrollbar(outer, orient="vertical", width=10,
                                 command=self.canvas.yview, takefocus=0)
        self.canvas.configure(yscrollcommand=self.vbar.set)
        # Grid, not pack: the canvas expands to fill, and a scrollbar packed
        # after it gets whatever is left, which is nothing, so it never appears.
        # A grid column is reserved whether or not anything is in it.
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.vbar.grid_remove()
        body = tk.Frame(self.canvas, bg=self.BG)
        self.body_id = self.canvas.create_window((0, 0), window=body, anchor="nw")
        self.body = body
        # The inner frame has to follow the canvas's width or the contents keep
        # their natural width and the wrapping is wrong.
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self.body_id, width=e.width))
        # Bound to the panel itself rather than the whole application, and
        # rebound each time because the widgets are rebuilt: bind_all here would
        # stack a handler per rebuild and scroll by a multiple.
        for widget in (self.panel, self.canvas, body):
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(seq, self.on_panel_wheel)

        head = tk.Frame(body, bg=self.BG)
        head.pack(fill="x", padx=self.PAD, pady=(self.GAP, 0))
        tk.Label(head, text="SafePII", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 10)).pack(side="left")
        for text, cmd in (("✕", self.close_panel), ("⚙", self.open_settings)):
            tk.Label(head, text=text, bg=self.BG, fg=self.DIM, font=("Segoe UI", 10),
                     cursor="hand2", padx=6).pack(side="right")
            head.winfo_children()[-1].bind("<Button-1>", lambda e, c=cmd: c())
        tk.Label(body, text=self.summary(), bg=self.BG, fg=self.DIM, font=("Segoe UI", 8),
                 anchor="w").pack(fill="x", padx=self.PAD)

        for p in self.problems:
            self.problem_row(body, p)

        tk.Frame(body, bg=self.LINE, height=1).pack(fill="x", pady=(self.GAP, 0))
        for label, key, cmd in (("Guard", "guard", "toggle_guard"),
                                ("Unmask replies", "unmask", "toggle_unmask"),
                                ("Overlay", "overlay", "toggle_overlay"),
                                ("Files", "fileGuard", "toggle_file_guard")):
            self.toggle_row(body, label, bool(self.cfg.get(key, True)), cmd, locked(key))

        self.folder_rows(body)

        if self.recent:
            tk.Frame(body, bg=self.LINE, height=1).pack(fill="x", pady=(self.GAP, 0))
            tk.Label(body, text="Recent", bg=self.BG, fg=self.DIM, font=("Segoe UI", 8),
                     anchor="w").pack(fill="x", padx=self.PAD, pady=(self.TIGHT, 2))
            for when, level, msg in self.recent[:5]:
                row = tk.Frame(body, bg=self.BG)
                row.pack(fill="x", padx=self.PAD)
                tk.Label(row, text=when, bg=self.BG, fg=self.DIM, font=("Segoe UI", 8),
                         width=5, anchor="w").pack(side="left")
                tk.Label(row, text=msg, bg=self.BG,
                         fg={"error": self.ERR, "warn": self.WARN}.get(level, self.DIM),
                         font=("Segoe UI", 8), anchor="w", wraplength=self.PANEL_W - 80,
                         justify="left").pack(side="left", fill="x", expand=True)

        if self.update:
            # Quiet and persistent: neither a problem to acknowledge nor a
            # message that should scroll away.
            newer, url = self.update
            tk.Frame(body, bg=self.LINE, height=1).pack(fill="x", pady=(self.GAP, 0))
            row = tk.Frame(body, bg=self.BG)
            row.pack(fill="x", padx=self.PAD, pady=(self.TIGHT, 0))
            tk.Label(row, text=f"Version {newer} is available. You have {__version__}.",
                     bg=self.BG, fg=self.DIM, font=("Segoe UI", 8), anchor="w",
                     wraplength=self.PANEL_W - 2 * self.PAD, justify="left").pack(fill="x")
            if url:
                link = tk.Label(row, text="Download it", bg=self.BG, fg=self.ACCENT,
                                font=("Segoe UI", 8, "underline"), cursor="hand2", anchor="w")
                link.pack(anchor="w")
                link.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

        foot = tk.Frame(body, bg=self.BG)
        foot.pack(fill="x", padx=self.PAD, pady=self.GAP)
        mask = tk.Label(foot, text="Mask now", bg=self.ACCENT, fg="#0e1420", padx=14, pady=4,
                        font=("Segoe UI", 9, "bold"), cursor="hand2")
        mask.pack(side="left")
        mask.bind("<Button-1>", lambda e: self.commands.put(("mask",)))
        newsess = tk.Label(foot, text="New session", bg=self.BG, fg=self.DIM,
                           font=("Segoe UI", 9), cursor="hand2", padx=10)
        newsess.pack(side="left")
        newsess.bind("<Button-1>", lambda e: self.commands.put(("new_session",)))
        self.place_panel()

    def summary(self) -> str:
        n = len(self.problems)
        if n:
            return f"{n} thing{'' if n == 1 else 's'} to look at"
        if not self.cfg.get("guard", True):
            return "Guard is off — messages are not checked before they send"
        if POLICY:
            return f"Guard on. {len(POLICY)} setting(s) fixed by your administrator."
        return "Guard on. Enter is checked before it sends."

    def problem_row(self, parent, p) -> None:
        colour = self.ERR if p["level"] == "error" else self.WARN
        wrap = tk.Frame(parent, bg=self.BG)
        wrap.pack(fill="x", padx=self.PAD, pady=(self.GAP, 0))
        tk.Frame(wrap, bg=colour, width=3).pack(side="left", fill="y")
        inner = tk.Frame(wrap, bg=self.BG)
        inner.pack(side="left", fill="x", expand=True, padx=(self.TIGHT, 0))
        tk.Label(inner, text=p["msg"], bg=self.BG, fg=self.FG, font=("Segoe UI", 9),
                 wraplength=self.PANEL_W - 2 * self.PAD - 30, justify="left",
                 anchor="w").pack(fill="x")
        if not p.get("alarm"):
            # A blocking alert has no dismiss: it goes when the cause goes.
            got = tk.Label(inner, text="Got it", bg=self.BG, fg=self.DIM,
                           font=("Segoe UI", 8, "underline"), cursor="hand2", anchor="w")
            got.pack(anchor="w", pady=(2, 0))
            got.bind("<Button-1>", lambda e, m=p["msg"]: self.clear_problem(m))

    def folder_rows(self, body) -> None:
        """Sharing a folder with Claude, from the place the user already comes to
        for masking. Claude's own folder picker has nothing to offer once the
        administrator has emptied `allowedWorkspaceFolders`; this replaces it, and
        unlike that picker it can say what will be served before it is."""
        if broker_mod is None or not self.cfg.get("sharing", True):
            return
        tk.Frame(body, bg=self.LINE, height=1).pack(fill="x", pady=(self.GAP, 0))
        name, served = self.sharing
        if name:
            row = tk.Frame(body, bg=self.BG, height=self.ROW_H)
            row.pack(fill="x", padx=self.PAD, pady=1)
            row.pack_propagate(False)
            tk.Label(row, text=f"Sharing {name}", bg=self.BG, fg=self.FG,
                     font=("Segoe UI", 9), anchor="w").pack(side="left")
            stop = tk.Label(row, text="Stop", bg=self.BG, fg=self.DIM,
                            font=("Segoe UI", 9, "underline"), cursor="hand2", anchor="e")
            stop.pack(side="right")
            stop.bind("<Button-1>", lambda e: self.commands.put(("stop_sharing",)))
            tk.Label(body, text=f"{served} file(s) masked as Claude reads them.",
                     bg=self.BG, fg=self.DIM, font=("Segoe UI", 8), anchor="w",
                     wraplength=self.PANEL_W - 2 * self.PAD,
                     justify="left").pack(fill="x", padx=self.PAD)
            return
        row = tk.Frame(body, bg=self.BG, height=self.ROW_H, cursor="hand2")
        row.pack(fill="x", padx=self.PAD, pady=1)
        row.pack_propagate(False)
        tk.Label(row, text="Share a folder with Claude…", bg=self.BG, fg=self.FG,
                 font=("Segoe UI", 9), anchor="w").pack(side="left")
        for w in (row,) + tuple(row.winfo_children()):
            w.bind("<Button-1>", lambda e: self.pick_folder())
        tk.Label(body, text="Claude can ask for one too, and this is what it opens.",
                 bg=self.BG, fg=self.DIM, font=("Segoe UI", 8), anchor="w",
                 wraplength=self.PANEL_W - 2 * self.PAD,
                 justify="left").pack(fill="x", padx=self.PAD)

    def pick_folder(self, reason: str = "", asked: bool = False) -> None:
        """Pick, then read what would happen, then decide. One folder at a time:
        the panel shows what is being served and nothing is guessed at.

        `asked` means Claude is waiting on the other end, so every way out of
        here has to tell it something -- a tool call left hanging is worse than
        one that says no.
        """
        def no(said: str) -> None:
            if asked:
                self.commands.put(("folder_declined", said))

        try:
            from tkinter import filedialog
            title = "Share a folder with Claude"
            if asked:
                self.alert(f"Claude is asking for a folder"
                           + (f": {reason}" if reason else "") + ".", "info")
            chosen = filedialog.askdirectory(parent=self.root, mustexist=True, title=title)
        except Exception as e:  # noqa: BLE001
            log(f"sharing: the folder picker failed: {e}")
            no("SafePII could not open a folder picker on this machine.")
            return
        if not chosen:
            no("The person closed the folder picker without choosing one.")
            return
        counts = broker_mod.preview(chosen, bool(self.cfg.get("shareAllowCode", False)))
        if counts.get("error"):
            self.alert(counts["error"], "warn")
            no(counts["error"])
            return
        if not counts["served"]:
            said = (f"Nothing in that folder can be masked, so there is nothing "
                    f"to share. {broker_mod.describe(counts)}")
            self.alert(said, "warn")
            no(said)
            return
        self.confirm_share(chosen, counts, asked=asked)

    def confirm_share(self, path: str, counts: dict, asked: bool = False) -> None:
        """What will be served, before it is. The same shape as ask_restore:
        consent is asked once, in words, with the count in it."""
        w = tk.Toplevel(self.root)
        w.title("SafePII")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.configure(bg=self.BG)
        tk.Label(w, text=f"Share {ntpath.basename(path.rstrip('/' + chr(92))) or path}?",
                 bg=self.BG, fg=self.FG, font=("Segoe UI Semibold", 10),
                 anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        tk.Label(w, text=broker_mod.describe(counts), bg=self.BG, fg=self.DIM,
                 font=("Segoe UI", 9), wraplength=380, justify="left",
                 anchor="w").pack(fill="x", padx=16)
        tk.Label(w, text="Claude never sees the folder itself, only what SafePII hands it. "
                         "Anything it writes back lands beside the folder, not in it.",
                 bg=self.BG, fg=self.DIM, font=("Segoe UI", 8), wraplength=380,
                 justify="left", anchor="w").pack(fill="x", padx=16, pady=(6, 0))
        btns = tk.Frame(w, bg=self.BG)
        btns.pack(fill="x", padx=16, pady=14)
        share = tk.Label(btns, text="Share it", bg=self.ACCENT, fg="#0e1420", padx=14, pady=4,
                         font=("Segoe UI", 9, "bold"), cursor="hand2")
        share.pack(side="left")
        share.bind("<Button-1>", lambda e: (w.destroy(),
                                            self.commands.put(("share_folder", path))))
        cancel = tk.Label(btns, text="Cancel", bg=self.BG, fg=self.DIM,
                          font=("Segoe UI", 9), cursor="hand2", padx=10)
        cancel.pack(side="left")
        cancel.bind("<Button-1>", lambda e: (
            w.destroy(),
            self.commands.put(("folder_declined", "The person decided not to share it."))
            if asked else None))

    def toggle_row(self, parent, label: str, on: bool, cmd: str, fixed: bool = False) -> None:
        row = tk.Frame(parent, bg=self.BG, height=self.ROW_H,
                       cursor="arrow" if fixed else "hand2")
        row.pack(fill="x", padx=self.PAD, pady=1)
        row.pack_propagate(False)
        tk.Label(row, text=label, bg=self.BG, fg=self.DIM if fixed else self.FG,
                 font=("Segoe UI", 9), anchor="w").pack(side="left")
        # The word, not only the colour, says which way it is set.
        tk.Label(row, text=("🔒 " if fixed else "") + ("on" if on else "off"), bg=self.BG,
                 fg=self.DIM if fixed else (self.OK if on else self.DIM),
                 font=("Segoe UI", 9, "bold"), anchor="e").pack(side="right")
        if fixed:
            return               # set by an administrator: not ours to change
        for w in (row,) + tuple(row.winfo_children()):
            w.bind("<Button-1>", lambda e, c=cmd: self.commands.put((c,)))

    def ask_restore(self, name: str, path: str) -> None:
        """Consent before a file leaves the machine. SafePII cannot see inside a
        zipped Office file, so it cannot know whether this one came from Claude
        or from the user's email."""
        w = tk.Toplevel(self.root)
        w.title("SafePII")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        w.configure(bg=self.BG)
        tk.Label(w, text=f"{name} may hold masked values.", bg=self.BG, fg=self.FG,
                 font=("Segoe UI Semibold", 10), anchor="w").pack(
            fill="x", padx=self.PAD, pady=(self.PAD, 2))
        tk.Label(w, text="SafePII cannot see inside this file without sending it to your\n"
                         "SafePII server. Send it, so any tokens in it can be restored?",
                 bg=self.BG, fg=self.DIM, font=("Segoe UI", 9),
                 justify="left", anchor="w").pack(fill="x", padx=self.PAD, pady=(0, self.GAP))
        row = tk.Frame(w, bg=self.BG)
        row.pack(fill="x", padx=self.PAD, pady=(0, self.PAD))

        def answer(value):
            w.destroy()
            self.commands.put(("restore_answer", path, value))

        for text, value, primary in (("Send", "yes", True), ("Not this one", "no", False),
                                     ("Always", "always", False), ("Never ask", "never", False)):
            b = tk.Label(row, text=text, bg=self.ACCENT if primary else "#2a3346",
                         fg="#0e1420" if primary else self.FG, padx=12, pady=4,
                         font=("Segoe UI", 9, "bold" if primary else "normal"), cursor="hand2")
            b.pack(side="left", padx=(0, self.TIGHT))
            b.bind("<Button-1>", lambda e, v=value: answer(v))
        w.protocol("WM_DELETE_WINDOW", lambda: answer("no"))

    # ---- the hover tooltip over Claude's own text
    def show_tip(self, text, x: int, y: int) -> None:
        if not text:
            self.tip.withdraw()
            return
        self.tip_label.configure(text=text)
        self.tip.update_idletasks()
        w, h = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        # Clamped to the screen this is appearing on, not the primary one, or a
        # tooltip over Claude on a second monitor jumps back to the first.
        left, top, right, bottom = work_area(self.tip)
        x = max(left, min(x, right - w))
        if y + h > bottom:
            y = max(top, y - h - 30)
        self.tip.geometry(f"+{x}+{y}")
        self.tip.deiconify()

    def toast(self, msg: str, error: bool = False) -> None:
        self.alert(msg, "error" if error else "info")

    def pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                t = ev["type"]
                if t == "composer":
                    self.place(ev["rect"]) if ev["visible"] else self.hide()
                elif t == "alert":
                    self.alert(ev["msg"], ev.get("level", "info"))
                elif t == "toast":
                    self.toast(ev["msg"], ev.get("error", False))
                elif t == "busy":
                    self.busy = ev["busy"]
                    self.render_pill()
                elif t == "check" and self.settings_win and self.settings_win.winfo_exists():
                    self.check_label.configure(text=ev["msg"], fg="#065f46" if ev["ok"] else "#991b1b")
                elif t == "auth":
                    self.alert(ev["msg"], "info" if ev["ok"] else "error")
                    if self.settings_win and self.settings_win.winfo_exists():
                        self.check_label.configure(text=ev["msg"], fg="#065f46" if ev["ok"] else "#991b1b")
                elif t == "session":
                    self.alert(f"New session for {ev.get('title') or 'this chat'}", "info")
                elif t == "sharing":
                    self.sharing = (ev.get("name") or "", ev.get("served") or 0)
                    self.render_pill()
                    if self.panel:
                        self.build_panel()
                elif t in ("guard", "unmask", "overlay_state", "file_guard"):
                    # The toggles live in the panel and the hover tooltip now,
                    # both of which read cfg, so one redraw covers all four.
                    self.render_pill()
                    if self.panel:
                        self.build_panel()
                elif t == "overlay":
                    self.overlay.render(ev["items"], ev.get("win"))
                elif t == "update":
                    self.update = (ev["version"], ev.get("url") or "")
                    if self.panel:
                        self.build_panel()
                elif t == "ask_restore":
                    self.ask_restore(ev["name"], ev["path"])
                elif t == "alarm":
                    self.set_alarm(ev["msg"])
                elif t == "alarm_clear":
                    self.set_alarm(None)
                elif t == "block_drop":
                    self.blocker.show(ev["rect"]) if ev["rect"] else self.blocker.hide()
                elif t == "collapse":
                    # A click landed outside the bar. A blocking alert is the one
                    # thing that keeps the panel open: it is not dismissed by
                    # being clicked away from.
                    if not any(p.get("alarm") for p in self.problems):
                        self.close_panel()
                elif t == "tip":
                    self.show_tip(ev["text"], ev["x"], ev["y"])
        except queue.Empty:
            pass
        self.check_folder_request()
        self.root.after(15, self.pump)

    def check_folder_request(self) -> None:
        """Claude has called request_folder and its tool call is waiting.

        Read from SHARED rather than the event queue because the picker is modal
        and the queue keeps filling while it is open: a second event behind this
        one would raise a second picker over the first.
        """
        want = SHARED.get("want_folder")
        if not want or self.asking:
            return
        self.asking = True
        try:
            self.pick_folder(reason="" if want is True else str(want), asked=True)
        finally:
            self.asking = False

    # ---- settings
    def open_settings(self) -> None:
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        w = self.settings_win = tk.Toplevel(self.root)
        w.title(f"{APP_NAME} helper settings")
        w.resizable(False, False)
        w.attributes("-topmost", True)
        pad = {"padx": 10, "pady": 4}
        tk.Label(w, text="Server URL" + (" 🔒" if locked("serverUrl") else "")).grid(
            row=0, column=0, sticky="w", **pad)
        url = tk.Entry(w, width=48)
        url.insert(0, self.cfg.get("serverUrl") or DEFAULTS["serverUrl"])
        url.grid(row=0, column=1, **pad)
        if locked("serverUrl"):
            url.configure(state="readonly")
        tk.Label(w, text="Service key (optional; mr_…\nor the legacy shared key)").grid(row=1, column=0, sticky="w", **pad)
        key = tk.Entry(w, width=48, show="•")
        key.insert(0, self.cfg.get("apiKey") or "")
        key.grid(row=1, column=1, **pad)
        pre = tk.BooleanVar(value=self.cfg.get("preamble", True))
        tk.Checkbutton(w, text="Prefix the first masked message of a session with the token preamble",
                       variable=pre).grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        hide_scroll = tk.BooleanVar(value=self.cfg.get("overlayHideOnScroll", False))
        tk.Checkbutton(w, text="Hide the overlay while scrolling (instead of following the scroll)",
                       variable=hide_scroll).grid(row=6, column=0, columnspan=2, sticky="w", **pad)
        watch_dl = tk.BooleanVar(value=self.cfg.get("watchDownloads", True))
        tk.Checkbutton(w, text="Restore tokens in files Claude produces, as they land in Downloads",
                       variable=watch_dl).grid(row=9, column=0, columnspan=2, sticky="w", **pad)
        tk.Label(w, text="Files whose tokens SafePII cannot see\nwithout sending them").grid(
            row=11, column=0, sticky="w", **pad)
        unreadable = tk.StringVar(value=self.cfg.get("restoreUnreadable", "ask"))
        row_u = tk.Frame(w)
        row_u.grid(row=11, column=1, sticky="w", **pad)
        for text, value in (("Ask me", "ask"), ("Always send", "always"), ("Never send", "never")):
            tk.Radiobutton(row_u, text=text, variable=unreadable, value=value).pack(side="left")
        del_tok = tk.BooleanVar(value=self.cfg.get("deleteTokenCopy", False))
        tk.Checkbutton(w, text="…and delete the token copy afterwards (off: both files are kept)",
                       variable=del_tok).grid(row=10, column=0, columnspan=2, sticky="w", **pad)
        ov_debug = tk.BooleanVar(value=self.cfg.get("overlayDebug", False))
        tk.Checkbutton(w, text="Log every line the overlay walks (diagnostics; noisy)",
                       variable=ov_debug).grid(row=8, column=0, columnspan=2, sticky="w", **pad)
        block_drops = tk.BooleanVar(value=self.cfg.get("blockDrops", True))
        tk.Checkbutton(w, text="Refuse files dropped from Explorer (they cannot be masked in flight; "
                               "the paperclip can)", variable=block_drops).grid(
            row=7, column=0, columnspan=2, sticky="w", **pad)
        self.check_label = tk.Label(w, text="", anchor="w")
        self.check_label.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.commands.put(("check",))   # show who we are, if anyone
        tk.Label(w, text=f"SafePII helper {__version__}").grid(row=12, column=0, columnspan=2,
                                                               sticky="w", **pad)
        tk.Label(w, text=f"Sessions: {len(self.cfg.get('sessions') or {})} known, "
                         f"{len(self.cfg.get('chats') or {})} chats bound; current {(self.cfg.get('sessionId') or 'none')[:8]}    "
                         f"Hotkeys: Ctrl+Shift+M mask, Ctrl+Shift+U unmask clipboard\n"
                         f"Config: {CONFIG_FILE}", justify="left", fg="#6b7280").grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        btns = tk.Frame(w)
        btns.grid(row=12, column=0, columnspan=2, sticky="e", **pad)

        def apply():
            # A value an administrator has fixed is never taken from this window.
            if not locked("serverUrl"):
                self.cfg["serverUrl"] = url.get().strip() or DEFAULTS["serverUrl"]
            self.cfg["apiKey"] = key.get().strip()
            for name, var in (("preamble", pre), ("overlayHideOnScroll", hide_scroll),
                              ("blockDrops", block_drops), ("overlayDebug", ov_debug),
                              ("watchDownloads", watch_dl), ("deleteTokenCopy", del_tok)):
                if not locked(name):
                    self.cfg[name] = bool(var.get())
            if not locked("restoreUnreadable"):
                self.cfg["restoreUnreadable"] = unreadable.get()
            save_config(self.cfg)

        def test():
            apply()
            self.check_label.configure(text="Testing…", fg="#374151")
            self.commands.put(("check",))

        def sign_in():
            apply()
            self.check_label.configure(text="Opening the browser…", fg="#374151")
            SignIn(self.cfg, self.events).start()

        tk.Button(btns, text="Sign in", command=sign_in).pack(side="left", padx=4)
        tk.Button(btns, text="Sign out", command=lambda: (apply(), self.commands.put(("sign_out",)))).pack(side="left", padx=4)
        tk.Button(btns, text="Test connection", command=test).pack(side="left", padx=4)
        tk.Button(btns, text="Save", command=lambda: (apply(), w.destroy())).pack(side="left", padx=4)
        tk.Button(btns, text="Quit helper", command=self.root.destroy).pack(side="left", padx=4)

    def run(self) -> None:
        self.root.mainloop()


# ----------------------------------------------------------------------------- main
def main() -> int:
    # Claude Desktop starts this process itself when the broker is registered as
    # a stdio server, and hands it --stdio --bridge. A packaged build has no
    # separate broker.py to point at -- there is one executable -- so the same
    # one answers to both, and nothing but JSON-RPC may reach stdout.
    if "--stdio" in sys.argv[1:]:
        if broker_mod is None:
            print("This build has no file broker.", file=sys.stderr)
            return 1
        return broker_mod.main(sys.argv[1:])
    if not sys.platform.startswith("win"):
        print("This prototype is Windows-only (UI Automation). See docs/DESKTOP_APP_RESEARCH.md for macOS.")
        return 1
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # UIA rectangles are physical pixels
    except Exception:  # noqa: BLE001
        pass
    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    log(f"start: SafePII helper {__version__} "
        f"server={cfg.get('serverUrl')} guard={cfg.get('guard', True)} log={LOG_FILE}"
        + (f" mask={len(args)} file(s)" if args else ""))
    commands: "queue.Queue[tuple]" = queue.Queue()
    events: "queue.Queue[dict]" = queue.Queue()
    def supervise():
        """Restart the worker if it ever stops. It is written not to, but the
        guard depends on it and a silent death takes the Enter key with it."""
        worker = Automation(cfg, commands, events)
        worker.start()
        while True:
            time.sleep(2.0)
            if worker.is_alive():
                continue
            log("supervisor: the worker thread died; restarting it")
            events.put({"type": "alarm", "msg": "SafePII restarted its guard. "
                                                "Check the last message you sent was masked."})
            worker = Automation(cfg, commands, events)
            worker.start()
            commands.put(("load_vault",))

    threading.Thread(target=supervise, name="supervisor", daemon=True).start()
    Hotkeys(cfg, commands, events).start()
    OverlayWorker(cfg, events).start()
    commands.put(("load_vault",))
    if args:
        commands.put(("mask_files", args))
    Bar(cfg, commands, events).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
