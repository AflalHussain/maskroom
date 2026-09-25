"""Experiment C (Windows): can a helper find TOK_ tokens in Claude Desktop's replies
through UI Automation, fast enough and precisely enough to draw the real values
over them (or under the mouse)?

See docs/DESKTOP_APP_RESEARCH.md section 5.4 (which called this impractical before the
composer probe showed how well Claude Desktop's tree behaves) and section 3.2.

Run ON THE WINDOWS PC, same setup as uia_composer_probe.py:

    py uia_reply_probe.py

Before running: in Claude Desktop, send a masked message through the helper and let
Claude reply with the tokens repeated (ask it "repeat every identifier back to me").
Keep that chat on screen. The script then:

  1. finds the Claude window and the document element that carries the page text
  2. reads the whole document text through TextPattern and times it
  3. enumerates every TOK_ token range with FindText and times it, printing each
     token's bounding rectangles (what an overlay would draw over)
  4. checks GetVisibleRanges (only the viewport, or the whole page?)
  5. asks you to hover the mouse over a token: RangeFromPoint + expand-to-word
  6. asks you to scroll: re-enumerates and shows whether rectangles moved
  7. asks you to start a new streaming reply: samples the enumeration every 0.5 s
     for 15 s to see the cost and the lag while text is changing

Everything is appended to uia_reply_probe_report.txt next to the script - paste it back.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sys
import time

try:
    import uiautomation as auto
except ImportError:  # pragma: no cover
    print("Missing dependency. Run:  py -m pip install uiautomation")
    sys.exit(1)

TOKEN_RE = re.compile(r"TOK_[A-Z][A-Z0-9_]*?_[0-9A-F]{8}", re.IGNORECASE)
REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uia_reply_probe_report.txt")


def log(msg: str = "") -> None:
    print(msg)
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")


def ask(prompt: str) -> str:
    a = input(prompt + " ").strip()
    log(f"    [you] {prompt} {a}")
    return a


def section(title: str) -> None:
    log(""); log("=" * 78); log(title); log("=" * 78)


def timed(label, fn):
    t0 = time.perf_counter()
    try:
        v = fn()
        dt = (time.perf_counter() - t0) * 1000
        log(f"  OK   {label}: {dt:.0f} ms")
        return v, dt
    except Exception as e:  # noqa: BLE001
        dt = (time.perf_counter() - t0) * 1000
        log(f"  FAIL {label}: {type(e).__name__}: {e} ({dt:.0f} ms)")
        return None, dt


# ------------------------------------------------------------------ locate
def find_claude_window():
    for w in auto.GetRootControl().GetChildren():
        try:
            if w.ClassName == "Chrome_WidgetWin_1" and "claude" in (w.Name or "").lower():
                return w
        except Exception:  # noqa: BLE001
            continue
    return None


def find_text_documents(win, limit_depth=12):
    """Every element under the window that supports TextPattern, with its text length."""
    found = []

    def walk(c, depth):
        if depth > limit_depth:
            return
        try:
            tp = c.GetPattern(auto.PatternId.TextPattern)
        except Exception:  # noqa: BLE001
            tp = None
        if tp is not None:
            try:
                n = len(tp.DocumentRange.GetText(-1) or "")
            except Exception:  # noqa: BLE001
                n = -1
            found.append((c, tp, n, depth))
            return  # children of a text container are inside its range already
        try:
            kids = c.GetChildren()
        except Exception:  # noqa: BLE001
            kids = []
        for k in kids:
            walk(k, depth + 1)

    walk(win, 0)
    return found


def enumerate_tokens(tp, needle="tok_"):
    """[(text, [rects])] for every token in the document, via FindText from the
    end of the previous hit. Case-insensitive so lowercased tokens are found too."""
    out = []
    rng = tp.DocumentRange
    search = rng.Clone()
    for _ in range(500):
        hit = search.FindText(needle, False, True)
        if hit is None:
            break
        # extend the hit to the token's end: move the end forward by characters while it still matches
        tok = hit.Clone()
        for _ in range(40):
            probe = tok.Clone()
            if probe.MoveEndpointByUnit(auto.TextPatternRangeEndpoint.End, auto.TextUnit.Character, 1, waitTime=0) != 1:
                break
            t = probe.GetText(-1)
            if not TOKEN_RE.match(t) and not re.match(r"TOK_[A-Z0-9_]*$", t, re.IGNORECASE):
                break
            tok = probe
        text = tok.GetText(-1)
        rects = [(r.left, r.top, r.right, r.bottom) for r in tok.GetBoundingRectangles()]
        out.append((text, rects))
        # continue after this hit
        search.MoveEndpointByRange(auto.TextPatternRangeEndpoint.Start, tok, auto.TextPatternRangeEndpoint.End, waitTime=0)
    return out


# ------------------------------------------------------------------ phases
def main() -> int:
    with open(REPORT, "a", encoding="utf-8") as fh:
        fh.write("\n\n")
    section(f"Maskroom UIA reply probe  {_dt.datetime.now():%Y-%m-%d %H:%M}")
    log(f"  python {sys.version.split()[0]}  uiautomation {getattr(auto, 'VERSION', '?')}")

    section("PHASE 1 - window and text documents")
    ask("Claude Desktop is open on a chat whose reply contains TOK_ tokens? Press Enter.")
    win = find_claude_window()
    if win is None:
        log("  Claude window not found (class Chrome_WidgetWin_1 with 'Claude' in the title).")
        return 2
    log(f"  window: name={win.Name!r} rect={win.BoundingRectangle}")
    docs, dt = timed("walk tree for TextPattern elements", lambda: find_text_documents(win))
    if not docs:
        log("  no element with TextPattern found")
        return 2
    for c, tp, n, depth in docs:
        log(f"    - {c.ControlTypeName:<16} depth={depth} name={(c.Name or '')[:40]!r} class={c.ClassName[:40]!r} textlen={n}")
    # pick the one with the most text
    doc, tp, n, _ = max(docs, key=lambda d: d[2])
    log(f"  using: {doc.ControlTypeName} name={(doc.Name or '')[:40]!r} textlen={n}")

    section("PHASE 2 - read the document")
    text, dt = timed("DocumentRange.GetText(-1)", lambda: tp.DocumentRange.GetText(-1))
    text = text or ""
    log(f"  {len(text)} chars; {len(TOKEN_RE.findall(text))} token-like strings by regex")
    log(f"  first 300 chars: {text[:300]!r}")

    section("PHASE 3 - enumerate token ranges and rectangles")
    toks, dt = timed("enumerate_tokens (FindText loop)", lambda: enumerate_tokens(tp))
    toks = toks or []
    log(f"  {len(toks)} ranges; {dt / max(1, len(toks)):.0f} ms per token")
    for t, rects in toks[:12]:
        log(f"    {t!r:<34} rects={rects}")
    if len(toks) > 12:
        log(f"    ... {len(toks) - 12} more")

    section("PHASE 4 - visible ranges")
    vis, dt = timed("GetVisibleRanges", lambda: tp.GetVisibleRanges())
    if vis:
        log(f"  {len(vis)} visible range(s)")
        for r in vis[:3]:
            t = r.GetText(200)
            log(f"    len={len(r.GetText(-1))} rects={[(x.left, x.top, x.right, x.bottom) for x in r.GetBoundingRectangles()][:3]} text={t[:80]!r}")

    section("PHASE 5 - range under the mouse")
    ask("Hover the mouse over a TOK_ token in the reply, keep it still, then press Enter here.")
    x, y = auto.GetCursorPos()
    log(f"  cursor at ({x},{y})")
    rng, dt = timed("RangeFromPoint", lambda: tp.RangeFromPoint(x, y))
    if rng is not None:
        w = rng.Clone(); w.ExpandToEnclosingUnit(auto.TextUnit.Word, waitTime=0)
        log(f"  word: {w.GetText(-1)!r} rects={[(r.left, r.top, r.right, r.bottom) for r in w.GetBoundingRectangles()]}")
        ln = rng.Clone(); ln.ExpandToEnclosingUnit(auto.TextUnit.Line, waitTime=0)
        log(f"  line: {ln.GetText(-1)[:120]!r}")
    ctl, dt = timed("ControlFromPoint", lambda: auto.ControlFromPoint(x, y))
    if ctl is not None:
        log(f"  element: {ctl.ControlTypeName} name={(ctl.Name or '')[:60]!r} class={ctl.ClassName[:40]!r}")

    section("PHASE 6 - scroll")
    if toks:
        before = toks[0][1]
        ask("Scroll the chat up or down a little (mouse wheel), then press Enter here.")
        toks2, dt = timed("re-enumerate after scroll", lambda: enumerate_tokens(tp))
        if toks2:
            log(f"  first token rects before={before} after={toks2[0][1]}")

    section("PHASE 7 - streaming")
    ask("In Claude, ask for a long reply that repeats the tokens several times, press send, then press Enter here at once.")
    t_end = time.time() + 15
    last_len = -1
    while time.time() < t_end:
        t0 = time.perf_counter()
        try:
            n = len(tp.DocumentRange.GetText(-1) or "")
            k = len(enumerate_tokens(tp))
            dt = (time.perf_counter() - t0) * 1000
            log(f"  t={15 - (t_end - time.time()):4.1f}s textlen={n:6d} ({'+' if n > last_len else '='}) tokens={k:3d} cost={dt:.0f} ms")
            last_len = n
        except Exception as e:  # noqa: BLE001
            log(f"  error during streaming sample: {type(e).__name__}: {e}")
        time.sleep(0.5)

    section("DONE - paste uia_reply_probe_report.txt back")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("  aborted")
        sys.exit(130)
