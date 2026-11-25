"""跨機器可攜 hook 指令與 .agora/hook.py 啟動器回歸測試（Refs #9）"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import cli, config, parties, quota  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout


def _make_repo(tmp: str) -> Path:
    root = Path(tmp)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "user.email", "t@t")
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _make_fake_agora(base: Path) -> Path:
    shutil.copytree(cli.AGORA_ROOT_DIR / "skills", base / "skills")
    (base / "agoralib").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(cli.AGORA_ROOT_DIR / "agoralib" / "quota_hook.py", base / "agoralib" / "quota_hook.py")
    return base


def _run_codex_hook(command: str, cwd: Path, payload: dict, env: dict) -> subprocess.CompletedProcess:
    """以 shell 執行 .codex/hooks.json 的指令（將開頭 python 換成目前測試用 sys.executable）"""
    assert command.startswith("python ")
    shell_cmd = f'"{sys.executable}" {command[len("python "):]}'
    return subprocess.run(
        shell_cmd, cwd=cwd, shell=True, input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )


def _run_agy_hook(command: str, cwd: Path, payload: dict, env: dict) -> subprocess.CompletedProcess:
    """模擬 Antigravity 直接依空白切參數執行 .agents/hooks.json 的指令（不經 shell、不處理引號）"""
    parts = command.split(" ")
    assert parts[0] == "python"
    return subprocess.run(
        [sys.executable, *parts[1:]], cwd=cwd, input=json.dumps(payload),
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60,
    )


class TestPortableHooks(unittest.TestCase):
    def _install(self, ws: cli.Workspace, home: Path, agora_root: Path | None = None) -> str:
        out = io.StringIO()
        ctx_root = patch.object(cli, "AGORA_ROOT_DIR", agora_root) if agora_root else patch.object(cli, "AGORA_ROOT_DIR", cli.AGORA_ROOT_DIR)
        with patch.object(Path, "home", return_value=home), ctx_root, \
                patch.object(parties, "_CMD", "agora"), patch("sys.stdout", out), patch.dict(os.environ):
            os.environ.pop(parties.NESTED_ENV, None)
            cli.cmd_install(ws, None)
        return out.getvalue()

    def _clean_env(self, **extra: str) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in (
            parties.NESTED_ENV, quota.PROBE_ENV, "AGORA_SUBAGENTS",
            "CLAUDE_PROJECT_DIR", "AGORA_WORKSPACE", "AGORA_WORKTREE", "AGORA_CONTROL_ROOT",
        )}
        env.update(extra)
        return env

    def test_two_machines_produce_byte_identical_hooks_json(self):
        """兩台不同家目錄（含空白與類 Windows 短路徑）install 產出的 .codex/hooks.json 與 .agents/hooks.json 位元組完全相同"""
        with tempfile.TemporaryDirectory() as ws1_tmp, tempfile.TemporaryDirectory() as ws2_tmp, \
                tempfile.TemporaryDirectory() as env_tmp:
            env = Path(env_tmp)
            home1 = env / "machine_a" / "Richard Clayd"
            home2 = env / "machine_b" / "RICHAR~1"
            root1 = _make_fake_agora(home1 / ".agora" / "core")
            root2 = _make_fake_agora(home2 / "AGORA~1" / "core")

            ws1, ws2 = cli.Workspace(Path(ws1_tmp)), cli.Workspace(Path(ws2_tmp))
            self._install(ws1, home1, root1)
            self._install(ws2, home2, root2)

            for rel in (".codex/hooks.json", ".agents/hooks.json"):
                b1 = (ws1.root / rel).read_bytes()
                b2 = (ws2.root / rel).read_bytes()
                self.assertEqual(b1, b2, rel)
                text = b1.decode("utf-8")
                self.assertNotIn("Richard Clayd", text, rel)
                self.assertNotIn("RICHAR~1", text, rel)

            # Claude 的 .claude/settings.local.json 是個人本地設定（不進 git），維持絕對路徑不依賴工作目錄
            claude1 = (ws1.root / ".claude" / "settings.local.json").read_text(encoding="utf-8")
            self.assertIn(root1.as_posix(), claude1)
            self.assertIn("quota_hook.py", claude1)

            # 本機路徑也寫在受 git 忽略的 .agora/hook.py 啟動器裡
            self.assertIn(root1.as_posix(), (ws1.root / ".agora" / "hook.py").read_text(encoding="utf-8"))
            self.assertIn(root2.as_posix(), (ws2.root / ".agora" / "hook.py").read_text(encoding="utf-8"))

    def test_antigravity_hook_commands_have_no_quotes_or_path_spaces(self):
        """Antigravity 執行 hook 時不處理引號：指令不能含引號，參數內也不能含空白"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            ws = cli.Workspace(Path(tmp))
            home = Path(home_tmp) / "User With Space"
            agora_root = _make_fake_agora(home / ".agora" / "core")
            self._install(ws, home, agora_root)

            data = json.loads((ws.root / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            cmds = cli._all_commands(data)
            self.assertTrue(cmds)
            for cmd in cmds:
                self.assertNotIn('"', cmd)
                self.assertNotIn("'", cmd)
                parts = cmd.split(" ")
                self.assertEqual(len(parts), 4, cmd)
                self.assertEqual(parts[:2], ["python", "-c"])
                self.assertIn(parts[3], ("agy-pre", "agy-tool"))

    def test_migrates_legacy_formats_and_install_status_detects_them(self):
        """舊格式（絕對路徑、8.3 短路徑、舊 legacy agora.py、舊版相對路徑）能被 install_status 判為 outdated 並遷移"""
        legacy_cases = [
            (
                "absolute_path",
                'python "C:/Users/Other Machine/.agora/core/agoralib/quota_hook.py" codex-prompt',
                'python "C:/Users/Other Machine/.agora/core/agoralib/quota_hook.py" claude-prompt',
                'python "C:/Users/Other Machine/.agora/core/agoralib/quota_hook.py" claude-tool',
                'python C:/Users/OTHERM~1/AGORA~1/core/agoralib/QUOTA_~1.PY agy-pre',
                'python C:/Users/OTHERM~1/AGORA~1/core/agoralib/QUOTA_~1.PY agy-tool',
            ),
            (
                "short_path_and_legacy_agora_py",
                'python D:/github/agora/agora.py codex-prompt',
                'python C:/Users/RICHAR~1/AGORA~1/core/agoralib/QUOTA_~1.PY claude-prompt',
                'python "C:/Users/Richard Clayd/agora/agora.py" claude-tool',
                'python D:/github/agora/agora.py agy-pre',
                'python C:/Users/RICHAR~1/AGORA~1/core/agoralib/QUOTA_~1.PY agy-tool',
            ),
            (
                "old_relative_launcher_cmd",
                'python .agora/hook.py codex-prompt',
                'python .agora/hook.py claude-prompt',
                'python .agora/hook.py claude-tool',
                'python .agora/hook.py agy-pre',
                'python .agora/hook.py agy-tool',
            ),
        ]
        for label, codex_cmd, claude_p, claude_t, agy_pre, agy_tool in legacy_cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
                ws, home = cli.Workspace(Path(tmp)), Path(home_tmp)
                self._install(ws, home)
                (ws.root / ".agora" / "hook.py").unlink()

                mine = {"hooks": [{"type": "command", "command": "echo mine"}]}
                (ws.root / ".codex" / "hooks.json").write_text(json.dumps({
                    "hooks": {"UserPromptSubmit": [mine, {"hooks": [{"type": "command", "command": codex_cmd, "timeout": 15}]}]}
                }), encoding="utf-8")
                (ws.root / ".claude" / "settings.local.json").write_text(json.dumps({
                    "hooks": {
                        "UserPromptSubmit": [mine, {"hooks": [{"type": "command", "command": claude_p, "timeout": 15}]}],
                        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": claude_t, "timeout": 15}]}],
                    }
                }), encoding="utf-8")
                (ws.root / ".agents" / "hooks.json").write_text(json.dumps({
                    "agora-quota": {"PreInvocation": [{"type": "command", "command": agy_pre, "timeout": 15}]},
                    "agora-subagents": {"PreToolUse": [
                        {"matcher": t, "hooks": [{"type": "command", "command": agy_tool, "timeout": 15}]}
                        for t in parties.AGY_SUBAGENT_TOOLS
                    ]},
                }), encoding="utf-8")

                with patch.object(Path, "home", return_value=home):
                    st = cli.install_status(ws)
                self.assertEqual(st["state"], "outdated", st)
                self.assertEqual(st["missing"], [], st)
                for expected in ("Codex hook", "Claude 額度 hook", "Antigravity 額度 hook", "Antigravity 子代理開關 hook",
                                 "Hook 啟動器（.agora/hook.py）"):
                    self.assertIn(expected, st["outdated"])

                self._install(ws, home)
                with patch.object(Path, "home", return_value=home):
                    st_after = cli.install_status(ws)
                self.assertEqual(st_after["state"], "installed", st_after)

                plan = cli.install_plan()
                codex_groups = json.loads((ws.root / ".codex" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["UserPromptSubmit"]
                self.assertEqual(codex_groups[0], mine)
                self.assertEqual([h["command"] for g in codex_groups[1:] for h in g["hooks"]],
                                 [f"{plan['hook']} codex-prompt"])
                claude_groups = json.loads((ws.root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))["hooks"]
                self.assertEqual(claude_groups["UserPromptSubmit"][0], mine)
                self.assertEqual([h["command"] for g in claude_groups["UserPromptSubmit"][1:] for h in g["hooks"]],
                                 [f"{plan['claude_hook']} claude-prompt"])
                self.assertEqual([h["command"] for g in claude_groups["PostToolUse"] for h in g["hooks"]],
                                 [f"{plan['claude_hook']} claude-tool"])

    def test_reinstall_new_format_does_not_rewrite_files_or_change_mtime(self):
        """已是新格式時重跑 install 不改寫檔案（mtime、CRLF 與自訂順序都保留）"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            ws, home = cli.Workspace(Path(tmp)), Path(home_tmp)
            self._install(ws, home)
            plan = cli.install_plan()

            codex_path = ws.root / ".codex" / "hooks.json"
            codex_data = {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": f"{plan['hook']} codex-prompt", "timeout": 15}]},
                        {"hooks": [{"type": "command", "command": "echo after"}]},
                    ]
                }
            }
            codex_path.write_bytes((json.dumps(codex_data, indent=2) + "\n").replace("\n", "\r\n").encode("utf-8"))
            agy_path = ws.root / ".agents" / "hooks.json"
            agy_path.write_bytes(agy_path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))

            targets = [codex_path, agy_path, ws.root / ".claude" / "settings.local.json", ws.root / ".agora" / "hook.py"]
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in targets}

            out = self._install(ws, home)
            self.assertIn("沒有改動任何檔案", out)
            self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in targets}, before)

    def test_install_status_other_alive_and_dead_launcher_roots(self):
        """install_status 正確辨識另一份仍存在的 Agora（installed + different）與已消失的 Agora（outdated）"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp, \
                tempfile.TemporaryDirectory() as other_tmp:
            ws, home = cli.Workspace(Path(tmp)), Path(home_tmp)
            other_root = _make_fake_agora(Path(other_tmp) / "other agora")
            self._install(ws, home, other_root)

            with patch.object(Path, "home", return_value=home), patch.object(parties, "_CMD", "agora"):
                st = cli.install_status(ws)
            self.assertEqual(st["state"], "installed", st)
            self.assertTrue(st["source"]["different"])
            self.assertEqual(st["source"]["installed"], other_root.as_posix())

            (other_root / "agoralib" / "quota_hook.py").unlink()
            with patch.object(Path, "home", return_value=home), patch.object(parties, "_CMD", "agora"):
                st_dead = cli.install_status(ws)
            self.assertEqual(st_dead["state"], "outdated", st_dead)
            self.assertTrue(any("已不存在的 Agora" in x for x in st_dead["outdated"]))

    def test_install_warns_when_modifying_git_tracked_hooks_and_silent_when_unchanged(self):
        """install 修改受 git 追蹤的檔案時輸出明確警告；已是新格式不改檔時不輸出修改警告"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp:
            root = _make_repo(tmp)
            home = Path(home_tmp)
            ws = cli.Workspace(root)

            codex_file = root / ".codex" / "hooks.json"
            codex_file.parent.mkdir(parents=True)
            codex_file.write_text(json.dumps({
                "hooks": {"UserPromptSubmit": [{"hooks": [{
                    "type": "command",
                    "command": 'python "C:/Users/Old User/.agora/core/agoralib/quota_hook.py" codex-prompt',
                    "timeout": 15,
                }]}]}
            }, indent=2) + "\n", encoding="utf-8")
            _git(root, "add", ".codex/hooks.json")
            _git(root, "commit", "-qm", "track legacy codex hooks")

            out1 = self._install(ws, home)
            self.assertIn("修改了以下受 git 追蹤的檔案", out1)
            self.assertIn(".codex/hooks.json", out1)

            _git(root, "add", ".codex/hooks.json")
            _git(root, "commit", "-qm", "migrate to portable hook")
            out2 = self._install(ws, home)
            self.assertIn("沒有改動任何檔案", out2)
            self.assertNotIn("修改了以下受 git 追蹤的檔案", out2)

    def test_hooks_work_in_root_subdir_worktree_and_never_exit_2_when_launcher_missing(self):
        """實測根目錄、子目錄、分派 worktree 與啟動器不存在四種情境：皆能正確執行或靜默 exit 0（絕不回傳 exit 2）"""
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home_tmp, \
                tempfile.TemporaryDirectory() as outside_tmp:
            ws, home = cli.Workspace(Path(tmp)), Path(home_tmp)
            self._install(ws, home)
            config.update_parties(ws.root, disable=["claude"])

            codex_cmd = json.loads((ws.root / ".codex" / "hooks.json").read_text(encoding="utf-8"))[
                "hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
            agy_hooks = json.loads((ws.root / ".agents" / "hooks.json").read_text(encoding="utf-8"))
            agy_pre_cmd = agy_hooks["agora-quota"]["PreInvocation"][0]["command"]
            agy_tool_cmd = agy_hooks["agora-subagents"]["PreToolUse"][0]["hooks"][0]["command"]
            claude_cmd = json.loads((ws.root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))[
                "hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

            subdir = ws.root / "src" / "nested"
            subdir.mkdir(parents=True)
            worktree_dir = ws.root / ".agora" / "worktrees" / "fake-dispatch-id"
            worktree_dir.mkdir(parents=True)
            outside = Path(outside_tmp)

            # 1) 根目錄、子目錄、分派 worktree（內無 .agora/hook.py）：Codex 與 Antigravity 皆找到啟動器並成功執行
            for label, cwd, extra_env in (
                ("root", ws.root, {}),
                ("subdir", subdir, {}),
                ("worktree_no_env", worktree_dir, {}),
                ("worktree_with_control_root", worktree_dir, {"AGORA_CONTROL_ROOT": str(ws.root), "AGORA_WORKTREE": str(worktree_dir)}),
            ):
                with self.subTest(scenario=label):
                    p_codex = _run_codex_hook(codex_cmd, cwd, {"prompt": "幫我修 bug"}, self._clean_env(**extra_env))
                    self.assertEqual(p_codex.returncode, 0, (label, p_codex.stderr))
                    ctx = json.loads(p_codex.stdout)["hookSpecificOutput"]["additionalContext"]
                    self.assertIn("【額度】", ctx)
                    if label != "worktree_no_env":
                        self.assertIn("【Agora 勾選】", ctx)

                    p_tool = _run_agy_hook(
                        agy_tool_cmd, cwd, {"toolCall": {"name": "invoke_subagent"}},
                        self._clean_env(AGORA_SUBAGENTS="0", **extra_env),
                    )
                    self.assertEqual(p_tool.returncode, 0, (label, p_tool.stderr))
                    self.assertEqual(json.loads(p_tool.stdout)["decision"], "deny")

                    p_pre = _run_agy_hook(agy_pre_cmd, cwd, {"invocationNum": 1}, self._clean_env(**extra_env))
                    self.assertEqual(p_pre.returncode, 0, (label, p_pre.stderr))
                    self.assertIn("【額度】", json.loads(p_pre.stdout)["injectSteps"][0]["ephemeralMessage"])

            # 2) 外部 worktree 目錄搭配 AGORA_CONTROL_ROOT：也能找到主工作區的啟動器並正常執行
            p_ext = _run_agy_hook(
                agy_tool_cmd, outside, {"toolCall": {"name": "invoke_subagent"}},
                self._clean_env(AGORA_SUBAGENTS="0", AGORA_CONTROL_ROOT=str(ws.root)),
            )
            self.assertEqual(p_ext.returncode, 0, p_ext.stderr)
            self.assertEqual(json.loads(p_ext.stdout)["decision"], "deny")

            # 3) Claude hook 維持絕對路徑：即使 cwd 切到外部目錄、或啟動器被刪除，仍照常執行
            (ws.root / ".agora" / "hook.py").unlink()
            p_claude = _run_codex_hook(
                claude_cmd, outside, {"prompt": "hi"},
                self._clean_env(CLAUDE_PROJECT_DIR=str(ws.root)),
            )
            self.assertEqual(p_claude.returncode, 0, p_claude.stderr)
            self.assertIn("【額度】", json.loads(p_claude.stdout)["hookSpecificOutput"]["additionalContext"])

            # 4) 啟動器不存在（已刪除，或在完全沒有 .agora/hook.py 的外部目錄）：Codex 與 Antigravity 必須 exit 0，絕不 exit 2
            for label, cwd in (("deleted_in_root", ws.root), ("deleted_in_worktree", worktree_dir), ("outside_no_launcher", outside)):
                with self.subTest(missing_launcher=label):
                    p_codex = _run_codex_hook(codex_cmd, cwd, {"prompt": "hi"}, self._clean_env())
                    self.assertEqual(p_codex.returncode, 0, (label, p_codex.stderr))
                    self.assertEqual(p_codex.stdout.strip(), "")

                    p_pre = _run_agy_hook(agy_pre_cmd, cwd, {"invocationNum": 1}, self._clean_env())
                    self.assertEqual(p_pre.returncode, 0, (label, p_pre.stderr))
                    self.assertEqual(p_pre.stdout.strip(), "")

                    p_tool = _run_agy_hook(agy_tool_cmd, cwd, {"toolCall": {"name": "invoke_subagent"}}, self._clean_env(AGORA_SUBAGENTS="0"))
                    self.assertEqual(p_tool.returncode, 0, (label, p_tool.stderr))
                    self.assertEqual(p_tool.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
