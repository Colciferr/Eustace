"""Unit tests for the security guards in cli.py.

Run from the repo root with an interpreter that has the project dependencies, e.g. the pipx venv:
    <venv>\\Scripts\\python.exe -I tests\\test_guards.py -v
cli.py builds its API client lazily in main(), so importing it needs no key or network.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.argv = ["cli"]  # never let unittest flags (-v, --devmode) reach cli's flag parser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cli  # noqa: E402

HOME = os.path.expanduser("~")


def _make_junction(link: str, target: str) -> bool:
    """Creates a directory junction with mklink /J (no admin required). Returns False if unavailable."""
    try:
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True, text=True)
        return r.returncode == 0 and cli._is_link_or_junction(link)
    except OSError:
        return False


class _CaptureApproval:
    """Stand-in for APPROVAL_HANDLER that records the approval request and denies it."""

    def __init__(self):
        self.calls = []
        self.approve_all_for_turn = False
        self.last_decision = "auto"

    def request_approval(self, action_title, detail_text="", diff_text=""):
        self.calls.append((action_title, detail_text, diff_text))
        self.last_decision = "denied_user"
        return False

    def reset_turn(self):
        pass


class CanonicalPathTests(unittest.TestCase):
    def test_rejects_device_unc_and_stream_spellings(self):
        for bad in (
            r"\\?\C:\Windows\System32",
            r"\\.\pipe\eustace",
            r"\\srv\share\file.txt",
            r"\\localhost\C$\Users",
            "//srv/share/file.txt",
            r"C:\a\b.txt:stream",
            r"C:\Users\x\.eustace\.env::$DATA",
            "nul",
            "",
        ):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    cli._canonical_path(bad)
                canon, err = cli._resolve_model_path(bad)
                self.assertIsNone(canon)
                self.assertTrue(err.startswith("Security Error"))
                self.assertTrue(cli._is_sensitive(bad), "un-canonicalizable spelling must fail closed")

    def test_plain_local_paths_pass(self):
        here = os.path.abspath(__file__)
        self.assertEqual(os.path.normcase(cli._canonical_path(here)), os.path.normcase(os.path.realpath(here)))
        canon, err = cli._resolve_model_path("README.md")
        self.assertIsNone(err)
        self.assertTrue(os.path.isabs(canon))

    def test_short_name_resolves_to_long_name(self):
        short = r"C:\PROGRA~1"
        if not os.path.exists(short):
            self.skipTest("8.3 names not available on this volume")
        canon = cli._canonical_path(short)
        self.assertNotIn("~", canon)
        self.assertEqual(os.path.normcase(canon), os.path.normcase(os.path.realpath(os.environ["ProgramFiles"])))
        self.assertTrue(cli._is_protected_path(canon))


class JunctionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))
        self.plain = os.path.join(self.tmp, "plain")
        os.mkdir(self.plain)
        # Point the junction AT an existing protected home; nothing is ever written through it.
        self.target = next((h for h in cli.PROTECTED_HOMES if os.path.isdir(h)), None)
        self.link = os.path.join(self.tmp, "jump")
        self.have_link = self.target is not None and _make_junction(self.link, self.target)

    def tearDown(self):
        if os.path.lexists(self.link):
            os.rmdir(self.link)  # removes the junction only, never its target
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_dir_is_not_a_link(self):
        self.assertFalse(cli._is_link_or_junction(self.plain))
        self.assertFalse(cli._has_link_component(os.path.join(self.plain, "new.txt")))
        self.assertIsNone(cli._recheck_after_approval(os.path.join(self.plain, "new.txt")))

    def test_junction_detected_resolved_and_blocked(self):
        if not self.have_link:
            self.skipTest("could not create a junction (no existing Eustace home or mklink unavailable)")
        self.assertTrue(cli._is_link_or_junction(self.link))
        via_link = os.path.join(self.link, "sessions", "autosave.json")
        canon = cli._canonical_path(via_link)
        self.assertTrue(cli._is_under(canon, self.target), f"{canon} should resolve under {self.target}")
        self.assertTrue(cli._is_sensitive(via_link))
        self.assertTrue(cli._has_link_component(via_link))
        self.assertIn("Security Error", cli._recheck_after_approval(via_link) or "")
        listing = cli.list_directory(self.tmp)
        self.assertIn("[LINK] jump", listing)
        self.assertNotIn("autosave", listing)


class SensitivePatternTests(unittest.TestCase):
    def test_credential_stores_and_dirs(self):
        for p in (
            os.path.join(HOME, ".aws", "credentials"),
            os.path.join(HOME, ".kube", "config"),
            os.path.join(HOME, ".docker", "config.json"),
            os.path.join(HOME, "AppData", "Roaming", "Microsoft", "Windows", "PowerShell", "PSReadLine", "ConsoleHost_history.txt"),
            os.path.join(HOME, ".eustace", "audit.jsonl"),
            os.path.join(HOME, ".eustace-dev", ".env"),
            os.path.join(HOME, "proj", "terraform.tfstate"),
            os.path.join(HOME, "proj", "web.config"),
        ):
            with self.subTest(path=p):
                self.assertTrue(cli._is_sensitive(p))

    def test_ordinary_files_not_sensitive(self):
        self.assertFalse(cli._is_sensitive(os.path.join(os.getcwd(), "README.md")))


class ControlCharacterTests(unittest.TestCase):
    def test_visible_controls(self):
        self.assertEqual(cli._visible_controls("a\rb"), "a\\x0db")
        self.assertEqual(cli._visible_controls("\x1b]52;c;QUJD\x07"), "\\x1b]52;c;QUJD\\x07")
        self.assertEqual(cli._visible_controls("x\x9by"), "x\\x9by")
        self.assertEqual(cli._visible_controls("keep\nnew\tline"), "keep\nnew\tline")
        self.assertEqual(cli._sanitize_for_display("a\r\nb"), "a\nb")

    def test_run_powershell_rejects_hidden_controls_before_approval(self):
        saved = cli.APPROVAL_HANDLER
        cli.APPROVAL_HANDLER = cap = _CaptureApproval()
        try:
            for cmd in ("Write-Host ok # comment\rRemove-Item hidden", "Write-Host \x1b[2Jok", "a\x0bb"):
                with self.subTest(cmd=cmd):
                    self.assertIn("hidden control characters", cli.run_powershell(cmd))
            self.assertEqual(cap.calls, [], "rejection must happen before any approval prompt")
        finally:
            cli.APPROVAL_HANDLER = saved

    def test_approval_panel_shows_interpreter_and_visible_command(self):
        saved = cli.APPROVAL_HANDLER
        cli.APPROVAL_HANDLER = cap = _CaptureApproval()
        try:
            out = cli.run_powershell("Write-Host line1\r\nWrite-Host line2")
            self.assertIn("Operation aborted", out)
            self.assertEqual(len(cap.calls), 1)
            _, detail, _ = cap.calls[0]
            self.assertIn("Interpreter:", detail)
            self.assertIn("line1\nWrite-Host line2", detail)
            self.assertNotIn("\r", detail)
        finally:
            cli.APPROVAL_HANDLER = saved


class PowerShellHostTests(unittest.TestCase):
    def test_trusted_host_is_absolute_and_outside_cwd(self):
        shell = cli._trusted_powershell()
        self.assertIsNotNone(shell)
        self.assertTrue(os.path.isabs(shell))
        self.assertTrue(os.path.isfile(shell))
        self.assertFalse(cli._is_under(os.path.realpath(shell), cli._workspace_root()))
        self.assertTrue(shell.lower().endswith(("pwsh.exe", "powershell.exe")))


class OpenFileTests(unittest.TestCase):
    def test_non_document_extension_blocked_before_approval(self):
        saved = cli.APPROVAL_HANDLER
        cli.APPROVAL_HANDLER = cap = _CaptureApproval()
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))
        try:
            for name in ("evil.hta", "evil.pif", "evil.msc", "evil.reg", "evil.url", "app.application", "addin.xll", "noext", "evil.hta. "):
                p = os.path.join(tmp, name.rstrip(" ."))
                with open(p, "w") as f:
                    f.write("x")
                with self.subTest(name=name):
                    self.assertIn("Security Error", cli.open_file(os.path.join(tmp, name)))
            self.assertIn("Security Error", cli.open_file(os.path.join(tmp, "noext") + ":Zone.Identifier"))
            self.assertEqual(cap.calls, [])
            # A trailing-dot/space spelling of an allowed type resolves to the real .txt file and reaches approval
            with open(os.path.join(tmp, "ok.txt"), "w") as f:
                f.write("x")
            self.assertIn("Operation aborted", cli.open_file(os.path.join(tmp, "ok.txt. ")))
            self.assertEqual(len(cap.calls), 1)
            self.assertTrue(cap.calls[0][0].endswith("ok.txt"))
        finally:
            cli.APPROVAL_HANDLER = saved
            shutil.rmtree(tmp, ignore_errors=True)


class AgentMdDiscoveryTests(unittest.TestCase):
    def test_stops_at_git_root(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))
        try:
            root = os.path.join(tmp, "repo")
            sub = os.path.join(root, "pkg", "sub")
            os.makedirs(sub)
            os.mkdir(os.path.join(root, ".git"))
            for d in (tmp, root, sub):
                with open(os.path.join(d, "AGENT.md"), "w") as f:
                    f.write("# instructions\n")
            found = [os.path.normcase(p) for p in cli._agent_md_candidates(sub)]
            self.assertEqual(found, [os.path.normcase(os.path.join(root, "AGENT.md")), os.path.normcase(os.path.join(sub, "AGENT.md"))])
            # Without a git root only cwd itself is consulted
            lone = os.path.join(tmp, "lone")
            os.mkdir(lone)
            self.assertEqual(cli._agent_md_candidates(lone), [])
            with open(os.path.join(lone, "AGENT.md"), "w") as f:
                f.write("x")
            self.assertEqual([os.path.normcase(p) for p in cli._agent_md_candidates(lone)], [os.path.normcase(os.path.join(lone, "AGENT.md"))])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PreviewFaithfulnessTests(unittest.TestCase):
    def setUp(self):
        self.saved = cli.APPROVAL_HANDLER
        cli.APPROVAL_HANDLER = self.cap = _CaptureApproval()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))

    def tearDown(self):
        cli.APPROVAL_HANDLER = self.saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_excel_discloses_truncated_cells(self):
        updates = {f"A{i}": i for i in range(1, 31)}
        out = cli.update_excel(os.path.join(self.tmp, "book.xlsx"), cell_updates=updates)
        self.assertIn("Operation aborted", out)
        _, detail, _ = self.cap.calls[0]
        self.assertIn("Cell updates (30):", detail)
        self.assertIn("(+5 more cells)", detail)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "book.xlsx")))

    def test_write_docx_previews_title(self):
        out = cli.write_docx(os.path.join(self.tmp, "memo.docx"), "body text", title="Secret Title")
        self.assertIn("Operation aborted", out)
        _, _, diff = self.cap.calls[0]
        self.assertIn("# Secret Title", diff)

    def test_read_outside_workspace_requires_approval(self):
        p = os.path.join(self.tmp, "note.txt")
        with open(p, "w") as f:
            f.write("hello")
        self.assertFalse(cli._is_under(os.path.realpath(p), cli._workspace_root()), "temp dir must be outside cwd for this test")
        out = cli.read_file(p)
        self.assertIn("Operation aborted", out)
        self.assertEqual(len(self.cap.calls), 1)
        self.assertIn("outside the working directory", self.cap.calls[0][0])
        # Inside the workspace stays approval-free
        self.cap.calls.clear()
        self.assertIn("Eustace", cli.read_file("README.md"))
        self.assertEqual(self.cap.calls, [])


class StreamCancellationTests(unittest.TestCase):
    """A stalled network call must not block ESC / Ctrl+C: the worker thread owns the socket, the main thread polls."""

    class _StalledClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kwargs):
                    import time as _t
                    _t.sleep(30)  # simulates a server that never answers
                    return iter(())

    def test_esc_interrupts_stalled_request_quickly(self):
        import time as _t
        saved_client, saved_abort = cli.client, cli.check_user_abort
        deadline = _t.monotonic() + 0.3

        def fake_abort():
            if _t.monotonic() > deadline:
                raise KeyboardInterrupt("Aborted via ESC key")

        cli.client, cli.check_user_abort = self._StalledClient(), fake_abort
        try:
            t0 = _t.monotonic()
            worker = cli._StreamWorker(model="x", messages=[], stream=True)
            with self.assertRaises(KeyboardInterrupt):
                for _ in worker:
                    pass
            worker.cancel()
            self.assertLess(_t.monotonic() - t0, 2.0, "cancellation must not wait for the stalled request")
        finally:
            cli.client, cli.check_user_abort = saved_client, saved_abort

    def test_chunks_and_errors_are_relayed(self):
        class _Client:
            class chat:
                class completions:
                    @staticmethod
                    def create(**kwargs):
                        if kwargs.get("fail"):
                            raise RuntimeError("boom")
                        return iter(["c1", "c2"])

        saved_client = cli.client
        cli.client = _Client()
        try:
            self.assertEqual(list(cli._StreamWorker(model="x", messages=[])), ["c1", "c2"])
            with self.assertRaises(RuntimeError):
                list(cli._StreamWorker(model="x", messages=[], fail=True))
        finally:
            cli.client = saved_client


class ToolCallExtraTests(unittest.TestCase):
    """Gemini 3 attaches extra_content.google.thought_signature to streamed tool calls and 400s the
    follow-up request unless it is echoed back; GenAI.mil sends nothing and must see no change."""

    def test_signature_is_picked_up_from_attribute_or_model_extra(self):
        from types import SimpleNamespace
        sig = {"google": {"thought_signature": "abc123"}}
        self.assertEqual(cli._tool_call_extra(SimpleNamespace(extra_content=sig)), sig)
        self.assertEqual(cli._tool_call_extra(SimpleNamespace(model_extra={"extra_content": sig})), sig)

    def test_absent_or_malformed_extra_is_ignored(self):
        from types import SimpleNamespace
        self.assertIsNone(cli._tool_call_extra(SimpleNamespace()))
        self.assertIsNone(cli._tool_call_extra(SimpleNamespace(extra_content=None, model_extra=None)))
        self.assertIsNone(cli._tool_call_extra(SimpleNamespace(extra_content={})))
        self.assertIsNone(cli._tool_call_extra(SimpleNamespace(extra_content="not-a-dict")))


class ApiErrorFormattingTests(unittest.TestCase):
    def test_gemini_quota_error_is_summarized(self):
        class FakeErr(Exception):
            status_code = 429
            body = [{"error": {
                "code": 429,
                "message": "You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://example",
                "details": [
                    {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
                    {"retryDelay": "1592s"},
                ],
            }}]
        line = cli._format_api_error(FakeErr("Error code: 429 - [huge json]"))
        self.assertIn("API error 429", line)
        self.assertIn("You exceeded your current quota", line)
        self.assertNotIn("For more information", line)
        self.assertIn("GenerateRequestsPerDayPerProjectPerModel-FreeTier = 20", line)
        self.assertIn("retry after 1592s", line)
        self.assertIn("/model", line)

    def test_unparseable_error_shows_start_of_raw_body_not_headers(self):
        from types import SimpleNamespace
        class FakeErr(Exception):
            status_code = 400
            body = None
            response = SimpleNamespace(text="<html>Bad Request from proxy</html>", url="https://x/v1", headers={"set-cookie": "s=1"})
        saved = os.environ.pop("EUSTACE_DEBUG", None)
        try:
            line = cli._format_api_error(FakeErr("Error code: 400"))
            self.assertIn("API error 400", line)
            self.assertIn("Bad Request from proxy", line)
            self.assertNotIn("set-cookie", line)
            os.environ["EUSTACE_DEBUG"] = "1"
            self.assertIn("set-cookie", cli._format_api_error(FakeErr("Error code: 400")))
        finally:
            os.environ.pop("EUSTACE_DEBUG", None)
            if saved is not None:
                os.environ["EUSTACE_DEBUG"] = saved

    def test_plain_exception_falls_back_to_its_text(self):
        line = cli._format_api_error(RuntimeError("socket closed"))
        self.assertIn("RuntimeError", line)
        self.assertIn("socket closed", line)


class ApiKeyEntryTests(unittest.TestCase):
    """The Ctrl+V fallback must never save a clipboard error message (or anything unkeylike) as the key."""

    def _run(self, typed, clipboard):
        saved = (cli.console.input, cli._clipboard_text_raw, cli._merge_env_line, os.environ.pop(cli.API_KEY_ENV, None))
        written = []
        answers, clips = list(typed), list(clipboard)
        cli.console.input = lambda *a, **k: answers.pop(0)
        cli._clipboard_text_raw = lambda: clips.pop(0)
        cli._merge_env_line = lambda path, key, value: written.append(value)
        try:
            return cli.ensure_api_key(), written
        finally:
            cli.console.input, cli._clipboard_text_raw, cli._merge_env_line = saved[:3]
            os.environ.pop(cli.API_KEY_ENV, None)
            if saved[3] is not None:
                os.environ[cli.API_KEY_ENV] = saved[3]

    def test_ctrl_v_reads_clipboard(self):
        key, written = self._run(["\x16"], [" sk-abc123\r\n"])
        self.assertEqual(key, "sk-abc123")
        self.assertEqual(written, ["sk-abc123"])

    def test_unreadable_or_empty_clipboard_reprompts(self):
        key, written = self._run(["\x16", "\x16", "typed-key"], [None, ""])
        self.assertEqual(key, "typed-key")
        self.assertEqual(written, ["typed-key"])

    def test_gives_up_without_saving(self):
        written = []
        with self.assertRaises(SystemExit):
            _, written = self._run(["\x16", "", "  "], [None])
        self.assertEqual(written, [])

    def test_malformed_stored_key_is_not_used(self):
        os.environ[cli.API_KEY_ENV] = "bad key\x16"
        saved_input, saved_merge = cli.console.input, cli._merge_env_line
        cli.console.input = lambda *a, **k: "good"
        cli._merge_env_line = lambda *a: None
        try:
            self.assertEqual(cli.ensure_api_key(), "good")
        finally:
            cli.console.input, cli._merge_env_line = saved_input, saved_merge
            os.environ.pop(cli.API_KEY_ENV, None)


if __name__ == "__main__":
    unittest.main()
