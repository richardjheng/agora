import io
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, config


def _update_in_process(root, shares, started, resume=None):
    if resume is None:
        started.set()
        config.update_parties(root, shares=shares)
        return

    load = config.load

    def pause_after_read(*args, **kwargs):
        data = load(*args, **kwargs)
        started.set()
        if not resume.wait(15):
            raise TimeoutError("Test writer was not released")
        return data

    with patch.object(config, "load", pause_after_read):
        config.update_parties(root, shares=shares)


class ConfigWriteTests(unittest.TestCase):
    def test_parallel_updates_hold_lock_across_read_and_write(self):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.save(root, {"unrelated": True})
            first_read, second_started, resume = [ctx.Event() for _ in range(3)]
            first = ctx.Process(target=_update_in_process,
                                args=(root, {"codex": "3"}, first_read, resume))
            second = ctx.Process(target=_update_in_process,
                                 args=(root, {"claude": "4"}, second_started))
            first.start()
            try:
                self.assertTrue(first_read.wait(10))
                with self.assertRaisesRegex(ValueError, "config.json"):
                    with config._config_lock(config.config_path(root), timeout=0):
                        self.fail("The first writer must still hold the lock")
                second.start()
                self.assertTrue(second_started.wait(10))
                resume.set()
                for proc in (first, second):
                    proc.join(10)
                    self.assertEqual(proc.exitcode, 0)
                self.assertEqual(config.load(root), {
                    "unrelated": True,
                    "parties": {"codex": {"share": 3}, "claude": {"share": 4}},
                })
            finally:
                resume.set()
                for proc in (first, second):
                    if proc.pid is not None:
                        if proc.is_alive():
                            proc.terminate()
                        proc.join(5)
                        proc.close()

    def test_interrupted_write_keeps_original_file_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.save(root, {"original": True})
            path = config.config_path(root)
            original = path.read_bytes()
            with patch.object(config.os, "fsync", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    config.save(root, {"new": True})
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(list(path.parent.glob(".config.json.*.tmp")))
            config.save(root, {"new": True})
            self.assertEqual(config.load(root), {"new": True})

    def test_replace_retries_windows_sharing_violation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config.save(root, {"original": True})
            replace = config.os.replace
            error = PermissionError("Sharing violation")
            error.winerror = 32

            def replace_after_violation(source, target):
                if replace_mock.call_count == 1:
                    raise error
                replace(source, target)

            with patch.object(config.os, "replace", side_effect=replace_after_violation) as replace_mock:
                with patch.object(config.time, "sleep"):
                    config.save(root, {"new": True})
                self.assertEqual(replace_mock.call_count, 2)
            self.assertEqual(config.load(root), {"new": True})


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
