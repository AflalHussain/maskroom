---
status: accepted
date: 2026-09-25
---

# Native clients sign in through the system browser and a loopback code exchange

The Windows desktop helper (`desktop/`) has no cookie jar and no identity popup, so the
cookie session that the web UI and the Chrome extension share cannot reach it. We decided
that a native client signs in the way native apps do (RFC 8252): it listens on a random
`127.0.0.1` port, opens the server's `/auth/login` in the system browser with that loopback
URL as `next`, and the server, after the provider flow, redirects the browser there with a
one-time code. The client posts the code to `/auth/exchange` and receives a login-session
token, which it sends as `Authorization: Bearer <token>`. The token is an ordinary
`login_sessions` row: same sliding lifetime, revoked by disabling the user, ended by
`POST /auth/logout` with the bearer header. Codes live in a new `login_codes` table, hashed
like sessions, single-use, and expire after a minute.

## Considered options

- **A service key per user.** Works today without server changes, but an administrator
  must issue each one and the audit trail records the key's name, not the person. Kept as
  the fallback for servers without sign-on and for shared machines.
- **The client speaks OIDC itself (public client, PKCE).** Rejected for the same reasons as
  in ADR 0002: provider quirks and token refresh would live in every client, and a
  provider-issued token cannot be revoked centrally before it expires.
- **Returning the session token on the loopback URL directly.** Rejected: the token would
  sit in browser history and proxy logs. The code is worthless a minute later or after
  one use.
- **Accepting `localhost` as a loopback target.** Rejected: it can resolve to a
  non-loopback address; only `127.0.0.1` and `[::1]` with an explicit port are accepted.

## Consequences

- `Authorization: Bearer` now carries either a service key (`mr_…`) or a login-session
  token; a presented-but-invalid bearer is anonymous and never falls through to the cookie.
- The browser also receives the normal session cookie during a loopback sign-in, so the
  user ends up signed in to the web UI as well.
- Schema version 3 adds `login_codes` (created by `init_schema` on start; no column
  migration).
- Anyone who can run code on the user's machine can listen on a loopback port; that is the
  standard native-app trust boundary and no worse than the config file the token lives in.
