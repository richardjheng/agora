// 「AI 與模型」面板：勾選參與的 AI、選討論與接手工作模型（含思考深度）、顯示額度。狀態由擴充套件推送，變更送回擴充套件執行 agora parties。
(function () {
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  let state = null;
  const openSections = AgoraSettingsSummary.loadOpenState(vscode.getState());

  function persistOpenState() {
    vscode.setState(AgoraSettingsSummary.mergeOpenState(vscode.getState(), openSections));
  }

  function makeDetails({ key, defaultOpen, className, summary, children }) {
    const initialOpen = AgoraSettingsSummary.isSectionOpen(openSections, key, defaultOpen);
    let lastOpen = initialOpen;
    return el("details", {
      class: className,
      open: initialOpen,
      ontoggle: (e) => {
        if (e.target !== e.currentTarget) return;
        const nextOpen = Boolean(e.target.open);
        if (nextOpen === lastOpen) return;
        lastOpen = nextOpen;
        AgoraSettingsSummary.setSectionOpen(openSections, key, nextOpen);
        persistOpenState();
      },
    }, summary, ...children);
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) node.append(c.nodeType ? c : String(c));
    }
    return node;
  }

  function fmtReset(ts) {
    if (!ts) return "未知";
    const d = new Date(ts * 1000);
    const pad = (n) => String(n).padStart(2, "0");
    const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    return d.toDateString() === new Date().toDateString() ? time : `${d.getMonth() + 1}/${d.getDate()} ${time}`;
  }

  function singleQuotaRow(row, threshold) {
    const known = typeof row.remaining_pct === "number" && Number.isFinite(row.remaining_pct);
    const pct = known ? Math.max(0, Math.min(100, row.remaining_pct)) : 0;
    const level = pct < threshold ? "low" : pct < 25 ? "mid" : "ok";
    const meter = el("progress", { class: `quota-meter ${level}`, max: 100,
      value: known ? pct : null, "aria-label": `${row.label}剩餘額度` });
    const title = `${row.label}：剩餘 ${AgoraQuota.percent(row.remaining_pct)}`;
    return el("div", { class: `quota-detail${row.active ? " active" : ""}`, title },
      el("div", { class: "quota-heading" },
        el("span", { class: "quota-label" }, row.label),
        row.active ? el("span", { class: "quota-current" }, "目前模型") : null,
        el("strong", { class: `quota-value ${known && pct < threshold ? "low" : ""}` }, AgoraQuota.percent(row.remaining_pct))),
      known ? meter : null,
      el("div", { class: "muted small" }, row.resets_at ? `${fmtReset(row.resets_at)} 重置` : "重置時間未提供"));
  }

  function quotaBar(u, threshold) {
    if (!u) return el("div", { class: "quota muted" }, state.quota ? "額度：無資料" : "額度：查詢中…");
    const group = el("div", { class: "quota-group" },
      el("div", { class: "usage-title" }, "帳號剩餘額度"));
    if (u.blocked) {
      group.appendChild(el("div", { class: "quota blocked", title: u.blocked.message || u.blocked.reason },
        `⛔ 無法使用（${u.blocked.reason}，${fmtReset(u.blocked.at)} 偵測）`));
    } else {
      for (const row of AgoraQuota.rows(u)) group.appendChild(singleQuotaRow(row, threshold));
    }
    const stale = u.updated_at && Date.now() / 1000 - u.updated_at > 2 * 3600;
    group.appendChild(el("div", { class: `small ${stale ? "error" : "muted"}` },
      u.updated_at ? `額度更新：${fmtReset(u.updated_at)}${stale ? " · 可能過期" : ""}` : "額度更新時間未知"));
    return group;
  }

  function select(options, value, onChange, disabled) {
    const s = el("select", { disabled, onchange: (e) => onChange(e.target.value) },
      options.map((o) => el("option", { value: o.value, selected: o.value === value }, o.label)));
    return s;
  }

  function partyCard(p) {
    const names = state.names;
    const setting = state.parties[p];
    const cat = (state.catalog && state.catalog[p]) || { models: [], efforts: [], default: {} };
    const def = cat.default || {};
    const disabled = !setting.enabled;

    // 模型：CLI 預設 + 目錄 + 目前的自訂值 + 自訂…
    const models = cat.models || [];
    const modelOptions = [{ value: "", label: `CLI 預設${def.model ? `（${def.model}）` : ""}` }];
    for (const m of models) modelOptions.push({ value: m.id, label: m.label === m.id ? m.id : `${m.label} · ${m.id}` });
    if (setting.model && !models.some((m) => m.id === setting.model)) {
      modelOptions.push({ value: setting.model, label: `${setting.model}（自訂）` });
    }
    modelOptions.push({ value: "__custom__", label: "自訂…" });
    const modelSelect = select(modelOptions, setting.model || "", (v) => {
      if (v === "__custom__") vscode.postMessage({ type: "custom", party: p });
      else vscode.postMessage({ type: "set", party: p, field: "model", value: v });
    }, disabled);

    // 思考深度（原推理強度／--effort）：Codex 依所選模型支援的等級；Antigravity 的強度已包含在模型名稱裡
    const effectiveModel = models.find((m) => m.id === (setting.model || def.model));
    const efforts = (effectiveModel && effectiveModel.efforts && effectiveModel.efforts.length)
      ? effectiveModel.efforts : (cat.efforts || []);
    let effortRow = null;
    if (efforts.length) {
      const effortOptions = [{ value: "", label: `CLI 預設${def.effort ? `（${def.effort}）` : ""}` }]
        .concat(efforts.map((e) => ({ value: e, label: e })));
      if (setting.effort && !efforts.includes(setting.effort)) {
        effortOptions.push({ value: setting.effort, label: `${setting.effort}（此模型不支援）` });
      }
      effortRow = el("label", { class: "row", title: "推理強度／--effort" },
        el("span", { class: "label" }, "思考深度"),
        select(effortOptions, setting.effort || "", (v) => vscode.postMessage({ type: "set", party: p, field: "effort", value: v }), disabled));
    }

    const discSummary = AgoraSettingsSummary.discussionSummary(setting, cat);
    const discussionSection = makeDetails({
      key: `${p}.discussion`,
      defaultOpen: false,
      className: "settings-section discussion-section",
      summary: el("summary", { class: "settings-summary", title: `討論時用的模型：${discSummary}` },
        el("span", { class: "settings-summary-title" }, "討論時用的模型"),
        el("span", { class: "settings-summary-sep" }, "："),
        el("span", { class: "settings-summary-value" }, discSummary)),
      children: [
        el("div", { class: "settings-body" },
          el("div", { class: "muted small section-hint" },
            "Agora 叫它參與討論時使用；不影響你直接在對話框聊天。討論串或指令可覆蓋；分派時等級沒設定也會沿用。"),
          el("label", { class: "row" }, el("span", { class: "label" }, "模型"), modelSelect),
          el("div", { class: "muted small" }, `模型清單：${models.length} 項 · ${state.catalog && state.catalog.fetched_at ? fmtReset(state.catalog.fetched_at) + " 更新" : "更新時間未知"}`),
          effortRow),
      ],
    });

    // 接手工作時用的模型：輕量／標準／重度，各列模型下拉與思考深度，選「沿用討論設定」表示清除；可展開設定額度不足時的備用模型
    let tiersSection = null;
    if (!disabled) {
      const TIER_DEFS = AgoraSettingsSummary.TIER_DEFS;
      const tiers = setting.tiers || {};
      const fbRuleHint = "自動派工時會先檢查各 AI 此等級的主模型；全部不合格時才考慮備用模型，且備用模型也須通過額度檢查。";

      const tierItems = TIER_DEFS.map((lvl) => {
        const tierCfg = tiers[lvl.id] || null;
        const curTierModel = tierCfg ? (tierCfg.model || "") : "";
        const curTierEffort = tierCfg ? (tierCfg.effort || "") : "";
        const curTierFb = tierCfg ? tierCfg.fallback : null;
        const curFbModel = curTierFb ? (curTierFb.model || "") : "";
        const curFbEffort = curTierFb ? (curTierFb.effort || "") : "";

        // 等級模型選項：沿用討論設定 + catalog 模型 + 自訂值 + 自訂…
        const tModelOptions = [{ value: "", label: "沿用討論設定" }];
        for (const m of models) {
          tModelOptions.push({ value: m.id, label: m.label === m.id ? m.id : `${m.label} · ${m.id}` });
        }
        if (curTierModel && !models.some((m) => m.id === curTierModel)) {
          tModelOptions.push({ value: curTierModel, label: `${curTierModel}（自訂）` });
        }
        tModelOptions.push({ value: "__custom__", label: "自訂…" });

        const tModelSelect = select(tModelOptions, curTierModel, (v) => {
          if (v === "__custom__") {
            vscode.postMessage({ type: "customTier", party: p, level: lvl.id, fallback: false });
          } else if (v === "") {
            vscode.postMessage({ type: "tier", party: p, level: lvl.id, value: "default" });
          } else {
            const effModel = models.find((m) => m.id === v);
            const effs = (effModel && effModel.efforts && effModel.efforts.length) ? effModel.efforts : (cat.efforts || []);
            const eff = (curTierEffort && effs.includes(curTierEffort)) ? `:${curTierEffort}` : "";
            vscode.postMessage({ type: "tier", party: p, level: lvl.id, value: `${v}${eff}` });
          }
        }, disabled);

        // 等級思考深度（所選模型有 efforts 才顯示）
        const effectiveTierModel = models.find((m) => m.id === curTierModel);
        const tierEfforts = (effectiveTierModel && effectiveTierModel.efforts && effectiveTierModel.efforts.length)
          ? effectiveTierModel.efforts : (curTierModel ? (cat.efforts || []) : []);
        let tEffortSelect = null;
        if (tierEfforts.length && curTierModel) {
          const tEffortOptions = [{ value: "", label: "預設深度" }].concat(tierEfforts.map((e) => ({ value: e, label: e })));
          if (curTierEffort && !tierEfforts.includes(curTierEffort)) {
            tEffortOptions.push({ value: curTierEffort, label: `${curTierEffort}（不支援）` });
          }
          tEffortSelect = select(tEffortOptions, curTierEffort, (v) => {
            vscode.postMessage({ type: "tier", party: p, level: lvl.id, value: v ? `${curTierModel}:${v}` : curTierModel });
          }, disabled);
          tEffortSelect.className = "tier-effort";
          tEffortSelect.title = "推理強度／--effort";
        }

        // 額度不足時的備用模型（fallback）選項
        const fbModelOptions = [{ value: "", label: "無備用模型" }];
        for (const m of models) {
          fbModelOptions.push({ value: m.id, label: m.label === m.id ? m.id : `${m.label} · ${m.id}` });
        }
        if (curFbModel && !models.some((m) => m.id === curFbModel)) {
          fbModelOptions.push({ value: curFbModel, label: `${curFbModel}（自訂）` });
        }
        fbModelOptions.push({ value: "__custom__", label: "自訂…" });

        const fbModelSelect = select(fbModelOptions, curFbModel, (v) => {
          if (v === "__custom__") {
            vscode.postMessage({ type: "customTier", party: p, level: lvl.id, fallback: true });
          } else if (v === "") {
            vscode.postMessage({ type: "tierFallback", party: p, level: lvl.id, value: "default" });
          } else {
            const effModel = models.find((m) => m.id === v);
            const effs = (effModel && effModel.efforts && effModel.efforts.length) ? effModel.efforts : (cat.efforts || []);
            const eff = (curFbEffort && effs.includes(curFbEffort)) ? `:${curFbEffort}` : "";
            vscode.postMessage({ type: "tierFallback", party: p, level: lvl.id, value: `${v}${eff}` });
          }
        }, disabled);

        const effectiveFbModel = models.find((m) => m.id === curFbModel);
        const fbEfforts = (effectiveFbModel && effectiveFbModel.efforts && effectiveFbModel.efforts.length)
          ? effectiveFbModel.efforts : (curFbModel ? (cat.efforts || []) : []);
        let fbEffortSelect = null;
        if (fbEfforts.length && curFbModel) {
          const fbEffortOptions = [{ value: "", label: "預設深度" }].concat(fbEfforts.map((e) => ({ value: e, label: e })));
          if (curFbEffort && !fbEfforts.includes(curFbEffort)) {
            fbEffortOptions.push({ value: curFbEffort, label: `${curFbEffort}（不支援）` });
          }
          fbEffortSelect = select(fbEffortOptions, curFbEffort, (v) => {
            vscode.postMessage({ type: "tierFallback", party: p, level: lvl.id, value: v ? `${curFbModel}:${v}` : curFbModel });
          }, disabled);
          fbEffortSelect.className = "tier-effort";
          fbEffortSelect.title = "推理強度／--effort";
        }

        const fbKey = `${p}.${lvl.id}`;
        const hasFallback = !!curFbModel;
        const fbSummaryText = hasFallback
          ? `額度不足時的備用模型：${curFbModel}${curFbEffort ? ` (${curFbEffort})` : ""}`
          : "額度不足時的備用模型";
        const fbDetails = makeDetails({
          key: fbKey,
          defaultOpen: hasFallback,
          className: "tier-fallback",
          summary: el("summary", { title: `${fbSummaryText}。${fbRuleHint}` },
            el("span", { class: "tier-fallback-summary-text" }, fbSummaryText)),
          children: [
            el("div", { class: `tier-fallback-content${fbEffortSelect ? "" : " no-effort"}` },
              fbModelSelect,
              fbEffortSelect),
          ],
        });

        return el("div", { class: "tier-item" },
          el("div", { class: `tier-main${tEffortSelect ? "" : " no-effort"}` },
            el("span", { class: "tier-label" }, lvl.label),
            tModelSelect,
            tEffortSelect),
          fbDetails);
      });

      const tiersSummaryText = AgoraSettingsSummary.tiersSummary(setting);
      tiersSection = makeDetails({
        key: `${p}.tiers`,
        defaultOpen: false,
        className: "settings-section tiers-section",
        summary: el("summary", { class: "settings-summary", title: `接手工作時用的模型：${tiersSummaryText}` },
          el("span", { class: "settings-summary-title" }, "接手工作時用的模型"),
          el("span", { class: "settings-summary-sep" }, "："),
          el("span", { class: "settings-summary-value" }, tiersSummaryText)),
        children: [
          el("div", { class: "settings-body" },
            el("div", { class: "muted small section-hint" },
              "依工作複雜度、風險與驗證難度選擇（對應 assign --tier light/standard/heavy）"),
            ...tierItems),
        ],
      });
    }

    // 自動派工比例：0 表示不自動分派給它（只討論與審查）；其他值為自動派工件數權重（依比例輪流，不保證 token 比例）
    let shareRow = null;
    if (!disabled) {
      const enabled = state.order.filter((x) => state.parties[x].enabled);
      const total = enabled.reduce((sum, x) => sum + state.parties[x].share, 0);
      const autoDispatches = (x) => (state.usage && state.usage[x] && state.usage[x].dispatch_auto) || 0;
      const totalAuto = enabled.reduce((sum, x) => sum + autoDispatches(x), 0);
      const targetPct = total ? Math.round(setting.share / total * 100) : 0;
      const days = state.shareDays || 7;
      const actualPct = totalAuto ? `${Math.round(autoDispatches(p) / totalAuto * 100)}%` : "-";
      const target = `目標 ${targetPct}%`;
      const actual = ` · 近 ${days} 天自動派工 ${actualPct}`;
      const input = el("input", {
        type: "number", min: "0", step: "1", value: String(setting.share), class: "share",
        title: setting.share === 0
          ? "你的分配比例是 0：只負責討論、主持、交接與審查，不自己實作（也不透過子代理代做）；需要動手時寫工作單分派（預設不寫 --to）。要讓你實作，須由使用者明確指定。"
          : "自動派工件數的加權比例（依比例輪流，不保證 token 比例）；0 表示只負責討論、主持、交接與審查（不自己實作）",
        onchange: (e) => {
          const v = e.target.value.trim();
          if (/^\d+$/.test(v)) vscode.postMessage({ type: "set", party: p, field: "share", value: v });
          else e.target.value = String(setting.share);
        },
      });
      shareRow = el("label", { class: "row" }, el("span", { class: "label" }, "自動派工比例"),
        el("span", { class: "share-line" }, input, el("span", { class: "share-text" }, `${target}${actual}`)));
    }

    // 內建子代理：被 Agora 呼叫時能否啟動自己的子代理；預設改為關閉（評估中）
    let subagentRow = null;
    if (!disabled) {
      const hint = SUBAGENT_HINTS[p] || "";
      const isAllowed = setting.subagents === true;
      subagentRow = el("label", { class: "row subagents-row", title: hint },
        el("span", { class: "label" }, "子代理"),
        el("span", { class: "share-line" },
          el("input", {
            type: "checkbox", checked: isAllowed,
            onchange: (e) => vscode.postMessage({ type: "set", party: p, field: "subagents", value: e.target.checked ? "on" : "off" }),
          }),
          el("span", { class: "share-text" }, "允許使用內建子代理（預設關閉，評估中）")));
    }

    // Codex 接手工作時的沙箱（討論一律唯讀）：跑 west build 等需寫入工作區外快取時可設為完整存取
    let sandboxRow = null;
    if (!disabled && p === "codex") {
      const sandboxOptions = [
        { value: "", label: "只能寫工作區（workspace-write，預設）" },
        { value: "danger-full-access", label: "完整存取（danger-full-access）" },
        { value: "read-only", label: "唯讀（read-only）" },
      ];
      sandboxRow = el("label", { class: "row", title: "Codex 接手工作時的沙箱，討論一律唯讀；需執行 west build 等寫入工作區外快取／工具鏈的指令時請選完整存取" },
        el("span", { class: "label" }, "接手沙箱"),
        select(sandboxOptions, setting.sandbox === "workspace-write" ? "" : setting.sandbox || "", (v) => vscode.postMessage({ type: "set", party: p, field: "sandbox", value: v }), disabled));
    }

    // 近 7 天用量：已觀測 token（附「另有 N 次呼叫缺資料」）、接手輪數、自動／指定分派次數、子代理啟動次數（與未觀測次數）
    let usageSection = null;
    if (state.usage && state.usage[p]) {
      const u = state.usage[p];
      const missingText = u.calls_missing_tokens ? `（另有 ${u.calls_missing_tokens} 次呼叫缺資料）` : "";
      const tokenLine = `Tokens：已觀測 ${(u.tokens_7d || 0).toLocaleString()}${missingText}`;
      const roundLine = `接手：${u.rounds_7d || 0} 輪（自動分派 ${u.dispatch_auto || 0} 次 · 指定 ${u.dispatch_explicit || 0} 次）`;
      const subMissing = u.subagents_unobserved ? `（另有 ${u.subagents_unobserved} 次未觀測）` : "";
      const subLine = `子代理：啟動 ${u.subagents_started_7d || 0} 次${subMissing}`;

      usageSection = el("div", { class: "usage-block", title: "僅統計此專案經 Agora 呼叫時觀測到的用量" },
        el("div", { class: "usage-title" }, "此專案近 7 天用量"),
        el("div", { class: "usage-row" }, tokenLine),
        el("div", { class: "muted" }, "僅含 Agora 觀測紀錄；非帳號總用量。"),
        el("div", { class: "usage-row" }, roundLine),
        el("div", { class: "usage-row" }, subLine));
    }

    const quota = state.quota ? state.quota[p] : null;
    return el("section", { class: `card${disabled ? " disabled" : ""}` },
      el("label", { class: "head" },
        el("input", {
          type: "checkbox", checked: setting.enabled,
          onchange: (e) => vscode.postMessage({ type: "toggle", party: p, enabled: e.target.checked }),
        }),
        el("span", { class: "name" }, names[p]),
        disabled ? el("span", { class: "badge" }, "未參與") : null),
      quotaBar(quota, (state.quota && state.quota.threshold_pct) || 5),
      discussionSection,
      tiersSection,
      shareRow,
      subagentRow,
      sandboxRow,
      usageSection,
      cat.error ? el("div", { class: "error" }, `⚠ ${cat.error}`) : null);
  }

  // 版本：Agora 核心（agora --version）與擴充套件；核心以 pip install -e 安裝時會隨程式更新，擴充套件要重新打包安裝。
  // 「更新 Agora」：有新版 Release 就下載安裝、重新載入，再補齊此專案的檔案；「重新下載修復」同版也重新安裝
  function versionLine(v) {
    const ext = `擴充套件 v${v.extension}`;
    if (!state.workspace || !state.agoraFound) return el("p", { class: "muted small version" }, ext);
    if (!v.core) return el("p", { class: "muted small version" }, `Agora 核心版本：查詢中… · ${ext}`);
    const same = v.core === v.extension;
    const text = same ? `Agora v${v.core}` : `Agora 核心 v${v.core} · ${ext}`;
    const src = v.source;
    const hint = same ? null : src && src.kind === "source"
      ? `（核心來自原始碼 ${src.root}；按「更新 Agora」可選擇改用內建核心）`
      : "（版本不一致，按「更新 Agora」）";
    return el("div", { class: "version-row" },
      el("p", { class: `muted small version${same ? "" : " mismatch"}` }, text, hint ? el("span", { class: "hint" }, hint) : null),
      el("div", { class: "actions version-actions" },
        el("button", { class: "secondary", title: "查 GitHub Releases：有新版就下載安裝並重新載入，再補齊此專案的 Agora 檔案；沒有新版時只補齊",
          onclick: () => vscode.postMessage({ type: "update" }) }, "更新 Agora"),
        el("button", { class: "secondary", title: "同版也重新下載安裝，再補齊此專案的 Agora 檔案（檔案遺失或損壞時用）",
          onclick: () => vscode.postMessage({ type: "update", repair: true }) }, "重新下載修復")));
  }

  const SUBAGENT_HINTS = {
    claude: "被 Agora 呼叫時能否用 Task 派出 Claude Code 的內建子代理（general-purpose、Explore 等）。預設關閉（評估中），關閉時直接禁用該工具。開啟時子代理在同一 worktree 執行，這不是強制隔離。",
    antigravity: "被 Agora 呼叫時能否用 invoke_subagent 等工具派出 Antigravity 的子代理。預設關閉（評估中），關閉時由專案裡的 hook 擋下（需已安裝或更新到此專案）。開啟時子代理在同一 worktree 執行，這不是強制隔離。",
    codex: "被 Agora 呼叫時能否用 spawn_agent 派出 Codex 的子代理。預設關閉（評估中）。Codex 目前無法強制關閉：關閉時只在提示中要求不要使用，若仍使用會標記在逐字稿。開啟時子代理在同一 worktree 執行，這不是強制隔離。",
  };
  const SUBAGENT_OFF = { claude: "已禁用", antigravity: "由 hook 擋下", codex: "只能提示，無法強制" };

  function render() {
    if (!state) return;
    const children = [];
    if (!state.workspace) {
      children.push(el("p", { class: "muted" }, "請先開啟一個資料夾。"));
    } else if (!state.agoraFound) {
      children.push(el("div", { class: "notice" }, "找不到 Agora。請執行 pip install -e <Agora 資料夾>，或在設定中指定 agora.root。",
        el("button", { onclick: () => vscode.postMessage({ type: "settings" }) }, "開啟設定")));
    } else {
      const install = state.install;
      if (install && install.state !== "installed") {
        const text = {
          outdated: `此專案的 Agora 項目是舊版（${install.outdated.join("、")}），更新後 AI 才會使用目前版本的指令。`,
          partial: `此專案的 Agora 只裝了一部分（缺少：${install.missing.join("、")}），AI 可能不知道可以透過 Agora 找其他 AI。`,
          missing: "此專案尚未安裝 Agora：AI 不知道可以透過 Agora 找其他 AI 討論，也沒有額度提醒。安裝時三方的 skills、hook 與權限都會裝（在沒勾選的 AI 對話框下指令時，由它轉達給勾選的 AI）。",
        }[install.state];
        children.push(el("div", { class: "notice" }, text,
          el("button", { onclick: () => vscode.postMessage({ type: install.state === "missing" ? "install" : "update" }) },
            install.state === "missing" ? "安裝到此專案" : "更新")));
      }
      if (state.errors.parties) children.push(el("div", { class: "error" }, state.errors.parties));
      if (state.parties) {
        children.push(el("p", { class: "muted small" },
          "這裡的模型與等級只影響 Agora 發起的討論與分派；你直接在對話框對話時，模型請看各 AI 自己的介面。"));
        children.push(...state.order.map(partyCard));
      } else if (!state.errors.parties) {
        children.push(el("p", { class: "muted" }, "載入中…"));
      }
      if (state.errors.catalog) children.push(el("div", { class: "error" }, `模型清單：${state.errors.catalog}`));
      if (state.errors.quota) children.push(el("div", { class: "error" }, `額度：${state.errors.quota}`));
      children.push(el("div", { class: "actions" },
        el("button", { onclick: () => vscode.postMessage({ type: "newThread" }) }, "新增討論串"),
        el("button", { onclick: () => vscode.postMessage({ type: "refreshModelsAndQuota" }) }, "更新模型與額度"),
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshQuota" }) }, "更新額度"),
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshModels" }) }, "更新模型清單")));
      children.push(el("p", { class: "muted small" },
        "勾選表示同意該 AI 參與此專案接下來的討論與分派：進行中的討論串在下一次輪到發言時就會加入它（先讀完前面的討論），"
        + "取消勾選後不再請它回覆；已在背景進行的分派工作不會換人，用「新增討論串」時指定參與者的討論串也不會自動加入。"
        + "設定存在 .agora/config.json；未參與的 AI 不會被呼叫，也不會成為接手方。"
        + "各 AI 可在「討論時用的模型」與「接手工作時用的模型」（輕量／標準／重度，含額度不足時的備用模型）設定對應模型；分派時依比例輪流，額度只作為資格篩選。"
        + "分派工作時建立獨立 branch（agora/<分派 ID>）與 git worktree，接手方完成後由 Agora 核對並代做 commit（不自動 push/merge）。"
        + "內建子代理預設關閉（評估中），被呼叫時盡量由主模型完成；開啟時在同一 worktree 執行，由父代理整合驗證，這不是強制隔離。"
        + "自動派工比例為 0 表示只負責討論、主持、交接與審查（不自己實作），大於 0 為自動派工件數的加權比例（依比例輪流，不保證 token 比例）；指定接手方仍須符合額度資格，零權重接手方需使用者明確覆寫。"));
    }
    children.push(versionLine(state.versions || {}));
    app.replaceChildren(...children);
  }

  window.addEventListener("message", (e) => {
    if (e.data.type === "state") {
      state = e.data.state;
      render();
    }
  });
  vscode.postMessage({ type: "ready" });
})();
