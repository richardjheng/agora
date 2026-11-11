const test = require("node:test");
const assert = require("node:assert");
const { modelLabel, tierLabel, relayModelLabel } = require("../modellabel");

const cases = [
  ["opus", "opus", "模型 opus"],
  [null, "gpt-6-astra", "模型 gpt-6-astra（CLI 預設）"],
  ["opus", "claude-opus-5-5", "請求 opus → 回報 claude-opus-5-5"],
  ["gemini-4-argon-high", null, "請求 gemini-4-argon-high（未回報）"],
  [null, null, "CLI 預設（未回報）"],
  ["", "", "CLI 預設（未回報）"],
];

for (const [requested, reported, expected] of cases) {
  test(`模型標示 ${JSON.stringify([requested, reported])}`, () => {
    assert.strictEqual(modelLabel(requested, reported), expected);
  });
}

test("缺少欄位與單邊空字串視為未指定／未回報", () => {
  assert.strictEqual(modelLabel(undefined, undefined), "CLI 預設（未回報）");
  assert.strictEqual(modelLabel("", "gpt-6-astra"), "模型 gpt-6-astra（CLI 預設）");
  assert.strictEqual(modelLabel("opus", ""), "請求 opus（未回報）");
  assert.strictEqual(modelLabel("opus", undefined), "請求 opus（未回報）");
});

test("等級名稱與未指定等級", () => {
  assert.strictEqual(tierLabel("light"), "輕量");
  assert.strictEqual(tierLabel("standard"), "標準");
  assert.strictEqual(tierLabel("heavy"), "重度");
  assert.strictEqual(tierLabel(null), "未指定");
  assert.strictEqual(tierLabel(undefined), "未指定");
  assert.strictEqual(tierLabel(""), "未指定");
  assert.strictEqual(tierLabel("custom"), "custom");
});

test("第一輪完成後凍結模型，第二輪執行中仍保留第一輪的 CLI 預設來源", () => {
  const relay = { model: "X", effort: "high", rounds: 1,
    last_model_requested: null, last_effort_requested: "high", last_model_reported: "X" };
  assert.strictEqual(relayModelLabel(relay),
    "目前請求：X（推理強度 high） · 最近完成一輪回報：模型 X（CLI 預設）（推理強度 high）");
  assert.strictEqual(relayModelLabel({ ...relay, rounds: 2, last_model_requested: "X" }),
    "目前請求：X（推理強度 high） · 最近完成一輪回報：模型 X（推理強度 high）");
});

test("第一輪執行中尚無完成輪次，不把目前請求當作回報", () => {
  assert.strictEqual(relayModelLabel({ model: "X", effort: "high", rounds: 0 }),
    "目前請求：X（推理強度 high） · 最近完成一輪回報：尚無完成的輪次");
  assert.strictEqual(relayModelLabel({ model: null, rounds: 0, last_model_requested: null,
    last_effort_requested: null, last_model_reported: null }),
  "目前請求：CLI 預設模型 · 最近完成一輪回報：尚無完成的輪次");
});

test("最近一輪未回報與舊資料不沿用目前模型或首次回報", () => {
  assert.strictEqual(relayModelLabel({ model: "B", rounds: 2, last_model_requested: "A",
    last_model_reported: null, model_reported: "first" }),
  "目前請求：B · 最近完成一輪回報：請求 A（未回報）");
  assert.strictEqual(relayModelLabel({ model: "B", rounds: 1, model_reported: "first" }),
    "目前請求：B · 最近完成一輪回報：CLI 預設（未回報）");
});

test("歷史請求與推理強度和目前設定分開標示", () => {
  assert.strictEqual(relayModelLabel({ model: "B", effort: "high", rounds: 1,
    last_model_requested: "A", last_effort_requested: "low", last_model_reported: "C" }),
  "目前請求：B（推理強度 high） · 最近完成一輪回報：請求 A → 回報 C（推理強度 low）");
  assert.strictEqual(relayModelLabel({ rounds: 1, last_model_requested: null, last_model_reported: null }),
    "目前請求：CLI 預設模型 · 最近完成一輪回報：CLI 預設（未回報）");
});
