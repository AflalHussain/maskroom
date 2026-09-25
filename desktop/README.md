# Maskroom desktop helper (Windows prototype)

The Claude Desktop counterpart of [`extension/`](../extension). Claude Desktop is a
hardened Electron app that cannot load the browser extension, so this helper works from
the outside through the Windows UI Automation API, the same mechanism Grammarly uses.
Background and the options that were rejected are in
[`docs/DESKTOP_APP_RESEARCH.md`](../docs/DESKTOP_APP_RESEARCH.md); the write path it
relies on was verified with [`scripts/desktop/uia_composer_probe.py`](../scripts/desktop/uia_composer_probe.py).

**Status: prototype.** Windows only. Unsupported by Anthropic, dependent on Claude
Desktop keeping a standard ProseMirror composer, and never the control you rely on
(the same caveats as the extension).

## What it does

| | |
|---|---|
| **Bar** | When the Claude Desktop composer has keyboard focus, a small floating bar appears above it. It never takes focus away from Claude. |
| **Mask** | Button on the bar, or `Ctrl+Shift+M` anywhere. Reads the composer, sends the text to your Maskroom server (`POST /api/mask`), writes the pseudonymized text back in place. **You still press send.** The first masked message of a session is prefixed with the token preamble, as the extension does. |
| **Guard** (default on) | Pressing Enter while the composer holds text that has not been checked yet runs it through Maskroom first. If anything was masked the send is *held* so you can read what will leave the machine, then press Enter again. If nothing needed masking, Enter is replayed and your send goes through as typed. Shift+Enter is a newline and is never held; Enter outside the composer is passed straight through. The guard fails closed: if the server is unreachable the send is held and the bar says why — switch the guard off (bar button) to send anyway. Only the keyboard is hooked: the on-screen Send button is not guarded. |
| **Unmask on hover** (default on) | Rest the mouse on a token in a reply and a tooltip shows that line with the real values. The helper keeps the session's vault locally (`GET /api/session/<id>/vault`, refreshed after each mask) and restores with the same tolerant rules as the extension's `tokens.js` (any case, spaced or escaped underscores, truncated ids; unknown tokens are left alone). Nothing on Claude's screen is changed. |
| **Overlay** (experimental, default on) | Paints the real values over the tokens in replies, on a transparent click-through window that covers Claude. Tokens are located by character offset in the page text (one move per token, read back to verify; the text search the probe showed mis-aligning is not used), rectangles are refreshed every tick so scrolling is followed, and the page text is re-read twice a second for streaming. Each patch samples the pixel beside the token for its background and reads the token's font family, size, weight, italic and colour from the text range's UI Automation attributes so the value is drawn in the same style (a web font that is not installed on the PC falls back to Segoe UI); it shrinks to fit and ends with "…" when the real value is still wider. A dotted underline marks a restored value. The composer is never overlaid. Only exact `TOK_…` spellings are overlaid; lowercased or spaced ones still restore on hover and copy. **If it looks wrong, switch it off on the bar.** |
| **Unmask on copy** (default on) | Copy text out of Claude and the clipboard is restored before you paste it anywhere; the bar reports the count. `Ctrl+Shift+U` does the same on demand through the server (`POST /api/unmask`). The helper's own masked paste is never "restored". |
| **Sessions** | One Maskroom session (vault) per Claude chat, as in the extension. The chat on screen is identified by its URL (Chromium exposes the page URL as the document's value), else by the tokens visible on the page (a token's random id names its session), else by the chat title (`<title> - Claude` on the document). Masking uses that chat's session, so switching chats switches sessions; a brand-new chat gets a new session on its first mask and is bound to its URL on the next. *new session* on the bar starts a fresh vault for the chat on screen. Restore (hover, copy, overlay) searches every known vault at once, so it works whichever chat is current; the last 25 sessions are kept. |
| **Sign in** | Gear button → *Sign in* opens the server's login page in your browser. After the identity provider, the server sends the browser back to a loopback port on this PC with a one-time code, which the helper exchanges for its own session (`POST /auth/exchange`). *Sign out* ends it, and lets the provider end its session too. |
| **Settings** | Gear button: server URL, optional service key, preamble toggle, *Test connection* (`GET /api/me`). Opens on first run. |

Not covered: the Send button (mouse), file masking, macOS. The overlay is a trial: the
research doc (§5.4) measured a naïve token sweep at up to 1.4 s, so it is built around
offset moves and per-range rectangle refreshes instead; whether that is smooth enough is
being judged on a real machine, and it will be dropped if not.

## Install

1. Python 3.12 from python.org with *Add python.exe to PATH* ticked, then:
   ```
   py -m pip install uiautomation
   ```
2. Run it, as the same Windows user that runs Claude Desktop and not elevated:
   ```
   py desktop\helper.py
   ```
   The settings window opens. Enter the server URL (for the hosted service,
   `https://safepii.hsenidmobile.com`), click *Sign in*, complete the sign-in in the
   browser, close that tab. The window shows *Signed in as you@…*. *Save*.
3. Open Claude Desktop and click into the composer. The MASKROOM bar appears above it.

Without sign-on (`MASKROOM_AUTH_MODE=off`, or a shared machine) paste a service key
instead of signing in: `maskroom-admin key create desktop-<user>` on the server, or the
legacy `MASKROOM_API_KEY`.

Config: `%APPDATA%\Maskroom\helper.json` (server URL, session token, current vault).
An administrator can pre-set the server URL for everyone on a machine in
`%ProgramData%\Maskroom\helper.json` (`{"serverUrl": "https://safepii.hsenidmobile.com"}`),
which the helper reads before the user's file, like the extension's managed `serverUrl` key.

## How it is built

One file, [`helper.py`](helper.py), standard library plus `uiautomation`.

- **UIA worker thread** (`Automation`): polls the focused element four times a second
  and recognises the composer by its `ProseMirror` class name inside `Claude.exe`.
  Runs every server call. Writes back with `ValuePattern.SetValue`, verifies by
  re-reading, and falls back to select-all + paste (also verified by the probe).
- **Hotkey thread** (`Hotkeys`): `RegisterHotKey` for `Ctrl+Shift+M/U`, plus the guard's
  `WH_KEYBOARD_LL` hook. The hook swallows a plain Enter while `Claude.exe` is in front
  and posts a `guard` command; the worker checks the text and either rewrites it (send
  held) or replays Enter with `SendInput`, which the hook lets through because synthetic
  input carries `LLKHF_INJECTED`. An Enter pressed while a check was still running is
  dropped, as the extension does, so a masked text is never sent unread.
- **tkinter main thread** (`Bar`): the floating bar (`WS_EX_NOACTIVATE`, so clicking
  it leaves focus in Claude), toasts, and the settings window. Reads an event queue;
  never touches UIA.
- **Sessions**: `chat_identity()` reads the page document's `Name` and `ValuePattern.Value`;
  `resolve_session()` tries the url map, then the token→session map built from the cached
  vaults, then the title; `bind_session()` records the choice. A 404 from the server
  forgets the session and re-resolves.
- **Hover and clipboard** run on the UIA worker's idle tick: when the mouse rests for
  0.35 s over the Claude window, `TextPattern.RangeFromPoint` on the page document (cached;
  found by walking the window for the element with the most text) expanded to a line,
  restored locally, shown in a no-activate tooltip under the line. The clipboard is watched
  through `GetClipboardSequenceNumber`; a change while Claude is in front that contains
  tokens is restored in place.
- **Sign-in thread** (`SignIn`): a one-request `http.server` on `127.0.0.1:<random>`,
  `webbrowser.open` to `/auth/login?next=<that URL>/done`, then `POST /auth/exchange`
  with the code the server redirected back with. The token is stored in the config file
  and sent as `Authorization: Bearer`. A 401 later clears it and the bar says to sign in.
- The server contract is otherwise the one in `extension/background.js`:
  `X-Requested-With: maskroom`, `Authorization: Bearer mr_…` for a service key or
  `X-API-Key` for the legacy key, and the `/api/session`, `/api/mask`, `/api/unmask`,
  `/api/me`, `/auth/logout` routes.

## Next steps (from the research doc, §8.2)

1. Guard the Send button too: a `WH_MOUSE_LL` hook hit-tested against the button's UIA
   rectangle.
2. File drop target → `/api/process`, and a Downloads watcher → `/api/unmask-file`.
3. Package as a signed executable; macOS port after the `AXValue` experiment.
