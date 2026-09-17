# Maskroom enterprise deployment — sysadmin runbook

Force-install the Maskroom extension across a managed Chrome fleet and lock its guard
on. Background and citations: [`../docs/ENTERPRISE_ENFORCEMENT.md`](../docs/ENTERPRISE_ENFORCEMENT.md).

- **Fixed extension ID:** `lcmdehcdpfddkjgajmlpgfholdekpgio`
  (set by the `key` in [`../extension/manifest.json`](../extension/manifest.json); stable on
  every machine and every rebuild).
- **Signing key:** `local/maskroom-signing-key.pem` in the source tree — **secret**, git-ignored.
  Whoever packages the `.crx` needs it; store it in the company secret vault, never commit it.
- **Ready-made policy files:** in [`policies/`](policies) and [`update.xml`](update.xml),
  pre-filled with the ID. Replace `EXTENSIONS.YOURCO.EXAMPLE` and `yourco` with real values.

There are two independent things to push: **force-install** (Chrome-native) and **guard
lock** (our per-extension config). Do both. Also disable Incognito, or the guard is skipped.

---

## Step 1 — Decide how the extension is hosted

| | Unlisted Chrome Web Store (recommended) | Self-hosted |
|---|---|---|
| Effort | Upload once; Google hosts + serves updates | You run an HTTPS server, sign + host the `.crx` and `update.xml` |
| `update_url` | `https://clients2.google.com/service/update2/crx` | your `update.xml` URL |
| Use when | Default | Company forbids the Web Store / must not use Google infra |

If Web Store: publish the built `extension/` folder as **unlisted**, then in the policy files
replace the `update_url` with Google's URL above and skip Step 2.

---

## Step 2 — (Self-hosted only) package and host

1. **Package the `.crx`** using the fixed signing key (from a machine with Chrome):
   ```
   google-chrome --pack-extension=/path/to/extension \
                 --pack-extension-key=/path/to/maskroom-signing-key.pem
   ```
   This writes `extension.crx` next to the folder. Rename it `maskroom-<version>.crx`
   (version = `manifest.json`'s `version`, currently `0.1.0`).
   **The key must be PKCS#8** (`-----BEGIN PRIVATE KEY-----`). Chrome rejects the older
   PKCS#1 form (`BEGIN RSA PRIVATE KEY`) with *"private key must be a valid format
   (PKCS#8-format PEM-encoded RSA key)"*. The shipped `local/maskroom-signing-key.pem` is
   already PKCS#8; to convert any PKCS#1 key without changing the extension ID:
   ```
   openssl pkcs8 -topk8 -nocrypt -in old-key.pem -out maskroom-signing-key.pem
   ```
   *(Chrome UI equivalent: `chrome://extensions → Pack extension`, pointing at the folder and
   the existing `.pem`. Do not let it generate a new key — that would change the ID.)*

2. **Host the `.crx` and its update manifest.** The Maskroom server does this for you — no
   manual `update.xml` editing per host:
   - **Drop the packaged `.crx` where the server looks for it.** By default that is the
     `local/` folder in the source tree; override with the `EXT_DIST_DIR` environment
     variable. The server picks the highest-versioned `maskroom-*.crx` (or any `*.crx`) there.
   - The server then serves, over whatever host/scheme the request arrives on:
     - **`GET /ext/update.xml`** — generated on the fly; its `codebase` and the app id are
       filled in from the request (honouring `X-Forwarded-Proto`/`X-Forwarded-Host` behind a
       proxy or tunnel), so the same deployment works on `localhost`, an ngrok URL, or the
       production hostname **without editing any file**.
     - **`GET /ext/maskroom.crx`** — the packaged extension, served with
       `Content-Type: application/x-chrome-extension` (required, or Chrome rejects the
       download).
   - Point `update_url` in the install policy at **`https://<your-maskroom-host>/ext/update.xml`**
     (the policy files ship with `https://MASKROOM.YOURCO.EXAMPLE/ext/update.xml` — replace
     the host only).

   *Manual alternative:* host `maskroom-<version>.crx` and the static [`update.xml`](update.xml)
   (fix its `codebase`) on any HTTPS URL yourself, and point `update_url` there instead.

3. Point `update_url` in the install policy at the **`update.xml`** URL (not the `.crx`).

---

## Step 3 — Push the policies

Pick the platform. Replace the placeholder host/org first.

**Chrome Browser Cloud Management (any OS):** follow
[`policies/cbcm-console.md`](policies/cbcm-console.md). No files to host if using the Web Store.

**Linux** — copy into `/etc/opt/chrome/policies/managed/` (root):
```
sudo cp policies/linux/maskroom-install.json /etc/opt/chrome/policies/managed/
sudo cp policies/linux/maskroom-guard.json   /etc/opt/chrome/policies/managed/
```
`maskroom-install.json` force-installs + pins + disables Incognito; `maskroom-guard.json`
locks the guard.

**Windows** — import [`policies/windows/maskroom.reg`](policies/windows/maskroom.reg)
(or set the same values via GPO). It covers force-install, Incognito off, and the guard lock.

**macOS** — deliver the two plists in [`policies/macos/`](policies/macos) through your MDM:
`com.google.Chrome.plist` (force-install + Incognito) and
`com.google.Chrome.extensions.<ID>.plist` (guard lock).

---

## Step 4 — Verify

On a managed machine, fully quit and reopen Chrome, then:

1. `chrome://policy` → **Reload policies**. Confirm:
   - `ExtensionSettings` lists the ID with `force_installed`.
   - `IncognitoModeAvailability` = 1.
   - Under the extension, `guardLocked` = true.
2. `chrome://extensions`: Maskroom is present, has **no remove/disable control** (managed),
   and is pinned.
3. Open `https://claude.ai`: the bar shows **`guard: on 🔒`**, clicking it does nothing, and
   the options page shows the Guard checkbox disabled and "locked by your administrator".
4. The **Maskroom server URL** is pushed to every install by the `serverUrl` key in the
   guard-lock config (Step 3), so users never type it and the options field is locked. Confirm
   the extension reaches it — the bar shows a session id and a pseudonym count, not "no session".

---

## Step 5 — Updating later

1. Bump `version` in `extension/manifest.json`.
2. Web Store: re-upload. Self-hosted: repackage with the **same** `.pem` and drop the new
   `maskroom-<version>.crx` in `EXT_DIST_DIR` (default `local/`) — the server picks the
   highest version and regenerates `/ext/update.xml` automatically; no file edits. (Manual
   hosting: upload the new `.crx` and edit `update.xml`'s `version` + `codebase`.) Version
   must increase.
3. Chrome auto-updates on its schedule; no user action.

---

## What this does and does not guarantee

Guaranteed on a **managed** browser: the extension cannot be removed or disabled, and the
guard cannot be turned off. Not covered, and needing OS/device management to close: a
different browser (Firefox/Safari), an unmanaged personal device, a non-enrolled profile, and
Incognito (disabled here via policy). The guard also **fails closed** — if the Maskroom
server is unreachable, sends are blocked, so plan server availability. Full detail and
sources: [`../docs/ENTERPRISE_ENFORCEMENT.md`](../docs/ENTERPRISE_ENFORCEMENT.md) §C.
