---
status: accepted
date: 2026-09-22
---

# Sign-on is OpenID Connect on the server; roles and service keys live in Maskroom

Maskroom had no authentication, only two shared secrets and a user id the client asserted
in a header. We decided that the server runs the OpenID Connect authorization-code flow
against whatever provider a client company has (discovery-based, so Google, Entra, Okta and
Keycloak all look the same) and issues its own database-backed session cookie, which the
web UI and the Chrome extension both use. The extension does not speak OIDC itself: it
opens the server's login page in an identity popup and then carries the cookie. Roles
(staff, auditor, admin) are stored in Maskroom and changed only by an administrator, never
derived from provider groups. Scripts use named service keys; the old shared key survives
as a deprecated service identity so a rollout can be staged. Keycloak ships as an optional
compose profile for organisations without a provider.

## Considered options

- **OIDC inside the extension with bearer tokens.** Rejected: token refresh and each
  provider's quirks would live in a fleet-managed extension, and a provider-issued token
  cannot be revoked before it expires. The cookie costs one indexed lookup per request and
  can be revoked centrally.
- **Mapping roles from provider groups.** Rejected for now: group claims differ per
  provider and are absent on several; a users table with an admin console is predictable
  for every client. It can be added later as an optional import.
- **Removing the shared key immediately.** Rejected: the server and the extension are
  deployed separately; a staged rollout needs both to work for a while.

## Consequences

- Sessions, vaults and file runs now have an owner and are served only to that principal.
  Rows created with auth off have no owner and are admin-only after the switch.
- Cookie-authenticated requests that change state must send `X-Requested-With: maskroom`;
  the extension and the pages do. Credentials never appear in query strings.
- On https the session cookie is `SameSite=None; Secure` so the extension's requests carry
  it regardless of Chrome's first-party treatment of extension fetches; plain-http dev uses
  `Lax` and relies on that treatment.
- `MASKROOM_AUTH_MODE=off` keeps the pre-SSO behaviour for demos and tests; production is
  expected to run `oidc`.
- Schema version 2 adds users, login_sessions, api_keys, runs and `sessions.owner_id`
  (applied by `init_schema` on start).
