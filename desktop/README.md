# SafePII desktop helper for Claude Desktop (Windows)

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
| **Bar** | When the composer has keyboard focus, a small pill appears above it: a **Mask** button, the SafePII shield mark (the same drawing the extension uses), a word, and a chevron. It sits clear of the composer's border, right-aligned to it, with rounded ends. The mark says what it is; the word says how it is doing, and reads *SafePII* when there is nothing to report. Hovering it reports all four toggles without showing four controls. Clicking the chevron opens a panel with the current problems, the toggles, recent messages, *Mask now* and *New session*. It opens on whichever side of the pill has room, preferring above so it does not cover the composer, and **scrolls** rather than being cut off when neither side can hold it. It never takes focus away from Claude, and it never opens the panel by itself except once for a blocking alert. |
| **Alerts** | Graded. A confirmation such as "3 values masked" shows on the pill for a few seconds and drops into *Recent*. A warning or an error, such as a file attached unmasked, is **kept**: the glyph changes shape, a count appears, and it stays in the panel until you click *Got it*. Every state has its own shape as well as its own colour. |
| **Mask** | Button on the bar, or `Ctrl+Shift+M` anywhere. Reads the composer, sends the text to your SafePII server (`POST /api/mask`), writes the pseudonymized text back in place. **You still press send.** The first masked message of a session is prefixed with the token preamble, as the extension does. |
| **Guard** (default on) | Pressing Enter while the composer holds text that has not been checked yet runs it through SafePII first. If anything was masked the send is *held* so you can read what will leave the machine, then press Enter again. If nothing needed masking, Enter is replayed and your send goes through as typed. Shift+Enter is a newline and is never held; Enter outside the composer is passed straight through. The guard fails closed: if the server is unreachable the send is held and the bar says why — switch the guard off (bar button) to send anyway. Only the keyboard is hooked: the on-screen Send button is not guarded. |
| **File guard** (default on) | Claude's attach menu (the **+** button, then the first entry) opens the Windows file dialog; the helper watches it. It is recognised by the *foreground window's* class, not by scanning the accessibility tree, which never found it on a real machine. What the dialog hands over is a *display* name, so it is resolved to a real file first: Explorer hides known extensions, so a picked `.xlsx` arrives as a bare stem, relative to a folder the dialog does not spell out either. The folder is recovered from the file list's or the breadcrumb's accessible value, and the extension by matching the stem in it. **A pick that cannot be located is held**, not attached, because an unmaskable file reaching Claude is the disclosure the guard exists to stop. Confirming a supported file, by Enter, by the *Open* button, or by **double-clicking it in the list** (`.xlsx .xlsm .pdf .docx .pptx .csv .tsv .txt .json`) is intercepted with Enter or the *Open* button: the file goes to the server (`POST /api/process` with this chat's session), the masked copy is written to `%APPDATA%\SafePII\files`, its path is typed into the dialog's *File name* box and the confirm is replayed. Claude attaches the masked copy and never sees the original. A type SafePII cannot mask (an image, an archive) is **attached as it is with a warning on the bar**, the same choice the extension makes; only a type that should have been masked and could not (server unreachable, too large) holds the attachment, and the dialog stays open so you can retry or switch the guard off. `Ctrl+V` with files on the clipboard goes the same way. |
| **Drop blocking** (default **off**) | A file dragged from Explorer onto Claude cannot be masked in flight (the drop is a shell handshake, not a message we can rewrite), so it is refused: while a drag is held over Claude an invisible window with no drop target sits on top, Windows shows "not allowed", and the bar says to use the paperclip. **This is all-or-nothing**: what is being dragged cannot be read before the drop lands (Explorer hides file extensions by default, so even the selection's names do not classify reliably), so a dragged image is refused along with a dragged spreadsheet. It is **off by default**: it works by putting an invisible window over Claude, which eats clicks while it is up, and that is too blunt a thing to switch on for everyone. Turn it on in settings (`blockDrops`) if dragged files are a real risk in your fleet, and accept that a dragged image is refused along with a dragged spreadsheet. It now comes down by itself a few seconds after a drag ends, whatever happens. The paperclip stays guarded either way. |
| **Explorer menu** | `install-context-menu.ps1` adds *Mask with SafePII* to the right-click menu for those types (HKCU only, no admin). The masked copy lands in the files folder; attach it by hand. Remove with `-Uninstall`. |
| **Unmask on hover** (default on) | Rest the mouse on a token in a reply and a tooltip shows that line with the real values. The helper keeps the session's vault locally (`GET /api/session/<id>/vault`, refreshed after each mask) and restores with the same tolerant rules as the extension's `tokens.js` (any case, spaced or escaped underscores, truncated ids; unknown tokens are left alone). Nothing on Claude's screen is changed. |
| **Overlay** (experimental, default on) | Paints the real values over the tokens in replies, on a transparent click-through window that covers Claude. Tokens are found by **walking the visible lines**: a line's text and its rectangle cost one call each, and a token's offset inside its own line is small, so positioning it is cheap. Counting into the line is not always right, though: a list marker or a formatting run (bold, a link, a code span) makes Chromium's character offsets disagree with the string it hands back, by no fixed amount. A token that fails the read-back is therefore placed by **hit testing** instead, the one primitive measured as exact, and each token's own font is read so a bold token is painted bold. Between walks each token's rectangle is re-read, which is what follows scrolling. Each patch samples the pixel beside the token for its background and reads the token's font family, size, weight, italic and colour from the text range's UI Automation attributes so the value is drawn in the same style (a web font that is not installed on the PC falls back to Segoe UI); it shrinks to fit and ends with "…" when the real value is still wider. The size is read in points, off a 96-dpi page, while every rectangle is in physical pixels, so it is converted with the DPI of **the monitor Claude is on** — one number for the whole process makes the values a third too small on a 150% screen beside a 100% one. A dotted underline marks a restored value. Painting is clipped to the **conversation viewport**, found by hit-testing the middle of the window and taking the first vertically scrollable ancestor, and cut short above the **composer**; a patch must fall entirely inside that band. That band is not enough on its own, because Claude's header, its usage banners and its composer *float over* the conversation, which keeps scrolling underneath them, so they sit inside the scroller's own rectangle. Each token is therefore also asked **what is on top of it**: the element at its centre point carries the token in its name when the text is visible, and belongs to whatever covers it when it is not. That handles any floating panel without naming them. The settings window has *Hide the overlay while scrolling* if following it still looks wrong. Only exact `TOK_…` spellings are overlaid; lowercased or spaced ones still restore on hover and copy. **If it looks wrong, switch it off on the bar.** |
| **Downloads** (default on) | A file Claude produces arrives full of tokens. As one lands in your Downloads folder the helper restores it through the server (`POST /api/unmask-file` with the current chat's session) and writes `<name>_restored.<ext>` beside it. Both files are kept: deleting something you downloaded is your call, and there is a setting for it. A download cannot be caught in flight on the desktop, so it is picked up once it has landed and stopped growing, which takes a second or two. Supported: `.xlsx .xlsm .docx .pptx .md .txt .csv .tsv .json .html .xml .yaml`. Not PDF, whose bytes were destroyed by redaction rather than tokenised. Unresolved tokens are reported: they were masked in a different chat. |
| **Unmask on copy** (default on) | Copy text out of Claude and the clipboard is restored before you paste it anywhere; the bar reports the count. `Ctrl+Shift+U` does the same on demand through the server (`POST /api/unmask`). The helper's own masked paste is never "restored". |
| **Sessions** | One SafePII session (vault) per Claude chat, as in the extension. The chat on screen is identified by its URL (Chromium exposes the page URL as the document's value), else by the tokens visible on the page (a token's random id names its session), else by the chat title (`<title> - Claude` on the document). Masking uses that chat's session, so switching chats switches sessions; a brand-new chat gets a new session on its first mask and is bound to its URL on the next. *new session* on the bar starts a fresh vault for the chat on screen. Restore (hover, copy, overlay) searches every known vault at once, so it works whichever chat is current; the last 25 sessions are kept. |
| **Sign in** | Gear button → *Sign in* opens the server's login page in your browser. After the identity provider, the server sends the browser back to a loopback port on this PC with a one-time code, which the helper exchanges for its own session (`POST /auth/exchange`). *Sign out* ends it, and lets the provider end its session too. |
| **Settings** | Gear button: server URL, optional service key, preamble toggle, *Test connection* (`GET /api/me`). Opens on first run. |

Not covered: the Send button (mouse), macOS, images (SafePII cannot mask them, so they
go out unmasked with a warning, as in the extension), and files reaching Claude through
Cowork or a mounted folder (policy keys, research §5.5). The overlay is still a trial, judged on a
real machine (research §5.4 has the measurements behind its design).

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
3. Open Claude Desktop and click into the composer. The SAFEPII bar appears above it.

Without sign-on (`MASKROOM_AUTH_MODE=off`, or a shared machine) paste a service key
instead of signing in: `maskroom-admin key create desktop-<user>` on the server, or the
legacy `MASKROOM_API_KEY`.

Config: `%APPDATA%\SafePII\helper.json` (server URL, session token, current vault).
An administrator can pre-set the server URL for everyone on a machine in
`%ProgramData%\SafePII\helper.json` (`{"serverUrl": "https://safepii.hsenidmobile.com"}`),
which the helper reads before the user's file, like the extension's managed `serverUrl` key.

## Developing: keep the Windows PC in sync

The Linux dev box serves this folder over the office LAN and the Windows PC pulls it,
restarting the helper on every change. Nothing to install on Windows.

```bash
./desktop/dev-serve.sh                      # on Linux: serves desktop/ at http://<ip>:8765/
```
```powershell
# on Windows, once, from the folder where helper.py lives (copy dev-sync.ps1 there first):
powershell -ExecutionPolicy Bypass -File dev-sync.ps1 -Source http://10.27.149.168:8765
```

The watcher fetches `helper.py` every 2 s, writes it when its hash changed, and restarts
the helper as its own child process. Close the window to stop both. Plain HTTP with no
auth, so LAN and dev only; `dev-serve.sh` serves only this folder, never the repo root.

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
- **Overlay thread** (`OverlayWorker`): its own COM apartment, so a slow read can never
  delay the Enter guard. It walks the visible lines, positions each token inside its line
  (verified by reading the range back, falling back to hit testing when the offsets
  disagree with the text), re-reads
  rectangles every 60 ms, and re-walks when something moved or after 1.5 s.
  **This replaced a design that located each token from the start of the document**: that
  measured 400 ms per token, 30 s for one real chat, and queued the guard behind it.
  `tests/test_desktop_overlay.py` pins the walk down against a fake accessibility layer,
  so it stays checkable without Windows.
- **tkinter main thread** (`Bar`): the floating bar (`WS_EX_NOACTIVATE`, so clicking
  it leaves focus in Claude), toasts, and the settings window. Reads an event queue;
  never touches UIA.
- **Files**: the dialog is found from `GetForegroundWindow` plus its Win32 class; its
  *File name* box and *Open* button are searched breadth-first through the whole dialog,
  because in the modern dialog they are not direct children, and the search is retried on
  every poll until both are there, since a dialog that has just appeared is not yet built.
  A dialog with no Open button (a Save dialog) is left alone. Their rectangles are then
  published to the hook, which decides by arithmetic: a low-level hook has to return fast,
  and it never receives `WM_LBUTTONDBLCLK` at all (Windows synthesises that later), so the
  double click on a file is timed in the hook itself. A
  double-clicked file is read from the list's selection when the *File name* box has not
  filled in yet. `FileApi` posts a hand-rolled multipart body to `/api/process` and fetches the
  result from `/api/download/<run>/<name>` (the same contract `extension/background.js`
  uses). `poll_dialog()` spots Claude's `#32770` dialog; the keyboard hook swallows Enter
  and the mouse hook swallows a click on *Open* while it is up, both routing to
  `cmd_dialog_confirm()`, which reads the *File name* box, masks, writes the path back
  through `ValuePattern.SetValue` and replays the confirm. Clipboard files use `CF_HDROP`
  in both directions.
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

## The file broker (prototype)

A folder attached to Cowork goes around every guard above: the agent reads it inside a sandbox
VM, and nothing here is in that path. [`broker.py`](broker.py) is the other half of the answer
— an MCP server that serves a folder to Claude with the personal data already replaced, so
that it is the only way files get in. The reasoning, and why the reads cannot be intercepted
instead, are in [`docs/DESKTOP_APP_RESEARCH.md`](../docs/DESKTOP_APP_RESEARCH.md) §5.6 and
ADR [0009](../docs/adr/0009-folder-access-is-masked-by-being-the-only-way-in.md).

```bash
python desktop/broker.py --root ~/work --server https://safepii.example.com
```

It prints the managed configuration to deploy:

```json
{"managedMcpServers": [{"name": "safepii-files",
                        "url": "http://127.0.0.1:47821/mcp", "transport": "http"}]}
```

HTTP on the loopback interface rather than a stdio extension, because a server an
administrator pushes may speak only `http` or `sse`, and loopback is the one plain-HTTP
endpoint Claude Desktop's URL check accepts without complaint. Pair it with
`allowedWorkspaceFolders: []`, which makes the raw folder unattachable, and the broker is the
only path left.

| Tool | What it does |
|---|---|
| `list_files` | Everything in the folder, with what SafePII cannot check listed but marked **NOT SERVED** rather than quietly missing. |
| `read_file` | One file, masked. Lines are numbered **after** masking, because a token is longer than the value it replaced and an offset taken from the real bytes points somewhere else. |
| `search_files` | Masks the search term first, so looking for a real name finds that name's token. Only whole values match, and the reply says so. |
| `write_file` | Restores the tokens and writes to an output folder *beside* the served one, never over the original. Tokens from another session are reported, not guessed. |

**File names.** By default a name becomes a handle — `d01/f003.csv` — because a name discloses
as much as the file does and masking one is only best-effort: measured against a live server,
`kyc/Nimal Perera - loan.csv` is not recognised at all, and a probe that gets around the
separator read the `md` in `notes.md` as a surname. A handle cannot leak what the detector
misses. `--names mask` is available for a folder whose names carry meaning the work needs, and
`--names real` for one whose names are known to be safe.

**What it will not serve.** Source code (masking it would corrupt it), spreadsheets and PDFs
(they need the server's document pipeline, which this prototype does not call yet), and
anything it cannot read as text. A file SafePII cannot check is a file that must not reach the
model, which is the same choice the guard makes about a message it cannot check. Tables *are*
served, through `/api/process` rather than `/api/mask`: sent as prose, a `.csv` comes back with
its identifiers masked and its people still in place.

**How a user shares a folder.** From the bar, not from Claude's folder picker — that picker
has nothing to offer once an administrator has emptied `allowedWorkspaceFolders`, which is what
makes SafePII the only way in.

1. Chevron → **Share a folder with Claude…** → the Windows folder picker.
2. A confirmation says what will happen before anything happens: *"Claude would see 9 files
   (140 kB), masked. 2 would not be served: 1 source code, 1 needs the document pipeline. File
   names are replaced with handles."* The count is arithmetic over extensions, so it appears
   instantly and costs no server call.
3. **Share it.** The panel then reads *Sharing loans* with a **Stop** beside it, and the hover
   readout says so too, because a shared folder is a live path off the machine.
4. In Claude, the user just asks. The folder's vault joins the ones restore searches, so a
   value Claude read out of a file is a real value under the mouse in the chat.

One folder at a time: sharing a second replaces the first. Two would mean two vaults to explain
and two sets of handles that look alike, for a case nobody has asked for. Sharing survives the
restart that every update causes, and says so rather than resuming silently; it stops by itself
when the vaults are dropped, at sign-out or after the idle timeout, because a folder still
answering with tokens this process can no longer turn back into values is worse than one that
is not being served. An administrator can switch sharing off entirely, fix the name policy, or
move the port ([`enterprise/policies/windows/admx/`](../enterprise/policies/windows/admx/README.md)).

`tests/test_broker.py` covers the broker with both ends over real HTTP; `tests/test_desktop_sharing.py`
covers the flow above, including that nothing is served until the user says so.

## Diagnosing the overlay

Every walk reports its whole outcome, so any fragment of `%APPDATA%\SafePII\helper.log`
is conclusive:

```
overlay: walked 58 lines (6 with tokens), placed 13 (2 by hit test), UNPLACED 1: TOK_PERSON_3669FE1A in 404 ms; style {...}
```

- `UNPLACED` — the token was found in a line but neither counting characters nor hit
  testing could pin it down. 
- `NOT IN ANY VAULT` — the token is on screen but no known session holds it, which is a
  session problem, not a placement one.
- `behind a panel` — the token is covered by the header, a banner or the composer, so it
  is deliberately not painted. Not a fault.
- Named in neither, and not painted — the line it sits on was never walked.

The scaling of the screen being painted on is reported whenever it changes, which is what
to check first if the values look too small or too large beside Claude's own text:

```
overlay: painting at 144 dpi (1.50x) on the monitor at (1920, 0, 3840, 1080)
```

For that last case turn on *Log every line the overlay walks* in settings
(`overlayDebug`), reproduce, and the log lists each walked line with its y range and text,
plus why a token on it could not be placed. It is noisy; turn it off afterwards.

Copy the log with `Copy-Item` rather than redirecting it, or the line breaks are lost:

```powershell
Copy-Item $env:APPDATA\SafePII\helper.log $HOME\Desktop\helper.log
```

## Next steps (from the research doc, §8.2)

1. Guard the Send button too: a `WH_MOUSE_LL` hook hit-tested against the button's UIA
   rectangle.
2. A file drop target on the bar, so a file can be masked without the dialog.
3. Package as a signed executable; macOS port after the `AXValue` experiment.
