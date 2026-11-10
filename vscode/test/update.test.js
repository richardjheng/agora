const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { PassThrough } = require("stream");
const { compareVersions, releaseInfo, request, download } = require("../update");

/** 假的 https.get：routes 是 {網址: {status, headers, body}} */
function fakeGet(routes, seen = []) {
  return (url, opts, cb) => {
    seen.push({ url, headers: opts.headers });
    const route = routes[url] || { status: 404 };
    const res = new PassThrough();
    res.statusCode = route.status || 200;
    res.headers = route.headers || {};
    const req = new PassThrough();
    process.nextTick(() => {
      cb(res);
      res.end(route.body || "");
    });
    return req;
  };
}

const RELEASE = {
  tag_name: "v0.2.15",
  html_url: "https://github.com/richardjheng/agora/releases/tag/v0.2.15",
  assets: [
    { name: "notes.txt", browser_download_url: "https://example.com/notes.txt" },
    { name: "agora-0.2.15.vsix", browser_download_url: "https://github.com/richardjheng/agora/releases/download/v0.2.15/agora-0.2.15.vsix" },
  ],
};

test("比較版本號：逐段比數字，v 前綴可有可無，無法解析時視為相同", () => {
  assert.ok(compareVersions("v0.2.15", "0.2.14") > 0);
  assert.ok(compareVersions("0.2.9", "0.2.14") < 0);
  assert.ok(compareVersions("1.0.0", "0.99.99") > 0);
  assert.strictEqual(compareVersions("v0.2.14", "0.2.14"), 0);
  assert.strictEqual(compareVersions("nightly", "0.2.14"), 0);
});

test("從 Release 取出版本與 .vsix 附檔", () => {
  assert.deepStrictEqual(releaseInfo(RELEASE), {
    version: "0.2.15",
    name: "agora-0.2.15.vsix",
    url: RELEASE.assets[1].browser_download_url,
    page: RELEASE.html_url,
  });
});

test("草稿、預先發行、沒有 .vsix、版本號不對的 Release 不使用", () => {
  assert.strictEqual(releaseInfo({ ...RELEASE, draft: true }), null);
  assert.strictEqual(releaseInfo({ ...RELEASE, prerelease: true }), null);
  assert.strictEqual(releaseInfo({ ...RELEASE, assets: [RELEASE.assets[0]] }), null);
  assert.strictEqual(releaseInfo({ ...RELEASE, tag_name: "latest" }), null);
  assert.strictEqual(releaseInfo(null), null);
});

test("跟隨轉址並帶 User-Agent（GitHub API 必須）", async () => {
  const seen = [];
  const get = fakeGet({
    "https://a.test/x": { status: 302, headers: { location: "https://b.test/y" } },
    "https://b.test/y": { status: 200, body: "ok" },
  }, seen);
  const res = await request("https://a.test/x", { get });
  let body = "";
  for await (const chunk of res) body += chunk;
  assert.strictEqual(body, "ok");
  assert.deepStrictEqual(seen.map((s) => s.url), ["https://a.test/x", "https://b.test/y"]);
  assert.ok(seen.every((s) => s.headers["User-Agent"]));
});

test("HTTP 錯誤與查詢次數上限會說明原因", async () => {
  await assert.rejects(request("https://a.test/missing", { get: fakeGet({}) }), /HTTP 404/);
  const limited = fakeGet({ "https://a.test/api": { status: 403, headers: { "x-ratelimit-remaining": "0" } } });
  await assert.rejects(request("https://a.test/api", { get: limited }), /查詢次數已達上限/);
});

test("下載 .vsix 到指定資料夾，失敗時不留下不完整的檔案", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-update-"));
  try {
    const info = { name: "agora-0.2.15.vsix", url: "https://a.test/agora.vsix" };
    const file = await download(info, tmp, { get: fakeGet({ [info.url]: { body: "PK-vsix" } }) });
    assert.strictEqual(path.dirname(file), tmp);
    assert.match(path.basename(file), /^agora-0\.2\.15-[0-9a-f-]+\.vsix$/);
    assert.strictEqual(fs.readFileSync(file, "utf8"), "PK-vsix");

    await assert.rejects(download({ ...info, name: "agora-0.2.16.vsix" }, tmp, { get: fakeGet({}) }), /HTTP 404/);
    assert.deepStrictEqual(fs.readdirSync(tmp), [path.basename(file)]);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("多視窗同時下載同版使用唯一檔名，保留各自內容與既有檔案", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-update-"));
  try {
    const info = { name: "agora-0.2.15.vsix", url: "https://a.test/agora.vsix" };
    fs.writeFileSync(path.join(tmp, info.name), "existing");
    const files = await Promise.all(["first", "second"].map((body) =>
      download(info, tmp, { get: fakeGet({ [info.url]: { body } }) })));
    assert.notStrictEqual(files[0], files[1]);
    assert.deepStrictEqual(files.map((file) => fs.readFileSync(file, "utf8")), ["first", "second"]);
    assert.strictEqual(fs.readFileSync(path.join(tmp, info.name), "utf8"), "existing");
    assert.ok(fs.readdirSync(tmp).every((name) => name.endsWith(".vsix")));
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("串流中斷會關閉輸出並移除 .part，不影響另一個下載", async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-update-"));
  try {
    const info = { name: "agora.vsix", url: "https://a.test/agora.vsix" };
    const brokenGet = (url, opts, cb) => {
      const res = new PassThrough();
      res.statusCode = 200;
      res.headers = {};
      process.nextTick(() => {
        cb(res);
        res.write("partial");
        setImmediate(() => res.destroy(new Error("connection lost")));
      });
      return new PassThrough();
    };
    const [file] = await Promise.all([
      download(info, tmp, { get: fakeGet({ [info.url]: { body: "complete" } }) }),
      assert.rejects(download(info, tmp, { get: brokenGet }), /connection lost/),
    ]);
    assert.deepStrictEqual(fs.readdirSync(tmp), [path.basename(file)]);
    assert.strictEqual(fs.readFileSync(file, "utf8"), "complete");
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test("下載完成但改名失敗也清除殘檔", async (t) => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "agora-update-"));
  try {
    t.mock.method(fs, "renameSync", () => { throw new Error("rename failed"); });
    const info = { name: "agora.vsix", url: "https://a.test/agora.vsix" };
    await assert.rejects(download(info, tmp, { get: fakeGet({ [info.url]: { body: "complete" } }) }), /rename failed/);
    assert.deepStrictEqual(fs.readdirSync(tmp), []);
  } finally {
    t.mock.restoreAll();
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
