# Production readiness — what stands between this and a fintech sale

*Compiled 2026-09-27 from a full review of the server (`webui/`, `maskroom/`, `deploy/`) and
the Windows desktop helper (`desktop/helper.py`), against the goal of selling SafePII to a
large regulated financial institution. Companion to [`HOSTING_SPEC.md`](HOSTING_SPEC.md),
[`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md), [`ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md)
and [`../ROADMAP.md`](../ROADMAP.md), which covers detection quality in more depth.*

**How to read this.** Every item has an id (`SEC-1`, `GUARD-2`…) so it can be cited in a
commit or a ticket. **Verified** means it was reproduced or the code path was read line by
line during this review; **reported** means it came out of the review unconfirmed and should
be checked before acting. Effort is a rough order of magnitude, not an estimate.

**The honest summary.** The architecture, the auth model, the decision records and the
hosting spec are unusually good for a product this size. Two vulnerabilities are live on
`safepii.hsenidmobile.com` today. The desktop helper's central promise — that unmasked
content cannot leave the machine — is broken on several paths, one of them recorded
happening twice in a real user log. Neither half is shippable to a bank yet, and nothing
found here is architectural: it is all finishable work.

---

## Priority order

P0 is live exposure or data at risk now. P1 blocks the customer's security review. P2 blocks
a fleet rollout. P3 is needed for contract signature and for scale.

Two of these cannot be closed by writing code here. **FOLDER-1** is a limit of what
Claude Desktop lets a standard deployment enforce, and is answered with the
customer's own endpoint controls; **PKG-2**'s certificate is a purchase with a lead
time. Both are listed anyway, because a reader asking "what is outstanding" needs
to see them.

| # | Id | Item | Category | Effort |
|---|---|---|---|---|
| 1 | SEC-1 | Reflected XSS on `/auth/signed-out` | Security | hours |
| 2 | SEC-2 | Path traversal in the run-download routes | Security | hours |
| 3 | DATA-1 | Real conversation data untracked in the repo | Data | minutes |
| 4 | OPS-1 | 94 commits exist only on one laptop | Operations | minutes |
| 5 | SEC-3 | Hard-coded token salt fallback | Security | hours |
| 6 | SEC-4 | Auth defaults to off and fails open on misconfiguration | Security | hours |
| 7 | ~~GUARD-1~~ | ~~File dialog fails open when its parts are not found~~ **done** | Fail-open | hours |
| 8 | ~~REL-1~~ | ~~Worker thread can die, leaving Enter swallowed forever~~ **done** | Reliability | hours |
| 9 | ~~DATA-2~~ | ~~Downloads watcher uploads every Office file~~ **done** | Data | 1 day |
| 10 | ~~DATA-3~~ | ~~Helper log records conversation text, no rotation~~ **done** | Data | 1 day |
| 11 | MASK-1 | PDF redaction reports success when it did not redact | Masking | 1 day |
| 11a | FOLDER-1 | A user can add their own file-reading MCP server | Folder | not closable here |
| 11b | PKG-6 | The deployed server predates the posture and update routes | Packaging | hours |
| 11c | FOLDER-2 | Spreadsheets and PDFs are refused rather than served | Folder | days |
| 12 | SEC-5 | Bundled Keycloak is a dev realm with seeded accounts | Security | 1 day |
| 13 | SEC-6 | Legacy shared API key bypasses single sign-on | Security | hours |
| 14 | ~~DATA-4~~ | ~~Overlay paints real PII into every screen capture~~ **done** | Data | hours |
| 15 | ~~DATA-5~~ | ~~Vaults survive sign-out in helper memory~~ **done** | Data | hours |
| 16 | SEC-7 | Loopback sign-in has no `state` parameter | Security | hours |
| 17 | GUARD-2 | Remaining fail-open paths in the helper | Fail-open | days |
| 18 | DATA-6 | Audit trail is a cleartext PII store | Data | days |
| 19 | DATA-7 | Run directories are never cleaned | Data | 1 day |
| 20 | DATA-8 | Right to erasure is not implementable | Data | days |
| 21 | REL-2 | One global engine lock serialises all analysis | Reliability | days |
| 22 | SEC-8 | No rate limiting or request size cap | Security | 1 day |
| 23 | REL-3 | Engine cache is unbounded and client-controlled | Reliability | hours |
| 24 | ~~PKG-1~~ | ~~No admin lock for the helper~~ **done** | Packaging | days |
| 25 | PKG-2 | No installer, no signing, no version, no auto-update — **build kit done, signing certificate outstanding** | Packaging | weeks |
| 26 | PKG-3 | Desktop users are invisible in the audit console | Packaging | days |
| 27 | PKG-4 | `dev-sync.ps1` must never ship | Packaging | minutes |
| 28 | OPS-2 | No health check, access log, metrics or CI | Operations | days |
| 29 | MASK-2 | No recall metric, known gaps excluded from assertions | Masking | days |
| 30 | CODE-1 | Three copies of the token rules (~~two product names~~ **done**) | Code health | days |
| 31 | DOC-* | Compliance artefacts | Compliance | weeks |

---

## Security (SEC)

### SEC-1 — Reflected XSS on `/auth/signed-out` — P0, verified
`webui/auth.py:418` interpolates into HTML with no escaping; `webui/auth.py:515` puts
`safe_next(request.args.get("next"))` inside an `href='…'`, and `safe_next`
(`webui/auth.py:312`) returns any string starting with `/` verbatim, so a quote escapes the
attribute. `/auth/` is in `OPEN_PREFIXES` (`webui/auth.py:54`), so this needs no login. The
same unescaped helper is reached from `webui/auth.py:439` with the provider's
`error_description`, a second likely injection point.
**Why it matters.** Script on that origin can add the `X-Requested-With` CSRF header itself
and drive the authenticated API as the victim, including vault export. A reflected XSS on
the login path of a PII service is an automatic fail in a bank's application security review.

### SEC-2 — Path traversal in the run-download routes — P0, verified
`webui/app.py:814` (`_run_file`) basenames `fname` correctly but only basenames `run_id`,
and `os.path.basename("..")` is `".."`. So `/api/download/../maskroom.db` resolves out of the
runs directory into the data directory. Reported as returning the whole SQLite database:
vaults, audit originals, users and key hashes. Exposure is worst in the default auth mode,
where `owned()` returns true unconditionally (`webui/auth.py:297`). `run_id` is never
validated as a 12-character hex id.

### SEC-3 — Hard-coded token salt fallback — P0, verified
`maskroom/engine.py:89` and `maskroom/store/sessions.py:146` both fall back to a literal salt
published in this repository. Tokens are `SHA-256(value + salt)`.
**Why it matters.** If the environment variable is ever missing, anyone with the source can
brute-force names, NICs and phone numbers out of masked output offline, with no vault. It
turns pseudonymised output back into personal data. `docs/TECHNICAL_DESIGN.md:271` already
recommends HMAC with a managed key. Make the variable mandatory with no fallback first, then
move to HMAC.

### SEC-4 — Auth defaults to off and fails open — P1, verified
`webui/auth.py:238` defaults `MASKROOM_AUTH_MODE` to `off`. In that mode `_legacy_gate` only
gates `/api/*` if `MASKROOM_API_KEY` happens to be set, and `owned()` short-circuits to true.
Production does set `oidc` (`deploy/aws/docker-compose.yml:41`), so a single missing variable
silently downgrades the product to an unauthenticated PII service with no refusal to boot.

### SEC-5 — Bundled Keycloak is a development realm — P1, reported
`deploy/aws/keycloak/realm-maskroom.json` has `sslRequired: none`, no brute-force protection,
no password policy, no MFA, and two permanent seeded accounts. Keycloak runs `start-dev`
(`deploy/aws/docker-compose.yml:71`), acknowledged at `docs/DEPLOY_AWS.md:146`.

### SEC-6 — Legacy shared API key bypasses sign-on — P1, verified
`webui/auth.py` accepts `X-API-Key` regardless of mode and returns a `staff` principal named
`legacy-api-key`. No expiry, no rotation, no per-person attribution. Still wired into the
production environment file.

### SEC-7 — Loopback sign-in has no `state` — P1, reported
`SignIn.run` (`desktop/helper.py:1774`) accepts any GET on any path and takes `code` from the
query, with no nonce generated or verified. RFC 8252 section 8.9 requires it. A local process
or a web page the user visits can inject an authorization code once the port is guessed, so
the user's PII is masked into an attacker's vault. Check against
[`adr/0003-native-client-sign-in.md`](adr/0003-native-client-sign-in.md), which should be
amended.

### SEC-8 — No rate limiting or size cap — P2, reported
No limiter in `webui/`, `maskroom/` or `deploy/aws/nginx/safepii.conf`. Nothing throttles
`/auth/login`, `/auth/exchange`, key verification or the expensive `/api/process`. Flask has
no `MAX_CONTENT_LENGTH`; the only bound is nginx's body size. Combined with REL-1 and REL-2,
a few slow uploads take the service down for everyone, and the clients fail closed, so a
denial of service on SafePII blocks the whole company from using Claude.

---

## Fail-open paths (GUARD)

The product's promise is that unmasked content cannot leave. These are the paths where it does.

### GUARD-1 — The file dialog fails open when its parts are not found — **DONE 2026-09-27**
`desktop/helper.py:1494` and `:1499` set `SHARED["dialog_open"] = False` when the File name
box or the Open button cannot be found, which stops the hook intercepting, so the original
file is attached. `desktop/helper.py:1546` does the same at the second entry point, logging
"letting the dialog through". **Observed twice in a real user log.** The correct behaviour is
the one already used for an unlocatable pick at `desktop/helper.py:1553`: hold, and say so.
**This was the single most important item in this document.**
**Fixed:** a missing part no longer disarms the guard. A dialog with no Open button is a
Save dialog and is left alone; an open-type dialog whose File name box cannot be read stays
armed and *holds* the confirm with a message pointing at copy-paste. Covered by four tests
in `tests/test_desktop_overlay.py`.

### GUARD-2 — The remaining fail-open paths — P1/P2, reported
1. `desktop/helper.py:1707` — `read_text` returns `""` when both reads throw, and the guard
   cannot tell an empty composer from a failed read, so it replays Enter.
2. `desktop/helper.py:1702` — `current_composer()` returning `None` after any accessibility
   hiccup replays Enter unmasked.
3. ~~`_exe_cache` never expires and Windows recycles process ids~~ **DONE 2026-09-28.**
   Reported in the field as the bar appearing over a browser: a process id the helper had
   cached as Claude had changed hands. The cache now expires after two seconds and is
   bounded, and `claude_in_front()`, which decides whether a key is swallowed, never uses a
   cached answer at all. Five tests cover it, including an id changing hands.
4. `desktop/helper.py:1927` — Ctrl+V is intercepted only if the clipboard read succeeds
   inside the hook; a clipboard lock returns nothing and the files paste through.
5. `desktop/helper.py:1936` — Ctrl+Enter is excluded from the guard. Worth five minutes of
   testing whether Claude Desktop treats it as send.
6. The Send button is not guarded at all, only the keyboard. An evaluator will click it.
7. `desktop/helper.py:1096` — types outside `MASK_EXTS` are attached as-is with a toast that
   vanishes after six seconds. The list omits `.md .html .xml .yaml .log .sql .eml .rtf`,
   and `RESTORE_EXTS` includes several of those, so the helper will restore a Markdown file
   it would not mask.
8. `desktop/helper.py:1645` — drop blocking depends on the mouse hook having installed;
   failure is a log line the user never sees.

### GUARD-3 — Loose token matching can restore the wrong person's value — P2, reported
`desktop/helper.py:369` accepts a six-character prefix match in either direction across the
union of up to 25 vaults. The single-hit rule reduces but does not remove the collision.
The Python port also drops the `unresolved` report that `extension/tokens.js:51` produces,
so a partial restore is silent.

---

## Data protection (DATA)

### DATA-1 — Real conversation data untracked in the repo — P0, verified
`desktop/helper.log` (17 MB, 186,902 lines of conversation text) and four screenshots of real
chats sit untracked in `desktop/`, and `*.log` and `*.PNG` are not in `.gitignore`. One
`git add -A` publishes patient and transaction details.

### DATA-2 — Downloads watcher uploads every Office file — **DONE 2026-09-27**
`desktop/helper.py:1239` returns true unconditionally for zipped Office formats, and
`poll_downloads` watches the whole Downloads folder with no filter on origin. A bank
statement or customer list saved from email is read and posted to the server within seconds.
`extension/background.js:183` refuses anything not from Claude; the desktop has no referrer
to check, so "we cannot tell, so we upload everything" is the wrong default.
**Fixed:** `holds_tokens` answers true, false or *unknown*. Visible tokens are restored as
before, a file with certainly none is ignored, and a zipped Office file asks the user first,
with "always" and "never" remembered (`restoreUnreadable`).
**Why it matters.** This is an unannounced egress channel from the endpoint. A third-party
risk review stops the rollout here.

### DATA-3 — Helper log records conversation text — **DONE 2026-09-27**
`overlayDebug` makes the walk write every walked line to `%APPDATA%\Maskroom\helper.log`.
The log always records chat titles and URLs, file names and full folder paths, and
`dump_dialog` writes the name and value of every control in a file dialog. No rotation, no
size cap, no retention, no redaction. `claude_in_front()` also logs the foreground executable
on every Enter pressed anywhere in Windows, which is an application-usage trail written by a
tool not presented as monitoring software.
**Fixed:** `redact()` reports the shape of a value rather than the value, and every log call
that touched conversation text, a chat title, a file name, a folder path or a dialog control
goes through it. The log rotates at 2 MB keeping one previous file. `save_config` also became
atomic. Still open: the foreground-executable line on every Enter, which is `GUARD-2` work.

### DATA-4 — Overlay paints real PII into screen captures — **DONE 2026-09-27**
The overlay window does not set `WDA_EXCLUDEFROMCAPTURE`, so restored values appear in Teams
and Zoom shares, the Snipping Tool, window thumbnails and any screen-recording agent.
**Fixed:** both the overlay and the hover tooltip are excluded from capture, and a failure to
apply that is logged rather than passing silently.

### DATA-5 — Vaults survive sign-out — **DONE 2026-09-27**
`cmd_sign_out` (`desktop/helper.py:991`) clears the token and nothing else. Hover, clipboard
restore and the overlay keep revealing real values from all cached sessions until the process
is killed. Nothing clears them on idle or on workstation lock either.
**Fixed:** `forget_vaults()` drops every cached mapping and the published index, on sign-out,
on workstation lock, and after `forgetAfterIdleMinutes` of no input (default 15).

### DATA-6 — The audit trail is a cleartext PII store — P1, verified
`maskroom/store/audit.py:67` writes `input_text` (the raw original) and `output_text` into
plain `Text` columns, and file operations copy the original uploaded file to disk. Called on
every mask and unmask. Protection is retention only. Any auditor-role user can read every
other user's originals, and reads of audit records are not themselves audited.

### DATA-7 — Run directories are never cleaned — P1, verified
`webui/app.py` writes the original upload, `vault.json` (the plaintext re-identification key)
and `result.json` (which contains the detected raw strings) into a run directory.
`RunStore.sweep()` exists and **nothing calls it**, and nothing deletes the directories.
Every file anyone has ever masked, plus its re-identification key, accumulates forever,
outliving the 90-day retention the customer would be quoted.

### DATA-8 — Right to erasure is not implementable — P2, reported
There is no delete endpoint for an audit record, a session or a user's data. The audit search
filter does not cover `input_text`/`output_text`, so you cannot even find the records holding
a given person. A deletion clause in their contract cannot be honoured today.

### DATA-9 — Audit attribution and source IP are spoofable — P2, reported
With auth off the recorded user is whatever header the client sends, and `client_ip()` trusts
`X-Forwarded-For` with no trusted-proxy list. An audit trail whose actor can be set by the
caller is not evidential.

---

## Reliability (REL)

### REL-1 — The worker thread can die, leaving Enter swallowed — **DONE 2026-09-27**
`desktop/helper.py:654` calls `poll_focus()` outside the try block that guards every other
poll. One accessibility error ends the thread silently. The keyboard hook runs on a different
thread and keeps swallowing Enter, queueing commands nobody consumes, so the Enter key stops
working in Claude with no message and no recovery short of Task Manager. Decide deliberately
whether a dead worker should fail open and loud, or closed with a visible banner.
**Fixed:** the loop body is wrapped so nothing can end it, a supervisor restarts the thread
if it ever stops, and the worker publishes a heartbeat. The hook checks that heartbeat before
trusting the guard: a stale one raises a banner on the bar that stays until the worker
answers again, and `onGuardFailure` (default `hold`) decides whether keys are held or passed
through meanwhile. That choice is the customer's, because holding every Enter makes Claude
unusable and the user then kills the helper, which protects nobody.

### REL-2 — One global lock serialises all analysis — P2, reported
`maskroom/store/sessions.py:152` holds a process-wide lock around the whole request. With one
worker and four threads, nothing analyses concurrently. `docs/HOSTING_SPEC.md:44` already
prescribes splitting into a files service and a text service with `WEB_CONCURRENCY`, and
**neither compose file implements it**. The deployed topology does not match the sizing
document it was built against: one 17-second scanned PDF blocks all 200 users while the
clients fail closed.

### REL-3 — Engine cache unbounded and client-controlled — P2, reported
`webui/app.py:111` caches engines keyed on options taken straight from the request, with no
eviction. A user posting slightly different `min_score` values allocates a new engine per
request until the container is killed. `nlp_model` is also client-chosen and each value loads
about a gigabyte.

### REL-4 — Helper state grows without bound — P2, reported
`_exe_cache`, `downloads_done`, `masked_paths` and `seen_classes` never shrink; the events
queue is unbounded. `save_config` is called from three threads with a non-atomic write, and a
torn file silently reverts every setting to defaults, including the server URL and every chat
binding, with no error shown.

### REL-5 — A slow server freezes the Enter key, then replays a burst — P2, reported
Timeouts are 300 seconds for uploads and 60 for the API, all on the thread that services the
guard queue. Each Enter pressed meanwhile is swallowed and queued, then replayed when the
worker drains, into whatever window has focus at that moment.

### REL-6 — The drop blocker can stick over Claude — P2, reported
The invisible click-eating window is taken down only by a poll that stops running if the file
guard is switched off mid-drag. The bar that would switch it back on is inside the covered
rectangle. Needs an unconditional watchdog.

---

## Masking quality (MASK)

### MASK-1 — PDF redaction reports success when it did not redact — P1, reported
`maskroom/pdf.py:45` prints a warning and returns when OCR language data is missing, and the
page is not redacted, but the request completes with a success payload. The same holds for a
scanned page with a junk text layer. Nothing surfaces "this page was not analysed" to the
API, the UI or the audit record.
**Why it matters.** Telling a user a document is clean when it is not is the worst failure
this product can have. See also `ROADMAP.md` item 2, which proposes OCR confidence reporting.

### MASK-2 — No recall metric, and known gaps are excluded — P2, verified
`tests/test_excel_stress.py:14` excludes a known Sinhala ceiling from the assertion.
Nothing produces precision and recall per entity type, nothing tracks it over time, and
nothing gates a regression. The buyer will ask what your detection recall is for names, NIC
numbers and account numbers, and how you know it has not regressed.

### MASK-3 — Office parts and embedded images pass through — P2, reported
`maskroom/office.py` masks a fixed list of document parts and copies every other part byte
for byte, including embedded images and charts. A scanned identity card pasted into a Word
file is shipped unmasked and the run reports success. An entity split across two formatting
runs is also missed.

---

## Packaging and enterprise deployment (PKG)

### PKG-1 — No admin lock — **DONE 2026-09-28**
`desktop/helper.py:164` reads the machine-wide config **then lets the user's file override
it**, so it is a default, not a policy. Every toggle is a one-click button writing to a
user-writable file, and nothing records that the guard was turned off. The browser extension
already has this ([`ENTERPRISE_ENFORCEMENT.md`](ENTERPRISE_ENFORCEMENT.md),
`extension/managed_schema.json`). Needs policy keys under `HKLM` with an ADMX template, a
locked set the user file cannot override, disabled controls in the bar, and an audit event.
**This was the control the buyer's security team will ask for by name.**
**Fixed:** the helper reads `HKLM\SOFTWARE\Policies\SafePII\Helper` last, which only an
administrator can write. A locked setting shows a padlock, its toggle refuses with a message
naming the administrator, the settings window greys the field, and it is never written back
into the user's file. Ships with an ADMX/ADML template and a deployment README at
`enterprise/policies/windows/admx/`.

### PKG-2 — No installer, signing, version or auto-update — **build kit DONE 2026-09-28; certificate outstanding**
The install today is "install Python, pip install a package, run a script". There is no
version string anywhere in `desktop/helper.py`. An unsigned program that installs a global
keyboard hook is blocked by SmartScreen, quarantined by endpoint protection, and refused by
application allowlisting. **Done:** `desktop/packaging/` holds a PyInstaller spec (one directory, no UPX, `comtypes`
named as a hidden import), a generated Windows version resource, a WiX MSI that installs per
machine and starts per user through an HKLM Run value and takes `SERVERURL=` at install time,
a multi-size icon, and `build.ps1` tying it together. The server serves
`/desktop/latest.json` and `/desktop/SafePIIHelper.msi` in the same shape as the extension's
update route, choosing the newest by version number rather than file date; the helper checks
every six hours and reports, deliberately installing nothing.

**Outstanding, and it is a purchase, not code.** Since **1 June 2023** the CA/Browser Forum
has required the private key of every code signing certificate, OV as well as EV, to live in
FIPS 140-2 Level 2 or Common Criteria EAL4+ hardware, so a certificate file is no longer
issued. Either a hardware token or a cloud signing service (Azure Trusted Signing, DigiCert
KeyLocker, SSL.com eSigner) is needed; `build.ps1` calls whatever `SAFEPII_SIGN_COMMAND`
names, so the choice does not change the build. **Start the procurement now: it blocks
release and nothing else.**

**Not yet verified on Windows.** Nothing in `desktop/packaging/` has been run, because this
repository is developed on Linux. The first build on a Windows machine is the test.

**WiX licensing, found 2026-10-02:** an unpinned `dotnet tool install --global wix`
now installs v7, which will not build until its **Open Source Maintenance Fee**
EULA is accepted — a commercial licence for a product that is sold. The build is
pinned to WiX v5, which is the only version band that works at all: the `Files`
element that harvests the built folder arrived in v5, and v6 brought the fee. So
nothing is blocked, and the room to move is one major version wide. If the
toolchain is ever taken forward, that fee is a line item somebody has to own.

### PKG-6 — The deployed server predates this work — P1, verified 2026-10-02
`/api/event` (the helper reporting posture an administrator should see) and
`/desktop/latest.json` (update checks) exist in source and not on the image
running at `safepii.hsenidmobile.com`. Until it is rebuilt, an unmanaged MCP
server is reported only on the machine it happened on, and no helper can discover
that it is out of date. `docker-build.sh && docker-publish.sh`, then
[`DEPLOY_AWS.md`](DEPLOY_AWS.md) §6.

### PKG-7 — The update check had never run — **DONE 2026-10-02**
`update_checked` was initialised in `OverlayWorker.__init__` and read in
`Automation.poll_update`, which raised on every idle tick. Caught and logged, so
nothing looked broken and the helper never once asked the server what the current
build was. Found by a user's log filling up. `tests/test_source_hygiene.py` now
fails on an attribute read in one class and initialised only in another's
`__init__`, which is the shape of that mistake.

### PKG-3 — Desktop users are invisible in the audit console — P2, verified
The extension records intercept outcomes; the helper sends nothing to the server. The only
trail is the local log. A regulator asking you to prove masking was enabled for a given user
on a given date cannot be answered.

### PKG-4 — `dev-sync.ps1` must never ship — P2, verified
It fetches and executes `helper.py` over plain HTTP from a hard-coded address every two
seconds with no signature check, and `desktop/README.md` documents it as the workflow.
Exclude it from any customer artefact, and expect a penetration tester to find it in the repo.

### PKG-5 — Windows only — P3
No macOS build. The `AXValue` write-back experiment in
[`DESKTOP_APP_RESEARCH.md`](DESKTOP_APP_RESEARCH.md) section 8.2 is still open.

---

## Folder access (FOLDER)

The broker (`desktop/broker.py`) serves one folder to Claude Desktop with the
personal data replaced as it is read. Design and the options rejected:
[ADR 0009](adr/0009-folder-access-is-masked-by-being-the-only-way-in.md),
[`DESKTOP_APP_RESEARCH.md`](DESKTOP_APP_RESEARCH.md) §5.6. Deployment:
[`enterprise/WINDOWS-RUNBOOK.md`](../enterprise/WINDOWS-RUNBOOK.md).

### FOLDER-1 — A user can add their own MCP server — P1, verified on a machine
The protection half is enforced: `allowedWorkspaceFolders: []` forbids every
folder, and Claude's own request-a-folder tool with it ("denied without prompting
the user"). The *registration* half is not. `managedMcpServers` is third-party
only — tested 2026-10-02, both transports, both hives, while
`allowedWorkspaceFolders` on the same key was demonstrably in force — so on a
standard deployment the broker is registered through `claude_desktop_config.json`,
which needs `isLocalDevMcpEnabled` left on. That same key is what would otherwise
stop a user adding a filesystem MCP server of their own and reading raw files.
**Not closable from here.** The answers are application allowlisting (a user's
server has to execute something; ours is the signed MSI binary), an ACL on the
config file, and the helper reporting `unmanaged-mcp-server` to the audit trail.
Fully closed only in third-party mode.

### FOLDER-2 — Spreadsheets and PDFs are refused, not served — P2, verified
The broker serves prose through `/api/mask` and tables through `/api/process`,
and refuses `.xlsx .docx .pptx .pdf` because serving them means rendering the
masked output back as text. For a finance customer that is most of their data.
The server already masks all of them; what is missing is the text view.

### FOLDER-3 — Masked file names are best-effort — P2, verified
Measured against a live engine: `kyc/Nimal Perera - loan.csv` is not recognised
as prose, and a probe that works around the separator read the `md` in `notes.md`
as a surname. Names are masked per path component with a separator probe, to the
same standard as file contents, which are also best-effort. `--names handles`
removes the risk and costs the model the ability to tell one file from another,
which makes it read more of them (ADR 0009).

### FOLDER-4 — Nothing is masked for a Code session's shell — P2, reported
`mode: ro` is enforced at the OS level on Cowork's mount, but in Code sessions it
binds Claude's file tools only, not Bash or SSH. The runbook switches Code off.
A customer who needs Claude to execute against their data needs the synthetic
filesystem assessed in [`DESKTOP_APP_RESEARCH.md`](DESKTOP_APP_RESEARCH.md)
§5.6.2, which is deferred rather than rejected and is written up as a handover:
what is genuinely hard (a kernel-mode driver per platform), what is merely costly
(masking becomes eager, which caching on mtime bounds), and the afternoon's
experiment on a Linux host that settles the one unknown worth knowing before
anybody buys a driver.

### FOLDER-5 — Write-back is one-way — P3, reported
`write_file` restores tokens and writes beside the served folder, never into it.
There is no path for Claude to edit a file in place, and `mode: ro` means there
should not be one until somebody has decided what review looks like.

---

## Operations (OPS)

### OPS-1 — The work exists only on one laptop — P0, verified
`feat/desktop-helper` has no upstream and 94 commits are unpushed. A disk failure loses the
helper, the sign-on work and the deployment setup.

### OPS-2 — No health check, access log, metrics or CI — P2, reported
There is no `/healthz` or `/readyz`; the container healthcheck probes a static dictionary
that never touches the database, so it reports healthy while Postgres is down. Gunicorn runs
with no access log, and no Docker log limit, so the disk fills. No metrics, no alerting. No
pipeline of any kind, so the test suite is never run automatically and there is no dependency
scan, no static analysis, no image scan and no software bill of materials.

### OPS-3 — Migrations, rollback and backups — P2, reported
`maskroom/store/db.py:93` is two hand-written column additions with no ordering and no down
path, and no check that the recorded schema version is not newer than the running code, so a
release with a schema change cannot be rolled back. The backup is a manual one-liner with no
schedule, retention, encryption or restore drill, and it covers the database but not the
audit file copies and run directories on the bind mount.

### OPS-4 — Secrets are environment variables — P3, reported
The salt, the Flask secret, the identity client secret and the database password are all
visible in `docker inspect` and `/proc`. `docs/HOSTING_SPEC.md:74` says they belong in a
secret manager; no code path reads from one.

---

## Code health (CODE)

### CODE-1 — Duplication and naming — P2, verified (naming **DONE 2026-09-27**)
The token restore rules exist in three hand-maintained implementations: `maskroom/engine.py`,
`extension/tokens.js` and `desktop/helper.py`. They already differ, since the Python port
omits the unresolved report. `MASK_EXTS` has three copies and `RESTORE_EXTS` two. The product
also had two names. **Corrected:** the server pages and the extension already said SafePII to
users; what looked like a count of stray "Maskroom" strings in them is the `MaskroomAuth`
JavaScript identifier and the `MASKROOM_API_KEY` environment variable, neither of which a
user sees. Only the helper was wrong, and it now says SafePII in its bar, its windows, its
sign-in page and its folders, carrying settings over from the old folder on first run so an
upgrade does not sign anyone out. `X-Requested-With: maskroom` stays as it is: that is the
CSRF value the server checks, not a product name.

The three copies of the token rules are still outstanding.

### CODE-2 — The helper is one 2,925-line file — P3
Clean seams are already visible in the section banners: config and logging, the server API,
tokens, Win32, accessibility, sessions, the file guard, downloads, hooks, the overlay, the
user interface, sign-in. The file guard is about 470 lines and the highest-risk code in the
product; it deserves its own module and its own test file.

### CODE-3 — Testing gaps — P2, verified
`tests/test_desktop_overlay.py` covers the overlay, path resolution and the downloads
watcher. Untested: the guard state machine, which is the core promise; the file-guard
decision matrix, including both observed fail-opens; `TokenIndex`, which shares no vectors
with the JavaScript original; the hook decision logic, which is pure arithmetic and needs no
Windows; config precedence and corruption; session resolution; and the hand-rolled multipart
builder, which does not escape the filename.

---

## Compliance artefacts (DOC) — P3, none exist

A regulated buyer will ask for all of these in procurement.

| Id | Artefact |
|---|---|
| DOC-1 | Data protection impact assessment |
| DOC-2 | Records of processing, plus a data-flow diagram showing PII crossing to Anthropic |
| DOC-3 | Data processing agreement with a sub-processor list, and zero-retention terms with the LLM vendor |
| DOC-4 | Retention and deletion policy, reconciled with what the code does, and an erasure procedure |
| DOC-5 | Encryption-at-rest statement and key-management policy |
| DOC-6 | Independent penetration test report and remediation log |
| DOC-7 | SOC 2 Type II or ISO 27001, or a written control mapping with compensating controls |
| DOC-8 | Secure development evidence: CI, review policy, static analysis, dependency scanning, SBOM, vulnerability response SLA |
| DOC-9 | Detection accuracy card: per-entity precision and recall on a documented corpus, with limitations stated as a specification |
| DOC-10 | Incident response and breach notification plan |
| DOC-11 | Business continuity: stated RTO and RPO with a tested restore |
| DOC-12 | Access control policy, periodic recertification, service-key rotation |
| DOC-13 | Source-code escrow or exit plan, and liability insurance |

---

## What is already strong

Worth stating plainly, because it is what the sale leads with, and because none of it needs
rework.

- Sign-on runs the OpenID Connect flow on the server against any discovery-based provider,
  and issues a database-backed session that can be revoked centrally.
- Session tokens and service keys are stored as hashes, so a copy of the database forges
  neither ([`adr/0002-oidc-login-and-roles.md`](adr/0002-oidc-login-and-roles.md)).
- Sessions, vaults and file runs carry an owner and are served only to that principal.
- Roles are held in the product and changed by an administrator, not mapped from the
  provider's claims.
- The masking engine fails closed on an unsupported type and on a parse failure, re-masking
  is idempotent, and an unresolvable token is reported rather than guessed.
- The decision records, the hosting spec and the two research documents give a buyer's
  architect real answers, which most products this size cannot produce.
