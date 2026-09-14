# Enterprise enforcement — locking the Maskroom guard across a browser fleet

*Researched 2026-09-14 against primary sources (Google Chrome Enterprise policy
documentation at `support.google.com/chrome/a` and `chromeenterprise.google`, the
Chrome Extensions developer docs at `developer.chrome.com`, and the Chromium
administrators guide) and this repository's extension code under
[`extension/`](../extension). Chrome policy surfaces change; re-verify the cited
pages before deploying. Companion to
[`LLM_MIDDLEWARE_RESEARCH.md`](LLM_MIDDLEWARE_RESEARCH.md) and
[`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md).*

**The question.** Our Manifest V3 extension *"Maskroom for Claude"*
([`extension/manifest.json`](../extension/manifest.json)) pseudonymizes PII in the
claude.ai composer before send, through a **guard** feature that is on by default and
toggleable by the user. An administrator wants to (1) force-install it so users cannot
remove or disable it, (2) lock the guard so users cannot turn it off, and (3) ideally
prevent bypass by another browser/profile/incognito/device. What is actually possible
with managed-browser policy, and what must the extension implement?

**The short answer.**

1. **Force-install and prevent disable/removal — YES, fully, with policy alone.**
   `ExtensionSettings` with `installation_mode: "force_installed"` (or the legacy
   `ExtensionInstallForcelist`) installs the extension silently and the user "can't
   remove" it and cannot disable it. This requires a *managed* Chrome (Chrome Browser
   Cloud Management enrolment, or OS-level GPO/plist/JSON policy). See §A.

2. **Lock the guard so the user cannot turn it off — YES, but ONLY if the extension is
   built to honour it.** There is no admin policy that reaches an extension's arbitrary
   internal toggle. The supported mechanism is **managed storage**
   (`chrome.storage.managed`): the extension declares a schema, the admin pushes a
   `guardLocked: true` value via the per-extension "3rd party" policy, and *the
   extension code* must read that value, force the guard on, and make its own
   toggle/options inert. Managed storage is read-only to the extension and to the user,
   so a user cannot change the value — but the enforcement lives in our code, not in
   Chrome. **This is now implemented** (extension `guardLocked` managed key); §B and §D
   describe the mechanism and the admin policy to set.

3. **What the extension must implement:** a `storage.managed_schema`, a read of
   `chrome.storage.managed` on startup **and** on `chrome.storage.onChanged` for the
   `"managed"` area, guard forced on when `guardLocked` is set, and the bar/options
   toggle rendered disabled. See §D.

4. **Residual bypasses (state honestly):** policy binds the *managed browser*, not
   claude.ai. A user on an unmanaged personal device, a different browser (Firefox,
   Safari), a non-enrolled profile, or **incognito** (force-installed extensions do
   **not** run in incognito by default) can still reach claude.ai with no guard. Closing
   these needs fleet-wide device/OS management (block other browsers, disable incognito,
   force a managed profile). And the guard **fails closed** — if the Maskroom server is
   unreachable, sends are blocked, which is a safety property but also an availability
   dependency. See §C.

---

## A. Force-install and prevent removal / disable

### A.1 `ExtensionSettings` — the modern, recommended policy

Google now steers admins to the single `ExtensionSettings` policy, which "overrides"
the older individual extension policies. Per-extension you set an object keyed by the
32-character extension ID; the `"*"` key sets the default for all other extensions.

`installation_mode` values, verbatim from Google's admin help:

| Value | Behaviour |
|---|---|
| `allowed` | Users can install (default if undefined). |
| `blocked` | Users cannot install the extension. |
| `force_installed` | "Automatically installs without user interaction; **users cannot remove it**." Requires `update_url`. |
| `normal_installed` | Automatically installs, but **users can disable it**. Requires `update_url`. |
| `removed` | Users cannot install; a previously installed copy is uninstalled (Chrome 75+). |

`"*"` **cannot** be set to `force_installed` or `normal_installed` (Chrome would not
know which extension to fetch); use it for a `blocked` default instead
([Configure ExtensionSettings policy](https://support.google.com/chrome/a/answer/9867568)).

So the lock is `force_installed` (not `normal_installed`, which the user could disable).
The admin-console force-install workflow confirms the guarantee in plain words: "Users
can't remove items that are force-installed. The items also bypass any blocked apps and
extensions" ([Automatically install apps and extensions](https://support.google.com/chrome/a/answer/6306504)).

Hardening fields available on the same per-extension object
([Configure ExtensionSettings policy](https://support.google.com/chrome/a/answer/9867568)):

- **`toolbar_pin`** — `force_pinned` keeps the icon "always visible, user cannot hide";
  also `default_pinned` / `default_unpinned`.
- **`runtime_blocked_hosts`** / **`runtime_allowed_hosts`** — up to 100 host patterns
  (e.g. `*://*.example.com`) that the extension may/may not touch. Not needed to lock
  the guard, but relevant if you want to constrain where the extension runs (leave
  `https://claude.ai` allowed).
- **`blocked_permissions`** / **`allowed_permissions`** — an extension requesting a
  blocked permission will not install; a blocked *optional* permission is auto-declined.
  Use with care: our extension needs `storage` and `downloads`
  ([`manifest.json`](../extension/manifest.json)); do not block those.
- **`update_url`** — for Chrome Web Store extensions this is
  `https://clients2.google.com/service/update2/crx`; `override_update_url` forces Chrome
  to keep using it for updates.

### A.2 `ExtensionInstallForcelist` — the legacy equivalent

The older `ExtensionInstallForcelist` policy takes a list of string entries, each
`"<extension_id>;<update_url>"` (extension ID, a semicolon, then the update manifest
URL — for the Web Store `https://clients2.google.com/service/update2/crx`)
(VERIFIED 2026-09-14: each entry is `<extension_id>;<update_url>` — the 32-char
extension ID, a semicolon, then the update-manifest URL; for the Web Store the URL is
`https://clients2.google.com/service/update2/crx`, e.g.
`pckdojakecnhhplcgfflhndiffaohfah;https://clients2.google.com/service/update2/crx`. The
update URL in this policy is used only for the initial install; later updates follow the
URL in the extension's own manifest. Confirmed against
[Set Chrome app and extension policies (Windows)](https://support.google.com/chrome/a/answer/7532015)
and the Chrome ADMX reference on
[admx.help](https://admx.help/?Category=Chrome&Policy=Google.Policies.Chrome::ExtensionInstallForcelist).)
It yields the same non-removable install as `force_installed` — Google's Windows policy
page states for `force_installed` that "Users can't disable or remove them." `ExtensionSettings` supersedes and can override it, so prefer
`ExtensionSettings`; the forcelist entry is given in §D only for completeness.

### A.3 It requires a *managed* browser

None of this applies to an unmanaged Chrome. Force-install works for "signed-in users
on any device or enrolled browsers on Windows, Mac, or Linux," and the organization
must have Chrome browser management enabled
([Automatically install apps and extensions](https://support.google.com/chrome/a/answer/6306504)).
The two delivery routes are:

- **Chrome Browser Cloud Management (CBCM)** via the Google Admin console — the console
  generates the `ExtensionSettings` JSON for you.
- **OS-level policy** — Windows GPO/registry, macOS configuration profile (plist), or
  Linux JSON under `/etc/opt/chrome/policies/managed/`. This is also where the
  per-extension managed configuration in §B is delivered
  ([Configuring Apps and Extensions by Policy](https://www.chromium.org/administrators/configuring-policy-for-extensions/)).

### A.4 Incognito is **not** covered by force-install

Force-installed extensions do **not** run in incognito automatically: "As an admin, you
can't automatically install extensions in Incognito mode … However, users themselves
can allow individual extensions to run in Incognito mode"
([Extensions in Incognito mode](https://support.google.com/chrome/a/answer/13130396)).
There is no `ExtensionSettings` field that force-enables an extension in incognito.
Consequence: **incognito is a guard bypass** — the user opens claude.ai in an incognito
window where our content script never loads. To close it, disable incognito with
`IncognitoModeAvailability = 1` ("Disabled") — values are `0` available, `1` disabled,
`2` forced ([IncognitoModeAvailability](https://chromeenterprise.google/policies/incognito-mode-availability/),
[Allow private browsing](https://support.google.com/chrome/a/answer/9302896)). A related
policy, `MandatoryExtensionsForIncognitoNavigation`, can *require* named extensions be
allowed in incognito before incognito browsing is permitted, but it still depends on the
user enabling them, so disabling incognito outright is the clean control
([MandatoryExtensionsForIncognitoNavigation](https://chromeenterprise.google/policies/mandatory-extensions-for-incognito-navigation/)).

### A.5 Hosting: Chrome Web Store vs self-hosted

Both work for a force-installed extension:

- **Chrome Web Store** (public or unlisted) — `update_url` =
  `https://clients2.google.com/service/update2/crx`. Simplest.
- **Self-hosted `.crx` + update manifest** — host the `.crx` and an XML update manifest
  on your own server and point `update_url` at that XML. On Linux the external
  preferences file's `external_update_url` "has to point to the xml file," and an entry
  can reference "a Chrome Web Store extension, an externally hosted extension or a CRX
  extension file"
  ([Extension installation methods / distribute](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions)).
  Historically, self-hosted (non-Web-Store) sources also needed `ExtensionInstallSources`
  allowlisting and `ExtensionAllowedTypes` to permit the type; `ExtensionSettings` now
  overrides those individual policies
  ([Set Chrome app and extension policies (Windows)](https://support.google.com/chrome/a/answer/7532015)).
  Since Maskroom is an internal tool not necessarily published to the store, an unlisted
  Web Store listing or a self-hosted `.crx` are both viable.

---

## B. Locking the extension's OWN settings (the guard toggle)

### B.1 Why policy alone cannot do it

Force-install, `blocked_permissions`, `runtime_blocked_hosts` and `toolbar_pin` are the
*only* hard, admin-side controls Chrome offers over an extension. They govern
**install, permissions, hosts and pinning** — not arbitrary internal state. There is no
Chrome policy that reads or overrides our `guard` boolean. Today that boolean lives in
`chrome.storage.local` and is flipped by the bar button and the options page
([`content.js`](../extension/content.js) line ~548;
[`options.js`](../extension/options.js) line ~24), entirely under user control. **No
admin can lock it without the extension cooperating.**

### B.2 The supported mechanism — managed storage (`chrome.storage.managed`)

The one channel through which an admin can push configuration *into* an extension is
**managed storage**: "Managed storage is read-only for policy-installed extensions. It's
managed by system administrators, using a developer-defined schema and enterprise
policies … configured by a system administrator, instead of the user"
([chrome.storage](https://developer.chrome.com/docs/extensions/reference/api/storage)).
Properties that make it the right tool:

- It is **read-only** to the extension and **not settable by the user** — only an admin
  sets it via policy. A user cannot change a managed value.
- It is a distinct storage **area** alongside `local`, `sync`, and `session`
  ([chrome.storage](https://developer.chrome.com/docs/extensions/reference/api/storage)).
- Changes fire `chrome.storage.onChanged` with the area name (`"managed"`), so the
  extension can react live if policy changes
  ([chrome.storage](https://developer.chrome.com/docs/extensions/reference/api/storage)).

The extension advertises what admins may set by declaring a JSON Schema in the manifest:

```json
{
  "name": "My enterprise extension",
  "storage": { "managed_schema": "schema.json" }
}
```

([storage manifest key](https://developer.chrome.com/docs/extensions/reference/manifest/storage)).
Once declared, the extension and its configured policies appear at `chrome://policy`,
which is also how an admin verifies the value landed
([Configuring Apps and Extensions by Policy](https://www.chromium.org/administrators/configuring-policy-for-extensions/)).

### B.3 The critical nuance

**Managed storage does not change behaviour on its own.** Setting `guardLocked: true`
does nothing unless the extension is written to read it and act. So the extension must:

1. On startup, read `chrome.storage.managed` (with a safe fallback if empty/undefined —
   most installs have no managed policy).
2. If `guardLocked` is set, force `settings.guard = true` and treat the guard as
   immutable regardless of `chrome.storage.local`.
3. Ignore or override any user attempt to toggle: the bar `guard` button
   ([`content.js`](../extension/content.js) ~548) and the options checkbox
   ([`options.js`](../extension/options.js) ~24 / [`options.html`](../extension/options.html) ~22)
   must become inert (disabled, shown as "locked by your organization").
4. Add a `chrome.storage.onChanged` listener for the `"managed"` area — the existing
   listener only handles `"local"`
   ([`content.js`](../extension/content.js) ~598) — so a later policy change takes
   effect without reinstall.

Because managed storage is admin-only and read-only, once (2)–(4) are in place the user
has no supported path to turn the guard off — not via the bar, not via options, not by
editing `local` storage (the code prefers the managed value).

### B.4 Can settings be locked WITHOUT extension support?

**No — not for internal state.** The admin-side hard controls (install / permissions /
hosts / pin) cannot express "guard must stay on." Managed storage is the bridge, and it
is inert until our code honours it. This is a code change we own (§D). (One blunt,
unsupported-as-a-real-lock alternative: `runtime_blocked_hosts` could stop the extension
running on some site, but it cannot force a feature *on*, and blocking claude.ai would
disable the whole product. Not applicable.)

---

## C. Residual bypasses — stated honestly

Policy binds the **managed browser instance**, not the claude.ai website. Every gap
below is real:

- **Different browser / unmanaged device.** claude.ai is reachable from Firefox, Safari,
  a personal laptop, or a phone, where our extension and these policies do not exist.
  Enforcement is only as strong as the fleet: to close this the org must manage the OS
  (block installation/use of other browsers, restrict to managed devices). Outside
  managed hardware there is no client-side control we can impose.
- **Incognito.** As in §A.4, force-installed extensions do not run in incognito by
  default; disable incognito (`IncognitoModeAvailability = 1`) or the guard is trivially
  skipped.
- **Managed profile vs managed device.** Policy can apply at the *device/browser* level
  (machine enrolment, OS policy) or the *user profile* level (a managed Google profile).
  A device-level `ExtensionSettings` covers every profile on that browser, including
  guests, more completely than a user-level policy that a user could escape by using a
  different, unmanaged profile. Consider forcing sign-in / a managed
  profile so the guard cannot be dodged by profile-switching (VERIFIED 2026-09-14):
  `BrowserSignin` set to force sign-in makes the user pick and sign in to a managed
  profile before using the browser, and per Google "users can no longer open Guest mode …
  the default value of `BrowserGuestModeEnabled` will be set to disabled"
  ([Force users to sign in](https://support.google.com/chrome/a/answer/7572556),
  [BrowserSignin](https://chromeenterprise.google/policies/browser-signin/)).
  `RestrictSigninToPattern` is a regular expression limiting which Google accounts may be
  the browser's primary/managed account
  ([RestrictSigninToPattern](https://chromeenterprise.google/policies/restrict-signin-to-pattern/)),
  and `BrowserGuestModeEnabled=false` (or `BrowserGuestModeEnforced`) removes Guest mode
  outright ([BrowserGuestModeEnforced](https://chromeenterprise.google/policies/browser-guest-mode-enforced/)).
  These bind the managed browser only; a genuinely separate unmanaged browser or device
  is still outside their reach (see below).
- **Availability dependency (fail-closed).** The guard needs the Maskroom server
  reachable. When masking fails, `maskComposer()` throws and the send is blocked — the
  guard already `preventDefault()`/`stopImmediatePropagation()`s the Enter/send before
  claude.ai handles it, and does not resend on error
  ([`content.js`](../extension/content.js) ~202, ~228–246). This is the correct safety
  posture (no PII leaks on failure) but means a server outage blocks users from sending.
  Plan server availability accordingly.
- **Anthropic-side.** None of this is an Anthropic-supported control; it is browser-fleet
  enforcement of a *client-side* mechanism against a page whose structure can change. The
  only genuinely server-enforced control on the Claude side is Claude Enterprise
  **inference hooks** (detector/gate, not a masker) — see
  [`LLM_MIDDLEWARE_RESEARCH.md`](LLM_MIDDLEWARE_RESEARCH.md). Treat this document's
  enforcement as defence-in-depth for a fleet you control, not as an unbypassable
  guarantee.

---

## D. Concrete admin recipe

Throughout, replace `<EXTENSION_ID>` with the real 32-character ID and `<UPDATE_URL>`
with the hosting update manifest (Chrome Web Store:
`https://clients2.google.com/service/update2/crx`).

### D.1 Force-install + pin + harden (`ExtensionSettings`)

```json
{
  "<EXTENSION_ID>": {
    "installation_mode": "force_installed",
    "update_url": "https://clients2.google.com/service/update2/crx",
    "toolbar_pin": "force_pinned"
  },
  "*": {
    "installation_mode": "blocked"
  }
}
```

- `force_installed` → non-removable, cannot be disabled by the user (§A.1).
- `toolbar_pin: force_pinned` → the Maskroom icon stays visible.
- The `"*": blocked` default is optional org hardening (blocks all *other* extensions);
  drop it if the org allows other extensions.
- Optional extra hardening on the `<EXTENSION_ID>` object (only if you understand the
  effect): `"runtime_blocked_hosts"` / `"runtime_allowed_hosts"` (leave claude.ai
  allowed), and `"blocked_permissions"` — **do not** block `storage` or `downloads`,
  which Maskroom requires ([`manifest.json`](../extension/manifest.json)).

Delivered via the Admin console (CBCM) → *Chrome browser* → *Apps & extensions*, or
OS-level as the `ExtensionSettings` policy value.

Legacy equivalent (`ExtensionInstallForcelist`), one string per extension:

```
<EXTENSION_ID>;https://clients2.google.com/service/update2/crx
```

Prefer `ExtensionSettings`; it overrides the forcelist.

Also, to prevent the incognito bypass (§A.4), set separately:

```
IncognitoModeAvailability = 1     (Disabled)
```

### D.2 The managed configuration that locks the guard

This is the **per-extension "3rd party" policy** (distinct from `ExtensionSettings`),
delivered to the extension's own namespace. The value the admin pushes:

```json
{ "guardLocked": true }
```

Where it goes on each platform
([Configuring Apps and Extensions by Policy](https://www.chromium.org/administrators/configuring-policy-for-extensions/)):

**Windows (registry / GPO)** — under the 3rdparty extensions path, keyed by ID:

```
HKEY_LOCAL_MACHINE\Software\Policies\Google\Chrome\3rdparty\extensions\<EXTENSION_ID>\policy
    guardLocked  (REG_DWORD) = 1
```

(`HKCU` also works; Chromium uses `...\Chromium\3rdparty\...`.)

**macOS (managed plist / configuration profile)** — a preference under the bundle
`com.google.Chrome.extensions.<EXTENSION_ID>`, each key wrapped with `state=always` and
a `value` (MCX form):

```xml
<key>guardLocked</key>
<dict>
  <key>state</key><string>always</string>
  <key>value</key><true/>
</dict>
```

(managed preferences domain `com.google.Chrome.extensions.<EXTENSION_ID>`; deliver via
an MDM `.mobileconfig` profile.)

**Linux** — a JSON file in `/etc/opt/chrome/policies/managed/` (e.g.
`maskroom_guard.json`) using the `3rdparty` → `extensions` → `<id>` → `policy` nesting:

```json
{
  "3rdparty": {
    "extensions": {
      "<EXTENSION_ID>": {
        "guardLocked": true
      }
    }
  }
}
```

**CBCM Admin console** — *Chrome browser* → *Apps & extensions* → select the Maskroom
extension → **Policy for extensions**, and paste the extension configuration JSON:

```json
{ "guardLocked": { "Value": true } }
```

(the console/JSON form wraps each policy value under a `"Value"` key). Verify on a
managed machine at `chrome://policy` (Reload policies), where the extension and
`guardLocked` should be listed.

### D.3 What the extension must add

1. **`manifest.json`** — declare the schema:

   ```json
   "storage": { "managed_schema": "managed_schema.json" }
   ```

   (`"storage"` is a top-level manifest key; the `storage` *permission* is already
   present in [`manifest.json`](../extension/manifest.json).)

2. **`managed_schema.json`** — a JSON Schema for the admin-settable keys:

   ```json
   {
     "type": "object",
     "properties": {
       "guardLocked": {
         "title": "Force the guard on and prevent users disabling it",
         "type": "boolean"
       }
     }
   }
   ```

3. **Read and honour it (behaviour changes).** In the content script
   ([`content.js`](../extension/content.js)) and options
   ([`options.js`](../extension/options.js)):

   - On startup, `const { guardLocked } = await chrome.storage.managed.get({ guardLocked: false }).catch(() => ({}))`
     (managed storage is absent on unmanaged installs — fall back to `false`).
   - If `guardLocked`, set `settings.guard = true` and keep it true, ignoring the
     `guard` value in `chrome.storage.local`.
   - Make the bar `guard` button inert when locked — skip the toggle branch at
     [`content.js`](../extension/content.js) ~548 and render it disabled ("guard:
     locked") in `renderBar()` (~572–574).
   - Disable the options checkbox ([`options.html`](../extension/options.html) ~22 /
     [`options.js`](../extension/options.js) ~24) and label it as org-locked.
   - Extend the `chrome.storage.onChanged` listener
     ([`content.js`](../extension/content.js) ~598) to also handle the `"managed"` area
     so a later policy flip re-locks/unlocks without reinstall.

   **Status: implemented.** The extension now ships
   [`managed_schema.json`](../extension/managed_schema.json) with a `guardLocked` boolean,
   [`manifest.json`](../extension/manifest.json) declares
   `"storage": { "managed_schema": "managed_schema.json" }`,
   [`content.js`](../extension/content.js) reads `chrome.storage.managed` on boot
   (`loadManaged`), forces the guard on and makes the bar toggle inert with a "guard: on 🔒"
   label when locked, re-applies the lock on any local write, and handles the `"managed"`
   `onChanged` area so a policy flip re-locks/unlocks live;
   [`options.js`](../extension/options.js) checks and disables the Guard checkbox and marks
   it "locked by your administrator". Admins only need the policy in D.2/D.3 — no code
   changes remain. Automated tests cover the unlocked path; the locked path needs a real
   managed policy (a fixed extension id via a manifest `key`, plus the platform policy
   file) to exercise end to end.

With D.1 (force-install, non-removable, pinned, incognito disabled) plus D.2+D.3
(managed `guardLocked` honoured by the extension), a user on a managed browser cannot
remove the extension, cannot disable it, and cannot turn the guard off. The residual
gaps in §C (other browsers, unmanaged devices/profiles) remain and require fleet/OS
management to close.

---

## Sources

Primary — Google Chrome Enterprise / admin help:
- Configure ExtensionSettings policy — https://support.google.com/chrome/a/answer/9867568
- Automatically install (force-install) apps and extensions — https://support.google.com/chrome/a/answer/6306504
- Set Chrome app and extension policies (Windows) — https://support.google.com/chrome/a/answer/7532015
- Set Chrome app and extension policies (Linux) — https://support.google.com/chrome/a/answer/7517525
- Set Chrome app and extension policies (Mac) — https://support.google.com/chrome/a/answer/7517624
- Extensions in Incognito mode — https://support.google.com/chrome/a/answer/13130396
- Allow private browsing (IncognitoModeAvailability) — https://support.google.com/chrome/a/answer/9302896

Primary — chromeenterprise.google policy list (JavaScript SPA that did not render for
automated fetch; the two items relied on — the forcelist string format and the
profile-lockdown policies — were re-verified 2026-09-14 against support.google.com and the
Chrome ADMX mirror on admx.help, listed under Verification below):
- ExtensionInstallForcelist — https://chromeenterprise.google/policies/extension-install-forcelist/
- BrowserSignin — https://chromeenterprise.google/policies/browser-signin/
- RestrictSigninToPattern — https://chromeenterprise.google/policies/restrict-signin-to-pattern/
- BrowserGuestModeEnforced — https://chromeenterprise.google/policies/browser-guest-mode-enforced/
- IncognitoModeAvailability — https://chromeenterprise.google/policies/incognito-mode-availability/
- MandatoryExtensionsForIncognitoNavigation — https://chromeenterprise.google/policies/mandatory-extensions-for-incognito-navigation/

Verification (2026-09-14, live references):
- Force-installed extensions are non-removable — "Users can't disable or remove them" — https://support.google.com/chrome/a/answer/7532015
- ExtensionInstallForcelist `id;update_url` format + Web Store update URL — https://support.google.com/chrome/a/answer/7532015 and https://admx.help/?Category=Chrome&Policy=Google.Policies.Chrome::ExtensionInstallForcelist
- Force sign-in disables Guest mode / requires managed profile — https://support.google.com/chrome/a/answer/7572556

Primary — Chrome Extensions developer docs / Chromium:
- chrome.storage API (managed storage, areas, onChanged) — https://developer.chrome.com/docs/extensions/reference/api/storage
- storage manifest key (managed_schema) — https://developer.chrome.com/docs/extensions/reference/manifest/storage
- Extension installation / distribution methods — https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions
- Configuring Apps and Extensions by Policy (per-platform 3rdparty paths) — https://www.chromium.org/administrators/configuring-policy-for-extensions/

Repository:
- [`extension/manifest.json`](../extension/manifest.json), [`extension/content.js`](../extension/content.js), [`extension/options.js`](../extension/options.js), [`extension/options.html`](../extension/options.html)
- Cross-reference: [`docs/LLM_MIDDLEWARE_RESEARCH.md`](LLM_MIDDLEWARE_RESEARCH.md) (Claude Enterprise inference hooks — the server-enforced control)
