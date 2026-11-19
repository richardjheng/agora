// 同時供 extension host 與 webview 使用，保持額度標示一致。
(function (root) {
  function percent(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? `${Number(Math.max(0, Math.min(100, value)).toFixed(1))}%` : "未知";
  }

  function rows(usage) {
    if (!usage) return [];
    const pools = Object.entries(usage.pools || {});
    const windows = Object.entries(usage.windows || {});
    const entries = pools.length ? pools : windows;
    if (!entries.length) return [{ ...usage, label: usage.window_label || "剩餘額度" }];
    return entries.filter(([, value]) => value && typeof value === "object")
      .map(([key, value]) => ({ ...value, key,
        label: value.full_label || value.label || key,
        active: !!pools.length && key === usage.window,
      })).sort((a, b) => Number(b.active) - Number(a.active));
  }

  const api = { percent, rows };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.AgoraQuota = api;
})(globalThis);
