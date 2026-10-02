# SafePII

PII pseudonymization before data reaches an LLM: a Presidio engine with Sri
Lankan identifiers, a web UI and JSON API, a Chrome extension for claude.ai, a
Windows helper for Claude Desktop, an admin audit trail and a rules console.
`README.md` has the overview.

**SafePII** is the product name; **maskroom** is the implementation's — the Python
package, the `maskroom-admin` command, the `MASKROOM_*` variables and the
`X-Requested-With: maskroom` header that clients and server agree on. User-facing
text says SafePII. `CONTEXT.md` has the vocabulary and settles which word to use.

## Running the tests

```bash
pii_env/bin/pytest -q                                   # everything (~5 min; loads the NLP model)
pii_env/bin/pytest -q tests/test_desktop_overlay.py     # the Windows helper, no Windows needed
```

The desktop suites run against a fake accessibility layer, but the ones that
build the real bar need a display and **skip silently without one** (23 of them).
On a headless machine or in CI: `xvfb-run -a pii_env/bin/pytest -q`.

## The desktop side

`desktop/helper.py` and `desktop/broker.py` are Windows programs. They are tested
here against fakes, and the parts that cannot be — the low-level hooks, the
frozen build, the registry — have repeatedly been wrong on a real machine while
right in the tests. When something there cannot be reproduced locally, **add a
log line and ask for the log** rather than guessing from here; guessing has cost
several round trips, and the log has settled every one of them.

Before changing behaviour, read the decision rather than re-deriving it:
`docs/adr/` (0004–0009 are the desktop ones), `docs/DESKTOP_APP_RESEARCH.md` §5.6
for folder access, and `enterprise/WINDOWS-RUNBOOK.md` for what a fleet needs.

## Two rules this project learned the hard way

**Check Claude Desktop's behaviour, do not infer it.** Its configuration schema
can be read from the published Linux package (`downloads.claude.ai`, unpack the
`.deb`, read `resources/app.asar` as text) — but a declared scope there is not a
promise about behaviour: `managedMcpServers` says it supports standard
deployments and does not. Verify on a machine, and record which source said what.

**A file written through a shell here-doc loses a backslash layer.** Windows
paths in docstrings and regexes have arrived mangled more than once;
`tests/test_source_hygiene.py` compiles every source file and fails on the
warning that follows.

## Agent skills

### Issue tracker

Issues are tracked as GitHub issues in `AflalHussain/maskroom` (via the `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Default canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context (`CONTEXT.md` + `docs/adr/` at the repo root). See `docs/agents/domain.md`.
