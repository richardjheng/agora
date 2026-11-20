"""統一工作區根目錄解析（CLI 與 hook 共用）與設定檔路徑輸出測試。"""

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, config, quota_hook, worktree


def _init_git(root: Path):
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def _clean_env(extra=None):
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_PROJECT_DIR", "AGORA_WORKSPACE", worktree.WORK_ENV,
                        worktree.CONTROL_ENV, worktree.DISPATCH_ENV, worktree.MANIFEST_ENV,
                        cli.NESTED_ENV)}
    if extra:
        env.update(extra)
    return env


class TestUnifiedRootResolution(unittest.TestCase):
    def test_nested_folder_resolves_same_root_for_cli_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve() / "proj"
            nested = proj / "a" / "b" / "c"
            nested.mkdir(parents=True)
            config.update_parties(proj, disable=["claude"])

            with patch.dict(os.environ, _clean_env(), clear=True), \
                    patch.object(Path, "cwd", return_value=nested), \
                    patch.object(os, "getcwd", return_value=str(nested)):
                cli_ws = cli.Workspace(cli.resolve_workspace(None))
                hook_root = quota_hook.project_root()

            self.assertEqual(cli_ws.root, proj)
            self.assertEqual(cli_ws.control_root, proj)
            self.assertEqual(hook_root, proj)

    def test_agora_above_git_root_resolves_same_root_for_cli_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp).resolve() / "outer"
            repo = outer / "repo"
            src = repo / "src"
            src.mkdir(parents=True)
            _init_git(repo)
            config.update_parties(outer, disable=["claude"])

            with patch.dict(os.environ, _clean_env(), clear=True), \
                    patch.object(Path, "cwd", return_value=src), \
                    patch.object(os, "getcwd", return_value=str(src)):
                cli_ws = cli.Workspace(cli.resolve_workspace(None))
                hook_root = quota_hook.project_root()

            self.assertEqual(cli_ws.root, outer)
            self.assertEqual(cli_ws.control_root, outer)
            self.assertEqual(hook_root, outer)

    def test_auto_search_keeps_dispatch_worktree_without_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp).resolve() / "proj"
            wt = proj / ".agora" / "worktrees" / "task1"
            nested = wt / "src" / "pkg"
            nested.mkdir(parents=True)
            _init_git(wt)

            with patch.dict(os.environ, _clean_env(), clear=True):
                for cwd in (wt, nested):
                    with self.subTest(cwd=cwd), patch.object(Path, "cwd", return_value=cwd):
                        self.assertEqual(cli.resolve_workspace(None), wt)

    def test_auto_search_excludes_global_home_agora_but_explicit_allows_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp).resolve() / "home"
            (fake_home / ".agora").mkdir(parents=True)
            uninit_git = fake_home / "projects" / "app"
            uninit_sub = uninit_git / "pkg"
            uninit_sub.mkdir(parents=True)
            _init_git(uninit_git)
            plain = fake_home / "notes"
            plain.mkdir(parents=True)

            with patch.object(Path, "home", return_value=fake_home):
                # 家目錄下未初始化的 git 專案：排除 ~/.agora，CLI 退回 git 根，hook 回 None
                with patch.dict(os.environ, _clean_env(), clear=True), \
                        patch.object(Path, "cwd", return_value=uninit_sub), \
                        patch.object(os, "getcwd", return_value=str(uninit_sub)):
                    self.assertIsNone(config.find_root(uninit_sub))
                    self.assertEqual(cli.resolve_workspace(None), uninit_git)
                    self.assertIsNone(quota_hook.project_root())

                # 家目錄下未初始化的非 git 資料夾：CLI 退回 cwd，hook 回 None
                with patch.dict(os.environ, _clean_env(), clear=True), \
                        patch.object(Path, "cwd", return_value=plain), \
                        patch.object(os, "getcwd", return_value=str(plain)):
                    self.assertEqual(cli.resolve_workspace(None), plain)
                    self.assertIsNone(quota_hook.project_root())

                # 直接在家目錄執行：自動搜尋仍排除 ~/.agora，hook 不誤判為專案
                with patch.dict(os.environ, _clean_env(), clear=True), \
                        patch.object(Path, "cwd", return_value=fake_home), \
                        patch.object(os, "getcwd", return_value=str(fake_home)):
                    self.assertIsNone(config.find_root(fake_home))
                    self.assertEqual(cli.resolve_workspace(None), fake_home)
                    self.assertIsNone(quota_hook.project_root())

                # 明確指定 --workspace 或 AGORA_WORKSPACE 為家目錄時不受排除限制
                with patch.dict(os.environ, _clean_env(), clear=True):
                    self.assertEqual(cli.resolve_workspace(str(fake_home)), fake_home)
                with patch.dict(os.environ, _clean_env({"AGORA_WORKSPACE": str(fake_home)}), clear=True):
                    self.assertEqual(cli.resolve_workspace(None), fake_home)
                    self.assertEqual(quota_hook.project_root(), fake_home)

    def test_explicit_precedence_workspace_arg_over_worktree_over_env_over_auto(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            arg_dir = base / "arg"
            wt_dir = base / "wt"
            env_dir = base / "env"
            auto_dir = base / "auto"
            for d in (arg_dir, wt_dir, env_dir, auto_dir):
                (d / ".agora").mkdir(parents=True)

            env = _clean_env({worktree.WORK_ENV: str(wt_dir), "AGORA_WORKSPACE": str(env_dir)})
            with patch.dict(os.environ, env, clear=True), patch.object(Path, "cwd", return_value=auto_dir):
                self.assertEqual(cli.resolve_workspace(str(arg_dir)), arg_dir)
                self.assertEqual(cli.resolve_workspace(None), wt_dir)

            env_no_wt = _clean_env({"AGORA_WORKSPACE": str(env_dir), "CLAUDE_PROJECT_DIR": str(auto_dir)})
            with patch.dict(os.environ, env_no_wt, clear=True), patch.object(Path, "cwd", return_value=auto_dir):
                self.assertEqual(cli.resolve_workspace(None), env_dir)
                self.assertEqual(quota_hook.project_root(), env_dir)

    def test_worktree_control_root_mapping_shared_by_cli_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            ctrl = base / "ctrl"
            wt = ctrl / ".agora" / "worktrees" / "task1"
            wt_sub = wt / "sub"
            wt_sub.mkdir(parents=True)
            config.update_parties(ctrl, disable=["claude"], models={"codex": "ctrl-model"}, shares={"codex": "0"})

            env = _clean_env({worktree.WORK_ENV: str(wt), worktree.CONTROL_ENV: str(ctrl)})
            with patch.dict(os.environ, env, clear=True), \
                    patch.object(Path, "cwd", return_value=wt_sub), \
                    patch.object(os, "getcwd", return_value=str(wt_sub)):
                ws = cli.Workspace(cli.resolve_workspace(None))
                hook_root = quota_hook.project_root()

                self.assertEqual(ws.root, wt)
                self.assertEqual(ws.control_root, ctrl)
                self.assertEqual(hook_root, ctrl)
                self.assertEqual(ws.config_path(), config.config_path(ctrl))
                self.assertEqual(ws.enabled(), ["antigravity", "codex"])

                # call_opts、build_prompt、cmd_parties、cmd_route 都讀 control_root
                state = {"topic": "t", "parties": {"codex": {"session_id": None, "seen": 0, "model": None}}, "messages": []}
                self.assertEqual(cli.call_opts(ws, state, "codex")["model"], "ctrl-model")
                self.assertIn("分配比例是 0", cli.build_prompt(ws, state, "codex"))

                out = io.StringIO()
                with patch("sys.stdout", out):
                    cli.cmd_parties(ws, argparse.Namespace(
                        enable=None, disable=None, model=None, effort=None, share=None,
                        subagents=None, sandbox=None, json=True,
                    ))
                parties_data = json.loads(out.getvalue())
                self.assertFalse(parties_data["parties"]["claude"]["enabled"])
                self.assertEqual(parties_data["parties"]["codex"]["model"], "ctrl-model")

                route_out = io.StringIO()
                with patch("sys.stdout", route_out):
                    cli.cmd_route(ws, argparse.Namespace(me="codex", json=True))
                route_data = json.loads(route_out.getvalue())
                self.assertEqual(route_data["enabled"], ["antigravity", "codex"])
                self.assertEqual(route_data["config"], str(config.config_path(ctrl)))
                self.assertIn("分配比例是 0", route_data["instruction"])

    def test_disabled_errors_and_route_include_actual_config_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config.update_parties(root, disable=["antigravity"])
            ws = cli.Workspace(root)
            cfg_str = str(config.config_path(root))

            with self.assertRaises(SystemExit) as cm:
                cli.require_enabled(ws, "antigravity")
            self.assertIn("未在此專案啟用", str(cm.exception.code))
            self.assertIn(cfg_str, str(cm.exception.code))

            state = {"topic": "t", "parties": {"claude": {"session_id": None, "seen": 0, "model": None},
                                               "codex": {"session_id": None, "seen": 0, "model": None}},
                     "messages": []}
            with self.assertRaises(SystemExit) as cm:
                cli.targets_for(state, "human", "antigravity", ws.enabled(), ws.config_path())
            self.assertIn("未在此專案啟用", str(cm.exception.code))
            self.assertIn(cfg_str, str(cm.exception.code))

            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, state)
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_send(ws, argparse.Namespace(thread="t1", sender="human", to="antigravity",
                                                    message="hi", file=None, blind=False))
            self.assertIn("未在此專案啟用", str(cm.exception.code))
            self.assertIn(cfg_str, str(cm.exception.code))

            out_text = io.StringIO()
            with patch("sys.stdout", out_text):
                cli.cmd_route(ws, argparse.Namespace(me="antigravity", json=False))
            self.assertIn(cfg_str, out_text.getvalue())

            out_json = io.StringIO()
            with patch("sys.stdout", out_json):
                cli.cmd_route(ws, argparse.Namespace(me="antigravity", json=True))
            data = json.loads(out_json.getvalue())
            self.assertEqual(data["config"], cfg_str)


if __name__ == "__main__":
    unittest.main()
