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
import queue
import sys
import types
from pathlib import Path

import pytest

CHAR_W, LINE_H, TEXT_X = 8, 18, 100      # the fake's screen geometry
COMPOSER = (100, 690, 900, 730)
WINDOW = (0, 90, 1000, 440)


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

    def RangeFromPoint(self, x, y):
        """Honest geometry: the character under the point. Hit testing is the
        primitive the real Chromium got right, so the fake keeps it exact."""
        for i, (text, ly) in enumerate(self.lines):
            if ly <= y <= ly + LINE_H:
                ci = max(0, min(len(text) - 1, (x - TEXT_X) // CHAR_W))
                return FakeRange(self.lines, i, ci, ci + 1, drift=self.drift)
        return None


class FakeWin:
    def __init__(self, rect):
        self.BoundingRectangle = Rect(*rect)


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
    m.ControlFromPoint = lambda x, y: None
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
        ["Here is the summary you asked for", 100],
        ["Patient: TOK_PERSON_2615D96E admitted", 120],
        ["Account TOK_FINANCIAL_ACCOUNT_52940232 is overdue", 140],
        ["No identifiers on this line at all", 160],
        ["Below the fold: TOK_PERSON_AAAA1111", 460],      # under the window bottom
        ["composer draft TOK_PERSON_2615D96E here", 700],  # inside the composer
    ]
    vault = {"TOK_PERSON_2615D96E": "Nimal Perera",
             "TOK_FINANCIAL_ACCOUNT_52940232": "8801-2233-9",
             "TOK_PERSON_AAAA1111": "Should Not Appear"}

    monkeypatch.setattr(helper, "log", lambda m: None)
    monkeypatch.setattr(helper, "screen_pixel", lambda x, y: "#ffffff")
    monkeypatch.setattr(helper, "foreground_exe", lambda: helper.CLAUDE_EXE)
    helper.SHARED["index"] = helper.TokenIndex(vault)
    helper.SHARED["composer_rect"] = COMPOSER

    def build(drift=0):
        worker = helper.OverlayWorker({"overlay": True}, queue.Queue())
        worker.doc, worker.win = FakeDoc(lines, drift=drift), FakeWin(WINDOW)
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
    text = overlay.lines[1][0]
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
    overlay.lines[1][0] = "Patient: TOK_PERSON_99999999 admitted"
    overlay.lines[1][1] -= 10                      # and it moved, so the text is re-checked
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
    text = overlay.lines[1][0]
    assert item["rect"][0] == TEXT_X + text.index(item["tok"]) * CHAR_W


def test_a_token_no_vault_knows_is_reported(overlay, monkeypatch):
    """Silence was the whole problem: a token on screen that no vault holds now
    says so, which separates a lookup miss from a placement failure."""
    said = []
    monkeypatch.setattr(overlay.helper, "log", said.append)
    helper = overlay.helper
    helper.SHARED["index"] = helper.TokenIndex({"TOK_PERSON_2615D96E": "Nimal Perera"})
    w = overlay.build()
    w.walk(w.doc, helper.SHARED["index"])
    assert [it["tok"] for it in w.items] == ["TOK_PERSON_2615D96E"]
    assert any("TOK_FINANCIAL_ACCOUNT_52940232" in m and "no known session vault" in m
               for m in said), said
