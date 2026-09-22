# Maskroom

Pseudonymization and redaction of personal data before it reaches an LLM or leaves the
organisation, with Sri Lankan identifiers as first-class citizens.

## Language

**Token**:
The deterministic placeholder (`TOK_<ENTITY>_<hash>`) that replaces one detected value.
_Avoid_: placeholder, pseudonym id, mask

**Vault**:
The mapping from tokens back to the original values, plus which tokens came from numeric
cells. The re-identification key; as sensitive as the data itself.
_Avoid_: mapping file, dictionary, key store

**Session**:
One conversation's shared vault, identified by a session id, so every masking call and
un-masking pass in that conversation uses the same tokens. Expires after idle time.
_Avoid_: conversation store, context

**Run**:
One file operation (mask, redact, restore or un-mask) and the scratch files it produced.
_Avoid_: job, task

**Audit record**:
What one operation received and produced, attributed to a user, kept for a bounded
retention period for administrators.
_Avoid_: log entry, history item

**Rules overlay**:
The administrator's deny terms, allow terms and regex rules layered on top of the built-in
locale policy. Every saved version is a **revision**.
_Avoid_: custom policy, config, overrides

**Locale policy**:
The built-in, country-specific detection knowledge (identifiers, column rules, place names)
shipped with the engine.
_Avoid_: profile, ruleset

## Access

**Principal**:
Whoever a request is attributed to: a signed-in user, a service key, the legacy shared key,
or nobody. Every session, run and audit record is tied to one.
_Avoid_: caller, client, account

**Role**:
What a principal may do: staff (mask and unmask, own files), auditor (also read the audit
trail), admin (also rules, users and service keys). Managed in Maskroom, not by the identity
provider.
_Avoid_: permission level, group

**Service key**:
A named, revocable credential for a script, gateway or MCP server. Shown once at creation.
_Avoid_: API key, token (that word means a pseudonym here)

**Sign-on session**:
A user's login, held in a cookie and revocable by an administrator. Distinct from a
masking session, which is a vault.
_Avoid_: login token, auth session
