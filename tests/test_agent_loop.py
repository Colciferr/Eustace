"""End-to-end tests of the streaming agent loop against a scripted client."""
import json
import os
import unittest

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, call, typed, output


class ToolSchemaTests(unittest.TestCase):
    def test_schema_matches_golden_file(self):
        """The tool schema is the model-facing API. Any change must be deliberate: update tools_schema.json with it."""
        golden = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools_schema.json")
        with open(golden, "r", encoding="utf-8") as f:
            self.assertEqual(json.loads(f.read()), cli.TOOLS)

    def test_every_tool_is_callable_and_labelled(self):
        names = [t["function"]["name"] for t in cli.TOOLS]
        self.assertEqual(sorted(names), sorted(cli.AVAILABLE_FUNCTIONS))
        self.assertEqual(sorted(names), sorted(cli.TOOL_FRIENDLY_LABELS))


class BasicLoopTests(unittest.TestCase):
    def test_plain_text_reply(self):
        client = ScriptedClient([text("Hello "), text("there")])
        with sandbox(client=client, approval=RecordingApproval()):
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
            cli.run_agent_turn(messages)
        self.assertEqual(messages[-1], {"role": "assistant", "content": "Hello there"})
        self.assertEqual(len(client.requests), 1)

    def test_tool_round_trip(self):
        client = ScriptedClient(
            [call("list_directory", '{"path": "."}', id="c1")],
            [text("done")],
        )
        with sandbox(client=client, approval=RecordingApproval()) as (ws, _):
            open(os.path.join(ws, "note.txt"), "w").close()
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "ls"}]
            cli.run_agent_turn(messages)
        roles = [m["role"] for m in messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant"])
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "list_directory")
        self.assertEqual(messages[3]["tool_call_id"], "c1")
        self.assertIn("note.txt", messages[3]["content"])
        # The second request carried the tool result back to the model
        self.assertEqual(client.requests[1]["messages"][-1]["role"], "tool")


def assert_history_valid(test, messages):
    """Every assistant tool call must be followed by exactly one tool result with its id, in order."""
    i = 0
    while i < len(messages):
        calls = messages[i].get("tool_calls") or []
        for n, tc in enumerate(calls, start=1):
            test.assertLess(i + n, len(messages), "tool call without a result")
            test.assertEqual(messages[i + n]["role"], "tool")
            test.assertEqual(messages[i + n]["tool_call_id"], tc["id"])
        i += 1 + len(calls)
        if i < len(messages):
            test.assertNotEqual(messages[i]["role"], "tool", "tool result without a matching call")


class InterruptedTurnTests(unittest.TestCase):
    def _messages(self):
        return [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]

    def test_interrupt_between_tool_calls_keeps_completed_work(self):
        class InterruptOnApproval(RecordingApproval):
            def request_approval(self, *a, **k):
                raise KeyboardInterrupt

        client = ScriptedClient([
            call("list_directory", '{"path": "."}', id="c1", index=0),
            call("write_file", '{"path": "a.txt", "content": "x"}', id="c2", index=1),
            call("list_directory", "{}", id="c3", index=2),
        ])
        with sandbox(client=client, approval=InterruptOnApproval()) as (ws, _):
            messages = self._messages()
            cli.run_agent_turn(messages)
            self.assertFalse(os.path.exists(os.path.join(ws, "a.txt")))
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "tool", "tool", "assistant"])
        assert_history_valid(self, messages)
        self.assertIn("Contents of", messages[3]["content"])            # the call that ran keeps its real result
        self.assertEqual(messages[4]["content"], cli.INTERRUPTED_TOOL_NOTE)
        self.assertEqual(messages[5]["content"], cli.INTERRUPTED_TOOL_NOTE)
        self.assertIn("interrupted", messages[-1]["content"])

    def test_interrupt_while_streaming_after_a_tool_round(self):
        def interrupted_stream():
            yield text("Partial ans")
            raise KeyboardInterrupt

        client = ScriptedClient([call("list_directory", "{}", id="c1")], interrupted_stream)
        with sandbox(client=client, approval=RecordingApproval()):
            messages = self._messages()
            cli.run_agent_turn(messages)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "tool", "assistant"])
        assert_history_valid(self, messages)
        self.assertTrue(messages[-1]["content"].startswith("Partial ans"))
        self.assertIn("interrupted", messages[-1]["content"])

    def test_interrupt_before_anything_happened(self):
        def interrupted_stream():
            raise KeyboardInterrupt
            yield

        with sandbox(client=ScriptedClient(interrupted_stream), approval=RecordingApproval()):
            messages = self._messages()
            cli.run_agent_turn(messages)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])

    def test_api_error_rolls_back_but_records_what_ran(self):
        client = ScriptedClient([call("list_directory", '{"path": "."}', id="c1")], RuntimeError("boom"))
        with sandbox(client=client, approval=RecordingApproval()):
            messages = self._messages()
            cli.run_agent_turn(messages)
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant"])
        last = messages[-1]["content"]
        self.assertIn("boom", last)
        self.assertIn("Tool calls that completed before the failure", last)
        self.assertIn("- list_directory(", last)
        self.assertIn("Contents of", last)

    def test_api_error_with_no_tool_calls(self):
        with sandbox(client=ScriptedClient(RuntimeError("boom")), approval=RecordingApproval()):
            messages = self._messages()
            cli.run_agent_turn(messages)
        self.assertNotIn("Tool calls", messages[-1]["content"])


class ToolCallAssemblyTests(unittest.TestCase):
    def _assemble(self, *deltas):
        buf = {}
        for d in deltas:
            cli._accumulate_tool_call(buf, d.choices[0].delta.tool_calls[0])
        return [(buf[i]["id"], buf[i]["function"]["name"], buf[i]["function"]["arguments"]) for i in sorted(buf)]

    def test_indexed_stream_with_split_arguments(self):
        got = self._assemble(
            call("read_file", "", id="a", index=0),
            call(None, '{"pa', index=0),
            call("list_directory", "{}", id="b", index=1),
            call(None, 'th": "x"}', index=0),
        )
        self.assertEqual(got, [("a", "read_file", '{"path": "x"}'), ("b", "list_directory", "{}")])

    def test_missing_index_with_split_arguments_stays_one_call(self):
        got = self._assemble(
            call("read_file", "", id="a", index=None),
            call(None, '{"pa', index=None),
            call(None, 'th": "x"}', index=None),
        )
        self.assertEqual(got, [("a", "read_file", '{"path": "x"}')])

    def test_missing_index_new_id_starts_a_new_call(self):
        got = self._assemble(
            call("read_file", '{"path": "x"}', id="a", index=None),
            call("list_directory", "{}", id="b", index=None),
        )
        self.assertEqual(got, [("a", "read_file", '{"path": "x"}'), ("b", "list_directory", "{}")])

    def test_missing_index_and_id_new_name_starts_a_new_call(self):
        got = self._assemble(
            call("read_file", '{"path": "x"}', index=None),
            call("list_directory", "{}", index=None),
        )
        self.assertEqual(got, [("", "read_file", '{"path": "x"}'), ("", "list_directory", "{}")])


class DispatchValidationTests(unittest.TestCase):
    def _run(self, name, args):
        client = ScriptedClient([call(name, args, id="c1")], [text("ok")])
        approval = RecordingApproval(default=True)
        with sandbox(client=client, approval=approval) as (ws, _):
            with open(os.path.join(ws, "note.txt"), "w") as f:
                f.write("x" * 500)
            messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]
            cli.run_agent_turn(messages)
            created = sorted(os.listdir(ws))
        assert_history_valid(self, messages)
        return messages[3]["content"], created, approval

    def test_truncated_json_runs_nothing(self):
        result, created, approval = self._run("write_file", '{"path": "a.txt", "content": "half')
        self.assertIn("not valid JSON", result)
        self.assertEqual(created, ["note.txt"])
        self.assertEqual(approval.calls, [])

    def test_non_object_arguments(self):
        result, _, _ = self._run("list_directory", '["."]')
        self.assertIn("must be a JSON object", result)

    def test_unknown_tool(self):
        result, _, _ = self._run("format_disk", "{}")
        self.assertIn("Unknown tool 'format_disk'", result)

    def test_undeclared_parameter_is_rejected(self):
        """read_file's internal max_chars is not in the schema, so the model cannot raise the truncation limit."""
        result, _, _ = self._run("read_file", '{"path": "note.txt", "max_chars": 10}')
        self.assertIn("does not accept the parameter(s): max_chars", result)

    def test_missing_required_parameter(self):
        result, _, _ = self._run("write_file", '{"path": "a.txt"}')
        self.assertIn("missing required parameter(s): content", result)

    def test_empty_arguments_string_means_no_arguments(self):
        result, _, _ = self._run("list_directory", "")
        self.assertIn("note.txt", result)


class CompactTests(unittest.TestCase):
    HISTORY = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "list"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "list_directory", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "Contents of ..."},
        {"role": "assistant", "content": "done"},
    ]

    def test_compact_sends_tools_but_forbids_calls_and_replaces_history(self):
        client = ScriptedClient([text("Summary: "), text("listed a folder.")])
        with sandbox(client=client):
            out = cli.compact_messages(list(self.HISTORY))
        req = client.requests[0]
        self.assertEqual(req["tool_choice"], "none")
        self.assertEqual(req["tools"], cli.TOOLS)
        self.assertTrue(req["stream"])
        self.assertEqual(req["messages"][-1]["content"], cli.COMPACT_INSTRUCTION)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant"])
        self.assertIn("Summary: listed a folder.", out[1]["content"])

    def test_compact_can_be_interrupted_and_leaves_history_alone(self):
        def interrupted():
            yield text("Summ")
            raise KeyboardInterrupt

        history = list(self.HISTORY)
        with sandbox(client=ScriptedClient(interrupted)):
            with self.assertRaises(KeyboardInterrupt):
                cli.compact_messages(history)
        self.assertEqual(history, self.HISTORY)

    def test_empty_summary_is_an_error(self):
        with sandbox(client=ScriptedClient([text("  ")])):
            with self.assertRaises(RuntimeError):
                cli.compact_messages(list(self.HISTORY))


class DisplayFidelityTests(unittest.TestCase):
    def test_link_targets_are_printed(self):
        with sandbox():
            cli.console.print(cli._render_markdown("See [the docs](https://evil.example/c?d=secret)."))
            self.assertIn("https://evil.example/c?d=secret", output())

    def test_reply_text_cannot_smuggle_terminal_escapes(self):
        with sandbox():
            cli.console.print(cli._render_markdown("ok\x1b]52;c;QUJD\x07 done"))
            self.assertNotIn("\x1b]52", output())
            self.assertIn("\\x1b]52;c;QUJD\\x07", output())


class ApprovalPromptTests(unittest.TestCase):
    LONG_DIFF = "\n".join(f"+line {i}" for i in range(1, 151))

    def test_default_is_deny(self):
        handler = cli.CLIApprovalHandler()
        with sandbox(), typed(""):
            self.assertFalse(handler.request_approval("Edit file: x", diff_text="+a"))
        self.assertEqual(handler.last_decision, "denied_user")

    def test_only_whole_word_answers_approve(self):
        for answer, expected in (("y", True), ("YES", True), (" yes ", True), ("yeah sure", False), ("you should", False),
                                 ("always", False), ("1", False), ("ok", False)):
            handler = cli.CLIApprovalHandler()
            with self.subTest(answer=answer), sandbox(), typed(answer):
                self.assertEqual(handler.request_approval("Edit file: x", diff_text="+a"), expected)

    def test_long_diff_is_cut_with_notice_and_v_shows_all(self):
        handler = cli.CLIApprovalHandler()
        with sandbox(), typed("v", "y"):
            self.assertTrue(handler.request_approval("Edit file: x", diff_text=self.LONG_DIFF))
            out = output()
        self.assertIn("50 more diff lines", out)
        self.assertIn("View Full Diff", out)
        self.assertIn("150 + line 150", out)

    def test_long_diff_without_v_never_shows_the_tail(self):
        handler = cli.CLIApprovalHandler()
        with sandbox(), typed("n"):
            self.assertFalse(handler.request_approval("Edit file: x", diff_text=self.LONG_DIFF))
            out = output()
        self.assertIn("100 + line 100", out)
        self.assertNotIn("+ line 101", out)

    def test_short_diff_offers_no_view_option(self):
        handler = cli.CLIApprovalHandler()
        with sandbox(), typed("v"):  # "v" is not an option here, so it is a denial
            self.assertFalse(handler.request_approval("Edit file: x", diff_text="+a"))
            self.assertNotIn("View Full Diff", output())

    def test_control_characters_in_title_and_diff_are_visible(self):
        handler = cli.CLIApprovalHandler()
        with sandbox(), typed("n"):
            handler.request_approval("Edit file: a\rb", diff_text="+safe\x1b[2Jtext")
            out = output()
        self.assertIn("a\\x0db", out)
        self.assertIn("\\x1b[2J", out)
        self.assertNotIn("\x1b[2J", out)


class MainLoopSmokeTests(unittest.TestCase):
    """Drives main() itself through every slash command with scripted keyboard input."""

    def test_full_session(self):
        client = ScriptedClient(
            [text("Hello!")],                                           # reply to "hi"
            [text("Summary of the chat.")],                             # /compact
            [call("list_directory", "{}", id="c1")], [text("Listed.")], # reply to "list"
        )
        saved_init, saved_model, saved_name = cli._init_client, cli.MODEL, cli.AUTOSAVE_NAME
        cli._init_client = lambda: None
        try:
            with sandbox(client=client, approval=RecordingApproval()) as (ws, home):
                with typed("", "/help", "/status", "/nope", "/sessions", "hi", "/save my session", "/sessions", "/compact",
                           "/model", "", "/model custom-model-id", "/clear", "list", "/resume my session", "/resume 99", "/exit"):
                    cli.main()
                out = output()
                saved = sorted(os.listdir(cli.SESSIONS_DIR))
                with open(cli.AUDIT_LOG_PATH, encoding="utf-8") as f:
                    audit_lines = f.read().splitlines()
                self.assertEqual(cli.load_saved_model(), "custom-model-id")
        finally:
            cli._init_client, cli.MODEL, cli.AUTOSAVE_NAME = saved_init, saved_model, saved_name
        self.assertIn(f"Eustace v{cli.VERSION}", out)
        for name, (description, _) in cli.COMMANDS.items():  # /help lists every command the loop dispatches
            self.assertIn(name, out)
            self.assertIn(description, out)
        self.assertIn("Session:", out)
        self.assertIn("Unknown command /nope", out)
        self.assertIn("No saved sessions yet", out)
        self.assertIn("Hello!", out)
        self.assertIn("Context compacted", out)
        self.assertIn("Select AI Model", out)
        self.assertIn("custom-model-id", out)
        self.assertIn("Context cleared", out)
        self.assertIn("Resumed session", out)
        self.assertIn("not found", out)
        self.assertIn("Goodbye", out)
        self.assertNotIn("Error:", out)
        self.assertIn("my_session.json", saved)
        self.assertEqual(len(saved), 2)
        self.assertEqual(len(audit_lines), 1)
        self.assertEqual(client.rounds, [])

    def test_end_of_input_exits_cleanly(self):
        saved_init = cli._init_client
        cli._init_client = lambda: None

        def eof(prompt=""):
            raise EOFError

        import builtins
        real_input = builtins.input
        builtins.input = eof
        try:
            with sandbox(client=ScriptedClient(), approval=RecordingApproval()):
                cli.main()
                self.assertIn("Goodbye", output())
        finally:
            builtins.input = real_input
            cli._init_client = saved_init


if __name__ == "__main__":
    unittest.main()
