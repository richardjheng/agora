const test = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const vm = require("vm");
const { syncCore, coreHash, readBuild } = require("../core");

function makeBundle(dir, content = "print('agora')") {
  fs.mkdirSync(path.join(dir, "agoralib"), { recursive: true });
  fs.mkdirSync(path.join(dir, "skills", "codex"), { recursive: true });
  fs.writeFileSync(path.join(dir, "agora.py"), content);
  fs.writeFileSync(path.join(dir, "agoralib", "__init__.py"), '__version__ = "9.9.9"');
  fs.writeFileSync(path.join(dir, "skills", "codex", "AGENTS.md"), "instructions");
  fs.writeFileSync(path.join(dir, "README.md"), "readme");
  fs.writeFileSync(path.join(dir, "BUILD.json"), JSON.stringify({ version: "9.9.9", hash: coreHash(dir) }));
}

function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "agora-core-"));
  t.after(() => {
    t.mock.restoreAll();
    fs.rmSync(root, { recursive: true, force: true });
  });
  const bundled = path.join(root, "ext", "core");
  const dest = path.join(root, "home", ".agora", "core");
  makeBundle(bundled);
  return { root, bundled, dest, parent: path.dirname(dest) };
}

function installed(t) {
  const f = fixture(t);
  assert.strictEqual(syncCore(f.bundled, f.dest), f.dest);
  f.oldHash = coreHash(f.dest);
  makeBundle(f.bundled, "print('new')");
  return f;
}

test("同步內建核心到固定位置，內容相同時不重複複製，更新時換新", (t) => {
  const { bundled, dest, parent } = fixture(t);
  assert.strictEqual(syncCore(bundled, dest), dest);
  fs.writeFileSync(path.join(dest, "marker"), "x");
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.ok(fs.existsSync(path.join(dest, "marker")));
  makeBundle(bundled, "print('new')");
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.ok(!fs.existsSync(path.join(dest, "marker")));
  assert.strictEqual(coreHash(dest), coreHash(bundled));
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("沒有內建核心時回傳 undefined", (t) => {
  const { root, dest } = fixture(t);
  assert.strictEqual(syncCore(path.join(root, "none"), dest), undefined);
});

for (const damage of ["missing", "modified"]) {
  test("BUILD hash 相同但檔案 " + damage + " 會重新複製", (t) => {
    const { bundled, dest } = fixture(t);
    syncCore(bundled, dest);
    const target = path.join(dest, "skills", "codex", "AGENTS.md");
    if (damage === "missing") fs.unlinkSync(target);
    else fs.writeFileSync(target, "changed");
    assert.strictEqual(syncCore(bundled, dest), dest);
    assert.strictEqual(coreHash(dest), coreHash(bundled));
  });
}

test("暫存副本驗證失敗不替換舊核心，且移除暫存檔與鎖", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const copy = fs.cpSync;
  t.mock.method(fs, "cpSync", (src, target, opts) => {
    copy(src, target, opts);
    fs.writeFileSync(path.join(target, "README.md"), "corrupt copy");
  });
  const logs = [];
  assert.strictEqual(syncCore(bundled, dest, (s) => logs.push(s)), dest);
  assert.strictEqual(coreHash(dest), oldHash);
  assert.ok(logs.some((s) => s.includes("雜湊驗證失敗")));
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("複製中斷或 BUILD.json 損壞都保留舊核心", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const copy = fs.cpSync;
  for (const failure of ["interrupted", "manifest"]) {
    t.mock.method(fs, "cpSync", (src, target, opts) => {
      copy(src, target, opts);
      if (failure === "interrupted") throw new Error("copy interrupted");
      fs.writeFileSync(path.join(target, "BUILD.json"), "{}");
    });
    assert.strictEqual(syncCore(bundled, dest), dest);
    assert.strictEqual(coreHash(dest), oldHash);
    assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
    t.mock.restoreAll();
  }
});

for (const failAt of ["backup", "install"]) {
  test("改名失敗（" + failAt + "）保留或還原舊核心", (t) => {
    const { bundled, dest, parent, oldHash } = installed(t);
    const rename = fs.renameSync;
    t.mock.method(fs, "renameSync", (src, target) => {
      if ((failAt === "backup" && src === dest) || (failAt === "install" && src.startsWith(dest + ".tmp-"))) {
        throw new Error("EPERM: locked");
      }
      return rename(src, target);
    });
    assert.strictEqual(syncCore(bundled, dest), dest);
    assert.strictEqual(coreHash(dest), oldHash);
    assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
  });
}

test("首次安裝失敗回傳 bundled；還原失敗保留唯一舊備份", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const rename = fs.renameSync;
  t.mock.method(fs, "renameSync", (src, target) => {
    if (target === dest) throw new Error("destination locked");
    return rename(src, target);
  });
  assert.strictEqual(syncCore(bundled, dest), bundled);
  const backups = fs.readdirSync(parent).filter((n) => n.startsWith("core.backup-"));
  assert.strictEqual(backups.length, 1);
  assert.strictEqual(coreHash(path.join(parent, backups[0], "core")), oldHash);
  assert.strictEqual(syncCore(bundled, dest), bundled);
  assert.strictEqual(coreHash(path.join(parent, backups[0], "core")), oldHash);
});

test("刪備份失敗不回滾、不覆蓋舊備份，下次同步即使同 hash 也清理", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const existing = fs.mkdtempSync(dest + ".backup-");
  fs.writeFileSync(path.join(existing, "marker"), "keep");
  const rm = fs.rmSync;
  t.mock.method(fs, "rmSync", (target, opts) => {
    if (target.startsWith(dest + ".backup-")) throw new Error("backup busy");
    return rm(target, opts);
  });
  const logs = [];
  assert.strictEqual(syncCore(bundled, dest, (s) => logs.push(s)), dest);
  assert.strictEqual(coreHash(dest), coreHash(bundled));
  assert.strictEqual(fs.readFileSync(path.join(existing, "marker"), "utf8"), "keep");
  const backups = fs.readdirSync(parent).filter((n) => n.startsWith("core.backup-") && n !== path.basename(existing));
  assert.strictEqual(backups.length, 1);
  assert.strictEqual(coreHash(path.join(parent, backups[0], "core")), oldHash);
  assert.ok(logs.some((s) => s.includes("backup busy")));
  t.mock.restoreAll();
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("鎖被占用不替換；沒有可用舊核心時回傳 bundled", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const lock = path.join(parent, "core.lock");
  const content = JSON.stringify({ pid: 1234, time: Date.now(), token: "other" });
  fs.writeFileSync(lock, content);
  const logs = [];
  assert.strictEqual(syncCore(bundled, dest, (s) => logs.push(s)), dest);
  assert.strictEqual(coreHash(dest), oldHash);
  assert.strictEqual(fs.readFileSync(lock, "utf8"), content);
  assert.ok(logs.some((s) => s.includes("鎖被占用")));
  fs.unlinkSync(path.join(dest, "agora.py"));
  assert.strictEqual(syncCore(bundled, dest), bundled);
});

test("過期鎖可接手，近期未寫完的鎖不能接手", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const lock = path.join(parent, "core.lock");
  fs.writeFileSync(lock, "");
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.strictEqual(coreHash(dest), oldHash);
  fs.writeFileSync(lock, JSON.stringify({ pid: 1234, time: Date.now() - 121000 }));
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.strictEqual(coreHash(dest), coreHash(bundled));
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("同步期間其他視窗無法進入，鎖記錄 pid 與時間", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const copy = fs.cpSync;
  t.mock.method(fs, "cpSync", (src, target, opts) => {
    const lock = JSON.parse(fs.readFileSync(path.join(parent, "core.lock"), "utf8"));
    assert.strictEqual(lock.pid, process.pid);
    assert.ok(Date.now() - lock.time < 2000);
    assert.strictEqual(syncCore(bundled, dest), dest);
    assert.strictEqual(coreHash(dest), oldHash);
    copy(src, target, opts);
  });
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.strictEqual(coreHash(dest), coreHash(bundled));
});

test("接手過期鎖的程序中止後，殘留 reclaim 也能回收", (t) => {
  const { bundled, dest, parent } = installed(t);
  const lock = path.join(parent, "core.lock");
  const reclaim = lock + ".reclaim";
  const old = new Date(Date.now() - 121000);
  fs.writeFileSync(lock, "");
  fs.utimesSync(lock, old, old);
  fs.writeFileSync(reclaim, "");
  fs.utimesSync(reclaim, old, old);
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.strictEqual(coreHash(dest), coreHash(bundled));
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("過期鎖接手期間其他視窗不得建立新鎖", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const lock = path.join(parent, "core.lock");
  fs.writeFileSync(lock, JSON.stringify({ time: Date.now() - 121000 }));
  const unlink = fs.unlinkSync;
  let raced = false;
  t.mock.method(fs, "unlinkSync", (target) => {
    unlink(target);
    if (target === lock && !raced) {
      raced = true;
      assert.strictEqual(syncCore(bundled, dest), dest);
      assert.strictEqual(coreHash(dest), oldHash);
      assert.ok(!fs.existsSync(lock));
    }
  });
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.ok(raced);
  assert.strictEqual(coreHash(dest), coreHash(bundled));
  assert.deepStrictEqual(fs.readdirSync(parent), ["core"]);
});

test("失去鎖的程序不得切換或刪除新持有者的鎖", (t) => {
  const { bundled, dest, parent, oldHash } = installed(t);
  const copy = fs.cpSync;
  const lock = path.join(parent, "core.lock");
  const content = JSON.stringify({ pid: 9999, time: Date.now(), token: "successor" });
  t.mock.method(fs, "cpSync", (src, target, opts) => {
    copy(src, target, opts);
    fs.writeFileSync(lock, content);
  });
  assert.strictEqual(syncCore(bundled, dest), dest);
  assert.strictEqual(coreHash(dest), oldHash);
  assert.strictEqual(fs.readFileSync(lock, "utf8"), content);
});

test("coreHash 沿用舊打包順序、忽略快取，bundle-core 共用計算並擋版本不符", (t) => {
  const { root } = fixture(t);
  const repo = path.join(root, "repo");
  makeBundle(repo);
  fs.writeFileSync(path.join(repo, "agoralib", "z.py"), "z");
  fs.mkdirSync(path.join(repo, "agoralib", "a"));
  fs.writeFileSync(path.join(repo, "agoralib", "a", "b.py"), "b");
  fs.mkdirSync(path.join(repo, "agoralib", "__pycache__"));
  fs.writeFileSync(path.join(repo, "agoralib", "__pycache__", "x.pyc"), "cache");
  fs.writeFileSync(path.join(repo, "agoralib", "ignored.pyc"), "cache");
  const expected = crypto.createHash("sha256");
  for (const rel of ["agora.py", "agoralib/__init__.py", "agoralib/a/b.py", "agoralib/z.py", "skills/codex/AGENTS.md", "README.md"]) {
    expected.update(rel).update(fs.readFileSync(path.join(repo, rel)));
  }
  const hash = expected.digest("hex").slice(0, 16);
  assert.strictEqual(coreHash(repo), hash);
  const scripts = path.join(repo, "vscode", "scripts");
  fs.mkdirSync(scripts, { recursive: true });
  const pkg = path.join(repo, "vscode", "package.json");
  fs.writeFileSync(pkg, JSON.stringify({ version: "9.9.9" }));
  const source = fs.readFileSync(path.join(__dirname, "..", "scripts", "bundle-core.js"), "utf8");
  const errors = [];
  let calls = 0;
  const context = {
    __dirname: scripts,
    require: (id) => id === "../core" ? { coreHash: (dir) => { calls++; return coreHash(dir); } } : require(id),
    console: { log() {}, error: (s) => errors.push(s) },
    process: { exit: (code) => { throw new Error("exit " + code); } },
  };
  vm.runInNewContext(source, context);
  const output = path.join(repo, "vscode", "core");
  assert.strictEqual(calls, 1);
  assert.strictEqual(readBuild(output).hash, hash);
  assert.strictEqual(coreHash(output), hash);
  assert.ok(!fs.existsSync(path.join(output, "agoralib", "__pycache__")));
  fs.writeFileSync(path.join(output, "marker"), "previous build");
  fs.writeFileSync(pkg, JSON.stringify({ version: "9.9.8" }));
  assert.throws(() => vm.runInNewContext(source, { ...context }), /exit 1/);
  assert.ok(errors.some((s) => s.includes("9.9.9") && s.includes("9.9.8")));
  assert.strictEqual(fs.readFileSync(path.join(output, "marker"), "utf8"), "previous build");
});
