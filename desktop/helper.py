"""Maskroom desktop helper (Windows prototype).

Sits beside Claude Desktop the way the Chrome extension sits inside claude.ai:
when the composer has focus, a small floating bar appears above it with a Mask
button. Mask (button, or Ctrl+Shift+M anywhere) reads the composer through UI
Automation, sends the text to the Maskroom server (POST /api/mask), and writes
the pseudonymized text back in place through ValuePattern.SetValue - the write
path verified by scripts/desktop/uia_composer_probe.py. You still press send.

Ctrl+Shift+U restores TOK_ tokens in whatever is on the clipboard (POST
/api/unmask) so text copied out of a Claude reply can be pasted elsewhere with
the real values. There is no on-screen unmask of replies: see
docs/DESKTOP_APP_RESEARCH.md section 5.4 for why.

Run:   py -m pip install uiautomation
       py desktop\\helper.py

Config lives in %APPDATA%\\Maskroom\\helper.json (server URL, key, session id).
Open the settings window from the gear button on the bar.

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
import urllib.request
from pathlib import Path

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    print("Missing dependency. Run:  py -m pip install uiautomation")
    sys.exit(1)

APP_NAME = "Maskroom"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_FILE = CONFIG_DIR / "helper.json"
DEFAULTS = {
    "serverUrl": "http://127.0.0.1:5170",
    "apiKey": "",          # a named service key (mr_...) or the legacy shared key
    "sessionId": None,
    "preamble": True,
    "preambleSent": [],
}
CLAUDE_EXE = "claude.exe"
COMPOSER_CLASS_HINT = "ProseMirror"
POLL_S = 0.25
HIDE_GRACE_S = 0.8

# ----------------------------------------------------------------------------- config
def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
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
        key = (self.cfg.get("apiKey") or "").strip()
        if key.startswith("mr_"):
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

    def cmd_check(self) -> None:
        r = self.server.api("/api/me")
        if r["ok"]:
            d = r["data"] or {}
            p = d.get("principal") or {}
            who = p.get("name") or p.get("email") or ("anonymous, auth off" if d.get("auth_mode") == "off" else "ok")
            self.emit(type="check", ok=True, msg=f"Connected as {who}")
        else:
            self.emit(type="check", ok=False, msg=r["error"] or "not signed in")

    # ---- the two actions
    def cmd_mask(self) -> None:
        ctrl = self.current_composer()
        if ctrl is None:
            self.toast("Click into the Claude composer first.", error=True)
            return
        text = self.read_text(ctrl)
        if not text.strip():
            self.toast("Nothing to mask.")
            return
        self.emit(type="busy", busy=True)
        try:
            sid = self.ensure_session()
            r = self.server.api("/api/mask", "POST", {"text": text, "session_id": sid})
            if not r["ok"] and r["status"] == 404:
                self.cfg["sessionId"] = None
                sid = self.ensure_session()
                r = self.server.api("/api/mask", "POST", {"text": text, "session_id": sid})
            if not r["ok"]:
                self.toast(r["error"], error=True)
                return
            d = r["data"]
            out = d["masked"]
            if d["changed"] and self.cfg.get("preamble", True) and sid not in self.cfg["preambleSent"]:
                out = d["preamble"] + "\n\n" + out
                self.cfg["preambleSent"] = (self.cfg["preambleSent"] + [sid])[-50:]
                save_config(self.cfg)
            how = ""
            if d["changed"]:
                how = self.write_text(ctrl, out)
            n = len(d["findings"])
            if n:
                self.toast(f"{n} value{'' if n == 1 else 's'} masked ({how}). Review, then press send.")
            else:
                self.toast("No PII detected — safe to send.")
            self.emit(type="vault", entries=d.get("vault_entries", 0))
        finally:
            self.emit(type="busy", busy=False)

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


# ----------------------------------------------------------------------------- hotkeys
class Hotkeys(threading.Thread):
    MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT, WM_HOTKEY = 0x0002, 0x0004, 0x4000, 0x0312
    BINDINGS = {1: ("M", "mask"), 2: ("U", "unmask_clipboard")}

    def __init__(self, commands: "queue.Queue[tuple]", events: "queue.Queue[dict]"):
        super().__init__(name="hotkeys", daemon=True)
        self.commands = commands
        self.events = events

    def run(self) -> None:
        mods = self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT
        for hid, (key, _) in self.BINDINGS.items():
            if not _user32.RegisterHotKey(None, hid, mods, ord(key)):
                self.events.put({"type": "toast", "error": True,
                                 "msg": f"Ctrl+Shift+{key} is taken by another app; use the bar instead."})
        msg = wt.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY and msg.wParam in self.BINDINGS:
                self.commands.put((self.BINDINGS[msg.wParam][1],))


# ----------------------------------------------------------------------------- UI
class Bar:
    """The floating bar above the composer, plus toast, settings and the event pump."""

    W, H = 330, 34
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
        if not (cfg.get("apiKey") or "").strip():
            self.root.after(300, self.open_settings)

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
                elif t == "check" and self.settings_win:
                    self.check_label.configure(text=ev["msg"], fg="#065f46" if ev["ok"] else "#991b1b")
                elif t == "session":
                    self.toast(f"Session {ev['id'][:8]}…")
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
        tk.Label(w, text="API key (mr_… service key\nor legacy shared key)").grid(row=1, column=0, sticky="w", **pad)
        key = tk.Entry(w, width=48, show="•")
        key.insert(0, self.cfg.get("apiKey") or "")
        key.grid(row=1, column=1, **pad)
        pre = tk.BooleanVar(value=self.cfg.get("preamble", True))
        tk.Checkbutton(w, text="Prefix the first masked message of a session with the token preamble",
                       variable=pre).grid(row=2, column=0, columnspan=2, sticky="w", **pad)
        self.check_label = tk.Label(w, text="", anchor="w")
        self.check_label.grid(row=3, column=0, columnspan=2, sticky="w", **pad)
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
    Hotkeys(commands, events).start()
    Bar(cfg, commands, events).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
