# Locking the SafePII desktop helper across a fleet

Everything the helper does can be fixed by an administrator, so a user cannot
switch protection off. This is the desktop counterpart of the Chrome extension's
managed policy, described in
[`docs/ENTERPRISE_ENFORCEMENT.md`](../../../../docs/ENTERPRISE_ENFORCEMENT.md).

## How it works

The helper reads three sources, in this order:

1. its own defaults;
2. `%ProgramData%\SafePII\helper.json`, a machine-wide **default** the user may override;
3. `%APPDATA%\SafePII\helper.json`, the user's own settings;
4. `HKLM\SOFTWARE\Policies\SafePII\Helper`, which **has the last word**.

Only an administrator can write to that registry key. A value set there cannot
be changed by the user: the panel shows the setting with a padlock, the toggle
refuses with a message naming the administrator, the settings window greys the
field, and the value is never written back into the user's file, so it does not
look chosen once the policy is withdrawn.

Nothing has to be configured. With no policy key present the helper behaves
exactly as it does today.

## Deploying it

**Group Policy.** Copy `SafePII.admx` into the domain central store at
`\\<domain>\SYSVOL\<domain>\Policies\PolicyDefinitions\`, and
`en-US\SafePII.adml` into the `en-US` folder beside it. On a single machine, use
`C:\Windows\PolicyDefinitions\` instead. The settings appear under **Computer
Configuration → Administrative Templates → SafePII → Desktop helper**.

**Intune.** Import the same two files under **Devices → Configuration →
Import ADMX**, then create an Administrative Template profile from them.

**Without either**, the registry key can be set directly, which is also what to
use for a quick test:

```
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v serverUrl /t REG_SZ /d "https://safepii.example.com" /f
reg add "HKLM\SOFTWARE\Policies\SafePII\Helper" /v guard /t REG_DWORD /d 1 /f
```

The helper reads policy at startup, so restart it after a change.

## What can be set

| Setting | Value | Type |
|---|---|---|
| SafePII server address | `serverUrl` | string |
| Check messages before they are sent | `guard` | 1 / 0 |
| Mask files before they are attached | `fileGuard` | 1 / 0 |
| Refuse files dragged onto Claude | `blockDrops` | 1 / 0 |
| Show real values in replies | `unmask` | 1 / 0 |
| Paint real values over tokens | `overlay` | 1 / 0 |
| Restore files that Claude produces | `watchDownloads` | 1 / 0 |
| Files whose contents cannot be checked | `restoreUnreadable` | `ask` / `always` / `never` |
| When the guard cannot run | `onGuardFailure` | `hold` / `warn` |
| Forget real values after idle minutes | `forgetAfterIdleMinutes` | number |
| Diagnostic detail in the log | `overlayDebug` | 1 / 0 |

A value that is not on this list is ignored and the helper says so in its log,
so a typo cannot stop it starting.

## The one decision worth making deliberately

`onGuardFailure` says what happens if the helper's own worker stops answering,
so nothing can be masked. **Hold** stops anything being sent, which is the
stricter reading of the promise, but makes Claude unusable until it recovers and
invites the user to close the helper, which protects nobody. **Warn** lets the
message through with a banner that stays up. Both are defensible; the default is
hold, and it is a setting rather than an assumption because it is the customer's
risk decision, not ours.
