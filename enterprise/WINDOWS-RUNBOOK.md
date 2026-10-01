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

## Before you start

- **A signed MSI.** `desktop/packaging/build.ps1 -Sign` produces it. Do not deploy
  unsigned: it installs a global keyboard hook, and SmartScreen, endpoint
  protection and application allowlisting will all object — correctly.
  [`../desktop/packaging/README.md`](../desktop/packaging/README.md) covers the
  certificate, which is a purchase with a lead time.
- **A reachable SafePII server**, and a way for clients to authenticate: either
  OIDC sign-on, or a service key per machine (`maskroom-admin key create …`).
- **Claude Desktop ≥ 1.26832.0** on every machine, for the per-folder `mode`
  field. Anything current is well past that.

---

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
| `isLocalDevMcpEnabled` | `0` | **A user adding their own MCP server** — a filesystem server would read raw files and SafePII would never see it. See the caveat in Step 4 |
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

The helper serves on `http://127.0.0.1:47821/mcp` for as long as it runs. Claude
Desktop has to be told, and there are two ways with very different properties.

**The managed way, which is the one to deploy:**

```
reg add "HKLM\SOFTWARE\Policies\Claude" /v managedMcpServers /t REG_SZ /d "[{\"name\":\"safepii-files\",\"url\":\"http://127.0.0.1:47821/mcp\",\"transport\":\"http\"}]" /f
```

A managed server may speak only `http` or `sse`, and loopback is the one
plain-HTTP endpoint the app accepts without complaint. Being policy, the user
cannot remove it — and because it needs nothing from `isLocalDevMcpEnabled`, you
can switch that off in Step 3 and close the bypass.

> **Verify this one on a machine before you roll it out.** How a JSON document is
> encoded in a flat registry value is the single thing that could not be confirmed
> by reading the application package; the Windows registry reader is not in the
> published Linux build. The helper logs the exact shape it expects at startup:
> `managed configuration: {"managedMcpServers": …}`.

**The fallback, if the managed key will not take:** a stdio server in
`claude_desktop_config.json` (the file **Developer → Open App Config File**
opens):

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

**Know what this costs you.** That file is user-writable, and the route depends on
`isLocalDevMcpEnabled` staying **on** — which is exactly the key that otherwise
stops a user adding their own file-reading MCP server. So:

- A user can delete our entry. They lose the feature; they do **not** gain
  unmasked access, because `allowedWorkspaceFolders: []` still stands.
- A user can add a filesystem MCP server of their own and read raw files through
  it. **This is a real bypass**, and it is the reason the managed route is worth
  getting working.

---

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
6. **The log tells the story**, at `%APPDATA%\SafePII\helper.log`:
   `broker listening …`, `sharing <path> …`, `shared folder vault: N new token(s)`.

---

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
- **The stdio route leaves `isLocalDevMcpEnabled` on**, which lets a user add
  their own file-reading MCP server. Use the managed route (Step 4).
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
reg add "HKLM\SOFTWARE\Policies\Claude" /v managedMcpServers /t REG_SZ /d "[{\"name\":\"safepii-files\",\"url\":\"http://127.0.0.1:47821/mcp\",\"transport\":\"http\"}]" /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isLocalDevMcpEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isDesktopExtensionEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isDesktopExtensionDirectoryEnabled /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Policies\Claude" /v isClaudeCodeForDesktopEnabled /t REG_DWORD /d 0 /f
```

Both applications read policy at startup, so restart them after a change.
