import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, config


class LegacyTierConfigTests(unittest.TestCase):
    def test_query_leaves_legacy_config_intact_and_successful_update_cleans_all_tiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy_parties = {
                "codex": {
                    "tiers": {
                        "light": {"model": "light-main", "fallback": {"model": "light-old"}},
                        "heavy": {"model": "heavy-main", "effort": "high", "fallback": {"model": "heavy-old"}},
                    },
                },
                "claude": {
                    "enabled": False,
                    "tiers": {"standard": {"model": "claude-main", "fallback": "legacy"}},
                },
            }
            for update, party, field, value in (
                ({"shares": {"codex": "3"}}, "codex", "share", 3),
                ({"models": {"antigravity": "discussion-model"}}, "antigravity", "model", "discussion-model"),
            ):
                with self.subTest(update=update):
                    config.save(root, {"parties": legacy_parties})
                    path = config.config_path(root)
                    before = path.read_bytes()
                    out = io.StringIO()
                    with patch("sys.stdout", out):
                        cli.main(["--workspace", tmp, "parties", "--json"])
                    result = json.loads(out.getvalue())
                    self.assertEqual(result["parties"]["codex"]["tiers"]["heavy"],
                                     {"model": "heavy-main", "effort": "high"})
                    self.assertEqual(path.read_bytes(), before)
                    with self.assertRaises(ValueError):
                        config.update_parties(root, disable=["codex", "antigravity"])
                    self.assertEqual(path.read_bytes(), before)

                    config.update_parties(root, **update)
                    saved = config.load(root)["parties"]
                    self.assertEqual(saved[party][field], value)
                    self.assertEqual(saved["codex"]["tiers"], {
                        "light": {"model": "light-main"},
                        "heavy": {"model": "heavy-main", "effort": "high"},
                    })
                    self.assertEqual(saved["claude"]["tiers"], {"standard": {"model": "claude-main"}})
