// 模型與工作等級的共用顯示文字；不依賴 VS Code。
function modelLabel(requested, reported) {
  requested = requested || null;
  reported = reported || null;
  if (requested && reported) {
    return requested === reported ? `模型 ${reported}` : `請求 ${requested} → 回報 ${reported}`;
  }
  if (reported) return `模型 ${reported}（CLI 預設）`;
  if (requested) return `請求 ${requested}（未回報）`;
  return "CLI 預設（未回報）";
}

function tierLabel(tier) {
  switch (tier) {
    case "light": return "輕量";
    case "standard": return "標準";
    case "heavy": return "重度";
    default: return tier || "未指定";
  }
}

function relayModelLabel(relay) {
  let current = relay.model || "CLI 預設模型";
  if (relay.effort) current += `（推理強度 ${relay.effort}）`;
  let recent = modelLabel(relay.last_model_requested, relay.last_model_reported);
  if (relay.last_effort_requested) recent += `（推理強度 ${relay.last_effort_requested}）`;
  if (!relay.rounds && !relay.last_model_requested && !relay.last_effort_requested && !relay.last_model_reported) {
    recent = "尚無完成的輪次";
  }
  return `目前請求：${current} · 最近完成一輪回報：${recent}`;
}

module.exports = { modelLabel, tierLabel, relayModelLabel };
