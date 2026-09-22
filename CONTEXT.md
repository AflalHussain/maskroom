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
