const test = require("node:test");
const assert = require("node:assert");
const { coreMatches } = require("../versions");

test("內建核心要版本與 build 雜湊都相同", () => {
  const build = { version: "1.2.3", hash: "abc" };
  assert.strictEqual(coreMatches({ version: "1.2.3", kind: "bundled", build: "abc" }, build, "1.2.3"), true);
  assert.strictEqual(coreMatches({ version: "1.2.3", kind: "bundled", build: "old" }, build, "1.2.3"), false);
  assert.strictEqual(coreMatches({ version: "1.2.3", kind: "bundled", build: "abc" }, null, "1.2.3"), false);
  assert.strictEqual(coreMatches({ version: "1.2.2", kind: "bundled", build: "abc" }, build, "1.2.3"), false);
});

test("原始碼核心只比版本；來源不明或舊版核心（沒有 core 欄位）一律不算符合", () => {
  assert.strictEqual(coreMatches({ version: "1.2.3", kind: "source" }, null, "1.2.3"), true);
  assert.strictEqual(coreMatches({ version: "1.2.4", kind: "source" }, null, "1.2.3"), false);
  assert.strictEqual(coreMatches({ version: "1.2.3", kind: "unknown" }, null, "1.2.3"), false);
  assert.strictEqual(coreMatches(null, null, "1.2.3"), false);
});
