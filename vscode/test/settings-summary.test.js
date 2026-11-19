const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const AgoraQuota = require("../media/quota-display");
const AgoraSettingsSummary = require("../media/settings-summary");
const {
  discussionSummary,
  tiersSummary,
  loadOpenState,
  isSectionOpen,
  setSectionOpen,
  mergeOpenState,
} = AgoraSettingsSummary;

test("discussionSummary: CLI 預設（含／不含預設模型與思考深度）", () => {
  const claudeCatalog = {
    models: [{ id: "opus", label: "Opus" }],
    efforts: ["low", "medium", "high"],
    default: { model: "opus", effort: "high" },
  };
  assert.equal(discussionSummary({}, claudeCatalog), "CLI 預設（opus · high）");
  assert.equal(discussionSummary({ model: "", effort: "" }, claudeCatalog), "CLI 預設（opus · high）");
  assert.equal(discussionSummary({}, { models: [], efforts: [], default: {} }), "CLI 預設");
  assert.equal(discussionSummary(null, null), "CLI 預設");
});

test("discussionSummary: 指定模型與有無思考深度", () => {
  const codexCatalog = {
    models: [{ id: "gpt-5", label: "GPT-5", efforts: ["low", "medium", "high"] }],
    efforts: [],
    default: { model: "gpt-5", effort: "medium" },
  };
  assert.equal(discussionSummary({ model: "gpt-5", effort: "high" }, codexCatalog), "gpt-5 · high");
  assert.equal(discussionSummary({ model: "gpt-5", effort: "" }, codexCatalog), "gpt-5");
  assert.equal(discussionSummary({ model: "", effort: "high" }, codexCatalog), "CLI 預設（gpt-5） · high");
  assert.equal(discussionSummary({ model: "", effort: "high" }, { models: [], efforts: ["high"], default: {} }), "CLI 預設 · high");
});

test("discussionSummary: Antigravity 無 efforts 時不顯示思考深度", () => {
  const agyCatalog = {
    models: [
      { id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)" },
      { id: "gemini-4-neon-medium", label: "Gemini 4 Neon (Medium)" },
    ],
    efforts: [],
    default: { model: "gemini-4-argon-high", effort: "high" },
  };
  assert.equal(discussionSummary({}, agyCatalog), "CLI 預設（gemini-4-argon-high）");
  assert.equal(discussionSummary({ model: "gemini-4-neon-medium" }, agyCatalog), "gemini-4-neon-medium");
});

test("tiersSummary: 完整設定、未設等級沿用、無推理強度、忽略舊欄位", () => {
  assert.equal(
    tiersSummary({
      tiers: {
        light: { model: "opus", effort: "low" },
        standard: { model: "opus", effort: "medium" },
        heavy: { model: "opus", effort: "high" },
      },
    }),
    "輕量 opus/low · 標準 opus/medium · 重度 opus/high",
  );

  assert.equal(
    tiersSummary({
      tiers: {
        light: { model: "haiku" },
        heavy: { model: "opus", effort: "high", fallback: { model: "sonnet", effort: "medium" } },
      },
    }),
    "輕量 haiku · 標準 沿用討論設定 · 重度 opus/high",
  );

  assert.equal(
    tiersSummary({}),
    "輕量 沿用討論設定 · 標準 沿用討論設定 · 重度 沿用討論設定",
  );
});

test("open/close state: 預設收合、各 AI 獨立、持久化與還原", () => {
  const openMap = loadOpenState(undefined);
  assert.equal(isSectionOpen(openMap, "claude.discussion", false), false);
  assert.equal(isSectionOpen(openMap, "claude.tiers", false), false);
  assert.equal(isSectionOpen(openMap, "codex.discussion", false), false);

  // 展開 claude.discussion 不影響 codex.discussion
  setSectionOpen(openMap, "claude.discussion", true);
  assert.equal(isSectionOpen(openMap, "claude.discussion", false), true);
  assert.equal(isSectionOpen(openMap, "codex.discussion", false), false);

  // 模擬 webview setState -> 重新載入 getState，並保留其他 state 欄位
  const saved = mergeOpenState({ otherKey: 123 }, openMap);
  assert.equal(saved.otherKey, 123);
  const restored = loadOpenState(saved);
  assert.equal(isSectionOpen(restored, "claude.discussion", false), true);
  assert.equal(isSectionOpen(restored, "claude.tiers", false), false);
  assert.equal(isSectionOpen(restored, "codex.discussion", false), false);
});

function createMockNode(tag) {
  const node = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    className: "",
    attrs: {},
    listeners: {},
    children: [],
    open: false,
    setAttribute(k, v) {
      this.attrs[k] = v;
      if (k === "open") this.open = true;
    },
    addEventListener(type, fn) {
      if (!this.listeners[type]) this.listeners[type] = [];
      this.listeners[type].push(fn);
    },
    append(...items) {
      for (const item of items) this.children.push(item);
    },
    appendChild(item) {
      this.children.push(item);
      return item;
    },
    replaceChildren(...items) {
      this.children = [...items];
    },
    toggleTo(nextOpen) {
      this.open = Boolean(nextOpen);
      for (const fn of this.listeners.toggle || []) {
        fn({ target: this, currentTarget: this });
      }
    },
  };
  return node;
}

function findAll(root, pred, out = []) {
  if (!root || typeof root !== "object") return out;
  if (pred(root)) out.push(root);
  for (const c of root.children || []) findAll(c, pred, out);
  return out;
}

function runPanel(initialPersistedState) {
  let persisted = initialPersistedState;
  const app = createMockNode("div");
  const windowListeners = {};
  const posted = [];
  const sandbox = {
    console,
    Date,
    Number,
    Math,
    Object,
    Array,
    String,
    Boolean,
    Set,
    RegExp,
    AgoraQuota,
    AgoraSettingsSummary,
    acquireVsCodeApi() {
      return {
        getState: () => persisted,
        setState: (s) => { persisted = s; },
        postMessage: (m) => posted.push(m),
      };
    },
    document: {
      getElementById: (id) => (id === "app" ? app : null),
      createElement: createMockNode,
    },
    window: {
      addEventListener(type, fn) {
        if (!windowListeners[type]) windowListeners[type] = [];
        windowListeners[type].push(fn);
      },
    },
  };
  const code = fs.readFileSync(path.join(__dirname, "../media/panel.js"), "utf8");
  vm.runInNewContext(code, sandbox);
  return {
    app,
    posted,
    getPersisted: () => persisted,
    pushState(state) {
      for (const fn of windowListeners.message || []) {
        fn({ data: { type: "state", state } });
      }
    },
  };
}

function sampleState() {
  return {
    workspace: "D:/repo",
    agoraFound: true,
    install: { state: "installed" },
    errors: {},
    names: { claude: "Claude Code", antigravity: "Antigravity", codex: "Codex" },
    order: ["claude", "antigravity", "codex"],
    catalog: {
      fetched_at: 1700000000,
      claude: { models: [{ id: "opus", label: "Opus" }], efforts: ["low", "medium", "high"], default: { model: "opus", effort: "high" } },
      antigravity: { models: [{ id: "gemini-4-argon-high", label: "Gemini 4 Argon (High)" }], efforts: [], default: { model: "gemini-4-argon-high" } },
      codex: { models: [{ id: "gpt-5", label: "GPT-5", efforts: ["low", "medium", "high"] }], efforts: [], default: { model: "gpt-5", effort: "medium" } },
    },
    parties: {
      claude: {
        enabled: true,
        model: "",
        effort: "",
        share: 1,
        tiers: {
          heavy: { model: "opus", effort: "high" },
        },
      },
      antigravity: {
        enabled: false,
        model: "",
        effort: "",
        share: 1,
        tiers: {},
      },
      codex: {
        enabled: true,
        model: "gpt-5",
        effort: "high",
        share: 1,
        tiers: {},
      },
    },
  };
}

test("panel.js: 預設收合、重繪與重載保持、各 AI 獨立、停用 AI 保留討論區塊、忽略舊開合鍵", () => {
  const saved = { openSections: { "claude.heavy": true } };
  const env1 = runPanel(saved);
  env1.pushState(sampleState());

  const cards1 = findAll(env1.app, (n) => n.tagName === "SECTION");
  assert.equal(cards1.length, 3);

  // Claude: 討論與接手工作區塊預設收合
  const claudeDisc1 = findAll(cards1[0], (n) => n.className.includes("discussion-section"))[0];
  const claudeTiers1 = findAll(cards1[0], (n) => n.className.includes("tiers-section"))[0];
  assert.equal(claudeDisc1.open, false);
  assert.equal(claudeTiers1.open, false);
  assert.equal(findAll(claudeTiers1, (n) => n.tagName === "DETAILS").length, 1);
  assert.equal(findAll(claudeTiers1, (n) => n.className === "tier-item").length, 3);

  claudeTiers1.toggleTo(false);
  assert.deepEqual(env1.getPersisted(), saved);

  // Antigravity（停用）：沒有接手工作區塊，但討論區塊仍存在且可收合
  const agyDisc1 = findAll(cards1[1], (n) => n.className.includes("discussion-section"))[0];
  const agyTiers1 = findAll(cards1[1], (n) => n.className.includes("tiers-section"));
  assert.ok(agyDisc1);
  assert.equal(agyDisc1.open, false);
  assert.equal(agyTiers1.length, 0);

  // Codex：預設收合
  const codexDisc1 = findAll(cards1[2], (n) => n.className.includes("discussion-section"))[0];
  assert.equal(codexDisc1.open, false);

  // 使用者展開 Claude 討論與接手工作區塊
  claudeDisc1.toggleTo(true);
  claudeTiers1.toggleTo(true);

  // 重繪後保持狀態，且不影響 Codex
  env1.pushState(sampleState());
  const cards2 = findAll(env1.app, (n) => n.tagName === "SECTION");
  assert.equal(findAll(cards2[0], (n) => n.className.includes("discussion-section"))[0].open, true);
  assert.equal(findAll(cards2[0], (n) => n.className.includes("tiers-section"))[0].open, true);
  assert.equal(findAll(cards2[2], (n) => n.className.includes("discussion-section"))[0].open, false);

  // 模擬 webview 重新載入：使用剛剛 setState 存下的資料
  const env2 = runPanel(env1.getPersisted());
  env2.pushState(sampleState());
  const cards3 = findAll(env2.app, (n) => n.tagName === "SECTION");
  assert.equal(findAll(cards3[0], (n) => n.className.includes("discussion-section"))[0].open, true);
  assert.equal(findAll(cards3[0], (n) => n.className.includes("tiers-section"))[0].open, true);
  assert.equal(findAll(cards3[2], (n) => n.className.includes("discussion-section"))[0].open, false);
});
