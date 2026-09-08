# Maskroom for Claude — browser extension

A Chrome (Manifest V3) extension that puts Maskroom between you and the claude.ai
composer. It is the convenience layer described in
[`docs/LLM_MIDDLEWARE_RESEARCH.md`](../docs/LLM_MIDDLEWARE_RESEARCH.md) §1.1 and
carries the caveats listed there: **unsupported by Anthropic, dependent on claude.ai's
markup, blind to files, and never the control you rely on.**

## What it does

| | |
|---|---|
| **Mask** | Replaces the text in the composer with pseudonymized text from your Maskroom server (`TOK_<TYPE>_<ID>` tokens). Button on the floating bar, or `Ctrl/Cmd+Shift+M`. **You still press send.** |
| **Guard** (default on) | Pressing Enter or the send button while the composer holds text that has not been checked yet runs it through Maskroom first. If anything was masked the send is *stopped* so you can read what will leave the browser, then press Enter again. If nothing needed masking, your send goes through as typed (the extension replays the click you made). |
| **Unmask view** (default on) | Restores the real values in replies **on screen only** — the DOM you see. The conversation stored by Anthropic keeps the tokens. Tolerates lowercased, spaced, hyphenated, markdown-escaped or truncated tokens; unknown tokens are left as they are. Restored elements get a dotted underline. |
| **Preamble** | The first masked message of a session is prefixed with a one-line instruction telling the model to repeat tokens verbatim. |
| **Sessions** | One Maskroom session (vault) per claude.ai chat, remembered across reloads. *New session* starts a fresh vault for the current chat. |

Not covered: file and image attachments (use the staging page at `/staging`), other
Claude surfaces (Cowork, desktop, mobile, Claude in Chrome), and anything typed in a
tab where the extension is disabled.

## Install (unpacked)

1. Run the Maskroom server: `pii_env/bin/python webui/app.py` (default `http://127.0.0.1:5170`).
2. Open `chrome://extensions`, enable *Developer mode*, *Load unpacked*, choose this
   `extension/` directory.
3. Click the extension icon → set the server URL and API key (if `MASKROOM_API_KEY` is
   set on the server) → *Test connection* → *Save*. A non-localhost URL asks for a host
   permission once.
4. Open claude.ai. The MASKROOM bar appears bottom-right.

## How it is built

- `background.js` — the only code that talks to the server (so the page needs no CORS
  and the API key never enters the page).
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
token-only text passes the guard, the same value gets the same token later in the
session, and the session survives a reload.

## Terms and risk

Anthropic's consumer terms restrict accessing the service "through automated or
non-human means". This extension never sends anything by itself — every send is a human
keypress or click — and it only rewrites what that human is about to send. Whether that
falls inside the clause is a judgement call your organisation must make; the extension is
provided for that evaluation, not as a supported product. Detection misses still leave the
browser: pseudonymization is not anonymization.
