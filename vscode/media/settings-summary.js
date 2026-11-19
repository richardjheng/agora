// 同時供 webview 與 node 測試使用：組裝面板收合區塊的設定摘要，以及開合狀態存取邏輯。
(function (root) {
  const TIER_DEFS = [
    { id: "light", label: "輕量" },
    { id: "standard", label: "標準" },
    { id: "heavy", label: "重度" },
  ];

  function availableEfforts(modelId, catalog) {
    const models = (catalog && Array.isArray(catalog.models)) ? catalog.models : [];
    const defModel = (catalog && catalog.default && catalog.default.model) || "";
    const effectiveModel = models.find((m) => m && m.id === (modelId || defModel));
    if (effectiveModel && Array.isArray(effectiveModel.efforts) && effectiveModel.efforts.length) {
      return effectiveModel.efforts;
    }
    return (catalog && Array.isArray(catalog.efforts)) ? catalog.efforts : [];
  }

  /**
   * 討論時用的模型摘要（僅呈現專案設定，不推算執行結果）。
   * 例：
   * - 未指定模型與思考深度：「CLI 預設（opus · high）」或「CLI 預設（gemini-4-argon-high）」或「CLI 預設」
   * - 指定模型與思考深度：「gpt-5 · high」
   * - 只指定模型：「gpt-5」
   * - 只指定思考深度：「CLI 預設（opus） · high」或「CLI 預設 · high」
   */
  function discussionSummary(setting, catalog) {
    const s = setting || {};
    const def = (catalog && catalog.default) || {};
    const model = s.model || "";
    const effort = s.effort || "";
    const hasEfforts = availableEfforts(model, catalog).length > 0;

    if (!model && !effort) {
      const defParts = [];
      if (def.model) defParts.push(def.model);
      if (hasEfforts && def.effort) defParts.push(def.effort);
      return defParts.length ? `CLI 預設（${defParts.join(" · ")}）` : "CLI 預設";
    }

    const parts = [];
    if (model) {
      parts.push(model);
    } else {
      parts.push(def.model ? `CLI 預設（${def.model}）` : "CLI 預設");
    }
    if (effort) {
      parts.push(effort);
    }
    return parts.join(" · ");
  }

  /**
   * 接手工作時用的模型摘要（僅呈現專案設定，不推算執行結果）。
   * 例：「輕量 opus/low · 標準 opus/medium · 重度 opus/high · 備用 2」
   * 未設定的等級寫「沿用討論設定」。
   */
  function tiersSummary(setting) {
    const tiers = (setting && setting.tiers) || {};
    const parts = [];
    let fallbackCount = 0;

    for (const lvl of TIER_DEFS) {
      const cfg = tiers[lvl.id] || null;
      const model = cfg && cfg.model ? cfg.model : "";
      const effort = cfg && cfg.effort ? cfg.effort : "";
      const value = model ? (effort ? `${model}/${effort}` : model) : "沿用討論設定";
      parts.push(`${lvl.label} ${value}`);
      if (cfg && cfg.fallback && cfg.fallback.model) {
        fallbackCount += 1;
      }
    }

    if (fallbackCount > 0) {
      parts.push(`備用 ${fallbackCount}`);
    }
    return parts.join(" · ");
  }

  /** 從 vscode.getState() 還原開合狀態表（僅保留布林值）。 */
  function loadOpenState(saved) {
    const map = {};
    const src = saved && typeof saved === "object" && saved.openSections && typeof saved.openSections === "object"
      ? saved.openSections : {};
    for (const [k, v] of Object.entries(src)) {
      if (typeof v === "boolean") map[k] = v;
    }
    return map;
  }

  /** 查詢區塊是否展開：有明確開／關紀錄時以紀錄為準，從未操作才採用 defaultOpen。 */
  function isSectionOpen(openMap, key, defaultOpen) {
    if (openMap && Object.prototype.hasOwnProperty.call(openMap, key) && typeof openMap[key] === "boolean") {
      return openMap[key];
    }
    return Boolean(defaultOpen);
  }

  /** 記錄使用者明確開／關狀態。 */
  function setSectionOpen(openMap, key, isOpen) {
    openMap[key] = Boolean(isOpen);
    return openMap;
  }

  /** 產生合併既有 webview state 的持久化物件。 */
  function mergeOpenState(prevState, openMap) {
    const base = prevState && typeof prevState === "object" ? { ...prevState } : {};
    base.openSections = { ...openMap };
    return base;
  }

  const api = {
    TIER_DEFS,
    availableEfforts,
    discussionSummary,
    tiersSummary,
    loadOpenState,
    isSectionOpen,
    setSectionOpen,
    mergeOpenState,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AgoraSettingsSummary = api;
})(globalThis);
