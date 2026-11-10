"""The terminal UI: diff rendering, the approval menu, the input box, completions, and the plain fallback."""
import io
import os
import sys
import unittest

from rich.console import Console

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, call, typed, keys, output

DOWN, UP, ENTER, CTRL_C, CTRL_D, ALT_ENTER = "\x1b[B", "\x1b[A", "\r", "\x03", "\x04", "\x1b\r"


def drawn(renderable, width: int) -> str:
    out = io.StringIO()
    Console(file=out, width=width).print(renderable)
    return out.getvalue()


OLD = "# Weekly status\n## Open items\nSuspense: 7 Nov 2026\nStatus: awaiting input\nOwner: Plans & Programs\n"
NEW = "# Weekly status\n## Open items\nSuspense: 14 Nov 2026\nStatus: in coordination\nOwner: Plans & Programs\n"


class DiffTextTests(unittest.TestCase):
    def test_text_without_a_final_newline_keeps_one_entry_per_line(self):
        diff = cli.build_unified_diff("a", "b", "f.txt")
        self.assertEqual(diff.splitlines()[-2:], ["-a", "+b"])

    def test_final_newline_only_change_is_stated(self):
        self.assertIn("\\ Final newline added", cli.build_unified_diff("a", "a\n", "f.txt"))
        self.assertIn("\\ Final newline removed", cli.build_unified_diff("a\nb\n", "a\nc", "f.txt"))
        self.assertEqual(cli.build_unified_diff("a\n", "a\n", "f.txt"), "")


class DiffRenderTests(unittest.TestCase):
    def rows(self, old=OLD, new=NEW):
        return cli._diff_rows(cli.build_unified_diff(old, new, "status.md"))

    def test_rows_pair_removed_with_added_and_number_both_sides(self):
        rows = self.rows()
        self.assertEqual(rows[2], ("chg", 3, "Suspense: 7 Nov 2026", 3, "Suspense: 14 Nov 2026"))
        self.assertEqual(rows[3], ("chg", 4, "Status: awaiting input", 4, "Status: in coordination"))
        self.assertEqual(rows[4], ("ctx", 5, "Owner: Plans & Programs", 5, "Owner: Plans & Programs"))

    def test_line_numbers_across_hunks_and_uneven_changes(self):
        old = "".join(f"line {i}\n" for i in range(1, 41))
        new = old.replace("line 5\n", "line 5\ninserted\n").replace("line 30\n", "")
        rows = cli._diff_rows(cli.build_unified_diff(old, new, "f.txt"))
        self.assertIn(("chg", None, None, 6, "inserted"), rows)
        self.assertIn(("chg", 30, "line 30", None, None), rows)
        self.assertIn(("sep", None, None, None, None), rows)
        self.assertIn(("ctx", 31, "line 31", 31, "line 31"), rows)  # after +1 and -1 the numbering lines up again
        self.assertIn(("ctx", 29, "line 29", 30, "line 29"), rows)

    def test_wide_window_draws_before_and_after_side_by_side(self):
        out = drawn(cli._render_diff(self.rows(), 116), 120)
        self.assertRegex(out, r"before +│ +after")
        self.assertRegex(out, r" 3 Suspense: 7 Nov 2026 +│ +3 Suspense: 14 Nov 2026")
        self.assertRegex(out, r" 5 Owner: Plans & Programs +│ +5 Owner: Plans & Programs")
        self.assertNotIn("- Suspense", out)

    def test_narrow_window_stacks_with_line_numbers(self):
        out = drawn(cli._render_diff(self.rows(), 76), 80)
        self.assertNotIn("│", out)
        self.assertIn(" 3 - Suspense: 7 Nov 2026", out)
        self.assertIn(" 3 + Suspense: 14 Nov 2026", out)
        self.assertIn(" 5   Owner: Plans & Programs", out)

    def test_line_too_long_for_half_the_window_stacks(self):
        long_new = NEW.replace("in coordination", "in coordination with " + "many offices " * 8)
        out = drawn(cli._render_diff(self.rows(new=long_new), 116), 120)
        self.assertNotIn("│", out)
        self.assertIn("+ Status: in coordination with many offices", out)

    def test_new_file_stacks_because_there_is_no_before(self):
        out = drawn(cli._render_diff(self.rows(old=""), 116), 120)
        self.assertNotIn("before", out)
        self.assertIn(" 1 + # Weekly status", out)

    def test_wide_characters_do_not_break_the_columns(self):
        rows = self.rows(old="項目: 未完了\nend\n", new="項目: 完了\nend\n")
        lines = drawn(cli._render_diff(rows, 116), 120).splitlines()
        self.assertEqual(len({line.index("│") + sum(1 for c in line[:line.index("│")] if ord(c) > 0x2E80) for line in lines if "│" in line}), 1)


class MenuTests(unittest.TestCase):
    OPTIONS = [("yes", "Yes"), ("all", "Yes, always"), ("tell", "No, tell")]

    def ask(self, *chunks, allow_view=False):
        with sandbox(), keys(*chunks):
            return cli._ask_menu("Proceed?", self.OPTIONS, allow_view=allow_view)

    def test_arrows_and_numbers_choose_and_enter_confirms(self):
        self.assertEqual(self.ask(DOWN, ENTER), "yes")
        self.assertEqual(self.ask(UP, ENTER), "tell")
        self.assertEqual(self.ask(DOWN, DOWN, ENTER), "all")
        self.assertEqual(self.ask(DOWN, DOWN, DOWN, DOWN, ENTER), "yes")  # wraps around
        self.assertEqual(self.ask("2", ENTER), "all")
        self.assertEqual(self.ask("3", UP, ENTER), "all")

    def test_nothing_is_approved_without_a_deliberate_choice_and_enter(self):
        self.assertIsNone(self.ask(ENTER, CTRL_C))        # Enter with nothing highlighted does nothing
        self.assertIsNone(self.ask("1", CTRL_C))          # a number alone does not confirm
        self.assertIsNone(self.ask("y", "yes", CTRL_C))   # letters mean nothing here
        self.assertIsNone(self.ask(CTRL_C))
        self.assertIsNone(self.ask("\x1b"))               # Esc

    def test_view_key_only_when_offered(self):
        self.assertEqual(self.ask("v", allow_view=True), "view")
        self.assertIsNone(self.ask("v", CTRL_C))

    def test_pending_keystrokes_are_discarded_before_the_menu_runs(self):
        events = []
        saved = cli._flush_console_input
        cli._flush_console_input = lambda: events.append("flush")
        try:
            self.ask(DOWN, ENTER)
        finally:
            cli._flush_console_input = saved
        self.assertEqual(events, ["flush"])


class ApprovalMenuTests(unittest.TestCase):
    def handler(self, tool="write_file"):
        h = cli.CLIApprovalHandler()
        h.current_tool = tool
        return h

    def test_yes(self):
        h = self.handler()
        with sandbox(), keys(DOWN, ENTER):
            self.assertTrue(h.request_approval("Create new file: a.txt", diff_text="+hello"))
            self.assertNotIn("[Y]", output())  # the typed-letter legend is not shown with the menu
        self.assertEqual(h.last_decision, "approved_user")

    def test_yes_for_this_tool_this_turn(self):
        h = self.handler()
        with sandbox(), keys("2", ENTER):
            self.assertTrue(h.request_approval("Create new file: a.txt"))
            self.assertTrue(h.request_approval("Create new file: b.txt"))  # no keys left: must not prompt
            h.current_tool = "edit_file"
        self.assertEqual(h.approved_tools, {"write_file"})

    def test_always_ask_requests_do_not_offer_approve_all(self):
        h = self.handler("run_powershell")
        with sandbox(), keys("2", ENTER, "check with me first", ENTER):
            # with only two options, 2 is "tell Eustace", never "approve all"
            self.assertFalse(h.request_approval("Run PowerShell command", always_ask=True))
        self.assertEqual(h.approved_tools, set())
        self.assertEqual(h.last_feedback, "check with me first")

    def test_cancel_is_a_denial(self):
        h = self.handler()
        with sandbox(), keys(CTRL_C):
            self.assertFalse(h.request_approval("Create new file: a.txt"))
        self.assertEqual((h.last_decision, h.last_feedback), ("denied_user", ""))

    def test_view_shows_the_whole_diff_then_asks_again(self):
        h = self.handler()
        long_diff = "\n".join(f"+line {i}" for i in range(1, 151))
        with sandbox(), keys("v", DOWN, ENTER):
            self.assertTrue(h.request_approval("Create new file: a.txt", diff_text=long_diff))
            out = output()
        self.assertIn("50 more diff lines", out)
        self.assertIn("150 + line 150", out)

    def test_denial_reason_reaches_the_model_and_nothing_is_written(self):
        client = ScriptedClient([call("write_file", '{"path": "a.txt", "content": "x"}', id="c1")], [text("ok")])
        with sandbox(client=client) as (ws, _):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
            with keys("3", ENTER, "put it in notes.txt instead", ENTER):
                cli.run_agent_turn(messages)
            self.assertEqual(os.listdir(ws), [])
        result = messages[3]["content"]
        self.assertIn("Operation aborted", result)
        self.assertTrue(result.endswith("The user said: put it in notes.txt instead"))
        self.assertEqual(client.requests[1]["messages"][-1]["content"], result)

    def test_feedback_does_not_leak_into_the_next_tool_call(self):
        client = ScriptedClient([call("write_file", '{"path": "a.txt", "content": "x"}', id="c1"),
                                 call("list_directory", "{}", id="c2", index=1)], [text("ok")])
        with sandbox(client=client):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
            with keys("3", ENTER, "no thanks", ENTER):
                cli.run_agent_turn(messages)
        self.assertIn("The user said: no thanks", messages[3]["content"])
        self.assertNotIn("The user said", messages[4]["content"])


class InputBoxTests(unittest.TestCase):
    def box(self, *chunks):
        with sandbox(), keys(*chunks):
            return cli._prompt_box("model · ~", "~0 tokens")

    def test_enter_sends(self):
        self.assertEqual(self.box("hello there", ENTER), "hello there")

    def test_three_ways_to_add_a_line(self):
        self.assertEqual(self.box("one", ALT_ENTER, "two", ENTER), "one\ntwo")
        self.assertEqual(self.box("one\\", ENTER, "two", ENTER), "one\ntwo")
        self.assertEqual(self.box("one", "\n", "two", ENTER), "one\ntwo")  # Ctrl+J

    def test_multi_line_paste_is_one_message(self):
        pasted = "\x1b[200~first line\nsecond line\nthird line\x1b[201~"
        self.assertEqual(self.box(pasted, ENTER), "first line\nsecond line\nthird line")

    def test_empty_enter_does_nothing(self):
        self.assertEqual(self.box(ENTER, "  ", ENTER, "x", ENTER), "  x")

    def test_ctrl_c_clears_then_interrupts_and_ctrl_d_exits(self):
        self.assertEqual(self.box("junk", CTRL_C, "kept", ENTER), "kept")
        with self.assertRaises(KeyboardInterrupt):
            self.box(CTRL_C)
        with self.assertRaises(EOFError):
            self.box(CTRL_D)

    def test_history_is_saved_inside_the_eustace_home(self):
        from prompt_toolkit.history import FileHistory
        with sandbox() as (_, home), keys("remember me", ENTER):
            cli._prompt_box("m", "t")
            path = os.path.join(home, "history")
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(list(FileHistory(path).load_history_strings()), ["remember me"])

    def test_read_user_input_leaves_the_message_in_the_transcript(self):
        with sandbox(), keys("what changed?", ENTER):
            got = cli._read_user_input([{"role": "system", "content": "s"}])
            self.assertEqual(got, "what changed?")
            self.assertIn("> what changed?", output())

    def test_main_loop_runs_on_the_input_box(self):
        saved_init, saved_model = cli._init_client, cli.MODEL
        cli._init_client = lambda: None
        try:
            with sandbox(client=ScriptedClient([text("Hi.")]), approval=RecordingApproval()):
                with keys("/status", ENTER, "hello", ENTER, "/exit", ENTER):
                    cli.main()
                out = output()
        finally:
            cli._init_client, cli.MODEL = saved_init, saved_model
        self.assertIn("Session:", out)
        self.assertIn("> hello", out)
        self.assertIn("Hi.", out)
        self.assertIn("Goodbye", out)


class CompletionTests(unittest.TestCase):
    def names(self, typed_text):
        return [c[0] for c in cli._completions_for(typed_text)]

    def test_slash_commands_filter_as_you_type(self):
        self.assertEqual(self.names("/"), list(cli.COMMANDS))
        self.assertEqual(self.names("/s"), ["/status", "/save", "/sessions"])
        self.assertEqual(self.names("/SA"), ["/save"])
        self.assertEqual(self.names("/save notes"), [])     # past the first word: no menu
        self.assertEqual(self.names("see /s"), [])          # only at the start of the message
        self.assertEqual(cli._completions_for("/s")[0], ("/status", -2, cli.COMMANDS["/status"][0]))

    def test_at_completes_paths_and_hides_protected_and_ignored(self):
        with sandbox() as (ws, _):
            os.makedirs(os.path.join(ws, "reports"))
            os.makedirs(os.path.join(ws, ".git"))
            os.makedirs(os.path.join(ws, "node_modules"))
            for name in ("reply_template.docx", ".env", "secrets.txt", os.path.join("reports", "q3_report.docx"),
                         os.path.join("reports", "id_rsa")):
                open(os.path.join(ws, name), "w").close()
            sep = os.sep
            self.assertEqual(self.names("summarize @"), [f"reports{sep}", "reply_template.docx"])
            self.assertEqual(self.names("summarize @rep"), [f"reports{sep}", "reply_template.docx"])
            self.assertEqual(self.names("@reports/"), [f"reports{sep}q3_report.docx"])
            self.assertEqual(cli._completions_for("see @reports/q3")[0], (f"reports{sep}q3_report.docx", -len("reports/q3"), ""))
            for outside in ("@../", "@..", f"@{ws}", "@C:"):
                self.assertEqual(self.names(outside), [], outside)
            self.assertEqual(self.names("mail me at a@rep"), [])  # an @ inside a word is not a file reference


class FallbackTests(unittest.TestCase):
    def setUp(self):
        self.saved = (sys.stdin, sys.stdout, os.environ.pop("EUSTACE_PLAIN", None))

        class Tty(io.StringIO):
            def isatty(self):
                return True

        sys.stdin, sys.stdout = Tty(), Tty()

    def tearDown(self):
        sys.stdin, sys.stdout = self.saved[:2]
        os.environ.pop("EUSTACE_PLAIN", None)
        if self.saved[2] is not None:
            os.environ["EUSTACE_PLAIN"] = self.saved[2]

    def test_enabled_on_a_real_console(self):
        self.assertTrue(cli._ui_enabled())

    def test_switch_and_piped_input_turn_it_off(self):
        os.environ["EUSTACE_PLAIN"] = "1"
        self.assertFalse(cli._ui_enabled())
        os.environ["EUSTACE_PLAIN"] = "0"
        self.assertTrue(cli._ui_enabled())
        sys.stdin = io.StringIO()
        self.assertFalse(cli._ui_enabled())

    def test_blocked_prompt_toolkit_falls_back_to_plain_prompts(self):
        real = cli.importlib.import_module

        def blocked(name, *a, **k):
            if name.startswith("prompt_toolkit"):
                raise ImportError("An Application Control policy has blocked this file.")
            return real(name, *a, **k)

        cli.importlib.import_module = blocked
        try:
            self.assertFalse(cli._ui_enabled())
        finally:
            cli.importlib.import_module = real

    def test_plain_prompts_still_work(self):
        sys.stdin, sys.stdout = self.saved[:2]  # not a console: the plain path
        h = cli.CLIApprovalHandler()
        h.current_tool = "write_file"
        with sandbox(), typed("a"):
            self.assertTrue(h.request_approval("Create new file: a.txt", diff_text="+x"))
            self.assertIn("[A]", output())
        with sandbox(), typed("typed message"):
            self.assertEqual(cli._read_user_input([]), "typed message")


class DependencyTests(unittest.TestCase):
    def test_ui_dependencies_contain_no_compiled_code(self):
        """Compiled modules are what application control blocks; the pin on wcwidth exists to keep it that way."""
        import prompt_toolkit
        import wcwidth
        for package in (prompt_toolkit, wcwidth):
            folder = os.path.dirname(package.__file__)
            compiled = [f for _, _, files in os.walk(folder) for f in files if f.endswith((".pyd", ".so", ".dll"))]
            self.assertEqual(compiled, [], package.__name__)


if __name__ == "__main__":
    unittest.main()
