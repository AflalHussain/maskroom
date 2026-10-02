---
status: accepted
date: 2026-09-28
---

# The helper ships as a per-machine MSI, locked by registry policy, and never updates itself

A protection a user starts by hand is not a control. For the desktop helper to be worth
anything to a bank's security team, it has to arrive through the channel they already use,
be impossible for the user to switch off, and be verifiable as ours.

We decided:

- **PyInstaller builds a one-directory executable**, and **WiX v5 packages it as an MSI**
  (`desktop/packaging/`). The version comes from `__version__` in `desktop/helper.py` and
  nowhere else, so the file properties, the MSI, the startup log line and the update check
  cannot disagree.
- **Installed per machine** into Program Files, so a standard user cannot replace the binary
  that reads their screen; **started per user** through an `HKLM ...\Run` value, because the
  helper must run as the interactive user to read that session's accessibility tree and
  install hooks in it. A service running as SYSTEM cannot.
- **Settings are fixed by Group Policy**, in `HKLM\SOFTWARE\Policies\SafePII\Helper`, which
  has the last word over the machine-wide defaults file and the user's own settings
  (`enterprise/policies/windows/admx/`). A policy-set value shows a padlock in the panel and
  is never written back into the user's file. The MSI writes the same key from its
  `SERVERURL` property, so an installer and a policy agree.
- **The helper checks for a newer build every six hours and installs nothing.** It shows a
  line in its panel with a link. The server publishes `GET /desktop/latest.json` and
  `GET /desktop/SafePIIHelper.msi`, both open, chosen by version number rather than file
  date.
- **It must be signed before release.** Since 1 June 2023 the CA/Browser Forum requires every
  code-signing private key, OV as well as EV, to live in a FIPS 140-2 Level 2 or CC EAL4+
  hardware module, so a certificate file that can be copied to a build machine is no longer
  issued: it is a hardware token or a cloud signing service, with a lead time.
  `SAFEPII_SIGN_COMMAND` keeps `build.ps1` indifferent to which.

## Considered options

- **MSIX.** Has a built-in update mechanism, which is exactly what we are missing. Rejected
  for now: a packaged app runs with a virtualised registry and filesystem, and whether that
  affects a global low-level keyboard hook and always-on-top windows is a thing to establish
  on a real machine before betting a release on it. MSI is plain Win32 and raises no such
  question.
- **A one-file executable.** Rejected: it unpacks itself to a temporary folder on every
  launch, which costs startup time and is the shape endpoint protection flags most often. A
  program that installs a global keyboard hook and unpacks an executable at runtime is a poor
  thing to hand a security team. UPX compression is off for the same reason.
- **Self-updating.** Rejected deliberately. On a managed fleet, replacing the binary is the
  endpoint team's job through the MSI, and a program that can replace its own binary — one
  that reads every keystroke while Claude is in front — is a program worth attacking.
- **Ship unsigned and sign later.** Rejected. Unsigned, it will be stopped by SmartScreen,
  is likely to be quarantined, and will not pass application allowlisting. Those are the
  controls the customer has, working correctly.
- **Locking settings in a config file** instead of the registry. Rejected: a file the user
  can write is a default, not a policy. The distinction is the control the fleet's security
  team asks for by name.

## Consequences

- Release is blocked on a purchase, not on code (PKG-2). The build kit is complete and
  produces an unsigned MSI today, which is testable but not shippable.
- Publishing a build is a file copy into the server's `EXT_DIST_DIR`, the same folder the
  packaged extension goes in.
- An unknown policy value is ignored and logged, so a typo in a GPO cannot stop the helper
  starting.
- Two machine-level writes are needed at install time (Program Files, `HKLM\...\Run`), so
  installation requires administrator rights. That is expected for this class of software and
  is what Group Policy and Intune deployment provide.
- `comtypes` is named as a hidden import: it builds its type-library wrappers at import time
  and `uiautomation` reaches for them dynamically, so without it the frozen build starts and
  then fails at the first accessibility call, which is a worse failure than not starting.
