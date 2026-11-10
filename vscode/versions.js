// 一鍵更新用：判斷實際執行的 Agora 核心是不是這次要的版本。只依賴純資料（方便測試）。

/**
 * core 是 `agora parties --json` 的 core：{version, root, kind: "bundled" | "source" | "unknown", build}；
 * 舊版核心沒有這個欄位，呼叫者傳入 kind 為 unknown 的物件。bundledBuild 是擴充套件 core/BUILD.json 的內容。
 * - bundled：版本相同，且 build 標記等於擴充套件內建核心的雜湊
 * - source（例如 pip install -e 的 repo）：只能比版本，不能證明內容與 Release 相同
 * - unknown：一律不算符合，由使用者選擇
 */
function coreMatches(core, bundledBuild, extensionVersion) {
  if (!core || core.version !== extensionVersion) return false;
  if (core.kind === "bundled") return !!bundledBuild && core.build === bundledBuild.hash;
  return core.kind === "source";
}

module.exports = { coreMatches };
