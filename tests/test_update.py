"""eustace --update: resolving what to install, starting the installer window, and the persona offer afterwards."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from _harness import cli

MAIN = "1" * 40
TAG_OBJECT = "2" * 40
TAG_COMMIT = "3" * 40
LIGHT = "4" * 40

LS_REMOTE = "\n".join([
    f"{MAIN}\trefs/heads/main",
    f"{TAG_OBJECT}\trefs/tags/v1.5.0",
    f"{TAG_COMMIT}\trefs/tags/v1.5.0^{{}}",
    f"{LIGHT}\trefs/tags/1.6.0",
]) + "\n"


def _git(stdout=LS_REMOTE, returncode=0, stderr="", error=None):
    """Stands in for subprocess.run: answers ls-remote with only the refs that were asked for, as git does."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if error:
            raise error
        wanted = set(argv[3:])
        lines = [line for line in stdout.splitlines() if line.split("\t")[1] in wanted]
        return NS(returncode=returncode, stdout="\n".join(lines), stderr=stderr)

    run.calls = calls
    return run


def _read(path):
    """A file's exact text, or None when it does not exist."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


@contextlib.contextmanager
def _patched(run=None, installed="", git="C:\\Git\\cmd\\git.exe"):
    with tempfile.TemporaryDirectory() as home, contextlib.ExitStack() as stack:
        run = run or _git()
        popen = stack.enter_context(mock.patch.object(cli.subprocess, "Popen"))
        stack.enter_context(mock.patch.object(cli.subprocess, "run", run))
        stack.enter_context(mock.patch.object(cli, "_git_exe", lambda: git))
        stack.enter_context(mock.patch.object(cli, "_installed_commit", lambda: installed))
        stack.enter_context(mock.patch.object(cli, "_UPDATE_HOME", os.path.join(home, ".eustace")))
        stack.enter_context(mock.patch.object(cli, "SHIPPED_PROMPT_PATH", os.path.join(home, "shipped.md")))
        out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        yield NS(home=home, run=run, popen=popen, out=out)


class ResolveRefTests(unittest.TestCase):
    def test_a_branch_resolves_to_its_newest_commit(self):
        with _patched() as p:
            self.assertEqual(cli._resolve_update_ref("main"), (MAIN, "branch main"))
            argv, kwargs = p.run.calls[0]
            self.assertEqual(argv[:3], ["C:\\Git\\cmd\\git.exe", "ls-remote", cli.UPDATE_REPO_URL])
            self.assertEqual(kwargs["timeout"], 60)

    def test_an_annotated_tag_resolves_to_the_commit_not_the_tag_object(self):
        with _patched():
            self.assertEqual(cli._resolve_update_ref("v1.5.0"), (TAG_COMMIT, "tag v1.5.0"))

    def test_a_lightweight_tag_resolves(self):
        with _patched():
            self.assertEqual(cli._resolve_update_ref("1.6.0"), (LIGHT, "tag 1.6.0"))

    def test_a_full_commit_hash_is_taken_as_given(self):
        with _patched():
            self.assertEqual(cli._resolve_update_ref("a" * 40), ("a" * 40, "commit"))

    def test_an_unknown_name_or_a_short_hash_is_refused(self):
        with _patched():
            for ref in ("nope", "abc1234"):
                with self.subTest(ref=ref), self.assertRaisesRegex(RuntimeError, "no branch or tag named"):
                    cli._resolve_update_ref(ref)

    def test_names_that_are_not_refs_never_reach_git(self):
        with _patched() as p:
            for ref in ("", "-main", "--upload-pack=calc", "a..b", "main;calc", "a b", "main@{1}", "x" * 101,
                        "https://evil.example/x.git", "main\nother"):
                with self.subTest(ref=ref), self.assertRaisesRegex(RuntimeError, "not a branch, tag or commit"):
                    cli._resolve_update_ref(ref)
            self.assertEqual(p.run.calls, [])

    def test_git_problems_are_explained(self):
        cases = [
            (dict(git=None), "git was not found on PATH"),
            (dict(run=_git(returncode=128, stderr="warning: x\nfatal: unable to access")), "fatal: unable to access"),
            (dict(run=_git(error=subprocess.TimeoutExpired("git", 60))), "did not answer within 60 seconds"),
            (dict(run=_git(error=OSError("blocked by policy"))), "blocked by policy"),
        ]
        for kwargs, message in cases:
            with self.subTest(message=message), _patched(**kwargs):
                with self.assertRaisesRegex(RuntimeError, message):
                    cli._resolve_update_ref("main")

    def test_lines_that_are_not_ref_lines_are_ignored(self):
        with _patched(run=_git(stdout=f"not-a-hash\trefs/heads/main\n{MAIN}\trefs/heads/main\n")):
            self.assertEqual(cli._resolve_update_ref("main")[0], MAIN)


class GitLookupTests(unittest.TestCase):
    def test_git_is_only_taken_from_absolute_path_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            real, here = os.path.join(tmp, "Git", "cmd"), os.path.join(tmp, "project")
            os.makedirs(real)
            os.makedirs(here)
            for folder in (real, here):
                open(os.path.join(folder, "git.exe"), "w").close()
            cwd = os.getcwd()
            os.chdir(here)
            try:
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "bin", f'"{real}"'])}):
                    self.assertEqual(cli._git_exe(), os.path.join(real, "git.exe"))
                with mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", "bin"])}):
                    self.assertIsNone(cli._git_exe())  # the planted git.exe in the current folder is not used
            finally:
                os.chdir(cwd)


class SelfUpdateTests(unittest.TestCase):
    def test_the_same_commit_installs_nothing(self):
        with _patched(installed=MAIN) as p:
            self.assertEqual(cli._self_update([]), 0)
            p.popen.assert_not_called()
            self.assertIn("Already up to date.", p.out.getvalue())
            self.assertIn(f"commit {MAIN[:7]}", p.out.getvalue())

    def test_a_newer_commit_starts_the_installer_pinned_to_that_commit(self):
        with _patched(installed="9" * 40) as p:
            with open(cli.SHIPPED_PROMPT_PATH, "w", encoding="utf-8") as f:
                f.write("old persona\n")
            self.assertEqual(cli._self_update([]), 0)
            (argv,), kwargs = p.popen.call_args
            self.assertEqual(argv[:3], [sys.executable, "-c", cli._UPDATE_HELPER])
            self.assertEqual(argv[3:6], [str(os.getpid()), f"git+{cli.UPDATE_REPO_URL}@{MAIN}", cli.VERSION])
            self.assertEqual(argv[6], cli._persona_hash("old persona"))
            # not the project folder: pip starts git by bare name, and Windows looks in the current folder first
            self.assertEqual(kwargs["cwd"], cli._UPDATE_HOME)
            self.assertTrue(os.path.isdir(cli._UPDATE_HOME))
            self.assertIn("Updating in a new window", p.out.getvalue())

    def test_an_install_from_a_folder_is_always_updated_and_has_no_persona_hash(self):
        with _patched(installed="") as p:
            self.assertEqual(cli._self_update(["v1.5.0"]), 0)
            argv = p.popen.call_args[0][0]
            self.assertTrue(argv[4].endswith("@" + TAG_COMMIT))
            self.assertEqual(argv[6], "-")
            self.assertIn("installed from a folder", p.out.getvalue())
            self.assertIn("tag v1.5.0", p.out.getvalue())

    def test_a_commit_hash_is_reported_plainly(self):
        with _patched() as p:
            cli._self_update(["a" * 40])
            self.assertIn("Available: commit aaaaaaa\n", p.out.getvalue())

    def test_a_name_that_cannot_be_resolved_fails_without_installing(self):
        with _patched() as p:
            self.assertEqual(cli._self_update(["nope"]), 1)
            p.popen.assert_not_called()
            self.assertIn("Update failed:", p.out.getvalue())

    def test_an_installer_that_cannot_start_is_reported(self):
        with _patched() as p:
            p.popen.side_effect = OSError("blocked")
            self.assertEqual(cli._self_update([]), 1)
            self.assertIn("could not be started (blocked)", p.out.getvalue())


class HelperTests(unittest.TestCase):
    """Runs the installer window's own script with pip replaced by a recorder."""

    def _run(self, results):
        calls, results = [], list(results)

        def fake_call(argv):
            calls.append(argv)
            return results.pop(0) if results else 0

        argv = ["-c", "0", "git+https://example/x.git@" + MAIN, "1.6.0", "oldhash"]
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp, contextlib.ExitStack() as stack:
            os.chdir(tmp)
            try:
                stack.enter_context(mock.patch.object(sys, "argv", argv))
                stack.enter_context(mock.patch.object(subprocess, "call", fake_call))
                stack.enter_context(mock.patch("time.sleep", lambda s: None))
                stack.enter_context(mock.patch("builtins.input", lambda *a: ""))
                stack.enter_context(mock.patch.dict(os.environ))
                out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                with self.assertRaises(SystemExit) as done:
                    exec(compile(cli._UPDATE_HELPER, "<update helper>", "exec"), {"__name__": "__main__"})
                with open("update.log", encoding="utf-8") as f:
                    log = f.read()
            finally:
                os.chdir(cwd)
        return calls, done.exception.code, out.getvalue(), log

    def test_dependencies_first_then_a_forced_reinstall_of_eustace_alone_then_the_new_version_reports(self):
        calls, code, out, log = self._run([0, 0, 0])
        self.assertEqual(code, 0)
        spec = "git+https://example/x.git@" + MAIN
        self.assertEqual([c[:4] for c in calls[:2]], [[sys.executable, "-m", "pip", "install"]] * 2)
        self.assertNotIn("--force-reinstall", calls[0])
        self.assertEqual(calls[0][-1], spec)
        # pip leaves an unchanged version number alone, so without the force a new commit would not install
        for flag in ("--upgrade", "--force-reinstall", "--no-deps", spec):
            self.assertIn(flag, calls[1])
        self.assertEqual(calls[2], [sys.executable, "-m", "cli", "--post-update", "1.6.0", "oldhash"])
        self.assertIn("exit=0", log)

    def test_a_failed_dependency_step_leaves_the_install_alone(self):
        calls, code, out, log = self._run([1, 1, 1])
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 3)  # three tries of step 1, nothing else
        self.assertTrue(all("--force-reinstall" not in c for c in calls))
        self.assertIn("Eustace v1.6.0 is still in place", out)
        self.assertIn("exit=1", log)

    def test_a_step_that_fails_once_is_retried(self):
        calls, code, _, _ = self._run([1, 0, 0, 0])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[-1][3], "--post-update")


class PostUpdateTests(unittest.TestCase):
    OLD, NEW = "line one\nline two\n", "line one\nline two\nline three\n"

    def _run(self, mine, shipped, old_hash, answer=""):
        with _patched() as p:
            os.makedirs(cli._UPDATE_HOME)
            mine_path = os.path.join(cli._UPDATE_HOME, "system_prompt.md")
            for path, text in ((mine_path, mine), (cli.SHIPPED_PROMPT_PATH, shipped)):
                if text is not None:
                    with open(path, "w", encoding="utf-8", newline="") as f:
                        f.write(text)
            asked = []

            def fake_input(prompt=""):
                asked.append(prompt)
                if isinstance(answer, Exception):
                    raise answer
                return answer

            with mock.patch("builtins.input", fake_input):
                self.assertEqual(cli._post_update(["1.6.0", old_hash]), 0)
            return NS(mine=_read(mine_path), bak=_read(mine_path + ".bak"), asked=asked, out=p.out.getvalue())

    def test_reports_the_version_change(self):
        r = self._run(self.NEW, self.NEW, "-")
        self.assertIn(f"Eustace updated: v1.6.0 -> v{cli.VERSION}", r.out)
        self.assertEqual(r.asked, [])

    def test_an_unedited_copy_of_the_previous_persona_is_brought_up_to_date_without_asking(self):
        r = self._run(self.OLD, self.NEW, cli._persona_hash(self.OLD.strip()))
        self.assertEqual(r.mine, self.NEW)
        self.assertEqual(r.asked, [])
        self.assertIsNone(r.bak)
        self.assertIn("yours had no changes of your own", r.out)

    def test_an_edited_persona_is_shown_and_replaced_only_on_yes_with_a_backup(self):
        mine = "line one\nmy own line\n"
        r = self._run(mine, self.NEW, cli._persona_hash(self.OLD.strip()), answer="y")
        self.assertEqual(r.mine, self.NEW)
        self.assertEqual(r.bak, mine)
        self.assertIn("  -my own line", r.out)
        self.assertIn("  +line three", r.out)
        self.assertEqual(len(r.asked), 1)

    def test_no_or_no_answer_keeps_the_edited_persona(self):
        mine = "line one\nmy own line\n"
        for answer in ("", "n", "maybe", EOFError()):
            with self.subTest(answer=answer):
                r = self._run(mine, self.NEW, "-", answer=answer)
                self.assertEqual(r.mine, mine)
                self.assertIsNone(r.bak)
                self.assertIn("Kept yours", r.out)

    def test_a_first_update_has_no_previous_persona_to_compare_so_it_asks(self):
        r = self._run(self.OLD, self.NEW, "-", answer="yes")
        self.assertEqual(len(r.asked), 1)
        self.assertEqual(r.mine, self.NEW)
        self.assertEqual(r.bak, self.OLD)

    def test_nothing_is_offered_when_this_update_did_not_change_the_shipped_persona(self):
        r = self._run("entirely my own\n", self.NEW, cli._persona_hash(self.NEW.strip()))
        self.assertEqual(r.mine, "entirely my own\n")
        self.assertEqual(r.asked, [])

    def test_line_endings_and_a_bom_are_not_a_difference(self):
        r = self._run("\ufeff" + self.NEW.replace("\n", "\r\n"), self.NEW, "-")
        self.assertEqual(r.asked, [])
        self.assertTrue(r.mine.startswith("\ufeff"))  # untouched

    def test_no_persona_of_the_users_or_none_shipped_does_nothing(self):
        for mine, shipped in ((None, self.NEW), (self.OLD, None)):
            with self.subTest(mine=mine, shipped=shipped):
                r = self._run(mine, shipped, "-")
                self.assertEqual(r.mine, mine)
                self.assertEqual(r.asked, [])


class ShippedPersonaTests(unittest.TestCase):
    def test_the_shipped_persona_is_used_when_there_is_no_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            shipped = os.path.join(tmp, "shipped.md")
            with open(shipped, "w", encoding="utf-8") as f:
                f.write("shipped persona")
            missing = os.path.join(tmp, "missing.md")
            with mock.patch.object(cli, "GLOBAL_PROMPT_PATH", missing), \
                    mock.patch.object(cli, "BUNDLED_PROMPT_PATH", missing), \
                    mock.patch.object(cli, "SHIPPED_PROMPT_PATH", shipped):
                self.assertEqual(cli._load_persona(), "shipped persona")

    def test_pyproject_ships_the_persona_where_the_code_looks_for_it(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn('"share/eustace" = ["system_prompt.md", ', f.read())  # the star font follows it
        self.assertEqual(cli.SHIPPED_PROMPT_PATH, os.path.join(sys.prefix, "share", "eustace", "system_prompt.md"))

    def test_the_distribution_name_matches_pyproject(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
            self.assertIn(f'name = "{cli._DIST_NAME}"', f.read())


if __name__ == "__main__":
    unittest.main()
