"""測試不同的 Agora 安裝來源（面板與終端機）狀態偵測，以及討論提示中的工具限制說明"""

import argparse
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import cli, quota
from agoralib.parties import AGORA_ROOT as AGORA_ROOT_DIR

_TEST_HOME = Path(tempfile.mkdtemp())
quota.BLOCKED_FILE = _TEST_HOME / "blocked.json"
cli.INSTALL_WRITES = _TEST_HOME / "install_writes.jsonl"


def state_with(parties, messages=()):
    return {
        "topic": "測試主題",
        "parties": {p: {"session_id": None, "seen": 0, "model": None} for p in parties},
        "messages": [{"speaker": s, "text": t, "time": "now", "via_cli": v} for s, t, v in messages],
    }


class TestInstallSource(unittest.TestCase):
    def test_install_from_different_source_and_outdated(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as custom_agora:
            ws = cli.Workspace(Path(tmp))
            custom_root = Path(custom_agora)
            shutil.copytree(cli.AGORA_ROOT_DIR / "skills", custom_root / "skills")
            (custom_root / "agoralib").mkdir()
            shutil.copyfile(cli.AGORA_ROOT_DIR / "agoralib" / "quota_hook.py", custom_root / "agoralib" / "quota_hook.py")

            with patch.object(Path, "home", return_value=Path(home)):
                # 以「另一份 Agora 核心」執行 install
                with patch.object(cli, "AGORA_ROOT_DIR", custom_root):
                    cli.cmd_install(ws, None)

                # 用目前核心檢查：state 為 installed、source.different 為 True
                status = cli.install_status(ws)
                self.assertEqual(status["state"], "installed")
                self.assertTrue(status["source"]["different"])
                self.assertEqual(Path(status["source"]["expected"]).resolve(), cli.AGORA_ROOT_DIR.resolve())
                self.assertEqual(Path(status["source"]["installed"]).resolve(), custom_root.resolve())
                self.assertEqual(status["missing"], [])
                self.assertEqual(status["outdated"], [])
                self.assertTrue(len(status["source"]["items"]) > 0)

                # 文字輸出（agora install --status）測試：應包含安裝來源說明
                out = io.StringIO()
                with patch("sys.stdout", out):
                    cli.cmd_install(ws, argparse.Namespace(status=True, json=False))
                text = out.getvalue()
                self.assertIn("已安裝（目前版本）", text)
                self.assertIn(f"安裝來源：{status['source']['installed']}（目前執行的是 {status['source']['expected']}）", text)
                self.assertIn("內容是目前版本，只有 Agora 位置不同。要統一請固定用同一種方式安裝（面板或終端機）。", text)

                # 把某個 skill 改成舊內容後 → 狀態轉為 outdated
                skill = ws.root / ".claude" / "skills" / "agora" / "SKILL.md"
                skill.write_text("舊版內容", encoding="utf-8")
                status = cli.install_status(ws)
                self.assertEqual(status["state"], "outdated")
                self.assertIn("Claude Code skill「agora」", status["outdated"])


class TestInstallSourceEdges(unittest.TestCase):
    """交叉審查指出的邊界：來源已不存在、設定檔損壞、hook 指向不只一個來源"""

    def install_from(self, ws, home, root):
        shutil.copytree(cli.AGORA_ROOT_DIR / "skills", root / "skills")
        (root / "agoralib").mkdir()
        shutil.copyfile(cli.AGORA_ROOT_DIR / "agoralib" / "quota_hook.py", root / "agoralib" / "quota_hook.py")
        with patch.object(Path, "home", return_value=Path(home)), patch.object(cli, "AGORA_ROOT_DIR", root), \
                patch("sys.stdout", io.StringIO()):
            cli.cmd_install(ws, None)

    def test_missing_source_is_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as other:
            ws = cli.Workspace(Path(tmp))
            self.install_from(ws, home, Path(other) / "core")
            shutil.rmtree(Path(other) / "core")   # 那份 Agora 被刪除或搬走：hook 無法執行
            with patch.object(Path, "home", return_value=Path(home)):
                status = cli.install_status(ws)
            self.assertEqual(status["state"], "outdated")
            self.assertTrue(any("已不存在的 Agora" in x for x in status["outdated"]))

    def test_corrupt_settings_do_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            ws = cli.Workspace(Path(tmp))
            (ws.root / ".claude").mkdir()
            (ws.root / ".claude" / "settings.local.json").write_text(
                '{"hooks": {"x": [{"hooks": [{"command": "python C:\\bad\\agoralib\\quota_hook.py"}]}]}', encoding="utf-8")
            with patch.object(Path, "home", return_value=Path(home)):
                status = cli.install_status(ws)   # 不是合法 JSON：略過該檔，不中斷
            self.assertIn(status["state"], ("missing", "partial", "outdated"))

    def test_mixed_sources_are_all_recognized(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home, \
                tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            ws = cli.Workspace(Path(tmp))
            self.install_from(ws, home, Path(a) / "core")
            claude = (ws.root / ".claude" / "settings.local.json").read_text(encoding="utf-8")
            self.install_from(ws, home, Path(b) / "core")
            # Claude 的 hook 指回 A，其他 hook 是 B：兩個來源都要認得，不能把 B 的項目判成舊版
            (ws.root / ".claude" / "settings.local.json").write_text(claude, encoding="utf-8")
            with patch.object(Path, "home", return_value=Path(home)):
                status = cli.install_status(ws)
            self.assertTrue(status["source"]["mixed"])
            self.assertEqual(len(status["source"]["roots"]), 2)
            self.assertNotIn("Codex hook", status["outdated"])
            self.assertNotIn("Claude 額度 hook", status["outdated"])


class TestDiscussPrompt(unittest.TestCase):
    def test_first_prompt_antigravity_has_run_command_warning(self):
        ws = cli.Workspace(Path(tempfile.gettempdir()))
        s = state_with(["claude", "antigravity", "codex"], [("claude", "開場討論", False)])

        # Antigravity 第一次提示含 run_command 警告
        prompt_agy = cli.build_prompt(ws, s, "antigravity")
        self.assertIn("run_command", prompt_agy)
        self.assertIn("讀檔與搜尋工具", prompt_agy)

        # Claude 與 Codex 不含該警告
        prompt_claude = cli.build_prompt(ws, s, "claude")
        self.assertNotIn("run_command", prompt_claude)

        prompt_codex = cli.build_prompt(ws, s, "codex")
        self.assertNotIn("run_command", prompt_codex)

    def test_followup_prompt_does_not_contain_warning(self):
        ws = cli.Workspace(Path(tempfile.gettempdir()))
        s = state_with(["claude", "antigravity"], [("claude", "舊訊息", False), ("claude", "新訊息", False)])
        s["parties"]["antigravity"].update(session_id="sess-agy", seen=1)
        prompt = cli.build_prompt(ws, s, "antigravity")
        self.assertNotIn("run_command", prompt)
        self.assertIn("新訊息", prompt)
