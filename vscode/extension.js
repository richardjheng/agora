// Agora VSCode 擴充套件：選擇參與的 AI 與模型、管理討論串、查看額度。
// 所有設定與動作都透過 agora.py 完成（設定只有一份：<工作區>/.agora/config.json），討論串直接讀 state.json 顯示。

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const editorCtx = require("./context");
const { syncCore, readBuild } = require("./core");
const { findPython, missingPythonMessage } = require("./python");
const updates = require("./update");
const { coreMatches } = require("./versions");
const { relayModelLabel, tierLabel } = require("./modellabel");

const PARTIES = ["claude", "antigravity", "codex"];
const EXTENSION_VERSION = require("./package.json").version;
const NAMES = { claude: "Claude Code", antigravity: "Antigravity", codex: "Codex", human: "我" };
const SHORT = { claude: "Claude", antigravity: "Agy", codex: "Codex" };
const ACTIVE_RELAY = ["starting", "running", "waiting"];
const RELAY_LABELS = {
  starting: "啟動中", running: "執行中", waiting: "等額度重置", done: "已完成", stopped: "已暫停",
  failed: "失敗", max_rounds: "已達最大輪數", blocked: "需要人工處理", interrupted: "已中斷",
  needs_upgrade: "需要升級",
};

let output;
let extensionPath;
let bundledCore;   // 內建核心同步到 ~/.agora/core 後的位置（activate 時設定）

// ───────────────────────── 執行 agora.py ─────────────────────────

/** 明確指定的 Agora 位置：設定 agora.root，或直接從 repo 載入擴充套件（開發模式） */
function agoraRoot() {
  const configured = vscode.workspace.getConfiguration("agora").get("root");
  const candidates = [configured];
  try {
    candidates.push(path.resolve(fs.realpathSync(extensionPath), ".."));
  } catch {}
  return candidates.find((c) => c && fs.existsSync(path.join(c, "agora.py")));
}

function findOnPath(name) {
  const exts = process.platform === "win32" ? (process.env.PATHEXT || ".EXE;.CMD;.BAT").split(";") : [""];
  for (const dir of (process.env.PATH || "").split(path.delimiter).filter(Boolean)) {
    for (const ext of exts) {
      const file = path.join(dir, name + ext);
      if (fs.existsSync(file)) return file;
    }
  }
  return undefined;
}

/** 執行 Agora 的方式，依序：設定 agora.root → PATH 上的 agora 指令（pip install -e）→ 擴充套件內建的核心 */
function agoraCommand() {
  const python = findPython({ configured: vscode.workspace.getConfiguration("agora").get("pythonPath") });
  const root = agoraRoot();
  if (root) return { cmd: python.cmd, prefix: [...python.args, path.join(root, "agora.py")], source: root, python };
  const exe = findOnPath("agora");
  if (exe) return { cmd: exe, prefix: [], source: exe };
  if (bundledCore) {
    return { cmd: python.cmd, prefix: [...python.args, path.join(bundledCore, "agora.py")], source: bundledCore, python };
  }
  return undefined;
}

/** 子程序的環境變數：找到的 Python 與它的 Scripts 放在 PATH 最前面，
 * 讓 Agora 呼叫的 CLI（例如 Claude 的額度 hook：python …/quota_hook.py）也找得到同一個 Python */
function childEnv(agora) {
  const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
  const cmd = agora.python && agora.python.cmd;
  if (cmd && path.isAbsolute(cmd) && !/py\.exe$/i.test(cmd)) {
    const key = Object.keys(env).find((k) => k.toUpperCase() === "PATH") || "PATH";
    env[key] = [path.dirname(cmd), path.join(path.dirname(cmd), "Scripts"), env[key]].filter(Boolean).join(path.delimiter);
  }
  return env;
}

/** Agora 使用的資料夾：多資料夾工作區時優先選已有 .agora 的那個，都沒有才用第一個 */
function workspaceRoot() {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) return undefined;
  const withAgora = folders.find((f) => f.uri.scheme === "file" && fs.existsSync(path.join(f.uri.fsPath, ".agora")));
  return (withAgora || folders[0]).uri.fsPath;
}

class AgoraError extends Error {}

function killTree(proc) {
  if (process.platform === "win32") {
    cp.spawn("taskkill", ["/pid", String(proc.pid), "/T", "/F"], { windowsHide: true });
  } else {
    proc.kill();
  }
}

/** 執行 agora.py；失敗時丟出 AgoraError（訊息為 agora.py 印在 stderr 的最後一段） */
function runAgora(args, { input, token, onProgress, log = true } = {}) {
  const agora = agoraCommand();
  const ws = workspaceRoot();
  if (!agora) {
    return Promise.reject(new AgoraError("找不到 Agora：擴充套件內建的核心無法使用，請在設定 agora.root 指定 Agora 程式所在的資料夾，或執行 pip install -e <Agora 資料夾>。"));
  }
  if (!ws) {
    return Promise.reject(new AgoraError("請先開啟一個資料夾。"));
  }
  const cmd = agora.cmd;
  const fullArgs = [...agora.prefix, "--workspace", ws, ...args];
  if (log) {
    output.appendLine(`$ agora ${args.join(" ")}`);
  }
  return new Promise((resolve, reject) => {
    const proc = cp.spawn(cmd, fullArgs, {
      cwd: ws,
      env: childEnv(agora),
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.setEncoding("utf8");
    proc.stderr.setEncoding("utf8");
    proc.stdout.on("data", (d) => {
      stdout += d;
      if (log) output.append(d);
    });
    proc.stderr.on("data", (d) => {
      stderr += d;
      if (log) output.append(d);
      const line = d.trim().split("\n").pop();
      if (onProgress && line) onProgress(line);
    });
    proc.on("error", (e) => reject(new AgoraError(
      (e.code === "ENOENT" && agora.python && missingPythonMessage(9009)) || `無法執行 ${cmd}：${e.message}`)));
    proc.on("close", (code) => {
      const missing = agora.python && missingPythonMessage(code, stderr);
      if (code === 0) {
        resolve(stdout);
      } else if (token && token.isCancellationRequested) {
        reject(new AgoraError("已取消。"));
      } else if (missing) {
        reject(new AgoraError(missing));
      } else {
        const message = stderr.trim().split("\n").slice(-3).join("\n") || stdout.trim().split("\n").pop();
        reject(new AgoraError(message || `agora ${args[0]} 失敗（exit ${code}）`));
      }
    });
    if (token) token.onCancellationRequested(() => killTree(proc));
    proc.stdin.end(input || "");
  });
}

async function runJson(args) {
  return JSON.parse(await runAgora(args, { log: false }));
}

/** 需要等 AI 回覆的動作：顯示可取消的進度通知 */
const BACKGROUND_COMMANDS = ["send", "reply", "auto", "summarize"];

/** 討論在 agora.py 的背景程序執行：取消只是停止等待，討論會繼續，完成時另有通知 */
async function runLong(title, args, input) {
  const background = BACKGROUND_COMMANDS.includes(args[0]);
  try {
    return await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Agora：${title}`, cancellable: true },
      (progress, token) => runAgora(args, { input, token, onProgress: (m) => progress.report({ message: m }) }),
    );
  } catch (e) {
    if (background && e instanceof AgoraError && e.message === "已取消。") {
      vscode.window.showInformationMessage("已停止等待，討論在背景繼續，完成時會通知。要停止討論，請在討論串上按右鍵選「停止討論」。");
      return "";
    }
    throw e;
  }
}

function showError(e) {
  if (e instanceof AgoraError) {
    vscode.window.showErrorMessage(`Agora：${e.message}`);
  } else {
    output.appendLine(String(e && e.stack || e));
    vscode.window.showErrorMessage(`Agora：${e && e.message || e}`);
  }
}

// ───────────────────────── 討論串 ─────────────────────────

function threadsDir() {
  const ws = workspaceRoot();
  return ws && path.join(ws, ".agora", "threads");
}

function loadThreads() {
  const dir = threadsDir();
  if (!dir || !fs.existsSync(dir)) return [];
  const threads = [];
  for (const name of fs.readdirSync(dir)) {
    try {
      const state = JSON.parse(fs.readFileSync(path.join(dir, name, "state.json"), "utf8"));
      threads.push({ id: name, dir: path.join(dir, name), state });
    } catch {}
  }
  return threads.sort((a, b) => b.id.localeCompare(a.id));
}

class ThreadsProvider {
  constructor(activity) {
    this.activity = activity;
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  refresh() {
    this.emitter.fire();
  }

  getChildren(element) {
    return element ? [] : loadThreads();
  }

  getTreeItem(thread) {
    const { state } = thread;
    const item = new vscode.TreeItem(state.topic, vscode.TreeItemCollapsibleState.None);
    const messages = state.messages || [];
    const last = messages[messages.length - 1];
    const relay = state.relay;
    const job = this.activity.discussion(thread.id);
    const parts = [];
    if (job) parts.push(job.current ? `${NAMES[job.current]} 回覆中 ${minutesSince(job.since)}` : "討論啟動中");
    parts.push(`${messages.length} 則`);
    if (relay) {
      const denied = (relay.denied || []).length;
      let relayDesc = `${NAMES[relay.worker]} ${RELAY_LABELS[relay.status] || relay.status}${denied ? `（⚠ ${denied} 次工具被拒）` : ""}`;
      if (relay.branch) relayDesc += ` · ${relay.branch}`;
      const cInfo = relayCommitInfo(relay);
      if (cInfo && cInfo.sha) relayDesc += ` · commit ${cInfo.sha}`;
      parts.push(relayDesc);
    }
    if (state.keep) parts.push("保留");
    item.description = parts.join(" · ");
    const active = relay && ACTIVE_RELAY.includes(relay.status);
    item.iconPath = new vscode.ThemeIcon(job || active ? "sync~spin" : relay ? "tasklist" : state.keep ? "pinned" : "comment-discussion");
    item.contextValue = (active ? "thread-relay-active" : relay ? "thread-relay" : "thread") + (job ? "-job" : "")
      + (state.keep ? "-kept" : "");
    const tip = new vscode.MarkdownString();
    tip.appendMarkdown(`**${state.topic}**\n\n`);
    tip.appendMarkdown(`參與者：${Object.keys(state.parties).map((p) => NAMES[p] || p).join("、")}\n\n`);
    tip.appendMarkdown(`建立：${state.created}`);
    if (last) tip.appendMarkdown(`\n\n最後發言：${NAMES[last.speaker] || last.speaker}（${last.time}）`);
    if (relay) {
      tip.appendText(`\n\n等級 ${tierLabel(relay.tier)} · ${relayModelLabel(relay)}`);
      if (relay.branch) tip.appendText(`\n分支：${relay.branch}`);
      const wt = relay.worktree || relay.worktree_path;
      if (wt) tip.appendText(`\nWorktree：${wt}`);
      const cInfo = relayCommitInfo(relay);
      if (cInfo) tip.appendText(`\nCommit：${cInfo.text}`);
    }
    item.tooltip = tip;
    item.id = thread.id;
    item.command = { command: "agora.openTranscript", title: "開啟逐字稿", arguments: [thread] };
    return item;
  }
}

function minutesSince(ts) {
  if (!ts) return "";
  const m = Math.floor((Date.now() / 1000 - ts) / 60);
  return m < 1 ? "不到 1 分鐘" : `${m} 分鐘`;
}

/** 進行中的討論與分派（agora activity）；有東西結束時發出通知 */
class Activity {
  constructor() {
    this.items = null;
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
  }

  discussion(threadId) {
    return (this.items || []).find((x) => x.type === "discussion" && x.thread === threadId);
  }

  async load() {
    let items;
    try {
      items = JSON.parse(await runAgora(["activity", "--json"], { log: false }));
    } catch {
      return;
    }
    const before = this.items;
    this.items = items;
    if (before) {
      const key = (x) => `${x.type}:${x.thread}`;
      const now = new Set(items.map(key));
      for (const gone of before.filter((x) => !now.has(key(x)))) notifyFinished(gone);
    }
    this.emitter.fire();
  }
}

/** 解析 relay 的 commit 相關欄位（commit / commit_sha / commit_result / commit_status / check_result） */
function relayCommitInfo(relay) {
  if (!relay) return null;
  const c = relay.commit || relay.commit_sha || relay.commit_result;
  const status = relay.commit_status ? `[${relay.commit_status}]` : "";
  const check = relay.check_result ? (typeof relay.check_result === "object" ? (relay.check_result.exit_code === 0 ? "check 通過" : "check 失敗") : `check: ${relay.check_result}`) : "";
  if (!c && !status && !check) return null;
  if (c && typeof c === "object") {
    const sha = c.sha ? String(c.sha).slice(0, 7) : "";
    const msg = c.message ? `（${c.message}）` : "";
    const chk = c.check_passed !== undefined ? (c.check_passed ? "check 通過" : "check 失敗") : check;
    return { sha, text: [sha, status, msg, chk].filter(Boolean).join(" ") || "已提交", raw: c };
  }
  const s = c ? String(c) : "";
  const sha = s.slice(0, 7);
  return { sha, text: [sha, status, check].filter(Boolean).join(" ") || status, raw: c };
}

/** 「本次接手有 N 次工具被拒：read_url ×2」；沒有被拒時回傳空字串（與 cli.denial_summary 相同） */
function deniedSummary(relay) {
  const denied = relay.denied || [];
  if (!denied.length) return "";
  const counts = {};
  for (const d of denied) counts[d.action || "unknown"] = (counts[d.action || "unknown"] || 0) + 1;
  return `本次接手有 ${denied.length} 次工具被拒：` + Object.entries(counts).map(([a, n]) => `${a} ×${n}`).join("、");
}

function notifyFinished(item) {
  const thread = loadThreads().find((t) => t.id === item.thread);
  if (!thread) return;
  let text;
  let warn = false;
  if (item.type === "discussion") {
    let job = {};
    try {
      job = JSON.parse(fs.readFileSync(path.join(thread.dir, "job.json"), "utf8"));
    } catch {}
    if (job.status === "stopped") return;
    warn = job.status !== "done";
    text = warn ? `「${thread.state.topic}」的討論${job.status === "interrupted" ? "被中斷" : "失敗"}，詳見逐字稿。`
      : `「${thread.state.topic}」的討論已完成。`;
  } else {
    const relay = thread.state.relay || {};
    const denied = deniedSummary(relay);
    warn = relay.status !== "done" || !!denied;
    const branchInfo = relay.branch ? ` · 分支 ${relay.branch}` : "";
    const cInfo = relayCommitInfo(relay);
    const commitInfo = cInfo && cInfo.sha ? ` · commit ${cInfo.sha}` : "";
    text = `${NAMES[item.worker]} 接手「${thread.state.topic}」的工作：${RELAY_LABELS[relay.status] || relay.status}`
      + (relay.blocked_reason ? `（${relay.blocked_reason}）` : "")
      + ` · 等級 ${tierLabel(relay.tier)} · ${relayModelLabel(relay)}${branchInfo}${commitInfo}。`;
    if (denied) {
      text += ` ⚠️ ${denied}` + (relay.status === "done" ? "，之後卻宣告完成，請查證結果的來源。" : "。");
    }
  }
  const buttons = item.type === "relay" ? ["開啟逐字稿", "檢視變更"] : ["開啟逐字稿"];
  const shown = warn ? vscode.window.showWarningMessage(`Agora：${text}`, ...buttons)
    : vscode.window.showInformationMessage(`Agora：${text}`, ...buttons);
  shown.then((choice) => {
    if (choice === "檢視變更") vscode.commands.executeCommand("agora.changes", thread);
    else if (choice) openTranscript(thread);
  });
}

async function pickThread(arg) {
  if (arg && arg.id && arg.state) return arg;
  const threads = loadThreads();
  if (!threads.length) {
    vscode.window.showInformationMessage("此專案還沒有討論串。");
    return undefined;
  }
  const picked = await vscode.window.showQuickPick(
    threads.map((t) => ({ label: t.state.topic, description: t.id, thread: t })),
    { placeHolder: "選擇討論串" },
  );
  return picked && picked.thread;
}

async function openTranscript(thread) {
  const uri = vscode.Uri.file(path.join(thread.dir, "transcript.md"));
  await vscode.commands.executeCommand("markdown.showPreview", uri);
}

/** 目前編輯器的上下文：選取範圍（沒有選取時為整份檔案）的位置、內容與診斷 */
function editorContext(editor, includeCode) {
  const doc = editor.document;
  const sel = editor.selection;
  let range = null;
  let code = null;
  if (!sel.isEmpty) {
    // 選到下一行開頭（游標在第 0 欄）時，不把那一行算進範圍
    const end = sel.end.character === 0 && sel.end.line > sel.start.line ? sel.end.line - 1 : sel.end.line;
    range = { start: sel.start.line + 1, end: end + 1 };
    if (includeCode) code = doc.getText(sel);
  } else if (includeCode) {
    code = doc.getText();
  }
  const relPath = doc.isUntitled ? "（未命名檔案）" : vscode.workspace.asRelativePath(doc.uri, false).replace(/\\/g, "/");
  const diagnostics = vscode.languages.getDiagnostics(doc.uri).map((d) => ({
    line: d.range.start.line + 1, endLine: d.range.end.line + 1, column: d.range.start.character + 1,
    severity: d.severity, source: d.source, message: String(d.message).split("\n")[0],
  }));
  return editorCtx.formatContext({
    relPath, languageId: doc.languageId, version: doc.version, dirty: doc.isDirty, range, code,
    lineCount: doc.lineCount, diagnostics,
  });
}

/** 輸入訊息；有開啟的編輯器時，可附上它的上下文（位置、選取內容或整份檔案、診斷） */
async function askMessage(prompt) {
  const editor = vscode.window.activeTextEditor;
  let attached = "";
  if (editor && !editor.document.uri.fsPath.endsWith("transcript.md") && editor.document.uri.scheme !== "agora-base") {
    const doc = editor.document;
    const name = path.basename(doc.fileName);
    const sel = editor.selection;
    const scope = sel.isEmpty ? "整份檔案" : `第 ${sel.start.line + 1}–${sel.end.line + 1} 行`;
    const choice = await vscode.window.showQuickPick(
      [
        { label: `$(file-code) 輸入訊息，並附上 ${name} 的上下文`, description: `${scope}、路徑、行號與診斷`, value: "context" },
        { label: "$(edit) 只輸入訊息", value: "plain" },
      ],
      { placeHolder: prompt },
    );
    if (!choice) return undefined;
    if (choice.value === "context") {
      let includeCode = true;
      if (sel.isEmpty && editorCtx.tooLarge(doc.lineCount, doc.getText().length)) {
        const go = await vscode.window.showWarningMessage(
          `${name} 有 ${doc.lineCount} 行，太大不適合整份附上。請先選取要討論的範圍，或只附路徑與診斷。`,
          "只附路徑與診斷");
        if (!go) return undefined;
        includeCode = false;
      }
      attached = editorContext(editor, includeCode);
    }
  }
  const text = await vscode.window.showInputBox({
    prompt: attached ? `${prompt}（已附上編輯器上下文，可留空）` : prompt, ignoreFocusOut: true,
  });
  if (text === undefined) return undefined;
  const message = [text.trim(), attached].filter(Boolean).join("\n\n");
  return message || undefined;
}

// ───────────────────────── 分派期間的變更 ─────────────────────────

const STATUS_LABELS = { A: "新增", M: "修改", D: "刪除", T: "類型變更" };

/** agora-base:/<路徑>?sha=<基準>&path=<路徑>：分派基準裡的檔案內容；empty=1 表示該側沒有這個檔案 */
class BaselineProvider {
  provideTextDocumentContent(uri) {
    const q = new URLSearchParams(uri.query);
    if (q.get("empty")) return "";
    const ws = workspaceRoot();
    return new Promise((resolve, reject) => {
      // sha:./路徑 以工作區（git 的 cwd）為準，工作區是 repo 子資料夾時也正確
      cp.execFile("git", ["show", `${q.get("sha")}:./${q.get("path")}`], { cwd: ws, maxBuffer: 64 * 1024 * 1024, windowsHide: true },
        (err, stdout, stderr) => (err
          ? reject(new Error(`無法讀取分派基準中的 ${q.get("path")}：${(stderr || err.message).trim()}`))
          : resolve(stdout)));
    });
  }
}

function baselineUri(sha, file, empty) {
  const query = new URLSearchParams({ sha, path: file, ...(empty ? { empty: "1" } : {}) }).toString();
  return vscode.Uri.from({ scheme: "agora-base", path: `/${file}`, query });
}

// ───────────────────────── 參與方、模型、額度 ─────────────────────────

class Model {
  constructor() {
    this.parties = null;   // agora parties --json
    this.work = {};        // 近期各 AI 的接手輪數（同上）
    this.shareDays = 7;
    this.catalog = null;   // agora models --json
    this.quota = null;     // agora quota --json
    this.coreVersion = null;   // agora --version
    this.errors = {};
    this.emitter = new vscode.EventEmitter();
    this.onDidChange = this.emitter.event;
  }

  /** 安裝狀態（agora parties --json 的 install）：installed / outdated / partial / missing；只有設定檔不算安裝 */
  installed() {
    return !!this.install && ["installed", "outdated"].includes(this.install.state);
  }

  enabled() {
    return this.parties ? PARTIES.filter((p) => this.parties[p].enabled) : PARTIES;
  }

  async loadParties() {
    try {
      this.setParties(await runJson(["parties", "--json"]));
      delete this.errors.parties;
    } catch (e) {
      this.errors.parties = e.message;
    }
    this.emitter.fire();
  }

  setParties(data) {
    this.parties = data.parties;
    this.work = data.work || {};
    this.usage = data.usage || null;
    this.shareDays = data.share_days || 7;
    this.summarizer = data.summarizer || null;
    this.install = data.install || null;
    this.core = data.core || null;   // 實際執行的核心：{version, root, kind, build}（舊版核心沒有）
  }

  async loadVersion() {
    try {
      const out = await runAgora(["--version"], { log: false });
      this.coreVersion = (out.trim().match(/(\d+\.\d+\.\d+\S*)/) || [])[1] || out.trim();
    } catch {
      this.coreVersion = null;
    }
    this.emitter.fire();
  }

  async loadCatalog(refresh = false) {
    try {
      this.catalog = await runJson(["models", "--json", ...(refresh ? ["--refresh"] : [])]);
      delete this.errors.catalog;
    } catch (e) {
      this.errors.catalog = e.message;
    }
    this.emitter.fire();
  }

  async loadQuota(refresh = false) {
    try {
      this.quota = await runJson(["quota", "--json", ...(refresh ? ["--refresh"] : [])]);
      delete this.errors.quota;
    } catch (e) {
      this.errors.quota = e.message;
    }
    this.emitter.fire();
  }

  async update(args) {
    try {
      this.setParties(await runJson(["parties", ...args, "--json"]));
      if (args.includes("--model")) this.loadQuota();   // 切換模型可能改變對應的額度池（如 Antigravity 的 Argon / Flash 3.8）
    } catch (e) {
      showError(e);
    }
    this.emitter.fire();
  }

  snapshot() {
    return {
      workspace: workspaceRoot(),
      agoraFound: !!agoraCommand(),
      install: this.install,
      parties: this.parties,
      work: this.work,
      usage: this.usage,
      shareDays: this.shareDays,
      catalog: this.catalog,
      quota: this.quota,
      errors: this.errors,
      versions: { extension: EXTENSION_VERSION, core: this.coreVersion, source: this.core },
      names: NAMES,
      order: PARTIES,
    };
  }
}

const STALE_AFTER = 2 * 3600;   // 與 quota.STALE_AFTER 相同：超過就標示可能過期

function quotaText(u, { multiline = false } = {}) {
  if (!u) return "無資料";
  const text = quotaBody(u, multiline);
  return u.blocked || !u.updated_at || Date.now() / 1000 - u.updated_at <= STALE_AFTER ? text
    : text.replace(/\n|$/, "（資料可能過期）$&");
}

function quotaBody(u, multiline) {
  const fmt = (ts) => {
    if (!ts) return "未知";
    const d = new Date(ts * 1000);
    const sd = d.toDateString() === new Date().toDateString();
    return d.toLocaleString("zh-TW", sd ? { hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  };

  if (u.blocked) return `無法使用（${u.blocked.reason}，${fmt(u.blocked.at)} 偵測）`;
  const winKeys = u.windows ? Object.keys(u.windows) : [];
  if (winKeys.length > 1) {
    if (multiline) {
      const details = winKeys.map((k) => {
        const w = u.windows[k];
        return `  • ${w.label || k}：剩 ${Math.round(w.remaining_pct)}%（${fmt(w.resets_at)} 重置）`;
      }).join("\n");
      const tag = u.window_label || u.window || "最緊";
      return `剩 ${Math.round(u.remaining_pct)}%（${tag}，${fmt(u.resets_at)} 重置）\n${details}`;
    }
    const summary = winKeys.map((k) => {
      const w = u.windows[k];
      return `${w.label || k} ${Math.round(w.remaining_pct)}%`;
    }).join(" · ");
    return `剩 ${Math.round(u.remaining_pct)}%（${summary}）`;
  }

  const winLabel = (winKeys.length === 1 && u.windows[winKeys[0]].label) || (u.window_label && u.window ? u.window_label : null);
  const winTag = winLabel ? `${winLabel}，` : "";
  let base = `剩 ${Math.round(u.remaining_pct)}%（${winTag}${fmt(u.resets_at)} 重置）`;
  if (multiline && u.pools) {
    const others = Object.entries(u.pools)
      .map(([k, v]) => `  • ${v.label || k}：剩 ${Math.round(v.remaining_pct)}%（${fmt(v.resets_at)} 重置）`)
      .join("\n");
    if (others) base += `\n${others}`;
  }
  return base;
}

class PartiesView {
  constructor(model) {
    this.model = model;
    model.onDidChange(() => this.post());
  }

  resolveWebviewView(view) {
    this.view = view;
    const media = vscode.Uri.file(path.join(extensionPath, "media"));
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    const nonce = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);
    const js = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.js"));
    const css = view.webview.asWebviewUri(vscode.Uri.joinPath(media, "panel.css"));
    view.webview.html = `<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${view.webview.cspSource}; script-src 'nonce-${nonce}';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${css}"></head>
<body><div id="app">載入中…</div><script nonce="${nonce}" src="${js}"></script></body></html>`;
    view.webview.onDidReceiveMessage((msg) => this.onMessage(msg).catch(showError));
    view.onDidChangeVisibility(() => view.visible && this.post());
  }

  post() {
    if (this.view) this.view.webview.postMessage({ type: "state", state: this.model.snapshot() });
  }

  async onMessage(msg) {
    const m = this.model;
    switch (msg.type) {
      case "ready":
        this.post();
        if (!m.parties) m.loadParties();
        if (!m.coreVersion) m.loadVersion();
        if (!m.catalog) m.loadCatalog();
        if (!m.quota) m.loadQuota();
        break;
      case "toggle":
        await m.update([msg.enabled ? "--enable" : "--disable", msg.party]);
        break;
      case "set":
        await m.update([`--${msg.field}`, `${msg.party}=${msg.value || "default"}`]);
        break;
      case "custom": {
        const current = m.parties && m.parties[msg.party].model;
        const value = await vscode.window.showInputBox({
          prompt: `${NAMES[msg.party]} 的模型名稱（CLI 接受的完整名稱或別名）`,
          value: current || "",
        });
        if (value !== undefined) await m.update(["--model", `${msg.party}=${value.trim() || "default"}`]);
        else this.post();
        break;
      }
      case "tier":
        await m.update(["--tier", `${msg.party}.${msg.level}=${msg.value || "default"}`]);
        break;
      case "tierFallback":
        await m.update(["--tier-fallback", `${msg.party}.${msg.level}=${msg.value || "default"}`]);
        break;
      case "customTier": {
        const tiers = (m.parties && m.parties[msg.party] && m.parties[msg.party].tiers) || {};
        const curTier = tiers[msg.level];
        const curObj = msg.fallback ? (curTier && curTier.fallback) : curTier;
        const curModel = curObj ? curObj.model : "";
        const role = msg.fallback ? "替代模型" : "模型";
        const levelNames = { light: "輕量", standard: "標準", heavy: "重度" };
        const levelLabel = levelNames[msg.level] || msg.level;
        const value = await vscode.window.showInputBox({
          prompt: `${NAMES[msg.party]} ${levelLabel}等級的${role}名稱（CLI 接受的完整名稱或別名）`,
          value: curModel || "",
        });
        if (value !== undefined) {
          const val = value.trim() || "default";
          const flag = msg.fallback ? "--tier-fallback" : "--tier";
          await m.update([flag, `${msg.party}.${msg.level}=${val}`]);
        } else {
          this.post();
        }
        break;
      }
      case "refreshModels":
        await m.loadCatalog(true);
        break;
      case "refreshQuota":
        await m.loadQuota(true);
        break;
      case "install":
        await vscode.commands.executeCommand("agora.install");
        break;
      case "update":
        await vscode.commands.executeCommand(msg.repair ? "agora.repair" : "agora.update");
        break;
      case "newThread":
        await vscode.commands.executeCommand("agora.newThread");
        break;
      case "settings":
        await vscode.commands.executeCommand("workbench.action.openSettings", "agora.");
        break;
    }
  }
}

// ───────────────────────── 擴充套件更新 ─────────────────────────

const UPDATE_INTERVAL = 6 * 3600 * 1000;   // 自動檢查的間隔；多個視窗共用 globalState 的上次檢查時間
const UPDATE_KEYS = { checkedAt: "agora.update.checkedAt", installed: "agora.update.installed", skipped: "agora.update.skipped" };

/** 從 GitHub Releases 檢查並安裝新版（設定 agora.autoUpdate：auto 自動安裝、notify 只通知、off 不檢查） */
class Updater {
  constructor(context) {
    this.state = context.globalState;
    this.dev = context.extensionMode !== vscode.ExtensionMode.Production;
    this.running = null;
  }

  mode() {
    return vscode.workspace.getConfiguration("agora").get("autoUpdate") || "auto";
  }

  /** manual：從指令觸發，沒有新版或失敗時也要告知 */
  check(manual = false) {
    if (!this.running) this.running = this.run(manual).finally(() => { this.running = null; });
    return this.running;
  }

  async run(manual) {
    if (this.dev) {
      if (manual) vscode.window.showInformationMessage("Agora：開發模式（從原始碼載入）不檢查擴充套件更新。");
      return;
    }
    if (!manual) {
      if (this.mode() === "off" || Date.now() - this.state.get(UPDATE_KEYS.checkedAt, 0) < UPDATE_INTERVAL) return;
    }
    let info;
    try {
      info = await updates.fetchLatest();
      await this.state.update(UPDATE_KEYS.checkedAt, Date.now());
    } catch (e) {
      output.appendLine(`檢查擴充套件更新失敗：${e.message}`);
      if (manual) this.failed(`檢查更新失敗：${e.message}`);
      return;
    }
    if (!info || updates.compareVersions(info.version, EXTENSION_VERSION) <= 0) {
      if (manual) vscode.window.showInformationMessage(`Agora：擴充套件已是最新版（${EXTENSION_VERSION}）。`);
      return;
    }
    if (this.state.get(UPDATE_KEYS.installed) === info.version) {
      // 已經安裝（可能是另一個視窗裝的），只差重新載入
      if (manual) this.promptReload(info.version);
      return;
    }
    if (!manual && this.state.get(UPDATE_KEYS.skipped) === info.version) return;
    if (!manual && this.mode() === "auto") {
      await this.install(info, false);
      return;
    }
    const choice = await vscode.window.showInformationMessage(
      `Agora：擴充套件有新版 ${info.version}（目前 ${EXTENSION_VERSION}）。`, "更新", "查看變更", ...(manual ? [] : ["略過此版本"]));
    if (choice === "更新") await this.install(info, true);
    else if (choice === "查看變更") vscode.env.openExternal(vscode.Uri.parse(info.page));
    else if (choice === "略過此版本") await this.state.update(UPDATE_KEYS.skipped, info.version);
  }

  /** 下載 .vsix 並安裝；visible 時顯示下載進度；promptReload 為 false 時由呼叫者自己重新載入（一鍵更新）。回傳是否安裝成功 */
  async install(info, visible, { promptReload = true } = {}) {
    output.appendLine(`下載 Agora 擴充套件 ${info.version}：${info.url}`);
    let file;
    try {
      file = visible
        ? await vscode.window.withProgress(
          { location: vscode.ProgressLocation.Notification, title: `Agora：下載擴充套件 ${info.version}` }, () => updates.download(info))
        : await updates.download(info);
      await vscode.commands.executeCommand("workbench.extensions.installExtension", vscode.Uri.file(file));
    } catch (e) {
      output.appendLine(`更新擴充套件失敗：${e.message}`);
      this.failed(`更新到 ${info.version} 失敗：${e.message}`);
      return false;
    } finally {
      if (file) fs.rmSync(file, { force: true });
    }
    await this.state.update(UPDATE_KEYS.installed, info.version);
    output.appendLine(`已安裝 Agora 擴充套件 ${info.version}，重新載入視窗後生效。`);
    if (promptReload) this.promptReload(info.version);
    return true;
  }

  promptReload(version) {
    vscode.window.showInformationMessage(`Agora：擴充套件已更新到 ${version}，重新載入視窗後生效。`, "重新載入")
      .then((choice) => choice && vscode.commands.executeCommand("workbench.action.reloadWindow"));
  }

  failed(message) {
    vscode.window.showWarningMessage(`Agora：${message}`, "開啟 Releases 頁面")
      .then((choice) => choice && vscode.env.openExternal(vscode.Uri.parse(updates.RELEASES_PAGE)));
  }
}

// ───────────────────────── 一鍵更新 ─────────────────────────

// 待完成的一鍵更新，以工作區路徑為 key 存在 globalState（多個視窗共用；每個視窗只處理自己的工作區）：
// { version, phase, repair, at }。phase：waiting（等進行中的討論與分派結束）、installing（下載安裝中，安裝前就記下，
// 避免裝完、記錄前被中斷而漏掉補檔）、installed（已安裝，等重新載入後補齊此專案的檔案）
const PENDING_KEY = "agora.update.pending";
const UPDATE_LOCK_STALE = 10 * 60 * 1000;

class OneClickUpdate {
  /** hooks：{ coreVersion() 目前顯示的核心版本, refresh() 重新整理面板與討論串 } */
  constructor(context, updater, activity, hooks) {
    this.state = context.globalState;
    this.updater = updater;
    this.activity = activity;
    this.hooks = hooks;
    this.busy = false;
    activity.onDidChange(() => this.onActivityChange());
  }

  pending(ws = workspaceRoot()) {
    return ws ? (this.state.get(PENDING_KEY) || {})[ws] : undefined;
  }

  async setPending(ws, value) {
    const all = { ...(this.state.get(PENDING_KEY) || {}) };
    if (value) all[ws] = { ...value, at: Date.now() };
    else delete all[ws];
    await this.state.update(PENDING_KEY, all);
  }

  /** 同一個工作區同時只讓一個視窗執行補齊（多個視窗開同一個專案時）；回傳釋放函式，拿不到時回傳 null */
  lock(ws) {
    const file = path.join(ws, ".agora", "update.lock");
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true });
      try {
        if (Date.now() - fs.statSync(file).mtimeMs > UPDATE_LOCK_STALE) fs.rmSync(file, { force: true });
      } catch {}
      fs.writeFileSync(file, JSON.stringify({ pid: process.pid, at: Date.now() }), { flag: "wx" });
    } catch {
      return null;
    }
    return () => fs.rmSync(file, { force: true });
  }

  /** 面板的「更新 Agora」（repair=false）與「重新下載修復」（repair=true） */
  async run(repair) {
    const ws = workspaceRoot();
    if (!ws) throw new AgoraError("請先開啟一個資料夾。");
    if (this.busy) return;
    this.busy = true;
    try {
      await this.activity.load();
      const active = this.activity.items || [];
      if (active.length) {
        const wait = "等它們結束後再更新";
        const choice = await vscode.window.showWarningMessage(
          `Agora：此專案有 ${active.length} 個討論或分派正在進行。它們是獨立程序，重新載入視窗不會中斷，`
          + "但更新會替換它們正在使用的 Agora 核心。", { modal: true }, wait, "現在更新");
        if (!choice) return;
        if (choice === wait) {
          await this.setPending(ws, { phase: "waiting", repair });
          vscode.window.showInformationMessage("Agora：會在進行中的討論與分派都結束後自動更新。");
          return;
        }
      }
      await this.download(ws, repair);
    } finally {
      this.busy = false;
    }
  }

  /** 查 Release；有新版（或修復時同版）就下載安裝並重新載入，否則只補齊此專案的檔案 */
  async download(ws, repair) {
    let info;
    try {
      info = await updates.fetchLatest();
    } catch (e) {
      const go = await vscode.window.showWarningMessage(
        `Agora：無法查詢 GitHub Releases（${e.message}）。`, "只補齊此專案的檔案", "開啟 Releases 頁面");
      if (go === "開啟 Releases 頁面") vscode.env.openExternal(vscode.Uri.parse(updates.RELEASES_PAGE));
      if (go === "只補齊此專案的檔案") await this.complete(ws);
      return;
    }
    const diff = info ? updates.compareVersions(info.version, EXTENSION_VERSION) : -1;
    if (diff > 0 || (repair && diff === 0)) {
      await this.setPending(ws, { version: info.version, phase: "installing", repair });
      if (!(await this.updater.install(info, true, { promptReload: false }))) {
        await this.setPending(ws, null);
        return;
      }
      await this.setPending(ws, { version: info.version, phase: "installed", repair });
      output.appendLine(`一鍵更新：已安裝 ${info.version}，重新載入視窗後補齊此專案的 Agora 檔案。`);
      await vscode.commands.executeCommand("workbench.action.reloadWindow");
      return;
    }
    if (repair) {
      vscode.window.showWarningMessage(info
        ? `Agora：GitHub 上最新的 Release 是 ${info.version}，比目前的擴充套件（${EXTENSION_VERSION}）舊，不會降版；改為只補齊此專案的檔案。`
        : "Agora：GitHub 上沒有可用的 Release（含 .vsix）；改為只補齊此專案的檔案。");
    } else {
      output.appendLine(`一鍵更新：遠端沒有比 ${EXTENSION_VERSION} 新的版本${info ? `（最新 ${info.version}）` : ""}，只補齊此專案的檔案。`);
    }
    await this.complete(ws, info && diff >= 0 ? null : `遠端沒有比 ${EXTENSION_VERSION} 新的版本`);
  }

  /** 啟動時接續：重新載入後補齊；上次安裝被中斷時詢問 */
  async resume() {
    const ws = workspaceRoot();
    const p = this.pending(ws);
    if (!p || this.busy) return;
    if (p.phase === "waiting") return this.onActivityChange();
    if (updates.compareVersions(p.version, EXTENSION_VERSION) > 0) {
      // 還是舊版：安裝被中斷，或另一個視窗裝好但這個視窗還沒重新載入
      const installed = this.state.get(UPDATE_KEYS.installed) === p.version;
      const choice = await vscode.window.showWarningMessage(
        installed ? `Agora：擴充套件 ${p.version} 已安裝，重新載入視窗後補齊此專案的檔案。`
          : `Agora：上次更新到 ${p.version} 沒有完成。`,
        installed ? "重新載入" : "重試", "取消更新");
      if (choice === "重新載入") await vscode.commands.executeCommand("workbench.action.reloadWindow");
      else if (choice === "重試") await this.run(!!p.repair);
      else if (choice === "取消更新") await this.setPending(ws, null);
      return;
    }
    this.busy = true;
    try {
      await this.complete(ws);
    } finally {
      this.busy = false;
    }
  }

  async onActivityChange() {
    const ws = workspaceRoot();
    const p = this.pending(ws);
    if (!p || p.phase !== "waiting" || this.busy || !this.activity.items || this.activity.items.length) return;
    this.busy = true;
    try {
      vscode.window.showInformationMessage("Agora：進行中的討論與分派都結束了，開始更新。");
      await this.download(ws, !!p.repair);
    } finally {
      this.busy = false;
    }
  }

  /** 確認實際執行的核心是這次的版本，再執行 agora install 補齊此專案的檔案，install --status 驗收通過才清掉待辦 */
  async complete(ws, note = null) {
    const release = this.lock(ws);
    if (!release) {
      output.appendLine("一鍵更新：另一個視窗正在補齊此專案的檔案，略過。");
      return;
    }
    try {
      await this.fill(ws, note);
    } finally {
      release();
    }
  }

  /** complete 的本體（持有工作區鎖時呼叫）；重試時呼叫自己，不再取鎖 */
  async fill(ws, note) {
    // 核心在啟動時同步；被占用（例如背景程序正在使用）時換不上，這裡再試一次
    bundledCore = syncCore(path.join(extensionPath, "core"), undefined, (m) => output.appendLine(m)) || bundledCore;
    const bundledBuild = readBuild(path.join(extensionPath, "core"));
    const data = await runJson(["parties", "--json"]);
    const core = data.core || { kind: "unknown", version: this.hooks.coreVersion(), root: (agoraCommand() || {}).source };
    let source = "";
    if (!coreMatches(core, bundledBuild, EXTENSION_VERSION)) {
      if (core.kind === "bundled") {
        const retry = await vscode.window.showWarningMessage(
          `Agora：內建核心還沒換成 ${EXTENSION_VERSION}（可能有程序正在使用 ~/.agora/core），先不補齊此專案的檔案。`, "重試");
        if (retry) return this.fill(ws, note);
        return;   // 保留待辦，下次啟動時再試
      }
      const useBundled = "改用內建核心";
      const keep = "仍用目前核心";
      const choice = await vscode.window.showWarningMessage(
        `Agora：目前執行的核心是 ${core.root || "未知位置"}（版本 ${core.version || "未知"}，`
        + `${core.kind === "source" ? "原始碼" : "來源不明"}），不是這次更新的 ${EXTENSION_VERSION}。`
        + "用它補齊的檔案會是它那個版本的內容。", { modal: true }, useBundled, keep);
      if (!choice) return;   // 保留待辦
      if (choice === useBundled) {
        if (!bundledCore) throw new AgoraError("擴充套件內建的核心無法使用，請查看 Agora 輸出面板。");
        // agora.root 優先於 PATH（agoraCommand）；之後要改回原始碼核心，清掉這個設定即可
        await vscode.workspace.getConfiguration("agora").update("root", bundledCore, vscode.ConfigurationTarget.Global);
        output.appendLine(`一鍵更新：已把設定 agora.root 改為內建核心 ${bundledCore}（清掉這個設定即可改回 ${core.root}）。`);
        return this.fill(ws, note);
      }
      source = `（使用 ${core.root} 的核心 ${core.version || ""}）`;
    } else if (core.kind === "source") {
      source = `（使用原始碼核心 ${core.root}）`;
    }
    const out = await runAgora(["install"]);
    const status = await runJson(["install", "--status", "--json"]);
    this.hooks.refresh();
    if (status.state !== "installed") {
      const left = [...(status.missing || []).map((x) => `缺少 ${x}`), ...(status.outdated || []).map((x) => `舊版 ${x}`)];
      const retry = await vscode.window.showWarningMessage(
        `Agora：補齊後仍有項目未完成：${left.join("、") || status.state}。詳細內容在 Agora 輸出面板。`, "重試", "開啟輸出面板");
      if (retry === "開啟輸出面板") output.show(true);
      if (retry === "重試") return this.fill(ws, note);
      return;   // 保留待辦
    }
    await this.setPending(ws, null);
    const first = out.trim().split("\n")[0];
    vscode.window.showInformationMessage(`Agora 已更新到 ${status.version || EXTENSION_VERSION}${source}${note ? `；${note}` : ""}。${first}`);
  }
}

// ───────────────────────── 啟動 ─────────────────────────

function activate(context) {
  extensionPath = context.extensionPath;
  output = vscode.window.createOutputChannel("Agora");
  try {
    bundledCore = syncCore(path.join(extensionPath, "core"), undefined, (m) => output.appendLine(m));
  } catch (e) {
    output.appendLine(`無法準備內建的 Agora 核心：${e.message}`);
  }
  const model = new Model();
  const activity = new Activity();
  const threads = new ThreadsProvider(activity);
  activity.onDidChange(() => threads.refresh());

  const activityBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 51);
  activityBar.command = "workbench.view.extension.agora";
  activity.onDidChange(() => {
    const items = activity.items || [];
    if (!items.length) {
      activityBar.hide();
      return;
    }
    activityBar.text = `$(sync~spin) Agora ${items.length} 進行中`;
    activityBar.tooltip = "Agora 進行中（關閉視窗也會繼續）\n" + items.map((x) => x.type === "discussion"
      ? `💬 ${x.topic}：${x.current ? `${NAMES[x.current]} 回覆中（${minutesSince(x.since)}）` : "啟動中"}`
      : `🛠 ${x.topic}：${NAMES[x.worker]} 接手${RELAY_LABELS[x.status] || x.status}，已跑 ${x.rounds} 輪`
        + ` · 等級 ${tierLabel(x.tier)} · ${relayModelLabel(x)}`
        + (x.denied_summary ? `（⚠️ ${x.denied_summary}）` : "")).join("\n");
    activityBar.show();
  });
  context.subscriptions.push(activityBar);
  const panel = new PartiesView(model);

  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
  statusBar.command = "workbench.view.extension.agora";
  const updateStatusBar = () => {
    if (!model.quota || !workspaceRoot()) {
      statusBar.hide();
      return;
    }
    const enabled = model.enabled();
    const threshold = model.quota.threshold_pct || 5;
    statusBar.text = "$(organization) " + enabled.map((p) => {
      const u = model.quota[p];
      const pct = !u ? "?" : u.blocked ? "✕" : `${Math.round(u.remaining_pct)}%`;
      return `${SHORT[p]} ${pct}`;
    }).join(" · ");
    const low = enabled.some((p) => model.quota[p] && (model.quota[p].blocked || model.quota[p].remaining_pct < threshold));
    statusBar.backgroundColor = low ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
    statusBar.tooltip = "Agora 額度\n" + enabled.map((p) => `${NAMES[p]}：${quotaText(model.quota[p], { multiline: true })}`).join("\n");
    statusBar.show();
  };
  model.onDidChange(updateStatusBar);

  const refreshAll = () => {
    threads.refresh();
    model.loadParties();
  };

  const command = (id, fn) => context.subscriptions.push(
    vscode.commands.registerCommand(id, (...a) => Promise.resolve(fn(...a)).catch(showError)));

  command("agora.refresh", async () => {
    refreshAll();
    model.loadQuota();
    await activity.load();
  });

  command("agora.install", async () => {
    const out = await runAgora(["install"]);
    vscode.window.showInformationMessage(out.trim().split("\n")[0]);   // 第一行：這次改了哪些檔案（完整輸出在 Agora 輸出面板）
    refreshAll();
  });

  command("agora.openTranscript", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await openTranscript(thread);
  });

  command("agora.newThread", async () => {
    await model.loadParties();
    if (!model.installed()) {
      const go = await vscode.window.showWarningMessage("此專案尚未安裝 Agora，要先安裝嗎？", "安裝", "直接建立");
      if (!go) return;
      if (go === "安裝") await vscode.commands.executeCommand("agora.install");
    }
    const topic = await vscode.window.showInputBox({ prompt: "討論主題", ignoreFocusOut: true });
    if (!topic || !topic.trim()) return;
    const enabled = model.enabled();
    let parties = enabled;
    if (enabled.length > 1) {
      const picked = await vscode.window.showQuickPick(
        enabled.map((p) => ({ label: NAMES[p], party: p, picked: true })),
        { canPickMany: true, placeHolder: "參與這個討論串的 AI（全選時，之後在面板勾選的 AI 也會加入；少選則固定為這幾個）" },
      );
      if (!picked || !picked.length) return;
      parties = picked.map((x) => x.party);
    }
    // 全選時不帶 --parties：討論串跟著面板的勾選，之後才勾選的 AI 也會加入
    const partyArgs = parties.length === enabled.length ? [] : ["--parties", parties.join(",")];
    const id = (await runAgora(["new", topic.trim(), ...partyArgs])).trim().split("\n").pop();
    threads.refresh();
    const thread = loadThreads().find((t) => t.id === id);
    if (!thread) return;
    await openTranscript(thread);
    const opening = await vscode.window.showInputBox({
      prompt: "開場訊息（會請所有 AI 依序回覆；留空表示稍後再發言）", ignoreFocusOut: true,
    });
    if (opening && opening.trim()) {
      await runLong("等待 AI 回覆", ["send", id, "--from", "human", "--to", "all"], opening);
    }
  });

  command("agora.send", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const text = await askMessage(`在「${thread.state.topic}」發言`);
    if (!text) return;
    const enabled = model.enabled();
    const inThread = Object.keys(thread.state.parties).filter((p) => enabled.includes(p));
    if (!thread.state.parties_fixed) inThread.push(...enabled.filter((p) => !inThread.includes(p)));   // 之後才勾選的 AI（同 cli.thread_parties）
    const target = await vscode.window.showQuickPick(
      [
        { label: "$(organization) 所有 AI 依序回覆", description: inThread.map((p) => NAMES[p]).join("、"), to: "all" },
        { label: "$(eye-closed) 所有 AI 各自獨立作答", description: "看不到同一輪其他人的答案，適合決策題，之後再請大家互評", to: "all", blind: true },
        ...enabled.map((p) => ({ label: `$(person) 只請 ${NAMES[p]} 回覆`, to: p })),
        { label: "$(note) 只記錄，不請任何人回覆", to: "none" },
      ],
      { placeHolder: "請誰回覆？" },
    );
    if (!target) return;
    await openTranscript(thread);
    await runLong(target.to === "none" ? "記錄訊息" : "等待 AI 回覆",
      ["send", thread.id, "--from", "human", "--to", target.to, ...(target.blind ? ["--blind"] : [])], text);
  });

  command("agora.auto", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const rounds = await vscode.window.showInputBox({
      prompt: "讓 AI 依序自動發言幾輪？（各方都表示【已達成共識】會提前結束）", value: "4",
      validateInput: (v) => (/^\d+$/.test(v) && +v > 0 ? undefined : "請輸入正整數"),
    });
    if (!rounds) return;
    await openTranscript(thread);
    await runLong("AI 自動討論中", ["auto", thread.id, "--rounds", rounds]);
  });

  command("agora.assign", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const summary = thread.state.summary;
    let fromSummary = false;
    if (summary) {
      const source = await vscode.window.showQuickPick(
        [
          { label: "$(checklist) 使用總結的工作單", description: `${NAMES[summary.by] || summary.by} · ${summary.time}`, value: true },
          { label: "$(edit) 自己輸入工作單", value: false },
        ],
        { placeHolder: "工作單來源" },
      );
      if (!source) return;
      fromSummary = source.value;
      if (fromSummary && summary.error) {
        vscode.window.showErrorMessage(`Agora：無法依總結分派：${summary.error}。請重新總結或自己輸入工作單。`);
        return;
      }
    }
    const enabled = model.enabled();
    const worker = await vscode.window.showQuickPick(
      [
        { label: "$(sparkle) 依分配比例自動挑選", description: "略過額度不足與比例為 0 的 AI", to: null },
        ...enabled.map((p) => ({
          label: NAMES[p], description: model.quota ? quotaText(model.quota[p]) : "", to: p,
        })),
      ],
      { placeHolder: "把工作交給誰？接手方在自己的 branch／worktree 背景執行，完成後由 Agora commit，不 push、不 merge" },
    );
    if (!worker) return;
    // 分派會建立獨立 branch／worktree，必須列出允許修改的範圍（agora assign --allow，可多個）
    const allow = await vscode.window.showInputBox({
      prompt: "允許接手方修改的範圍（相對路徑或 glob，多個用空白分隔，例如 src/** tests/**）",
      ignoreFocusOut: true, validateInput: (v) => (v.trim() ? undefined : "至少要列一個範圍"),
    });
    if (!allow) return;
    let reason = [];
    if (worker.to) {
      const r = await vscode.window.showInputBox({ prompt: `指定 ${NAMES[worker.to]} 的理由（--reason 必填）`, ignoreFocusOut: true });
      if (!r || !r.trim()) return;
      reason = ["--reason", r.trim()];
    }
    const args = ["assign", thread.id, "--from", "human", ...(worker.to ? ["--to", worker.to, ...reason] : []),
      ...allow.trim().split(/\s+/).flatMap((a) => ["--allow", a])];
    let out;
    if (fromSummary) {
      try {
        out = await runAgora([...args, "--from-summary"]);
      } catch (e) {
        if (!(e instanceof AgoraError) || !e.message.includes("總結可能已過期")) throw e;
        const ok = await vscode.window.showWarningMessage(e.message.replace(/確認仍要.*$/, "").trim(), { modal: true }, "仍要分派");
        if (!ok) return;
        out = await runAgora([...args, "--from-summary", "--yes"]);
      }
    } else {
      const text = await askMessage("工作單：範圍、不能碰的檔案、驗收方式");
      if (!text) return;
      out = await runAgora(args, { input: text });
    }
    const lines = out.trim().split("\n").filter((l) => !l.startsWith("進度：") && !l.startsWith("查詢："));
    vscode.window.showInformationMessage(lines.join(" "));
    threads.refresh();
  });

  command("agora.openLive", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    await activity.load();
    const live = (activity.items || []).filter((x) => x.thread === thread.id && x.live).map((x) => x.live);
    if (!live.length) {
      vscode.window.showInformationMessage(`「${thread.state.topic}」目前沒有進行中的回覆。`);
      return;
    }
    for (const file of live) await vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(file));
  });

  command("agora.summarize", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    await model.loadParties();
    const def = model.summarizer;
    const parties = [...model.enabled()].sort((a, b) => (b === def) - (a === def));
    const picked = await vscode.window.showQuickPick(
      parties.map((p) => ({
        label: NAMES[p], party: p,
        description: [p === def ? "預設總結方" : "", model.quota ? quotaText(model.quota[p]) : ""].filter(Boolean).join(" · "),
      })),
      { placeHolder: "由誰總結？會開新的對話讀完整逐字稿，產出共識、取捨、異議與工作單" },
    );
    if (!picked) return;
    await runLong(`等待 ${picked.label} 產出總結`, ["summarize", thread.id, "--by", picked.party]);
    const file = path.join(thread.dir, "summary.md");
    if (fs.existsSync(file)) await vscode.commands.executeCommand("markdown.showPreview", vscode.Uri.file(file));
  });

  command("agora.changes", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const data = JSON.parse(await runAgora(["changes", thread.id, "--json"], { log: false }));
    if (!data.files.length) {
      vscode.window.showInformationMessage(`「${thread.state.topic}」分派期間沒有檔案變更。`);
      return;
    }
    const picked = await vscode.window.showQuickPick(
      data.files.map((f) => ({ label: f.path, description: STATUS_LABELS[f.status] || f.status, file: f })),
      { placeHolder: `分派期間的變更（${data.since} 起，期間任何人的修改都會列出）：選一個檔案檢視差異` },
    );
    if (!picked) return;
    const f = picked.file;
    const left = baselineUri(data.baseline, f.path, f.status === "A");
    // 隔離分派（worktree）：右側讀任務 branch 已提交的內容，不是主工作區（尚未 merge 時主工作區是舊內容或沒有新檔）
    const right = f.status === "D" ? baselineUri(data.baseline, f.path, true)
      : data.worktree && data.ref ? baselineUri(data.ref, f.path, false) : vscode.Uri.file(path.join(workspaceRoot(), f.path));
    await vscode.commands.executeCommand("vscode.diff", left, right, `${f.path}（分派基準 ↔ 目前）`);
  });

  command("agora.status", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const out = await runAgora(["status", thread.id]);
    output.show(true);
    const choice = await vscode.window.showInformationMessage(out.trim().split("\n")[0], "開啟進度檔");
    if (choice) {
      await vscode.window.showTextDocument(vscode.Uri.file(path.join(thread.dir, "progress.md")));
    }
  });

  command("agora.recall", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const ok = await vscode.window.showWarningMessage(
      `要請 ${NAMES[thread.state.relay && thread.state.relay.worker] || "接手方"} 在下一個檢查點暫停嗎？`,
      { modal: true }, "收回");
    if (!ok) return;
    vscode.window.showInformationMessage((await runAgora(["recall", thread.id])).trim());
    threads.refresh();
  });

  const fmtSize = (bytes) => (bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.ceil(bytes / 1024)} KB`);
  const sessionNames = (info) => [...new Set(info.sessions.map((s) => NAMES[s.party]))].join("、") || "無";

  command("agora.prune", async () => {
    const days = await vscode.window.showInputBox({
      prompt: "清理超過幾天沒有活動的討論串？（標記保留、分派工作未完成、使用中的一律不清）", value: "30",
      validateInput: (v) => (/^\d+(\.\d+)?$/.test(v) ? undefined : "請輸入天數"),
    });
    if (!days) return;
    const scan = JSON.parse(await runAgora(["prune", "--days", days, "--json"], { log: false }));
    if (!scan.candidates.length) {
      vscode.window.showInformationMessage(`沒有可清理的討論串（另有 ${scan.kept.length} 個保留）。`);
      return;
    }
    const picked = await vscode.window.showQuickPick(
      scan.candidates.map((c) => ({
        label: c.topic, picked: true, info: c,
        description: `閒置 ${c.idle_days} 天 · ${c.messages} 則 · ${fmtSize(c.bytes)}`,
        detail: `${c.id} · AI 對話紀錄：${sessionNames(c)}`,
      })),
      { canPickMany: true, placeHolder: `勾選要刪除的討論串（另有 ${scan.kept.length} 個不符合條件、會保留）` },
    );
    if (!picked || !picked.length) return;
    const ok = await vscode.window.showWarningMessage(
      `刪除 ${picked.length} 個討論串，以及 Agora 為它們在各 AI CLI 開的對話紀錄？此動作無法復原。`,
      { modal: true }, "刪除", "只刪討論串，保留對話紀錄");
    if (!ok) return;
    const args = ["prune", "--yes", ...picked.flatMap((x) => ["--thread", x.info.id])];
    if (ok !== "刪除") args.push("--keep-sessions");
    const result = JSON.parse(await runAgora([...args, "--json"]));
    threads.refresh();
    const msg = `已刪除 ${result.deleted.length} 個討論串。`;
    if (result.errors.length) {
      output.appendLine(result.errors.join("\n"));
      vscode.window.showWarningMessage(`${msg}有 ${result.errors.length} 筆對話紀錄沒有刪除，詳見 Agora 輸出。`);
    } else {
      vscode.window.showInformationMessage(msg);
    }
  });

  command("agora.deleteThread", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const scan = JSON.parse(await runAgora(["prune", "--thread", thread.id, "--json"], { log: false }));
    if (!scan.candidates.length) {
      vscode.window.showWarningMessage(`「${thread.state.topic}」不能刪除：${scan.kept[0].reason}。`);
      return;
    }
    const ok = await vscode.window.showWarningMessage(
      `刪除「${thread.state.topic}」？會一併刪除 Agora 為它在各 AI CLI 開的對話紀錄（${sessionNames(scan.candidates[0])}），無法復原。`,
      { modal: true }, "刪除", "只刪討論串，保留對話紀錄");
    if (!ok) return;
    await runAgora(["prune", "--yes", "--thread", thread.id, ...(ok === "刪除" ? [] : ["--keep-sessions"])]);
    threads.refresh();
  });

  command("agora.keep", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await runAgora(["keep", thread.id]);
    threads.refresh();
  });

  command("agora.unkeep", async (arg) => {
    const thread = await pickThread(arg);
    if (thread) await runAgora(["keep", thread.id, "--off"]);
    threads.refresh();
  });

  command("agora.stopJob", async (arg) => {
    const thread = await pickThread(arg);
    if (!thread) return;
    const ok = await vscode.window.showWarningMessage(
      `停止「${thread.state.topic}」進行中的討論？已收到的回覆都已寫入逐字稿。`, { modal: true }, "停止");
    if (!ok) return;
    vscode.window.showInformationMessage((await runAgora(["stop", thread.id])).trim());
    activity.load();
  });

  command("agora.quota", async () => {
    await runLong("查詢額度", ["quota", "--refresh"]);
    output.show(true);
    model.loadQuota();
  });

  command("agora.build", async () => {
    try {
      await runLong("執行建置", ["build"]);
      vscode.window.showInformationMessage("Agora：建置完成。");
    } finally {
      output.show(true);
    }
  });

  command("agora.check", async () => {
    try {
      await runLong("執行驗證", ["check"]);
      vscode.window.showInformationMessage("Agora：驗證通過。");
    } finally {
      output.show(true);
    }
  });

  const updater = new Updater(context);
  command("agora.checkUpdate", () => updater.check(true));
  const oneClick = new OneClickUpdate(context, updater, activity,
    { coreVersion: () => model.coreVersion, refresh: () => { refreshAll(); model.loadVersion(); } });
  command("agora.update", () => oneClick.run(false));
  command("agora.repair", () => oneClick.run(true));
  // 一鍵更新重新載入後接續：補齊此專案的檔案（等啟動時的其他查詢先跑）
  const resumeDelay = setTimeout(() => oneClick.resume().catch(showError), 3 * 1000);
  context.subscriptions.push({ dispose: () => clearTimeout(resumeDelay) });
  const updateDelay = setTimeout(() => updater.check(), 15 * 1000);   // 不和啟動時的其他查詢搶時間
  const updateTimer = setInterval(() => updater.check(), 3600 * 1000);   // 是否真的查詢由 UPDATE_INTERVAL 決定
  context.subscriptions.push({ dispose: () => { clearTimeout(updateDelay); clearInterval(updateTimer); } });

  context.subscriptions.push(
    output,
    statusBar,
    vscode.window.registerWebviewViewProvider("agora.parties", panel),
    vscode.window.registerTreeDataProvider("agora.threads", threads),
    vscode.workspace.registerTextDocumentContentProvider("agora-base", new BaselineProvider()),
  );

  // 討論串與設定檔變動時自動更新（AI 回覆、背景接手、從終端機改設定）
  const ws = workspaceRoot();
  if (ws) {
    const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(ws, ".agora/**/*.json"));
    let timer;
    const onChange = (uri) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        threads.refresh();
        model.loadParties();   // 設定變更，或分派進度改變了近期工作量
        if (uri && uri.fsPath.endsWith("config.json")) model.loadQuota();
        activity.load();
      }, 300);
    };
    watcher.onDidChange(onChange);
    watcher.onDidCreate(onChange);
    watcher.onDidDelete(onChange);
    context.subscriptions.push(watcher);
  }

  context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
    if (e.affectsConfiguration("agora")) refreshAll();
  }));

  const minutes = vscode.workspace.getConfiguration("agora").get("quotaRefreshMinutes");
  if (ws && fs.existsSync(path.join(ws, ".agora"))) {
    model.loadParties();
    model.loadQuota();
    activity.load();
  }
  const activityTimer = setInterval(() => {
    if (activity.items && activity.items.length) activity.load();   // 更新經過時間、偵測被強制結束的程序
  }, 30 * 1000);
  context.subscriptions.push({ dispose: () => clearInterval(activityTimer) });
  if (minutes > 0) {
    const interval = setInterval(() => model.loadQuota(), minutes * 60 * 1000);
    context.subscriptions.push({ dispose: () => clearInterval(interval) });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
