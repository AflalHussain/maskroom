// End-to-end check of the extension in a real Chromium against a harness
// page served in place of claude.ai. Needs a Maskroom server (MASKROOM_URL,
// default http://127.0.0.1:5199) and playwright (PLAYWRIGHT_MODULE path or
// a resolvable "playwright"). Run:
//   xvfb-run -a node extension/test/e2e.js
const path = require("path");
const fs = require("fs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const EXT = path.resolve(__dirname, "..");
const SERVER = process.env.MASKROOM_URL || "http://127.0.0.1:5199";
const HARNESS = fs.readFileSync(path.join(__dirname, "harness.html"), "utf8");
const assert = (c, m) => { if (!c) throw new Error("ASSERT: " + m); console.log("ok -", m); };

(async () => {
  const userDir = fs.mkdtempSync(path.join(require("os").tmpdir(), "mr-ext-"));
  const ctx = await chromium.launchPersistentContext(userDir, {
    headless: false,
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`, "--no-first-run"],
  });
  try {
    // Point the extension at the test server.
    let [sw] = ctx.serviceWorkers();
    if (!sw) sw = await ctx.waitForEvent("serviceworker");
    await sw.evaluate((url) => chrome.storage.local.set({ serverUrl: url, sessions: {} }), SERVER);
    // The first mask on a fresh server loads the NLP model (can exceed 30 s); warm it up
    // outside the timed checks.
    const warm = await sw.evaluate(async (base) => {
      const r = await fetch(`${base}/api/mask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "warm up" }) });
      return r.status;
    }, SERVER);
    assert(warm === 200, `server reachable at ${SERVER} and warmed up`);

    const page = await ctx.newPage();
    page.on("console", (m) => { if (m.type() === "error" || m.type() === "warning") console.log("  [page console]", m.text()); });
    page.on("pageerror", (e) => console.log("  [page error]", e.message));
    sw.on("console", (m) => console.log("  [worker console]", m.text()));
    await page.route("https://claude.ai/**", (route) => route.fulfill({ status: 200, contentType: "text/html", body: HARNESS }));
    await page.goto("https://claude.ai/chat/0a1b2c3d-e2e0-4000-8000-000000000001");
    await page.waitForSelector("#maskroom-bar", { timeout: 15000 });
    assert(true, "bar injected on claude.ai page");

    // 1. Guard: Enter with unmasked text masks instead of sending.
    const composer = page.locator(".ProseMirror");
    await composer.click();
    await page.keyboard.type("Nimal Perera (NIC 853421234V) called from 077-1234567 about the loan.");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".ProseMirror").innerText.includes("TOK_"), null, { timeout: 30000 });
    let text = await composer.innerText();
    assert(!text.includes("Nimal") && !text.includes("853421234V"), "guard masked the composer instead of sending");
    assert(text.startsWith("Note:"), "preamble prepended on first masked message");
    assert((await page.evaluate(() => window.sent.length)) === 0, "nothing was sent automatically");
    // last match: the preamble itself contains an example token
    const personTok = [...text.matchAll(/TOK_PERSON_[0-9A-F]+/g)].pop()[0];

    // 2. Second Enter sends the masked text (the human still sends).
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => window.sent.length === 1);
    const sent = await page.evaluate(() => window.sent[0]);
    assert(sent.includes(personTok) && !sent.includes("Nimal"), "masked text was what got sent");
    assert(await page.locator("#maskroom-bar .mr-meta").innerText().then((t) => /session \w+ · \d+ pseudonyms/.test(t)), "bar shows session and vault size");

    // 3. Replies streamed with mangled tokens are restored on screen.
    await page.evaluate((tok) => window.addReply(`Summary: ${tok.toLowerCase().replace(/_/g, " ")} has an overdue loan; unknown TOK_PERSON_99999999 stays.`), personTok);
    await page.waitForFunction(() => { const m = document.querySelector(".msg.assistant"); return m && m.textContent.includes("Nimal Perera"); }, null, { timeout: 10000 });
    const reply = await page.locator(".msg.assistant").innerText();
    assert(reply.includes("Nimal Perera") && reply.includes("TOK_PERSON_99999999"), "reply restored on screen, unknown token left alone");
    assert(await page.locator(".msg.assistant.maskroom-restored").count() === 1, "restored element marked");

    // 3b. Unmask view is two-way: off puts the tokens back, on restores again.
    await page.click('#maskroom-bar [data-act="unmask"]');
    await page.waitForFunction((tok) => document.querySelector(".msg.assistant").textContent.includes(tok), personTok.toLowerCase().replace(/_/g, " "));
    assert(await page.locator(".msg.assistant.maskroom-restored").count() === 0, "unmask view off: tokens back on screen, marker removed");
    await page.click('#maskroom-bar [data-act="unmask"]');
    await page.waitForFunction(() => document.querySelector(".msg.assistant").textContent.includes("Nimal Perera"));
    assert(await page.locator(".msg.assistant.maskroom-restored").count() === 1, "unmask view on again: restored");

    // 4. Text with nothing to mask: the guard checks it, then replays the send.
    await composer.click();
    await page.keyboard.type(`Thanks, what about ${personTok}?`);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => window.sent.length === 2, null, { timeout: 30000 });
    assert((await page.evaluate(() => window.sent[1])).includes(personTok), "clean text was sent on the first Enter after the check");

    // 5. Same value in a later turn gets the same token (session vault).
    await composer.click();
    await page.keyboard.type("Nimal Perera again please");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".ProseMirror").innerText.includes("TOK_"), null, { timeout: 30000 });
    text = await composer.innerText();
    assert(text.includes(personTok) && !text.startsWith("Note:"), "same token reused, preamble not repeated");

    // 6. Session survives a reload of the same chat.
    await page.reload();
    await page.waitForSelector("#maskroom-bar");
    await page.waitForFunction(() => /pseudonyms/.test(document.querySelector("#maskroom-bar .mr-meta").textContent));
    const meta = await page.locator("#maskroom-bar .mr-meta").innerText();
    assert(/\d+ pseudonyms/.test(meta) && !/no session/.test(meta), "session restored after reload");

    // ---------------------------------------------------------- guard
    const sentCount = () => page.evaluate(() => window.sent.length);
    const lastSent = () => page.evaluate(() => window.sent[window.sent.length - 1]);
    const bar = (act) => page.click(`#maskroom-bar [data-act="${act}"]`);

    // 6a. Shift+Enter is a newline, not a send: nothing masked, nothing sent.
    let before = await sentCount();
    await composer.click();
    await page.keyboard.type("Line one about Kumari Bandara");
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.type("line two");
    await page.waitForTimeout(600);
    assert((await sentCount()) === before && (await composer.innerText()).includes("Kumari Bandara"), "Shift+Enter neither sends nor masks");

    // 6b. The send button is guarded like Enter: first click masks and stops, second click sends.
    await page.click('button[aria-label="Send message"]');
    await page.waitForFunction(() => !document.querySelector(".ProseMirror").innerText.includes("Kumari"), null, { timeout: 30000 });
    assert((await sentCount()) === before, "send-button click masked instead of sending");
    await page.click('button[aria-label="Send message"]');
    await page.waitForFunction((n) => window.sent.length === n + 1, before);
    assert(!(await lastSent()).includes("Kumari") && (await lastSent()).includes("TOK_PERSON_"), "second click sent the masked text");

    // 6c. Editing after a mask: Enter masks and stops; the user adds words; the next Enter
    // re-checks, finds nothing new, and sends.
    before = await sentCount();
    await composer.click();
    await page.keyboard.type("Follow-up for Kumari Bandara, NIC 853421234V");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".ProseMirror").innerText.includes("TOK_LK_NIC_"), null, { timeout: 30000 });
    assert((await sentCount()) === before, "edit test: first Enter masked and stopped");
    await page.keyboard.press("End");
    await page.keyboard.type(" — please hurry");
    await page.keyboard.press("Enter");
    await page.waitForFunction((n) => window.sent.length === n + 1, before, { timeout: 30000 });
    const edited = await lastSent();
    assert(edited.includes("please hurry") && edited.includes("TOK_LK_NIC_") && !edited.includes("853421234V"), "edited-after-mask text re-checked and sent on the next Enter");

    // 6d. Guard off: raw text goes straight through; guard on again: intercepted.
    await bar("guard");
    await page.waitForFunction(() => document.querySelector('#maskroom-bar [data-act="guard"]').classList.contains("mr-off"));
    before = await sentCount();
    await composer.click();
    await page.keyboard.type("Raw note about Kumari Bandara with guard off");
    await page.keyboard.press("Enter");
    await page.waitForFunction((n) => window.sent.length === n + 1, before);
    assert((await lastSent()).includes("Kumari Bandara"), "guard off: text sent unmasked, as configured");
    await bar("guard");
    await page.waitForFunction(() => !document.querySelector('#maskroom-bar [data-act="guard"]').classList.contains("mr-off"));
    before = await sentCount();
    await composer.click();
    await page.keyboard.type("Guard back on, NIC 853421234V");
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => document.querySelector(".ProseMirror").innerText.includes("TOK_"), null, { timeout: 30000 });
    assert((await sentCount()) === before, "guard on again: intercepted and masked");
    await page.keyboard.press("Enter");
    await page.waitForFunction((n) => window.sent.length === n + 1, before);

    // ---------------------------------------------------------- files
    const fixture = path.join(__dirname, "fixture.xlsx");
    const attachments = () => page.evaluate(() => [...document.querySelectorAll(".attachment")].map((a) => a.textContent));
    const idle = () => page.waitForFunction(() => !document.querySelector('#maskroom-bar [data-act="mask"]').disabled, null, { timeout: 60000 });
    const lastAttachedText = () => page.evaluate(async () => { const f = window.attached[window.attached.length - 1]; return { name: f.name, size: f.size, text: await f.text() }; });

    // 7. Mask file through the bar: masked workbook attached via the page's file input.
    await idle();
    await page.setInputFiles("#maskroom-file", fixture);
    await page.waitForFunction(() => document.querySelector(".attachment"), null, { timeout: 60000 });
    let att = await lastAttachedText();
    assert(att.name === "fixture_masked.xlsx" && att.size > 0, "masked workbook attached with _masked name");
    assert(!(await attachments()).includes("fixture.xlsx"), "raw workbook never attached");
    const vault = await sw.evaluate(async (base) => {
      const { sessions } = await chrome.storage.local.get("sessions");
      const id = sessions["0a1b2c3d-e2e0-4000-8000-000000000001"];
      return (await (await fetch(`${base}/api/session/${id}/vault`)).json()).mappings;
    }, SERVER);
    const kumariTok = Object.entries(vault).find(([, v]) => v === "Kumari Bandara");
    assert(kumariTok, "workbook values joined the chat's session vault");
    await page.evaluate((tok) => window.addReply(`From the sheet: ${tok} earns 98000.`), kumariTok[0]);
    await page.waitForFunction(() => [...document.querySelectorAll(".msg.assistant")].some((m) => m.textContent.includes("Kumari Bandara")), null, { timeout: 10000 });
    assert(true, "reply about the file restored on screen from the shared vault");

    // 8. Markdown mode: attached .md holds tokens, not names.
    await sw.evaluate(() => chrome.storage.local.set({ excelAttach: "md" }));
    await idle();
    await page.setInputFiles("#maskroom-file", fixture);
    await page.waitForFunction(() => document.querySelectorAll(".attachment").length === 2, null, { timeout: 60000 });
    att = await lastAttachedText();
    assert(att.name === "fixture_masked.md" && att.text.includes("TOK_PERSON_") && !att.text.includes("Nimal") && att.text.includes("120000"), "markdown attachment is masked and keeps the salary");

    // 9. Guard: a file picked through claude.ai's own input is masked before it is attached.
    await idle();
    await page.setInputFiles("#native-file", fixture);
    await page.waitForFunction(() => document.querySelectorAll(".attachment").length === 3, null, { timeout: 60000 });
    att = await lastAttachedText();
    assert(att.name === "fixture_masked.md" && !(await attachments()).includes("fixture.xlsx"), "guard intercepted the native file picker");

    // 9b. Guard off: a file picked through claude.ai's own input is attached raw; guard on again catches it.
    await bar("guard");
    await page.waitForFunction(() => document.querySelector('#maskroom-bar [data-act="guard"]').classList.contains("mr-off"));
    let nAtt = (await attachments()).length;
    await idle();
    await page.setInputFiles("#native-file", fixture);
    await page.waitForFunction((n) => document.querySelectorAll(".attachment").length === n + 1, nAtt);
    assert((await lastAttachedText()).name === "fixture.xlsx", "guard off: native picker attaches the raw file, as configured");
    await bar("guard");
    await page.waitForFunction(() => !document.querySelector('#maskroom-bar [data-act="guard"]').classList.contains("mr-off"));
    nAtt = (await attachments()).length;
    await idle();
    await page.setInputFiles("#native-file", fixture);
    await page.waitForFunction((n) => document.querySelectorAll(".attachment").length === n + 1, nAtt, { timeout: 60000 });
    assert((await lastAttachedText()).name === "fixture_masked.md", "guard on again: native picker goes through masking");

    // 10. Drop path: with no file input on the page the extension drops the file on the composer area.
    await page.evaluate(() => window.disableFileInput());
    await idle();
    await page.setInputFiles("#maskroom-file", fixture);
    nAtt = (await attachments()).length;
    await page.waitForFunction((n) => document.querySelectorAll(".attachment").length === n + 1, nAtt, { timeout: 60000 });
    assert((await lastAttachedText()).name === "fixture_masked.md", "synthetic drop attached the masked file");

    // 11. Adopt a session created elsewhere (the staging page).
    const chatSession = await page.evaluate(() => document.querySelector("#maskroom-bar").dataset.session);
    const other = await sw.evaluate(async (base) => {
      const s = await (await fetch(`${base}/api/session`, { method: "POST" })).json();
      const m = await (await fetch(`${base}/api/mask`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: "Ruwan Jayawardena, NIC 912345678V", session_id: s.session_id }) })).json();
      return { id: s.session_id, token: m.findings.find((f) => f.entity === "PERSON").token };
    }, SERVER);
    page.once("dialog", (d) => d.accept(other.id));
    await page.click('#maskroom-bar [data-act="adopt"]');
    await page.waitForFunction((p) => document.querySelector("#maskroom-bar .mr-meta").textContent.includes(p), other.id.slice(0, 6), { timeout: 10000 });
    await page.evaluate((tok) => window.addReply(`Adopted: ${tok} is the borrower.`), other.token);
    await page.waitForFunction(() => [...document.querySelectorAll(".msg.assistant")].some((m) => m.textContent.includes("Ruwan Jayawardena")), null, { timeout: 10000 });
    assert(true, "adopted session's tokens restore on screen");
    // back to the chat's own session for the file round trips below
    page.once("dialog", (d) => d.accept(chatSession));
    await page.click('#maskroom-bar [data-act="adopt"]');
    await page.waitForFunction((id) => document.querySelector("#maskroom-bar").dataset.session === id, chatSession, { timeout: 10000 });

    // ------------------------------------------------ generated files
    const saved = () => sw.evaluate(() => self.__savedFiles.map((f) => ({ name: f.name, text: atob(f.b64) })));
    const waitSaved = (n) => sw.evaluate(async (n) => { for (let i = 0; i < 300 && self.__savedFiles.length < n; i++) await new Promise((r) => setTimeout(r, 200)); return self.__savedFiles.length; }, n).then((len) => assert(len >= n, `worker saved file #${n}`));
    const tokensOf = Object.keys(vault);
    const nimalTok = Object.entries(vault).find(([, v]) => v === "Nimal Perera")[0];

    // 12. Unmask file from the bar: the masked Markdown attached earlier comes back with names.
    const mdDir = fs.mkdtempSync(path.join(require("os").tmpdir(), "mr-md-"));
    const mdPath = path.join(mdDir, "fixture_masked.md");
    fs.writeFileSync(mdPath, await page.evaluate(async () => { const f = window.attached.find((a) => a.name.endsWith(".md")); return f.text(); }));
    let nSaved = (await saved()).length;
    await idle();
    await page.setInputFiles("#maskroom-unmask-file", mdPath);
    await waitSaved(nSaved + 1);
    let last = (await saved()).pop();
    assert(last.name === "fixture_masked_restored.md" && last.text.includes("Nimal Perera") && last.text.includes("Kumari Bandara") && !/TOK_/.test(last.text), `Unmask file restored the Markdown and saved it as *_restored.md (got ${last.name}: ${last.text.slice(0, 80)})`);

    // 13. Download intercept (blob): a generated report is saved restored; the token version is cancelled.
    nSaved = (await saved()).length;
    await page.evaluate((tok) => { window.reportText = `# Overdue\n\n| customer | note |\n|---|---|\n| ${tok.toLowerCase().replace(/_/g, " ")} | late |\n| TOK_PERSON_00000000 | unknown |\n`; }, nimalTok);
    const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#dlreport")]);
    await waitSaved(nSaved + 1);
    last = (await saved()).pop();
    assert(last.name === "report_restored.md" && last.text.includes("Nimal Perera") && last.text.includes("TOK_PERSON_00000000") && last.text.includes("<!-- maskroom:"), "intercepted blob download restored, unresolved token kept and noted");
    const fail = await dl.failure();
    assert(fail === "canceled" || (await dl.path()) !== null, `original download was cancelled or (tiny file) had already completed: ${fail}`);
    // Default mode: the token copy must not survive on disk.
    // Downloads the page started (the token copy) have no byExtensionId; the files
    // this extension saves (restored) do. That is the reliable signal — under
    // Playwright the on-disk names are opaque GUIDs.
    const claudeDownloads = () => sw.evaluate(async () => (await chrome.downloads.search({}))
      .filter((d) => !d.byExtensionId && (String(d.url).startsWith("blob:https://claude") || String(d.url).startsWith("https://claude")))
      .map((d) => ({ exists: d.exists, url: String(d.url).slice(0, 25) })));
    const liveTokenCopies = async () => (await claudeDownloads()).filter((d) => d.exists);
    assert((await liveTokenCopies()).length === 0, "default mode: masked token copy removed from disk");

    // 13b. keepMasked (demo) on: the token copy is kept beside the restored one.
    await sw.evaluate(() => chrome.storage.local.set({ keepMasked: true }));
    await page.waitForTimeout(200);
    nSaved = (await saved()).length;
    await Promise.all([page.waitForEvent("download").catch(() => null), page.click("#dlreport")]);
    await waitSaved(nSaved + 1);
    assert((await saved()).pop().name === "report_restored.md", "keepMasked: restored file still saved");
    assert((await liveTokenCopies()).length >= 1, "keepMasked: token copy kept on disk");
    await sw.evaluate(() => chrome.storage.local.set({ keepMasked: false }));
    await page.waitForTimeout(200);

    // 14. Download intercept (https link served by claude.ai): a masked workbook comes back restored.
    const maskedXlsx = await page.evaluate(async () => { const f = window.attached.find((a) => a.name.endsWith(".xlsx")); return Array.from(new Uint8Array(await f.arrayBuffer())); });
    await page.route("https://claude.ai/files/report.xlsx", (r) => r.fulfill({ status: 200, contentType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", body: Buffer.from(maskedXlsx) }));
    nSaved = (await saved()).length;
    await Promise.all([page.waitForEvent("download").catch(() => null), page.click("#dlxlsx")]);
    await waitSaved(nSaved + 1);
    last = (await saved()).pop();
    assert(last.name === "report_restored.xlsx", "intercepted https download saved as report_restored.xlsx");
    const xlsxPath = path.join(require("os").tmpdir(), `mr-restored-${Date.now()}.xlsx`);
    fs.writeFileSync(xlsxPath, Buffer.from(last.text, "binary"));
    const py = process.env.MASKROOM_PY || path.resolve(__dirname, "../../pii_env/bin/python");
    const cells = require("child_process").execFileSync(py, ["-c", `import openpyxl,sys;ws=openpyxl.load_workbook(sys.argv[1]).active;print([c.value for c in ws[2]])`, xlsxPath]).toString();
    assert(cells.includes("Nimal Perera") && cells.includes("853421234V") && !cells.includes("TOK_"), "restored workbook holds the original values");

    // 15. Intercept off: the download arrives untouched, with tokens.
    await sw.evaluate(() => chrome.storage.local.set({ interceptDownloads: false }));
    await page.waitForTimeout(300);
    nSaved = (await saved()).length;
    const [raw] = await Promise.all([page.waitForEvent("download"), page.click("#dlreport")]);
    const rawText = fs.readFileSync(await raw.path(), "utf8");
    assert(raw.suggestedFilename() === "report.md" && rawText.includes(nimalTok.toLowerCase().replace(/_/g, " ")), "intercept off: original token file downloaded as-is");
    await page.waitForTimeout(800);
    assert((await saved()).length === nSaved, "intercept off: nothing extra saved");
    await sw.evaluate(() => chrome.storage.local.set({ interceptDownloads: true }));

    console.log("\nALL EXTENSION E2E CHECKS PASSED");
  } finally {
    await ctx.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
