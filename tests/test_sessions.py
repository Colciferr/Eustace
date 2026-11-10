"""Autosaves are per run, pruned, written atomically, and /resume never picks the current run."""
import json
import os
import time
import unittest

from _harness import cli, sandbox

MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.saved_name = cli.AUTOSAVE_NAME

    def tearDown(self):
        cli.AUTOSAVE_NAME = self.saved_name

    def _autosave_as(self, name, content="hello"):
        cli.AUTOSAVE_NAME = name
        msgs = [MSGS[0], {"role": "user", "content": content}, MSGS[2]]
        path = cli.save_session(msgs, quiet=True)
        return path

    def test_new_run_does_not_overwrite_previous_run(self):
        with sandbox():
            first = self._autosave_as("auto_20260101_090000", "first run")
            second = self._autosave_as("auto_20260101_100000", "second run")
            self.assertNotEqual(first, second)
            with open(first, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["messages"][0]["content"], "first run")

    def test_resume_without_argument_skips_the_current_run(self):
        with sandbox():
            old = self._autosave_as("auto_20260101_090000", "first run")
            os.utime(old, (time.time() - 60, time.time() - 60))
            self._autosave_as("auto_20260101_100000", "second run")
            restored = cli.load_session("")
            self.assertEqual(restored[1]["content"], "first run")
            self.assertEqual(restored[0]["role"], "system")

    def test_resume_with_nothing_earlier(self):
        with sandbox():
            self.assertIsNone(cli.load_session(""))
            self._autosave_as("auto_20260101_100000")
            self.assertIsNone(cli.load_session(""))

    def test_resume_by_name_and_index(self):
        with sandbox():
            cli.save_session(MSGS, name="my work", quiet=True)
            self.assertEqual(cli.load_session("my work")[1]["content"], "hello")
            self.assertEqual(cli.load_session("1")[1]["content"], "hello")
            self.assertIsNone(cli.load_session("9"))
            self.assertIsNone(cli.load_session("nope"))

    def test_autosaves_are_pruned_but_named_sessions_are_not(self):
        with sandbox():
            cli.save_session(MSGS, name="keep_me", quiet=True)
            cli.save_session(MSGS, name="auto_but_named_by_user", quiet=True)
            for i in range(cli.MAX_AUTOSAVES + 5):
                self._autosave_as(f"auto_20260101_{i:06d}")
            files = sorted(os.listdir(cli.SESSIONS_DIR))
            autos = [f for f in files if f.startswith("auto_2026")]
            self.assertEqual(len(autos), cli.MAX_AUTOSAVES)
            self.assertEqual(autos[0], "auto_20260101_000005.json")
            self.assertIn("keep_me.json", files)
            self.assertIn("auto_but_named_by_user.json", files)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        with sandbox():
            path = self._autosave_as("auto_20260101_090000", "good")
            real_dump = cli.json.dump

            def exploding_dump(*a, **k):
                raise OSError("disk full")

            cli.json.dump = exploding_dump
            try:
                with self.assertRaises(OSError):
                    self._autosave_as("auto_20260101_090000", "bad")
            finally:
                cli.json.dump = real_dump
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["messages"][0]["content"], "good")
            self.assertEqual(os.listdir(cli.SESSIONS_DIR), ["auto_20260101_090000.json"])

    def test_empty_conversation_is_not_saved(self):
        with sandbox():
            self.assertEqual(cli.save_session([MSGS[0]], quiet=True), "")
            self.assertFalse(os.path.exists(cli.SESSIONS_DIR))


if __name__ == "__main__":
    unittest.main()
