# Research — delivering Maskroom's protection to Claude Desktop users

*Researched 2026-09-23 against primary sources (Anthropic/Claude support and developer
documentation, the Claude Desktop Linux package Anthropic publishes at `downloads.claude.ai`,
Grammarly's own support documentation, Apple developer documentation and macOS user guides,
Microsoft Learn Win32 reference, Electron and Chromium project documentation, Anthropic's
Consumer Terms and Usage Policy) and this repository's code. Desktop products and OS privacy
rules change quickly; re-verify the cited pages before acting on anything here.* Companion to
[`LLM_MIDDLEWARE_RESEARCH.md`](LLM_MIDDLEWARE_RESEARCH.md) and
[`ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md).

**The question.** The Chrome extension in [`extension/`](../extension) masks text in the
claude.ai composer before send (`TOK_<TYPE>_<ID>` tokens), guards Enter/send, masks attached
files, and restores tokens on screen and in downloaded files. It cannot reach the **Claude
Desktop** application. How can the same protection — mask before send, unmask on display,
mask files — be delivered to Claude Desktop users, and what does Grammarly (which inserts
itself into text fields of arbitrary desktop apps) actually do that we could copy? Which
options are supported by the vendors involved, and which are fragile or unsupported?
`LLM_MIDDLEWARE_RESEARCH.md` §1.2 and §1.6 and `ENTERPRISE_ENFORCEMENT.md` already cover the
API gateway pattern, Claude Code hooks, MCP, Inference hooks and the Claude Desktop MDM keys;
this document builds on them and covers only the desktop-app-specific delivery mechanisms.

**The short answer.**

1. **Claude Desktop is an Electron app whose Chat tab is claude.ai web content, and it is
   hardened against injection.** Verified from Anthropic's own Linux package
   (`claude-desktop_1.17377.1_amd64.deb`, Electron 42.5.1 / Chrome 148): `resources/app.asar`,
   `chrome_crashpad_handler`, `LICENSES.chromium.html`; the app's IPC guards accept only
   frames whose origin is `https://claude.ai` or `https://preview.claude.ai`; Electron fuses
   `runAsNode`, `nodeOptions` and `nodeCliInspect` are **disabled** and
   `embeddedAsarIntegrityValidation` and `onlyLoadAppFromAsar` are **enabled**. The only
   `loadExtension` use is a developer-profile path that loads React DevTools. There is no
   supported way to run our extension, a preload script or any code inside it, and modifying
   the bundle breaks integrity validation and code signing. **Not an option.**
2. **Grammarly's mechanism is the OS accessibility API, not app integration.** Grammarly for
   Mac needs exactly one macOS permission (Accessibility, PPPC bundle
   `com.grammarly.ProjectLlama`); Grammarly for Windows "relies on accessibility APIs" and
   checks the "UI Automation Text Pattern". It works in Electron apps because "Any HTML-based
   UI has accessibility support by default" (Electron 16+), and in browsers it *turns the
   browser extension off* and takes over from the desktop app. So a Maskroom desktop helper
   in the Grammarly style is **technically possible and platform-supported**: read the
   focused text via AX/UIA, show a floating "Mask" button, write back, and it works in Claude
   Desktop because Claude Desktop is Chromium. **Verified on Windows 2026-09-25** (§3.4):
   the composer is exposed as an `edit` element with a writable `ValuePattern`, and
   `ValuePattern.SetValue`, `IAccessible::put_accValue` and select-all + paste all replace
   the text in a way ProseMirror keeps and that reaches Anthropic as tokens. macOS is still
   untested. What it cannot do is *unmask replies on screen* (there is no way to repaint
   another app's text) and it cannot see files.
3. **The one supported way to see Claude Desktop's model traffic is "Claude Desktop on 3P"
   plus a gateway.** Anthropic documents a third-party-provider deployment mode
   (`inferenceProvider`, `inferenceGatewayBaseUrl`, `chatTabEnabled`, `bootstrapUrl`, all via
   MDM) in which "the Claude Code engine the app runs for every Chat, Cowork, and Code
   session" sends Messages-API requests to the customer's gateway. That is the desktop
   version of the `ANTHROPIC_BASE_URL` pattern in `LLM_MIDDLEWARE_RESEARCH.md` §1.4 and the
   only place where mask-before-send *and* unmask-on-display can be transparent and
   enforced. Anthropic's compatibility guide says a gateway should "inspect without
   modifying" because rewriting bodies breaks beta-header pairing and preserved thinking;
   body rewriting therefore works only for the plain-text parts we already rewrite in the
   API gateway design, and is *tolerated*, not supported. It requires an API/cloud
   billing relationship, not claude.ai subscriptions. The standard (claude.ai sign-in) mode
   has **no** gateway or proxy hook for the Chat tab.
4. **A clipboard/hotkey helper is the cheapest thing that works today in every app,
   including Claude Desktop**: global shortcut → copy selection → `POST /api/mask` → paste
   masked text. Global shortcuts and clipboard need no special permission on either OS;
   synthesising Cmd+C/Cmd+V on macOS needs the Accessibility permission (event taps and
   posting are gated on "Access for assistive devices"), and reading the pasteboard
   programmatically now triggers Apple's pasteboard-privacy alert unless it results from a
   paste-like user action. It is manual per message, has no guard and no on-screen unmask.
5. **On-screen unmasking of Claude's replies inside Claude Desktop is impractical.** It
   would require either injecting into the renderer (blocked, see 1) or drawing an overlay
   window positioned over text found through the accessibility tree — feasible for a
   demo, unusable for streamed, scrolling Markdown. Restoring *downloaded files* and
   *copied text* through the helper is realistic and reuses `/api/unmask` and
   `/api/unmask-file`.
6. **Files:** there is no hook into Claude Desktop's attach flow. The realistic paths are a
   Finder/Explorer context-menu "Mask with Maskroom" (macOS Services or Finder Sync,
   Windows static shell verbs) and a file-drop window in the helper, both writing a
   `*_masked` file the user then attaches.
7. **Routing desktop users back to the managed browser is the only path that gives the full
   feature set today.** The Claude Desktop MDM keys can disable Cowork
   (`secureVmFeaturesEnabled`), Code (`isClaudeCodeForDesktopEnabled`), local sessions,
   extensions and MCP, but **no documented key disables the Chat tab in standard mode**
   (`chatTabEnabled` is scoped to 3P deployments) and no Claude admin-console setting
   disables the desktop app. Blocking the app is an endpoint-management job (MDM/AppLocker)
   outside Anthropic's controls; `ENTERPRISE_ENFORCEMENT.md` then applies to the browser.
8. **Terms.** The Consumer Terms forbid accessing the Services "through automated or
   non-human means" and decompiling/reverse-engineering; a helper that only rewrites what a
   human is about to send is the same judgement call the extension already carries. In 3P
   mode users "don't need a claude.ai account", so the Commercial Terms apply instead.

Section 8 has the matrix and the recommended order of work.

---

## 1. What Claude Desktop is (verified)

### 1.1 Electron app, Chromium renderer, hardened fuses

Anthropic's support pages never say "Electron"; the evidence is Anthropic's own package.
The Linux apt repository documented in [Install Claude Desktop](https://support.claude.com/en/articles/10065433-install-claude-desktop)
(`https://downloads.claude.ai/claude-desktop/apt/stable`, "Maintainer: Anthropic PBC",
"Description: Desktop application for Claude.ai") was fetched and inspected on 2026-09-23:

| Fact | Evidence (`claude-desktop_1.17377.1_amd64.deb`) |
|---|---|
| Electron/Chromium | `/usr/lib/claude-desktop/` contains `claude-desktop` (217 MB binary with the strings `Electron/42.5.1` and `Chrome/148.0.7778.271`), `resources/app.asar` (36.9 MB), `chrome_crashpad_handler`, `chrome-sandbox`, `LICENSES.chromium.html`, `v8_context_snapshot.bin`, `libffmpeg.so`, `chrome_100_percent.pak`. `Depends:` is the standard Electron set (`libgtk-3-0, libnotify4, libnss3, libatspi2.0-0, libdrm2, libgbm1, libxtst6 …`). Native add-ons: `@ant/claude-native`, `node-pty` |
| Fuses | The Electron fuse sentinel (`dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX`) is followed by schema version 1, nine fuses, bytes `010011011`. In the order of `FuseV1Options` (`RunAsNode=0, EnableCookieEncryption=1, EnableNodeOptionsEnvironmentVariable=2, EnableNodeCliInspectArguments=3, EnableEmbeddedAsarIntegrityValidation=4, OnlyLoadAppFromAsar=5, LoadBrowserProcessSpecificV8Snapshot=6, GrantFileProtocolExtraPrivileges=7, WasmTrapHandlers=8` — [electron/fuses `config.ts`](https://raw.githubusercontent.com/electron/fuses/main/src/config.ts)) that is: **runAsNode off, cookie encryption on, NODE_OPTIONS off, --inspect off, asar integrity on, only-load-from-asar on**, V8-snapshot-per-process off, file:// privileges on, wasm trap handlers on |
| Chat tab = claude.ai web content | `app.asar` IPC handlers accept a message only when the sender frame's origin `=== "https://claude.ai"` or `=== "https://preview.claude.ai"`; the OAuth redirect is `https://claude.ai/desktop/callback`; windows are `WebContentsView`s driven by `loadURL(...)`. Anthropic's [Desktop page](https://code.claude.com/docs/en/desktop) lists the hosts the app loads "its application code and user content from": `claude.ai`, `*.claude.ai`, `assets-proxy.anthropic.com`, `*.claudeusercontent.com` … and the network-config page adds "The Claude Desktop app and claude.ai in a browser load their application code and user content from additional Anthropic CDN hosts" ([Enterprise network configuration](https://code.claude.com/docs/en/network-config)) |
| No accessibility opt-in | No call to `app.setAccessibilitySupportEnabled` / `isAccessibilitySupportEnabled` and no `AXManualAccessibility` string in `app.asar`; accessibility is whatever Chromium auto-detects (§3.3) |
| Extensions | `session.defaultSession.loadExtension` appears only in a developer-profile path ("React DevTools loaded from Chrome") and in a bundled `electron-devtools-installer`; "extensions" elsewhere in the bundle means `.mcpb` desktop extensions (`installDxt`, `loadExtensionMetadata`) |

What Electron says these fuses mean ([Electron Fuses](https://www.electronjs.org/docs/latest/tutorial/fuses)):
fuses are "'magic bits' in the Electron binary that can be flipped when packaging your
Electron app to enable or disable certain features/restrictions"; "Because they are flipped
at package time before you code sign your app, the OS becomes responsible for ensuring those
bits aren't flipped back via OS-level code signing validation." `runAsNode` controls whether
`ELECTRON_RUN_AS_NODE` is respected; `nodeOptions` whether `NODE_OPTIONS` and
`NODE_EXTRA_CA_CERTS` are respected; `embeddedAsarIntegrityValidation` "Validates `app.asar`
file content on macOS and Windows"; `onlyLoadAppFromAsar` loads app code "exclusively from
`app.asar`". The environment-variables page confirms `ELECTRON_RUN_AS_NODE` "is disabled if
the `runAsNode` fuse is deactivated" and `NODE_OPTIONS` "is ignored if the `nodeOptions` fuse
is disabled" ([Environment variables](https://www.electronjs.org/docs/latest/api/environment-variables)).

Caveats: only the **Linux** build was inspected. The macOS `.pkg`/`.dmg`
([Deploy Claude Desktop for macOS](https://support.claude.com/en/articles/12611117-deploy-claude-desktop-for-macos))
and Windows MSIX ([Deploy Claude Desktop for Windows](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows))
were not downloaded; they are built from the same bundle but their fuse bytes are
UNVERIFIED. Community packagers report the same structure on those platforms
([claude-desktop-debian discussion](https://github.com/aaddrick/claude-desktop-debian/discussions/529),
secondary).

**Consequence.** Every "put our code inside the app" idea — loading the Chrome extension,
a preload/`--require` script via `NODE_OPTIONS`, `ELECTRON_RUN_AS_NODE`, attaching a
debugger with `--inspect`, or editing `app.asar` — is closed by fuses, asar integrity and OS
code signing, and any patched copy is an unsigned, un-notarized, unsupported binary. Electron
itself states "Electron does not support arbitrary Chrome extensions from the store, and it
is a **non-goal** of the Electron project to be perfectly compatible with Chrome's
implementation of Extensions"; only unpacked extensions load, ".crx files do not work",
`chrome.storage.sync` and `chrome.storage.managed` "are **not** supported", and
`loadExtension` "must be called on every boot of your app"
([Extensions](https://www.electronjs.org/docs/latest/api/extensions),
[`ses.loadExtension`](https://www.electronjs.org/docs/latest/api/session)). Even if Anthropic
called it, our extension's managed-storage lock (`guardLocked`) would not exist there.

### 1.2 Standard mode vs "Claude Desktop on 3P"; where the Chat tab's traffic goes

Anthropic documents two deployment modes of the same binary:

- **Standard mode** (sign in with claude.ai): the Chat tab is the claude.ai web app inside a
  `WebContentsView`, talking to Anthropic directly. The support-site enterprise article lists
  the standard-mode policy keys ([Enterprise configuration for Claude Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)):
  `allowedWorkspaceFolders` ("Filepath or filepaths the user can mount to Cowork"),
  `autoUpdaterEnforcementHours`, `disableAutoUpdates`, `effortLevel`, `forceLoginOrgUUID`
  ("Require login to belong to a specific organization"), `isClaudeCodeForDesktopEnabled`
  ("Enable Claude code access in desktop"), `isDesktopExtensionEnabled` ("Enable/disable
  extensions"), `isDesktopExtensionDirectoryEnabled`, `isLocalDevMcpEnabled` ("Enable local MCP
  servers"), `secureVmFeaturesEnabled` ("Enable Cowork access in desktop"). macOS domain
  `com.anthropic.claudefordesktop`; Windows `HKLM:\SOFTWARE\Policies\Claude` (or HKCU).
  **None of these disables the Chat tab or points it anywhere.**
- **Third-party (3P) mode**: "Claude Desktop on third-party (3P) is configured through
  OS-native managed preferences … organizations can point Claude Desktop to their own
  inference backend instead of Anthropic's hosted service." "The app activates 3P mode only
  when [`inferenceProvider`] is set and the required credential keys for the selected provider
  are present and valid; otherwise it launches in standard mode." Keys include
  `inferenceProvider` (`gateway | anthropic | bedrock | mantle | vertex | foundry`),
  `inferenceGatewayBaseUrl` ("Full URL of inference gateway endpoint"),
  `inferenceGatewayApiKey`, `inferenceGatewayOidc`, `inferenceCustomHeaders`,
  `inferenceCredentialHelper`, `bootstrapUrl` ("HTTPS URL of a server that returns managed
  configuration after sign-in … Machine policy wins over bootstrap responses"),
  `chatTabEnabled` ("Show or hide the Chat tab in the app", "MDM + Bootstrap, Added in
  1.8089.0"), `disableDeploymentModeChooser` ("Users see only this provider at the login
  screen. The option to sign in to Claude.ai is hidden."), `egressProxyUrl`,
  `egressProxyPacUrl` ([Configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration)).
  In `app.asar` the `chatTabEnabled` schema entry is `scopes:["3p"]`, confirming it is a
  3P-only key.

In 3P mode the Chat tab does **not** talk to claude.ai. The network-proxy page describes
"**The agent**: the Claude Code engine the app runs for every Chat, Cowork, and Code session.
It sends inference requests…" and "When a session starts, the app asks the OS which proxy
applies to your inference endpoint (your gateway URL, or the provider endpoint …) and hands
that one proxy to the agent" ([Network proxy](https://claude.com/docs/third-party/claude-desktop/network-proxy)).
The Claude apps gateway page says the same from the gateway side: "Claude Desktop runs its
Cowork and Code tabs, plus the Chat tab when you enable it, on embedded Claude Code sessions
and sends their model requests through the gateway", "set `bootstrapUrl` in Claude Desktop's
managed configuration to `<listen.public_url>/user/bootstrap`", "To turn on the Chat tab as
well, set `chatTabEnabled` to `true`" ([Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway)).
This verifies and sharpens the sentence quoted in `LLM_MIDDLEWARE_RESEARCH.md` §1.2: it
applies to Desktop **in 3P/gateway mode only**.

### 1.3 Managed keys that matter for Maskroom

| Goal | Key(s) | Mode | Verdict |
|---|---|---|---|
| Keep raw-PII folders away from Cowork | `allowedWorkspaceFolders` | standard + 3P | Real, blunt (already noted in §1.2 of the middleware doc) |
| Switch off Cowork / Code / local sessions | `secureVmFeaturesEnabled`, `isClaudeCodeForDesktopEnabled`, `disableDesktopLocalSessions` ([Desktop page](https://code.claude.com/docs/en/desktop)) | standard | Reduces surfaces; Chat tab stays |
| Switch off the Chat tab | `chatTabEnabled` | **3P only** | No standard-mode equivalent documented; the app warns "At least one surface must remain enabled" (string in `app.asar`) |
| Force users into an org | `forceLoginOrgUUID` | standard | Does not change the client |
| Route model traffic through our gateway | `inferenceProvider: gateway`, `inferenceGatewayBaseUrl`, `bootstrapUrl`, `chatTabEnabled` | 3P | **The supported interception point** (§4.1) |
| Pin a proxy | `egressProxyUrl` / `egressProxyPacUrl` ("MDM only, Added in 1.44121.1") | documented on the 3P pages; listed as "app-behavior keys" that "can be in a managed profile alone" | Routing only: "A proxy setting is routing, not enforcement" |
| Block the desktop app entirely | none | — | Not an Anthropic control; the Cowork admin article documents only Cowork toggles ("Toggle off to disable Cowork for all users in your organization") ([Use Claude Cowork on Team and Enterprise plans](https://support.claude.com/en/articles/13455879-use-claude-cowork-on-team-and-enterprise-plans)) and the desktop-extension allowlist ([Enabling and using the desktop extension allowlist](https://support.claude.com/en/articles/12592343-enabling-and-using-the-desktop-extension-allowlist)) |

### 1.4 Network path, proxies and TLS

- **Proxy.** In 3P mode "With no proxy-related configuration, the app follows the operating
  system's proxy settings, including a PAC … script"; "Only `http://` and `https://` proxies
  are handed to the agent"; "There is no interactive proxy sign-in"; app updates "follow the
  OS proxy settings only". Standard mode is not covered by that page; the Electron/Chromium
  network stack follows OS proxy settings by default, and the standard-mode support article
  documents no proxy keys (VERIFIED absence, 2026-09-23).
- **TLS inspection.** "If your proxy performs TLS interception, it presents its own
  certificate authority. The app trusts the operating system's certificate store. On macOS,
  the app also configures the agent to trust the System keychain in addition to the bundled
  CA roots" ([Network proxy](https://claude.com/docs/third-party/claude-desktop/network-proxy)).
  For Claude Code: "Enterprise TLS-inspection proxies work without additional configuration
  when their root certificate is installed in the OS trust store and the runtime can read it"
  ([Enterprise network configuration](https://code.claude.com/docs/en/network-config)). No
  Anthropic page documents certificate pinning for the Chat tab; a community report says
  behind a Zscaler TLS-inspecting proxy "Chat and Cowork modes work fine" while the Code tab
  failed on CA propagation ([anthropics/claude-code#43158](https://github.com/anthropics/claude-code/issues/43158),
  secondary), consistent with the Chat tab trusting the OS store. The only documented pin is
  the CLI's pin of the *gateway's* leaf certificate ("The CLI fingerprints the gateway's TLS
  leaf certificate on first connect and pins it per hostname … inference requests use
  standard TLS validation without the pin").

So an enterprise TLS-inspection proxy *can* see the Chat tab's requests in standard mode
(they go to `claude.ai` origins, a proprietary web API, not the documented Messages API), but
nothing about rewriting them is supported: the format is undocumented, changes with the web
app, and any DLP-style rewrite would sit against the Consumer Terms (§7). This document
does not recommend it; see §4.2.

---

## 2. How Grammarly Desktop actually works

All statements are from Grammarly's support site, verified 2026-09-23.

### 2.1 macOS

- Permission: "In order to avoid the Accessibility prompt in the app, the configuration
  profile should be installed before the application is installed." The PPPC payload is
  Bundle ID `com.grammarly.ProjectLlama`, Access type **Accessibility**, "Allow", with the
  code requirement `anchor apple generic and identifier "com.grammarly.ProjectLlama" and (…
  certificate leaf[subject.OU] = W8F64X92K3)` ([How to deploy Grammarly for Mac](https://support.grammarly.com/hc/en-us/articles/8341875702413-How-to-deploy-Grammarly-for-Mac)).
  No Screen Recording, Input Monitoring or Automation permission is listed (VERIFIED absence).
- Mechanism: "For Grammarly for Mac to work on your website, the textual content must be
  made visible to accessibility by calling `setAccessibilityElement` on the corresponding
  `NSView`"; it recommends `NSTextView` or the `NSAccessibilityNavigableStaticText` protocol
  with `accessibilitySelectedTextRange()` / `setAccessibilitySelectedTextRange()`
  ([How do I integrate Grammarly with my website or application?](https://support.grammarly.com/hc/en-us/articles/10139846131213-How-do-I-integrate-Grammarly-with-my-website-or-application)).
  That is the NSAccessibility protocol, i.e. the same tree `AXUIElement` clients read (§3.1).

### 2.2 Windows

- "For Grammarly for Windows to work on your website or application, the textual content
  must be made visible to accessibility." Native apps: "Use Accessibility Insights for
  Windows to check that the text field properly supports UI Automation Text Pattern" and the
  "UI Automation Scroll Control Pattern" (same article). System requirements: "Windows 10
  (build 1903) or newer", ".NET Framework 4.7.2 +"
  ([System requirements](https://support.grammarly.com/hc/en-us/articles/4412835748877-What-are-the-system-requirements-for-Grammarly-for-Windows-and-Grammarly-for-Mac));
  the deployment article adds "WebView2" and a silent `/S` installer
  ([How to deploy Grammarly for Windows](https://support.grammarly.com/hc/en-us/articles/4422076438029-How-to-deploy-Grammarly-for-Windows)).
  No driver, service or special privilege is documented.

### 2.3 Electron apps and browsers

- "Any HTML-based UI has accessibility support by default" and "Grammarly doesn't initialize
  in input fields. Please use contenteditable or textarea tags and make sure the width is
  more than 150px." "If your app is Electron-based, we mainly support Electron 12+ for
  applications available in the App Store and Electron 16+ for other applications."
  (integration article). Claude Desktop is Electron 42 with a `contenteditable` ProseMirror
  composer (the extension's `SEL.composer` selectors), so it is squarely in Grammarly's
  supported envelope.
- Browsers: "You can use Grammarly in a variety of browsers by downloading Grammarly for
  Windows and Mac or by using one of our browser extensions"
  ([What do I need to use Grammarly?](https://support.grammarly.com/hc/en-us/articles/115000090811-What-do-I-need-to-use-Grammarly)),
  and "If you also use Grammarly for Windows and Mac, the browser extension will be turned
  off by default but will remain active in Google Docs"
  ([Browser extension user guide](https://support.grammarly.com/hc/en-us/articles/115000091592-Grammarly-s-browser-extension-user-guide)).
  So Grammarly does **not** defer to the extension; the desktop app takes over the browser
  through the same accessibility path (Google Docs, a canvas editor without a usable
  accessibility text tree, is the exception that proves the mechanism).
- UI: "a floating Grammarly widget should appear on your screen … When Grammarly detects a
  writing issue, it will underline the problematic word or phrase automatically. You'll also
  see the number of identified issues on the widget"; the widget has draggable "anchor
  points"; Microsoft Word gets "a different interface with a list of suggestions"; there is
  also "the Grammarly tab on the right side of your screen"
  ([Grammarly for Windows and Grammarly for Mac user guide](https://support.grammarly.com/hc/en-us/articles/4412816078349-Grammarly-for-Windows-and-Grammarly-for-Mac-user-guide)).
  The underline is drawn by Grammarly's own overlay window using the text's bounding
  rectangles from the accessibility tree (both AX and UIA expose them, §3); the app cannot
  draw inside the host's window.
- Where it cannot work, users turn it off per app: right-click the widget / menu-bar icon →
  "Turn off Grammarly", and "Settings → Block list"
  ([How to choose where Grammarly for Windows and Mac works](https://support.grammarly.com/hc/en-us/articles/4406998780813-How-to-choose-where-Grammarly-for-Windows-and-Mac-works));
  enterprise admins "specify which desktop applications Grammarly will not run on"
  ([Manage application controls](https://support.grammarly.com/hc/en-us/articles/27454187430029-Manage-application-controls)).

### 2.4 What it sees; privacy statements

"Grammarly can't access anything you type unless you are actively using a Grammarly product
offering, such as the browser extension, in a text field or you use AI features that
consider additional content"; "We make it clear when Grammarly is active"
([Is Grammarly a keylogger?](https://support.grammarly.com/hc/en-us/articles/360003816032-Is-Grammarly-a-keylogger)).
"Sensitive text such as credit card information or passwords is ignored or excluded by our
software on a best-effort basis" ([Does Grammarly's product read everything I write?](https://support.grammarly.com/hc/en-us/articles/360003835311-Does-Grammarly-s-product-read-everything-I-write)).
Technically the Accessibility permission lets an app read the *whole* tree of every app
("access and control your Mac through accessibility features" — Apple, §3.1); the "only the
focused text field" scope is Grammarly's design choice and privacy promise, not an OS
boundary. A Maskroom helper would have to make the same promise, and its audit log
(`_audit(action="mask" …)` in `webui/app.py`) should record only what the user chose to mask.

### 2.5 What this means for Maskroom

Grammarly proves the shape: **a signed, notarized helper with one permission (Accessibility
on macOS; none beyond a normal user process on Windows) can read the focused text of Claude
Desktop's composer, draw a floating button beside it, and write text back.** It also shows
the limits: everything Grammarly does is *about the text field the user is editing*. It has
no equivalent of our unmask view (it never rewrites the host's rendered output) and no
equivalent of file interception.

---

## 3. Platform accessibility APIs

### 3.1 macOS Accessibility (`AXUIElement`)

From Apple's `AXUIElement.h` reference: "Assistive applications use the functions defined in
this header file to communicate with and control accessible applications running in macOS";
`AXUIElementCreateApplication` "Creates the top-level accessibility object for an application
by process ID"; `AXUIElementCreateSystemWide` "Returns an accessibility object that provides
access to system attributes"; `AXUIElementCopyAttributeValue` "Returns the value of an
accessibility object's attribute"; `AXUIElementSetAttributeValue` "Sets an accessibility
object's attribute to a specified value"; `AXObserverCreate` / `AXObserverAddNotification`
register for notifications; errors include `kAXErrorAPIDisabled` ("Accessibility API is
disabled") and `kAXErrorNotImplemented` ("Process doesn't fully support accessibility API")
([AXUIElement.h](https://developer.apple.com/documentation/applicationservices/axuielement_h)).
`AXIsProcessTrustedWithOptions` "Returns whether the current process is a trusted
accessibility client"; `kAXTrustedCheckOptionPrompt` is "A CFBooleanRef indicating whether the
user will be informed if the current process is untrusted … Prompting occurs asynchronously"
([AXIsProcessTrustedWithOptions](https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions)).

Attributes and notifications we would use (Apple constant reference, verified via the
doc-data endpoints listed under Verification):

| Constant | Apple's description |
|---|---|
| `kAXFocusedApplicationAttribute` | "Indicates the application element that is currently accepting keyboard input. This attribute is supported by the system-wide accessibility object" |
| `kAXFocusedUIElementAttribute` | the focused element of an application (no abstract published) |
| `kAXValueAttribute` | "The value of an accessibility object is user-modifiable and represents the setting of the associated user interface element, such as the contents of an editable text field" |
| `kAXSelectedTextAttribute` | "The currently selected text within this accessibility object. This attribute is required for all accessibility objects that represent editable text elements." |
| `kAXValueChangedNotification` | "The value of an accessibility object's value attribute was changed." |
| `kAXFocusedUIElementChangedNotification` | "The focused accessibility object has changed." |
| `kAXSelectedTextChangedNotification` | "Notification that a different set of text was selected." |

`AXAttributeConstants.h` notes "Some attributes are settable, others are read-only"; whether
a given element's `AXValue` or `AXSelectedText` is settable is per element
(`AXUIElementIsAttributeSettable`) and, for a Chromium `contenteditable`, is **UNVERIFIED
here** — it must be tested with Accessibility Inspector against Claude Desktop. Grammarly's
guidance ("`setAccessibilitySelectedTextRange()`") suggests it writes through the selection
API rather than replacing `AXValue`. The robust fallback is select-all + paste (§5.1).

Permission: the user-facing description is "Apps … can access and control your Mac through
accessibility features", granted in System Settings › Privacy & Security › Accessibility
([Allow accessibility apps to access your Mac](https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac)).
This is the single TCC grant Grammarly for Mac ships a PPPC profile for.

### 3.2 Windows UI Automation

"Microsoft UI Automation is an accessibility framework that enables Windows applications to
provide and consume programmatic information about user interfaces (UIs). It provides
programmatic access to most UI elements on the desktop. It enables assistive technology
products, such as screen readers, to provide information about the UI to end users and to
manipulate the UI by means other than standard input"
([UI Automation](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32)).

- Reading: the **Text** pattern "enables applications and controls to expose a simple text
  object model, enabling clients to retrieve textual content, text attributes, and embedded
  objects from text-based controls"; `ITextRangeProvider::GetText` "should return the plain
  text in the range"; `GetBoundingRectangles` returns per-line rectangles (what an overlay
  needs); `UIA_Text_TextChangedEventId` "must be raised after any text change occurs"
  ([Text and TextRange Control Patterns](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-implementingtextandtextrange)).
- Writing: "**IValueProvider** complements **ITextProvider** by providing a programmatic way
  to change the text" (same page); `IValueProvider::SetValue` "Sets the value of control",
  but "multi-line edit controls do not implement **IValueProvider**; instead they provide
  access to their content by implementing **ITextProvider**"
  ([IValueProvider::SetValue](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcore/nf-uiautomationcore-ivalueprovider-setvalue));
  the Value-pattern guidelines add "Multi-line edit controls must implement **IValueProvider**
  if their contents can be changed" ([Value Control Pattern](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-implementingvalue)).
  Chromium does expose a writable ValuePattern on Claude Desktop's `contenteditable`
  (verified 2026-09-25, §3.4), even though Chromium's own UIA provider "is currently under development. It can be enabled via the
  `--enable-features=UiaProvider` browser command line switch"
  ([Chromium UI Automation](https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/browser/uiautomation.md)),
  and Chromium lists "IAccessibleEx plus UI Automation (very limited)" against "MSAA/IAccessible
  (complete), IAccessible2 (complete)" ([Chromium accessibility design doc](https://www.chromium.org/developers/design-documents/accessibility/)).
  Grammarly's "Any HTML-based UI has accessibility support by default" shows UIA clients do
  get usable text from Electron in practice (through Windows' MSAA/IA2 bridging), and the
  probe below confirms the writable surface on Claude Desktop. Select-all + paste remains
  the fallback, and it also worked.
- Focus: `IUIAutomation::AddFocusChangedEventHandler` — "Focus-changed events are
  system-wide; you cannot set a narrower scope"
  ([AddFocusChangedEventHandler](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation-addfocuschangedeventhandler)).
- No permission prompt exists on Windows; the constraint is UIPI: `SendInput` "is subject to
  UIPI. Applications are permitted to inject input only into applications that are at an
  equal or lesser integrity level" ([SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)).
  Claude Desktop is a per-user MSIX ("packaged as a per-user application by default"), i.e.
  medium integrity, same as a helper installed per user.

### 3.3 How Chromium/Electron expose the tree (and how to switch it on)

"Accessibility features in Chrome are off by default and enabled automatically on-demand";
because assistive APIs are "synchronous, while Chromium is multi-process", Chromium keeps
"a representation of the entire accessibility tree cached in the main process", which "may
lag what's in the renderer process by a fraction of a second"
([Chromium accessibility overview](https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md)).
Detection: on Windows "Chrome calls NotifyWinEvent with EVENT_SYSTEM_ALERT and the custom
object id of 1. If it subsequently receives a WM_GETOBJECT call for that custom object id, it
assumes that assistive technology is running"; on macOS Chromium "turns on or off
accessibility support based on whether it sees a client, such as VoiceOver, has set the
AXEnhancedUserInterface attribute on the main application window"; override with
`--force-renderer-accessibility` or `chrome://accessibility`
([design doc](https://www.chromium.org/developers/design-documents/accessibility/)).

Electron adds: "Electron applications will automatically enable accessibility features in
the presence of assistive technology (e.g. JAWS on Windows or VoiceOver on macOS)"; on macOS
"third-party assistive technology can toggle accessibility" by setting the
`AXManualAccessibility` attribute on the app element via `AXUIElementSetAttributeValue`
(code sample in the page); `app.setAccessibilitySupportEnabled(enabled)` exists but "the
user's system assistive utilities have priority over this setting and will override it"
([Electron Accessibility](https://www.electronjs.org/docs/latest/tutorial/accessibility)).
`app.isAccessibilitySupportEnabled()` "will return `true` if the use of assistive
technologies, such as screen readers, has been detected"; "Rendering the accessibility tree
can significantly impact your app's performance"
([Electron app API](https://www.electronjs.org/docs/latest/api/app)).

For a Maskroom helper this means: on macOS, set `AXManualAccessibility` (Electron) or
`AXEnhancedUserInterface` (Chromium) on Claude Desktop's application element once, then read
the focused element; on Windows, simply walking the tree with a UIA client triggers
Chromium's `WM_GETOBJECT` detection. No cooperation from Anthropic is needed, because
Claude Desktop does not call `setAccessibilitySupportEnabled` either way (§1.1). The
performance note is a real cost the user pays while the helper is running.

### 3.4 Does the claude.ai composer expose a writable value?

**Windows: yes, verified 2026-09-25** with `scripts/desktop/uia_composer_probe.py`
(Python 3.12.10, `uiautomation` 2.0.29, Claude Desktop `Claude.exe`, window class
`Chrome_WidgetWin_1`, `FrameworkId` `Chrome`). The composer is
`div[contenteditable="true"].ProseMirror` (`extension/content.js` `SEL.composer`); UIA
reports it as an `EditControl` named "Write your prompt to Claude", class
`tiptap ProseMirror ProseMirror-focused`, supporting `ValuePattern`, `TextPattern`,
`TextEditPattern`, `TextChildPattern`, `ScrollItemPattern` and `LegacyIAccessiblePattern`.

| Probe step | Result |
|---|---|
| Read: `TextPattern.DocumentRange.GetText`, `ValuePattern.Value`, `IAccessible.accValue` | All return the typed text |
| `TextPattern.DocumentRange.GetBoundingRectangles` | One rectangle per line — enough to anchor an overlay button |
| `ValuePattern.IsReadOnly` | `False` |
| Write 1: `ValuePattern.SetValue(masked)` | Text replaced; shown on screen; survived typing another character; **sent message contained the tokens** |
| Write 2: `LegacyIAccessiblePattern.SetValue` (`IAccessible::put_accValue`) | Same outcome as write 1 |
| Write 3: clipboard + `SendInput` Ctrl+A, Ctrl+V | Same outcome as write 1 |

ProseMirror keeps its own document model and re-renders the DOM; the concern was that a
value written through the accessibility API would land in the DOM but be reverted by the
editor. It is not: all three writes were kept through further typing and through send.
Two things the probe also showed: Chromium's tree is off until a UIA client asks (the
first run, before the composer had focus, saw only a bare `PaneControl` with no text
patterns), and the first `GetFocusedControl` after Claude gains focus can take a moment,
so a helper should poll rather than read once.

**macOS: still untested.** The same question applies to `AXValue` / `AXSelectedText`
(§3.1); the Windows result makes a positive answer likely but not certain, because the
macOS write goes through a different Chromium accessibility bridge.

---

## 4. Network-level interception of Claude Desktop

### 4.1 Supported: Claude Desktop on 3P + a gateway

This is the desktop form of `LLM_MIDDLEWARE_RESEARCH.md` §1.4 (Messages-API gateway) and
the only route where **both** halves of Maskroom — pseudonymize the request, restore the
streamed response — can be transparent and enforced for desktop Chat:

- Anthropic's own gateway: "Claude apps gateway is a self-hosted service that sits between
  your developers' Claude Code clients and your model provider"; Claude Desktop "connects to
  the same gateway through a different MDM key: set `bootstrapUrl` … and opt the user's policy
  in with a `desktop` key"; "Once connected, Claude Desktop sends model requests from every
  enabled tab through the gateway. It shows the Cowork and Code tabs by default. To turn on
  the Chat tab as well, set `chatTabEnabled` to `true`" ([Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway)).
  Constraints: Linux server only, OIDC only, private-network address required, Claude Code
  v2.1.203+ on the gateway; developers "don't need a claude.ai account, an API key, or a
  subscription, because requests to the model go through the gateway using the
  organization's upstream credential"; usage "is billed to your organization's provider
  account at API rates" ([Run Claude Code through a gateway](https://code.claude.com/docs/en/gateways)).
- Your own gateway: `inferenceProvider: gateway` + `inferenceGatewayBaseUrl` in 3P mode
  ([Configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration));
  "Anthropic doesn't endorse, maintain, or audit other gateway products" ([Gateways](https://code.claude.com/docs/en/gateways)).

What a gateway may do with the body (Anthropic's compatibility guide, verified 2026-09-23):
"**Forward unchanged**: pass it to the upstream byte-for-byte … Anything not marked forward
unchanged is yours to consume or ignore"; "pass `anthropic-*` request headers and request
body fields through unchanged rather than allowlisting the ones you see today"; "A gateway
that rewrites or redacts request bodies for content inspection breaks the pairing the same
way stripping does, so inspect without modifying"; "Forward `cache_control` unchanged
wherever it appears, and don't convert block-form `system` or message content to plain
strings"; the preserved-thinking check "fails when `system`, `tools`, or earlier `messages`
content differs from the request that produced the thinking. A gateway that rewrites any of
that content can cause the rejection itself"; "forward error response bodies unmodified"
([Claude Code gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol)).

Reading for Maskroom: rewriting **only the text of `content` blocks** (leaving every field,
header, `system` order, `cache_control` marker and tool schema intact, and rewriting each
turn *identically* on every request so preserved thinking still matches) is technically
compatible with these rules, but it is explicitly outside what Anthropic supports, and the
deterministic per-session vault is what makes the "identical on every request" property
hold. The existing §1.4 design (streaming un-tokenization, vault keyed by conversation)
applies unchanged; Claude Desktop just becomes another client of it.

Where it does not apply: standard-mode Claude Desktop, claude.ai in a browser, Cowork cloud
sessions ("run on Anthropic-managed infrastructure", middleware doc §1.2). And it moves
users off claude.ai subscriptions onto API/cloud billing, which is a commercial decision.

### 4.2 Not supported: TLS interception of claude.ai traffic

An enterprise TLS-inspection proxy with its root in the OS store will see the standard-mode
Chat tab's requests (§1.4). Rewriting them is a bad idea for four documented reasons:
(1) the format is claude.ai's private web API, not the Messages API — nothing documents it,
and it changes with the web app; (2) the Consumer Terms forbid decompiling/reverse
engineering the Services and automated access (§7); (3) Anthropic's only DLP integration
for claude.ai is Inference hooks, which are allow/deny only ("Rewriting or redacting a prompt
is not supported", middleware doc §1.6); (4) responses are streamed and rendered by the
page — a proxy rewrite of tokens would change what Anthropic stores, which is the opposite
of what the extension's *on-screen-only* unmask does. Detection/blocking at the proxy is a
legitimate DLP posture; transformation is not.

---

## 5. Delivery mechanisms that need no app-specific integration

### 5.1 Clipboard / global-hotkey helper (works in any app today)

Flow: user selects text (or focuses the composer) → `Ctrl/Cmd+Shift+M` → helper copies the
selection → `POST /api/mask` `{text, session_id}` → helper pastes `masked` back. Reverse
hotkey: copy a reply → `POST /api/unmask` → paste restored text into a note. Files: drop
onto the helper → `/api/process` → open the `*_masked` file for attaching.

| Piece | macOS | Windows |
|---|---|---|
| Global hotkey | Electron `globalShortcut`: "The shortcut is global; it will work even if the app does not have the keyboard focus"; only media keys need "trusted accessibility client" ([globalShortcut](https://www.electronjs.org/docs/latest/api/global-shortcut)). Tauri: `register('CommandOrControl+Shift+C', …)` ([global-shortcut plugin](https://v2.tauri.app/plugin/global-shortcut/)) | `RegisterHotKey` "Defines a system-wide hot key" and posts `WM_HOTKEY` ([RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey)); same Electron/Tauri APIs |
| Read/write clipboard | `NSPasteboard` "An object that transfers data to and from the pasteboard server … shared by all running apps"; **new:** `accessBehavior` — "The current pasteboard access behavior. The user can customize this behavior per-app in System Settings for any app that has triggered a pasteboard access alert in the past"; `detectedPatterns(for:)` lets you inspect "without notifying the person using the app" ([NSPasteboard](https://developer.apple.com/documentation/appkit/nspasteboard)). Apple's AppKit updates page: "Prepare your app for an upcoming feature in macOS that alerts a person using a device when your app programmatically reads the general pasteboard. The system shows the alert only if the pasteboard access wasn't a result of someone's input on a UI element that the system considers paste-related" ([AppKit updates](https://developer.apple.com/documentation/updates/appkit)) | `OpenClipboard` / `GetClipboardData` / `SetClipboardData`, `AddClipboardFormatListener` → `WM_CLIPBOARDUPDATE` ([Using the Clipboard](https://learn.microsoft.com/en-us/windows/win32/dataxchg/using-the-clipboard)); no permission |
| Simulate Cmd/Ctrl+C and +V | `CGEvent.post(tap:)` "Posts a Quartz event into the event stream at a specified location" ([CGEvent.post](https://developer.apple.com/documentation/coregraphics/cgevent/post(tap:))). Apple's API page states no permission, but event taps "receive key up and key down events if … The current process is running as the root user [or] Access for assistive devices is enabled" ([CGEvent.tapCreate](https://developer.apple.com/documentation/coregraphics/cgevent/tapcreate(tap:place:options:eventsofinterest:callback:userinfo:))); in practice posting synthetic keystrokes to other apps is gated by the same TCC **Accessibility** grant (pynput: "Recent versions of macOS restrict monitoring of the keyboard for security reasons … Your application must be white listed under Enable access for assistive devices" — [pynput limitations](https://pynput.readthedocs.io/en/latest/limitations.html)); Apple's user guide for Input Monitoring: apps may "monitor your keyboard, mouse, or trackpad even when you're using other apps" ([Control access to input monitoring](https://support.apple.com/guide/mac-help/control-access-to-input-monitoring-on-mac-mchl4cedafb6/mac)) | `SendInput` "Synthesizes keystrokes, mouse motions, and button clicks" and "inserts the events … serially into the keyboard or mouse input stream"; UIPI applies ([SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)) |
| Guard (intercept Enter) | An active `CGEventTap` can swallow Return when the focused app is Claude Desktop (needs Accessibility/Input Monitoring; "Only processes running as the root user may locate an event tap at the point where HID events enter the window server", session taps are fine) | `SetWindowsHookEx(WH_KEYBOARD_LL)` "monitors low-level keyboard input events", "Global only"; "Global hooks are a shared resource … Global hooks should be restricted to special-purpose applications" ([SetWindowsHookEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw)) |

Honest UX: it is manual per message; without the guard, a forgotten hotkey sends raw PII;
with the guard, the helper is a keylogger-shaped process that must be scoped to Claude
Desktop's window and audited like Grammarly does (§2.4). There is no unmask of replies on
screen; replies are unmasked only when copied or downloaded. Same-session tokens still
resolve because `/api/mask` keys the vault by `session_id` — the helper needs a "current
session" concept mirroring the extension's per-chat session, but it cannot know which chat
is open, so sessions are per-helper or user-selected.

### 5.2 Input method / text service (not recommended)

- macOS Input Method Kit: "provides a streamlined programming interface that lets you
  develop input methods"; `IMKServer` "Manages client connections to your input method";
  `IMKInputController` — "For every input session there is a corresponding
  `IMKInputController` object" ([InputMethodKit](https://developer.apple.com/documentation/inputmethodkit)).
- Windows Text Services Framework: "provides a simple and scalable framework for the
  delivery of advanced text input and natural language technologies … A TSF text service
  provides multilingual support and delivers text services such as keyboard processors,
  handwriting recognition, and speech recognition"; "designed for use by Component Object
  Model (COM) programmers using the C/C++ programming languages"
  ([Text Services Framework](https://learn.microsoft.com/en-us/windows/win32/tsf/text-services-framework)).

An input method sees keystrokes as they are composed and could replace what is committed,
but PII detection needs the whole message (names span words, NICs are entered as one run
but addresses are not), the user must select our IME as their keyboard, and a Sinhala/Tamil
user already uses a real IME. It is the wrong layer: overkill with worse UX than §5.1.

### 5.3 Overlay / floating "Mask" button via accessibility (the Grammarly shape)

Helper watches focus (`kAXFocusedUIElementChangedNotification` / UIA focus-changed events),
recognises Claude Desktop's composer (owning app bundle `com.anthropic.claudefordesktop`,
role text area/document, `contenteditable`), enables Chromium accessibility (§3.3), reads
the text (`kAXValueAttribute` / TextPattern `DocumentRange.GetText`), and draws a small
always-on-top window at the field's bounding rectangle with **Mask** and the finding count
(the same bar `content.js` draws in the page, moved into a native window). On click it
writes back through `AXValue`/`AXSelectedText`/`SetValue` if settable, else through
select-all + paste (§5.1). A guard is the §5.1 key hook plus "did we already mask this exact
text" (`lastMasked` logic in `content.js`). File masking stays a drop target on the same
window. This is exactly what Grammarly ships on both platforms with one permission on macOS
and none on Windows, so it is **platform-supported and durable** as long as Claude Desktop
keeps a standard `contenteditable`. It is **unsupported by Anthropic** in the same sense as
the extension. Still no on-screen unmask of replies.

### 5.4 Screen / overlay unmasking of replies — impractical

**Measured 2026-09-25** with `scripts/desktop/uia_reply_probe.py` on Claude Desktop
(Windows). The page is one `DocumentControl` with `TextPattern` (2.4k chars read in 53 ms).
Locating tokens for an overlay is the problem: `FindText` returned mis-aligned ranges on
this document (hits offset by two characters, some with no rectangles), and a full sweep of
a 5.9k-char reply with 22 tokens cost **0.6–1.4 s per pass**, far too slow to follow
scrolling or streaming. `GetVisibleRanges` took 547 ms. So painting real values over every
token on screen stays off the table. What *is* cheap and exact: `RangeFromPoint` under the
mouse took **1 ms** and, expanded to a word, returned the token text and its rectangle
precisely (`TOK_PERSON_2615D96E`, one 184×20 px rectangle); `ControlFromPoint` (6 ms) gave
the list item with the whole line. The helper therefore restores on hover (tooltip with the
line's real values) and on copy. It *also* paints values over tokens, but only by
walking the visible lines and positioning each token inside its own line: a first
attempt that located tokens by their offset from the start of the document measured
**400 ms per token and 30 s for one real chat** on Windows (2026-09-26), which starved
the send guard sharing that thread. Offsets into a whole document are not a usable
primitive here; per-line offsets and per-range rectangle refreshes are. See
`desktop/README.md`.

To restore tokens *visually* inside Claude Desktop you would: read the reply text and its
line rectangles from the accessibility tree (possible: `GetBoundingRectangles` on Windows,
`AXBoundsForRange` on macOS), then paint replacement text in an overlay window on top of the
app. Reasons it fails in practice: streamed replies re-flow continuously; tokens are inside
Markdown, code blocks, tables and artifacts (the `www.claudeusercontent.com` frame); every
scroll, resize or theme change invalidates the overlay; the overlay must exactly reproduce
font, size, colour and background to be readable, and any mismatch is worse than the token;
and the accessibility tree "may lag what's in the renderer process by a fraction of a
second" (§3.3). Screen-scraping instead of accessibility adds the Screen Recording
permission ("Some apps and websites can access and record the screen and audio on your
Mac" — [Control access to screen recording](https://support.apple.com/guide/mac-help/control-access-to-screen-recording-on-mac-mchld6aa7d23/mac))
and OCR errors. Verdict: demo-able, not shippable. The workable substitutes are (a) unmask
on copy (§5.1), (b) unmask on download through the helper's folder watcher calling
`/api/unmask-file`, and (c) the gateway (§4.1), where the restored text *is* what the app
renders.

### 5.5 Routing desktop users to the web app (policy)

There is no Anthropic key to disable the standard-mode Chat tab or the app (§1.3). What an
organisation can do:

1. Block or uninstall Claude Desktop with endpoint management (MDM app restrictions,
   AppLocker/Intune on Windows, Jamf restricted software on macOS) — outside Anthropic's
   documentation and outside this document's sources.
2. If the app must stay, cut its non-chat surfaces with the standard-mode keys
   (`secureVmFeaturesEnabled=false`, `isClaudeCodeForDesktopEnabled=false`,
   `disableDesktopLocalSessions=true`, `isLocalDevMcpEnabled=false`,
   `isDesktopExtensionEnabled=false`, `allowedWorkspaceFolders`), and `forceLoginOrgUUID` so
   only the managed org is usable.
3. Force-install and lock the extension in the managed browser per
   [`ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md) §A–D; its §C residual bypasses
   now include "the user opens Claude Desktop instead" unless step 1 is done.
4. Claude Enterprise **Inference hooks** remain the only server-side gate that covers
   "the desktop or mobile apps" too (middleware doc §1.6) — as a detector, not a masker.

### 5.6 Local MCP server / Cowork

Covered in `LLM_MIDDLEWARE_RESEARCH.md` §1.2 and §1.5: local MCP servers and `.mcpb`
extensions are "only available in Claude Desktop and Claude Code" and are tools the model
calls, useless for text the user has already typed. A Maskroom `.mcpb` (mask a file the
model fetches, or a `mask_text` tool the user asks for) is complementary to everything here,
not a substitute. Not re-researched.

### 5.7 Files

Nothing outside the app can intercept Claude Desktop's attach/drop flow. The non-app paths:

- **macOS Services**: "Services are features exported by your application for the benefit
  of other applications … Users access services through the Services menu that's found in
  every application's application menu" ([System Services introduction](https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/SysServices/introduction.html));
  declared in `Info.plist` under `NSServices` with `NSSendTypes` ("The data types that the
  service can read"), `NSReturnTypes` ("The data types that the service returns"),
  `NSMessage`, `NSMenuItem`, `NSKeyEquivalent`
  ([NSServices](https://developer.apple.com/documentation/bundleresources/information-property-list/nsservices)).
  A text service with send *and* return types replaces the selection in place — the most
  Apple-native "Mask selection" there is, and it needs **no** Accessibility permission. A file
  service (`NSSendFileTypes`) gives "Mask with Maskroom" on Finder selections.
- **Finder Sync** is for sync clients: "Unlike most extension points, Finder Sync doesn't add
  features to a host app. Instead, it lets you modify the behavior of the Finder itself …
  Use this framework when you need to synchronize the contents of a local folder with a
  remote data source" ([Finder Sync](https://developer.apple.com/documentation/findersync)) —
  badges and menus only for folders it registers; Services are the better fit.
- **Windows** static verbs: add a `shell\<verb>\command` subkey under the file type's ProgID
  or `HKEY_CLASSES_ROOT\*\shell` with `(Default) = "…\maskroom-helper.exe" --mask "%1"`; "For
  each verb subkey, create a command subkey with the default value set to the command line
  for activating the items"; cascading menus via `SubCommands`; no COM required
  ([Creating Shortcut Menu Handlers](https://learn.microsoft.com/en-us/windows/win32/shell/context-menu-handlers)).
- Helper window: a drop target calling `/api/process` (already what `background.js
  upload()` does) and, for Claude's outputs, a "Restore file" drop target calling
  `/api/unmask-file` and a Downloads-folder watcher that mirrors the extension's download
  intercept.

Excel/PDF/Word/PowerPoint/CSV handling is the server's job already; the helper only moves
bytes, exactly like `background.js`.

---

## 6. Implementation stack options for a cross-platform helper

| Stack | Global hotkey | Clipboard | Accessibility tree | Notes |
|---|---|---|---|---|
| **Electron** | `globalShortcut` (built in) | `clipboard` module: `readText`/`writeText`, "modeled after the W3C Clipboard API", renderer use deprecated in 40+ ([clipboard](https://www.electronjs.org/docs/latest/api/clipboard)) | none built in; native add-on needed. `node-mac-permissions` covers "Accessibility … Input Monitoring … Screen Capture" with `getAuthStatus` and `askForAccessibilityAccess()` ("Opens System Preferences at the Accessibility pane (no programmatic API available)") ([node-mac-permissions](https://github.com/codebytere/node-mac-permissions)) | Reuses `tokens.js` and the extension's `background.js` fetch code verbatim; heaviest runtime |
| **Tauri (Rust)** | `global-shortcut` plugin, Windows/Linux/macOS ([docs](https://v2.tauri.app/plugin/global-shortcut/)) | `clipboard-manager` plugin `readText`/`writeText` ([docs](https://v2.tauri.app/plugin/clipboard/)) | none built in; crates `accessibility` ("Bindings for macOS Accessibility services", modules `ui_element`, `attribute`, `action`) ([docs.rs](https://docs.rs/accessibility/latest/accessibility/)) and `uiautomation` ("a wrapper for windows uiautomation") ([uiautomation-rs](https://github.com/leexgone/uiautomation-rs)) | Small binary; JS UI can share `tokens.js`; Rust glue for AX/UIA |
| **Native (Swift + C#)** | Carbon/`NSEvent.addGlobalMonitor` / `RegisterHotKey` | `NSPasteboard` / Win32 clipboard | first-class: `AXUIElement` (§3.1), `System.Windows.Automation` or `IUIAutomation` (§3.2) | Two codebases; best fidelity for the overlay |
| **Python** | `pynput` (macOS: must be trusted for keyboard monitoring, see §5.1) | `pyobjc` AppKit / `pywin32` | `pyobjc-framework-ApplicationServices` wraps the HIServices/AX C API; `pywinauto` `backend="uia"` covers "WinForms, WPF, Store apps, Qt5, browsers", with the caveat "Chrome requires `--force-renderer-accessibility` cmd flag before starting" ([pywinauto getting started](https://pywinauto.readthedocs.io/en/latest/getting_started.html), [PyObjC framework wrappers](https://pyobjc.readthedocs.io/en/latest/notes/framework-wrappers.html)) | Fastest prototype (same language as the server); packaging/signing is the pain |

macOS shipping requirements for any of them: Developer ID signing, Hardened Runtime and
notarization — "Beginning in macOS 10.15, all software built after June 1, 2019, and
distributed with Developer ID must be notarized"; prerequisites include "Enable the Hardened
Runtime capability" and "a secure timestamp" ([Notarizing macOS software before distribution](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)).
A PPPC profile pre-granting Accessibility (as Grammarly documents) needs a stable bundle ID
and code requirement, i.e. a stable signing identity from day one. Windows needs an
Authenticode-signed installer to avoid SmartScreen; per-user install keeps it at the same
integrity level as Claude Desktop's MSIX.

---

## 7. Terms of service

Consumer Terms of Service, "Effective October 8, 2025", section 3 ("Use of our Services"),
verified 2026-09-23 ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)). The
prohibited-use list includes, verbatim:

- "To decompile, reverse engineer, disassemble, or otherwise reduce our Services to
  human-readable form, except when these restrictions are prohibited by applicable law."
- "To crawl, scrape, or otherwise harvest data or information from our Services other than
  as permitted under these Terms."
- "Except when you are accessing our Services via an Anthropic API Key or where we otherwise
  explicitly permit it, to access the Services through automated or non-human means, whether
  through a bot, script, or otherwise."
- "To engage in any other conduct that restricts or inhibits any person from using or
  enjoying our Services, or that we reasonably believe exposes us … to any liability,
  damages, or detriment of any type, including reputational harms."

There is no separate "modify the app" clause (VERIFIED absence); patching the desktop
bundle would fall under decompiling/reverse engineering and, practically, is blocked by
§1.1 anyway. The Usage Policy (effective September 15, 2025) prohibits "Intentionally bypass
capabilities, restrictions, or guardrails established within our products for the purposes
of instructing the model to produce harmful outputs (e.g., jailbreaking or prompt injection)
without prior authorization from Anthropic" ([Usage Policy](https://www.anthropic.com/legal/aup))
— masking is not that. Assessment per option:

- A clipboard/overlay helper that only rewrites text a human is about to send, on a human
  keypress, is the same posture as the extension (README "Terms and risk"): a judgement call
  for the organisation, not a technical block. Inspecting the app's private API from a proxy
  is closer to "reverse engineer" and "automated means" and should be avoided.
- 3P mode: "Developers … don't need a claude.ai account" and usage is billed to the
  organisation's provider account; the Commercial Terms govern, and gateways are a
  documented, if "inspect without modifying", pattern.

Grammarly's terms are irrelevant here beyond the mechanism and were not researched.

---

## 8. Recommendation

### 8.1 Comparison matrix

| Option | Works in Claude Desktop? | Mask before send | Unmask on display | Files | Supported / fragile | Effort |
|---|---|---|---|---|---|---|
| A. Run the Chrome extension / inject into the app | **No** — fuses, asar integrity, signing (§1.1) | — | — | — | Unsupported and technically blocked | — |
| B. Clipboard + global hotkey helper (§5.1) | Yes, and every other app | Manual per message; optional key-hook guard | Only on copy/download | Drop target → `/api/process`; watcher → `/api/unmask-file` | Platform-supported APIs; Anthropic-unsupported; macOS needs Accessibility only for synthetic keys and may trigger the pasteboard alert | Small (days): reuse `background.js` fetch/upload logic and `tokens.js` |
| C. Accessibility overlay helper, Grammarly-style (§5.3) | Yes (Chromium exposes the tree on demand; **Windows write verified §3.4**, macOS untested) | Yes, in place; guard via key hook | No | Same drop target | Platform-supported (one TCC grant on macOS, none on Windows); Anthropic-unsupported; depends on Claude Desktop keeping a standard `contenteditable` | Medium (weeks): AX/UIA glue, overlay window, write-back tests |
| D. Overlay/screen unmask of replies (§5.4) | Technically | — | Partial, fragile | — | Impractical | Large; not recommended |
| E. Claude Desktop on 3P + Maskroom gateway (§4.1) | **Yes, Chat/Cowork/Code tabs**, transparent and enforced | Yes | **Yes** (the app renders the restored stream) | Text of attachments as they appear in `content` blocks; raw file bytes not seen | Gateway pattern is documented; body rewriting is "inspect without modifying" territory; requires 3P deployment, API/cloud billing, Linux gateway | Medium–large; same work as the §1.4 gateway, plus MDM rollout |
| F. Block Desktop, force managed browser + extension (§5.5) | N/A (removes the app) | Yes (extension) | Yes (extension) | Yes (extension) | Endpoint-management job; no Anthropic key for it; extension itself remains unsupported | Small if MDM exists |
| G. Inference hooks (middleware §1.6) | Yes (all apps) | Deny only | No | Text of attachments | Officially supported, Enterprise beta | Small server; needs Enterprise |
| H. Local MCP `.mcpb` (middleware §1.5) | Yes | Only when the model calls it | No | Yes, for files the tool fetches | Supported product surface; opt-in | Small |
| I. Input method / TSF (§5.2) | Yes | Per keystroke, wrong granularity | No | No | Supported APIs, wrong layer | Large; not recommended |

### 8.2 Recommended order of work

1. **Policy first, this week.** Decide per organisation: either Claude Desktop is blocked at
   the endpoint (F) and `ENTERPRISE_ENFORCEMENT.md` applies, or it is allowed with Cowork,
   Code, local MCP and extensions switched off by the standard-mode keys (§5.5 step 2) while
   B/C are built. Document that no Anthropic key disables the Chat tab in standard mode.
2. **Ship B — the tray/menu-bar helper.** Same server, same endpoints: `POST /api/mask`,
   `POST /api/unmask`, `POST /api/process`, `POST /api/unmask-file`, `GET /api/me` (sign-on
   via the server's `/auth/login` in a system browser, as `background.js` does with
   `launchWebAuthFlow`). Bundle `extension/tokens.js` unchanged for client-side restore of
   copied text (it is already a UMD module). Add the file drop target, the `*_masked` output
   and the Downloads watcher. Keep sessions explicit ("current session" in the tray menu,
   the staging page's session id copy, as the extension's *use session id…* does). Audit
   with `user`, `ip`, `kind` exactly as the extension's calls are audited today. Start in
   Python or Tauri; sign and notarize from the first build so the bundle ID/code requirement
   never changes.
3. **Prototype the C write-back before committing to it.** (b) Windows is **done**
   (2026-09-25, `scripts/desktop/uia_composer_probe.py`, §3.4): `ValuePattern.SetValue` on the
   composer is the in-place write path, with select-all + paste as the proven fallback.
   The first prototype built on it is `desktop/helper.py` (bar above the composer, Mask
   button and hotkey, clipboard unmask; no guard or files yet).
   (a) macOS is still open — set `AXManualAccessibility` on the app element, read the
   composer's `AXValue`, check `AXUIElementIsAttributeSettable` for `AXValue` and
   `AXSelectedText`, try both writes, watch what ProseMirror keeps. If the macOS write does
   not stick, C's write path there is select-all + paste. Either way, add the key-hook
   guard only after the overlay works.
4. **If the organisation moves to Claude Desktop on 3P (or already runs Claude apps gateway),
   put Maskroom on the gateway path (E).** This is the §1.4/§3.1 gateway from the middleware
   doc; nothing desktop-specific beyond MDM keys (`inferenceProvider`, `inferenceGatewayBaseUrl`
   or `bootstrapUrl`, `chatTabEnabled=true`) and streaming un-tokenization that never touches
   fields other than text content. It is the only option in this table with unmask on
   display and enforcement, so it should be the target architecture for managed fleets that
   can accept API billing.
5. **Keep G (Inference hooks) as the server-side detector** for Enterprise customers,
   covering the desktop app whatever client-side option is deployed.

Do not build D or I.

---

## 9. What is not possible (as of 2026-09-23)

- **Running the Maskroom extension, or any of our code, inside Claude Desktop.** Electron
  does not support Chrome Web Store extensions; Claude Desktop only calls `loadExtension` for
  React DevTools in a developer profile; `runAsNode`, `nodeOptions` and `nodeCliInspect` are
  fused off and asar integrity is fused on (Linux build verified; macOS/Windows assumed).
- **Disabling the Chat tab or the desktop app through Anthropic configuration in standard
  mode.** `chatTabEnabled` exists only for 3P deployments; no admin-console setting disables
  the desktop app.
- **A gateway or proxy hook for standard-mode Chat.** The Chat tab is claude.ai web content
  talking to Anthropic; only 3P mode routes it through a gateway.
- **Supported body rewriting at a gateway.** Anthropic's guidance is "inspect without
  modifying"; rewriting content text is tolerated only insofar as it keeps every other field
  and each turn's content identical across requests.
- **Unmasking Claude's replies on screen inside Claude Desktop** without injecting into the
  renderer; overlays are demo-grade only.
- **Intercepting Claude Desktop's file attach/drop.** Files must be masked before the user
  attaches them (helper, Services/shell verb, web UI).
- **Silent, permission-free operation on macOS** for anything that simulates keys or, soon,
  reads the pasteboard outside a paste-like action; Accessibility trust (and possibly the
  pasteboard alert) is unavoidable, exactly as for Grammarly.
- **Terms clarity.** Anthropic has not "explicitly permit[ted]" client-side rewriting tools
  for consumer surfaces; the automated-access clause is the same open question as for the
  extension.

---

## Sources

Anthropic / Claude — support and product documentation (verified 2026-09-23)
- https://support.claude.com/en/articles/10065433-install-claude-desktop
- https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop
- https://support.claude.com/en/articles/12611117-deploy-claude-desktop-for-macos
- https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows
- https://support.claude.com/en/articles/13455879-use-claude-cowork-on-team-and-enterprise-plans
- https://support.claude.com/en/articles/12592343-enabling-and-using-the-desktop-extension-allowlist (seen via search snippet; UNVERIFIED verbatim)
- https://www.anthropic.com/engineering/desktop-extensions ("We ship Node.js with Claude Desktop")
- https://raw.githubusercontent.com/anthropics/mcpb/main/README.md
- Claude Desktop Linux package: https://downloads.claude.ai/claude-desktop/apt/stable (Packages index and `claude-desktop_1.17377.1_amd64.deb`, inspected locally)

Anthropic — Claude Desktop on 3P documentation (claude.com/docs)
- https://claude.com/docs/third-party/claude-desktop/configuration
- https://claude.com/docs/third-party/claude-desktop/network-proxy
- https://claude.com/docs/third-party/claude-desktop/mdm

Anthropic — Claude Code / gateway documentation (code.claude.com)
- https://code.claude.com/docs/en/desktop
- https://code.claude.com/docs/en/claude-apps-gateway
- https://code.claude.com/docs/en/claude-apps-gateway-config (fetched; its "Claude Desktop overlay" section did not surface in the automated extract — the overlay facts are taken from the claude-apps-gateway page instead)
- https://code.claude.com/docs/en/gateways
- https://code.claude.com/docs/en/llm-gateway-protocol
- https://code.claude.com/docs/en/network-config

Anthropic — terms and policies
- https://www.anthropic.com/legal/consumer-terms (Effective October 8, 2025)
- https://www.anthropic.com/legal/aup (Effective September 15, 2025)

Grammarly — support documentation
- https://support.grammarly.com/hc/en-us/articles/8341875702413-How-to-deploy-Grammarly-for-Mac
- https://support.grammarly.com/hc/en-us/articles/4422076438029-How-to-deploy-Grammarly-for-Windows
- https://support.grammarly.com/hc/en-us/articles/10139846131213-How-do-I-integrate-Grammarly-with-my-website-or-application
- https://support.grammarly.com/hc/en-us/articles/4412816078349-Grammarly-for-Windows-and-Grammarly-for-Mac-user-guide
- https://support.grammarly.com/hc/en-us/articles/4406998780813-How-to-choose-where-Grammarly-for-Windows-and-Mac-works
- https://support.grammarly.com/hc/en-us/articles/27454187430029-Manage-application-controls
- https://support.grammarly.com/hc/en-us/articles/4412835748877-What-are-the-system-requirements-for-Grammarly-for-Windows-and-Grammarly-for-Mac
- https://support.grammarly.com/hc/en-us/articles/115000090811-What-do-I-need-to-use-Grammarly
- https://support.grammarly.com/hc/en-us/articles/115000091592-Grammarly-s-browser-extension-user-guide
- https://support.grammarly.com/hc/en-us/articles/360003816032-Is-Grammarly-a-keylogger
- https://support.grammarly.com/hc/en-us/articles/360003835311-Does-Grammarly-s-product-read-everything-I-write
- https://support.grammarly.com/hc/en-us/articles/360041105652-Grammarly-with-other-apps-Slack-Skype-Apple-Mail-Discord-etc

Apple — developer documentation and user guides
- https://developer.apple.com/documentation/applicationservices/axuielement_h
- https://developer.apple.com/documentation/applicationservices/1459186-axisprocesstrustedwithoptions
- https://developer.apple.com/documentation/applicationservices/kaxvalueattribute
- https://developer.apple.com/documentation/applicationservices/kaxselectedtextattribute
- https://developer.apple.com/documentation/applicationservices/kaxfocuseduielementattribute
- https://developer.apple.com/documentation/applicationservices/kaxfocusedapplicationattribute
- https://developer.apple.com/documentation/applicationservices/kaxvaluechangednotification
- https://developer.apple.com/documentation/applicationservices/kaxfocuseduielementchangednotification
- https://developer.apple.com/documentation/applicationservices/kaxselectedtextchangednotification
- https://developer.apple.com/documentation/applicationservices/axattributeconstants_h
- https://developer.apple.com/documentation/coregraphics/cgevent/post(tap:)
- https://developer.apple.com/documentation/coregraphics/cgevent/tapcreate(tap:place:options:eventsofinterest:callback:userinfo:)
- https://developer.apple.com/documentation/coregraphics/quartz-event-services
- https://developer.apple.com/documentation/appkit/nspasteboard
- https://developer.apple.com/documentation/updates/appkit
- https://developer.apple.com/documentation/inputmethodkit
- https://developer.apple.com/documentation/findersync
- https://developer.apple.com/documentation/bundleresources/information-property-list/nsservices
- https://developer.apple.com/library/archive/documentation/Cocoa/Conceptual/SysServices/introduction.html
- https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution
- https://support.apple.com/guide/mac-help/allow-accessibility-apps-to-access-your-mac-mh43185/mac
- https://support.apple.com/guide/mac-help/control-access-to-input-monitoring-on-mac-mchl4cedafb6/mac
- https://support.apple.com/guide/mac-help/control-access-to-screen-recording-on-mac-mchld6aa7d23/mac

Microsoft — Win32 reference (Microsoft Learn)
- https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32
- https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-implementingtextandtextrange
- https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-implementingvalue
- https://learn.microsoft.com/en-us/windows/win32/api/uiautomationcore/nf-uiautomationcore-ivalueprovider-setvalue
- https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomation-addfocuschangedeventhandler
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw
- https://learn.microsoft.com/en-us/windows/win32/dataxchg/using-the-clipboard
- https://learn.microsoft.com/en-us/windows/win32/tsf/text-services-framework
- https://learn.microsoft.com/en-us/windows/win32/shell/context-menu-handlers

Electron / Chromium project documentation
- https://www.electronjs.org/docs/latest/tutorial/fuses
- https://raw.githubusercontent.com/electron/fuses/main/src/config.ts (`FuseV1Options` order)
- https://www.electronjs.org/docs/latest/api/environment-variables
- https://www.electronjs.org/docs/latest/api/extensions
- https://www.electronjs.org/docs/latest/api/session
- https://www.electronjs.org/docs/latest/tutorial/accessibility
- https://www.electronjs.org/docs/latest/api/app
- https://www.electronjs.org/docs/latest/api/global-shortcut
- https://www.electronjs.org/docs/latest/api/clipboard
- https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/overview.md
- https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/browser/uiautomation.md
- https://chromium.googlesource.com/chromium/src/+/main/docs/accessibility/browser/how_a11y_works.md
- https://www.chromium.org/developers/design-documents/accessibility/

Stack libraries (project documentation)
- https://v2.tauri.app/plugin/global-shortcut/ , https://v2.tauri.app/plugin/clipboard/
- https://docs.rs/accessibility/latest/accessibility/ , https://github.com/leexgone/uiautomation-rs
- https://pynput.readthedocs.io/en/latest/limitations.html
- https://pywinauto.readthedocs.io/en/latest/getting_started.html
- https://pyobjc.readthedocs.io/en/latest/notes/framework-wrappers.html
- https://github.com/codebytere/node-mac-permissions

Secondary (community; used only where marked)
- https://github.com/anthropics/claude-code/issues/43158 (Chat/Cowork work behind a Zscaler TLS proxy, Code tab did not)
- https://github.com/aaddrick/claude-desktop-debian/discussions/529 (app.asar / Electron packaging on other platforms)

Repository
- [`extension/README.md`](../extension/README.md), [`extension/content.js`](../extension/content.js) (`SEL`, guard, `lastMasked`), [`extension/background.js`](../extension/background.js) (`api`, `upload`, download intercept, `launchWebAuthFlow`), [`extension/tokens.js`](../extension/tokens.js) (UMD restore logic)
- [`webui/app.py`](../webui/app.py) routes `/api/mask`, `/api/unmask`, `/api/process`, `/api/unmask-file`, `/api/session/<id>/vault`, `/api/me`
- [`docs/LLM_MIDDLEWARE_RESEARCH.md`](LLM_MIDDLEWARE_RESEARCH.md) §1.2, §1.4, §1.5, §1.6; [`docs/ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md)

### Verification

- **Apple developer pages** are JavaScript-rendered and returned only a title to the
  automated fetcher. Every Apple developer citation above was verified 2026-09-23 through
  Apple's documentation data endpoint (`https://developer.apple.com/tutorials/data/documentation/<path>.json`)
  for the same path; `AXUIElementSetAttributeValue`, `AXObserverAddNotification` and
  `CGEventPost` (C names) returned 404 there, so their descriptions are taken from the
  `AXUIElement.h` header listing and from the Swift-named pages `CGEvent.post(tap:)` and
  `CGEvent.tapCreate(...)`, which did render. The System Services archive page returned HTTP
  300 at `…/SysServices/introduction/introduction.html` and rendered at
  `…/SysServices/introduction.html`.
- **`code.claude.com/docs/en/claude-apps-gateway-config`** rendered, but the automated
  extract reported no "Claude Desktop overlay" section; the desktop facts are cited from
  `claude-apps-gateway` (which links to that section) and from Claude Desktop's own
  configuration reference.
- **Claude Desktop internals** (Electron 42.5.1, Chrome 148.0.7778.271, fuse bytes
  `010011011`, claude.ai origin checks, `loadExtension` usage, `chatTabEnabled` scope) were
  read from `claude-desktop_1.17377.1_amd64.deb` downloaded from Anthropic's apt repository
  on 2026-09-23 and unpacked in the session scratchpad; nothing was executed. macOS and
  Windows builds were not inspected.
- **Grammarly's "Grammarly with other apps"** article is a stub that only points to the
  desktop apps; the mechanism statements come from the integration and deployment articles.
- **npmjs.com** (node-mac-permissions) and **pypi.org** (pyobjc-framework-ApplicationServices)
  refused or failed automated fetch; the GitHub README and the PyObjC docs were used instead.
- No Anthropic page was found that states Claude Desktop is Electron, that documents
  certificate pinning for the Chat tab, or that lets an admin disable the desktop app; those
  are reported as verified absences after searching support.claude.com, code.claude.com and
  claude.com/docs.
