---
status: accepted
date: 2026-09-28
---

# A folder handed to Claude is masked by being the only way in, not by intercepting reads

A user who attaches a folder to Cowork hands Claude Desktop everything in it, and the guard
that protects the composer never sees it. The obvious instinct — intercept the read and mask
it on the way past — cannot be satisfied: those reads happen inside the sandbox VM against a
mount the app makes, Cowork's delivery of Claude Code hooks is unresolved and unstable, and
the folder allowlist resolves symlinks, so a farm of links cannot stand in front of the real
files. The research is in `docs/DESKTOP_APP_RESEARCH.md` §5.6.

We decided to stop trying to sit in the read path and instead **remove every path that is not
ours**. Two documented, administrator-enforced surfaces do that together:

- **`allowedWorkspaceFolders`** decides what a user may attach at all. `[]` means "No folders
  may be attached … cannot read or write the user's filesystem"; a single root confines
  attachment to what we put there; `mode: "ro"` lets the agent view and search without
  modifying, and `isDefaultSelected` makes the right folder the obvious one.
- **A local MCP server**, which is a host-side process we own. Its results are produced by our
  code, so a file is masked *as it is served*: nothing pre-computed, nothing masked written to
  disk, and every read recorded in the audit trail. It is an **MCP endpoint over HTTP on
  `127.0.0.1`, served by the helper**, and it is deployed to a fleet through the
  `managedMcpServers` policy key, because that key accepts only `http` and `sse` transports and
  loopback is the one plain-HTTP endpoint the app's URL check tolerates. A stdio `.mcpb`
  extension is the prototype shape, not the deployable one: stdio servers are user-added, under
  `isLocalDevMcpEnabled`.

Two profiles follow, and a customer picks one. **Strict**: folders off entirely, the SafePII
MCP file broker the only way files reach the agent. **Working**: one allowed root holding
masked copies, read-only, so the agent can still run code — against masked data.

The strict profile is what we lead with. It keeps the enforcement in Anthropic's own key
rather than in something of ours that can be switched off, has no mirror to fall stale, and
duplicates nothing sensitive.

Both profiles were checked against the app's own configuration schema before this was written
(§5.6.6): `allowedWorkspaceFolders` carries `scopes: ["3p", "1p"]` and accepts the per-folder
object in standard mode as well as third-party, so the working profile is deployable on a
claude.ai-sign-in fleet; managed MCP servers are connected by "Cowork, Chat and Code sessions",
so the broker is not a Chat-only device; and the policy fails closed (`failClosedValue: []`),
so a policy the app cannot parse leaves no folder attachable rather than every folder.

## Considered options

- **Mask at read time through a Claude Code hook.** The natural answer, and not available:
  hook support in Cowork is an open request whose central problem is that the configuration
  lives on the host while the session runs in a Linux sandbox, and there are reports of hooks
  firing when they were disabled. A control has to be dependable in both directions.
- **A mirror of symlinks**, so nothing is copied. Rejected on a documented fact: the allowlist
  is enforced against the resolved path.
- **A synthetic filesystem — FUSE, WinFsp or ProjFS — presenting masked content under the
  folder the user already attaches.** Technically the most appealing answer, and the only one
  that would also mask what `grep` and `Bash` see inside the sandbox, which no MCP server can
  reach. Deferred, assessed in full at §5.6.2. The blocking objection is not the platform work
  but the size contract: a filesystem must answer `getattr` before anyone opens anything, and
  masked content is a different length, so either reads are wrong or every file is masked to
  answer a `stat` — which `ls -l`, `find` and `grep -r` all perform. On top of that it is a
  kernel-mode driver per platform (no FUSE on Windows), virtiofsd's cache mode and DAX
  behaviour cannot be read from the package, and it would still need
  `allowedWorkspaceFolders`, because the raw folder would still exist. It replaces the
  delivery, not the control. Revisit for a customer who needs code execution over masked
  data.
- **A pre-masked mirror as the only design.** Kept as the working profile, not as the answer.
  It duplicates data, can fall stale between passes, and needs write-back designed; the strict
  profile has none of those problems.
- **Do nothing and forbid folder use by policy.** `allowedWorkspaceFolders: []` on its own is
  exactly this, and it is a legitimate answer for a customer who does not need the feature. It
  is the same refusal-as-solution we rejected for the desktop as a whole in ADR 0004, so it is
  the floor rather than the plan.

We also decided **who asks for the folder**. Claude Desktop has a built-in tool for
requesting folder access mid-session — one dialog, exact paths, granted for that session — and
that interaction is the one users will already know. Rather than requiring the person to share
a folder before they start, the broker offers `request_folder`: Claude calls it when it needs
files and none are shared, the SafePII picker opens carrying the model's stated reason, and the
tool call waits for the answer. The flow becomes *just ask Claude*. Two rules are copied
straight from Anthropic's own tool description, which has evidently learned them: ask once for
the minimal set, and on a decline ask in conversation rather than again.

## Consequences

- SafePII becomes a *file server* to the model, not only a masking API. That is a new client
  shape: tools for listing, reading, searching and writing back, each one masking or restoring
  as it goes. `desktop/broker.py` is the prototype.
- **Telling Claude Desktop where the broker is has three doors, and only two are usable.**
  The managed `managedMcpServers` policy takes HTTP on `127.0.0.1` and is the deployment
  answer. The app's own *Add a connector* field is not: it is for a remote server and requires
  an https address, which a loopback endpoint cannot offer without a certificate the machine
  trusts. The third is `claude_desktop_config.json` and stdio, where Claude starts the process
  itself — `broker.py --stdio --bridge` relays to the helper's own broker, so the folder is
  still served once, by the helper holding the sign-in and the vault, and the transport is the
  only thing that changes. The bridge answers `initialize` and `tools/list` on its own when
  nothing is being served, because Claude Desktop starts it before anybody has shared a folder
  and a failure at that moment marks the connector broken for the session.
- **The broker must route by file shape, not treat everything as text.** A table sent through
  `/api/mask` comes back with its identifiers masked and its people not, because a name in a
  comma-separated row has no context around it. Tabular files go through `/api/process`, which
  masks by column. Verified against a live server, and the reason a broker can look like it
  works while leaking.
- **File names are replaced with handles by default, not masked.** Masking a name is
  best-effort in a way masking content is not — a path separator or an underscore defeats the
  detector, and a probe that gets around them read a file extension as a surname. A handle
  cannot leak what the detector misses; masking names stays available for a folder whose names
  matter to the work, and is documented as best-effort.
- The strict profile costs the model shell access to the real files. For an analyst with
  spreadsheets that is acceptable; for a developer it is not, which is why the working profile
  exists.
- Several problems have to be designed rather than met in production, and they are recorded in
  §5.6.5: filenames disclose before a byte is read, search stops matching unless the query is
  masked with the same vault, code must be passed through unmasked rather than corrupted,
  `TOK_…` is longer than the value so offsets drift, and a file Claude writes comes back full
  of tokens.
- A folder session is one more vault. Because restore already searches every known vault
  (ADR 0007), the chat surfaces — hover, copy, the screen overlay — need no changes.
- **Dropping the values and stopping the folder are separate things**, learned on the first
  Windows run. The helper forgets the real values when the desk is unattended, and that was
  wired to stop serving too; locking the screen then killed a folder mid-task. But the folder
  is masked by the server, so nothing about it depends on what this process holds: the values
  go, the serving continues, and the values are fetched again when the person returns. Signing
  out is different and does stop it, because the broker serves with that sign-in.
- `mode: "ro"` is stronger than the public documentation suggests, and only in Cowork. The
  schema states that Bash there runs with the folder "mounted read-only at the OS level" and
  file-tool writes blocked in-process; the caveat about shells not enforcing it applies to Code
  sessions and SSH. The working profile depends on Cowork's guarantee and must say that it is a
  Cowork guarantee.
- The mirror root does not have to exist before the policy names it: the app creates an
  admin-configured workspace folder it cannot find.
- **The broker listens for the helper's lifetime, not the folder's.** It used to come up when
  a folder was shared, which made "nothing shared" indistinguishable from "nothing running" —
  confusing in practice — and, more decisively, left Claude unable to ask for a folder, since
  asking goes through the broker. One server; the folder is swapped in beneath it, so a change
  of folder does not break the connection Claude Desktop already holds. With nothing shared,
  a file tool answers with what to do about it rather than failing.
- **A request has to end, one way or another.** `request_folder` blocks the model's tool call
  because the result *is* what the person decided, but every exit answers it: a share, a
  cancelled picker, a folder with nothing servable in it, or a timeout. A tool call left
  hanging is worse than one that says nobody answered.
- What is left to establish on a real machine is small and named in §5.6.6: that a Cowork task
  with no folder attached lists and calls a loopback managed server's tools, and how the agent
  behaves when a tool result is visibly tokenised. The second is a design question — the tool
  description and each result say what the tokens are, which is more than the chat preamble can
  do — and the failure worth watching for is not confusion but helpfulness: an agent that
  decides a token is a typo and corrects it.
