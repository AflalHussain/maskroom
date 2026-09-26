"""Maskroom desktop helper (Windows prototype).

Sits beside Claude Desktop the way the Chrome extension sits inside claude.ai:
when the composer has focus, a small floating bar appears above it with a Mask
button. Mask (button, or Ctrl+Shift+M anywhere) reads the composer through UI
Automation, sends the text to the Maskroom server (POST /api/mask), and writes
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
(POST /api/process), the masked copy is written to the Maskroom files folder,
its path is typed into the dialog's File name box and the confirm is
replayed, so Claude attaches the masked copy and never sees the original.
A type Maskroom cannot mask (an image, an archive) is attached as it is with
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
call per token, which is how scrolling is followed), and the page text is
re-read twice a second to catch streaming. Each token's font family, size,
weight, italic and colour are read from the text range's attributes so the
patch is drawn in the same style (a web font that is not installed falls
back to Segoe UI). The composer is never overlaid. Scrolling: a mouse
hook notes wheel events over Claude; while the page scrolls, one anchor
token's rectangle is read every 20 ms (a single call) and every patch is
translated by that delta on the canvas, then, once the scroll settles, all
rectangles are refreshed exactly. Set "overlayHideOnScroll" in the config
(or the settings checkbox) to hide the overlay during scrolls instead.
If it does not look right, switch it off on the bar; hover and copy remain.

Run:   py -m pip install uiautomation
       py desktop\\helper.py

Sessions follow the chat, as the extension's do. The chat is identified by
the page URL that Chromium exposes as the document's value (claude.ai/chat/
<id>), else by the tokens visible on the page (every token id is random, so
a token names its session), else by the chat title. Each chat gets its own
Maskroom session (vault); switching chats switches the session used for
masking. Restore (hover, copy, overlay) searches every known vault at once,
so it never depends on which chat is current.

Sign-in: the gear button's "Sign in" opens the server's login page in the
system browser. The server sends the browser through the identity provider and
finally to http://127.0.0.1:<port>/done on this machine with a one-time code,
which the helper exchanges (POST /auth/exchange) for a session token it then
sends as `Authorization: Bearer`. A service key (mr_...) pasted into settings
works too, for servers without sign-on or for shared machines.

Config lives in %APPDATA%\\Maskroom\\helper.json (server URL, token, session
id). An administrator can pre-set the server URL for every user of a machine
in %ProgramData%\\Maskroom\\helper.json (read first, like the extension's
managed serverUrl key).

Threads: UI Automation and the server calls run on one worker thread (COM is
apartment-bound); the global hotkeys run on their own thread because
RegisterHotKey delivers to the registering thread; tkinter owns the main thread
and only ever reads a queue. Nothing here presses Enter in Claude.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
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

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    print("Missing dependency. Run:  py -m pip install uiautomation")
    sys.exit(1)

APP_NAME = "Maskroom"
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
    "fileGuard": True,     # intercept the file dialog / paste / drop
    "blockDrops": True,    # refuse Explorer drops on Claude (they cannot be masked in flight)
    "filesDir": "",        # where masked copies are written (default: <config>/files)
    "preambleSent": [],
}
OVERLAY_TEXT_S = 0.5       # how often the page text is re-read for new tokens
SCROLL_TICK_S = 0.02       # anchor poll while scrolling
SCROLL_SETTLE_S = 0.15     # no movement for this long = scroll over
SHARED = {"scroll_at": 0.0, "dialog_open": False, "drag_at": 0.0, "blocking": False,
          "blocking_since": 0.0}
OVERLAY_MAX_TOKENS = 80
MAX_KNOWN_SESSIONS = 25    # vaults kept locally for restore
MASK_EXTS = (".xlsx", ".xlsm", ".pdf", ".docx", ".pptx", ".csv", ".tsv", ".txt", ".json")
MAX_UPLOAD = 25 * 1024 * 1024
DIALOG_POLL_S = 0.4
TIP_MAX_CHARS = 400
SIGNIN_TIMEOUT_S = 300
CLAUDE_EXE = "claude.exe"
COMPOSER_CLASS_HINT = "ProseMirror"
POLL_S = 0.12
HIDE_GRACE_S = 0.8

# ----------------------------------------------------------------------------- config
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for path in (MACHINE_CONFIG, CONFIG_FILE):   # machine-wide defaults, then the user's own
        try:
            data = json.loads(path.read_text("utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError):
            pass
    cfg["chats"] = dict(cfg.get("chats") or {})
    cfg["sessions"] = dict(cfg.get("sessions") or {})
    sid = cfg.get("sessionId")
    if sid and sid not in cfg["sessions"]:          # a session from before chats were tracked
        cfg["sessions"][sid] = {"title": None, "chat": None, "last": time.time()}
    return cfg


LOG_FILE = CONFIG_DIR / "helper.log"
_log_lock = threading.Lock()


def log(msg: str) -> None:
    """Diagnostic trail (guard decisions, hook status). Paste it back when something misbehaves."""
    line = f"{time.strftime('%H:%M:%S')} [{threading.current_thread().name}] {msg}\n"
    try:
        with _log_lock:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError:
        pass


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


# ----------------------------------------------------------------------------- server
class Server:
    """The same contract extension/background.js uses, over urllib."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _request(self, path: str, method: str, data, headers: dict):
        url = self.cfg["serverUrl"].rstrip("/") + path
        headers = {"X-Requested-With": "maskroom", "Accept": "application/json", **headers}
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
                    "error": f"Cannot reach Maskroom at {self.cfg['serverUrl']} ({getattr(e, 'reason', e)})."}

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
        headers = {"X-Requested-With": "maskroom", "Accept": "application/json"}
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
                    "error": f"Cannot reach Maskroom at {self.cfg['serverUrl']} ({reason}). Is the server running?"}


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
_exe_cache: dict[int, str] = {}


def process_exe(pid: int) -> str:
    """Lower-cased executable name for a pid ('' when unreadable)."""
    if pid in _exe_cache:
        return _exe_cache[pid]
    name = ""
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            size = wt.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                name = os.path.basename(buf.value).lower()
        finally:
            _kernel32.CloseHandle(h)
    _exe_cache[pid] = name
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
        self.ov_text = None           # page text at the last token scan
        self.ov_text_at = 0.0
        self.ov_items: list[dict] = []   # {"range", "value", "rect", "bg"}
        self.ov_last_frame = None     # what was last sent to the UI
        self.ov_visible = False
        self.ov_win = None            # window rect of the last frame
        self.scrolling = False
        self.scroll_moved_at = 0.0
        self.anchor = None            # item whose rect is polled while scrolling
        self.files = FileApi(self.server)
        self.dialog = None            # the open file dialog, while Claude has one
        self.dialog_seen = 0.0
        self.masked_paths: set[str] = set()   # our own outputs: never re-masked

    # ---- plumbing
    def emit(self, **ev) -> None:
        self.events.put(ev)

    def toast(self, msg: str, error: bool = False) -> None:
        self.emit(type="toast", msg=msg, error=error)

    def run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            while True:
                try:
                    cmd = self.commands.get(timeout=SCROLL_TICK_S if self.scrolling else POLL_S)
                except queue.Empty:
                    if (self.cfg.get("overlay", True) and self.ov_visible) or self.scrolling:
                        try:
                            if self.track_scroll():
                                continue          # mid-scroll: nothing else this tick
                        except Exception as e:  # noqa: BLE001
                            log(f"scroll track error {type(e).__name__}: {e}")
                            self.scrolling = False
                    self.poll_focus()
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
                    if self.cfg.get("overlay", True):
                        try:
                            self.poll_overlay()
                        except Exception as e:  # noqa: BLE001
                            log(f"overlay poll error {type(e).__name__}: {e}")
                            self.hide_overlay()
                    continue
                try:
                    getattr(self, "cmd_" + cmd[0])(*cmd[1:])
                except Exception as e:  # noqa: BLE001 - keep the worker alive
                    self.toast(f"{type(e).__name__}: {e}", error=True)

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
                r = ctrl.BoundingRectangle
            except Exception:  # noqa: BLE001
                return
            if self.composer is None:
                log(f"composer focused: class={ctrl.ClassName!r} pid={ctrl.ProcessId}")
            self.composer = ctrl
            self.last_seen = time.time()
            self.emit(type="composer", visible=True, rect=(r.left, r.top, r.right, r.bottom))
        elif time.time() - self.last_seen > HIDE_GRACE_S:
            if self.composer is not None:
                try:
                    where = f"{ctrl.ControlTypeName} class={ctrl.ClassName!r} pid={ctrl.ProcessId}" if ctrl else "none"
                except Exception:  # noqa: BLE001
                    where = "?"
                log(f"composer lost; focus now: {where}")
            self.composer = None
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
        self.token_owner = owner
        log(f"vault index: {len(union)} tokens across {len(self.vaults)} sessions")

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
        if self.ov_text is not None and time.time() - self.ov_text_at < 1.0:
            return self.ov_text
        doc = self.page_document()
        try:
            return doc.DocumentRange.GetText(-1) or "" if doc else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- hover tooltip
    def page_document(self):
        """Claude's page document element (the one with the most text), cached."""
        now = time.time()
        if self.doc is not None or now - self.doc_checked < 2.0:
            return self.doc
        self.doc_checked = now
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
        self.doc = best
        self.doc_ctrl = best_ctrl
        self.claude_win = best_win if best is not None else None
        if best is not None:
            url, title = self.chat_identity()
            log(f"page document found ({best_n} chars) url={url!r} title={title!r}")
        else:
            log("page document not found")
        return best

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

    # ---- overlay
    def hide_overlay(self) -> None:
        if self.ov_visible:
            self.ov_visible = False
            self.ov_last_frame = None
            self.emit(type="overlay", items=None)

    @staticmethod
    def range_style(rng) -> dict:
        """Font family / size (pt) / weight / italic / foreground of a text range,
        from UIA text attributes. Missing or mixed values are left out."""
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
            if isinstance(v, types) and not (key == "fg" and isinstance(v, bool)):
                style[key] = v
        if "fg" in style:
            c = int(style["fg"])
            style["fg"] = f"#{c & 0xFF:02x}{(c >> 8) & 0xFF:02x}{(c >> 16) & 0xFF:02x}"
        return style

    def scan_tokens(self, doc, text: str) -> None:
        """Locate every known token in the page text by character offset: one
        endpoint move per token, verified by reading the range back."""
        items = []
        comp = None
        if self.composer is not None:
            try:
                comp = self.composer.BoundingRectangle
            except Exception:  # noqa: BLE001
                comp = None
        t0 = time.perf_counter()
        base = doc.DocumentRange
        for m in list(EXACT_RE.finditer(text))[:OVERLAY_MAX_TOKENS]:
            tok = m.group(0)
            value = self.index.vault.get(tok)
            if value is None:
                continue
            rng = base.Clone()
            moved = rng.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start, auto.TextUnit.Character, m.start(), waitTime=0)
            rng.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, rng, auto.TextPatternRangeEndpoint.Start, waitTime=0)
            rng.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End, auto.TextUnit.Character, len(tok), waitTime=0)
            got = (rng.GetText(-1) or "")
            if got.upper() != tok.upper():
                # Offsets drifted (embedded objects / line breaks count differently): nudge a few chars.
                fixed = False
                for d in (-1, 1, -2, 2, -3, 3):
                    r2 = base.Clone()
                    r2.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.Start, auto.TextUnit.Character, m.start() + d, waitTime=0)
                    r2.MoveEndpointByRange(auto.TextPatternRangeEndpoint.End, r2, auto.TextPatternRangeEndpoint.Start, waitTime=0)
                    r2.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End, auto.TextUnit.Character, len(tok), waitTime=0)
                    if (r2.GetText(-1) or "").upper() == tok.upper():
                        rng, fixed = r2, True
                        break
                if not fixed:
                    log(f"overlay: could not place {tok} (got {got!r} at {m.start()}, moved {moved})")
                    continue
            style = self.range_style(rng)
            items.append({"range": rng, "value": value, "rect": None, "bg": None, "comp": comp, "style": style})
        self.ov_items = items
        sample = items[0]["style"] if items else {}
        log(f"overlay: scanned {len(items)} tokens in {(time.perf_counter() - t0) * 1000:.0f} ms; style {sample}")

    def poll_overlay(self) -> None:
        if foreground_exe() != CLAUDE_EXE or not self.index.vault:
            self.hide_overlay()
            return
        doc = self.page_document()
        if doc is None or self.claude_win is None:
            self.hide_overlay()
            return
        now = time.time()
        if now - self.ov_text_at >= OVERLAY_TEXT_S:
            self.ov_text_at = now
            try:
                text = doc.DocumentRange.GetText(-1) or ""
            except Exception:  # noqa: BLE001
                self.doc = None
                self.hide_overlay()
                return
            if text != self.ov_text:
                self.ov_text = text
                self.scan_tokens(doc, text)
        if not self.ov_items:
            self.hide_overlay()
            return
        try:
            win = self.claude_win.BoundingRectangle
        except Exception:  # noqa: BLE001
            self.doc = None
            self.hide_overlay()
            return
        frame = []
        for it in self.ov_items:
            try:
                rects = it["range"].GetBoundingRectangles()
            except Exception:  # noqa: BLE001
                rects = []
            it["on_screen"] = False
            if not rects:
                it["rect"] = None
                continue
            r = rects[0]
            rect = (r.left, r.top, r.right, r.bottom)
            if r.right - r.left < 8 or r.bottom - r.top < 6:
                continue
            if r.top < win.top or r.bottom > win.bottom:
                continue                              # outside the window
            it["on_screen"] = True
            comp = it["comp"]
            if comp is not None and not (r.bottom < comp.top or r.top > comp.bottom):
                continue                              # never overlay the composer
            if it["rect"] != rect or it["bg"] is None:
                it["rect"] = rect
                it["bg"] = screen_pixel(r.left - 2, (r.top + r.bottom) // 2) or it["bg"] or "#ffffff"
            frame.append((rect, it["value"], it["bg"], it["style"]))
        self.ov_win = (win.left, win.top, win.right, win.bottom)
        if frame != self.ov_last_frame:
            self.ov_last_frame = frame
            self.ov_visible = bool(frame)
            self.emit(type="overlay", items=frame or None, win=self.ov_win)

    def pick_anchor(self):
        for it in self.ov_items:
            if it.get("rect") and it.get("on_screen"):
                return it
        return None

    def track_scroll(self) -> bool:
        """Follow a scroll with one UIA call per tick. Returns True while a
        scroll is in progress (the caller then skips the slower polls)."""
        now = time.time()
        wheel = now - SHARED["scroll_at"] < 0.4
        if not self.scrolling:
            if not wheel:
                return False
            self.scrolling = True
            self.scroll_moved_at = now
            self.anchor = self.pick_anchor()
            self.set_tip(None)
            if self.cfg.get("overlayHideOnScroll"):
                self.emit(type="overlay", items=None)
            log("scroll: start")
        if self.anchor is None:
            self.anchor = self.pick_anchor()
        if self.anchor is not None and not self.cfg.get("overlayHideOnScroll"):
            try:
                rects = self.anchor["range"].GetBoundingRectangles()
            except Exception:  # noqa: BLE001
                rects = []
            if rects:
                r = rects[0]
                ol, ot, _, _ = self.anchor["rect"]
                dx, dy = r.left - ol, r.top - ot
                if dx or dy:
                    for it in self.ov_items:
                        if it.get("rect"):
                            l, t, rr, b = it["rect"]
                            it["rect"] = (l + dx, t + dy, rr + dx, b + dy)
                    self.emit(type="overlay_shift", dx=dx, dy=dy)
                    self.scroll_moved_at = now
            else:
                self.anchor = None                    # scrolled off screen: pick another next tick
        if now - self.scroll_moved_at > SCROLL_SETTLE_S and not wheel:
            self.scrolling = False
            self.anchor = None
            self.ov_last_frame = None                 # force an exact refresh
            log("scroll: settled")
            self.poll_overlay()
            return False
        return True

    def cmd_toggle_overlay(self) -> None:
        self.cfg["overlay"] = not self.cfg.get("overlay", True)
        save_config(self.cfg)
        self.hide_overlay()
        self.emit(type="overlay_state", on=self.cfg["overlay"])
        self.toast("Overlay on: real values are painted over tokens." if self.cfg["overlay"]
                   else "Overlay off.")

    def cmd_toggle_unmask(self) -> None:
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
            log(f"session {sid[:8]} for chat url={url!r} title={title!r} ({how})")
            self.emit(type="session", id=sid, title=title)
        self.bind_session(sid, url, title)
        return sid

    def cmd_new_session(self) -> None:
        """A fresh vault for the chat on screen, replacing whatever it was bound to."""
        url, title = self.chat_identity()
        sid = self.create_session()
        self.vaults[sid] = {}
        self.bind_session(sid, url, title)
        self.rebuild_index()
        self.toast(f"New session {sid[:8]}… for {title or 'this chat'}")

    def cmd_sign_out(self) -> None:
        r = self.server.api("/auth/logout", "POST", {})
        self.cfg["token"] = ""
        save_config(self.cfg)
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
                self.toast(f"{n} value{'' if n == 1 else 's'} masked ({how}). Review, then press send.")
            else:
                self.toast("No PII detected — safe to send.")
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
          "unmasked"  Maskroom cannot mask this type (an image, an archive…), so
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
            log(f"file guard: {msg}")
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

    # ---- the Windows file dialog Claude opens for its paperclip
    def find_dialog(self):
        """Claude's open-file dialog, or None. Identified by class #32770 owned
        by Claude.exe with a File name edit box."""
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.ClassName != "#32770" or process_exe(w.ProcessId) != CLAUDE_EXE:
                    continue
            except Exception:  # noqa: BLE001
                continue
            return w
        return None

    @staticmethod
    def dialog_parts(dlg):
        """(file-name edit, confirm button) of a Windows common file dialog."""
        edit = confirm = None
        for c in dlg.GetChildren():
            try:
                t, name = c.ControlTypeName, (c.Name or "")
            except Exception:  # noqa: BLE001
                continue
            if edit is None and t == "ComboBoxControl":
                for k in c.GetChildren():           # the editable part of the combo
                    try:
                        if k.ControlTypeName == "EditControl":
                            edit = k
                            break
                    except Exception:  # noqa: BLE001
                        pass
            if edit is None and t == "EditControl":
                edit = c
            if confirm is None and t == "ButtonControl" and name.strip().strip("&").lower() in ("open", "ok", "attach", "select"):
                confirm = c
        return edit, confirm

    def dialog_paths(self, dlg, edit) -> list[str]:
        """Absolute paths the dialog would return: the File name box, resolved
        against the folder it is browsing. Handles "a" "b" multi-select."""
        try:
            raw = (edit.GetPattern(auto.PatternId.ValuePattern).Value or "").strip()
        except Exception:  # noqa: BLE001
            raw = ""
        if not raw:
            return []
        names = re.findall(r'"([^"]+)"', raw) or [raw]
        folder = self.dialog_folder(dlg)
        out = []
        for n in names:
            p = Path(n)
            out.append(str(p if p.is_absolute() else Path(folder or "") / n))
        return out

    @staticmethod
    def dialog_folder(dlg) -> str:
        """The folder the dialog is browsing, from the breadcrumb toolbar's name
        ("Address: Documents" style) - best effort; empty when unreadable."""
        for c in dlg.GetChildren():
            try:
                if c.ControlTypeName == "ToolBarControl" and "address" in (c.Name or "").lower():
                    for k in c.GetChildren():
                        nm = (k.Name or "")
                        if ":\\" in nm or nm.startswith("\\\\"):
                            return nm
            except Exception:  # noqa: BLE001
                continue
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
            self.dialog = None
            SHARED["dialog_open"] = False
            return
        if self.dialog is None:
            log("file dialog open")
            self.toast("File dialog: the file you pick will be masked before Claude sees it.")
        self.dialog = dlg
        SHARED["dialog_open"] = True

    def cmd_dialog_confirm(self, pressed_at: float) -> None:
        """The hook swallowed Enter (or a click on Open) in Claude's file dialog."""
        dlg = self.find_dialog()
        if dlg is None:
            SHARED["dialog_open"] = False
            return
        edit, confirm = self.dialog_parts(dlg)
        if edit is None:
            log("file guard: no File name box found; letting the dialog through")
            self.dialog_replay(dlg, confirm)
            return
        paths = self.dialog_paths(dlg, edit)
        if not paths:
            self.dialog_replay(dlg, confirm)         # empty box, or a folder double-click
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
            self.toast(msg + ".", error=True)
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
        self.toast(msg + ".", error=warn)
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
        self.toast(msg + ".", error=warn)
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
                               "(it is guarded), or switch drop blocking off in settings.", error=True)
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
        if not SHARED["blocking"]:
            SHARED["blocking"] = True
            SHARED["blocking_since"] = time.time()
            self.emit(type="block_drop", rect=(r.left, r.top, r.right, r.bottom))
            log("drop blocker up")

    def cmd_toggle_file_guard(self) -> None:
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
                self.wfile.write(("<!doctype html><meta charset=utf-8><title>Maskroom</title>"
                                  "<body style='font-family:system-ui;margin:3rem'>"
                                  + ("<h2>Signed in to Maskroom</h2><p>You can close this tab and go back to Claude.</p>"
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
        self._proc = None            # keep the callback alive for the hook's lifetime

    def claude_in_front(self) -> bool:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = process_exe(pid.value)
        if exe != CLAUDE_EXE:
            log(f"hook: foreground is {exe or '?'} (pid {pid.value}), not {CLAUDE_EXE}")
        return exe == CLAUDE_EXE

    WH_MOUSE_LL, WM_MOUSEWHEEL, WM_MOUSEHWHEEL = 14, 0x020A, 0x020E

    WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202

    def mouse_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        """Notes wheel scrolling, and swallows a click on the file dialog's
        confirm button so the pick can be masked first."""
        try:
            if n_code >= 0:
                if w_param in (self.WM_MOUSEWHEEL, self.WM_MOUSEHWHEEL) and foreground_exe() == CLAUDE_EXE:
                    SHARED["scroll_at"] = time.time()
                elif w_param == self.WM_LBUTTONDOWN and SHARED["dialog_open"] and self.cfg.get("fileGuard", True):
                    ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if not (ms.flags & 0x01) and self.on_dialog_confirm(ms.pt.x, ms.pt.y):   # LLMHF_INJECTED
                        self.swallow_click = True
                        self.commands.put(("dialog_confirm", time.time()))
                        log("hook: dialog Open click swallowed")
                        return 1
                elif w_param == self.WM_LBUTTONDOWN and foreground_exe() != CLAUDE_EXE:
                    SHARED["drag_at"] = time.time()     # a drag may have begun elsewhere
                elif w_param == self.WM_LBUTTONUP and self.swallow_click:
                    self.swallow_click = False
                    return 1
        except Exception as e:  # noqa: BLE001
            log(f"mouse hook error {type(e).__name__}: {e}")
        return _user32.CallNextHookEx(None, n_code, w_param, l_param)

    @staticmethod
    def on_dialog_confirm(x: int, y: int) -> bool:
        """Is (x,y) inside the confirm button of Claude's file dialog?"""
        try:
            ctl = auto.ControlFromPoint(x, y)
            if ctl is None or ctl.ControlTypeName != "ButtonControl":
                return False
            if (ctl.Name or "").strip().strip("&").lower() not in ("open", "ok", "attach", "select"):
                return False
            return process_exe(ctl.ProcessId) == CLAUDE_EXE
        except Exception:  # noqa: BLE001
            return False

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
                    front = self.claude_in_front()
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
        self.fonts: dict[tuple, tkfont.Font] = {}
        self.families = {f.lower() for f in tkfont.families(root)}
        self.dpi = root.winfo_fpixels("1i")
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

    def shift(self, dx: int, dy: int) -> None:
        if self.visible and (dx or dy):
            self.canvas.move("all", dx, dy)

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
        c = self.canvas
        c.delete("all")
        for (l, t, r, b), value, bg, style in items:
            x, y, w, h = l - left, t - top, r - l, b - t
            bg = bg if bg and bg.lower() != self.KEY else "#ffffff"
            fg = style.get("fg") or self.text_colour(bg)
            family = self.resolve_family(style.get("family"))
            bold = float(style.get("weight") or 400) >= 600
            italic = bool(style.get("italic"))
            if style.get("size"):
                px = max(9, int(round(float(style["size"]) * self.dpi / 72)))
            else:
                px = max(9, int(h * 0.68))
            f = self.font(family, px, bold, italic)
            text = value
            while f.measure(text) > w and px > 9:
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
class Bar:
    """The floating bar above the composer, plus toast, settings and the event pump."""

    W, H = 620, 34
    BG, FG, ACCENT, ERR = "#1f2937", "#e5e7eb", "#93a4c4", "#f87171"

    def __init__(self, cfg: dict, commands: "queue.Queue[tuple]", events: "queue.Queue[dict]"):
        self.cfg, self.commands, self.events = cfg, commands, events
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)

        self.win = tk.Toplevel(self.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=self.BG)
        self.win.withdraw()
        f = tk.Frame(self.win, bg=self.BG, padx=6, pady=4)
        f.pack(fill="both", expand=True)
        tk.Label(f, text="MASKROOM", bg=self.BG, fg=self.ACCENT, font=("Segoe UI", 8, "bold")).pack(side="left", padx=(0, 6))
        self.mask_btn = tk.Button(f, text="Mask", command=lambda: self.commands.put(("mask",)),
                                  bg="#374151", fg=self.FG, activebackground="#4b5563", relief="flat",
                                  padx=8, font=("Segoe UI", 9, "bold"))
        self.mask_btn.pack(side="left")
        self.guard_btn = tk.Button(f, text="", command=lambda: self.commands.put(("toggle_guard",)),
                                   bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                                   font=("Segoe UI", 8))
        self.guard_btn.pack(side="left", padx=(6, 0))
        self.set_guard_label(cfg.get("guard", True))
        self.unmask_btn = tk.Button(f, text="", command=lambda: self.commands.put(("toggle_unmask",)),
                                    bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                                    font=("Segoe UI", 8))
        self.unmask_btn.pack(side="left", padx=(6, 0))
        self.set_unmask_label(cfg.get("unmask", True))
        self.overlay_btn = tk.Button(f, text="", command=lambda: self.commands.put(("toggle_overlay",)),
                                     bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                                     font=("Segoe UI", 8))
        self.overlay_btn.pack(side="left", padx=(6, 0))
        self.set_overlay_label(cfg.get("overlay", True))
        self.fileguard_btn = tk.Button(f, text="", command=lambda: self.commands.put(("toggle_file_guard",)),
                                       bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                                       font=("Segoe UI", 8))
        self.fileguard_btn.pack(side="left", padx=(6, 0))
        self.set_fileguard_label(cfg.get("fileGuard", True))
        tk.Button(f, text="new session", command=lambda: self.commands.put(("new_session",)),
                  bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                  font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))
        tk.Button(f, text="⚙", command=self.open_settings, bg=self.BG, fg=self.ACCENT,
                  activebackground=self.BG, relief="flat", font=("Segoe UI", 9)).pack(side="right")
        self.status = tk.Label(f, text="", bg=self.BG, fg=self.FG, font=("Segoe UI", 8), anchor="w")
        self.status.pack(side="left", padx=(8, 0), fill="x", expand=True)
        make_no_activate(self.win)

        self.tip = tk.Toplevel(self.root)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.configure(bg="#111827")
        self.tip.withdraw()
        self.tip_label = tk.Label(self.tip, text="", bg="#111827", fg="#f9fafb", font=("Segoe UI", 10),
                                  justify="left", wraplength=520, padx=10, pady=6)
        self.tip_label.pack()
        make_no_activate(self.tip)

        self.overlay = Overlay(self.root)
        self.blocker = DropBlocker(self.root)

        self.toast_after = None
        self.settings_win = None
        self.visible = False
        self.rect = None
        self.root.after(15, self.pump)
        if not ((cfg.get("apiKey") or "").strip() or (cfg.get("token") or "").strip()):
            self.root.after(300, self.open_settings)

    def set_guard_label(self, on: bool) -> None:
        self.guard_btn.configure(text=f"guard: {'on' if on else 'off'}", fg=self.ACCENT if on else self.ERR)

    def set_fileguard_label(self, on: bool) -> None:
        self.fileguard_btn.configure(text=f"files: {'on' if on else 'off'}", fg=self.ACCENT if on else self.ERR)

    def set_overlay_label(self, on: bool) -> None:
        self.overlay_btn.configure(text=f"overlay: {'on' if on else 'off'}", fg=self.ACCENT if on else self.ERR)

    def set_unmask_label(self, on: bool) -> None:
        self.unmask_btn.configure(text=f"unmask: {'on' if on else 'off'}", fg=self.ACCENT if on else self.ERR)

    def show_tip(self, text, x: int, y: int) -> None:
        if not text:
            self.tip.withdraw()
            return
        self.tip_label.configure(text=text)
        self.tip.update_idletasks()
        w, h = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = max(0, min(x, sw - w))
        if y + h > sh:
            y = max(0, y - h - 30)
        self.tip.geometry(f"+{x}+{y}")
        self.tip.deiconify()

    # ---- placement
    def place(self, rect) -> None:
        left, top, right, bottom = rect
        w = self.W
        x = right - w
        y = top - self.H - 6
        if y < 0:
            y = bottom + 6
        if rect != self.rect:
            self.win.geometry(f"{w}x{self.H}+{x}+{y}")
            self.rect = rect
        if not self.visible:
            self.win.deiconify()
            self.visible = True

    def hide(self) -> None:
        if self.visible:
            self.win.withdraw()
            self.visible = False
            self.rect = None

    # ---- messages
    def toast(self, msg: str, error: bool = False) -> None:
        self.status.configure(text=msg, fg=self.ERR if error else self.FG)
        if self.toast_after:
            self.root.after_cancel(self.toast_after)
        self.toast_after = self.root.after(6000, lambda: self.status.configure(text=""))
        if not self.visible:
            # Nothing to anchor to: show briefly at the bottom-right of the screen.
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            self.win.geometry(f"{self.W}x{self.H}+{sw - self.W - 24}+{sh - self.H - 80}")
            self.win.deiconify()
            self.root.after(4000, lambda: None if self.visible else self.win.withdraw())

    def pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                t = ev["type"]
                if t == "composer":
                    self.place(ev["rect"]) if ev["visible"] else self.hide()
                elif t == "toast":
                    self.toast(ev["msg"], ev.get("error", False))
                elif t == "busy":
                    self.mask_btn.configure(text="Masking…" if ev["busy"] else "Mask",
                                            state="disabled" if ev["busy"] else "normal")
                elif t == "check" and self.settings_win and self.settings_win.winfo_exists():
                    self.check_label.configure(text=ev["msg"], fg="#065f46" if ev["ok"] else "#991b1b")
                elif t == "auth":
                    self.toast(ev["msg"], not ev["ok"])
                    if self.settings_win and self.settings_win.winfo_exists():
                        self.check_label.configure(text=ev["msg"], fg="#065f46" if ev["ok"] else "#991b1b")
                elif t == "session":
                    self.toast(f"Session {ev['id'][:8]}… for {ev.get('title') or 'this chat'}")
                elif t == "guard":
                    self.set_guard_label(ev["on"])
                elif t == "unmask":
                    self.set_unmask_label(ev["on"])
                elif t == "overlay":
                    self.overlay.render(ev["items"], ev.get("win"))
                elif t == "overlay_shift":
                    self.overlay.shift(ev["dx"], ev["dy"])
                elif t == "overlay_state":
                    self.set_overlay_label(ev["on"])
                elif t == "file_guard":
                    self.set_fileguard_label(ev["on"])
                elif t == "block_drop":
                    self.blocker.show(ev["rect"]) if ev["rect"] else self.blocker.hide()
                elif t == "tip":
                    self.show_tip(ev["text"], ev["x"], ev["y"])
        except queue.Empty:
            pass
        self.root.after(15, self.pump)

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
        tk.Label(w, text="Server URL").grid(row=0, column=0, sticky="w", **pad)
        url = tk.Entry(w, width=48)
        url.insert(0, self.cfg["serverUrl"])
        url.grid(row=0, column=1, **pad)
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
        block_drops = tk.BooleanVar(value=self.cfg.get("blockDrops", True))
        tk.Checkbutton(w, text="Refuse files dropped from Explorer (they cannot be masked in flight; "
                               "the paperclip can)", variable=block_drops).grid(
            row=7, column=0, columnspan=2, sticky="w", **pad)
        self.check_label = tk.Label(w, text="", anchor="w")
        self.check_label.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.commands.put(("check",))   # show who we are, if anyone
        tk.Label(w, text=f"Sessions: {len(self.cfg.get('sessions') or {})} known, "
                         f"{len(self.cfg.get('chats') or {})} chats bound; current {(self.cfg.get('sessionId') or 'none')[:8]}    "
                         f"Hotkeys: Ctrl+Shift+M mask, Ctrl+Shift+U unmask clipboard\n"
                         f"Config: {CONFIG_FILE}", justify="left", fg="#6b7280").grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        btns = tk.Frame(w)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", **pad)

        def apply():
            self.cfg["serverUrl"] = url.get().strip() or DEFAULTS["serverUrl"]
            self.cfg["apiKey"] = key.get().strip()
            self.cfg["preamble"] = bool(pre.get())
            self.cfg["overlayHideOnScroll"] = bool(hide_scroll.get())
            self.cfg["blockDrops"] = bool(block_drops.get())
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
    if not sys.platform.startswith("win"):
        print("This prototype is Windows-only (UI Automation). See docs/DESKTOP_APP_RESEARCH.md for macOS.")
        return 1
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # UIA rectangles are physical pixels
    except Exception:  # noqa: BLE001
        pass
    cfg = load_config()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    log(f"start: server={cfg.get('serverUrl')} guard={cfg.get('guard', True)} log={LOG_FILE}"
        + (f" mask={len(args)} file(s)" if args else ""))
    commands: "queue.Queue[tuple]" = queue.Queue()
    events: "queue.Queue[dict]" = queue.Queue()
    Automation(cfg, commands, events).start()
    Hotkeys(cfg, commands, events).start()
    commands.put(("load_vault",))
    if args:
        commands.put(("mask_files", args))
    Bar(cfg, commands, events).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
