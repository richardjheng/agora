const test = require("node:test");
const assert = require("node:assert/strict");
const { percent, rows } = require("../media/quota-display");

test("low quota retains decimals and missing quota is not zero", () => {
  assert.equal(percent(0.8), "0.8%");
  assert.equal(percent(10.27), "10.3%");
  assert.equal(percent(100), "100%");
  for (const value of [null, undefined, NaN, "20"]) assert.equal(percent(value), "未知");
});

test("all AGY pools remain visible even when windows only include Gemini", () => {
  const u = { window: "hydrogen", pools: {
    argon: { label: "Gemini 4 Argon", remaining_pct: 0.8 },
    hydrogen: { label: "Gemini 4 Hydrogen", remaining_pct: 10.3 },
    flash: { label: "Gemini 3.8 Flash", remaining_pct: 10.3 },
    claude: { label: "Claude", remaining_pct: 100 },
    oss: { label: "GPT-OSS 120B", remaining_pct: 100 },
  }, windows: { hydrogen: { label: "Hydrogen", active: true } } };
  const result = rows(u);
  assert.equal(result.length, 5);
  assert.equal(result[0].key, "hydrogen");
  assert.equal(result.filter(r => r.active).length, 1);
  assert.ok(result.some(r => r.key === "claude"));
  assert.ok(result.some(r => r.key === "oss"));
  assert.equal(Object.keys(u.pools)[0], "argon");
});

test("Claude time windows and Codex single weekly window preserve backend values", () => {
  assert.deepEqual(rows({ windows: { five: { label: "5 小時", remaining_pct: 100 },
    week: { label: "7 天", remaining_pct: 16 } } }).map(r => r.remaining_pct), [100, 16]);
  const r = rows({ remaining_pct: 60, windows: { primary: { label: "7 天", remaining_pct: 60 } } });
  assert.equal(r[0].label, "7 天");
  assert.equal(r[0].active, false);
  assert.deepEqual(rows(null), []);
});
