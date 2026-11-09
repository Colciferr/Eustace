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


if __name__ == "__main__":
    unittest.main()
