"""Adversarial path spellings: every way of naming a protected file must be refused by every tool that takes
a path, before any approval prompt, and must leave the file untouched.

Two targets: one protected by location (inside an Eustace home) and one by name (.env in the workspace).
The Windows-only spellings (case, trailing dots, 8.3 names, device prefixes, streams, junctions) run on CI.
"""
import ctypes
import os
import subprocess
import unittest

from _harness import cli, RecordingApproval, sandbox

WINDOWS = os.name == "nt"
SECRET = "TOP-SECRET-MARKER"


def _short_path(path: str) -> str | None:
    """The 8.3 spelling of a path, if the volume has one that differs."""
    if not WINDOWS:
        return None
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(path, buf, len(buf))
    return buf.value if 0 < n < len(buf) and buf.value.lower() != path.lower() else None


def spellings(path: str, cwd: str) -> list[str]:
    folder, name = os.path.split(path)
    out = [
        path,
        os.path.relpath(path, cwd),
        os.path.join(folder, ".", name),
        os.path.join(folder, "..", os.path.basename(folder), name),
        os.path.join(folder, "nonexistent", "..", name),
    ]
    if WINDOWS:
        out += [
            path.upper(),
            path.swapcase(),
            path.replace("\\", "/"),
            path + ".",
            path + " ",
            path + ". . ",
            path[0].lower() + path[1:],
            "\\\\?\\" + path,
            "\\\\.\\" + path,
            "\\\\localhost\\" + path[0] + "$" + path[2:],
            path + "::$DATA",
            path + ":stream",
        ]
        short = _short_path(path)
        if short:
            out.append(short)
    return out


class ProtectedPathSpellingTests(unittest.TestCase):
    def setUp(self):
        self.approval = RecordingApproval(default=True)  # approve everything: only the guards may stop a call
        self.ctx = sandbox(approval=self.approval)
        self.ws, self.home = self.ctx.__enter__()
        self.saved_homes = cli.PROTECTED_HOMES
        cli.PROTECTED_HOMES = [os.path.normcase(self.home)]
        self.by_location = os.path.join(self.home, "data.txt")
        self.by_name = os.path.join(self.ws, ".env")
        for p in (self.by_location, self.by_name):
            with open(p, "w") as f:
                f.write(SECRET)

    def tearDown(self):
        cli.PROTECTED_HOMES = self.saved_homes
        self.ctx.__exit__(None, None, None)

    def _assert_refused(self, result: str, spelling: str, tool: str):
        self.assertTrue(result.startswith("Security Error"), f"{tool}({spelling!r}) returned: {result[:120]}")
        self.assertNotIn(SECRET, result)

    def test_every_tool_refuses_every_spelling(self):
        tools = {
            "read_file": lambda p: cli.read_file(p),
            "write_file": lambda p: cli.write_file(p, "overwritten"),
            "edit_file": lambda p: cli.edit_file(p, SECRET, "edited"),
            "open_file": lambda p: cli.open_file(p),
            "write_pptx": lambda p: cli.write_pptx(p, [{"title": "x"}]),
            "edit_pptx": lambda p: cli.edit_pptx(p, [{"op": "delete_slide", "slide": 1}]),
            "write_pptx(template)": lambda p: cli.write_pptx(os.path.join(self.ws, "o.pptx"), [{"title": "x"}], template_path=p),
            "write_pptx(image)": lambda p: cli.write_pptx(os.path.join(self.ws, "o.pptx"), [{"image_path": p}]),
            "fill_docx_template(template)": lambda p: cli.fill_docx_template(p, os.path.join(self.ws, "out.docx"), {}),
        }
        for target in (self.by_location, self.by_name):
            for spelling in spellings(target, self.ws):
                for tool, run in tools.items():
                    with self.subTest(tool=tool, spelling=spelling):
                        self._assert_refused(run(spelling), spelling, tool)
            with open(target) as f:
                self.assertEqual(f.read(), SECRET)
        self.assertEqual(self.approval.calls, [], "a guard must refuse before the user is ever asked")

    def test_tools_that_add_an_extension_cannot_write_into_a_protected_folder(self):
        base = os.path.join(self.home, "report")
        tools = {
            "write_docx": lambda p: cli.write_docx(p, "body"),
            "update_excel": lambda p: cli.update_excel(p, append_rows=[["a"]]),
            "write_pptx": lambda p: cli.write_pptx(p, [{"title": "x"}]),
            "edit_pptx": lambda p: cli.edit_pptx(p, [{"op": "delete_slide", "slide": 1}]),
            "fill_docx_template(output)": lambda p: cli.fill_docx_template(os.path.join(self.ws, "t.docx"), p, {}),
        }
        for spelling in spellings(base, self.ws):
            for tool, run in tools.items():
                with self.subTest(tool=tool, spelling=spelling):
                    self._assert_refused(run(spelling), spelling, tool)
        self.assertEqual(sorted(os.listdir(self.home)), ["data.txt"])
        self.assertEqual(self.approval.calls, [])

    def test_listing_and_search_do_not_reveal_protected_content(self):
        for spelling in spellings(self.home, self.ws):
            with self.subTest(spelling=spelling):
                self._assert_refused(cli.list_directory(spelling), spelling, "list_directory")
                self._assert_refused(cli.search_files(spelling, query="SECRET"), spelling, "search_files")
        listing = cli.list_directory(self.ws)
        self.assertIn(".env (protected: contents not readable)", listing)
        self.assertNotIn(SECRET, cli.search_files(self.ws, query="SECRET"))
        self.assertNotIn(".env", cli.search_files(self.ws, file_pattern="*env*"))

    @unittest.skipUnless(WINDOWS, "junctions are an NTFS feature")
    def test_junction_into_a_protected_folder(self):
        link = os.path.join(self.ws, "shortcut")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, self.home], capture_output=True).returncode == 0
        if not made:
            self.skipTest("could not create a junction")
        try:
            via = os.path.join(link, "data.txt")
            for tool, result in (
                ("read_file", cli.read_file(via)),
                ("write_file", cli.write_file(via, "x")),
                ("edit_file", cli.edit_file(via, SECRET, "x")),
                ("write_file(new)", cli.write_file(os.path.join(link, "new.txt"), "x")),
                ("write_docx", cli.write_docx(os.path.join(link, "new"), "x")),
            ):
                with self.subTest(tool=tool):
                    self._assert_refused(result, via, tool)
            self.assertNotIn(SECRET, cli.search_files(self.ws, query="SECRET"))
            self.assertIn("[LINK] shortcut", cli.list_directory(self.ws))
            self.assertEqual(sorted(os.listdir(self.home)), ["data.txt"])
        finally:
            os.rmdir(link)

    @unittest.skipUnless(WINDOWS, "symlink-swap race is exercised on NTFS")
    def test_target_swapped_for_a_junction_during_approval(self):
        """The user approves a write to an ordinary folder; before it lands, that folder becomes a junction
        into a protected one. The post-approval recheck must catch it."""
        victim = os.path.join(self.ws, "outdir")
        os.mkdir(victim)
        test = self

        class Swap(RecordingApproval):
            def request_approval(self, *a, **k):
                os.rmdir(victim)
                made = subprocess.run(["cmd", "/c", "mklink", "/J", victim, test.home], capture_output=True).returncode == 0
                if not made:
                    test.skipTest("could not create a junction")
                return True

        cli.APPROVAL_HANDLER = Swap()
        try:
            result = cli.write_file(os.path.join(victim, "planted.txt"), "x")
            self.assertTrue(result.startswith("Security Error"), result)
            self.assertEqual(sorted(os.listdir(self.home)), ["data.txt"])
        finally:
            if os.path.lexists(victim):
                os.rmdir(victim)


if __name__ == "__main__":
    unittest.main()
