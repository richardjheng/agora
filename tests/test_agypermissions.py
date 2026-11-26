"""Antigravity 指令權限完整比對，使用暫存家目錄。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli


class TestAgyPermissions(unittest.TestCase):
    def test_missing_commands_match_the_prompt_exactly(self):
        legacy = 'python "C:/Agora Tools/agora.py"'
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)):
            path = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
            self.assertEqual(cli.missing_agy_commands("agora"), cli.AGENT_COMMANDS)
            path.parent.mkdir(parents=True)
            for command, missing in (("agora", ()), ("agora", ("test",)), ("agora", ("check",)), (legacy, ())):
                with self.subTest(command=command, missing=missing):
                    allow = [f"command({command} {sub})" for sub in cli.AGENT_COMMANDS if sub not in missing]
                    path.write_text(json.dumps({"permissions": {"allow": allow}}), encoding="utf-8")
                    self.assertEqual(cli.missing_agy_commands(command), missing)
            self.assertEqual(cli.missing_agy_commands("agora"), cli.AGENT_COMMANDS)

    def test_invalid_settings_report_all_commands_missing(self):
        valid_rules = [f"command(agora {sub})" for sub in cli.AGENT_COMMANDS]
        malformed = ("{", "null", "{}", '{"permissions": []}', '{"permissions": {"allow": "command(agora quota)"}}',
                     json.dumps({"permissions": {"allow": [*valid_rules, None]}}))
        with tempfile.TemporaryDirectory() as home, patch.object(Path, "home", return_value=Path(home)):
            path = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
            path.parent.mkdir(parents=True)
            for content in malformed:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    self.assertEqual(cli.missing_agy_commands("agora"), cli.AGENT_COMMANDS)
