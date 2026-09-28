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
- **A filesystem filter driver, or a Windows Projected File System provider**, presenting
  masked content on demand with no duplication. Technically the nicest answer and rejected for
  now: whether a projected filesystem survives being mounted into the sandbox VM is untested,
  and it is a large piece of work resting on that assumption. Revisit only if both mechanisms
  above prove insufficient.
- **A pre-masked mirror as the only design.** Kept as the working profile, not as the answer.
  It duplicates data, can fall stale between passes, and needs write-back designed; the strict
  profile has none of those problems.
- **Do nothing and forbid folder use by policy.** `allowedWorkspaceFolders: []` on its own is
  exactly this, and it is a legitimate answer for a customer who does not need the feature. It
  is the same refusal-as-solution we rejected for the desktop as a whole in ADR 0004, so it is
  the floor rather than the plan.

## Consequences

- SafePII becomes a *file server* to the model, not only a masking API. That is a new client
  shape: tools for listing, reading, searching and writing back, each one masking or restoring
  as it goes.
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
- `mode: "ro"` is stronger than the public documentation suggests, and only in Cowork. The
  schema states that Bash there runs with the folder "mounted read-only at the OS level" and
  file-tool writes blocked in-process; the caveat about shells not enforcing it applies to Code
  sessions and SSH. The working profile depends on Cowork's guarantee and must say that it is a
  Cowork guarantee.
- The mirror root does not have to exist before the policy names it: the app creates an
  admin-configured workspace folder it cannot find.
- What is left to establish on a real machine is small and named in §5.6.6: that a Cowork task
  with no folder attached lists and calls a loopback managed server's tools, and how the agent
  behaves when a tool result is visibly tokenised. The second is a design question — the tool
  description and each result say what the tokens are, which is more than the chat preamble can
  do — and the failure worth watching for is not confusion but helpfulness: an agent that
  decides a token is a typo and corrects it.
