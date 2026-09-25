"""Experiment B (Windows): can a helper write into Claude Desktop's composer through
UI Automation, or must it fall back to select-all + paste?

See docs/DESKTOP_APP_RESEARCH.md §3.2, §3.4 and §8.2 step 3(b).

Run ON THE WINDOWS PC, from any terminal (no admin needed):

    py -m pip install uiautomation
    py uia_composer_probe.py

The script is interactive. It asks you to click into the Claude Desktop composer, then:

  1. reads the focused element and lists every UIA pattern it supports
  2. reads the composer text through TextPattern (the read half)
  3. tries ValuePattern.SetValue with masked text          (write attempt 1)
  4. tries LegacyIAccessiblePattern.SetValue                (write attempt 2)
  5. does select-all + paste through the clipboard          (the fallback)

After each write it re-reads the text and asks you what you see, and whether the
change survived typing one more character. It never presses Enter in Claude; you
decide whether to send. Everything is appended to uia_probe_report.txt next to the
script - paste that file back.

Nothing here talks to a Maskroom server; the "masked" text is a fixed sample.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import time
import traceback

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover - message for the person running it
    print("Missing dependency. Run:  py -m pip install uiautomation")
    sys.exit(1)

MASKED_SAMPLE = (
    "Call TOK_PERSON_1A2B3C4D on TOK_PHONE_NUMBER_5E6F7A8B, "
    "NIC TOK_LK_NIC_9C0D1E2F. (maskroom probe)"
)
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uia_probe_report.txt")


# --------------------------------------------------------------------------- output
def log(msg: str = "") -> None:
    print(msg)
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def ask(prompt: str) -> str:
    answer = input(prompt + " ").strip()
    log(f"    [you] {prompt} {answer}")
    return answer


def section(title: str) -> None:
    log("")
    log("=" * 78)
    log(title)
    log("=" * 78)


def attempt(label: str, fn):
    """Run fn(), log the result or the exception, return (ok, value)."""
    try:
        value = fn()
        log(f"  OK   {label}: {value!r}")
        return True, value
    except Exception as exc:  # noqa: BLE001 - we want every failure recorded
        log(f"  FAIL {label}: {type(exc).__name__}: {exc}")
        return False, None


# --------------------------------------------------------------------------- helpers
def describe(ctrl: auto.Control) -> None:
    attempt("ControlTypeName", lambda: ctrl.ControlTypeName)
    attempt("LocalizedControlType", lambda: ctrl.LocalizedControlType)
    attempt("Name", lambda: ctrl.Name)
    attempt("AutomationId", lambda: ctrl.AutomationId)
    attempt("ClassName", lambda: ctrl.ClassName)
    attempt("ProcessId", lambda: ctrl.ProcessId)
    attempt("BoundingRectangle", lambda: str(ctrl.BoundingRectangle))
    attempt("IsEnabled", lambda: ctrl.IsEnabled)
    attempt("IsKeyboardFocusable", lambda: ctrl.IsKeyboardFocusable)
    attempt("HasKeyboardFocus", lambda: ctrl.HasKeyboardFocus)
    attempt("FrameworkId", lambda: ctrl.FrameworkId)
    attempt("ProviderDescription", lambda: ctrl.ProviderDescription)
    attempt("TopLevel window Name", lambda: ctrl.GetTopLevelControl().Name)
    attempt("TopLevel window ClassName", lambda: ctrl.GetTopLevelControl().ClassName)


def ancestry(ctrl: auto.Control, depth: int = 6) -> None:
    log("  Ancestors (nearest first):")
    cur = ctrl
    for _ in range(depth):
        try:
            cur = cur.GetParentControl()
        except Exception:  # noqa: BLE001
            break
        if cur is None:
            break
        try:
            log(f"    - {cur.ControlTypeName:<18} name={cur.Name[:60]!r} class={cur.ClassName!r}")
        except Exception as exc:  # noqa: BLE001
            log(f"    - <unreadable: {exc}>")


def supported_patterns(ctrl: auto.Control) -> dict[str, object]:
    found: dict[str, object] = {}
    for pid, pname in sorted(auto.PatternIdNames.items()):
        try:
            pat = ctrl.GetPattern(pid)
        except Exception:  # noqa: BLE001
            pat = None
        if pat is not None:
            found[pname] = pat
    log("  Supported patterns: " + (", ".join(found) if found else "(none)"))
    return found


def read_text(ctrl: auto.Control) -> str | None:
    """Best-effort read through TextPattern, then ValuePattern, then LegacyIAccessible."""
    for label, getter in (
        ("TextPattern.DocumentRange.GetText", lambda: ctrl.GetTextPattern().DocumentRange.GetText(-1)),
        ("ValuePattern.Value", lambda: ctrl.GetValuePattern().Value),
        ("LegacyIAccessiblePattern.Value", lambda: ctrl.GetLegacyIAccessiblePattern().Value),
        ("Name", lambda: ctrl.Name),
    ):
        try:
            text = getter()
        except Exception:  # noqa: BLE001
            continue
        if text is not None:
            log(f"  read via {label}: {text!r}")
            return text
    log("  read: no pattern returned text")
    return None


def wait_for_focus_in(process_hint: str, seconds: int = 20) -> auto.Control | None:
    """Poll the focused control until it lives in a window whose title mentions Claude."""
    deadline = time.time() + seconds
    last = None
    while time.time() < deadline:
        try:
            ctrl = auto.GetFocusedControl()
            top = ctrl.GetTopLevelControl()
            title = (top.Name or "") if top else ""
            last = f"{ctrl.ControlTypeName} in {title!r}"
            if process_hint.lower() in title.lower():
                return ctrl
        except Exception as exc:  # noqa: BLE001
            last = f"error: {exc}"
        time.sleep(0.5)
    log(f"  timed out; last focused element was {last}")
    return None


def survival_check(ctrl: auto.Control, expect_fragment: str) -> None:
    """Did ProseMirror keep the write in its own model, or only in the DOM?"""
    log("  Re-reading right after the write:")
    after = read_text(ctrl) or ""
    log(f"  contains token fragment {expect_fragment!r}: {expect_fragment in after}")
    ask("Look at the composer. Does it SHOW the masked text? (y/n)")
    ask("Now click at the END of the composer text and type one character (e.g. x). Done? (Enter)")
    log("  Re-reading after you typed:")
    after2 = read_text(ctrl) or ""
    log(f"  still contains {expect_fragment!r}: {expect_fragment in after2}")
    ask("Did the masked text stay, or did the original come back / get mangled? (stayed/reverted/mangled)")


# --------------------------------------------------------------------------- phases
def phase_locate(hint: str) -> auto.Control | None:
    section("PHASE 1 - locate the focused composer")
    log("Open Claude Desktop, start a NEW chat, type a short sentence with a fake phone number,")
    log("e.g.  Call Nimal on 0771234567 about the invoice")
    ask("Then CLICK INTO THE COMPOSER so it has the caret. Press Enter here within 20 s, then click back into Claude.")
    log(f"  polling the focused element for a window whose title contains {hint!r}...")
    ctrl = wait_for_focus_in(hint)
    if ctrl is None:
        log("  Could not find focus inside Claude. If the window title does not contain 'Claude',")
        log("  rerun with:  py uia_composer_probe.py \"<part of the window title>\"")
        return None
    log("  Focused element found.")
    describe(ctrl)
    ancestry(ctrl)
    return ctrl


def phase_read(ctrl: auto.Control) -> dict[str, object]:
    section("PHASE 2 - patterns and read")
    pats = supported_patterns(ctrl)

    if "TextPattern" in pats:
        tp = pats["TextPattern"]
        attempt("TextPattern.DocumentRange.GetText", lambda: tp.DocumentRange.GetText(-1))
        attempt("TextPattern.SupportedTextSelection", lambda: tp.SupportedTextSelection)
        attempt("TextPattern.GetSelection() count", lambda: len(tp.GetSelection()))
        attempt(
            "TextPattern.DocumentRange.GetBoundingRectangles",
            lambda: [str(r) for r in tp.DocumentRange.GetBoundingRectangles()],
        )
    else:
        log("  TextPattern ABSENT - the overlay would have no line rectangles to anchor to.")

    if "ValuePattern" in pats:
        vp = pats["ValuePattern"]
        attempt("ValuePattern.IsReadOnly", lambda: vp.IsReadOnly)
        attempt("ValuePattern.Value", lambda: vp.Value)
    else:
        log("  ValuePattern ABSENT - matches Microsoft's note that multi-line edits often omit it.")

    if "LegacyIAccessiblePattern" in pats:
        lp = pats["LegacyIAccessiblePattern"]
        attempt("Legacy.Role", lambda: lp.Role)
        attempt("Legacy.State", lambda: lp.State)
        attempt("Legacy.Value", lambda: lp.Value)
        attempt("Legacy.DefaultAction", lambda: lp.DefaultAction)

    if "TextEditPattern" in pats:
        log("  TextEditPattern present (Chromium UIA provider is on).")
    return pats


def phase_write_value(ctrl: auto.Control, pats: dict[str, object]) -> None:
    section("PHASE 3 - write attempt 1: ValuePattern.SetValue")
    if "ValuePattern" not in pats:
        log("  skipped: no ValuePattern")
        return
    ask("Click back into the composer, then press Enter here. Writing in 3 s...")
    time.sleep(3)
    ok, _ = attempt("ValuePattern.SetValue", lambda: pats["ValuePattern"].SetValue(MASKED_SAMPLE))
    if ok:
        survival_check(ctrl, "TOK_PERSON_1A2B3C4D")


def phase_write_legacy(ctrl: auto.Control, pats: dict[str, object]) -> None:
    section("PHASE 4 - write attempt 2: LegacyIAccessiblePattern.SetValue (IAccessible::put_accValue)")
    if "LegacyIAccessiblePattern" not in pats:
        log("  skipped: no LegacyIAccessiblePattern")
        return
    ask("Restore the composer to your original sentence (Ctrl+Z or retype), click into it, then Enter here. Writing in 3 s...")
    time.sleep(3)
    ok, _ = attempt("Legacy.SetValue", lambda: pats["LegacyIAccessiblePattern"].SetValue(MASKED_SAMPLE))
    if ok:
        survival_check(ctrl, "TOK_PERSON_1A2B3C4D")


def phase_paste(ctrl: auto.Control) -> None:
    section("PHASE 5 - fallback: select-all + paste via clipboard and SendInput")
    ask("Restore the composer to your original sentence, click into it, then Enter here. Pasting in 3 s...")
    time.sleep(3)
    attempt("SetClipboardText", lambda: auto.SetClipboardText(MASKED_SAMPLE))
    attempt("SendKeys Ctrl+A", lambda: auto.SendKeys("{Ctrl}a", waitTime=0.2))
    attempt("SendKeys Ctrl+V", lambda: auto.SendKeys("{Ctrl}v", waitTime=0.5))
    time.sleep(0.5)
    survival_check(ctrl, "TOK_PERSON_1A2B3C4D")


def phase_send() -> None:
    section("PHASE 6 - optional send check (you press Enter in Claude, not the script)")
    log("If one of the writes 'stayed', send that message in Claude and look at the sent bubble.")
    ask("Did the SENT message contain the TOK_ tokens (y), the original text (n), or did you skip (s)?")


# --------------------------------------------------------------------------- main
def main() -> int:
    global_hint = sys.argv[1] if len(sys.argv) > 1 else "Claude"
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    section(f"Maskroom UIA composer probe  {_dt.datetime.now():%Y-%m-%d %H:%M}")
    log(f"  python {sys.version.split()[0]}  uiautomation {getattr(auto, 'VERSION', '?')}")
    log(f"  report file: {REPORT}")
    log("  Reminder: run the probe and Claude Desktop as the same user (UIPI); do not run this elevated.")

    ctrl = None
    try:
        ctrl = phase_locate(global_hint)
        if ctrl is None:
            return 2
        pats = phase_read(ctrl)
        phase_write_value(ctrl, pats)
        phase_write_legacy(ctrl, pats)
        phase_paste(ctrl)
        phase_send()
    except KeyboardInterrupt:
        log("  aborted by user")
        return 130
    except Exception:  # noqa: BLE001
        log("  UNEXPECTED ERROR:")
        log(traceback.format_exc())
        return 1
    section("DONE - paste uia_probe_report.txt back")
    return 0


if __name__ == "__main__":
    sys.exit(main())
