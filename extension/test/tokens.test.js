// Run with:  node --test extension/test/tokens.test.js
const test = require("node:test");
const assert = require("node:assert");
const { buildIndex, restore } = require("../tokens.js");

const idx = buildIndex({
  TOK_PERSON_8B584CCF: "Nimal Perera",
  TOK_PERSON_1A2B3C4D: "Kumari Bandara",
  TOK_LK_NIC_0F0F0F0F: "853421234V",
  TOK_US_SSN_ABCDEF01: "123-45-6789",
});

test("exact tokens restore", () => {
  const r = restore("Call TOK_PERSON_8B584CCF re TOK_LK_NIC_0F0F0F0F.", idx);
  assert.strictEqual(r.text, "Call Nimal Perera re 853421234V.");
  assert.strictEqual(r.report.restored, 2);
  assert.deepStrictEqual(r.report.unresolved, []);
});

for (const m of ["tok_person_8b584ccf", "TOK PERSON 8B584CCF", "TOK-PERSON-8B584CCF",
                 "TOK\\_PERSON\\_8B584CCF", "TOK_PERSON_8B584C", "TOK_PERSON_8B584CCF12"]) {
  test(`mangled token restores: ${m}`, () => {
    const r = restore(`According to ${m}, overdue.`, idx);
    assert.strictEqual(r.text, "According to Nimal Perera, overdue.");
    assert.strictEqual(r.report.fuzzy.length, 1);
  });
}

test("multi-word entity types", () => {
  assert.strictEqual(restore("tok lk nic 0f0f0f0f / TOK US SSN ABCDEF01", idx).text,
                     "853421234V / 123-45-6789");
});

test("unknown or ambiguous tokens are reported, not guessed", () => {
  const r = restore("See TOK_PERSON_99999999.", idx);
  assert.strictEqual(r.text, "See TOK_PERSON_99999999.");
  assert.strictEqual(r.report.unresolved.length, 1);
  const two = buildIndex({ TOK_PERSON_ABCDEF12: "A", TOK_PERSON_ABCDEF34: "B" });
  assert.strictEqual(restore("TOK_PERSON_ABCDEF", two).text, "TOK_PERSON_ABCDEF");
});

test("token-free text is untouched", () => {
  const s = "stock levels and tokens of appreciation";
  assert.strictEqual(restore(s, idx).text, s);
});
