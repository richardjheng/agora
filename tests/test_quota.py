"""Antigravity 額度查詢與顯示回歸測試（全部 mock subprocess、檔案與 HTTP，不連網）"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import quota, sandbox  # noqa: E402


SAMPLE_STATUS = {
    "userStatus": {
        "cascadeModelConfigData": {
            "defaultOverrideModelConfig": {"modelOrAlias": {"model": "M418"}},
            "clientModelSorts": [{
                "groups": [{
                    "modelLabels": ["Gemini 4 Neon (Medium)"]
                }]
            }],
            "clientModelConfigs": [
                {
                    "label": "Gemini 4 Neon (Medium)",
                    "modelId": "gemini-4-neon-medium",
                    "modelOrAlias": {"model": "M418"},
                    "quotaInfo": {"remainingFraction": 0.892, "resetTime": "2099-01-01T00:00:00Z"},
                },
            ],
        }
    }
}


def setUpModule():
    patcher = patch.object(sandbox, "network_restricted", return_value=False)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class TestListProcessesEncodingAndDiagnostics(unittest.TestCase):
    def test_powershell_uses_utf8_and_parses_chinese_cmdline(self):
        seen = {}
        payload = [
            {"ProcessId": 1001, "CommandLine": 'agy.exe --hub --hub-port=51234 --add-dir="D:\\github\\agora"'},
            {"ProcessId": 1002, "CommandLine": 'agy.exe -p "中文提示：修復額度快取與編碼問題"'},
        ]

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(cmd, 0, stdout="\ufeff" + json.dumps(payload, ensure_ascii=False), stderr="")

        with patch.object(quota.subprocess, "run", side_effect=fake_run):
            procs = quota._list_processes()

        self.assertIn("[Console]::OutputEncoding=[Text.Encoding]::UTF8;", seen["cmd"][-1])
        self.assertEqual(seen["kwargs"].get("encoding"), "utf-8")
        self.assertEqual(len(procs), 2)
        self.assertIn("中文提示", procs[1]["CommandLine"])
        self.assertIsNone(quota._LAST_AGY_ERROR)

    def test_json_parse_failure_records_diagnostic_and_persists_to_cache(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout='[{"ProcessId": 1, "CommandLine": "bad \\x"}]', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "antigravity_quota.json"
            lock_path = Path(tmp) / "refresh.lock"
            old_ts = int(time.time()) - 20000
            cache_path.write_text(json.dumps({"schema": 2, "updated_at": old_ts, "pools": {}}), encoding="utf-8")

            with patch.object(quota.subprocess, "run", side_effect=fake_run), \
                    patch.object(quota, "GEMINI_DIR", Path(tmp) / ".gemini"), \
                    patch.object(quota, "AGY_CACHE", cache_path), \
                    patch.object(quota, "REFRESH_LOCK", lock_path), \
                    patch.object(quota, "update_codex_snapshot_from_session"), \
                    patch.object(quota, "probe_claude"), \
                    patch.dict(os.environ, {}, clear=False):
                os.environ.pop("AGORA_INVOKED", None)
                out = io.StringIO()
                with patch("sys.stdout", out), \
                        patch.object(quota, "claude_usage", return_value=None), \
                        patch.object(quota, "codex_usage", return_value=None):
                    quota.main(["--refresh"])

            self.assertIsNotNone(quota._LAST_AGY_ERROR)
            self.assertIn("JSON", quota._LAST_AGY_ERROR)
            self.assertIn("更新失敗", out.getvalue())
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["updated_at"], old_ts)
            self.assertIn("JSON", saved["last_error"])
            self.assertIsInstance(saved["last_error_at"], int)

    def test_quiet_and_json_modes_do_not_leak_diagnostic_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(quota, "refresh"), \
                    patch.object(quota, "_LAST_AGY_ERROR", "解析 Antigravity 程序清單 JSON 失敗"), \
                    patch.object(quota, "all_usage", return_value={"claude": None, "antigravity": None, "codex": None}):
                out_quiet = io.StringIO()
                with patch("sys.stdout", out_quiet):
                    quota.main(["--refresh", "--quiet"])
                self.assertEqual(out_quiet.getvalue(), "")

                out_json = io.StringIO()
                with patch("sys.stdout", out_json):
                    quota.main(["--refresh", "--json"])
                parsed = json.loads(out_json.getvalue())
                self.assertIn("antigravity", parsed)


class TestDiscoveryAndCandidates(unittest.TestCase):
    def _write_json(self, path: Path, data: dict, mtime: float):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        os.utime(path, (mtime, mtime))

    def test_hub_json_live_pid_and_workspace_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            gemini = Path(tmp) / ".gemini"
            ws_dir = str(Path(tmp) / "my_workspace")
            other_dir = str(Path(tmp) / "other_workspace")
            now = time.time()

            # 較舊 mtime 但對應執行中目前工作區 hub
            self._write_json(
                gemini / "antigravity" / "daemon" / "hub_5001.json",
                {"pid": 101, "httpPort": 5001, "httpsPort": 5002, "csrfToken": "tok-ws"},
                now - 5000,
            )
            # 較新 mtime、對應另一個執行中 hub
            self._write_json(
                gemini / "antigravity-ide" / "daemon" / "hub_6001.json",
                {"pid": 202, "httpPort": 6001, "csrfToken": "tok-other"},
                now - 100,
            )
            # 最新 mtime 但已死亡的舊 hub
            self._write_json(
                gemini / "antigravity-backup" / "daemon" / "hub_7001.json",
                {"pid": 999, "httpPort": 7001, "csrfToken": "tok-dead"},
                now - 10,
            )

            procs = [
                {"ProcessId": 202, "CommandLine": f'agy.exe --hub --hub-port=6001 --add-dir="{other_dir}"'},
                {"ProcessId": 101, "CommandLine": f'agy.exe --hub --hub-port=5001 --add-dir="{ws_dir}"'},
            ]
            with patch.object(quota, "GEMINI_DIR", gemini), \
                    patch.object(quota, "_list_processes", return_value=procs), \
                    patch.dict(os.environ, {"AGORA_WORKSPACE": ws_dir}):
                cands = quota._candidates()

            ports_and_tokens = [(c["protocol"], c["port"], c["csrf"]) for c in cands]
            self.assertEqual(ports_and_tokens[0], ("http", 5001, "tok-ws"))
            self.assertEqual(ports_and_tokens[1], ("https", 5002, "tok-ws"))
            self.assertEqual(ports_and_tokens[2], ("http", 6001, "tok-other"))
            self.assertEqual(ports_and_tokens[3], ("http", 7001, "tok-dead"))

    def test_candidates_are_capped_and_newest_files_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            gemini = Path(tmp) / ".gemini"
            base = time.time() - 1000
            for i in range(30):
                self._write_json(
                    gemini / "antigravity" / "daemon" / f"hub_{40000 + i}.json",
                    {"pid": 9000 + i, "httpPort": 40000 + i, "csrfToken": f"tok-{i}"},
                    base + i * 10,
                )

            with patch.object(quota, "GEMINI_DIR", gemini), \
                    patch.object(quota, "_list_processes", return_value=[]):
                cands = quota._candidates()

            self.assertLessEqual(len(cands), quota.MAX_CANDIDATES)
            self.assertLessEqual(len(cands), quota.MAX_DISCOVERY_FILES)
            # 最新的 hub_40029 排第一，舊的不會出現
            self.assertEqual(cands[0]["port"], 40029)
            self.assertNotIn(40000, [c["port"] for c in cands])

    def test_hub_port_without_discovery_file_is_still_tried(self):
        with tempfile.TemporaryDirectory() as tmp:
            gemini = Path(tmp) / ".gemini"
            procs = [{"ProcessId": 303, "CommandLine": "agy.exe --hub --hub-port=55555"}]
            with patch.object(quota, "GEMINI_DIR", gemini), \
                    patch.object(quota, "_list_processes", return_value=procs):
                cands = quota._candidates()

            self.assertEqual(len(cands), 1)
            self.assertEqual((cands[0]["protocol"], cands[0]["port"], cands[0]["csrf"]), ("http", 55555, None))

    def test_legacy_ls_json_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            gemini = Path(tmp) / ".gemini"
            self._write_json(
                gemini / "antigravity" / "daemon" / "ls_12345.json",
                {"httpPort": 12345, "httpsPort": 12346, "csrfToken": "legacy-tok"},
                time.time(),
            )
            with patch.object(quota, "GEMINI_DIR", gemini), \
                    patch.object(quota, "_list_processes", return_value=[]):
                cands = quota._candidates()

            self.assertEqual([(c["protocol"], c["port"], c["csrf"]) for c in cands], [
                ("http", 12345, "legacy-tok"),
                ("https", 12346, "legacy-tok"),
            ])

    def test_empty_homepage_csrf_uses_discovery_file_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            gemini = Path(tmp) / ".gemini"
            now = time.time()
            # 模擬 agy 1.5.x：一個沒有探索檔的 hub（首頁 csrfToken 為空），加上一個有 hub_*.json token 的 hub
            self._write_json(
                gemini / "antigravity" / "daemon" / "hub_51002.json",
                {"pid": 402, "httpPort": 51002, "csrfToken": "hub-file-csrf"},
                now,
            )
            procs = [
                {"ProcessId": 401, "CommandLine": "agy.exe --hub --hub-port=51001"},
                {"ProcessId": 402, "CommandLine": "agy.exe --hub --hub-port=51002"},
            ]
            seen_calls = []

            def fake_request(c, path, body=None, headers=None, timeout=4):
                seen_calls.append((c["port"], path, headers))
                if path == "/":
                    return '<script>window.__APP_CONFIG__={"csrfToken":""};</script>'
                if c["port"] == 51002 and (headers or {}).get("x-codeium-csrf-token") == "hub-file-csrf":
                    return json.dumps(SAMPLE_STATUS)
                raise RuntimeError("401 missing CSRF token")

            with patch.object(quota, "GEMINI_DIR", gemini), \
                    patch.object(quota, "_list_processes", return_value=procs), \
                    patch.object(quota, "_request", side_effect=fake_request):
                result = quota.query_antigravity()

            self.assertIsNotNone(result)
            self.assertEqual(result["pools"]["gemini-4-neon"]["remaining_pct"], 89.2)
            # 51002 有探索檔 token，優先於沒有探索檔的 51001，直接呼叫 GetUserStatus
            self.assertEqual(seen_calls[0][0], 51002)
            self.assertIn("GetUserStatus", seen_calls[0][1])

    def test_empty_homepage_csrf_skips_to_discovery_fallback(self):
        """即使第一個候選沒有 token 且首頁 csrfToken 為空，仍會繼續嘗試探索檔的 token"""
        cands = [
            {"protocol": "http", "port": 51001, "csrf": None},
            {"protocol": "http", "port": 51002, "csrf": "fallback-tok"},
        ]
        seen = []

        def fake_request(c, path, body=None, headers=None, timeout=4):
            seen.append((c["port"], path))
            if path == "/":
                return '<script>window.__APP_CONFIG__={"csrfToken":""};</script>'
            if c["port"] == 51002 and (headers or {}).get("x-codeium-csrf-token") == "fallback-tok":
                return json.dumps(SAMPLE_STATUS)
            raise RuntimeError("401")

        with patch.object(quota, "_candidates", return_value=cands), \
                patch.object(quota, "_request", side_effect=fake_request):
            result = quota.query_antigravity()

        self.assertIsNotNone(result)
        self.assertEqual(seen, [(51001, "/"), (51002, f"{quota.LS_SERVICE}/GetUserStatus")])


class TestDescribeStale(unittest.TestCase):
    def test_describe_shows_unknown_when_older_than_stale_after(self):
        now = time.time()
        u = {
            "remaining_pct": 100.0,
            "resets_at": None,
            "window": "gemini-4-neon",
            "window_label": "Neon",
            "updated_at": now - (quota.STALE_AFTER + 3600),
        }
        text = quota.describe("Antigravity", u)
        self.assertTrue(text.startswith("Antigravity：額度未知（上次 100%，"))
        self.assertIn("小時前", text)
        self.assertNotIn("剩 100%", text)

    def test_describe_shows_unknown_when_resets_at_passed(self):
        now = time.time()
        u = {
            "remaining_pct": 42.0,
            "resets_at": now - 60,
            "window": "gemini-4-neon",
            "window_label": "Neon",
            "updated_at": now - 600,
        }
        text = quota.describe("Antigravity", u)
        self.assertEqual(text, "Antigravity：額度未知（上次 42%，10 分鐘前）")

    def test_describe_fresh_usage_unchanged(self):
        now = time.time()
        u = {
            "remaining_pct": 89.2,
            "resets_at": now + 3600,
            "window": "gemini-4-neon",
            "window_label": "Neon",
            "updated_at": now - 120,
        }
        text = quota.describe("Antigravity", u)
        self.assertTrue(text.startswith("Antigravity 剩 89%（Neon，"))
        self.assertNotIn("額度未知", text)


if __name__ == "__main__":
    unittest.main()
