# SafePII

Pseudonymization and redaction of personal data before it reaches an LLM or leaves the
organisation, with Sri Lankan identifiers as first-class citizens.

## Name

**SafePII** is the product: what a user reads in the web pages, the browser extension and
the desktop helper. **Maskroom** is the implementation's name and stays where renaming
would break something a customer has deployed — the Python package `maskroom`, the
`maskroom-admin` command, the `MASKROOM_*` environment variables and the
`X-Requested-With: maskroom` header that clients and server agree on. Neither name is
wrong; user-facing text says SafePII, code and configuration say maskroom.
_Avoid_: using Maskroom in anything a user reads.

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
locale policy (`maskroom/overlay.py`). Every saved version is a **revision**. Always name
it in full: the desktop helper paints a **screen overlay**, which is an unrelated thing.
_Avoid_: custom policy, config, overrides; a bare "overlay" for either sense

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
trail), admin (also rules, users and service keys). Managed in SafePII, not by the identity
provider.
_Avoid_: permission level, group

**Service key**:
A named, revocable credential for a script, gateway or MCP server. Shown once at creation.
_Avoid_: API key, token (that word means a pseudonym here)

**Sign-on session**:
A user's login, held in a cookie and revocable by an administrator. Distinct from a
masking session, which is a vault.
_Avoid_: login token, auth session

## Clients

**Client**:
Anything that masks on a user's behalf outside the web pages: the Chrome extension on
claude.ai, and the desktop helper on Claude Desktop. They share one server contract and
one vocabulary, and differ only in how they reach the composer.
_Avoid_: agent, plugin, integration

**Helper**:
The Windows program (`desktop/helper.py`) that protects Claude Desktop from outside the
application, through UI Automation. One program; the bar is its face.
_Avoid_: desktop app, agent, daemon

**Bar**:
The small pill the client shows above the composer: the Mask button, the mark, one word of
state, a count, and a chevron that opens the **panel**. The same thing in both clients.
_Avoid_: toolbar, widget, HUD, badge

**Panel**:
What the chevron opens: the current problems, the toggles, recent messages, and the
actions. Where a warning is kept until it is acknowledged.
_Avoid_: popup, menu, dropdown

**Guard**:
The check that runs before a message is sent, holding the send if anything was masked.
Named alone it means the message guard; the **file guard** is the same idea for an
attachment, and **drop blocking** refuses a dragged file it cannot check.
_Avoid_: auto-mask, interceptor, protection

**Held**:
What the guard does to a send or an attachment it will not let through yet: stopped,
visibly, with a way forward. Distinct from **blocked**, which is refusal with no way
forward, and from **failing open**, which is letting it through.
_Avoid_: blocked, cancelled, paused

**Screen overlay**:
The helper's transparent, click-through window that paints real values over the tokens in
a reply. Each painted value is a **patch**. It changes nothing in Claude. Distinct from the
**rules overlay**.
_Avoid_: a bare "overlay", annotation layer, injection

**Walk**:
One pass over the visible lines of a reply to find the tokens on screen and place their
patches. The unit the screen overlay's cost and its log lines are measured in.
_Avoid_: scan, sweep, refresh

**Administrator policy**:
A setting fixed by the organisation that the user cannot change — Group Policy in
`HKLM\SOFTWARE\Policies\SafePII\Helper` for the helper, managed storage for the
extension. Distinct from a **managed default**, which is pre-set for the user but theirs to
change, and from the **locale policy**, which is detection knowledge.
_Avoid_: config, setting, lock
