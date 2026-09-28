---
status: accepted
date: 2026-09-28
---

# The guard intercepts Enter in a low-level hook, and fails closed

A *Mask* button protects only the user who remembers to press it. The promise a fintech
customer buys is that unmasked text cannot leave the machine, so the helper has to be in the
path of the send.

We decided the guard is a `WH_KEYBOARD_LL` hook that swallows a plain Enter while
`Claude.exe` is in front and the composer holds text that has not been checked. The worker
runs the text through the server; if anything was masked the send is *held* so the user reads
what will leave, and a second Enter sends it. If nothing needed masking, Enter is replayed
with `SendInput`, which the hook lets through because synthetic input carries
`LLKHF_INJECTED`. Shift+Enter is a newline and is never held. An Enter pressed while a check
is still running is dropped, as the extension does, so a masked text is never sent unread.

When the guard cannot run at all — the server is unreachable, or the worker is not answering
— the default is to hold the send and say why, and the user can switch the guard off on the
bar to send anyway. `onGuardFailure` makes that a customer's decision (`hold` or `warn`), and
an administrator can fix it by policy.

## Considered options

- **Hook the Send button instead.** It is the other way to send, and it is not guarded
  today — a `WH_MOUSE_LL` hook hit-tested against the button's rectangle is the known next
  step. Rejected as the *first* mechanism because the keyboard is how the composer is
  actually used, and because a mouse hook that misjudges a rectangle eats clicks.
- **Poll the composer and mask as the user types.** Rejected: it rewrites text under the
  cursor while someone is mid-sentence, and it cannot distinguish a half-typed identifier
  from a finished one.
- **Fail open** when the guard cannot run. Rejected as the default. A guard that lets the
  message through when it is broken protects nobody on the day it matters; a customer who
  prefers availability to that guarantee sets `onGuardFailure=warn` deliberately.
- **Refuse dragged files by default.** The drop blocker works by putting an invisible window
  over Claude, and that window eats clicks while it is up. Left **off** by default and
  documented, because a blunt control switched on for everyone costs more than it buys; the
  paperclip is guarded either way.

## Consequences

- The helper holds a global keyboard hook. That is what the mechanism is, and it is the
  reason the package must be signed (ADR 0008) and installed where a standard user cannot
  replace it.
- A hook that is slow blocks the whole desktop's input, so the hook itself only ever does
  arithmetic and posts a command; every server call happens on the worker thread. The file
  dialog's rectangles are published to the hook for the same reason, and the double click on
  a file is timed inside it, because a low-level hook never receives `WM_LBUTTONDBLCLK`.
- Failing closed means a bug in the helper can stop a user sending anything. This happened
  once during development — the liveness check ran before the "is Claude in front" check, so
  Enter was swallowed in every application — and the shape of the fix is now permanent: the
  front-window test gates everything, *busy* is distinguished from *dead*
  (`SHARED["worker_busy"]`, `WORKER_DEAD_S`), and the drop blocker comes down by itself after
  `BLOCKER_MAX_S` whatever happens.
- Only the keyboard is guarded, so the honest statement of coverage — the Send button is
  not — belongs in the user-facing documentation, and it is there.
