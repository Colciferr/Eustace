"""eustace --uninstall: the question, what it removes, what it keeps, and the window that does the removing."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from _harness import cli


@contextlib.contextmanager
def _patched(answer="y", popen_error=None, font_error=None):
    """A fake machine: nothing is uninstalled, no window opens, and the settings folder is a temp folder."""
    with tempfile.TemporaryDirectory() as home, contextlib.ExitStack() as stack:
        settings = os.path.join(home, ".eustace")
        os.makedirs(settings)
        with open(os.path.join(settings, "audit.jsonl"), "w", encoding="utf-8") as f:
            f.write("{}\n")
        popen = stack.enter_context(mock.patch.object(cli.subprocess, "Popen", side_effect=popen_error))
        removed, asked = [], []

        def remove_font():
            removed.append(True)
            if font_error:
                raise font_error
            return 0

        def fake_input(prompt=""):
            asked.append(prompt)
            if isinstance(answer, Exception):
                raise answer
            return answer

        stack.enter_context(mock.patch.object(cli, "_remove_star_font", remove_font))
        stack.enter_context(mock.patch.object(cli, "_UPDATE_HOME", settings))
        stack.enter_context(mock.patch("builtins.input", fake_input))
        out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        yield NS(settings=settings, popen=popen, removed=removed, asked=asked, out=out)


class UninstallTests(unittest.TestCase):
    def test_anything_but_yes_changes_nothing(self):
        for answer in ("", "n", "no", "maybe", "yes please", EOFError()):
            with _patched(answer) as p:
                self.assertEqual(cli._uninstall(["--uninstall"]), 0)
                self.assertEqual(len(p.asked), 1)
                self.assertEqual((p.removed, p.popen.call_count), ([], 0), answer)
                self.assertIn("Nothing was changed", p.out.getvalue())

    def test_the_question_says_what_is_kept(self):
        with _patched("n") as p:
            cli._uninstall(["--uninstall"])
            for word in (p.settings, "API key", "saved sessions", "audit log", "kept"):
                self.assertIn(word, p.asked[0])

    def test_yes_removes_the_font_and_starts_the_uninstaller_in_its_own_window(self):
        for answer in ("y", "Y", " yes "):
            with _patched(answer) as p:
                self.assertEqual(cli._uninstall(["--uninstall"]), 0)
                self.assertEqual(p.removed, [True])
                (argv,), kwargs = p.popen.call_args
                command, hint = cli._uninstall_command()
                self.assertEqual(argv[:3], [command[0], "-c", cli._UNINSTALL_HELPER])
                self.assertEqual(argv[3:], [str(os.getpid()), json.dumps(command), cli._user_font_path(), hint])
                self.assertEqual(kwargs["cwd"], p.settings)
                self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                self.assertIn(p.settings, p.out.getvalue())

    def test_yes_flag_skips_the_question(self):
        with _patched(AssertionError("asked anyway")) as p:
            self.assertEqual(cli._uninstall(["--uninstall", "--yes"]), 0)
            self.assertEqual((p.asked, p.removed, p.popen.call_count), ([], [True], 1))

    def test_the_settings_folder_is_never_touched(self):
        with _patched() as p:
            cli._uninstall(["--uninstall"])
            self.assertEqual(os.listdir(p.settings), ["audit.jsonl"])

    def test_a_font_that_cannot_be_removed_does_not_stop_the_uninstall(self):
        with _patched(font_error=OSError("registry is read-only")) as p:
            self.assertEqual(cli._uninstall(["--uninstall"]), 0)
            self.assertEqual(p.popen.call_count, 1)
            self.assertIn("registry is read-only", p.out.getvalue())

    def test_a_window_that_cannot_be_opened_says_what_to_run_instead(self):
        with _patched(popen_error=OSError("no")) as p:
            self.assertEqual(cli._uninstall(["--uninstall"]), 1)
            self.assertIn(cli._uninstall_command()[1], p.out.getvalue())

    def test_a_setup_install_is_removed_with_pipx_under_the_python_it_was_made_from(self):
        with tempfile.TemporaryDirectory() as env:
            with mock.patch.object(cli.sys, "prefix", env):
                command, hint = cli._uninstall_command()
                self.assertEqual(command, [sys.executable, "-m", "pip", "uninstall", "-y", cli._DIST_NAME])
                open(os.path.join(env, "pipx_metadata.json"), "w").close()
                with mock.patch.object(cli.sys, "_base_executable", r"C:\Python314\python.exe", create=True):
                    command, hint = cli._uninstall_command()
                    # not the environment's own python.exe: pipx could not delete a folder that is running it
                    self.assertEqual(command, [r"C:\Python314\python.exe", "-m", "pipx", "uninstall", cli._DIST_NAME])
                    self.assertIn(f"pipx uninstall {cli._DIST_NAME}", hint)

    def test_the_flag_is_documented_and_handled_before_a_session_starts(self):
        cli_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cli.py")
        shown = subprocess.run([sys.executable, cli_py, "--help"], capture_output=True, text=True, timeout=120)
        self.assertIn("--uninstall", shown.stdout)
        with open(cli_py, encoding="utf-8") as f:
            source = f.read()
        self.assertLess(source.index('if "--uninstall" in _ARGS'), source.index("DEV_MODE = "))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNotNone(cli.shell_deny_match("eustace --uninstall --yes"))


class HelperWindowTests(unittest.TestCase):
    """The script that runs in the new window, run for real with a stand-in for the uninstall command."""

    def run_helper(self, exit_code):
        with tempfile.TemporaryDirectory() as tmp:
            font = os.path.join(tmp, "font.ttf")
            open(font, "wb").close()
            finished = subprocess.Popen([sys.executable, "-c", "pass"])
            finished.wait()
            command = [sys.executable, "-c", f"import sys; print('removing'); sys.exit({exit_code})"]
            done = subprocess.run(
                [sys.executable, "-c", cli._UNINSTALL_HELPER, str(finished.pid), json.dumps(command), font, "RUN THIS"],
                cwd=tmp, capture_output=True, text=True, timeout=120, env={**os.environ, "EUSTACE_UPDATE_NO_PAUSE": "1"})
            with open(os.path.join(tmp, "uninstall.log"), encoding="utf-8") as f:
                log = f.read()
            return NS(code=done.returncode, out=done.stdout, log=log, font_left=os.path.exists(font), tmp=tmp)

    def test_success_runs_the_command_deletes_the_font_file_and_logs(self):
        r = self.run_helper(0)
        self.assertEqual(r.code, 0)
        self.assertIn("removing", r.out)
        self.assertIn("Eustace is uninstalled", r.out)
        self.assertIn(os.path.basename(r.tmp), r.out)  # where the settings folder is
        self.assertIn("exit=0", r.log)
        self.assertFalse(r.font_left)

    def test_a_failed_command_is_reported_with_what_to_run(self):
        r = self.run_helper(3)
        self.assertEqual(r.code, 3)
        self.assertIn("RUN THIS", r.out)
        self.assertNotIn("Eustace is uninstalled", r.out)
        self.assertIn("exit=3", r.log)


if __name__ == "__main__":
    unittest.main()
