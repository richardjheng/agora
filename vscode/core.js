// 內建的 Agora 核心：擴充套件附帶 core/（打包時由 scripts/bundle-core.js 產生），啟動時同步到 ~/.agora/core。
// 用固定位置而不是擴充套件資料夾（含版本號、每次更新都會變），skills 與 Antigravity 權限裡的指令路徑才不會因更新失效。
// 只依賴 Node 內建模組，不依賴 vscode 模組（方便測試）。

const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");

const ENTRIES = ["agora.py", "agoralib", "skills", "README.md"];
const LOCK_TTL = 2 * 60 * 1000;

/** 與打包時共用：固定入口順序、資料夾依名稱排序、忽略 Python 快取。 */
function coreHash(dir) {
  const hash = crypto.createHash("sha256");
  function visit(rel) {
    const full = path.join(dir, rel);
    if (fs.statSync(full).isDirectory()) {
      for (const name of fs.readdirSync(full).sort()) {
        if (name === "__pycache__" || name.endsWith(".pyc")) continue;
        visit(path.join(rel, name));
      }
    } else {
      hash.update(rel.replace(/\\/g, "/")).update(fs.readFileSync(full));
    }
  }
  for (const entry of ENTRIES) visit(entry);
  return hash.digest("hex").slice(0, 16);
}

function readBuild(dir) {
  try {
    return JSON.parse(fs.readFileSync(path.join(dir, "BUILD.json"), "utf8"));
  } catch {
    return null;
  }
}

function usable(dir) {
  try {
    const build = readBuild(dir);
    return !!build && fs.statSync(path.join(dir, "agora.py")).isFile()
      && coreHash(dir) === build.hash;
  } catch {
    return false;
  }
}

// 所有取得／接手／釋放都先獨占短期 guard；避免過期檢查與刪除之間換了持有者。
function acquireLock(file, log) {
  const token = crypto.randomUUID();
  const guardFile = `${file}.reclaim`;
  function owns(target, owner) {
    try { return JSON.parse(fs.readFileSync(target, "utf8")).token === owner; }
    catch { return false; }
  }
  function expired(target) {
    const stat = fs.statSync(target);
    let time = stat.mtimeMs;
    try {
      const recorded = JSON.parse(fs.readFileSync(target, "utf8")).time;
      if (typeof recorded === "number" && Number.isFinite(recorded)) time = recorded;
    } catch { /* 未寫完的鎖以建立時間判斷 */ }
    return Date.now() - time > LOCK_TTL;
  }
  function withGuard(action) {
    const owner = crypto.randomUUID();
    let fd;
    try {
      try { fd = fs.openSync(guardFile, "wx"); }
      catch (e) {
        if (e.code !== "EEXIST" || !expired(guardFile)) throw e;
        fs.unlinkSync(guardFile);
        fd = fs.openSync(guardFile, "wx");
      }
      fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, time: Date.now(), token: owner }));
      fs.closeSync(fd);
      fd = undefined;
      return action();
    } finally {
      if (fd !== undefined) {
        try { fs.closeSync(fd); } catch { /* 保留原始錯誤 */ }
      }
      if (owns(guardFile, owner)) fs.unlinkSync(guardFile);
    }
  }
  try {
    withGuard(() => {
      if (fs.existsSync(file)) {
        if (!expired(file)) throw new Error("核心同步鎖被占用");
        fs.unlinkSync(file);
        log("已接手超過 2 分鐘的核心同步鎖");
      }
      const fd = fs.openSync(file, "wx");
      try {
        fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, time: Date.now(), token }));
      } finally {
        fs.closeSync(fd);
      }
    });
    return {
      owns: () => owns(file, token),
      release() {
        try { withGuard(() => { if (owns(file, token)) fs.unlinkSync(file); }); }
        catch (e) { log(`無法清除核心同步鎖：${e.message}`); }
      },
    };
  } catch (e) {
    log(`無法取得核心同步鎖：${e.message}`);
    return null;
  }
}

/**
 * 把 bundled（擴充套件的 core/）同步到 dest（預設 ~/.agora/core），回傳可用的核心位置；沒有內建核心時回傳 undefined。
 * 內容雜湊相同就不複製；先複製到暫存資料夾再換上，換不上時（檔案被占用）沿用舊的。
 */
function syncCore(bundled, dest = path.join(os.homedir(), ".agora", "core"), log = () => {}) {
  const want = readBuild(bundled);
  if (!want || !fs.existsSync(path.join(bundled, "agora.py"))) return undefined;
  const fallback = () => usable(dest) ? dest : bundled;
  const parent = path.dirname(dest);
  let lock;
  let tmp;
  let backup;
  let moved = false;
  function remove(dir) {
    if (!dir) return;
    try { fs.rmSync(dir, { recursive: true, force: true }); }
    catch (e) { log(`無法清除 ${dir}：${e.message}`); }
  }
  function cleanBackups() {
    const prefix = `${path.basename(dest)}.backup-`;
    try {
      for (const name of fs.readdirSync(parent)) {
        if (name.startsWith(prefix) && /^[A-Za-z0-9]{6}$/.test(name.slice(prefix.length))) {
          remove(path.join(parent, name));
        }
      }
    } catch (e) { log(`無法清理核心備份：${e.message}`); }
  }
  try {
    fs.mkdirSync(parent, { recursive: true });
    lock = acquireLock(path.join(parent, "core.lock"), log);
    if (!lock) return fallback();
    const have = readBuild(dest);
    if (have && have.hash === want.hash && usable(dest)) {
      cleanBackups();
      return dest;
    }
    tmp = fs.mkdtempSync(`${dest}.tmp-`);
    fs.cpSync(bundled, tmp, { recursive: true });
    const copied = readBuild(tmp);
    if (!copied || copied.hash !== want.hash || copied.version !== want.version || coreHash(tmp) !== want.hash) {
      throw new Error("暫存核心內容雜湊驗證失敗");
    }
    if (!lock.owns()) throw new Error("核心同步鎖已由其他程序接手");
    // 先獨占備份資料夾，再把舊核心移入；不覆蓋任何既有備份。
    if (fs.existsSync(dest)) {
      backup = fs.mkdtempSync(`${dest}.backup-`);
      fs.renameSync(dest, path.join(backup, "core"));
      moved = true;
    }
    fs.renameSync(tmp, dest);
    tmp = undefined;
    moved = false;
    // 切換已完成，清理備份失敗不應觸發回滾。
    cleanBackups();
    backup = undefined;
    log(`已更新 Agora 核心 ${want.version} → ${dest}`);
    return dest;
  } catch (e) {
    log(`無法更新 ${dest}：${e.message}`);
    if (moved) {
      try {
        fs.renameSync(path.join(backup, "core"), dest);
        moved = false;
      } catch (restoreError) {
        log(`無法還原核心；舊核心保留於 ${backup}：${restoreError.message}`);
      }
    }
    return fallback();
  } finally {
    remove(tmp);
    // 還原失敗時保留唯一的舊核心，供後續復原。
    if (!moved) remove(backup);
    if (lock) lock.release();
  }
}

module.exports = { syncCore, readBuild, coreHash };
