# Research: shipping the SafePII extension on Edge, Firefox and Safari

*Researched 2026-10-02 against primary sources: MDN WebExtensions documentation and
browser-compat-data (BCD, `main` at commit `f2dd714f49`, 2026-10-02), Mozilla Extension
Workshop and the Firefox administrator reference (`firefox-admin-docs.mozilla.org`, which now
replaces the `mozilla/policy-templates` README), Firefox source and Bugzilla, Microsoft Learn
(Edge extension and Edge policy docs), Chrome developer docs and Google's policy templates,
Apple developer documentation, Apple's `device-management` repository, the Apple Platform
Deployment guide and Safari release notes. Browser extension platforms change every release;
re-check the cited pages before acting.* Companion to
[`ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md) (whose §C lists "a different
browser" as an open bypass) and [`DESKTOP_APP_RESEARCH.md`](DESKTOP_APP_RESEARCH.md).

**The question.** The SafePII extension in [`extension/`](../extension) is a Chrome Manifest V3
extension. It is force-installed from a self-hosted `.crx`, configured and locked through
`chrome.storage.managed`, signs in through `chrome.identity.launchWebAuthFlow`, and restores
claude.ai downloads through `chrome.downloads`. What would it take to ship the same thing on
Microsoft Edge, Firefox and Safari (macOS, with a note on iOS/iPadOS)? For each browser this
document covers API support, the background model, enterprise force-install and managed
configuration, distribution and signing, extension-ID stability (which matters to the server's
login-redirect allowlist), and content-script differences.

**The short answer.**

1. **Edge: the extension works almost unchanged.** Microsoft states that Chrome's APIs and
   manifest keys are "code-compatible", and it lists `identity` (minus `getAuthToken`),
   `downloads`, `storage`, `permissions` and `tabs` as supported. The same self-hosted CRX,
   `update.xml` and `key` give the same extension ID under Edge's `ExtensionSettings` policy.
   The work is enterprise paperwork: Edge policy files, and the managed-storage registry path,
   which Microsoft documents only in a Q&A answer. One thing changes if the extension is
   published on Edge Add-ons: the store assigns its own ID, so the `chromiumapp.org` redirect
   host changes too.
2. **Firefox: one evening of porting, plus Mozilla signing.**
   - Every API the extension uses exists on desktop Firefox.
   - `background.service_worker` does not. Firefox runs `background.scripts` as an event page, and one manifest can list both.
   - Three things break and need small changes:
     - `downloads.download` refuses `data:` URLs, which `saveFile()` uses.
     - `storage.managed` has no Windows-registry backend and needs a browser restart to pick up changes.
     - The `identity` redirect host is `<sha1(id)>.extensions.allizom.org`, which the server's `safe_next()` does not accept.
   - Every XPI must be signed by Mozilla, even for self-hosting. "Unlisted" signing on AMO does that, and policy can then force-install from our own URL.
3. **Safari: a different product shape.**
   - Safari has **no `identity`, no `downloads` and no `storage.managed`**. It ignores `update_url` and has no `managed_schema`.
   - The extension must ship inside a signed macOS app, built with Xcode on a Mac. The cost is $99/yr for the Apple Developer Program.
   - Distribution is through the App Store, or Developer ID plus notarization. Safari supports the Developer ID route only from Safari 18.4.
   - An MDM can lock the extension on, including in Private Browsing (a DDM declaration, macOS 15+, supervised devices). It cannot push `serverUrl`/`guardLocked` through `storage.managed`. That configuration has to reach the extension through the containing app and native messaging.
   - The download intercept cannot be built. Only the click-time blob path in `content.js` can be kept.
   - Sign-in has to be redone: either a tab flow or the containing app doing the ADR 0003 loopback flow.
4. **iOS/iPadOS Safari** has the same gaps and adds more: no app-to-extension push, no `webRequest`, and no Developer ID. It is out of scope unless there is a specific demand.

Section 7 has the feature × browser table, §8 the things that need a real machine, §9 the
recommended order.

---

## 1. What the extension actually uses

Read from [`extension/manifest.json`](../extension/manifest.json) (v0.2.2),
[`background.js`](../extension/background.js), [`content.js`](../extension/content.js),
[`options.js`](../extension/options.js), [`managed_schema.json`](../extension/managed_schema.json),
and from the server's [`webui/auth.py`](../webui/auth.py) and the deployment kit in
[`enterprise/`](../enterprise).

| Area | What it does | Where |
|---|---|---|
| Sign-in | `identity.getRedirectURL("done")`, then `launchWebAuthFlow({url: <server>/auth/login?next=<redirect>, interactive: true})`. Sign-out calls it again with `interactive: false` to end the provider's session. Afterwards the server's session **cookie** authenticates every background `fetch(..., {credentials: "include"})`. | `background.js` `login()`, `logout()`, `request()` |
| Server allowlist | `safe_next()` accepts three forms: a same-origin path, `https://<id>.chromiumapp.org/...` for ids in `MASKROOM_EXTENSION_IDS`, and a loopback `http://127.0.0.1:<port>/...` or `[::1]`. **The loopback form requires an explicit port.** | `webui/auth.py` ~L307–328 |
| Download intercept | `downloads.onCreated` (filters on `byExtensionId`, `url` and `referrer`), then `search`, `cancel`, `erase` and `removeFile`. The restored file is saved with `downloads.download({url: "data:...;base64,..."})`. | `background.js` `intercept()`, `saveFile()` |
| Click-time intercept | A capture-phase click on an `<a href="blob:...">` keeps the page's blob and blocks the browser download. The content script reads the blob with `fetch(blobUrl)`, and the worker restores and saves it. | `content.js` ~L394–440, `fetchBytes` |
| Managed config | `storage.managed.get({serverUrl, guardLocked})`, with a cache invalidated by `storage.onChanged` (area `managed`). | `background.js`, `content.js`, `options.js` |
| Optional permission | `permissions.contains`/`request({origins: [serverOrigin]})` for an `http://` server, which is in `optional_host_permissions`. | `options.js` |
| Tabs/messaging | `tabs.query({url: "https://claude.ai/*"})`, `tabs.sendMessage(id, msg, {frameId: 0})`, `tabs.onRemoved`, and `runtime.sendMessage`/`onMessage` between the content script, worker and options page. `runtime.openOptionsPage`. | `background.js` |
| Background | `background.service_worker` (MV3) | `manifest.json` |
| Content scripts | Match `claude.ai/*` and `*.claudeusercontent.com/*`, with `all_frames: true`, `match_origin_as_fallback: true` and `document_idle`. Enter and the send click are intercepted with `window.addEventListener(..., true)`. A synthetic `KeyboardEvent("keydown", Enter)` or a `button.click()` replays the send. | `content.js` L292–295, `resend()` |
| ID and updates | `"key"` fixes the ID `lcmdehcdpfddkjgajmlpgfholdekpgio`. `"update_url"` points to the server's generated gupdate `update.xml`. | `manifest.json`, `enterprise/RUNBOOK.md` |
| Enterprise | Chrome `ExtensionSettings` (force_installed, toolbar_pin), `IncognitoModeAvailability = 1`, and `3rdparty` managed values. Delivered as `.reg`/ADMX under `Software\Policies\Google\Chrome`, Linux JSON, or macOS plists for the `com.google.Chrome` domains. | `enterprise/policies/` |

The guard is a content-script mechanism ([ADR 0006](adr/0006-the-guard-intercepts-enter-and-fails-closed.md)
records the same fail-closed rule for the desktop helper). It therefore depends on the content
script being injected into claude.ai without the user having to do anything. That turns out to
be the main Safari difference (§4.1).

## 2. Microsoft Edge

Edge is Chromium. BCD records Edge as `"mirror"` of Chrome for every API and key below
([BCD `webextensions/api/identity.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/identity.json),
[`downloads.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/downloads.json),
[`storage.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/storage.json),
[`manifest/content_scripts.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/manifest/content_scripts.json),
[`manifest/background.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/manifest/background.json)).

### 2.1 API support

- **Microsoft's API support table** (updated 2026-08-12, [Supported APIs for Microsoft Edge extensions](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/api-support)):
  - It lists `identity`, `downloads`, `storage`, `permissions` and `tabs` as supported for MV2 and MV3 on Windows, Linux, Mac and Android.
  - The note on `identity` reads: "Not supported: identity.getAccounts, identity.getAuthToken - As an alternate, you can use identity.launchWebAuthFlow". So `launchWebAuthFlow` is supported. We do not use `getAuthToken`.
  - The unsupported list is `gcm`, `identity.getAccounts`, `identity.getAuthToken`, `instanceID`, `readingList`, `devtools.recorder` and ChromeOS-only APIs. None of these is ours.
- **Manifest keys.** Microsoft's port guide says "The Extension APIs and manifest keys supported by Chrome are code-compatible with Microsoft Edge" ([Port a Chrome extension to Microsoft Edge](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/port-chrome-extension)). This covers `service_worker`, `match_origin_as_fallback` (Chrome 99) and `storage.managed_schema` through BCD's mirror. The same guide tells you to "Remove the update_url field" before submitting to Edge Add-ons. That step concerns store submission, not policy-installed self-hosted builds.
- **Redirect host, not confirmed.** No Microsoft page names the host `getRedirectURL()` returns in Edge. BCD's mirror implies Chrome's documented `https://<app-id>.chromiumapp.org/*` ([chrome.identity](https://developer.chrome.com/docs/extensions/reference/api/identity), updated 2026-09-11). This is an inference. **Verify on a real Edge** (§8): log `chrome.identity.getRedirectURL()` and complete one sign-in.

### 2.2 Background

Edge uses the same `background.service_worker` as Chrome, so no change is needed.

### 2.3 Enterprise

**Force-install.** Edge has the same two policies as Chrome:
- [`ExtensionInstallForcelist`](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist) (2026-07-09).
- [`ExtensionSettings`](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensionsettings) (2026-10-01), with `installation_mode: force_installed` and `update_url`.

**Edge Add-ons update URL.** An extension hosted on Edge Add-ons uses `https://edge.microsoft.com/extensionwebstorebase/v1/crx` ([ExtensionSettings detailed guide](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-ref-guide)). A Chrome Web Store extension can also be force-installed into Edge with Google's update URL (same guide).

**Self-hosted CRX: yes, on managed devices only.**
- macOS: "apps and extensions from outside the Microsoft Edge Add-ons website can only be force installed if the instance is managed via MDM, or joined to a domain via MCX" ([ExtensionInstallForcelist](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist)).
- Windows: **Microsoft's pages disagree.**
  - The current `ExtensionSettings` page (2026-10-01) allows off-store force-install on instances "joined to a Microsoft Active Directory domain or joined to Microsoft Azure Active Directory".
  - The self-hosting guide (2023-07-20) says "Self-hosted extensions won't work for Microsoft Entra joined devices unless they're Microsoft Entra hybrid joined" ([Publish and update extensions in the Edge Add-ons store / self-host](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-webstore)).
  - **Verify on an Entra-only joined machine.**
- Chrome's rule for comparison: Chrome's equivalent text also accepts devices "enrolled in Chrome Enterprise Core" ([Chrome policy templates JSON](https://chromeenterprise.google/static/json/policy_templates_en-US.json)). Edge has no such cloud-enrolment exemption in its policy text.

**Reusing the Chrome artefacts.**
- Edge's self-hosting guide uses the same `gupdate` XML format and says the PEM fixes the ID: "If you don't use the same PEM file, the app ID of the extension changes" ([self-host guide](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-webstore)).
- So the existing `maskroom-<ver>.crx`, `/ext/update.xml` and ID `lcmdehcdpfddkjgajmlpgfholdekpgio` should work unchanged. That is an inference from the docs; no page says it in one sentence.
- The update URL in the policy is used only for the first install: "subsequent updates of the extension use the update URL in the extension's manifest" ([ExtensionInstallForcelist](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist)).

**Managed storage (`serverUrl`, `guardLocked`).**
- Microsoft has no official doc page for this. Microsoft Q&A answers (Microsoft staff, 2023) say Edge supports `storage.managed` from `HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Microsoft\Edge\3rdparty\extensions\<id>\policy` ([Q&A 1461058](https://learn.microsoft.com/en-us/answers/questions/1461058/), [Q&A 753622](https://learn.microsoft.com/en-us/answers/questions/753622/)). The second thread notes that the values "don't show up under edge://policies".
- **No Microsoft source gives the macOS preference domain.** Chrome's convention suggests `com.microsoft.Edge.extensions.<id>`, which is unverified.
- Edge's own policies use the `com.microsoft.Edge` domain on macOS ([Configure Edge on macOS with Jamf](https://learn.microsoft.com/en-us/deployedge/configure-microsoft-edge-on-mac-jamf)).
- The repository's [`maskroom.reg`](../enterprise/policies/windows/maskroom.reg) and plists target Google paths only. An Edge copy would target `Microsoft\Edge`.

**Can the user remove or disable it?**
- No. Edge says "A user can't disable or remove the extension" for `force_installed` ([ref guide](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-ref-guide)), and "Users can't uninstall or turn off this setting" ([ExtensionInstallForcelist](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist)).

**InPrivate.**
- Force-install "doesn't apply to InPrivate mode" ([ExtensionInstallForcelist](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist)).
- "By design, you can't enable extensions for InPrivate browsing through Group Policy" ([Microsoft troubleshoot article](https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/manageability/enable-extension-inprivate-policy), 2026-01-28).
- The only closure is [`InPrivateModeAvailability`](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/inprivatemodeavailability) `= 1` (disabled). This is the same as Chrome's `IncognitoModeAvailability`.

### 2.4 Distribution and signing

Two routes:
- **Self-hosted CRX, as today.** Policy-installed on managed devices (§2.3). It reuses the existing packaging with `local/maskroom-signing-key.pem`.
- **Edge Add-ons.**
  - Free: "There is no registration fee" ([Register as a Microsoft Edge extension developer](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/create-dev-account), 2025-12-12).
  - Registration is through Partner Center with a personal Microsoft account ("doesn't support registering with a work or school account"). Company verification takes "a few days to a few weeks".
  - Certification "can take up to seven business days".
  - A **Hidden** visibility option removes the listing from search; you share its URL ([Publish an extension](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension), 2026-05-05).

### 2.5 Extension ID stability

- **Self-hosted:** the ID comes from the PEM/`key`, so it is the same as on Chrome, and `MASKROOM_EXTENSION_IDS` needs no change.
- **Edge Add-ons:** no official page explains how the store assigns IDs. A Microsoft Q&A answer says the store "will reject uploads containing a 'key' field" ([Q&A 1411624](https://learn.microsoft.com/en-us/answers/questions/1411624/), 2023), and users in that thread report the resulting ID did not match Chrome's. **Assume a store-published build has a different ID** and add it to `MASKROOM_EXTENSION_IDS`.

### 2.6 Content scripts

Edge uses the same Blink engine and content-script implementation, so claude.ai's ProseMirror composer and the `claudeusercontent.com` frames should behave as in Chrome. No Edge-specific difference was found in Microsoft's docs.

### 2.7 Edge on other platforms

- **Edge for Android:** the API table lists Android for `identity`, `downloads`, `storage` and `tabs`. Force-install policies exist for Android: `ExtensionSettings` ≥135, `ExtensionInstallForcelist` ≥149.
- **Mobile beta:** the beta notes add `ExtensionInstallForcelist` for iOS and Android in 150.0.4078.18 (2026-06-22) ([Edge mobile beta release notes](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-relnote-mobile-beta-channel)).
- **Not confirmed:** general Stable availability of extensions on Edge for Android.

## 3. Firefox (desktop)

The Firefox administrator reference lists release notes up to Firefox 157. Firefox 153 shipped
on 2026-07-21 and is also the new ESR ([Firefox 153 admin release notes](https://firefox-admin-docs.mozilla.org/release-notes/version/firefox-153/)).
Version numbers below are the first release that has the feature.

### 3.1 API support

| Used by us | Firefox | Notes |
|---|---|---|
| `identity.getRedirectURL`, `launchWebAuthFlow` | 53 | Redirect host differs, see below |
| `downloads.download/search/cancel/erase/removeFile/onCreated` | 47–48 | `data:` URLs refused, see below |
| `DownloadItem.byExtensionId` | 69 | "Always returns `undefined`" before 69 (BCD) |
| `DownloadItem.referrer`, `.mime`, `.filename` | 47 | |
| `storage.managed` | 57 | Restricted, see §3.3 |
| `storage.local`, `onChanged` | 45 | |
| `permissions.request/contains` | 55 | |
| `optional_host_permissions` | 128 | |
| `tabs.query/sendMessage/onRemoved`, `runtime.*`, `openOptionsPage` | 45–48 | |
| `content_scripts.all_frames` / `match_origin_as_fallback` | 48 / **128** | |
| `background.service_worker` | **not supported** | §3.2 |
| `key`, top-level `update_url` | not used | ID and updates go in `browser_specific_settings.gecko` |

Sources: BCD [`identity.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/identity.json),
[`downloads.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/downloads.json),
[`storage.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/storage.json),
[`permissions.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/api/permissions.json),
[`optional_host_permissions.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/manifest/optional_host_permissions.json),
[`content_scripts.json`](https://github.com/mdn/browser-compat-data/blob/main/webextensions/manifest/content_scripts.json)
(commit `f2dd714f49`). MDN states that Firefox supports the `chrome.*` namespace with
callbacks as well as `browser.*` with promises ([JavaScript APIs](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/JavaScript_APIs)).
The code's `chrome.*` calls therefore need no renaming.

**Identity: what breaks.**
- **Redirect host.** `getRedirectURL()` returns `https://<hex SHA-1 of the add-on ID>.extensions.allizom.org/`, not `chromiumapp.org`.
  - MDN says only that it is "a fixed domain name and a subdomain derived from the add-on's ID" ([identity](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/identity)).
  - The domain and the SHA-1 come from Firefox source: `computeHash(extension.id)` with `CryptoHash("sha1")` in `toolkit/components/extensions/child/ext-identity.js`, and the pref `extensions.webextensions.identity.redirectDomain = "extensions.allizom.org"` in `modules/libpref/init/all.js` ([gecko-dev mirror](https://github.com/mozilla/gecko-dev/blob/master/toolkit/components/extensions/child/ext-identity.js)).
- **The redirect URI must be one of two forms.** "Starting with Firefox 75, you must use the redirect URL returned by identity.getRedirectURL()". "Starting from Firefox 86, a loopback address with the format `http://127.0.0.1/mozoauth2/[subdomain of URL returned by identity.getRedirectURL()]` is permitted" (MDN [identity](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/identity)).
  - **Both forms fail `safe_next()` today.** The first is not `chromiumapp.org`. The second has no port, and `is_loopback()` requires one.
  - The server needs to accept `https://<sha1(gecko.id)>.extensions.allizom.org/...`, mirroring the `chromiumapp.org` rule.
- **What the flow does.** In `parent/ext-identity.js`:
  - Firefox first tries the URL in a background XHR with `mozAnon: false`, which carries cookies.
  - If that does not reach the redirect and `interactive` is false, it rejects with "Requires user interaction".
  - Otherwise it opens a real browser window (`launchWebAuthFlow_dialog`).
  - `logout()`'s silent `interactive: false` call should therefore behave as on Chrome. **Verify** (§8).
- **Cookies.** No document states that a cookie set during the flow is later sent by the background script's `fetch`. The source points that way: a non-anonymous XHR and a normal browser window share the default cookie jar.
  - Mozilla engineer Rob Wu: requests "from moz-extension documents (NOT content scripts) are treated as first-party since bug 1629436 got fixed, PROVIDED that the extension has host permissions for the URL" ([bug 1608685](https://bugzilla.mozilla.org/show_bug.cgi?id=1608685)).
  - So the server origin must be a *granted* host permission (see §3.3 on revocable host permissions). **Verify** the full cookie round trip.

**Downloads: what breaks.**
- **`saveFile()`'s `data:` URL.** `downloads.download` rejects `data:` URLs with "Access denied for URL data:" ([bug 1318564](https://bugzilla.mozilla.org/show_bug.cgi?id=1318564), status NEW).
  - The Firefox background is a document (§3.2), so `URL.createObjectURL(new Blob(...))` is available there. A blob URL created *in the background page* is the documented workaround in [bug 1696174](https://bugzilla.mozilla.org/show_bug.cgi?id=1696174).
  - That bug also says blob URLs created by content scripts or pages are refused.
  - So `saveFile()` needs a Firefox branch: blob in the background page, not `data:`.
- **Unsupported options** (BCD): `conflictAction: "prompt"`, `DownloadItem.danger` ("Always given as 'safe'") and the `endedAfter`/`endedBefore` query filters. We use `conflictAction: "uniquify"`, which is supported.
- **Untested.** The cancel-then-search-for-`complete` race in `intercept()` relies on Chrome timing. Firefox's download manager may report states differently. **Verify** with a small and a large file.

### 3.2 Background: event page, not service worker

- **No service worker.** "`background.service_worker` is not supported (see Firefox bug 1573659)" ([MDN background](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/background)). That bug's meta is still NEW with no plan ([bug 1573659](https://bugzilla.mozilla.org/show_bug.cgi?id=1573659)).
- **Event pages instead.** Firefox runs MV3 `background.scripts` as a non-persistent event page ([MV3 migration guide](https://extensionworkshop.com/documentation/develop/manifest-v3-migration-guide/)).
- **One manifest can serve both browsers.** MDN: "To support browsers with different Manifest V3 background script implementations, specify both `scripts` and `service_worker`".
  - From Chrome 121 the presence of `scripts` "is ignored" ([MDN background](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/background)). Chrome's own notes: "This is being done to enable using a single manifest file in extensions in multiple browsers" ([What's new in Chrome extensions, 2024-01-11](https://developer.chrome.com/docs/extensions/whats-new)).
  - From Firefox 121 the background page starts "regardless of the presence of `service_worker`".
  - `preferred_environment` exists from Firefox 136 and Safari 18 (BCD).
- **Code impact.** `background.js` uses `self` and module-level state (`tabVaults`, `ownDownloads`), which work in a page too. Its in-memory state is lost when the event page idles out, the same caveat as the Chrome worker.
- **What still cannot be shared.** `browser_specific_settings.gecko` is harmless to Chrome. `key` and top-level `update_url` are Chrome-only. A **single manifest is possible but not ideal**. A build step that emits a per-browser `manifest.json` from one source is the common pattern; that is an observation, not a cited requirement.

### 3.3 Enterprise

**Force-install from our own URL.**
- `ExtensionSettings` with `installation_mode: "force_installed"` and `install_url` ([ExtensionSettings](https://firefox-admin-docs.mozilla.org/reference/policies/extensionsettings/)). The mode "Automatically installs the extension and prevents it from being removed by the user".
- `install_url` can be any URL, including `file:///`.
- From Firefox 151, a policy `update_url` "overrides the update_url specified in the extension manifest".
- From Firefox 152, "force-installed extensions are always updated automatically, regardless of the updates_disabled setting".
- Delivery: `policies.json`, Windows GPO/ADMX, or a macOS plist for `org.mozilla.firefox` (same reference).
- **The XPI must still be Mozilla-signed** (§3.4). A self-hosted *unsigned* XPI is not possible on release Firefox.

**Can the user remove or disable it?**
- Removal is prevented (quote above).
- **Not confirmed:** Mozilla's docs do not say that a `force_installed` extension cannot be *disabled* (contrast `normal_installed`, which "allows it to be disabled by the user"). **Verify** in `about:addons`.

**Host permissions.** In MV3 they are user-revocable.
- MDN: "Users can grant or revoke host permissions on an ad hoc basis. Therefore, most browsers treat `host_permissions` as optional" ([host_permissions](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/host_permissions)). Install-time granting started in Firefox 127.
- **Firefox 153** closes this for managed installs: "Users can no longer change the host permissions of Manifest V3 extensions installed using force_installed". It also adds `runtime_allowed_hosts`/`runtime_blocked_hosts`/`allowed_permissions` ([Firefox 153 notes](https://firefox-admin-docs.mozilla.org/release-notes/version/firefox-153/)).
- **On older Firefox**, or a non-managed install, a user could revoke `claude.ai`, which switches the guard off silently. The extension should check `permissions.contains` and say so.

**Private windows.**
- `ExtensionSettings.<id>.private_browsing: true` (Firefox 136 / ESR 128.8) "indicates whether this extension should be enabled in private browsing". This is better than Chrome and Edge, where policy cannot turn the extension on in private windows.
- `PrivateBrowsingModeAvailability` (Firefox 130 / ESR 128.3; 1 = not available) is the alternative ([policy reference](https://firefox-admin-docs.mozilla.org/reference/policies/extensionsettings/)).
- **Not confirmed:** whether the user can untick "Run in Private Windows" when policy sets it.

**Managed storage.**
- Firefox reads `storage.managed` from either:
  - a native-manifest JSON of `"type": "storage"`. Paths: Linux `/usr/lib/mozilla/managed-storage/<id>.json`; macOS `/Library/Application Support/Mozilla/ManagedStorage/<id>.json`; Windows `HKLM\SOFTWARE\Mozilla\ManagedStorage\<id>`, whose default value points to the JSON file ([Native manifests](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests)).
  - the **`3rdparty`** enterprise policy: `{"policies": {"3rdparty": {"Extensions": {"<id>": {"serverUrl": "...", "guardLocked": true}}}}}` ([3rdparty policy](https://mozilla.github.io/policy-templates/#3rdparty); [Enterprise development](https://extensionworkshop.com/documentation/enterprise/enterprise-development/): "You can set a value via the 3rdparty enterprise policy and read it with storage.managed").
- Restrictions, from BCD's notes on `storage.managed`:
  - "Platform-specific storage backends, such as Windows registry keys, are not supported". Chrome's `3rdparty\extensions\<id>\policy` registry layout does not apply.
  - "Enforcement of extension-provided storage schemas is not supported". `managed_schema.json` is ignored.
  - "The `onChanged` event is not supported". BCD *also* lists `managed.onChanged` from Firefox 101, so the data contradicts itself. Do not rely on it.
- MDN: "In Firefox, a browser restart is required to load changes to the JSON manifest or policy into managed storage" ([storage.managed](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/storage/managed)).
- For GPO/Intune, "the extension developer should provide an ADMX file" (3rdparty doc). The repo's [`SafePII.admx`](../enterprise/policies/windows/admx/SafePII.admx) covers the desktop helper, not Firefox.

### 3.4 Distribution and signing

- **Signing is mandatory.** "Extensions and themes need to be signed by Mozilla before they can be installed in release and beta versions of Firefox" ([Signing and distribution overview](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/)).
  - Unsigned builds load only in Developer Edition, Nightly and ESR with `xpinstall.signatures.required=false`.
  - The `Preferences` policy can set that pref on ESR only ("Firefox ESR only", [Preferences policy](https://firefox-admin-docs.mozilla.org/reference/policies/preferences/)).
  - The [enterprise distribution guide](https://extensionworkshop.com/documentation/enterprise/enterprise-distribution/) offers ESR + unsigned, or AMO self-distribution signing.
- **Unlisted (self-distribution) signing.**
  - Submit each version to AMO on the *unlisted* channel. `web-ext sign --channel unlisted --api-key ... --api-secret ...` uploads it, waits for approval and downloads the signed XPI ([web-ext command reference](https://extensionworkshop.com/documentation/develop/web-ext-command-reference/)).
  - Signing "can take up to 24 hours ... or longer if your submission is selected for manual review", and add-ons remain subject to manual review at any time ([signing overview](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/)).
  - The AMO policies apply "regardless of how they are distributed" ([Add-on policies](https://extensionworkshop.com/documentation/publish/add-on-policies/)).
  - Source code must be submitted if the XPI is minified or bundled ([Source code submission](https://extensionworkshop.com/documentation/publish/source-code-submission/)). Our sources are plain JS today.
- **Self-hosted updates.**
  - Put `browser_specific_settings.gecko.update_url` (must be `https`) in the manifest, pointing to an `updates.json` of the form `{"addons": {"<id>": {"updates": [{"version": "...", "update_link": "https://.../safepii-x.y.z.xpi"}]}}}` ([Updating your extension](https://extensionworkshop.com/documentation/manage/updating-your-extension/)).
  - The XPI must be served as `application/x-xpinstall` ([Self-distribution](https://extensionworkshop.com/documentation/publish/self-distribution/)).
  - Our server's `/ext/update.xml` route is gupdate-only and would need a sibling for this format.
- **Required manifest fields for MV3 signing.**
  - `browser_specific_settings.gecko.id` is mandatory: "AMO does not assign an ID for MV3 extensions" ([browser_specific_settings](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/browser_specific_settings)).
  - `gecko.data_collection_permissions` is "Required for new extensions submitted to addons.mozilla.org from November 3, 2025" (Firefox 140+; [Firefox built-in data consent](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/)).
  - **Not confirmed:** whether unlisted submissions are exempt. Assume not.
  - SafePII sends composer text to *our* server, so the honest declaration is likely `websiteContent` and possibly `personallyIdentifyingInfo`. That is our reading, not Mozilla's.

### 3.5 Extension ID stability

- **The ID is fixed by us.** We choose `gecko.id`, for example `safepii@hsenidmobile.com`. It is the same on every install, and so is the redirect host derived from it.
  - Without a fixed ID, "the redirect URL will change each time you temporarily install the extension" ([getRedirectURL](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/identity/getRedirectURL)).
- **The internal origin is not stable.** The `moz-extension://<UUID>` origin is random per profile ([Chrome incompatibilities](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Chrome_incompatibilities)). Do not allowlist it on the server.
- **Server change.** `safe_next()` needs a Firefox rule. The host is a pure function of `gecko.id`, so it can be precomputed and added to configuration.

### 3.6 Content scripts

- **Frames.**
  - `all_frames` works.
  - `match_origin_as_fallback` arrives in Firefox 128 and requires a wildcard path in `matches`. Ours (`/*`) qualify ([content_scripts](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/content_scripts)).
  - BCD: content scripts "won't be injected into empty iframes at 'document_start'". We use `document_idle`.
  - The `claudeusercontent.com` frames are ordinary cross-origin https frames and are matched directly.
- **Xray vision.**
  - The content script's `globalThis` is "a distinct object inheriting from `window`".
  - "separate event handlers are not maintained per world": `el.onclick = ...` overwrites the page's own handler ([Content scripts](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Content_scripts)).
  - `content.js` uses `addEventListener` throughout, so the capture-phase Enter guard should be unaffected. No documented Firefox difference was found for `window.addEventListener("keydown", ..., true)`.
  - **Verify** two things on claude.ai: that the guard wins against ProseMirror's handler, and that the synthetic `KeyboardEvent` in `resend()`, constructed in the content-script compartment and dispatched into the page, is honoured.
- **Fetch from content scripts.** "In Chrome and Firefox in Manifest V3, these requests happen in context of the page" (same MDN page). The click-time path's `fetch(blobUrl)` of a page-created blob should therefore work. **Verify**, given the blob-principal issues in [bug 1696174](https://bugzilla.mozilla.org/show_bug.cgi?id=1696174).

### 3.7 Firefox for Android (brief)

- Not viable for this extension:
  - `identity` and `storage.managed` are `firefox_android: false`.
  - `downloads` was removed in Firefox for Android 79 (BCD).
  - Mozilla recommends MV2 for Android ([Developing for Firefox for Android](https://extensionworkshop.com/documentation/develop/developing-extensions-for-firefox-for-android/)).
- No primary source on Android enterprise policies was found.

## 4. Safari (macOS)

Apple's compatibility page is the anchor ([Assessing your Safari web extension's browser
compatibility](https://developer.apple.com/documentation/safariservices/assessing-your-safari-web-extension-s-browser-compatibility)):
"`identity`: Not supported. Initiate an OAuth flow in a new tab."; "`update_url`: Not
supported. Handle Safari web extension updates with the App Store."

### 4.1 API support

| Used by us | Safari (macOS) | Notes / workaround |
|---|---|---|
| `identity.*` | **No** | §4.1.1 |
| `downloads.*` | **No** | §4.1.2. Apple's packager names `downloads` in its own unsupported-keys warning ([Packaging a web extension for Safari](https://developer.apple.com/documentation/safariservices/packaging-a-web-extension-for-safari)) |
| `storage.managed` / `managed_schema` | **No** | §4.3 |
| `storage.local`, `onChanged` | 14 | |
| `permissions.request/contains` | 14 | "Only specific origin patterns will prompt the user" (BCD) |
| `host_permissions` / `optional_host_permissions` | 15.4 / 15.5 | |
| `tabs.query/sendMessage/onRemoved/onUpdated` | 14 | "The extension needs host permission" (Apple compatibility page) |
| `runtime.sendMessage/onMessage/openOptionsPage` | 14 | |
| `runtime.sendNativeMessage` | 14 | To the containing app's extension handler only |
| `options_ui` | 14 | "Options pages are always opened in a separate browser tab" (BCD) |
| `background.service_worker` | 15.4 | |
| `content_scripts.all_frames` | 14 | |
| `content_scripts.match_origin_as_fallback` / `match_about_blank` | **18.4** | |
| `key`, `update_url` | No | ID comes from the bundle ID; updates come through the app |

Sources: BCD files cited in §3.1 (Safari columns); Safari 15.4, 15.5 and 18.4 release notes
([Safari release notes index](https://developer.apple.com/documentation/safari-release-notes)).
The 18.4 notes read: "Added support for `match_about_blank` and `match_origin_as_fallback` to
inject content scripts and styles into more frames."

**Content scripts need the user's grant.** This matters more than any single API.
- BCD: "Content scripts are not applied to tabs until the user grants permission via the extension's access popover in the toolbar".
- So by default the guard does not run on claude.ai until the user clicks "Allow". On a managed Mac the DDM `AllowedDomains` key can pre-grant the site (§4.3), which is an inference from the schema text.
- On upgrade, "Safari Web Extensions are now turned off upon update if the new version requests more host permissions" (Safari 16.4 release notes).

#### 4.1.1 Sign-in without `identity`

Three documented building blocks. Our server already supports the pieces two of them need.

1. **A tab flow**, which is Apple's own suggestion.
   - `tabs.create` opens `<server>/auth/login?next=/auth/<landing>`. A same-origin path is already accepted by `safe_next()`.
   - The background watches `tabs.onUpdated` or `webNavigation` for the landing URL and closes the tab.
   - `webNavigation` only fires where the extension has host access: "Fixed WebNavigation events to no longer fire for webpages where the extension hasn't been granted access" (Safari 17 release notes). The server origin must be granted.
   - The session cookie lands in Safari's normal cookie jar as a first-party cookie of the server. **Not documented:** whether a later background `fetch(..., {credentials: "include"})` from the extension sends it. Safari 18.4 notes add that developers "will need to add a `browser.permissions.request({origins: []})` call before doing any `fetch()` that is blocked by CORS".
   - **This is the biggest unknown on Safari** (§8).
2. **The containing app signs in** using the ADR 0003 loopback flow ([ADR 0003](adr/0003-native-client-sign-in.md)).
   - The app opens the system browser to `/auth/login?next=http://127.0.0.1:<port>/...`, receives the one-time code, and exchanges it at `/auth/exchange` for a login-session token.
   - The extension background gets the token over `browser.runtime.sendNativeMessage` and sends `Authorization: Bearer`. The server already accepts this (`bearer_token()` in `webui/auth.py`).
   - Native messaging constraints ([Messaging between the app and JavaScript in a Safari web extension](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension)):
     - It needs the `nativeMessaging` permission.
     - It reaches only the containing app's extension handler: "Safari ignores the `application.id` parameter".
     - "Content scripts that are injected into web content cannot send messages to the native app extension". Only the background and extension pages can.
     - App and appex share data only through an App Group.
   - This sidesteps cookies entirely.
3. **`ASWebAuthenticationSession` in the app.** It works on macOS 10.15+: "In macOS, the system opens the user's default browser if it supports web authentication sessions, or Safari otherwise" ([ASWebAuthenticationSession](https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession)).
   - It needs a presentation anchor window, so it belongs in the app, not the appex. **Not confirmed** whether the appex can use it.
   - Its callback is a custom scheme or https, so the server would need a new allowlist entry.

#### 4.1.2 Downloads without `downloads`

- **Impossible:** there is no way to observe, cancel or erase a browser download. That rules out the `onCreated` intercept of https-link and script-started downloads.
- **Still possible:**
  - The capture-phase click on a `blob:` link in `content.js` can still stop the page's download.
  - The restored file can be saved from the content script with a fresh `blob:` URL and `<a download>`. Safari 18.1 notes: "Fixed blob URL downloads failing to trigger from an extension".
  - That is an inference that `<a download>` from extension context works; **verify**.
- **What the user loses:** any download not caught at the click lands with tokens. The user would use *Unmask file* by hand.

### 4.2 Background

- Safari 15.4 "Added support for `manifest_version` 3 ... `service_worker` background scripts", and Safari 16.4 added module workers.
- Safari also accepts non-persistent `background.scripts`. "Use a service worker for better compatibility with other browsers" ([Optimizing your web extension for Safari](https://developer.apple.com/documentation/safariservices/optimizing-your-web-extension-for-safari)). The dual `scripts` + `service_worker` manifest of §3.2 is therefore acceptable to Safari too.
- `preferred_environment` is available from Safari 18 (BCD).
- One Safari quirk to know: 17.6 fixed background pages that "would stop responding after about 30 seconds".

### 4.3 Enterprise (macOS)

**Lock the extension on: yes, via DDM.**
- The declarative configuration is `com.apple.configuration.safari.extensions.settings` ([schema](https://github.com/apple/device-management/blob/release/declarative/declarations/configurations/safari.extensions.settings.yaml), repo release v27.0, 2026-09-17; [Apple Platform Deployment: Safari extensions management](https://support.apple.com/guide/deployment/safari-extensions-management-declarative-depff7fad9d8/web)).
- Platform: macOS 15+, **supervised**, user scope; iOS 18+ supervised.
- Payload: `ManagedExtensions`, keyed by `"<bundle id> (<team id>)"`, with:
  - `State`: `Allowed` | `AlwaysOn` | `AlwaysOff`.
  - `PrivateBrowsing`: `AlwaysOn` keeps it on in private windows "if the extension is on outside of Private Browsing".
  - `AllowedDomains` / `DeniedDomains`.
- "In order for the extension to be managed, its host app needs to be present on the device". The declaration does not install the app.
- Apple: "These extension management features work for standard browsing and Private Browsing". Safari 18 release notes: "Added support for Device Management of extension enabled state, private browsing state, and website access".
- With `AlwaysOn` the user cannot switch it off. Removing the containing app is governed by how the app was installed (MDM-managed app), which is outside this declaration.

**Install the app.**
- Use MDM app installation: DDM `com.apple.configuration.app.managed` (macOS 26+), or a signed package via MDM.
- "A package needs to be signed with a signature verifiable by the device" ([Distribute packages to Mac computers](https://support.apple.com/guide/deployment/distribute-packages-to-mac-computers-dep873c25ac4/web)).

**Push `serverUrl` / `guardLocked`: no `storage.managed`. The routes:**
- **Newest, documented:** `com.apple.configuration.app.managed` has `AppConfig` and an `ExtensionConfigs` dictionary keyed by the extension's composed ID. Both are documented for iOS 18.4 and **macOS 27.0** in [`app.managed.yaml`](https://github.com/apple/device-management/blob/release/declarative/declarations/configurations/app.managed.yaml). The appex would read them with the [ManagedApp](https://developer.apple.com/documentation/managedapp) framework ("works with apps and app extensions").
  - **Discrepancy:** ManagedApp's own doc page lists iOS/iPadOS 18.4 and visionOS 2.4, not macOS. **Unconfirmed on macOS.**
- **Older macOS:** a managed-preferences profile (`com.apple.ManagedClient.preferences`) for the app's own domain, read by the app or appex and forwarded on `sendNativeMessage`.
  - **Not confirmed:** that a sandboxed appex can read that domain. No Apple doc found.
- **Either way, the extension's JS needs a Safari-only path.** It asks the native handler for its config where Chrome calls `storage.managed.get`.

**Private windows.**
- Without MDM, Safari 17+ has a per-extension user setting: BCD `incognito`, "Controlled by a user setting. When allowed, operates in spanning mode". The user can turn it off.
- With DDM, `PrivateBrowsing: AlwaysOn`.

### 4.4 Distribution and signing

**A containing app is required.** "You implement a Safari web extension as a macOS, visionOS, or iOS app extension" ([Safari web extensions](https://developer.apple.com/documentation/safariservices/safari-web-extensions)).

**Packaging.**
- `xcrun safari-web-extension-packager <dir>` ("This tool used to be named `safari-web-extension-converter`") generates the Xcode project ([Packaging a web extension for Safari](https://developer.apple.com/documentation/safariservices/packaging-a-web-extension-for-safari)).
- App Store Connect also offers a web packager "without requiring a Mac or access to Xcode", but its output goes only to TestFlight and the App Store ([Packaging and distributing with App Store Connect](https://developer.apple.com/documentation/safariservices/packaging-and-distributing-safari-web-extensions-with-app-store-connect)).
- Native-messaging code (§4.1.1, §4.3) has to be written in Xcode. **A macOS build machine with Xcode is needed.**

**Three distribution routes.**
- **Mac App Store.** Public or unlisted listing, with App Review, and updates through the App Store.
- **Developer ID + notarization outside the Store.** "you can sign and notarize your extension's app with a Developer ID to distribute it outside the Mac App Store" ([Distributing your Safari web extension](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension)).
  - Safari loads such extensions **only from 18.4**: "Added support for loading Safari Web Extensions that have been Developer ID-signed and notarized" (Safari 18.4 release notes; checked 2026-10-02).
  - Apple documents no auto-update for this route. Updates are re-pushed by MDM or come from a third-party updater.
- **Custom App** through Apple Business Manager: private distribution with App Review ([Distribute Custom Apps](https://support.apple.com/guide/deployment/distribute-custom-apps-dep0113f6e18/web)). The page does not state macOS support verbatim; **verify**.

**Cost.** "The Apple Developer Program annual fee is 99 USD and the Apple Developer Enterprise Program annual fee is 299 USD" ([Enrollment](https://developer.apple.com/support/enrollment/)). Developer ID and notarization are in the standard program ([Developer ID](https://developer.apple.com/developer-id/)).

**Development builds.** "Allow unsigned extensions" resets when Safari quits. "Add Temporary Extension…" (Safari 18.4+) lasts at most 24 hours ([Running your Safari web extension](https://developer.apple.com/documentation/safariservices/running-your-safari-web-extension)).

### 4.5 Extension identity

- **The ID is a composed string.** It is the extension's bundle identifier plus team identifier, `"com.example.App.Extension (TEAMID)"`. That is the DDM key format, and the format shown in WWDC22 session 10099 ([video](https://developer.apple.com/videos/play/wwdc2022/10099/)).
  - **Not confirmed:** that `browser.runtime.id` returns exactly that string.
- **The internal origin is not stable.** "When an extension is turned on in a profile, it is an entirely new instance of that extension. This means each instance will have a different UUID" ([WWDC23 10119](https://developer.apple.com/videos/play/wwdc2023/10119/)). Do not allowlist the extension origin on the server.
- **Consequence for the server.** Neither sign-in route in §4.1.1 needs a per-extension redirect host. The tab flow uses a same-origin path; the app flow uses loopback. So Safari adds no entry to `MASKROOM_EXTENSION_IDS`.

### 4.6 Content scripts

- **Frames.**
  - `all_frames` works.
  - `match_origin_as_fallback` and `match_about_blank` need Safari 18.4, so **18.4 is the practical minimum**.
  - 16.4 fixed "content scripts not injecting into subframes when extension accesses the page after a navigation". This is relevant to claude.ai's single-page navigation.
- **No content-script native messaging.** Content scripts cannot use native messaging (§4.1.1). Everything goes through the background, which is already our design.
- **Keyboard capture.** No Safari-specific documentation was found on capture-phase `keydown` or on dispatching a synthetic `KeyboardEvent` into a ProseMirror editor (WebKit). **Verify on claude.ai.**
- **Cookies.** Safari has no `credentials: "include"` exemption documented for extension pages. See §4.1.1.

### 4.7 iOS / iPadOS (brief)

- **Platform.** Safari web extensions run on iOS 15+, with a non-persistent background or a service worker.
- **Restrictions** (Apple compatibility page):
  - No `webRequest`.
  - No `windows.create/update/remove`.
  - "You can't send messages from a containing iOS app to your web extension's JavaScript scripts" (messaging doc).
- **Distribution:** App Store, TestFlight, Custom App or Enterprise. There is no Developer ID on iOS.
- **Management:** DDM can force the extension on (iOS 18, supervised). `ExtensionConfigs` and ManagedApp (iOS 18.4) are the documented way to hand it a server URL.
- **Value:** downloads and identity are absent here too. Only the composer guard and on-screen unmask would carry over.

## 5. One codebase, several manifests

- **What can be shared.** The shared JS can stay as it is (`chrome.*` works in Firefox, MDN). A dual `background` key serves Chrome, Edge, Firefox and Safari (§3.2, §4.2).
- **What cannot.** The following are per-browser:
  - `key` (Chrome/Edge self-hosted only).
  - Top-level `update_url` (Chrome/Edge; Safari ignores it).
  - `browser_specific_settings.gecko.{id, update_url, data_collection_permissions}` (Firefox).
  - `downloads`, `identity` and `nativeMessaging` in `permissions` (Safari lacks the first two and needs the third).
  - `storage.managed_schema` (Chrome/Edge only).
- **Branch points in the code.**
  - `saveFile()`: `data:` URL on Chrome, background blob on Firefox, content-script `<a download>` on Safari.
  - `login()`/`logout()`: identity, or tab or native flow.
  - `managed()`: `storage.managed`, or native config.
  - The download intercept: absent on Safari.

## 6. Server-side implications

- **`safe_next()`** ([`webui/auth.py`](../webui/auth.py) ~L314):
  - **Edge (self-hosted):** no change.
  - **Edge (Add-ons):** add the store-assigned ID to `MASKROOM_EXTENSION_IDS`.
  - **Firefox:** add a rule for `https://<sha1(gecko.id)>.extensions.allizom.org/`. The alternative is Firefox's port-less `http://127.0.0.1/mozoauth2/<hash>` form, which `is_loopback()` rejects today. Firefox intercepts that URL itself; nothing listens on it.
  - **Safari:** a same-origin landing path, already accepted, or the existing loopback code exchange.
- **`/auth/signed-out`** redirects only to `https://` targets that pass `safe_next()`. The Firefox `allizom.org` host would need the same allowance for the silent logout.
- **Update manifests:** the server generates gupdate XML only. Firefox self-hosting needs an `updates.json` route and `application/x-xpinstall` for `.xpi`. Safari updates are outside the server.

## 7. Summary

| Feature | Chrome (today) | Edge | Firefox (desktop) | Safari (macOS) |
|---|---|---|---|---|
| MV3 background | works | works | **workaround**: event page via `background.scripts` | works (service worker 15.4+) |
| Content scripts in claude.ai + claudeusercontent frames | works | works | works (128+) | **workaround**: 18.4+, and needs a user site grant or MDM `AllowedDomains` |
| Capture-phase Enter guard / replay | works | works (same engine) | expected to work, verify | expected to work, verify |
| Sign-in (`launchWebAuthFlow`) | works | works (redirect host to verify) | **workaround**: `allizom.org` host must be allowlisted on server | **impossible** as is; workaround: tab flow or app plus native messaging |
| Cookie session from background fetch | works | works | works with a granted host permission (verify) | **unknown**; bearer token via app avoids it |
| Download intercept (`onCreated`/cancel/erase/removeFile) | works | works | works | **impossible** |
| Save restored file (`downloads.download` data:) | works | works | **workaround**: blob URL in background | **workaround**: `<a download>` blob from content script |
| Click-time blob download intercept | works | works | works (verify page blob fetch) | works, save via workaround |
| `storage.managed` serverUrl / guardLocked | works | works (registry path from MS Q&A) | **workaround**: `3rdparty` policy or native manifest; no registry; restart to apply | **impossible**; workaround: app config plus native messaging |
| Optional http host permission | works | works | works (128+) | works (15.5+) |
| Force-install self-hosted package | works (managed device) | works (managed device; Entra-only unclear) | works, but XPI must be Mozilla-signed | **workaround**: MDM installs signed app; DDM forces extension on (macOS 15+, supervised) |
| User cannot remove/disable | works | works | removal blocked; disable: verify; host perms locked from 153 | DDM `AlwaysOn` |
| Guard in private windows | disable Incognito | disable InPrivate (policy cannot enable extension) | `private_browsing: true` (136+) or disable | DDM `PrivateBrowsing: AlwaysOn` |
| Fixed ID | `key` | same `key` (self-hosted); new ID on Add-ons | `gecko.id` (we choose) | bundle ID + team ID |
| Signing / build | own PEM | own PEM, or Add-ons (free) | AMO unlisted signing per version | Apple Developer Program ($99/yr), Xcode on a Mac, notarization; Developer ID needs Safari 18.4+ |

## 8. Open questions that need a real machine

1. **Edge:** the value of `chrome.identity.getRedirectURL()`, expected `https://lcmdehcdpfddkjgajmlpgfholdekpgio.chromiumapp.org/`, and a complete sign-in and silent sign-out.
2. **Edge on Windows:** does a self-hosted CRX force-install on an *Entra-joined (not hybrid)* device? Microsoft's pages disagree (§2.3).
3. **Edge managed storage:** does `HKLM\SOFTWARE\Policies\Microsoft\Edge\3rdparty\extensions\<id>\policy` populate `storage.managed`? What is the macOS domain (`com.microsoft.Edge.extensions.<id>`?)? Does `edge://policy` show the values?
4. **Firefox sign-in:** after `launchWebAuthFlow` completes, does the background `fetch(..., {credentials: "include"})` carry the server cookie (Total Cookie Protection, granted host permission)? Does `interactive: false` sign-out reach the provider's end-session URL?
5. **Firefox downloads:** does the cancel/`search`/`complete`/`removeFile` sequence in `intercept()` behave as on Chrome? Does `byExtensionId` filter our own blob-URL saves? Can the content script `fetch()` a claude.ai page-created `blob:` URL?
6. **Firefox enterprise:**
   - Can the user *disable* a `force_installed` extension?
   - Can they untick "Run in Private Windows" when `private_browsing: true`?
   - Does `storage.managed` fire `onChanged` (BCD contradicts itself)?
   - Is `data_collection_permissions` enforced on unlisted submissions?
7. **Firefox content script:** does the synthetic Enter in `resend()` (Xray-constructed event) submit claude.ai's composer?
8. **Safari sign-in:** after a tab-based login on the server origin, does a background or service-worker `fetch(..., {credentials: "include"})` send the first-party cookie? Does ITP or partitioning interfere? This decides between the tab flow and the app-plus-bearer flow.
9. **Safari config:**
   - Can the sandboxed appex read a managed-preferences domain on macOS 15/26?
   - Is ManagedApp `ExtensionConfigs` actually available on macOS 27?
   - Does `browser.runtime.id` equal the DDM composed ID?
10. **Safari DDM:** does `AllowedDomains: ["*claude.ai", "*claudeusercontent.com", <server>]` pre-grant site access so content scripts run without the toolbar prompt?
11. **Safari downloads:** does `<a download>` with a `blob:` URL created in the content script save the restored file without a prompt? Does the click-time capture still stop claude.ai's own blob download in WebKit?
12. **Safari content script:** does the capture-phase guard win against ProseMirror? Is the synthetic Enter honoured in WebKit? Do the `claudeusercontent.com` preview frames get the script (18.4 `match_origin_as_fallback`)?

## 9. Recommended order of effort

1. **Edge first.** It is nearly free: it reuses the CRX, update.xml, ID and server allowlist. The work is Edge policy files (registry and plist under the `Microsoft\Edge` / `com.microsoft.Edge` paths) and settling questions 1–3 on one managed Windows machine and one Mac.
2. **Firefox second.** The work:
   - Add `gecko` settings and a dual `background`.
   - Add the Firefox branches in `saveFile()` and `managed()`.
   - Add the `allizom.org` allowlist rule.
   - Serve an `updates.json`.
   - Set up AMO unlisted signing (an AMO account and API keys; free).
   - Provide `3rdparty` policy examples.
   It needs ESR 128+ for `match_origin_as_fallback`, and Firefox 153 (ESR 153) for host permissions locked under force-install.
3. **Safari last, and only if a customer has supervised Macs.**
   - It needs a Mac with Xcode and an Apple Developer Program membership.
   - Before any porting, settle the cookie question (8) and the managed-config question (9).
   - The result is a reduced product: guard, mask and on-screen unmask, with no download intercept.
   - iOS/iPadOS only after that, if at all.

---

## Sources

**MDN / browser-compat-data** (commit `f2dd714f49`, 2026-10-02)
- [identity](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/identity), [identity.getRedirectURL](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/identity/getRedirectURL), [storage.managed](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/API/storage/managed), [Native manifests](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Native_manifests), [background](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/background), [browser_specific_settings](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/browser_specific_settings), [host_permissions](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/host_permissions), [content_scripts](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/manifest.json/content_scripts), [Content scripts](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Content_scripts), [Chrome incompatibilities](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/Chrome_incompatibilities), [JavaScript APIs](https://developer.mozilla.org/en-US/docs/Mozilla/Add-ons/WebExtensions/JavaScript_APIs)
- BCD `webextensions/api/{identity,downloads,storage,permissions,tabs,runtime}.json`, `webextensions/manifest/{background,content_scripts,host_permissions,optional_host_permissions,options_ui,storage,browser_specific_settings,incognito}.json` at [github.com/mdn/browser-compat-data](https://github.com/mdn/browser-compat-data/tree/main/webextensions). BCD has no entries for `key` or `update_url`.

**Mozilla**
- Extension Workshop: [MV3 migration guide](https://extensionworkshop.com/documentation/develop/manifest-v3-migration-guide/), [Signing and distribution overview](https://extensionworkshop.com/documentation/publish/signing-and-distribution-overview/), [Self-distribution](https://extensionworkshop.com/documentation/publish/self-distribution/), [Updating your extension](https://extensionworkshop.com/documentation/manage/updating-your-extension/), [web-ext command reference](https://extensionworkshop.com/documentation/develop/web-ext-command-reference/), [Source code submission](https://extensionworkshop.com/documentation/publish/source-code-submission/), [Add-on policies](https://extensionworkshop.com/documentation/publish/add-on-policies/), [Firefox built-in data consent](https://extensionworkshop.com/documentation/develop/firefox-builtin-data-consent/), [Enterprise distribution](https://extensionworkshop.com/documentation/enterprise/enterprise-distribution/), [Enterprise development](https://extensionworkshop.com/documentation/enterprise/enterprise-development/), [Developing for Firefox for Android](https://extensionworkshop.com/documentation/develop/developing-extensions-for-firefox-for-android/)
- Firefox admin reference: [ExtensionSettings](https://firefox-admin-docs.mozilla.org/reference/policies/extensionsettings/), [Preferences](https://firefox-admin-docs.mozilla.org/reference/policies/preferences/), [Firefox 153 release notes](https://firefox-admin-docs.mozilla.org/release-notes/version/firefox-153/); [policy-templates 3rdparty](https://mozilla.github.io/policy-templates/#3rdparty)
- Bugzilla: [1573659](https://bugzilla.mozilla.org/show_bug.cgi?id=1573659) (service worker), [1318564](https://bugzilla.mozilla.org/show_bug.cgi?id=1318564) (data: download), [1696174](https://bugzilla.mozilla.org/show_bug.cgi?id=1696174) (blob download), [1608685](https://bugzilla.mozilla.org/show_bug.cgi?id=1608685) (extension requests first-party)
- Firefox source (gecko-dev mirror): `toolkit/components/extensions/{child,parent}/ext-identity.js`, `modules/libpref/init/all.js`

**Microsoft**
- [Supported APIs](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/api-support), [Port a Chrome extension](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/port-chrome-extension), [Alternate distribution options](https://learn.microsoft.com/en-us/microsoft-edge/extensions/developer-guide/alternate-distribution-options), [Register as a developer](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/create-dev-account), [Publish an extension](https://learn.microsoft.com/en-us/microsoft-edge/extensions/publish/publish-extension)
- Policies: [ExtensionInstallForcelist](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensioninstallforcelist), [ExtensionSettings](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/extensionsettings), [ExtensionSettings detailed guide](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-ref-guide), [Self-host extensions](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-manage-extensions-webstore), [InPrivateModeAvailability](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-browser-policies/inprivatemodeavailability), [Extensions in InPrivate by policy](https://learn.microsoft.com/en-us/troubleshoot/microsoft-edge/manageability/enable-extension-inprivate-policy), [Configure Edge on macOS (Jamf)](https://learn.microsoft.com/en-us/deployedge/configure-microsoft-edge-on-mac-jamf), [Edge mobile beta release notes](https://learn.microsoft.com/en-us/deployedge/microsoft-edge-relnote-mobile-beta-channel)
- Microsoft Q&A, used only where no doc exists, and marked as such: [1461058](https://learn.microsoft.com/en-us/answers/questions/1461058/), [753622](https://learn.microsoft.com/en-us/answers/questions/753622/), [1411624](https://learn.microsoft.com/en-us/answers/questions/1411624/)

**Google**
- [chrome.identity](https://developer.chrome.com/docs/extensions/reference/api/identity), [What's new in Chrome extensions](https://developer.chrome.com/docs/extensions/whats-new), [Chrome policy templates JSON](https://chromeenterprise.google/static/json/policy_templates_en-US.json)

**Apple**
- Safari Services: [Safari web extensions](https://developer.apple.com/documentation/safariservices/safari-web-extensions), [Assessing browser compatibility](https://developer.apple.com/documentation/safariservices/assessing-your-safari-web-extension-s-browser-compatibility), [Packaging a web extension](https://developer.apple.com/documentation/safariservices/packaging-a-web-extension-for-safari), [Packaging with App Store Connect](https://developer.apple.com/documentation/safariservices/packaging-and-distributing-safari-web-extensions-with-app-store-connect), [Distributing your Safari web extension](https://developer.apple.com/documentation/safariservices/distributing-your-safari-web-extension), [Running your Safari web extension](https://developer.apple.com/documentation/safariservices/running-your-safari-web-extension), [Optimizing for Safari](https://developer.apple.com/documentation/safariservices/optimizing-your-web-extension-for-safari), [Messaging between the app and JavaScript](https://developer.apple.com/documentation/safariservices/messaging-between-the-app-and-javascript-in-a-safari-web-extension)
- [ASWebAuthenticationSession](https://developer.apple.com/documentation/authenticationservices/aswebauthenticationsession), [ManagedApp](https://developer.apple.com/documentation/managedapp)
- [Safari release notes](https://developer.apple.com/documentation/safari-release-notes): 15.4, 15.5, 16.4, 17, 17.6, 18, 18.1, 18.4, 26, 27
- [apple/device-management](https://github.com/apple/device-management/tree/release): `declarative/declarations/configurations/safari.extensions.settings.yaml`, `app.managed.yaml`
- Apple Platform Deployment: [Safari extensions management](https://support.apple.com/guide/deployment/safari-extensions-management-declarative-depff7fad9d8/web), [Distribute packages to Mac computers](https://support.apple.com/guide/deployment/distribute-packages-to-mac-computers-dep873c25ac4/web), [Distribute Custom Apps](https://support.apple.com/guide/deployment/distribute-custom-apps-dep0113f6e18/web)
- [Developer Program enrollment](https://developer.apple.com/support/enrollment/), [Developer ID](https://developer.apple.com/developer-id/), WWDC [22/10099](https://developer.apple.com/videos/play/wwdc2022/10099/), [23/10119](https://developer.apple.com/videos/play/wwdc2023/10119/)

**Repository**
- [`extension/`](../extension) (manifest, background, content, options, managed schema, README), [`webui/auth.py`](../webui/auth.py) (`is_loopback`, `safe_next`, `bearer_token`, `/auth/signed-out`), [`enterprise/RUNBOOK.md`](../enterprise/RUNBOOK.md), [`enterprise/policies/`](../enterprise/policies), [`enterprise/update.xml`](../enterprise/update.xml), [ADR 0003](adr/0003-native-client-sign-in.md), [ADR 0006](adr/0006-the-guard-intercepts-enter-and-fails-closed.md)

### Verification

- **How the sources were read.**
  - Apple developer pages are JavaScript-rendered. They were read through `https://developer.apple.com/tutorials/data/documentation/<path>.json`.
  - The Safari 18.4 "Developer ID-signed and notarized" line and the compatibility page's `identity`/`update_url` lines were re-checked there on 2026-10-02.
  - The Firefox 153 host-permission line and the `ExtensionSettings` `force_installed`, `install_url`, `update_url` and `private_browsing` text were re-checked on firefox-admin-docs on 2026-10-02.
- **Firefox's `allizom.org` redirect domain and SHA-1 derivation** come from Firefox source on the gecko-dev GitHub mirror, not from prose documentation. The `mozilla-firefox/firefox` mirror rate-limited the fetch.
- **The `mozilla/policy-templates` README now says it is not current.** Current policy text is cited from firefox-admin-docs.mozilla.org.
- **Microsoft Q&A answers** (Edge managed-storage path, store ID and `key`) are staff answers, not documentation. They are marked as such and listed in §8.
- **Nothing was run on a real browser.** Every "verify" in this document is untested.
