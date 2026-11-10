"""End-to-end tests of the streaming agent loop against a scripted client."""
import json
import os
import unittest

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, call


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


if __name__ == "__main__":
    unittest.main()
