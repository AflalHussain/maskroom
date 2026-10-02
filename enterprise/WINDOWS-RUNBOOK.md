# SafePII on Windows — sysadmin runbook

Deploy the desktop helper to a Windows fleet and close every path by which a file
could reach Claude Desktop unmasked. The browser side is a separate job with its
own runbook ([`RUNBOOK.md`](RUNBOOK.md)); do both, or a user simply opens Chrome.

Background: [`../docs/DESKTOP_APP_RESEARCH.md`](../docs/DESKTOP_APP_RESEARCH.md)
§5.6 and ADR [0009](../docs/adr/0009-folder-access-is-masked-by-being-the-only-way-in.md).
To try it on one machine first, [`../desktop/TESTING-FOLDER-SHARING.md`](../desktop/TESTING-FOLDER-SHARING.md)
walks the whole thing by hand.

## What this gives you

A user can talk to Claude Desktop about company files, and what leaves the machine
has had the personal data replaced with `TOK_<TYPE>_<ID>` tokens. The user still
reads the real values on screen, because the helper paints them back.

It rests on two halves, and **neither works alone**:

| | |
|---|---|
| **Claude cannot reach the filesystem** | `allowedWorkspaceFolders` set to `[]` |
| **SafePII can, and masks as it serves** | the helper's broker, registered as an MCP server |

Take away the first and Claude reads the raw folder directly, which nothing of
ours can see. Take away the second and Claude has no files at all.

---

## Prerequisites

Two lists. The first is what must be finished **before an administrator is given
this document**; none of it is their job, and handing the runbook over with any of
it outstanding wastes their time. The second is what they need on the day.

### Not ready until we have done these

| | Why it blocks | Where |
|---|---|---|
| **A code-signing certificate** — *for the real rollout, not the pilot* | The MSI installs a program that holds a global keyboard hook. Unsigned, SmartScreen stops it, endpoint protection is likely to quarantine it, and a publisher allowlisting rule cannot name it. Since 1 June 2023 the private key must live in a FIPS 140-2 Level 2 or CC EAL4+ module, so this is a purchase with a lead time, not a build step. **A pilot can go ahead without it** — see below | [`../desktop/packaging/README.md`](../desktop/packaging/README.md) |
| **One signed MSI built and installed end to end** | The build kit has never been run to completion on a Windows machine. Until a signed MSI has installed, started the helper, shared a folder and survived a reboot, this document describes something unproven | `desktop/packaging/build.ps1 -Sign` |
| **The server carrying this build** | `/api/event` (posture reporting) and `/desktop/latest.json` (update checks) exist in the source and not on the deployed image | [`../docs/DEPLOY_AWS.md`](../docs/DEPLOY_AWS.md) §1, §6 |
| **A decision on `onGuardFailure`** | `hold` stops anything being sent when the guard cannot run; `warn` lets it through loudly. It is the customer's call about their own risk, and it should be made before rollout rather than discovered during one | Step 2 |
| **A decision on folder sharing** | Whether users may share folders at all, and under which name policy. Step 2 again | Step 2 |
| **The browser half** | Locking Claude Desktop while leaving Chrome open protects nothing: the user opens claude.ai instead. That has its own prerequisites, including a packaged extension and its signing key | [`RUNBOOK.md`](RUNBOOK.md) |

### Piloting before the certificate arrives

The certificate blocks the *rollout*, not the *trial*, and waiting for it to
validate the design would be a waste of weeks. On machines the admin controls,
every control the certificate buys has an unsigned equivalent:

| Control | Signed | For a pilot |
|---|---|---|
| SmartScreen | passes on reputation | an admin installs it anyway; or exclude the path by policy |
| Endpoint protection | less likely to quarantine | add an exclusion for `C:\Program Files\SafePII` on the pilot machines |
| **Application allowlisting** | a **publisher** rule names the certificate | a **hash** or **path** rule names this exact binary |

That last row is the one that matters, because Step 4a — the control that stops a
user adding their own file-reading MCP server — leans on allowlisting. A hash rule
over `SafePIIHelper.exe` enforces exactly the same thing for a pilot group; it just
has to be reissued on every new build, which is why a publisher rule is what you
want at scale.

**What a pilot genuinely cannot tell you**: whether the signed artefact passes the
customer's own gates. Keep it to machines you can reimage, and do not take an
unsigned MSI past the pilot group.

The MSI itself is also optional for a pilot: it does four things — put the program
where the user cannot write to it, start it per user from `HKLM\…\Run`, write the
server address as policy, and add a shortcut — and all four are a handful of
commands. [`../desktop/packaging/README.md`](../desktop/packaging/README.md) spells
them out for a machine without the .NET SDK. What you lose is an upgrade path, an
uninstall entry and anything an endpoint tool can inventory, which is why it is a
pilot technique and not a deployment.

### What the administrator needs on the day

- **Domain or Intune** rights to push an MSI, ADMX templates and registry policy to
  `HKLM`.
- **The signed MSI**, and the SafePII server address.
- **Credentials for the clients**: either OIDC sign-on configured on the server, or
  one service key per machine (`maskroom-admin key create desktop-<machine>`).
- **Claude Desktop ≥ 1.26832.0** on every machine, for the per-folder `mode` field.
  Anything current is well past that; `winver` in the app's About dialog shows it.
- **Application allowlisting already operating** (AppLocker or WDAC). Step 4a leans
  on it, and it is the only thing that closes the one hole in this design. A fleet
  without it gets a weaker deployment, and should be told so rather than not.
- **A pilot machine** that can be broken and reimaged. Step 5 is not a formality:
  three of the behaviours here were wrong on a real machine and right on ours.

## Step 1 — Install the helper

Per machine, with the server address baked in:

```
msiexec /i SafePIIHelper-0.3.0.msi SERVERURL=https://safepii.yourco.example /qn
```

That writes the server address into SafePII's own policy key, so it agrees with
Step 2 rather than competing with it. The MSI installs into Program Files — so a
standard user cannot replace the binary that reads their screen — and starts the
helper per user through `HKLM\…\Run`, because it has to run as the interactive
user to read that session's accessibility tree.

Deploy it through Group Policy Software Installation or Intune like any other MSI.

---

## Step 2 — Lock SafePII's own settings

Import the template so the settings appear in the Group Policy editor:

- `enterprise/policies/windows/admx/SafePII.admx` → the domain central store
  (`\\<domain>\SYSVOL\<domain>\Policies\PolicyDefinitions\`)
- `enterprise/policies/windows/admx/en-US/SafePII.adml` → the `en-US` folder beside it

They appear under **Computer Configuration → Administrative Templates → SafePII →
Desktop helper**. Everything under
[`policies/windows/admx/README.md`](policies/windows/admx/README.md) can be fixed;
these are the ones that matter for folders:

| Setting | Value | Why |
|---|---|---|
| `serverUrl` | your server | Already set by the MSI; fix it here so it cannot drift |
| `guard` | `1` | Messages are checked before they send |
| `fileGuard` | `1` | Attachments are masked |
| `sharing` | `1` | The user may share a folder. `0` removes the feature entirely |
| `shareNames` | `mask` | Personal data in file names is masked. `handles` hides names completely, at the cost of Claude reading every file to find out what it has |
| `shareAllowCode` | `0` | Source files are refused rather than mangled or leaked |
| `sharePort` | `47821` | Must match Step 4 |
| `onGuardFailure` | `hold` | Nothing sends when the guard cannot run. `warn` trades that for availability — **decide this deliberately** |

A value set here cannot be changed by the user: the panel shows a padlock, the
toggle refuses by name, and it is never written back into the user's own file.

---

## Step 3 — Close Claude Desktop's other doors

All under `HKLM\SOFTWARE\Policies\Claude` (machine-wide; `HKCU` also works and is
useful for a quick test, but a user can write there, so do not rely on it).

**The one that does the work:**

```
reg add "HKLM\SOFTWARE\Policies\Claude" /v allowedWorkspaceFolders /t REG_SZ /d "[]" /f
```

`[]` means no folder may be attached. It is enforced against the *resolved* path,
so symlinks and `..` cannot escape it, and it **fails closed** — a policy the app
cannot parse leaves nothing attachable rather than everything. It also binds
Claude's own "may I have a folder?" tool: a request outside the allowed roots is
*"denied without prompting the user"*, so the model cannot talk a user into
granting one.

**The surfaces that would otherwise go round it:**

| Key | Set to | What it stops |
|---|---|---|
| `isLocalDevMcpEnabled` | **leave on** | It would stop a user adding their own MCP server — but it also stops *ours* loading, because on a standard deployment that is the only route (Step 4). Set it to `0` only in third-party mode |
| `isDesktopExtensionEnabled` | `0` | Desktop extensions, which can also read files |
| `isDesktopExtensionDirectoryEnabled` | `0` | Browsing for more of them |
| `isDesktopExtensionSignatureRequired` | `1` | If you do allow extensions, at least require a trusted publisher |
| `isClaudeCodeForDesktopEnabled` | `0` | Code sessions, where read-only is **not** enforced for Bash or SSH |
| `forceLoginOrgUUID` | your org | Sign-in with a personal account, which your controls do not reach |

Booleans are `REG_DWORD`; the list is `REG_SZ` holding JSON.

Leave `secureVmFeaturesEnabled` alone — that is Cowork itself, and with `[]` it is
already harmless, while turning it off removes the surface your users want.

---

## Step 4 — Point Claude Desktop at the broker

The helper serves on `http://127.0.0.1:47821/mcp` for as long as it runs, and
Claude Desktop has to be told. **On a standard deployment there is no policy that
does this.** Verified on a real machine, 2026-10-02: with `allowedWorkspaceFolders`
demonstrably in force on the same key, `managedMcpServers` was ignored in both its
HTTP and its stdio form, from `HKLM` and `HKCU`. Anthropic's own reference says why
— that key "applies only while the app runs in third-party mode (3P)".

So the registration goes in `claude_desktop_config.json`, the file that
**Developer → Open App Config File** opens. Its location varies: a packaged
install keeps it under `%LOCALAPPDATA%\Packages\…\LocalCache\Roaming\Claude\`,
nowhere near `%APPDATA%`. Use the menu item rather than guessing.

```json
{
  "mcpServers": {
    "safepii-files": {
      "command": "C:\\Program Files\\SafePII\\SafePIIHelper.exe",
      "args": ["--stdio", "--bridge"]
    }
  }
}
```

The same executable answers to both: given `--stdio` it is the bridge, and given
nothing it is the helper. `--bridge` means that process does not serve the folder
itself; it relays to the helper's broker, so the folder is still served once, by
the helper that holds the sign-in and the vault.

### What this costs, and how to get it back

That file is the user's to edit, and the route needs `isLocalDevMcpEnabled` to
stay **on** — the same key that would otherwise stop a user adding their own MCP
server. Two consequences, and only the second is a real risk:

- **A user can delete our entry.** They lose masked file access; they gain
  nothing, because `allowedWorkspaceFolders: []` still stands. The helper notices
  and says so, on the bar and in the audit trail.
- **A user can add a filesystem MCP server of their own** and read raw files
  through it. This is the one hole in the fleet story. Close it with the two
  controls below, which a regulated customer already operates.

**4a. Application allowlisting (AppLocker or WDAC) — the real control.** Any MCP
server a user adds has to execute something: `npx`, `node`, `python`, a binary.
Allow only signed company binaries and they cannot. This fits us exactly: our
bridge **is** `SafePIIHelper.exe`, the signed MSI binary, so the allowlist permits
ours and refuses theirs. No cooperation from Anthropic required.

**4b. Take write access to the config file away.** Write the entry as the
administrator, then deny the user `Write`, `WRITE_DAC` and `WRITE_OWNER` on it, so
they can neither edit it nor give themselves back the right to:

```powershell
$f = "<path from Developer → Open App Config File>"
icacls $f /inheritance:d
icacls $f /grant "Administrators:(F)" "SYSTEM:(F)"
icacls $f /deny  "$env:USERNAME:(W,WDAC,WO)"
```

A local administrator can undo this; a standard user cannot. If your users are
local administrators, none of this section means anything and 4a is all you have.

**4c. SafePII reports what it cannot prevent.** The helper watches that file and,
when an MCP server appears that is not ours, warns on the bar and records
`unmanaged-mcp-server` against the machine in the audit trail — with the server's
name and the path it was found at. It does the same when SafePII is *missing*
from the configuration, which is otherwise a silent "why does nothing work". Set
`watchClaudeConfig` to `0` to switch it off; there is rarely a reason to.

**If the customer runs Claude Desktop in third-party mode**, none of this applies:
`managedMcpServers` works there, so the registration is policy, `isLocalDevMcpEnabled`
can be `0`, and the hole closes properly. It is the only configuration where this
is fully enforceable, and it is a commercial decision rather than a technical one.

## Step 5 — Verify on one machine

1. **The helper is running and listening.**
   ```powershell
   Invoke-RestMethod -Uri http://127.0.0.1:47821/mcp -Method Post -ContentType application/json -Body '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```
   Five tools, `request_folder` among them.
2. **Policy reached the helper.** Open the panel: locked settings show a padlock.
   The log says `policy: ignoring unknown setting …` if you mistyped one.
3. **Folders cannot be attached.** In Cowork, try to add one. It should refuse:
   *"administrator has restricted which folders can be used here."*
4. **Claude can see the tools.** Ask it to list files; with nothing shared it
   should offer to ask for a folder rather than report an error.
5. **The round trip.** Share a folder from the bar, ask Claude to read a file, and
   check the reply is tokens on Claude's side and real values under your mouse.
6. **Posture reporting works.** Add a dummy MCP server to
   `claude_desktop_config.json`, wait a few seconds, and the bar should warn that
   Claude Desktop has a server SafePII does not manage. It should appear in the
   audit trail as `unmanaged-mcp-server` against that machine. Remove it again.
7. **The log tells the story**, at `%APPDATA%\SafePII\helper.log`:
   `broker listening …`, `sharing <path> …`, `shared folder vault: N new token(s)`,
   `watching Claude Desktop's config at …`, `posture: …`.

---

## Rolling it out to a fleet

Steps 1 to 5 describe one machine. A fleet is not one machine repeated, and the
order matters because two of these are visible to users the moment they land.

1. **Pilot, 1 machine.** Steps 1–5 by hand, on something reimageable. Confirm the
   whole round trip, not just that the helper starts.
2. **Pilot group, 5–10 machines, policy only.** Push Steps 2 and 3 — SafePII's
   settings and `allowedWorkspaceFolders: []` — *without* the MSI, to a group that
   does not use Cowork folders. This proves the policy lands on real, varied
   machines and is quiet: the only visible change is that folders cannot be
   attached.
3. **Pilot group, with the helper.** Add Step 1 and Step 4 to the same group. Now
   it is visible: a bar appears above the composer. Tell them first, and tell them
   what it is for — a privacy tool that arrives unannounced gets reported to the
   service desk as malware, and they are not wrong to.
4. **Measure before widening.** Over a week on the pilot group, look for
   `unmanaged-mcp-server` in the audit trail, helper restarts in the log, and
   anything in Recent that users have had to acknowledge. The point of a pilot is
   to find the thing nobody predicted; three of the behaviours in this document
   were wrong on a real machine and right in our tests.
5. **Widen by department**, not all at once, and keep one group unpoliced until
   last so there is somewhere to compare against.
6. **Then the browser half** ([`RUNBOOK.md`](RUNBOOK.md)), or users simply move to
   Chrome and the measurement above means nothing.

**Order these two together.** `allowedWorkspaceFolders: []` without the helper
takes a feature away and gives nothing back; the helper without the policy leaves
the raw folder reachable. Either alone is worse than neither, so stages 2 and 3
should be days apart, not weeks.

## What to tell users, once

The deployment is not silent and should not pretend to be. One paragraph, from
their own IT, before stage 3 lands:

> A tool called SafePII now runs alongside Claude. It replaces personal data —
> names, NIC numbers, phone numbers, account numbers — with placeholders before
> anything leaves your machine, and shows you the real values on your screen. You
> will see a small bar above the message box. Claude can no longer open folders on
> your computer directly; ask it for files and it will ask you to choose a folder
> through SafePII, which masks them as it hands them over. If something looks
> wrong, the bar has a panel with the last few things it did.

## Step 6 — Updating

Publish a new MSI by dropping it in the server's `EXT_DIST_DIR`; the helper checks
every six hours and **shows** a line in its panel. It installs nothing: on a
managed fleet that is your job through the MSI, and a program that can replace its
own binary — one holding a global keyboard hook — is a program worth attacking.

---

## What this enforces, and what it does not

Worth reading before you tell anyone it is airtight.

**Enforced by policy, not by us:**

- No folder can be attached to Cowork, including by Claude asking the user.
- SafePII's own settings cannot be changed by the user.
- The server address cannot be pointed somewhere else.

**Not enforced, and you should know:**

- **A user can kill the helper.** It runs in their session; Task Manager ends it,
  and nothing restarts it until they sign in again. With
  `allowedWorkspaceFolders: []` they lose file access rather than gain it, and the
  message guard goes with it — so messages they type are no longer checked. Have
  endpoint management watch for the process if that matters to you.
- **A user can add their own file-reading MCP server**, because registering ours
  needs `isLocalDevMcpEnabled` on and a standard deployment has no policy route
  (Step 4). Application allowlisting is the control; the config-file ACL raises
  the bar; SafePII reports it either way. In third-party mode this closes
  properly.
- **Read-only is a Cowork guarantee, not a Code one.** In Code sessions `mode: ro`
  binds Claude's file tools but not Bash or SSH, which is why Step 3 switches Code
  off.
- **Nothing here stops a user copying a file somewhere else** — email, a browser
  upload, a USB stick. That is DLP's job, not this.
- **The browser is a separate fleet control.** Without [`RUNBOOK.md`](RUNBOOK.md),
  a user opens claude.ai in Chrome and none of the above applies.

---

## Every registry value in one place

```
:: SafePII's own settings
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v serverUrl   /t REG_SZ    /d "https://safepii.yourco.example" /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v guard       /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v fileGuard   /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v sharing     /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v shareNames  /t REG_SZ    /d "mask" /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v shareAllowCode /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v sharePort   /t REG_DWORD /d 47821 /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v onGuardFailure /t REG_SZ /d "hold" /f

:: Claude Desktop
reg add "HKLM\SOFTWARE\Policies\Claude" /v allowedWorkspaceFolders /t REG_SZ /d "[]" /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isDesktopExtensionEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isDesktopExtensionDirectoryEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isClaudeCodeForDesktopEnabled /t REG_DWORD /d 0 /f
```

Both applications read policy at startup, so restart them after a change.

`managedMcpServers` is **not** in that list, and `isLocalDevMcpEnabled` is left
alone, for the reason in Step 4: on a standard deployment the first is ignored and
the second is what loads our broker. In third-party mode, add the first and set
the second to `0`.

```
:: Third-party mode only
reg add "HKLM\SOFTWARE\Policies\Claude" /v managedMcpServers /t REG_SZ /d "[{\"name\":\"safepii-files\",\"url\":\"http://127.0.0.1:47821/mcp\",\"transport\":\"http\"}]" /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isLocalDevMcpEnabled /t REG_DWORD /d 0 /f
```
