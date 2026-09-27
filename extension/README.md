# Maskroom for Claude — browser extension

A Chrome (Manifest V3) extension that puts Maskroom between you and the claude.ai
composer. It is the convenience layer described in
[`docs/LLM_MIDDLEWARE_RESEARCH.md`](../docs/LLM_MIDDLEWARE_RESEARCH.md) §1.1 and
carries the caveats listed there: **unsupported by Anthropic, dependent on claude.ai's
markup, blind to files, and never the control you rely on.**

## What it does

| | |
|---|---|
| **Bar** | A small pill in the corner: a **Mask** button, the SafePII mark, one word of state, and a chevron. The mark says what it is; the word says how it is doing, and reads *SafePII* when there is nothing to report. Hovering it reports both toggles without showing two controls. The chevron opens a panel holding the current problems, the toggles, recent messages and the file and session actions. Matches the Windows helper in [`desktop/`](../desktop/README.md). |
| **Alerts** | Graded. A confirmation such as "3 values masked" shows on the pill for a few seconds and drops into *Recent*. A warning or an error, such as a file attached unmasked, is **kept**: the word changes, a count appears beside it, and it stays in the panel until you click *Got it*. The old toast faded after three seconds whether it had been read or not. |
| **Mask** | Replaces the text in the composer with pseudonymized text from your SafePII server (`TOK_<TYPE>_<ID>` tokens). Button on the pill, or `Ctrl/Cmd+Shift+M`. **You still press send.** |
| **Guard** (default on) | Pressing Enter or the send button while the composer holds text that has not been checked yet runs it through Maskroom first. If anything was masked the send is *stopped* so you can read what will leave the browser, then press Enter again. If nothing needed masking, your send goes through as typed (the extension replays the click you made). Shift+Enter is a newline and is never intercepted. The guard fails closed: if the server is unreachable the send is held and the pill says why — switch the guard off to send anyway. |
| **Unmask view** (default on) | Restores the real values in replies **on screen only** — the DOM you see, including same-origin preview frames (see below). The conversation stored by Anthropic keeps the tokens. Tolerates lowercased, spaced, hyphenated, markdown-escaped or truncated tokens; unknown tokens are left as they are. Restored elements get a dotted underline. Switching the view off puts the tokens back on screen without a reload; on restores again. |
| **Mask file** | Pick a `.xlsx`/`.xlsm`/`.pdf`/`.docx`/`.pptx`/`.csv`/`.tsv`/`.txt`/`.json`; it goes to your Maskroom server and the **masked version is attached** to the chat in your place: Excel as a masked workbook (or Markdown tables, see settings), PDF as masked Markdown text, Word/PowerPoint masked in place with formatting kept, CSV/TSV masked column-aware, plain text/JSON masked as free text. The raw file never reaches claude.ai. With guard on, a file you drop on claude.ai or pick with its attach button is taken over and goes through the same path. If claude.ai does not accept the attachment, the masked file opens in a new tab so you can attach it by hand. |
| **Unmask file** | Pick a file Claude produced (`.xlsx/.xlsm`, `.docx/.pptx`, `.md/.txt/.csv/.tsv/.json/.html/.xml/.yaml`); it is restored through the server with this chat's vault and saved as `<name>_restored.<ext>`. Unresolved tokens are listed inside workbooks (sheet *Maskroom notes*) and Markdown/HTML (trailing comment). PDFs are not supported. |
| **Download intercept** (default on) | A download started on claude.ai (artifact, code-tool output, export) of a supported type is restored before it lands: the content is read first, the token version is cancelled only once the bytes are in hand, the restored file is saved as `*_restored`, and a toast reports the counts. If reading fails the original download proceeds untouched; if restoring fails the original bytes are saved under the original name. By default only the restored file is kept. For a plain-click blob download (the common case) the extension blocks the browser download at the click and saves only the restored file, so no token copy is ever written. For downloads it cannot catch at the click — https links, or ones a script starts — it falls back to letting the file land, then deletes the token copy from disk once the restored file is safely saved. Turn on *Also keep the masked copy* (options) to leave the token version beside the restored one — handy for demos. Word/PowerPoint caveat: a token split across formatting runs stays unresolved. |
| **Recent downloads log** | The options popup lists the last 20 download events with the outcome (restored / left as-is / failed / ignored) and the reason, so a failed restore can be diagnosed without opening DevTools. *Copy log* copies it as text. |
| **Admin lock (guardLocked)** | An enterprise administrator can force the guard on and stop users disabling it, via managed policy (`chrome.storage.managed`). When the `guardLocked` key is set, the guard is always on, the bar shows "guard: on 🔒", and the options checkbox is disabled. Managed storage is read-only to the user. See [`docs/ENTERPRISE_ENFORCEMENT.md`](../docs/ENTERPRISE_ENFORCEMENT.md) for the force-install and lock policy recipe. |
| **use session id…** | Switch this chat to a different session id — from another claude.ai chat, or from the staging page (click the id there to copy it) — so both share one vault and the same tokens, and replies about either are restored. |
| **Preamble** | The first masked message of a session is prefixed with a one-line instruction telling the model to repeat tokens verbatim. |
| **Sessions** | One Maskroom session (vault) per claude.ai chat, remembered across reloads. *New session* starts a fresh vault for the current chat. |

**On-site previews.** The unmask view also restores previews rendered as text in the page, and in same-origin/srcdoc preview frames (the extension runs in all frames and syncs the chat's vault down to them). Claude's artifact previews (spreadsheets, documents) render in the `www.claudeusercontent.com` frame, which the manifest now includes, so those restore too. The one kind *not* covered is a preview drawn to a **canvas or PDF viewer** (no text to rewrite). For those, download the file and let *Unmask file* or the download intercept restore it.

Not covered: images and file types Maskroom cannot mask (they are attached as-is with a
warning), other Claude surfaces (Cowork, desktop, mobile, Claude in Chrome), and anything
done in a tab where the extension is disabled. Attaching depends on claude.ai's hidden file
input or drop zone (`SEL.fileInput` / `SEL.dropTarget`).

## Install (unpacked)

1. Run the Maskroom server: `pii_env/bin/python webui/app.py` (default `http://127.0.0.1:5170`).
2. Open `chrome://extensions`, enable *Developer mode*, *Load unpacked*, choose this
   `extension/` directory.
3. Click the extension icon → set the server URL → *Test connection* → *Save*. A
   non-localhost URL asks for a host permission once. With sign-on enabled on the server
   (`MASKROOM_AUTH_MODE=oidc`) click *Sign in*: the server's login page opens in a popup
   and the extension keeps the session cookie. A server running without sign-on may
   still want the legacy API key, under *Legacy API key* in the popup.
4. Open claude.ai. The SafePII pill appears bottom-right.

## How it is built

- `background.js` — the only code that talks to the server (so the page needs no CORS
  and no credential enters the page); uploads files and fetches masked ones as base64;
  owns the download intercept (`chrome.downloads`) and the sign-in popup
  (`chrome.identity.launchWebAuthFlow` to the server's `/auth/login`).
- `content.js` — the bar, guard, composer replacement, session bookkeeping, and the
  MutationObserver that restores tokens in rendered text. All claude.ai selectors are in
  the `SEL` object at the top: **when claude.ai changes its markup, fix them there.**
- `tokens.js` — token restore logic shared with the tests; mirrors `unmask_text()` in
  `maskroom/engine.py`.
- The vault of the current session is held in the content script's memory for on-screen
  restore. It is fetched from your own server and never sent anywhere else.

## Tests

```bash
node --test extension/test/tokens.test.js                           # restore logic
PORT=5199 pii_env/bin/python webui/app.py &                          # a server
PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
  xvfb-run -a node extension/test/e2e.js                            # real Chromium + harness page
```

The end-to-end test loads the unpacked extension into Chromium, serves
`test/harness.html` (a stand-in composer/message area) in place of claude.ai, and checks:
guard masks instead of sending, nothing is sent automatically, the masked text is what
gets sent, streamed replies are restored on screen with unknown tokens left alone,
clean text is sent after the check, the same value gets the same token later in the
session, the session survives a reload, a workbook is masked and attached (file-input and
drop paths, `.xlsx` and `.md`), the guard intercepts claude.ai's own file picker, and an
adopted session's tokens restore; *Unmask file* and the download intercept (blob and
https downloads, intercept off) round-trip generated Markdown and a workbook. The guard is exercised both ways: Shift+Enter, the send
button, editing after a mask, and switching the guard off and on for text and for files.

## Terms and risk

Anthropic's consumer terms restrict accessing the service "through automated or
non-human means". This extension never sends anything by itself — every send is a human
keypress or click — and it only rewrites what that human is about to send. Whether that
falls inside the clause is a judgement call your organisation must make; the extension is
provided for that evaluation, not as a supported product. Detection misses still leave the
browser: pseudonymization is not anonymization.
