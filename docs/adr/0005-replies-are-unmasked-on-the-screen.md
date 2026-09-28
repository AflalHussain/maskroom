---
status: accepted
date: 2026-09-28
---

# A reply's real values are shown on the screen, never written back into Claude

A masked conversation reads in tokens, so the user has to be able to see what
`TOK_PERSON_3669FE1A` stands for. We decided the helper never edits a reply: it shows the
real values *over* Claude, on its own windows, and what Claude holds stays tokenised.

Three surfaces do this, all read-only: a tooltip when the mouse rests on a token, the
clipboard restored on copy, and — behind a toggle — an overlay that paints each value on a
transparent click-through window positioned over the token. The vault is cached locally and
restoration uses the same tolerant rules as the extension's `tokens.js`, so an unknown token
is left alone rather than guessed at.

The overlay is a trial, and stays behind a toggle the user can switch off on the bar. Its
design is dictated by what the accessibility API costs: locating a token from the start of
the document measured 400 ms per token and 30 seconds for one real chat, so it walks the
*visible lines* instead, where a token's offset inside its own line is small. Chromium's
character offsets disagree with the string it returns across formatting runs, by no fixed
amount, so a token that fails read-back is placed by hit testing, the one primitive measured
as exact. Painting is clipped to the conversation viewport and each token is asked what is on
top of it, because Claude's header, banners and composer float over a scroller that keeps
scrolling underneath them.

## Considered options

- **Rewrite the reply in place**, as the values arrive. The accessibility tree exposes a
  reply's text as read-only, so this is not available; were it available it would be wrong,
  because the next message Claude sends would then carry real values in its context.
- **Restore on copy only**, and show nothing on screen. This is the honest minimum and is
  what ships when the overlay is off. Rejected as the whole answer: a user reading a reply
  full of tokens cannot follow it, and will paste it into a text editor to read it, which is
  a worse outcome than showing it over the window.
- **Screenshot-and-redraw the conversation area.** Rejected in research: it needs continuous
  capture of the user's screen, which is a far heavier privacy claim than reading one
  window's text, and it cannot reflow text it did not lay out.
- **A side panel listing token → value.** Cheap and robust, and still worth having. Rejected
  as the primary because reading a reply then means reading two things at once.

## Consequences

- Nothing the helper shows can leak back into the conversation. This is the property that
  makes the feature safe, and it is worth defending against future convenience features.
- What is on screen and what Claude holds differ, which a user has to understand. A dotted
  underline marks a restored value in the overlay, and the tooltip is transient by nature.
- The overlay tracks scrolling by re-reading rectangles every 60 ms and can lag visibly on a
  slow machine; *Hide the overlay while scrolling* is the escape hatch, and switching it off
  entirely loses nothing but convenience.
- The real values are painted onto the screen, so they would appear in a screen capture.
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` excludes the overlay from capture.
- Vaults are held locally for up to 25 sessions (`MAX_KNOWN_SESSIONS`) so that restore works
  whichever chat is current. That cache is the re-identification key and is dropped after
  `forgetAfterIdleMinutes` of idleness and on sign-out.
- The walk is pinned down by `tests/test_desktop_overlay.py` against a fake accessibility
  layer, so it stays checkable on Linux without Windows.
