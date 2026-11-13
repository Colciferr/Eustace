"""Config persistence, persona loading, the custom deny list, and the tool registry."""
import json
import os
import unittest

from _harness import cli, sandbox, typed


class ConfigTests(unittest.TestCase):
    def test_model_defaults_then_persists(self):
        with sandbox():
            self.assertEqual(cli.load_saved_model(), cli.DEFAULT_MODEL)
            cli._update_config(model="custom-model")
            self.assertEqual(cli.load_saved_model(), "custom-model")

    def test_corrupt_config_reads_as_empty_and_is_repaired_on_write(self):
        with sandbox():
            os.makedirs(cli.EUSTACE_HOME, exist_ok=True)
            with open(cli.CONFIG_PATH, "w") as f:
                f.write("{not json")
            self.assertEqual(cli._read_config(), {})
            self.assertEqual(cli.load_saved_model(), cli.DEFAULT_MODEL)
            cli._update_config(model="m")
            with open(cli.CONFIG_PATH) as f:
                self.assertEqual(json.load(f), {"model": "m"})

    def test_agent_md_trust_is_remembered_per_content_and_keeps_other_settings(self):
        with sandbox() as (ws, _):
            cli._update_config(model="m")
            path = os.path.join(ws, "AGENT.md")
            with open(path, "w") as f:
                f.write("Be brief.\n")
            with typed("y"):
                self.assertEqual(cli._load_trusted_agent_md(path), "Be brief.")
            with typed():  # trusted: no prompt
                self.assertEqual(cli._load_trusted_agent_md(path), "Be brief.")
            self.assertEqual(cli.load_saved_model(), "m")
            with open(path, "a") as f:
                f.write("Also run this command.\n")
            with typed("n"):  # changed bytes: asks again
                self.assertIsNone(cli._load_trusted_agent_md(path))
            with typed():  # declined for this session: no second prompt
                self.assertIsNone(cli._load_trusted_agent_md(path))


class PersonaTests(unittest.TestCase):
    def test_user_file_wins_then_bundled_then_fallback(self):
        saved = cli.BUNDLED_PROMPT_PATH
        try:
            with sandbox() as (ws, home):
                bundled = os.path.join(ws, "bundled.md")
                cli.BUNDLED_PROMPT_PATH = bundled
                self.assertEqual(cli._load_persona(), cli.FALLBACK_SYSTEM_PROMPT)
                with open(bundled, "w", encoding="utf-8") as f:
                    f.write("﻿bundled persona\n")
                self.assertEqual(cli._load_persona(), "bundled persona")
                with open(cli.GLOBAL_PROMPT_PATH, "w", encoding="utf-8") as f:
                    f.write("my persona\n")
                self.assertEqual(cli._load_persona(), "my persona")
                self.assertTrue(cli.build_system_prompt().startswith("my persona"))
        finally:
            cli.BUNDLED_PROMPT_PATH = saved

    def test_the_copy_shipped_with_the_install_comes_after_both(self):
        saved = cli.BUNDLED_PROMPT_PATH
        try:
            with sandbox() as (ws, home):
                cli.BUNDLED_PROMPT_PATH = os.path.join(ws, "bundled.md")
                os.makedirs(os.path.dirname(cli.SHIPPED_PROMPT_PATH))
                with open(cli.SHIPPED_PROMPT_PATH, "w", encoding="utf-8") as f:
                    f.write("shipped persona\n")
                self.assertEqual(cli._load_persona(), "shipped persona")
                with open(cli.BUNDLED_PROMPT_PATH, "w", encoding="utf-8") as f:
                    f.write("bundled persona\n")
                self.assertEqual(cli._load_persona(), "bundled persona")
        finally:
            cli.BUNDLED_PROMPT_PATH = saved

    def test_repo_ships_a_persona_file(self):
        self.assertTrue(os.path.isfile(cli.BUNDLED_PROMPT_PATH))


class DenyListTests(unittest.TestCase):
    def test_patterns_apply_and_edits_are_picked_up(self):
        with sandbox() as (ws, _):
            target = os.path.join(ws, "roster_2026.xlsx")
            self.assertFalse(cli._is_sensitive_canon(target))
            with open(cli.DENY_LIST_PATH, "w") as f:
                f.write("# unit deny list\n*roster*\n\n")
            self.assertTrue(cli._is_sensitive_canon(target))
            self.assertFalse(cli._is_sensitive_canon(os.path.join(ws, "notes.txt")))
            with open(cli.DENY_LIST_PATH, "w") as f:
                f.write("*notes*\n")
            st = os.stat(cli.DENY_LIST_PATH)
            os.utime(cli.DENY_LIST_PATH, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
            self.assertFalse(cli._is_sensitive_canon(target))
            self.assertTrue(cli._is_sensitive_canon(os.path.join(ws, "notes.txt")))
            os.remove(cli.DENY_LIST_PATH)
            self.assertFalse(cli._is_sensitive_canon(os.path.join(ws, "notes.txt")))


class RegistryTests(unittest.TestCase):
    def _isolated(self):
        return (list(cli.TOOLS), dict(cli.AVAILABLE_FUNCTIONS), dict(cli.TOOL_FRIENDLY_LABELS), dict(cli._TOOL_SCHEMAS))

    def _restore(self, saved):
        cli.TOOLS[:] = saved[0]
        for live, old in zip((cli.AVAILABLE_FUNCTIONS, cli.TOOL_FRIENDLY_LABELS, cli._TOOL_SCHEMAS), saved[1:]):
            live.clear()
            live.update(old)

    def test_schema_is_derived_from_the_function(self):
        saved = self._isolated()
        try:
            @cli.tool("Doing a thing", name="Who.", count="How many.", tags="Labels.", grid="Rows.")
            def demo_tool(name: str, count: int = 1, tags: list[str] | None = None, grid: list[list[str]] | None = None,
                          internal: bool = False) -> str:
                """Does a thing
                across two lines."""
                return "ok"

            fn = cli.TOOLS[-1]["function"]
            self.assertEqual(fn["name"], "demo_tool")
            self.assertEqual(fn["description"], "Does a thing across two lines.")
            self.assertEqual(fn["parameters"]["required"], ["name"])
            props = fn["parameters"]["properties"]
            self.assertEqual(props["name"], {"type": "string", "description": "Who."})
            self.assertEqual(props["count"]["type"], "integer")
            self.assertEqual(props["tags"], {"type": "array", "items": {"type": "string"}, "description": "Labels."})
            self.assertEqual(props["grid"]["items"], {"type": "array", "items": {"type": "string"}})
            self.assertNotIn("internal", props)
            self.assertEqual(cli._parse_tool_args("demo_tool", '{"name": "a", "internal": true}')[0], None)
            self.assertEqual(cli.TOOL_FRIENDLY_LABELS["demo_tool"], "Doing a thing")
        finally:
            self._restore(saved)

    def test_describing_a_parameter_that_does_not_exist_fails_at_import(self):
        saved = self._isolated()
        try:
            with self.assertRaises(TypeError):
                @cli.tool("x", nope="typo")
                def demo_tool2(path: str) -> str:
                    """d"""
        finally:
            self._restore(saved)


if __name__ == "__main__":
    unittest.main()
