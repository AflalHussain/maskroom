# Packaging the SafePII desktop helper

Turns `desktop/helper.py` into a signed MSI that an endpoint team can push
through Group Policy or Intune, and that reports when a newer build exists.

## Build it

On a Windows machine with Python 3.12:

```powershell
pip install pyinstaller uiautomation
dotnet tool install --global wix          # the WiX toolset, for the MSI
.\desktop\packaging\build.ps1             # unsigned, for testing
.\desktop\packaging\build.ps1 -Sign       # for release; see Signing below
```

Output: `desktop\packaging\dist\SafePIIHelper-<version>.msi`.

The version comes from `__version__` in `desktop/helper.py` and nowhere else,
so the executable's Properties, the MSI, the log line at startup and the update
check can never disagree about which build this is.

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

**No separate harvest step.** WiX v4 globs the built folder itself, so there is
no generated file list that can fall out of step with what PyInstaller actually
produced.

**`comtypes` is named as a hidden import.** It builds its type-library wrappers
at import time and `uiautomation` reaches for them dynamically, so PyInstaller
cannot find them by following imports. Without those lines the frozen build
starts and then fails at the first accessibility call, which is a much worse
failure than not starting at all.
