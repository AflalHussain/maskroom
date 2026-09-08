# Research — maskroom as a middleware in front of Claude surfaces

*Researched 2026-09-08 against primary sources (Anthropic/Claude documentation and terms,
the MCP specification, Chrome extension docs) and this repository's code. Product surfaces
change quickly; re-verify the cited pages before acting on anything here.* Companion to
[`TECHNICAL_DESIGN.md`](TECHNICAL_DESIGN.md) and the "Reassessment for the cloud-LLM use
case" section of [`ROADMAP.md`](../ROADMAP.md).

**The question.** Can `maskroom` sit between a user and Claude — claude.ai, Claude Cowork,
Claude Code, or applications built on the Claude API — so that PII is pseudonymized before it
reaches Anthropic and restored in the answers that come back?

**The short answer.**

1. For **applications you build on the Claude API**, yes: a reverse proxy/gateway is the
   standard, supported pattern (`ANTHROPIC_BASE_URL`), and maskroom can rewrite request and
   response bodies there. This is the only place where *transparent, enforced* masking and
   unmasking both work.
2. For **Claude Code**, partly: hooks are official, but `UserPromptSubmit` can only *block*
   a prompt or add context — it "can't replace the prompt". Tool inputs (`PreToolUse`
   `updatedInput`) and tool results (`PostToolUse` `updatedToolOutput`) *can* be rewritten,
   and a gateway on `ANTHROPIC_BASE_URL` sees the full request. Anthropic's own guidance to
   gateway operators is "inspect without modifying".
3. For **claude.ai and Cowork**, no transparent interception exists outside enterprise
   controls. The only supported inline hook is Anthropic's **Inference hooks** (Claude
   Enterprise, beta): Anthropic POSTs each transcript to a server you run and waits for an
   `allow`/`deny` verdict — "Rewriting or redacting a prompt is not supported." maskroom can
   be that server as a *detector/gate*, not as a masker.
4. **MCP** cannot intercept prompts by design ("Servers should not be able to read the whole
   conversation"); a maskroom MCP server is an opt-in *tool* the model calls, useful when
   the PII enters through the tool (mask a file the server fetches), useless for text the
   user has already typed into the chat.
5. A **browser extension** for claude.ai is technically possible but unsupported, fragile,
   and sits against the consumer terms' automated-access clause.

Section 3 has the matrix and the recommended architectures.

---

## 1. Interception points per surface

### 1.1 claude.ai web app

**No pre-processing hook exists for user input or uploads.** The web app talks to Anthropic
directly; there is no documented developer hook that runs on the client before a message or
file is sent. What does exist:

| Mechanism | What it is | Interception value | Support status |
|---|---|---|---|
| File upload | Documents "PDF, DOCX, CSV, TXT, HTML, ODT, RTF, EPUB, JSON, XLSX", up to "500MB per file", "Up to 20 files per chat"; non-PDF documents are text-extracted ("Claude extracts text only from these files"); XLSX needs code execution enabled ([Upload files to Claude](https://support.claude.com/en/articles/8241126-upload-files-to-claude)) | None inline. Pre-masking a file *before* upload (today's maskroom workflow) is the only option | Supported workflow, user discipline required |
| Connectors / custom connectors (remote MCP) | "Custom connectors using remote MCP are available on Claude, Cowork, and Claude Desktop for users on Free, Pro, Max, Team, and Enterprise plans." "Claude connects to your remote MCP server from Anthropic's cloud infrastructure, rather than from your local device"; the server "must be reachable over the public internet from Anthropic's IP ranges". Team/Enterprise: "Only Owners can add them" ([Get started with custom connectors](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)) | Tool-call level only — see §1.5 | Supported; Anthropic warns "Custom connectors allow you to connect Claude to arbitrary services that have not been verified by Anthropic" |
| Claude in Chrome | A browser *agent* ("read, click, and navigate websites alongside you"), launched from the side panel, Cowork, or Claude Code; paid plans only ([Get started with Claude in Chrome](https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome)). "Claude in Chrome sends its chat traffic through your existing Claude endpoints (`claude.ai`, `api.anthropic.com`, `platform.claude.com`)"; ZDR "Not supported for Claude in Chrome, the same as Cowork" ([admin controls](https://support.claude.com/en/articles/13065128-claude-in-chrome-admin-controls)) | None — it is an additional *source* of data going to Anthropic (page text, screenshots), not an interception point. Admins get site allow/blocklists only | Supported product; no developer hook documented |
| Browser extension / userscript of our own | Technically feasible at the DOM/page-script level; **not** at the network level: under Manifest V3 "the `"webRequestBlocking"` permission is no longer available for most extensions … Policy installed extensions can continue to use `"webRequestBlocking"`", and even blocking `webRequest` can only alter headers, not the request body ([Chrome `webRequest` reference](https://developer.chrome.com/docs/extensions/reference/api/webRequest)). Content-script interception of the composer or monkey-patching `fetch` in the page context is the realistic route | Could mask text in the composer before submit and un-tokenize rendered replies. Cannot touch file uploads reliably (files are read by the page), cannot see connector/tool traffic, breaks on every UI change | **Unsupported.** Consumer terms §3 prohibit, "Except when you are accessing our Services via an Anthropic API Key or where we otherwise explicitly permit it, to access the Services through automated or non-human means, whether through a bot, script, or otherwise" ([Consumer Terms](https://www.anthropic.com/legal/consumer-terms)). Whether an extension that only rewrites what a human is about to send counts as "automated means" is a legal question, not a technical one — treat it as at-risk. The Usage Policy separately prohibits attempts to "Intentionally bypass capabilities, restrictions, or guardrails established within our products" ([Usage Policy](https://www.anthropic.com/legal/aup)); masking is not that, but an extension that manipulates the app is easy to mistake for it |

Data handling on claude.ai is governed by plan, not by anything we control: Free/Pro/Max
data may be used for training when the user's setting allows it (5-year retention), 30 days
otherwise; Team/Enterprise: 30-day standard retention ([Claude Code data usage](https://code.claude.com/docs/en/data-usage),
which restates the consumer/commercial policies). ZDR agreements cover "eligible Anthropic
APIs, Anthropic products that use your Commercial organization API key (including Claude
Code accessed via the API), and Claude Code for Enterprise plans" and explicitly not "Claude
Free, Pro, Max and when accounts from those plans use Claude Code" ([Privacy Center: ZDR scope](https://privacy.claude.com/en/articles/8956058-i-have-a-zero-retention-agreement-with-anthropic-what-products-does-it-apply-to)).
Enterprise chat "operates with standard retention" (per the Covered Models retention
article surfaced in search; UNVERIFIED as an exact quote — see Sources).

### 1.2 Claude Cowork

**What it is.** Anthropic's agentic knowledge-work product, on "Claude Desktop (macOS/Windows),
web (claude.ai), mobile (iOS/Android), and Chrome side panel"; paid plans. Sessions run in
two places ([Get started with Claude Cowork](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork),
[architecture overview](https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview)):

- **Cloud sessions (primary, beta):** "In a session in the cloud, the agent loop and code
  execution run in an isolated, temporary sandbox on Anthropic-managed infrastructure."
  Local folders are reached "through the Claude Desktop app on that device over an
  Anthropic-brokered connection" and "the agent's work, including any local files it opens
  through the desktop app, is processed on Anthropic's servers rather than staying on the
  device." "All traffic leaving the sandbox passes through a mandatory proxy the sandbox can't
  reconfigure or bypass, and only allow-listed destinations are reachable." "Connector
  authorization tokens never enter the sandbox; connector calls are made on the server side."
- **Local sessions (legacy desktop):** the agent loop runs natively on the device; "Code
  execution runs in an isolated virtual machine (VM)."

Data controls: "Zero data retention (ZDR) is not supported for Claude in Chrome, the same as
Cowork" ([Chrome admin controls](https://support.claude.com/en/articles/13065128-claude-in-chrome-admin-controls));
Cowork "isn't an Eligible Service under the BAA in any configuration" ([BAA Covered Models](https://support.claude.com/en/articles/15455031-covered-models-under-a-business-associate-agreement-baa),
quoted via search snippet — UNVERIFIED verbatim). Local-session history "isn't subject to
Anthropic's standard data retention policies" and cannot be centrally managed
([Cowork on Team and Enterprise](https://support.claude.com/en/articles/13455879-use-claude-cowork-on-team-and-enterprise-plans)).

**Extension points** (same sources plus [Use plugins in Claude](https://support.claude.com/en/articles/13837440-use-plugins-in-claude),
[When to use desktop and web connectors](https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors),
[Plugins reference](https://code.claude.com/docs/en/plugins-reference)):

| Extension point | Status in Cowork | Interception value |
|---|---|---|
| Remote MCP connectors | Work "across all Claude surfaces" including Cowork | Tool-level only (§1.5) |
| Local MCP servers / desktop extensions (`.mcpb`) | "only available in Claude Desktop and Claude Code—not on web or mobile"; the connector-choice article's quick reference limits them to "Desktop, Claude Code". They do not run inside cloud sessions | Not usable from cloud Cowork; local-session use is Desktop-only |
| Plugins | "Each plugin bundles skills, connectors, and sub-agents into a single package." "Hooks and sub-agents run only in Cowork, so they appear grayed out in chat." In Cowork and cloud sessions "Claude Code downloads the plugins enabled for your claude.ai account into `~/.claude/plugins/synced/` in the session's own environment and loads each one as `<name>@synced`" | **This is the one real hook into Cowork:** Cowork runs an embedded Claude Code session, so Claude Code hook events (§1.3) shipped inside a plugin run there. Same limits as Claude Code: prompt cannot be rewritten; tool inputs/outputs can. In cloud sessions an `http` hook to a maskroom endpoint must pass the sandbox egress allowlist (admin-managed "Code execution" network egress; whether hook traffic is subject to it is UNVERIFIED) |
| Skills | Instructions/scripts; run in chat and Cowork | Can *ask* Claude to call a masking tool first; not enforced |
| Scheduled tasks, projects, browser use | Product features | None |
| Admin controls (Team/Enterprise) | Enable/disable Cowork, cloud sessions toggle ("Enterprise plans: off by default"), built-in browser toggle, connector "Always allow" toggle, plugin marketplaces (auto-install/require/hide), auto-mode toggle, code-execution egress allowlists, OpenTelemetry event streaming, Enterprise skill/plugin scanning | Governance, not content transformation |
| Claude Desktop MDM policy | Keys include `isLocalDevMcpEnabled`, `isDesktopExtensionEnabled`, `secureVmFeaturesEnabled` ("Enable Cowork access in desktop"), `allowedWorkspaceFolders`, `forceLoginOrgUUID` ([Enterprise configuration for Claude Desktop](https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop)) | Can *restrict* which folders Cowork may mount — a blunt but real way to keep raw PII folders out of reach |

There is **no Cowork-specific hook system beyond plugin-carried Claude Code hooks**, and no
documented way to route cloud Cowork sessions through a customer gateway. Claude Desktop's
embedded sessions are a partial exception: with Anthropic's self-hosted *Claude apps gateway*,
"Claude Desktop runs its Cowork and Code tabs, plus the Chat tab when you enable it, on
embedded Claude Code sessions and sends their model requests through the gateway"
([Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway)). That covers
desktop *local* sessions; whether cloud Cowork sessions started from claude.ai web/mobile ever
touch a customer gateway is UNVERIFIED (the architecture article says they run on
Anthropic-managed infrastructure, which implies not).

### 1.3 Claude Code hooks

The [hooks reference](https://code.claude.com/docs/en/hooks) is precise about what each event
may do. The load-bearing sentences:

> A few events can also rewrite content rather than only allow or block it:
> - `PreToolUse`: `updatedInput` directly under `hookSpecificOutput` replaces a tool's arguments before it runs.
> - `PermissionRequest`: `updatedInput` inside the `decision` object.
> - `PostToolUse`: `updatedToolOutput` replaces the tool's result.
> - `UserPromptSubmit`: can't replace the prompt; it only injects `additionalContext` alongside it
>
> For redaction or transformation use cases, intercept at `PreToolUse` for outbound tool inputs and `PostToolUse` for inbound tool results.

Per event:

| Event | Input | Can it… | Notes |
|---|---|---|---|
| `UserPromptSubmit` | JSON with `prompt` ("the text the user submitted") | **Block** (`decision: "block"` "prevents the prompt from being processed and erases it from context", or exit 2 "Blocks prompt processing and erases the prompt"); **add context** (`additionalContext` or plain stdout, "injected as a system reminder"); set `sessionTitle`; `suppressOriginalPrompt`. **Cannot rewrite** the prompt | 30 s default timeout; a timed-out command/HTTP/MCP hook is cancelled and "The prompt still reaches Claude without that context" — i.e. it fails *open*. Only an Agent SDK callback hook fails closed |
| `UserPromptExpansion` | slash-command / MCP-prompt expansion, `command_name`, `command_args`, `prompt` | Block or add context only | Same shape as above |
| `PreToolUse` | tool name + `tool_input` | `permissionDecision` allow/deny/ask/defer, `updatedInput` ("Replaces the entire input object … Claude Code evaluates permission rules … against the input your hook returns") | Mask what a tool is about to *send out* (e.g. a WebFetch body, an MCP tool argument, a file about to be written) |
| `PostToolUse` | tool result | `updatedToolOutput` "Replaces the tool's output with the provided value before it is sent to Claude" (`updatedMCPToolOutput` for MCP tools) | Mask file contents returned by `Read`/`Grep`/`Bash` before the model sees them. Caveat: "Telemetry such as OpenTelemetry tool spans and analytics events also captures the original output before the hook runs" |
| `MessageDisplay` | batches of completed assistant-message lines | "Claude Code renders the hook's replacement text in their place" | **Display-side de-tokenization**: show `TOK_PERSON_…` as the real name to the human without ever sending the original to Anthropic. Display only; the transcript keeps the tokens |
| `Elicitation` / `ElicitationResult` | MCP elicitation requests | Answer/deny | Not relevant to masking |

Hook handler types: `command`, `http` ("send the event's JSON input as an HTTP POST request to
a URL. The endpoint communicates results back through the response body"), `mcp_tool`,
`prompt`, `agent`. `allowedHttpHookUrls` can restrict `http` hooks organization-wide. The
same events and fields are available programmatically in the [Agent SDK](https://code.claude.com/docs/en/agent-sdk/hooks)
("allow the operation, block it, modify the input, or inject context into the conversation").

Two facts about what leaves the machine: "Claude Code sends data over the network. This data
includes all user prompts and model outputs" and "Claude Code is compatible with most popular
VPNs and LLM proxies" ([data usage](https://code.claude.com/docs/en/data-usage)). Session
transcripts are cached "locally in plaintext under `~/.claude/projects/` for 30 days by default".

### 1.4 Claude API / SDK: gateway pattern

**Where maskroom fits.** Between the SDK client and `api.anthropic.com`, terminating the
Messages API: parse `messages[]`/`system`, pseudonymize text blocks, forward, then
de-tokenize the response (streaming or not). The SDK supports this natively:
`base_url` / `ANTHROPIC_BASE_URL` on the client, or a `DefaultHttpxClient(proxy=…)` (Python
SDK README, via the `claude-api` skill reference; also [Connect Claude Code to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect)).

**What a gateway must honour for Claude Code** ([gateway compatibility guide](https://code.claude.com/docs/en/llm-gateway-protocol)):

- Endpoints: `/v1/messages`, optionally `/v1/messages/count_tokens`; "Inference requests post
  to `/v1/messages?beta=true`, so match on the path, not the full URL." Optional `/v1/models`.
- Headers: "Forward `anthropic-version` and `anthropic-beta` unchanged"; "don't allowlist
  individual values, because the set changes with Claude Code releases."
- Streaming: "Stream inference responses. Claude Code reads the stream as it arrives, so if
  your gateway buffers complete responses before relaying them, Claude Code stalls."
  Keep-alive `ping` events must be forwarded (300 s silent-stream watchdog).
- Body: "Capabilities that add body fields pair them with a beta header … **A gateway that
  rewrites or redacts request bodies for content inspection breaks the pairing the same way
  stripping does, so inspect without modifying.**" Forward `cache_control` and the `system`
  array unchanged (the attribution block is stripped positionally by Anthropic).
- Errors: "forward error response bodies unmodified" (retry logic matches on wording).

Reading the body-rewrite warning precisely: the *breakage* described is header/field-pairing
breakage. Rewriting the *text inside* `messages[].content[].text` while leaving every field
and header in place does not remove a paired field, so it is technically survivable — but it
is explicitly outside what Anthropic supports, it defeats prompt caching whenever a token
changes an earlier turn (cache is a byte-prefix match), and Anthropic "doesn't endorse,
maintain, or audit third-party gateway products" ([Other LLM gateways](https://code.claude.com/docs/en/llm-gateway)).
Claude Code's own subagents, tool-result echoes and file reads also flow through the same
endpoint, so a gateway sees *everything* the CLI sends — a stronger interception point than
hooks, at the cost of parsing a fast-moving request schema.

**Subscriptions.** With `ANTHROPIC_AUTH_TOKEN`/`apiKeyHelper` the developer's claude.ai
subscription "isn't used"; usage bills to the gateway's provider credential. With only
`ANTHROPIC_BASE_URL` set, "a saved claude.ai login stays the active credential" and the
gateway must forward the OAuth capability in `anthropic-beta` ([gateway overview](https://code.claude.com/docs/en/gateways)).

**Can claude.ai / Cowork traffic be routed through such a gateway?** No, with the one
Desktop exception above. The gateway documentation is scoped to "Claude Code clients"; the
Claude apps gateway diagram shows "Claude Code clients and Claude Desktop's Chat, Cowork, and
Code tabs" as the only clients. claude.ai web/mobile and cloud Cowork sessions call
Anthropic's backend directly (Cowork cloud sandbox: "Anthropic-managed infrastructure";
Claude in Chrome: "your existing Claude endpoints"). The Claude apps gateway itself exposes
no content-transform plugin or middleware point in its documentation (grep of the page for
plugin/middleware/transform/rewrite finds only the `strictPluginOnlyCustomization` settings key).

**Anthropic-side data controls that apply here** ([API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention),
[Data residency](https://platform.claude.com/docs/en/manage-claude/data-residency)):
"Conversation content (your prompts and Claude's outputs) is not retained by default; the
exception is Covered Models, which require 30-day retention." ZDR is a per-organization
arrangement for eligible `/v1/messages` features. `inference_geo: "us"` pins inference to US
infrastructure at 1.1× price; only `"us"`/`"global"` exist and workspace geo is `"us"` only —
there is no Asia-Pacific residency option today, which matters for a Sri Lankan deployment.

### 1.5 MCP server pattern

The spec settles the question. Design principle 3 in both the 2025-06-18 and the current
2026-07-28 revisions: "**Servers should not be able to read the whole conversation, nor 'see
into' other servers** — Servers receive only necessary contextual information; Full
conversation history stays with the host" ([Architecture 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28/architecture)).
Servers "Expose resources, tools and prompts via MCP primitives" and "Request client input
(sampling, elicitation, roots)". There is no primitive by which a server observes or rewrites
the user's messages before inference.

- **Tools** are "model-controlled, meaning that the language model can discover and invoke
  tools automatically"; the server receives `arguments` and returns `content`
  ([Tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)). Clients
  "SHOULD … Show tool inputs to the user before calling the server, to avoid malicious or
  accidental data exfiltration."
- **Sampling** lets a server ask the *client* for an LLM completion; "there SHOULD always be a
  human in the loop with the ability to deny sampling requests" and applications should
  "Allow users to view and edit prompts before sending" ([Sampling](https://modelcontextprotocol.io/specification/2025-06-18/client/sampling)).
  The server composes the prompt it sends — it never receives the user's conversation.
- **Elicitation** requests structured input from the user; "Servers **MUST NOT** use
  elicitation to request sensitive information" ([Elicitation](https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation)).

Consequences for a maskroom MCP server (`mask_text`, `mask_file`, `unmask`):

1. **On claude.ai/Cowork the tool only helps when the PII enters through the tool.** If the
   user pastes a payroll extract into the chat and then says "mask this", the raw text is
   already in the transcript on Anthropic's side. The valuable shape is *fetch-and-mask*: the
   server reads the file from your storage (path, SharePoint/Drive id, database query) and
   returns masked text, so the original never enters the conversation.
2. **`unmask` must not return originals into the conversation** — a tool result is model
   context and goes straight back to Anthropic. De-tokenization has to happen outside Claude:
   return a `resource_link` to a maskroom-hosted page/download that the *user's browser*
   fetches, or leave restoration to the maskroom web UI.
3. Remote connectors are reached "from Anthropic's cloud infrastructure" and must be public
   (or IP-allowlisted); a maskroom MCP server is therefore an internet-facing service holding
   vaults — the ROADMAP's HMAC/encrypted-vault items become prerequisites.
4. In Claude Code and Desktop the same server can run locally over stdio, which keeps vault
   and salt on the machine.

### 1.6 Enterprise controls

| Control | What is documented | Relevance |
|---|---|---|
| **Inference hooks** (Claude Enterprise, **beta**) | "Inference hooks let a Claude Enterprise organization route every governed prompt through an AI security server, an HTTPS service that the organization or its security vendor operates, before inference runs. When a user submits a prompt, Anthropic sends the conversation transcript to your AI security server and waits for an allow or deny verdict; a denied request never reaches the model." "One hook governs conversations across claude.ai, Cowork, and Claude Code sessions in your Claude Enterprise organization, whether they run on the web, in the desktop or mobile apps, or in the CLI." Your server "sees what the user sees: transcript text, tool calls and their results, and text extracted from attachments. It never receives raw file or image bytes, system prompts, or Anthropic-internal context." **"Verdicts are allow or deny. Rewriting or redacting a prompt is not supported."** "Platform organizations (API access through the Claude Platform) are out of scope." Announced 2026-08-05 with Netskope, Palo Alto Networks, Proofpoint and Zscaler integrations; "open, webhook-based protocol with a published schema" ([overview](https://platform.claude.com/docs/en/manage-claude/inference-hooks), [endpoint](https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint), [configuration](https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration), [announcement](https://claude.com/blog/claude-enterprise-inference-hooks)) | **The only officially supported inline interception of claude.ai/Cowork traffic.** It is a *gate*, not a *transformer*. Contract: HTTPS POST, `User-Agent: anthropic-dlp/1`, Standard-Webhooks HMAC-SHA256 signature, body `{type:"prompt", actor, source.application ("claude-ai" / "claude-code"), messages[] with text / tool_use / tool_result / attachment{text}}`, up to 10 MB, untruncated; reply `{"action":"allow"}` or `{"action":"deny","deny_reason":…,"reference_id":…}` within a 1–10,000 ms timeout (default 5 s); fail-open or fail-closed is an admin choice; shadow mode, rollout %, role exclusions; must be a publicly routable host on port 443 (no tunnels); source IPs `160.79.106.0/24` |
| Compliance API | Post-hoc: "retrieves records after the fact" — chats, files, projects and "transcripts of Cowork, Claude Code, Claude Science, and Claude for Microsoft 365 sessions"; Enterprise only ([Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api)) | Audit/detection after disclosure, not prevention |
| OpenTelemetry | Cowork and Claude Code can "stream Cowork events to your SIEM and observability tools through OpenTelemetry" | Monitoring |
| Custom data retention | "The minimum retention period is 30 days"; Enterprise; storage only ([custom retention](https://support.claude.com/en/articles/10440198-custom-data-retention-controls-for-claude-enterprise)) | Reduces exposure window; does not prevent it |
| ZDR | API / Claude Code for Enterprise, "enabled on a per-organization basis by your account team"; not consumer plans; not Cowork or Claude in Chrome | Not available on the surfaces users actually chat in |
| US-only inference | Enterprise usage-based plans; covers "all the Claude apps, including Claude Code, Claude Desktop, etc."; "applies to inference only" — storage location is separate ([US-only inference](https://support.claude.com/en/articles/15422948-enable-us-only-inference-for-your-organization)) | No LK/APAC residency option |
| Claude Desktop MDM, Chrome admin controls, connector allowlists, plugin marketplaces, skill scanning | See §1.2 | Restrict *what can be reached*, not *what is sent* |
| DLP vendor integrations | Only via Inference hooks (above). The enterprise marketing page lists "Audit logs and OpenTelemetry monitoring", "Compliance API", "Data retention controls" ([Claude Enterprise](https://claude.com/solutions/enterprise)); no first-party PII-redaction feature is documented anywhere | Anthropic offers detection/denial hooks, never redaction |

---

## 2. What maskroom offers today, and what a middleware needs

### 2.1 Current interfaces (repo facts)

| Interface | Location | Shape |
|---|---|---|
| CLI | `maskroom/cli.py:9-68` | File in → file out. Accepts `.xlsx`/`.xlsm`/`.pdf` only (`cli.py:60-65`); `--restore` is Excel-only (`cli.py:54-57`). No text mode |
| Flask web UI / API | `webui/app.py:72-155` | `POST /api/process` multipart upload, same file types (`app.py:79`); builds a **new engine per request** (`app.py:91-99`); writes `vault.json` per run under `webui/runs/<id>/` (`app.py:125-127`); `GET /api/download/<run>/<file>`. No authentication (README "Add authentication"). `Dockerfile:27` says "a lock in app.py serializes engine use" but `app.py` contains no lock (grep) — with gunicorn `--threads 4` two uploads can run concurrently on separate engines; harmless today because each request has its own engine, but wrong once a shared engine/vault is introduced |
| Python API | `maskroom/engine.py` | `analyze_text()` (`:103`), `pseudonymize_text()` → `(text, changed)` (`:213-251`), `depseudonymize_text()` (`:253-255`), `save_vault()`/`load_vault()` plaintext JSON (`:259-274`), file pipelines via mixins |

### 2.2 Tokens, reversibility, vault lifecycle

- Token = `TOK_<ENTITY>_<SHA-256(value + salt)[:8]>`, hex prefix extended on collision
  (`engine.py:87-100`); regex `TOK_[A-Z0-9_]+_[0-9A-F]{8,}` (`rules.py:7`). Deterministic
  across files and sessions for a given salt; default salt is a hard-coded string when
  `PII_TOKEN_SALT` is unset (`engine.py:63`).
- Excel is reversible with the vault; PDF is irreversible by design (README).
- `depseudonymize_text()` is an **exact** regex substitution: unknown tokens are left as-is,
  and nothing handles case changes, inserted spaces, dropped hex characters or invented
  tokens (`engine.py:255`). `tests/test_text.py:25` covers only the clean round trip.
- The vault is an in-memory dict per engine instance (`engine.py:67`), persisted as plaintext
  JSON. The web UI creates one vault per upload; nothing spans a conversation.
- Spans overlapping existing tokens are skipped, so re-masking is idempotent (`engine.py:229-235`).

### 2.3 Roadmap items already identified for the LLM use case

`ROADMAP.md` "Reassessment for the cloud-LLM use case (2026-08-22)" already lists, in
priority order: (1) tolerant `depseudonymize_text` + LLM-friendly token surface form
(`PERSON_a7k3`-style) + unresolved-token report; (2) OCR confidence with block-on-poor;
(3) `BatchAnalyzerEngine` batching to make `en_core_web_trf` the free-text default;
(4) PDF text-mode output; (5) Sinhala/Tamil stage 1; (6) session vault lifecycle + HMAC
tokens + encrypted vault + run TTL; (7) strict quasi-identifier profile + structured run
reports. All of these are prerequisites for any of the architectures below; none of them is
sufficient on its own.

### 2.4 Gaps specific to a middleware role

| Gap | Why it matters | What is needed |
|---|---|---|
| **Text/JSON HTTP API** | Every architecture in §3 calls maskroom over HTTP with text, not files | `POST /v1/mask` `{text \| messages[], session_id, policy}` → `{masked, findings, unresolved}`; `POST /v1/unmask` `{text, session_id}`; `POST /v1/mask/batch`; `POST /v1/detect` (findings only, for the inference-hook gate). Idempotent on re-mask (already true) |
| **Streaming de-tokenization** | Gateway responses are SSE; a token can straddle two `content_block_delta` events | A stateful un-tokenizer that holds back any suffix that could be a token prefix (`TOK_`, `TOK_PER…`) and flushes on the next delta or `message_stop`; also handles the whole tolerant-match problem from ROADMAP item 1 inside the buffer |
| **Per-session / per-user vault store** | A conversation spans many requests; the vault must be found again for un-masking and retired later | Vault keyed by `(tenant, user, session)`, TTL, encrypted at rest (ROADMAP 3b), deterministic tokens so a value re-sent in turn 5 maps to the same token as turn 1; explicit `DELETE /v1/session` |
| **Latency** | A gateway or hook sits on the critical path of every turn; `UserPromptSubmit` and inference-hook defaults are 30 s and 5 s | Measured (`TECHNICAL_DESIGN.md §6`): ~250-cell workbook 3.8 s `lg` / 141 s `trf`; per-prompt text is far smaller, but `trf` on CPU is still seconds. Keep one warm engine per process (the web UI constructs an engine per request — `app.py:91`; whether spaCy model loading is amortized across constructions is UNVERIFIED, measure it), pre-load, `lg` for interactive paths, `trf` only for batch/file paths, and honour caller deadlines |
| **Auth and multi-tenancy** | The MCP and inference-hook variants are internet-facing | API keys or mTLS, per-tenant salts/keys, request signing verification (Standard Webhooks for inference hooks), rate limits |
| **Messages-API awareness** | A gateway must mask `system`, `messages[].content[].text`, `tool_result` content and document/attachment text, and *not* mask `tool_use.input` schemas, `cache_control`, beta fields | A thin Messages-API walker; forward everything else byte-for-byte |
| **Concurrency** | `Dockerfile` assumes a lock that does not exist | Add the lock (or a worker pool) before sharing an engine across threads |

---

## 3. Recommendation matrix

| # | Interception point | Feasible? | Officially supported? | maskroom changes required | Residual PII risk | Effort |
|---|---|---|---|---|---|---|
| A | **Reverse proxy / gateway for apps built on the Claude API** (`ANTHROPIC_BASE_URL`) | Yes — the only place both directions are transparent | Yes for routing (SDK `base_url`, gateway docs); body rewriting is not an Anthropic-supported operation but is entirely on your side of the API contract | Text API, Messages-API walker, streaming un-tokenizer, session vault, warm engine, auth | Detection misses go to Anthropic; token mangling by the model (mitigated by tolerant restore); quasi-identifiers (strict profile); prompt-cache misses whenever masking changes an earlier turn | Medium (≈2–3 weeks on top of ROADMAP items 1 and 6) |
| B | **Claude Code through the same gateway** | Yes | Routing yes; "inspect without modifying" is Anthropic's explicit guidance; unsupported features break if fields are stripped | As A, plus forward `anthropic-beta`/`anthropic-version`, `system` array, `cache_control`, pings and error bodies unchanged | As A, plus a fast-moving request schema (test against each Claude Code release) | Medium |
| C | **Claude Code hooks** (`PreToolUse` `updatedInput`, `PostToolUse` `updatedToolOutput`, `MessageDisplay` replacement, `UserPromptSubmit` block/context) | Yes | **Yes** — the documented "redaction or transformation" path | `http`-type hook endpoint that accepts the hook JSON and returns `updatedInput` / `updatedToolOutput`; a local `command` hook for offline use; a `MessageDisplay` de-tokenizer | **The typed prompt itself cannot be rewritten** — only blocked. Covers file contents and tool traffic, not what the user types. `UserPromptSubmit` command/HTTP hooks fail open on timeout | Low–medium |
| D | **Cowork via a synced plugin carrying the same hooks** | Partly — "Hooks and sub-agents run only in Cowork" and plugins are synced into Cowork/cloud sessions | Plugins are supported; which hook events fire in Cowork and whether `http` hooks can leave the cloud sandbox are UNVERIFIED | As C, packaged as a plugin; egress allowlisting by the admin | As C; cloud-session files are already on Anthropic's servers before any hook runs ("processed on Anthropic's servers"), so the hook only shapes what the *model* sees, not what Anthropic *stores* | Medium, plus verification |
| E | **MCP server "mask/fetch-and-mask" tool** (remote for claude.ai/Cowork/Desktop, stdio for Claude Code/Desktop) | Yes | Yes (custom connectors, local MCP) | MCP server wrapping the text API; `mask_file`/`fetch_and_mask` sources; `unmask` returning a resource link, never originals; internet-facing hardening (HMAC tokens, encrypted vault, auth) | Opt-in per call — the model or the user must choose to call it; anything typed or uploaded directly bypasses it; a single "unmask into chat" call re-discloses everything | Low–medium |
| F | **Inference hook "AI security server" for Claude Enterprise** | Yes (beta) | **Yes** — Anthropic's own inline DLP hook, all of claude.ai/Cowork/Claude Code | `POST /v1/detect`-style endpoint returning `{"action":"deny","deny_reason":"…contains an NIC and 3 phone numbers; mask with maskroom first"}` when findings exceed policy; Standard-Webhooks verification; public HTTPS host; sub-second detection (`lg`, warm engine); shadow-mode metrics | Cannot mask — it *denies*. Image-only content not inspected; admin may choose fail-open; requires Claude Enterprise; the transcript is sent to your server for every turn (your server becomes a PII sink — do not store) | Low–medium |
| G | **Browser extension / userscript on claude.ai** | Technically (content script on the composer + rendered replies); not at the network layer under MV3 | **No.** Consumer terms restrict "automated or non-human means"; breaks on UI changes; blind to uploads, connectors, Chrome agent, mobile | Text API + un-tokenize-on-render | High: partial coverage, silent breakage, policy exposure | Medium, ongoing maintenance |
| H | **Pre-mask files before upload** (today's web UI / CLI) | Yes | Yes (nothing to support) | Text-mode PDF output (ROADMAP 4), session vault so answers can be restored | Entirely dependent on user discipline; typed text uncovered | None / already done |

### 3.1 Recommended architectures

**(a) API gateway for applications you own.** Build the text API and a Messages-API-aware
proxy; run it on-premises; point your own applications (and, optionally, developers' Claude
Code via `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`) at it. This is the only architecture
in which masking is transparent, enforced, and reversible end-to-end, and the only one where
the vault never leaves your network. Pair it with an API-side ZDR arrangement where eligible.

**(b) maskroom MCP server** for Cowork, claude.ai, Claude Desktop and Claude Code — designed
around *fetch-and-mask* (the server reads the source and returns masked text) rather than
"mask this text I pasted", with un-masking delivered out-of-band. Remote-MCP hosting makes
the ROADMAP's HMAC/encrypted-vault/auth items mandatory first.

**(c) Claude Code hooks (and the same hooks in a Cowork plugin)** for redacting tool
inputs/outputs and de-tokenizing on display. Cheap, official, and complementary to (a): the
gateway catches what hooks cannot (the typed prompt), hooks catch what the gateway should not
have to parse (tool payload semantics).

**(d) Inference hook as the enforcement layer (Claude Enterprise only).** maskroom as the
"AI security server": allow prompts that contain only tokens, deny prompts that contain
detected PII with a reason pointing users at (b) or the web UI. This is the one supported way
to make masking *mandatory* on claude.ai and Cowork — by refusing unmasked input, not by
transforming it. Run in shadow mode first to calibrate false positives against real traffic.

**(e) Browser extension** — not recommended. Keep as an experiment only if the team accepts
unsupported status and the terms-of-service ambiguity; it must never be the control relied on.

### 3.2 What is not possible (as of 2026-09-08)

- **Transparent, enforced rewriting of claude.ai or Cowork traffic.** No client-side hook, no
  customer-side proxy, and no Anthropic-side transform exists. Inference hooks explicitly do
  not support "Rewriting or redacting a prompt". Cloud Cowork sessions run on Anthropic
  infrastructure and process local files there.
- **An MCP server that sees or rewrites the user's prompt.** Contrary to the protocol's design
  principle; the primitives are tools, resources, prompts, sampling, elicitation, roots.
- **Routing claude.ai or cloud Cowork through your own gateway.** Only Claude Code (and Claude
  Desktop's embedded Chat/Cowork/Code tabs via the Claude apps gateway) can be pointed at a
  gateway.
- **ZDR for the chat surfaces.** ZDR covers the API and Claude Code for Enterprise; "not
  supported for Claude in Chrome, the same as Cowork"; consumer plans excluded.
- **Data residency outside the US.** `inference_geo` offers `"us"` or `"global"` only.
- **A hook that rewrites the typed prompt in Claude Code.** `UserPromptSubmit` "can't replace
  the prompt".

### 3.3 Suggested order of work

1. ROADMAP item 1 (tolerant restore, token surface form) and item 6 (session vault, HMAC,
   encryption, TTL) — every architecture depends on them.
2. Text/JSON HTTP API with a warm engine, auth, and the missing engine lock (`Dockerfile:27`).
3. Claude Code hooks package (C) — smallest supported win; doubles as the Cowork plugin (D)
   once the open questions are verified against a real Cowork session.
4. Messages-API gateway (A/B) with streaming un-tokenization.
5. MCP server (E) built on the same API, fetch-and-mask first.
6. Inference-hook endpoint (F) if/when the organization is on Claude Enterprise.

---

## Sources

Anthropic / Claude — product and support documentation

- https://support.claude.com/en/articles/8241126-upload-files-to-claude
- https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp
- https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop
- https://support.claude.com/en/articles/11725091-when-to-use-desktop-and-web-connectors
- https://support.claude.com/en/articles/12012173-get-started-with-claude-in-chrome
- https://support.claude.com/en/articles/13065128-claude-in-chrome-admin-controls
- https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork
- https://support.claude.com/en/articles/13364135-use-claude-cowork-safely
- https://support.claude.com/en/articles/13455879-use-claude-cowork-on-team-and-enterprise-plans
- https://support.claude.com/en/articles/14479288-claude-cowork-architecture-overview
- https://support.claude.com/en/articles/13837440-use-plugins-in-claude
- https://support.claude.com/en/articles/12622667-enterprise-configuration-for-claude-desktop
- https://support.claude.com/en/articles/10440198-custom-data-retention-controls-for-claude-enterprise
- https://support.claude.com/en/articles/15422948-enable-us-only-inference-for-your-organization
- https://support.claude.com/en/articles/15455031-covered-models-under-a-business-associate-agreement-baa (seen only as a search snippet — UNVERIFIED verbatim)
- https://support.claude.com/en/articles/15425996-data-retention-practices-for-covered-models (seen only as a search snippet — UNVERIFIED verbatim)
- https://privacy.claude.com/en/articles/8956058-i-have-a-zero-retention-agreement-with-anthropic-what-products-does-it-apply-to
- https://claude.com/solutions/enterprise
- https://claude.com/blog/claude-enterprise-inference-hooks (Anthropic first-party announcement)

Claude Code documentation

- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/agent-sdk/hooks
- https://code.claude.com/docs/en/plugins-reference
- https://code.claude.com/docs/en/gateways
- https://code.claude.com/docs/en/llm-gateway
- https://code.claude.com/docs/en/llm-gateway-connect
- https://code.claude.com/docs/en/llm-gateway-protocol
- https://code.claude.com/docs/en/claude-apps-gateway
- https://code.claude.com/docs/en/data-usage

Claude Platform documentation

- https://platform.claude.com/docs/en/manage-claude/inference-hooks
- https://platform.claude.com/docs/en/manage-claude/inference-hooks-endpoint
- https://platform.claude.com/docs/en/manage-claude/inference-hooks-configuration
- https://platform.claude.com/docs/en/manage-claude/compliance-api
- https://platform.claude.com/docs/en/manage-claude/api-and-data-retention
- https://platform.claude.com/docs/en/manage-claude/data-residency

Terms and policies

- https://www.anthropic.com/legal/consumer-terms
- https://www.anthropic.com/legal/commercial-terms
- https://www.anthropic.com/legal/aup

MCP specification

- https://modelcontextprotocol.io/specification/2026-07-28/architecture
- https://modelcontextprotocol.io/specification/2025-06-18/architecture
- https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- https://modelcontextprotocol.io/specification/2025-06-18/client/sampling
- https://modelcontextprotocol.io/specification/2025-06-18/client/elicitation

Chrome extension platform

- https://developer.chrome.com/docs/extensions/reference/api/webRequest

Repository

- `README.md`, `ROADMAP.md`, `docs/TECHNICAL_DESIGN.md`
- `maskroom/engine.py` (`generate_token` :87, `analyze_text` :103, `pseudonymize_text` :213, `depseudonymize_text` :253, vault :259)
- `maskroom/rules.py:7` (`TOKEN_RE`), `maskroom/cli.py`, `webui/app.py`, `Dockerfile`, `tests/test_text.py`
