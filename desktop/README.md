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
| **Unmask clipboard** | `Ctrl+Shift+U`. Restores `TOK_` tokens in the clipboard through `POST /api/unmask` with the current session, so text copied out of a reply pastes with the real values. |
| **Sessions** | One current session (vault), remembered in the config file. *new session* on the bar starts a fresh vault. |
| **Sign in** | Gear button → *Sign in* opens the server's login page in your browser. After the identity provider, the server sends the browser back to a loopback port on this PC with a one-time code, which the helper exchanges for its own session (`POST /auth/exchange`). *Sign out* ends it, and lets the provider end its session too. |
| **Settings** | Gear button: server URL, optional service key, preamble toggle, *Test connection* (`GET /api/me`). Opens on first run. |

Not covered yet: the send guard (Enter interception), file masking, on-screen unmask of
replies (see research §5.4 for why that is impractical), macOS.

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
- **Hotkey thread** (`Hotkeys`): `RegisterHotKey` for `Ctrl+Shift+M/U`; posts commands
  to the worker.
- **tkinter main thread** (`Bar`): the floating bar (`WS_EX_NOACTIVATE`, so clicking
  it leaves focus in Claude), toasts, and the settings window. Reads an event queue;
  never touches UIA.
- **Sign-in thread** (`SignIn`): a one-request `http.server` on `127.0.0.1:<random>`,
  `webbrowser.open` to `/auth/login?next=<that URL>/done`, then `POST /auth/exchange`
  with the code the server redirected back with. The token is stored in the config file
  and sent as `Authorization: Bearer`. A 401 later clears it and the bar says to sign in.
- The server contract is otherwise the one in `extension/background.js`:
  `X-Requested-With: maskroom`, `Authorization: Bearer mr_…` for a service key or
  `X-API-Key` for the legacy key, and the `/api/session`, `/api/mask`, `/api/unmask`,
  `/api/me`, `/auth/logout` routes.

## Next steps (from the research doc, §8.2)

1. Send guard: a low-level keyboard hook that intercepts Enter in the composer when the
   text has not been checked, mirroring the extension's guard.
2. File drop target → `/api/process`, and a Downloads watcher → `/api/unmask-file`.
3. Package as a signed executable; macOS port after the `AXValue` experiment.
