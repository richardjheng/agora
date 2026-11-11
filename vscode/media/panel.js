// 「AI 與模型」面板：勾選參與的 AI、選模型與推理強度、顯示額度。狀態由擴充套件推送，變更送回擴充套件執行 agora parties。
(function () {
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  let state = null;
  const openFallbacks = new Set();

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

  function singleQuotaRow(pctVal, resetTs, label, threshold, fullLabel, extraTitle) {
    const pct = Math.max(0, Math.min(100, pctVal));
    const level = pct < threshold ? "low" : pct < 25 ? "mid" : "ok";
    const fill = el("div", { class: `fill ${level}` });
    fill.style.width = `${pct}%`;
    const resetStr = fmtReset(resetTs);
    const titleLabel = fullLabel || label;
    const labelPrefix = titleLabel ? `${titleLabel}：` : "";
    const title = `${labelPrefix}剩餘 ${pct.toFixed(1)}%，${resetStr} 重置${extraTitle || ""}`;
    return el("div", { class: "quota", title },
      label ? el("span", { class: "quota-label" }, label) : null,
      el("div", { class: "bar" }, fill),
      el("span", { class: "quota-text" }, `${Math.round(pct)}% · ${resetStr} 重置`));
  }

  function quotaBar(u, threshold) {
    if (!u) return el("div", { class: "quota muted" }, state.quota ? "額度：無資料" : "額度：查詢中…");
    if (u.blocked) {
      return el("div", { class: "quota blocked", title: u.blocked.message || u.blocked.reason },
        `⛔ 無法使用（${u.blocked.reason}，${fmtReset(u.blocked.at)} 偵測）`);
    }
    const group = quotaRows(u, threshold);
    if (u.updated_at && Date.now() / 1000 - u.updated_at > 2 * 3600) {   // 與 quota.STALE_AFTER 相同
      group.appendChild(el("div", { class: "quota muted" }, `資料 ${Math.floor((Date.now() / 1000 - u.updated_at) / 3600)} 小時前更新，可能過期`));
    }
    return group;
  }

  function quotaRows(u, threshold) {
    const winKeys = u.windows ? Object.keys(u.windows) : [];
    const poolSummary = u.pools
      ? Object.entries(u.pools).map(([k, v]) => `${v.label || k} ${Math.round(v.remaining_pct)}%`).join("、")
      : "";
    const extraTitle = poolSummary ? `（額度池：${poolSummary}）` : "";
    if (winKeys.length > 1) {
      const rows = winKeys.map((k) => {
        const w = u.windows[k];
        const full = (w.full_label || w.label || k) + (u.pools && w.active ? "（目前模型）" : "");
        return singleQuotaRow(w.remaining_pct, w.resets_at, w.label || k, threshold, full, extraTitle);
      });
      return el("div", { class: "quota-group" }, ...rows);
    }
    const label = (winKeys.length === 1 && u.windows[winKeys[0]].label) || (u.window_label && u.window ? u.window_label : null);
    const full = (winKeys.length === 1 && u.windows[winKeys[0]].full_label) || u.label || label;
    const row = singleQuotaRow(u.remaining_pct, u.resets_at, label, threshold, full, extraTitle);
    return el("div", { class: "quota-group" }, row);
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

    // 推理強度：Codex 依所選模型支援的等級；Antigravity 的強度已包含在模型名稱裡
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
      effortRow = el("label", { class: "row" }, el("span", { class: "label" }, "推理強度"),
        select(effortOptions, setting.effort || "", (v) => vscode.postMessage({ type: "set", party: p, field: "effort", value: v }), disabled));
    }

    // 等級區塊：輕量／標準／重度，各列模型下拉與推理強度，選「沿用目前模型」表示清除；可展開設定替代模型（fallback）
    let tiersSection = null;
    if (!disabled) {
      const TIER_DEFS = [
        { id: "light", label: "輕量" },
        { id: "standard", label: "標準" },
        { id: "heavy", label: "重度" },
      ];
      const tiers = setting.tiers || {};

      const tierItems = TIER_DEFS.map((lvl) => {
        const tierCfg = tiers[lvl.id] || null;
        const curTierModel = tierCfg ? (tierCfg.model || "") : "";
        const curTierEffort = tierCfg ? (tierCfg.effort || "") : "";
        const curTierFb = tierCfg ? tierCfg.fallback : null;
        const curFbModel = curTierFb ? (curTierFb.model || "") : "";
        const curFbEffort = curTierFb ? (curTierFb.effort || "") : "";

        // 等級模型選項：沿用目前模型 + catalog 模型 + 自訂值 + 自訂…
        const tModelOptions = [{ value: "", label: "沿用目前模型" }];
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

        // 等級推理強度（所選模型有 efforts 才顯示）
        const effectiveTierModel = models.find((m) => m.id === curTierModel);
        const tierEfforts = (effectiveTierModel && effectiveTierModel.efforts && effectiveTierModel.efforts.length)
          ? effectiveTierModel.efforts : (curTierModel ? (cat.efforts || []) : []);
        let tEffortSelect = null;
        if (tierEfforts.length && curTierModel) {
          const tEffortOptions = [{ value: "", label: "預設強度" }].concat(tierEfforts.map((e) => ({ value: e, label: e })));
          if (curTierEffort && !tierEfforts.includes(curTierEffort)) {
            tEffortOptions.push({ value: curTierEffort, label: `${curTierEffort}（不支援）` });
          }
          tEffortSelect = select(tEffortOptions, curTierEffort, (v) => {
            vscode.postMessage({ type: "tier", party: p, level: lvl.id, value: v ? `${curTierModel}:${v}` : curTierModel });
          }, disabled);
          tEffortSelect.className = "tier-effort";
        }

        // 替代模型（fallback）選項
        const fbModelOptions = [{ value: "", label: "無替代模型" }];
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
          const fbEffortOptions = [{ value: "", label: "預設強度" }].concat(fbEfforts.map((e) => ({ value: e, label: e })));
          if (curFbEffort && !fbEfforts.includes(curFbEffort)) {
            fbEffortOptions.push({ value: curFbEffort, label: `${curFbEffort}（不支援）` });
          }
          fbEffortSelect = select(fbEffortOptions, curFbEffort, (v) => {
            vscode.postMessage({ type: "tierFallback", party: p, level: lvl.id, value: v ? `${curFbModel}:${v}` : curFbModel });
          }, disabled);
          fbEffortSelect.className = "tier-effort";
        }

        const fbKey = `${p}.${lvl.id}`;
        const hasFallback = !!curFbModel;
        const isFbOpen = openFallbacks.has(fbKey) || hasFallback;
        const fbSummaryText = hasFallback ? `：${curFbModel}${curFbEffort ? ` (${curFbEffort})` : ""}` : "";
        const fbDetails = el("details", {
          class: "tier-fallback",
          open: isFbOpen,
          ontoggle: (e) => {
            if (e.target.open) openFallbacks.add(fbKey);
            else openFallbacks.delete(fbKey);
          },
        },
          el("summary", {}, `替代模型（fallback）${fbSummaryText}`),
          el("div", { class: `tier-fallback-content${fbEffortSelect ? "" : " no-effort"}` },
            fbModelSelect,
            fbEffortSelect));

        return el("div", { class: "tier-item" },
          el("div", { class: `tier-main${tEffortSelect ? "" : " no-effort"}` },
            el("span", { class: "tier-label" }, lvl.label),
            tModelSelect,
            tEffortSelect),
          fbDetails);
      });

      tiersSection = el("div", { class: "tiers-block" },
        el("div", { class: "tiers-title" }, "工作分派等級"),
        ...tierItems);
    }

    // 分配比例：0 表示不自動分派給它；其他值只當 token 用量的參考目標（自動挑選依額度池剩餘 %）
    let shareRow = null;
    if (!disabled) {
      const enabled = state.order.filter((x) => state.parties[x].enabled);
      const total = enabled.reduce((sum, x) => sum + state.parties[x].share, 0);
      const tokens = (x) => (state.usage && state.usage[x] && state.usage[x].tokens_7d) || 0;
      const used = enabled.reduce((sum, x) => sum + tokens(x), 0);
      const target = setting.share === 0 ? "不自動分派" : total ? `token 目標 ${Math.round(setting.share / total * 100)}%` : "";
      const actual = setting.share === 0 ? "" : ` · 近 ${state.shareDays} 天實際 ${used ? `${Math.round(tokens(p) / used * 100)}%` : "-"}`;
      const input = el("input", {
        type: "number", min: "0", step: "1", value: String(setting.share), class: "share",
        title: "0 表示只參與討論、不自動分派給它；其他值是 token 用量的參考目標，不影響自動挑選（依額度剩餘 %）",
        onchange: (e) => {
          const v = e.target.value.trim();
          if (/^\d+$/.test(v)) vscode.postMessage({ type: "set", party: p, field: "share", value: v });
          else e.target.value = String(setting.share);
        },
      });
      shareRow = el("label", { class: "row" }, el("span", { class: "label" }, "分配比例"),
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

      usageSection = el("div", { class: "usage-block", title: "近 7 天用量統計" },
        el("div", { class: "usage-title" }, "近 7 天用量"),
        el("div", { class: "usage-row" }, tokenLine),
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
      el("label", { class: "row" }, el("span", { class: "label" }, "模型"), modelSelect),
      effortRow,
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
    claude: "被 Agora 呼叫時能否用 Task 派出 Claude Code 的內建子代理（general-purpose、Explore 等）。預設關閉（評估中），關閉時直接禁用該工具。",
    antigravity: "被 Agora 呼叫時能否用 invoke_subagent 等工具派出 Antigravity 的子代理。預設關閉（評估中），關閉時由專案裡的 hook 擋下（需已安裝或更新到此專案）。",
    codex: "被 Agora 呼叫時能否用 spawn_agent 派出 Codex 的子代理。預設關閉（評估中）。Codex 目前無法強制關閉：關閉時只在提示中要求不要使用，若仍使用會標記在逐字稿。",
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
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshQuota" }) }, "更新額度"),
        el("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "refreshModels" }) }, "更新模型清單")));
      children.push(el("p", { class: "muted small" },
        "勾選表示同意該 AI 參與此專案接下來的討論與分派：進行中的討論串在下一次輪到發言時就會加入它（先讀完前面的討論），"
        + "取消勾選後不再請它回覆；已在背景進行的分派工作不會換人，用「新增討論串」時指定參與者的討論串也不會自動加入。"
        + "設定存在 .agora/config.json；未參與的 AI 不會被呼叫，也不會成為接手方。"
        + "各 AI 可依工作等級（輕量／標準／重度）設定對應的模型與替代模型（fallback）；分派時依等級對應的額度池剩餘 % 挑選接手方。"
        + "內建子代理預設關閉（評估中），被呼叫時盡量由主模型完成。"
        + "分配比例為 0 表示不自動分派給它，其他值只是 token 用量的參考目標；指定接手方時不受限制。"));
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
