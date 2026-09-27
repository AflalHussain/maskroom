"""The desktop helper's overlay: does it find tokens in Claude's replies and put
the patches in the right place?

The Windows helper (`desktop/helper.py`) is UI Automation code, but the part that
went wrong in practice is ordinary logic: the first version located every token by
moving a text-range endpoint thousands of characters from the start of the
document, which measured 400 ms per token and 30 s for one real chat. The
replacement walks the *visible lines* and positions each token inside its own
line. That is what this module pins down, against a fake accessibility layer, so
it stays checkable on any machine.

Covered: tokens on visible lines are placed, ones below the fold and ones in the
composer are not, the rectangle covers the token rather than the line, a
character-offset drift is recovered by nudging, the font style is read once, and
a scroll is reported as movement.
"""
import os
import queue
import sys
import time
import types
from pathlib import Path

import pytest

CHAR_W, LINE_H, TEXT_X = 8, 18, 100      # the fake's screen geometry
COMPOSER = (100, 690, 900, 730)
WINDOW = (0, 0, 1000, 740)        # the whole Claude window: header, chat, composer
BAND = (0, 90, 1000, 640)         # where conversation text may legitimately be painted


class Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def __repr__(self):
        return f"Rect({self.left},{self.top},{self.right},{self.bottom})"


class _TextUnit:
    Character, Format, Word, Line, Paragraph, Page, Document = range(7)


class _Endpoint:
    Start, End = 0, 1


class _PatternId:
    TextPattern, ValuePattern = 10014, 10002


class _AttrId:
    FontNameAttribute, FontSizeAttribute, FontWeightAttribute = 40005, 40006, 40007
    IsItalicAttribute, ForegroundColorAttribute = 40014, 40008


ATTRS = {40005: "Anthropic Sans Variable Text", 40006: 11.5,
         40007: 400, 40014: False, 40008: 0x0B0B0B}


class FakeRange:
    """A range over `lines[li]`.

    `drift` models the real defect: moving an endpoint by N characters does not
    land N characters into the string GetText returns, because a list marker or
    a formatting run counts differently. Screen geometry stays honest, which is
    what the hit-test fallback relies on.
    """

    def __init__(self, lines, li, start=0, end=None, drift=0):
        self.lines, self.li, self.start, self.drift = lines, li, start, drift
        self.end = len(lines[li][0]) if end is None else end

    def Clone(self):
        return FakeRange(self.lines, self.li, self.start, self.end, self.drift)

    def GetText(self, n=-1):
        return self.lines[self.li][0][self.start:self.end]

    def GetBoundingRectangles(self):
        _text, y = self.lines[self.li]
        if self.start >= self.end:
            return []
        return [Rect(TEXT_X + self.start * CHAR_W, y,
                     TEXT_X + self.end * CHAR_W, y + LINE_H)]

    def ExpandToEnclosingUnit(self, unit, waitTime=0):
        text = self.lines[self.li][0]
        if unit == _TextUnit.Line:
            self.start, self.end = 0, len(text)
        elif unit == _TextUnit.Word:
            i = min(self.start, max(0, len(text) - 1))
            while i > 0 and not text[i - 1].isspace():
                i -= 1
            j = max(i, self.end)
            while j < len(text) and not text[j].isspace():
                j += 1
            self.start, self.end = i, j
        return True

    def Move(self, unit, count, waitTime=0):
        if unit != _TextUnit.Line:
            return 0
        nxt = self.li + count
        if not 0 <= nxt < len(self.lines):
            return 0
        self.li, self.start, self.end = nxt, 0, len(self.lines[nxt][0])
        return count

    def MoveEndpointByUnit(self, endpoint, unit, count, waitTime=0):
        if unit != _TextUnit.Character:
            return 0
        length = len(self.lines[self.li][0])
        if endpoint == _Endpoint.Start:
            new = self.start + count - self.drift
            if new > length:
                return length - self.start
            self.start = max(0, new)
            self.end = max(self.end, self.start)
        else:
            new = self.end + count
            if new > length:
                return length - self.end
            self.end = max(self.start, new)
        return count

    def MoveEndpointByRange(self, endpoint, other, other_endpoint, waitTime=0):
        pos = other.start if other_endpoint == _Endpoint.Start else other.end
        if endpoint == _Endpoint.Start:
            self.start = pos
        else:
            self.end = pos
        return True

    def GetAttributeValue(self, aid):
        return ATTRS.get(aid)


class FakeDoc:
    def __init__(self, lines, drift=0):
        self.lines, self.drift = lines, drift

    @property
    def DocumentRange(self):
        return FakeRange(self.lines, 0, drift=self.drift)

    def ControlFromPoint(self, x, y):
        for (l, t, r, b), name in PANELS:
            if l <= x <= r and t <= y <= b:
                return FakeControl(name)
        for text, ly in self.lines:
            if ly <= y <= ly + LINE_H:
                return FakeControl(text)
        return None

    def RangeFromPoint(self, x, y):
        """Honest geometry: the character under the point, and for a point in a
        gap the nearest line, as a real hit test gives. Hit testing is the
        primitive Chromium got right, so the fake keeps it exact."""
        if not self.lines:
            return None
        i = min(range(len(self.lines)),
                key=lambda j: abs((self.lines[j][1] + LINE_H / 2) - y))
        text = self.lines[i][0]
        ci = max(0, min(len(text) - 1, (x - TEXT_X) // CHAR_W))
        return FakeRange(self.lines, i, ci, ci + 1, drift=self.drift)


class FakeWin:
    def __init__(self, rect):
        self.BoundingRectangle = Rect(*rect)


class FakeControl:
    """What a screen-point hit returns: either conversation text, whose name
    carries the line, or a floating panel, whose name is its own."""

    def __init__(self, name, parent=None):
        self.Name, self._parent = name, parent

    def GetParentControl(self):
        return self._parent


PANELS = [                       # float over the conversation, which scrolls under them
    ((0, 0, 1000, 90), "Overdue loan account reminders"),        # the header
    ((100, 580, 1000, 630), "You've used 75% of your weekly limit"),   # a usage banner
    ((100, 690, 900, 730), "Reply"),                             # the composer
]


def _fake_uia():
    m = types.ModuleType("uiautomation")
    m.Rect, m.TextUnit, m.TextPatternRangeEndpoint = Rect, _TextUnit, _Endpoint
    m.PatternId, m.TextAttributeId = _PatternId, _AttrId
    m.PatternIdNames = {}
    m.GetCursorPos = lambda: (0, 0)
    m.GetClipboardText = lambda: ""
    m.SetClipboardText = lambda t: True
    m.SendKeys = lambda *a, **k: None
    m.GetFocusedControl = lambda: None
    m.GetRootControl = lambda: None
    m.ControlFromPoint = lambda x, y: None      # replaced per test by the fake page
    m.UIAutomationInitializerInThread = lambda *a, **k: None
    return m


@pytest.fixture
def overlay(monkeypatch):
    """An OverlayWorker wired to a fake page, plus the mutable line list."""
    sys.modules.setdefault("uiautomation", _fake_uia())
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "desktop"))
    helper = pytest.importorskip("helper")

    lines = [
        ["Scrolled above the chat: TOK_PERSON_AAAA1111", 40],   # behind the header
        ["Here is the summary you asked for", 100],
        ["Patient: TOK_PERSON_2615D96E admitted", 120],
        ["Account TOK_FINANCIAL_ACCOUNT_52940232 is overdue", 140],
        ["No identifiers on this line at all", 160],
        ["Behind the composer: TOK_PERSON_AAAA1111", 660],      # the chat scrolls under it
        ["composer draft TOK_PERSON_2615D96E here", 700],       # in the composer itself
    ]
    vault = {"TOK_PERSON_2615D96E": "Nimal Perera",
             "TOK_FINANCIAL_ACCOUNT_52940232": "8801-2233-9",
             "TOK_PERSON_AAAA1111": "Should Not Appear"}

    monkeypatch.setattr(helper, "log", lambda m: None)
    monkeypatch.setattr(helper, "screen_pixel", lambda x, y: "#ffffff")
    monkeypatch.setattr(helper, "foreground_exe", lambda: helper.CLAUDE_EXE)
    helper.SHARED["index"] = helper.TokenIndex(vault)
    helper.SHARED["composer_rect"] = COMPOSER

    def build(drift=0, clip=BAND):
        worker = helper.OverlayWorker({"overlay": True}, queue.Queue())
        worker.doc, worker.win = FakeDoc(lines, drift=drift), FakeWin(WINDOW)
        worker.clip, worker.clip_at = clip, float("inf")   # pin the band; tick() recomputes it
        monkeypatch.setattr(helper.auto, "ControlFromPoint", worker.doc.ControlFromPoint)
        return worker

    ns = types.SimpleNamespace(helper=helper, lines=lines, build=build)
    ns.worker = build()
    return ns


def test_only_visible_tokens_outside_the_composer_are_placed(overlay):
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert sorted(it["tok"] for it in w.items) == [
        "TOK_FINANCIAL_ACCOUNT_52940232", "TOK_PERSON_2615D96E"]
    assert [it["value"] for it in w.items if it["tok"].startswith("TOK_PERSON")] == ["Nimal Perera"]


def test_the_rectangle_covers_the_token_not_the_line(overlay):
    """The offset drift must be recovered, or the patch lands on the wrong words."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    item = next(i for i in w.items if i["tok"] == "TOK_PERSON_2615D96E")
    text = overlay.lines[2][0]
    left = 100 + text.index(item["tok"]) * 8
    assert item["rect"][0] == left
    assert item["rect"][2] == left + len(item["tok"]) * 8


def test_style_is_read_for_each_token(overlay):
    """Per token, not once per reply: a bold token has to be painted bold."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert w.items
    for it in w.items:
        assert it["style"]["size"] == 11.5
        assert it["style"]["family"] == "Anthropic Sans Variable Text"
        assert it["style"]["fg"] == "#0b0b0b"
        assert it["style"]["italic"] is False


def test_frame_carries_one_entry_per_visible_token(overlay):
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    w.emit_frame()
    msg = w.events.get_nowait()
    assert msg["type"] == "overlay" and len(msg["items"]) == 2
    for rect, value, bg, style in msg["items"]:
        assert len(rect) == 4 and value and bg == "#ffffff" and style["size"] == 11.5


def test_a_scroll_is_reported_as_movement(overlay):
    """Movement is what makes the overlay follow a scroll and re-walk after it."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert w.refresh_rects() is False
    for line in overlay.lines:
        line[1] -= 40
    assert w.refresh_rects() is True
    assert [it["rect"][1] for it in w.items] == [80, 100]


def test_nothing_is_drawn_without_a_vault(overlay):
    helper = overlay.helper
    helper.SHARED["index"] = helper.TokenIndex({})
    w = overlay.worker
    w.visible = True
    w.tick()
    assert w.events.get_nowait()["items"] is None


def test_a_range_that_no_longer_holds_its_token_is_not_drawn(overlay):
    """Drawing one person's name over another's token is worse than drawing
    nothing, so a moved range is re-checked against the token it was placed on."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert all(it["rect"] for it in w.items)
    # The line is edited under us: same geometry, different text.
    overlay.lines[2][0] = "Patient: TOK_PERSON_99999999 admitted"
    overlay.lines[2][1] -= 10                      # and it moved, so the text is re-checked
    w.refresh_rects()
    stale = next(i for i in w.items if i["tok"] == "TOK_PERSON_2615D96E")
    assert stale["rect"] is None
    w.emit_frame()
    drawn = w.events.get_nowait()["items"]
    assert [v for _r, v, _b, _s in drawn] == ["8801-2233-9"]


def test_tokens_are_placed_when_offsets_disagree_with_the_text(overlay):
    """The bold-token failure: Chromium's character offsets ran against the line
    text, counting landed on the wrong characters, and the token went unpainted.
    Hit testing has to recover it."""
    w = overlay.build(drift=9)          # far beyond any fixed nudge
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert sorted(it["tok"] for it in w.items) == [
        "TOK_FINANCIAL_ACCOUNT_52940232", "TOK_PERSON_2615D96E"]
    item = next(i for i in w.items if i["tok"] == "TOK_PERSON_2615D96E")
    text = overlay.lines[2][0]
    assert item["rect"][0] == TEXT_X + text.index(item["tok"]) * CHAR_W


def test_a_token_no_vault_knows_is_reported(overlay, monkeypatch):
    """Silence was the whole problem: a token on screen that no vault holds is
    named in the walk summary, which separates a lookup miss from a placement
    failure. It goes in *every* walk line, not once per token, because a warning
    that fires once has already scrolled away by the time anyone reads the log."""
    said = []
    monkeypatch.setattr(overlay.helper, "log", said.append)
    helper = overlay.helper
    helper.SHARED["index"] = helper.TokenIndex({"TOK_PERSON_2615D96E": "Nimal Perera"})
    w = overlay.build()
    w.walk(w.doc, helper.SHARED["index"])
    assert [it["tok"] for it in w.items] == ["TOK_PERSON_2615D96E"]
    assert any("TOK_FINANCIAL_ACCOUNT_52940232" in m and "NOT IN ANY VAULT" in m
               for m in said), said


def test_nothing_is_painted_outside_the_conversation_band(overlay):
    """Tokens scrolled above the chat still report a rectangle, and the chat
    scrolls *under* the composer. Painting either put values over the header and
    over the reply box."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert all(it["tok"] != "TOK_PERSON_AAAA1111" for it in w.items), \
        "a line outside the band was walked"
    w.emit_frame()
    for rect, _value, _bg, _style in w.events.get_nowait()["items"]:
        assert rect[1] >= BAND[1] and rect[3] <= BAND[3], rect


def test_a_patch_straddling_the_band_edge_is_dropped(overlay):
    """Half a patch over the header is worse than none, so containment has to be
    total rather than an overlap."""
    w = overlay.worker
    w.walk(w.doc, overlay.helper.SHARED["index"])
    assert w.items
    w.items[0]["rect"] = (200, BAND[1] - 4, 400, BAND[1] + 14)   # crosses the top edge
    w.emit_frame()
    drawn = w.events.get_nowait()["items"] or []
    assert all(r[1] >= BAND[1] for r, *_ in drawn)
    assert len(drawn) == len(w.items) - 1


def test_every_walk_reports_its_own_outcome(overlay):
    """A log fragment has to be conclusive on its own."""
    w = overlay.worker
    said = []
    overlay.helper.log, saved = said.append, overlay.helper.log
    try:
        w.walk(w.doc, overlay.helper.SHARED["index"])
        w.walk(w.doc, overlay.helper.SHARED["index"])
    finally:
        overlay.helper.log = saved
    summaries = [m for m in said if "walked" in m]
    assert len(summaries) == 2
    for m in summaries:
        assert "with tokens)" in m and "placed 2" in m


def test_a_token_under_a_floating_panel_is_not_painted(overlay):
    """The header, a usage banner and the composer float over the conversation,
    which keeps scrolling under them, so they sit inside the scroller's own
    rectangle and no band can exclude them. Asking what is on top does."""
    helper = overlay.helper
    overlay.lines.append(["Behind the usage banner: TOK_PERSON_2615D96E", 595])
    w = overlay.build(clip=(0, 0, 1000, 740))      # deliberately no band at all
    w.walk(w.doc, helper.SHARED["index"])
    painted = [(it["tok"], it["rect"][1]) for it in w.items]
    assert all(y != 595 for _tok, y in painted), painted
    assert any(y == 120 for _tok, y in painted), "visible tokens must still be painted"


def test_covered_tokens_are_reported_apart_from_unplaced_ones(overlay):
    """They need different fixes, so the log must not conflate them."""
    helper = overlay.helper
    overlay.lines.append(["Behind the usage banner: TOK_PERSON_2615D96E", 595])
    said = []
    helper.log, saved = said.append, helper.log
    try:
        w = overlay.build(clip=(0, 0, 1000, 740))
        w.walk(w.doc, helper.SHARED["index"])
    finally:
        helper.log = saved
    summary = next(m for m in said if "walked" in m)
    assert "behind a panel" in summary, summary
    assert "UNPLACED" not in summary, summary      # a covered token is not a placement fault


# --------------------------------------------------------------------- file dialog
@pytest.fixture
def automation(monkeypatch):
    sys.modules.setdefault("uiautomation", _fake_uia())
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "desktop"))
    helper = pytest.importorskip("helper")
    return helper.Automation.__new__(helper.Automation)


def test_a_picked_name_without_its_extension_is_resolved(automation, tmp_path):
    """What the dialog hands over is a *display* name: Explorer hides known
    extensions, so a picked .xlsx arrives as a bare stem, relative to a folder
    the dialog does not spell out either."""
    real = tmp_path / "hr_leave_register_aug2026.xlsx"
    real.write_bytes(b"x")
    assert automation.resolve_pick("hr_leave_register_aug2026", str(tmp_path)) == str(real)
    assert automation.resolve_pick("hr_leave_register_aug2026.xlsx", str(tmp_path)) == str(real)
    assert automation.resolve_pick(str(real), "") == str(real)


def test_a_name_that_cannot_be_found_resolves_to_nothing(automation, tmp_path):
    """Which has to be held rather than waved through: an unmaskable file
    reaching Claude is the disclosure the guard exists to stop."""
    assert automation.resolve_pick("does_not_exist", str(tmp_path)) is None
    assert automation.resolve_pick("anything", "") is None
    assert automation.resolve_pick(str(tmp_path / "gone.xlsx"), "") is None


def test_a_name_with_glob_characters_is_taken_literally(automation, tmp_path):
    (tmp_path / "report[2026].xlsx").write_bytes(b"x")
    (tmp_path / "reportX.xlsx").write_bytes(b"x")
    assert automation.resolve_pick("report[2026]", str(tmp_path)) == str(tmp_path / "report[2026].xlsx")


def test_a_folder_is_recognised_in_an_accessible_value(automation):
    """Breadcrumbs carry it with a prefix, or not at all."""
    assert automation.looks_like_path("Address: C:\\Users\\a\\Documents") == "C:\\Users\\a\\Documents"
    assert automation.looks_like_path("C:\\Users\\a\\Documents") == "C:\\Users\\a\\Documents"
    assert automation.looks_like_path("\\\\server\\share\\docs") == "\\\\server\\share\\docs"
    assert automation.looks_like_path("Documents") == ""
    assert automation.looks_like_path("") == ""


# --------------------------------------------------------------------- downloads
@pytest.fixture
def watcher(automation, tmp_path):
    """An Automation wired to a fake server and a temporary Downloads folder."""
    helper = sys.modules["helper"]
    calls = {}

    class FakeFiles:
        def unmask(self, path, fields):
            calls["unmask"] = (path, fields)
            return {"ok": True, "status": 200, "error": None,
                    "data": {"run_id": "r1", "restored": 3, "unresolved": [],
                             "downloads": {"output": "restored" + Path(path).suffix}}}

        def download(self, run_id, name):
            return b"restored bytes", None

    automation.cfg = {"downloadsDir": str(tmp_path), "sessionId": "s1", "watchDownloads": True}
    automation.files = FakeFiles()
    automation.downloads_at = 0.0
    automation.downloads_done = set()
    automation.downloads_size = {}
    automation.emit = lambda **kw: None
    automation.toast = lambda msg, error=False: calls.setdefault("toasts", []).append(msg)
    helper.log = lambda m: None
    return types.SimpleNamespace(a=automation, dir=tmp_path, calls=calls, helper=helper)


def test_only_files_that_hold_tokens_are_sent(watcher):
    plain = watcher.dir / "notes.md"
    plain.write_text("nothing to restore here")
    tokened = watcher.dir / "reply.md"
    tokened.write_text("Call TOK_PERSON_2615D96E today")
    book = watcher.dir / "sheet.xlsx"
    book.write_bytes(b"PK\\x03\\x04 zipped, tokens are deflated out of sight")
    assert watcher.a.holds_tokens(plain) is False
    assert watcher.a.holds_tokens(tokened) is True
    assert watcher.a.holds_tokens(book) is True


def test_a_download_is_restored_beside_itself_and_the_original_is_kept(watcher):
    """Deleting a file the user downloaded is their call, not ours, so both are
    kept unless they ask otherwise."""
    src = watcher.dir / "reply.md"
    src.write_text("Call TOK_PERSON_2615D96E today")
    watcher.a.restore_download(src)
    out = watcher.dir / "reply_restored.md"
    assert out.read_bytes() == b"restored bytes"
    assert src.exists()
    assert watcher.calls["unmask"][1] == {"session_id": "s1"}


def test_the_token_copy_is_removed_only_when_asked(watcher):
    watcher.a.cfg["deleteTokenCopy"] = True
    src = watcher.dir / "reply.md"
    src.write_text("Call TOK_PERSON_2615D96E today")
    watcher.a.restore_download(src)
    assert (watcher.dir / "reply_restored.md").exists()
    assert not src.exists()


def test_the_watcher_leaves_alone_what_it_should(watcher, monkeypatch):
    """Files that were already there, ones it made itself, and types the server
    cannot restore. A PDF is redacted, not tokenised: there is nothing to put back."""
    sent = []
    monkeypatch.setattr(watcher.a, "restore_download", lambda p: sent.append(p.name))
    old = watcher.dir / "old.md"
    old.write_text("TOK_PERSON_2615D96E")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    (watcher.dir / "reply_restored.md").write_text("TOK_PERSON_2615D96E")
    (watcher.dir / "report.pdf").write_bytes(b"%PDF- TOK_PERSON_2615D96E")
    fresh = watcher.dir / "fresh.md"
    fresh.write_text("TOK_PERSON_2615D96E")
    watcher.a.poll_downloads()                     # first look records the size
    watcher.a.downloads_at = 0.0
    watcher.a.poll_downloads()                     # second look: unchanged, so it has landed
    assert sent == ["fresh.md"], sent


def test_a_file_still_being_written_waits_for_the_next_look(watcher, monkeypatch):
    sent = []
    monkeypatch.setattr(watcher.a, "restore_download", lambda p: sent.append(p.name))
    growing = watcher.dir / "big.csv"
    growing.write_text("TOK_PERSON_2615D96E,1")
    watcher.a.poll_downloads()                     # first sight: size recorded, nothing sent
    assert sent == []
    growing.write_text("TOK_PERSON_2615D96E,1\\nmore rows arriving")
    watcher.a.downloads_at = 0.0
    watcher.a.poll_downloads()                     # grew: still waiting
    assert sent == []
    watcher.a.downloads_at = 0.0
    watcher.a.poll_downloads()                     # stable now
    assert sent == ["big.csv"]


# --------------------------------------------------------------- the file guard
class FakeDialogPart:
    def __init__(self, name="", rect=(0, 0, 100, 20)):
        self.Name = name
        self.AutomationId = ""
        self.BoundingRectangle = Rect(*rect)


@pytest.fixture
def guard(automation, monkeypatch):
    """An Automation with a stubbed file dialog, for the arm/hold decisions."""
    helper = sys.modules["helper"]
    said, toasts = [], []
    monkeypatch.setattr(helper, "log", said.append)
    helper.SHARED["dialog_open"] = False
    automation.cfg = {"fileGuard": True}
    automation.dialog = automation.dialog_edit = automation.dialog_confirm = None
    automation.dialog_list = None
    automation.dialog_seen = 0.0
    automation.dialog_said = ""
    automation.dialog_dumped = True          # skip the contents dump in tests
    automation.toast = lambda msg, error=False: toasts.append((msg, error))
    automation.emit = lambda **kw: None
    return types.SimpleNamespace(a=automation, helper=helper, said=said, toasts=toasts,
                                 part=FakeDialogPart)


def test_a_dialog_it_cannot_read_stays_armed_and_holds(guard, monkeypatch):
    """The fail-open that shipped: a missing File name box disarmed the guard,
    so the user's original file was attached. Observed twice in a real log."""
    dlg = FakeDialogPart("Open")
    monkeypatch.setattr(guard.a, "find_dialog", lambda: dlg)
    monkeypatch.setattr(guard.a, "dialog_parts",
                        lambda d: (None, FakeDialogPart("Open"), None))   # button, no box
    guard.a.poll_dialog()
    assert guard.helper.SHARED["dialog_open"] is True, "the guard must stay armed"

    replayed = []
    monkeypatch.setattr(guard.a, "dialog_replay", lambda d, c: replayed.append(True))
    guard.a.cmd_dialog_confirm(time.time())
    assert replayed == [], "a dialog it cannot read must not be let through"
    assert any("cannot be masked" in m.lower() for m, _err in guard.toasts), guard.toasts


def test_a_save_dialog_is_left_alone(guard, monkeypatch):
    """No Open button means nothing is being attached, so there is nothing to
    guard and holding would break the user's downloads."""
    monkeypatch.setattr(guard.a, "find_dialog", lambda: FakeDialogPart("blob:https://claude.ai/x"))
    monkeypatch.setattr(guard.a, "dialog_parts", lambda d: (None, None, None))
    guard.a.poll_dialog()
    assert guard.helper.SHARED["dialog_open"] is False


def test_a_readable_dialog_arms_normally(guard, monkeypatch):
    monkeypatch.setattr(guard.a, "find_dialog", lambda: FakeDialogPart("Open"))
    monkeypatch.setattr(guard.a, "dialog_parts",
                        lambda d: (FakeDialogPart("File name:"), FakeDialogPart("Open"), None))
    guard.a.poll_dialog()
    assert guard.helper.SHARED["dialog_open"] is True
    assert guard.a.dialog_edit is not None
    assert any("will be masked" in m for m, _err in guard.toasts)


def test_a_closed_dialog_disarms_and_forgets_its_parts(guard, monkeypatch):
    monkeypatch.setattr(guard.a, "find_dialog", lambda: FakeDialogPart("Open"))
    monkeypatch.setattr(guard.a, "dialog_parts",
                        lambda d: (FakeDialogPart("File name:"), FakeDialogPart("Open"), None))
    guard.a.poll_dialog()
    monkeypatch.setattr(guard.a, "find_dialog", lambda: None)
    guard.a.dialog_seen = 0.0
    guard.a.poll_dialog()
    assert guard.helper.SHARED["dialog_open"] is False
    assert guard.a.dialog_edit is None and guard.a.dialog_confirm is None


# ------------------------------------------------------------ worker liveness
def test_the_hook_holds_when_the_worker_has_stopped_answering(automation, monkeypatch):
    """A dead worker used to leave the hook swallowing Enter forever, with no
    message: the Enter key simply stopped working in Claude."""
    helper = sys.modules["helper"]
    said, events = [], queue.Queue()
    monkeypatch.setattr(helper, "log", said.append)
    hooks = helper.Hotkeys.__new__(helper.Hotkeys)
    hooks.cfg, hooks.events, hooks.swallow_up = {}, events, False

    helper.SHARED["worker_beat"] = time.time()
    assert hooks.worker_alive() is True

    helper.SHARED["worker_beat"] = time.time() - helper.WORKER_DEAD_S - 1
    helper.SHARED["alarm"] = ""
    assert hooks.worker_alive() is False
    assert hooks.guard_failed(0, 0, 0) == 1, "default is to hold, not to let it through"
    assert hooks.swallow_up is True
    assert events.get_nowait()["what"] == "worker"
    assert any("stopped answering" in m for m in said), said


def test_the_hold_or_warn_choice_is_the_customers(automation, monkeypatch):
    """Holding every Enter makes Claude unusable and the user kills the helper,
    which protects nobody, so which way this goes is a policy setting."""
    helper = sys.modules["helper"]
    monkeypatch.setattr(helper, "log", lambda m: None)
    # _user32 is None off Windows; warn mode chains to the next hook through it
    monkeypatch.setattr(helper, "_user32", types.SimpleNamespace(CallNextHookEx=lambda *a: 0))
    hooks = helper.Hotkeys.__new__(helper.Hotkeys)
    hooks.cfg, hooks.events, hooks.swallow_up = {"onGuardFailure": "warn"}, queue.Queue(), False
    helper.SHARED["alarm"] = ""
    assert hooks.guard_failed(0, 0, 0) == 0, "warn mode passes the key through"
    assert hooks.swallow_up is False
    assert hooks.events.get_nowait()["what"] == "worker"
