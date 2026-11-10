"""Guardrails around approvals, the clipboard, listing, PHI screening, runaway turns, the audit log and dev mode."""
import json
import os
import unittest

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, call, typed, output

HOME = os.path.expanduser("~")


class ApproveAllScopeTests(unittest.TestCase):
    def test_a_covers_only_the_same_tool(self):
        h = cli.CLIApprovalHandler()
        with sandbox():
            h.current_tool = "write_file"
            with typed("a"):
                self.assertTrue(h.request_approval("Create new file: a"))
            with typed():  # same tool: no prompt
                self.assertTrue(h.request_approval("Create new file: b"))
            self.assertEqual(h.last_decision, "approved_all_turn")
            h.current_tool = "edit_file"
            with typed("n"):  # different tool: asks again
                self.assertFalse(h.request_approval("Edit file: c"))

    def test_always_ask_requests_are_never_covered_and_offer_no_a(self):
        h = cli.CLIApprovalHandler()
        with sandbox():
            h.current_tool = "run_powershell"
            h.approved_tools.add("run_powershell")  # even if it were somehow in the set
            with typed("a"):  # "a" is not an option on these prompts, so it is a denial
                self.assertFalse(h.request_approval("Run PowerShell command", always_ask=True))
            self.assertNotIn("Approve all", output())
            self.assertNotIn("run_powershell", [t for t in h.approved_tools if t != "run_powershell"])
            with typed("y"):
                self.assertTrue(h.request_approval("Run PowerShell command", always_ask=True))

    def test_new_turn_forgets_approve_all(self):
        h = cli.CLIApprovalHandler()
        with sandbox():
            h.current_tool = "write_file"
            with typed("a"):
                h.request_approval("x")
            h.reset_turn()
            with typed("n"):
                self.assertFalse(h.request_approval("x"))

    def test_shell_and_outside_workspace_reads_are_marked_always_ask(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, home):
            outside = os.path.join(home, "note.txt")
            with open(outside, "w") as f:
                f.write("x")
            cli.read_file(outside)
            cli.list_directory(home)
            cli.search_files(home, query="x")
            self.assertEqual(approval.always_ask, [True, True, True])
            if cli._trusted_powershell():
                cli.run_powershell("Write-Host hi")
                self.assertEqual(approval.always_ask[-1], True)

    def test_loop_tells_the_handler_which_tool_is_running(self):
        seen = []

        class Spy(RecordingApproval):
            def request_approval(self, *a, **k):
                seen.append(self.current_tool)
                return True

        client = ScriptedClient([call("write_file", '{"path": "a.txt", "content": "x"}', id="c1")], [text("ok")])
        with sandbox(client=client, approval=Spy()):
            cli.run_agent_turn([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}])
        self.assertEqual(seen, ["write_file"])


class ClipboardTests(unittest.TestCase):
    def setUp(self):
        self.saved = cli._clipboard_text_raw

    def tearDown(self):
        cli._clipboard_text_raw = self.saved

    def test_read_shows_a_preview_and_sends_nothing_when_denied(self):
        cli._clipboard_text_raw = lambda: "hunter2-password\nline 2\nline 3\nline 4\nline 5"
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval):
            result = cli.read_clipboard()
        self.assertIn("Operation aborted", result)
        self.assertNotIn("hunter2", result)
        title, detail, _ = approval.calls[0]
        self.assertIn("clipboard", title.lower())
        self.assertIn("44 characters", detail)
        self.assertIn("hunter2-password", detail)
        self.assertIn("+2 more lines", detail)
        self.assertNotIn("line 4", detail)

    def test_read_returns_text_when_approved(self):
        cli._clipboard_text_raw = lambda: "copied email body"
        with sandbox(approval=RecordingApproval(default=True)):
            self.assertEqual(cli.read_clipboard(), "copied email body")

    def test_empty_or_unreadable_clipboard_never_prompts(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval):
            cli._clipboard_text_raw = lambda: ""
            self.assertIn("empty", cli.read_clipboard())
            cli._clipboard_text_raw = lambda: None
            self.assertIn("Error", cli.read_clipboard())
        self.assertEqual(approval.calls, [])

    def test_phi_on_the_clipboard_is_blocked_before_the_prompt(self):
        cli._clipboard_text_raw = lambda: "Patient Diagnosis: ..."
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval):
            self.assertIn("Security Policy Block", cli.read_clipboard())
        self.assertEqual(approval.calls, [])

    def test_write_requires_approval_and_discloses_truncation(self):
        payload = "\n".join(f"Remove-Item C:\\data\\{i}" for i in range(60))
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval):
            result = cli.write_clipboard(payload)
        self.assertIn("Operation aborted", result)
        _, detail, _ = approval.calls[0]
        self.assertIn("Remove-Item C:\\data\\0", detail)
        self.assertIn("+20 more lines not shown", detail)


class ListingTests(unittest.TestCase):
    def test_listing_outside_the_workspace_asks_first(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, home):
            self.assertIn("Operation aborted", cli.list_directory(home))
            self.assertEqual(len(approval.calls), 1)
            self.assertIn("Contents of", cli.list_directory("."))
            self.assertEqual(len(approval.calls), 1)

    def test_protected_locations_are_not_listed_at_all(self):
        approval = RecordingApproval(default=True)
        saved = cli.PROTECTED_HOMES
        with sandbox(approval=approval) as (ws, home):
            cli.PROTECTED_HOMES = [os.path.normcase(home)]
            try:
                self.assertIn("Security Error", cli.list_directory(home))
            finally:
                cli.PROTECTED_HOMES = saved
        self.assertEqual(approval.calls, [])


class PhiScreeningTests(unittest.TestCase):
    def test_marker_late_in_the_file_is_still_caught(self):
        with sandbox(approval=RecordingApproval()) as (ws, _):
            with open(os.path.join(ws, "notes.txt"), "w") as f:
                f.write("filler line\n" * 2000 + "MEDICAL RECORD NUMBER: 123\n")
            self.assertIn("Security Policy Block", cli.read_file("notes.txt"))
            self.assertIn("No matches", cli.search_files(".", query="filler"))


class RunawayTurnTests(unittest.TestCase):
    def test_turn_pauses_after_the_round_limit(self):
        saved = cli.MAX_TOOL_ROUNDS
        cli.MAX_TOOL_ROUNDS = 3
        client = ScriptedClient(*[[call("list_directory", "{}", id=f"c{i}")] for i in range(10)])
        try:
            with sandbox(client=client, approval=RecordingApproval()):
                messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
                cli.run_agent_turn(messages)
        finally:
            cli.MAX_TOOL_ROUNDS = saved
        self.assertEqual(len(client.requests), 3)
        self.assertEqual([m["role"] for m in messages[2:]], ["assistant", "tool"] * 3 + ["assistant"])
        self.assertIn("Paused after 3 rounds", messages[-1]["content"])


class AuditLogTests(unittest.TestCase):
    def _records(self):
        with open(cli.AUDIT_LOG_PATH, encoding="utf-8") as f:
            raw = f.read()
        return raw, [json.loads(line) for line in raw.splitlines()]

    def test_data_is_hashed_and_actions_are_kept(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            with open(os.path.join(ws, "roster.txt"), "w") as f:
                f.write("SSgt Example, 555-0100, FILE-CONTENT-MARKER")
            cli.log_audit_event("read_file", {"path": "roster.txt"}, "auto", cli.read_file("roster.txt"))
            cli.log_audit_event("write_file", {"path": "a.txt", "content": "WRITE-CONTENT-MARKER"}, "approved_user", "Success: File written successfully to a.txt")
            cli.log_audit_event("fill_docx_template", {"template_path": "t.docx", "output_path": "o.docx",
                                                       "replacements": {"{{NAME}}": "DICT-VALUE-MARKER"}}, "denied_user",
                                "Operation aborted: User did NOT approve this action.")
            cli.log_audit_event("run_powershell", {"command": "Get-ChildItem " + "x" * 300}, "approved_user", "Exit code: 0\n--- stdout ---\nSTDOUT-MARKER")
            cli.log_audit_event("read_file", {"path": "missing.txt"}, "auto", "Error: File not found: missing.txt")
            raw, recs = self._records()
        for marker in ("FILE-CONTENT-MARKER", "WRITE-CONTENT-MARKER", "DICT-VALUE-MARKER", "STDOUT-MARKER"):
            self.assertNotIn(marker, raw)
        self.assertEqual(recs[0]["args"], {"path": "roster.txt"})
        self.assertRegex(recs[0]["result_preview"], r"^<\d+ chars, sha256:[0-9a-f]{12}>$")
        self.assertRegex(recs[1]["args"]["content"], r"^<20 chars, sha256:")
        self.assertTrue(recs[1]["result_preview"].startswith("Success: File written"))
        self.assertRegex(recs[2]["args"]["replacements"], r"^<dict, 1 items, sha256:")
        self.assertEqual((recs[2]["status"], recs[2]["decision"]), ("error", "denied_user"))
        self.assertTrue(recs[3]["args"]["command"].endswith("x" * 300))  # commands stay verbatim
        self.assertEqual(recs[4]["result_preview"], "Error: File not found: missing.txt")

    def test_loop_writes_one_record_per_tool_call(self):
        client = ScriptedClient([call("list_directory", "{}", id="c1"), call("nope", "{}", id="c2", index=1)], [text("ok")])
        with sandbox(client=client, approval=RecordingApproval()):
            cli.run_agent_turn([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}])
            _, recs = self._records()
        self.assertEqual([(r["tool"], r["status"]) for r in recs], [("list_directory", "ok"), ("nope", "error")])


class WriteProtectionTests(unittest.TestCase):
    def test_autorun_locations_are_write_protected(self):
        startup_user, startup_all = cli.PROTECTED_ROOTS[-2], cli.PROTECTED_ROOTS[-1]
        self.assertIn("startup", startup_user.lower())
        for p in (
            os.path.join(startup_user, "run.bat"),
            os.path.join(startup_all, "run.lnk"),
            os.path.join(HOME, "Documents", "WindowsPowerShell", "Microsoft.PowerShell_profile.ps1"),
            os.path.join(HOME, "OneDrive", "Documents", "PowerShell", "profile.ps1"),
        ):
            with self.subTest(path=p):
                self.assertTrue(cli._is_protected_path(p))
        for p in (
            os.path.join(HOME, "Documents", "proj", "profile.ps1"),
            os.path.join(HOME, "Documents", "WindowsPowerShell", "Modules", "tool.ps1"),
        ):
            with self.subTest(path=p):
                self.assertFalse(cli._is_protected_path(p))


class OutlookGuardTests(unittest.TestCase):
    def setUp(self):
        self.saved = (cli._get_outlook_ns, cli.DEV_MODE)

    def tearDown(self):
        cli._get_outlook_ns, cli.DEV_MODE = self.saved

    def test_draft_preview_discloses_how_much_is_hidden(self):
        cli._get_outlook_ns = lambda: (object(), None)
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval):
            result = cli.create_draft_email("Status", "\n".join(f"line {i}" for i in range(100)), to_hint="a@b.mil")
        self.assertIn("Operation aborted", result)
        _, detail, _ = approval.calls[0]
        self.assertIn("line 39", detail)
        self.assertNotIn("line 40", detail)
        self.assertIn("+60 more lines not shown", detail)

    def test_dev_mode_asks_before_touching_the_mailbox(self):
        def must_not_be_called():
            raise AssertionError("Outlook was contacted without approval")

        cli._get_outlook_ns = must_not_be_called
        cli.DEV_MODE = True
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval):
            self.assertIn("Operation aborted", cli.list_inbox())
            self.assertIn("Operation aborted", cli.get_calendar())
        self.assertEqual(approval.always_ask, [True, True])
        self.assertIn("public Gemini API", approval.calls[0][0])

    def test_normal_mode_has_no_extra_prompt(self):
        cli.DEV_MODE = False
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval):
            self.assertIsNone(cli._dev_mode_gate("inbox subject lines"))
        self.assertEqual(approval.calls, [])


class SearchLimitsTests(unittest.TestCase):
    def test_result_count_is_clamped(self):
        with sandbox(approval=RecordingApproval()) as (ws, _):
            for i in range(3):
                open(os.path.join(ws, f"f{i}.txt"), "w").close()
            self.assertIn("Found 1 matching", cli.search_files(".", max_results=0))
            self.assertIn("Found 3 matching", cli.search_files(".", max_results=10**9))


if __name__ == "__main__":
    unittest.main()
