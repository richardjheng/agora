// 擴充套件自己從 GitHub Releases 更新。
// 用 vsix 安裝的擴充套件不在 Marketplace / Open VSX 上，VSCode（與 Antigravity 等分支）的自動更新不會管它，
// 所以由擴充套件查最新的 Release、下載附檔的 .vsix，再交給 workbench.extensions.installExtension 安裝。
// 只依賴 Node 內建模組，不依賴 vscode 模組（方便測試）。

const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

const REPO = "richardjheng/agora";
const LATEST_URL = `https://api.github.com/repos/${REPO}/releases/latest`;
const RELEASES_PAGE = `https://github.com/${REPO}/releases`;

/** "v0.2.14" → [0, 2, 14]；不是版本號時回傳 null */
function parseVersion(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(v || "").trim());
  return m ? m.slice(1).map(Number) : null;
}

/** a 比 b 新回傳正數、舊回傳負數、相同（或無法比較）回傳 0 */
function compareVersions(a, b) {
  const x = parseVersion(a);
  const y = parseVersion(b);
  if (!x || !y) return 0;
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

/** 從 Release 取出版本與 .vsix 附檔；草稿、預先發行、沒有 .vsix 或版本號不對時回傳 null */
function releaseInfo(release) {
  if (!release || release.draft || release.prerelease) return null;
  const version = parseVersion(release.tag_name);
  const asset = (release.assets || []).find((a) => /\.vsix$/i.test(a.name || ""));
  if (!version || !asset || !asset.browser_download_url) return null;
  return { version: version.join("."), name: asset.name, url: asset.browser_download_url, page: release.html_url || RELEASES_PAGE };
}

/** GET 一個網址（跟隨轉址，GitHub 的附檔會轉到 objects.githubusercontent.com）；回傳 response 串流 */
function request(url, { accept = "application/json", redirects = 5, timeout = 30000, get = https.get } = {}) {
  return new Promise((resolve, reject) => {
    const req = get(url, { headers: { "User-Agent": "agora-vscode", Accept: accept }, timeout }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location) {
        res.resume();
        if (redirects <= 0) return reject(new Error("轉址次數過多"));
        return resolve(request(new URL(res.headers.location, url).toString(), { accept, redirects: redirects - 1, timeout, get }));
      }
      if (res.statusCode !== 200) {
        res.resume();
        const limited = res.statusCode === 403 && res.headers["x-ratelimit-remaining"] === "0";
        return reject(new Error(limited ? "GitHub API 查詢次數已達上限，稍後再試" : `HTTP ${res.statusCode}`));
      }
      resolve(res);
    });
    req.on("timeout", () => req.destroy(new Error("連線逾時")));
    req.on("error", reject);
  });
}

/** 查詢最新的 Release；回傳 releaseInfo（沒有可用的 .vsix 時為 null） */
async function fetchLatest(opts) {
  const res = await request(LATEST_URL, { accept: "application/vnd.github+json", ...opts });
  let body = "";
  res.setEncoding("utf8");
  for await (const chunk of res) body += chunk;
  return releaseInfo(JSON.parse(body));
}

/** 下載 .vsix 到暫存資料夾，回傳檔案路徑；先寫 .part 再改名，下載中斷不會留下不完整的 .vsix */
async function download(info, dir = os.tmpdir(), opts) {
  const file = path.join(dir, path.basename(info.name));
  const part = `${file}.part`;
  const res = await request(info.url, { accept: "application/octet-stream", timeout: 120000, ...opts });
  try {
    await new Promise((resolve, reject) => {
      const out = fs.createWriteStream(part);
      res.on("error", reject);
      res.on("aborted", () => reject(new Error("下載中斷")));
      out.on("error", reject);
      out.on("finish", resolve);
      res.pipe(out);
    });
    fs.renameSync(part, file);
  } catch (e) {
    fs.rmSync(part, { force: true });
    throw e;
  }
  return file;
}

module.exports = { REPO, RELEASES_PAGE, parseVersion, compareVersions, releaseInfo, request, fetchLatest, download };
