# Packaging the SafePII desktop helper

Turns `desktop/helper.py` into a signed MSI that an endpoint team can push
through Group Policy or Intune, and that reports when a newer build exists.

## Build it

On a Windows machine with Python 3.12:

```powershell
py -m pip install pyinstaller uiautomation
winget install Microsoft.DotNet.SDK.8     # or any .NET SDK; needed only for the MSI
dotnet tool install --global wix --version 5.*   # pin it; see below

powershell -ExecutionPolicy Bypass -File .\desktop\packaging\build.ps1 -SkipMsi
powershell -ExecutionPolicy Bypass -File .\desktop\packaging\build.ps1
powershell -ExecutionPolicy Bypass -File .\desktop\packaging\build.ps1 -Sign
```

**Pin WiX to v5 — there is exactly one version band that works.** The `<Files
Include>` element that harvests the built folder arrived in **v5**; on v4 the build
fails with "ComponentGroup contains an unexpected child element 'Files'". And **v6
and later** will not build at all until the **Open Source Maintenance Fee** EULA is
accepted, which for a product being sold is a commercial licence to settle rather
than a prompt to click through. So: v5. An unpinned install fetches v7 and stops
at the fee. `build.ps1` checks the installed version before trying, because both
failures read like this project's `.wxs` is at fault.

**The `-ExecutionPolicy Bypass` prefix is not optional** on a machine with the
default policy, which refuses to run the script and reports it in a way that reads
like the script is broken.

**A build after a test stops the executable first.** A helper or bridge still
running from the previous build holds its own DLLs open, and PyInstaller then
fails to clean its output with "Access is denied" on something like
`_internal\libcrypto-3.dll` — which reads like a permissions problem and is not.
The script stops anything running from its own `dist\` folder, and leaves an
installed helper in Program Files alone.

Note that the bridge, started by hand without redirected input, waits on stdin
forever rather than exiting. That is correct for a server Claude Desktop starts
and talks to; it does mean a manual check leaves a process behind.

**Start with `-SkipMsi`.** It freezes and stops, which is the quickest way to find
out whether the bundle is right before dealing with WiX at all — and the build
checks itself at that point: it starts the frozen executable as the stdio bridge
and asks it for its tools. That one question proves the executable runs, that
Python froze, that `broker.py` came with it, that `http.server` survived the
exclude list and that the `--stdio` entry point works. A frozen build fails in
ways the source never does, and quietly; this is what stops a silently crippled
MSI leaving the machine.

Output: `desktop\packaging\dist\SafePIIHelper-<version>.msi`.

The version comes from `__version__` in `desktop/helper.py` and nowhere else,
so the executable's Properties, the MSI, the log line at startup and the update
check can never disagree about which build this is.

## Deploying without the MSI, for a pilot

The MSI is packaging, not product. If `dotnet` is not on the machine — or is not
wanted on it — a pilot can be deployed by doing the four things the installer
does, which is all it does:

```powershell
# Elevated PowerShell. $src is what the build produced.
$src = "$HOME\safepii-build\desktop\packaging\dist\SafePIIHelper"
$dst = "C:\Program Files\SafePII"

# 1. The program, somewhere the user cannot write to.
Remove-Item $dst -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item $src $dst -Recurse

# 2. Start it for every user who signs in to this machine.
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run" `
    -Name SafePIIHelper -Value "`"$dst\SafePIIHelper.exe`""

# 3. The server address, as policy, so the user cannot point it elsewhere.
New-Item -Path "HKLM:\SOFTWARE\Policies\SafePII\Helper" -Force | Out-Null
Set-ItemProperty "HKLM:\SOFTWARE\Policies\SafePII\Helper" `
    -Name serverUrl -Value "https://safepii.example.com"

# 4. Start it now rather than waiting for the next sign-in.
Start-Process "$dst\SafePIIHelper.exe"
```

To remove it: delete the `SafePIIHelper` value under `Run`, stop the process, and
delete the folder.

**What you give up.** No upgrade story — a new build means repeating this, with
the old one stopped first, rather than `msiexec /i` replacing it in place. No
uninstall entry in Add/Remove Programs. No Start-menu shortcut. Nothing that an
endpoint-management tool can inventory or report compliance on. All of which
matter for a fleet and none of which matter for three machines you are watching.

Do not let this become the deployment. The MSI is what an endpoint team can
actually push, and it is also what gets signed.

## What the MSI does

- Installs **per machine**, into Program Files, so a standard user cannot
  replace the binary that reads their screen.
- Starts **per user**, through an `HKLM ... \Run` value. The helper has to run
  as the interactive user: it reads the accessibility tree of that user's
  session and installs hooks in it, which a service running as SYSTEM cannot do.
- Takes the server address at install time, so a fleet is configured in one
  step and the user cannot change it:

```
msiexec /i SafePIIHelper-0.3.0.msi SERVERURL=https://safepii.example.com /qn
```

  That writes the same policy key the
  [Group Policy template](../../enterprise/policies/windows/admx/README.md)
  uses, so the two agree.

- Upgrades in place. The upgrade code never changes, which is what tells
  Windows a new build replaces the old one rather than installing beside it.

## Signing

**This is a purchase with a lead time, and it blocks release.** Since
**1 June 2023** the CA/Browser Forum has required the private key of *every*
code signing certificate, organisation-validated as well as extended-validation,
to be generated and held in a FIPS 140-2 Level 2 or Common Criteria EAL4+
hardware module. A certificate file you can copy onto a build machine is no
longer issued. The options are:

- a **hardware token** posted to you, plugged into the build machine;
- a **cloud signing service** (Azure Trusted Signing, DigiCert KeyLocker,
  SSL.com eSigner), which suits an automated build better.

Set `SAFEPII_SIGN_COMMAND` to whatever your provider gives you; `build.ps1`
appends the file to sign and calls it, so a token, a cloud library and a plain
`signtool` invocation all work without changing the script.

Do not ship unsigned. A program that installs a global keyboard hook, unsigned,
will be stopped by SmartScreen, is likely to be quarantined by endpoint
protection, and will not pass application allowlisting. That is the whole point
of the control, and a bank will have all three.

## Updates

The server serves what the extension already does, in the same shape:

| Route | What it is |
|---|---|
| `GET /desktop/latest.json` | The newest published version, its size, its SHA-256 and where to get it |
| `GET /desktop/SafePIIHelper.msi` | That package |

Publish a build by dropping the MSI into the server's `EXT_DIST_DIR`, the same
folder the packaged extension goes in. The newest is chosen by **version
number**, not by file date, so re-copying an old file cannot look like a new
release. Both routes are reachable without signing in, because a machine fetches
them before anyone has.

The helper checks every six hours and, when it is behind, shows a quiet line in
its panel with a download link. **It does not install anything.** On a managed
fleet that is the endpoint team's job through the MSI, and a program that can
replace its own binary is a program worth attacking.

## Choices worth knowing about

**One directory, not one file.** A one-file build unpacks itself into a
temporary folder on every launch. That costs a second or two of startup, and it
is the shape antivirus engines most often flag: a program that installs a global
keyboard hook and unpacks an executable at runtime is a poor thing to hand an
endpoint team. The folder is inside the MSI, so nobody sees it.

**No UPX compression**, for the same reason and for no real gain.

**MSI rather than MSIX.** MSIX has a neat built-in update mechanism, but a
packaged app runs with a virtualised registry and filesystem, and whether that
affects a global low-level keyboard hook and always-on-top windows is the sort
of thing to establish on a real machine before betting a release on it. MSI is
plain Win32, deploys through Group Policy, and has no such questions.

**No separate harvest step.** WiX v5 globs the built folder itself, so there is
no generated file list that can fall out of step with what PyInstaller actually
produced.

**`comtypes` is named as a hidden import.** It builds its type-library wrappers
at import time and `uiautomation` reaches for them dynamically, so PyInstaller
cannot find them by following imports. Without those lines the frozen build
starts and then fails at the first accessibility call, which is a much worse
failure than not starting at all.
