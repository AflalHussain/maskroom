---
status: accepted
date: 2026-09-28
---

# One vault per chat, with the chat identified from what is on the screen

Tokens are only meaningful inside the session that minted them: the same name masked in two
chats gets two different ids, and restoring a reply with the wrong vault either fails or —
worse — resolves a token to somebody else's value. The extension can read the page's URL and
key a session off it. The helper has no such handle: it is outside the application, and
Claude Desktop has no address bar.

We decided the helper identifies the chat on screen, in this order, and keys one SafePII
session (one vault) to it:

1. **the page URL**, which Chromium exposes as the document element's value;
2. **the tokens visible on the page** — a token's random id names its session, so a chat with
   masked text identifies itself;
3. **the chat title**, read from the document's name (`<title> - Claude`).

`bind_session()` records the choice, so a brand-new chat gets a fresh session on its first
mask and is bound to its URL on the next poll. Masking always uses the current chat's
session, so switching chats switches vaults. Restore is deliberately different: hover, copy
and the overlay search **every** known vault at once, because a token on screen has to
resolve whichever chat is current.

## Considered options

- **One vault for everything.** Simple, and wrong: token ids would collide in meaning across
  conversations, and a chat's vault could never be forgotten independently.
- **Ask the user which session they are in.** Rejected. It is a question about our internals,
  asked at the moment the user is trying to send a message, and the answer is already on the
  screen.
- **Title only.** Rejected as the primary: titles are not unique, Claude renames a chat after
  the first message, and two untitled chats are indistinguishable. Kept as the last fallback,
  where being approximately right beats having no session at all.
- **Restore from the current chat's vault only.** Rejected: a user scrolling back through an
  older chat, or reading a reply after switching, would see tokens that we hold the values
  for. Searching every vault costs nothing here because the vaults are already cached.

## Consequences

- Restoration is cross-session by design, which is why token collisions matter: a truncated
  or loosely-matched token could resolve against another chat's vault. That risk is recorded
  as GUARD-3 in the production-readiness backlog, and exact `TOK_…` spellings are the only
  ones the overlay paints.
- Up to `MAX_KNOWN_SESSIONS` (25) vaults are held locally. That cache is the
  re-identification key, so it is dropped after `forgetAfterIdleMinutes` idle and on sign-out.
- A 404 from the server for a session forgets it locally and re-resolves, so a session
  expiring server-side does not wedge the chat.
- Identification is best-effort by nature. When all three signals fail the helper still
  masks — with a new session — rather than refusing, because refusing to mask is the worse
  failure.
- Desktop sessions are indistinguishable from web sessions in the audit trail today
  (PKG-3); the client that created a session is not recorded.
