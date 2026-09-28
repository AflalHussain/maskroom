---
status: accepted
date: 2026-09-28
---

# Claude Desktop is protected from outside the app, through Windows UI Automation

The Chrome extension can only protect claude.ai in a browser, and Claude Desktop is where
a large share of users type. It is a hardened Electron app: its fuses are set so it runs no
code we could supply, which we verified by reading them out of the shipped package, so the
extension cannot be loaded into it and no in-process integration exists to ask for.

We decided the desktop client works the way Grammarly's does: a separate program, running
as the same user, that reads and writes the composer through the platform's accessibility
API — Windows UI Automation. `desktop/helper.py` recognises the composer by its
`ProseMirror` class inside `Claude.exe`, reads its text, sends it to the SafePII server,
and writes the pseudonymized text back with `ValuePattern.SetValue`, verified by re-reading
and with select-all-and-paste as the fallback. Both directions were proved on a real machine
by `scripts/desktop/uia_composer_probe.py` before any of it was built.

This buys nothing from Anthropic and is owed nothing by them. The helper depends on Claude
Desktop keeping a standard editable composer, and says so where a user can read it.

## Considered options

- **Intercept the network.** Rejected for the Chat tab: its traffic to claude.ai cannot be
  read without breaking TLS, which is not a supported configuration and would put us in the
  path of the user's authentication. It *is* viable for "Claude Desktop on 3P", where the
  client is pointed at a gateway the customer controls; that remains available to a
  customer who runs one, and needs nothing from us.
- **A clipboard or hotkey helper.** Works in every application today and needs no
  per-application knowledge, but it protects only what the user remembers to run through
  it, and the guard — the thing that makes the promise — cannot exist. Kept as the shape of
  `Ctrl+Shift+M`, not as the product.
- **An input method or text service.** Would see every keystroke in every application,
  including passwords, and would be a far larger thing to justify to a bank's security
  review than a program that reads one window. Rejected.
- **Route desktop users to the web app by policy.** Simplest, and still the right answer
  for a fleet that can mandate it; it is documented in the research doc. Rejected as the
  only answer, because it is a refusal to solve the problem rather than a solution.

## Consequences

- Windows first, and Windows only for now. macOS has an equivalent API (`AXUIElement`),
  but Chromium exposes its tree there only after `AXManualAccessibility` is set, which is a
  separate experiment.
- Reads cross a process boundary and cost milliseconds each, which is the constraint that
  shapes everything else: see ADR 0005 and ADR 0006.
- The accessibility tree is Chromium's, built on demand and lagging the renderer, so the
  helper can never assume what it read is current. Every write is verified.
- A Claude Desktop release that changes the composer's implementation can break the helper.
  The failure is visible — the bar does not appear — rather than silent.
- The helper must run as the interactive user, not as a service, which decides how it is
  installed (ADR 0008).
