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

Ctrl+Shift+U restores TOK_ tokens in whatever is on the clipboard (POST
/api/unmask) so text copied out of a Claude reply can be pasted elsewhere with
the real values. There is no on-screen unmask of replies: see
docs/DESKTOP_APP_RESEARCH.md section 5.4 for why.

Run:   py -m pip install uiautomation
       py desktop\\helper.py

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
import sys
import threading
import time
import tkinter as tk
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
    "sessionId": None,
    "preamble": True,
    "guard": True,
    "preambleSent": [],
}
SIGNIN_TIMEOUT_S = 300
CLAUDE_EXE = "claude.exe"
COMPOSER_CLASS_HINT = "ProseMirror"
POLL_S = 0.25
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
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


# ----------------------------------------------------------------------------- server
class Server:
    """The same contract extension/background.js uses, over urllib."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

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


def _json_or_none(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


# ----------------------------------------------------------------------------- win32 bits
_IS_WIN = sys.platform.startswith("win")
_kernel32 = ctypes.windll.kernel32 if _IS_WIN else None
_user32 = ctypes.windll.user32 if _IS_WIN else None
if _IS_WIN:
    _kernel32.OpenProcess.restype = wt.HANDLE
    _kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    _kernel32.CloseHandle.argtypes = [wt.HANDLE]
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
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


def make_no_activate(tk_toplevel: tk.Toplevel) -> None:
    """Keep the bar from taking keyboard focus away from Claude when clicked."""
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
    tk_toplevel.update_idletasks()
    hwnd = _user32.GetParent(tk_toplevel.winfo_id()) or tk_toplevel.winfo_id()
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)


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

    # ---- plumbing
    def emit(self, **ev) -> None:
        self.events.put(ev)

    def toast(self, msg: str, error: bool = False) -> None:
        self.emit(type="toast", msg=msg, error=error)

    def run(self) -> None:
        with auto.UIAutomationInitializerInThread():
            while True:
                try:
                    cmd = self.commands.get(timeout=POLL_S)
                except queue.Empty:
                    self.poll_focus()
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
            self.composer = ctrl
            self.last_seen = time.time()
            self.emit(type="composer", visible=True, rect=(r.left, r.top, r.right, r.bottom))
        elif time.time() - self.last_seen > HIDE_GRACE_S:
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
        auto.SetClipboardText(text)
        auto.SendKeys("{Ctrl}a", waitTime=0.1)
        auto.SendKeys("{Ctrl}v", waitTime=0.3)
        return "paste"

    # ---- sessions
    def ensure_session(self) -> str:
        sid = self.cfg.get("sessionId")
        if sid:
            r = self.server.api(f"/api/session/{sid}")
            if r["ok"]:
                return sid
            if r["status"] not in (404, 403):
                raise RuntimeError(r["error"])
        r = self.server.api("/api/session", "POST", {})
        if not r["ok"]:
            raise RuntimeError(r["error"])
        self.cfg["sessionId"] = r["data"]["session_id"]
        save_config(self.cfg)
        self.emit(type="session", id=self.cfg["sessionId"])
        return self.cfg["sessionId"]

    def cmd_new_session(self) -> None:
        self.cfg["sessionId"] = None
        save_config(self.cfg)
        sid = self.ensure_session()
        self.toast(f"New session {sid[:8]}…")

    def cmd_sign_out(self) -> None:
        r = self.server.api("/auth/logout", "POST", {})
        self.cfg["token"] = ""
        save_config(self.cfg)
        redirect = (r["data"] or {}).get("redirect") if r["ok"] else None
        if redirect and redirect.startswith("http"):
            webbrowser.open(redirect)    # let the identity provider end its session too
        self.emit(type="auth", ok=True, signed_in=False, msg="Signed out.")

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
                self.cfg["sessionId"] = None
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
            result = {"done": True, "changed": bool(d["changed"])}
            return result
        finally:
            self.mask_done_at = time.time()
            self.emit(type="busy", busy=False)

    # ---- the guard
    @staticmethod
    def replay_enter() -> None:
        """Send the Enter the user asked for. Synthetic input carries the
        LLKHF_INJECTED flag, so the hook lets it through."""
        auto.SendKeys("{Enter}", waitTime=0)

    def cmd_guard(self, pressed_at: float) -> None:
        """An Enter the hook swallowed while Claude was in front."""
        if pressed_at < self.mask_done_at:
            self.toast("Send dropped: masking finished after you pressed Enter. Review, then press Enter again.", error=True)
            return
        ctrl = self.current_composer()
        if ctrl is None:                      # Enter somewhere else in Claude: not ours
            self.replay_enter()
            return
        text = self.read_text(ctrl)
        if not text.strip() or text.strip() == self.last_masked.strip():
            self.replay_enter()               # empty, or already checked: send as typed
            return
        r = self.mask_composer(ctrl)
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
        sid = self.cfg.get("sessionId")
        if not sid:
            self.toast("No session yet — mask something first.", error=True)
            return
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
        self._proc = None            # keep the callback alive for the hook's lifetime

    def claude_in_front(self) -> bool:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wt.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return process_exe(pid.value) == CLAUDE_EXE

    def hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        try:
            if n_code >= 0 and self.cfg.get("guard", True):
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == self.VK_RETURN and not (kb.flags & self.LLKHF_INJECTED):
                    if w_param in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                        plain = not (kb.flags & self.LLKHF_ALTDOWN) \
                            and not (_user32.GetAsyncKeyState(self.VK_SHIFT) & 0x8000) \
                            and not (_user32.GetAsyncKeyState(self.VK_CONTROL) & 0x8000)
                        if plain and self.claude_in_front():
                            self.swallow_up = True
                            self.commands.put(("guard", time.time()))
                            return 1
                    elif w_param in (self.WM_KEYUP, self.WM_SYSKEYUP) and self.swallow_up:
                        self.swallow_up = False
                        return 1
        except Exception:  # noqa: BLE001 - never let the hook die
            pass
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
        self._proc = _HOOKPROC(self.hook_proc)
        hook = _user32.SetWindowsHookExW(self.WH_KEYBOARD_LL, self._proc, _kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.events.put({"type": "toast", "error": True,
                             "msg": "Could not install the keyboard hook; the guard is unavailable."})
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY and msg.wParam in self.BINDINGS:
                self.commands.put((self.BINDINGS[msg.wParam][1],))
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))


# ----------------------------------------------------------------------------- UI
class Bar:
    """The floating bar above the composer, plus toast, settings and the event pump."""

    W, H = 400, 34
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
        tk.Button(f, text="new session", command=lambda: self.commands.put(("new_session",)),
                  bg=self.BG, fg=self.ACCENT, activebackground=self.BG, relief="flat",
                  font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))
        tk.Button(f, text="⚙", command=self.open_settings, bg=self.BG, fg=self.ACCENT,
                  activebackground=self.BG, relief="flat", font=("Segoe UI", 9)).pack(side="right")
        self.status = tk.Label(f, text="", bg=self.BG, fg=self.FG, font=("Segoe UI", 8), anchor="w")
        self.status.pack(side="left", padx=(8, 0), fill="x", expand=True)
        make_no_activate(self.win)

        self.toast_after = None
        self.settings_win = None
        self.visible = False
        self.rect = None
        self.root.after(100, self.pump)
        if not ((cfg.get("apiKey") or "").strip() or (cfg.get("token") or "").strip()):
            self.root.after(300, self.open_settings)

    def set_guard_label(self, on: bool) -> None:
        self.guard_btn.configure(text=f"guard: {'on' if on else 'off'}", fg=self.ACCENT if on else self.ERR)

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
                    self.toast(f"Session {ev['id'][:8]}…")
                elif t == "guard":
                    self.set_guard_label(ev["on"])
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

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
        self.check_label = tk.Label(w, text="", anchor="w")
        self.check_label.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.commands.put(("check",))   # show who we are, if anyone
        tk.Label(w, text=f"Session: {(self.cfg.get('sessionId') or 'none')[:8]}    "
                         f"Hotkeys: Ctrl+Shift+M mask, Ctrl+Shift+U unmask clipboard\n"
                         f"Config: {CONFIG_FILE}", justify="left", fg="#6b7280").grid(
            row=4, column=0, columnspan=2, sticky="w", **pad)
        btns = tk.Frame(w)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", **pad)

        def apply():
            self.cfg["serverUrl"] = url.get().strip() or DEFAULTS["serverUrl"]
            self.cfg["apiKey"] = key.get().strip()
            self.cfg["preamble"] = bool(pre.get())
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
    commands: "queue.Queue[tuple]" = queue.Queue()
    events: "queue.Queue[dict]" = queue.Queue()
    Automation(cfg, commands, events).start()
    Hotkeys(cfg, commands, events).start()
    Bar(cfg, commands, events).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
