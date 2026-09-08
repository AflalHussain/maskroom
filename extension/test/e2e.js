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

    const page = await ctx.newPage();
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

    console.log("\nALL EXTENSION E2E CHECKS PASSED");
  } finally {
    await ctx.close();
  }
})().catch((e) => { console.error(e); process.exit(1); });
