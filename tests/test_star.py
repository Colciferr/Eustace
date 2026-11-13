"""The star: the font that carries it, the thinking animation, and installing the font."""
import contextlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from rich.cells import cell_len

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, keys, output

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "fonts", cli.STAR_FONT_FILE)
ENTER = "\r"
GLYPHS = [chr(cli.STAR_BASE + i) for i in range(len(cli.STAR_WIDTHS) * len(cli.STAR_HEIGHTS))]


def builder():
    spec = importlib.util.spec_from_file_location("build_star_font", os.path.join(ROOT, "tools", "build_star_font.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # fontTools is only imported inside build(), so this needs nothing extra
    return module


class Ttf:
    """Just enough of a TrueType reader to check the committed font without fontTools (a dev-only dependency)."""

    def __init__(self, path):
        with open(path, "rb") as f:
            self.data = f.read()
        count = struct.unpack(">H", self.data[4:6])[0]
        self.tables = {}
        for i in range(count):
            tag, _, offset, length = struct.unpack(">4sIII", self.data[12 + 16 * i:28 + 16 * i])
            self.tables[tag.decode("latin-1")] = self.data[offset:offset + length]

    def cmap(self) -> dict:
        """codepoint -> glyph index, from the Windows full-repertoire (format 12) subtable."""
        table = self.tables["cmap"]
        for i in range(struct.unpack(">H", table[2:4])[0]):
            platform, encoding, offset = struct.unpack(">HHI", table[4 + 8 * i:12 + 8 * i])
            if (platform, encoding) == (3, 10) and struct.unpack(">H", table[offset:offset + 2])[0] == 12:
                groups = struct.unpack(">I", table[offset + 12:offset + 16])[0]
                out = {}
                for g in range(groups):
                    start, end, glyph = struct.unpack(">III", table[offset + 16 + 12 * g:offset + 28 + 12 * g])
                    out.update({cp: glyph + cp - start for cp in range(start, end + 1)})
                return out
        raise AssertionError("no format 12 cmap")

    def names(self) -> dict:
        """name ID -> text, Windows English records."""
        table = self.tables["name"]
        count, strings = struct.unpack(">HH", table[2:6])
        out = {}
        for i in range(count):
            platform, _, language, name_id, length, offset = struct.unpack(">HHHHHH", table[6 + 12 * i:18 + 12 * i])
            if platform == 3 and language == 0x409:
                out[name_id] = table[strings + offset:strings + offset + length].decode("utf-16-be")
        return out

    def advance(self, glyph: int) -> int:
        metrics = struct.unpack(">H", self.tables["hhea"][34:36])[0]
        return struct.unpack(">H", self.tables["hmtx"][4 * min(glyph, metrics - 1):][:2])[0]

    def box(self, glyph: int):
        """(xMin, yMin, xMax, yMax), or None for a glyph with no outline."""
        long_offsets = struct.unpack(">h", self.tables["head"][50:52])[0]
        loca = self.tables["loca"]
        if long_offsets:
            start, end = struct.unpack(">II", loca[4 * glyph:4 * glyph + 8])
        else:
            start, end = (2 * v for v in struct.unpack(">HH", loca[2 * glyph:2 * glyph + 4]))
        return struct.unpack(">hhhh", self.tables["glyf"][start + 2:start + 10]) if end > start else None

    ascent = property(lambda self: struct.unpack(">h", self.tables["hhea"][4:6])[0])
    panose = property(lambda self: tuple(self.tables["OS/2"][32:42]))
    fixed_pitch = property(lambda self: struct.unpack(">I", self.tables["post"][12:16])[0])


@contextlib.contextmanager
def star_font(mode=None):
    """A sandbox whose console reports the Eustace Mono font and whose input box is usable."""
    with sandbox() as (ws, home), keys() as pipe:
        cli._console_font_face = lambda: cli.STAR_FONT_FAMILY
        if mode is not None:
            with open(cli.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"star": mode}, f)
        yield pipe


LIVE = [False, True]  # Rich hides and restores the cursor itself while a reply streams in


def cursor_log():
    """Replaces console.show_cursor with a recorder; returns the list it appends to."""
    calls = []
    cli.console.show_cursor = lambda show=True: calls.append(show)
    return calls


class FontFileTests(unittest.TestCase):
    """The committed font, the constants in cli.py and the build script must describe the same sixteen glyphs."""

    def test_cli_and_the_build_script_agree(self):
        b = builder()
        for name in ("STAR_BASE", "STAR_WIDTHS", "STAR_HEIGHTS", "STAR_FONT_FAMILY", "STAR_FONT_FILE"):
            self.assertEqual(getattr(cli, name), getattr(b, name), name)

    def test_every_glyph_is_in_the_font_one_cell_wide_and_inside_the_line(self):
        font, b = Ttf(FONT), builder()
        cmap = font.cmap()
        cell = font.advance(cmap[ord("A")])
        px = cell // b.PIXELS_PER_CELL
        seen = set()
        for i, ch in enumerate(GLYPHS):
            self.assertIn(ord(ch), cmap, hex(ord(ch)))
            glyph = cmap[ord(ch)]
            seen.add(glyph)
            self.assertEqual(font.advance(glyph), cell, "a different advance would make the console treat it as wide")
            x0, y0, x1, y1 = font.box(glyph)
            width, height = cli.STAR_WIDTHS[i // len(cli.STAR_HEIGHTS)], cli.STAR_HEIGHTS[i % len(cli.STAR_HEIGHTS)]
            self.assertEqual((x1 - x0, y0), (width * px, height * px), hex(ord(ch)))
            self.assertEqual(y1 - y0, len(b.STAR) * px)
            self.assertEqual(x0 + x1, cell, "centred in the cell")
            self.assertLessEqual(y1, font.ascent, "the top of the bounce would be clipped")
        self.assertEqual(len(seen), len(GLYPHS), "two frames share a glyph")

    def test_the_font_is_renamed_as_its_license_requires(self):
        names = Ttf(FONT).names()
        self.assertEqual(names[1], cli.STAR_FONT_FAMILY)
        self.assertEqual(names[4], cli.STAR_FONT_FAMILY)
        for name_id in (1, 2, 4, 6, 16, 17):
            self.assertNotIn("cascadia", names.get(name_id, "").lower(), name_id)
        self.assertIn("Open Font License", names[13])
        with open(os.path.join(ROOT, "fonts", "OFL.txt"), encoding="utf-8") as f:
            license_text = f.read()
        self.assertIn("Reserved Font Name Cascadia Code", license_text)
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", license_text)

    def test_the_font_is_classified_as_monospaced(self):
        # the classification the build script copies from the variable Cascadia Mono
        font, b = Ttf(FONT), builder()
        self.assertEqual(font.panose, b.PANOSE)
        self.assertEqual(font.panose[3], 9, "PANOSE proportion 9 is 'monospaced'")
        self.assertTrue(font.fixed_pitch)

    def test_the_rest_of_the_font_is_untouched(self):
        cmap = Ttf(FONT).cmap()
        missing = [ch for ch in "Aa0>·—•…↑↓│─╭╰●✓❯" if ord(ch) not in cmap]  # symbols Eustace prints
        self.assertEqual(missing, [])
        private = [cp for cp in cmap if 0xE000 <= cp <= 0xF8FF]
        self.assertEqual(sorted(private), [ord(g) for g in GLYPHS])

    def test_the_font_and_license_are_installed_with_the_package(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            project = f.read()
        self.assertIn(f'"fonts/{cli.STAR_FONT_FILE}"', project)
        self.assertIn('"fonts/OFL.txt"', project)
        self.assertEqual(os.path.basename(cli.SHIPPED_FONT_PATH), cli.STAR_FONT_FILE)

    def test_squeezed_frames_stay_symmetric_and_keep_their_height(self):
        b = builder()
        for width in b.STAR_WIDTHS:
            rows = b.squeeze(b.STAR, width)
            self.assertEqual([len(r) for r in rows], [width] * len(b.STAR))
            self.assertTrue(all(r == r[::-1] for r in rows), rows)
            self.assertTrue(all("#" in r for r in rows), "a row vanished")
            self.assertTrue(all(any(r[c] == "#" for r in rows) for c in range(width)), "an empty column")

    def test_outline_traces_each_shape_once_without_inner_edges(self):
        b = builder()
        self.assertEqual(b.outline(["#"]), [[(0, 0), (0, 1), (1, 1), (1, 0)]])
        self.assertEqual(b.outline(["##", "##"]), [[(0, 0), (0, 2), (2, 2), (2, 0)]])
        ring = b.outline(["###", "#.#", "###"])
        self.assertEqual(len(ring), 2)  # outside and hole
        self.assertEqual(len(b.outline(["#.", ".#"])), 2)  # corner-touching pixels stay two simple contours
        for width in b.STAR_WIDTHS:
            for contour in b.outline(b.squeeze(b.STAR, width)):
                self.assertEqual(len(contour), len(set(contour)), "a contour passes through a point twice")
                area = sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(contour, contour[1:] + contour[:1])) / 2
                self.assertLess(area, 0, "filled shapes must run clockwise")
        filled = sum(row.count("#") for row in b.STAR)
        total = sum(-sum(x0 * y1 - x1 * y0 for (x0, y0), (x1, y1) in zip(c, c[1:] + c[:1])) / 2 for c in b.outline(list(b.STAR)))
        self.assertEqual(total, filled)  # the outline encloses exactly the filled pixels


class FrameTests(unittest.TestCase):
    def test_frames_stay_inside_the_sixteen_glyphs_and_start_from_the_idle_star(self):
        self.assertEqual(cli.star_frame(0), cli.STAR_IDLE)
        self.assertEqual(cli.STAR_IDLE, GLYPHS[0])
        for n in range(200):
            self.assertIn(cli.star_frame(n), GLYPHS)

    def test_the_loop_closes_and_every_frame_moves(self):
        loop = len(cli._STAR_SPIN) * len(cli._STAR_BOUNCE) // 2  # lcm(6, 8)
        frames = [cli.star_frame(n) for n in range(loop)]
        self.assertEqual([cli.star_frame(n) for n in range(loop, 2 * loop)], frames)
        for a, b in zip(frames, frames[1:] + frames[:1]):
            self.assertNotEqual(a, b)
        self.assertEqual(max(cli._STAR_SPIN), len(cli.STAR_WIDTHS) - 1)
        self.assertEqual(max(cli._STAR_BOUNCE), len(cli.STAR_HEIGHTS) - 1)
        for steps in (cli._STAR_SPIN, cli._STAR_BOUNCE):  # no jumps: each step is one size or one height
            self.assertTrue(all(abs(a - b) <= 1 for a, b in zip(steps, steps[1:] + steps[:1])))

    def test_every_glyph_takes_one_cell_in_both_width_tables(self):
        import wcwidth
        for ch in GLYPHS:
            self.assertEqual(cell_len(ch), 1)        # Rich: the status line and the transcript
            self.assertEqual(wcwidth.wcwidth(ch), 1)  # prompt_toolkit: the input box


class WhenTheStarShowsTests(unittest.TestCase):
    def test_default_is_auto_and_bad_values_fall_back_to_it(self):
        with sandbox():
            self.assertEqual(cli.get_star_mode(), "auto")
            for raw, want in ((" ON ", "on"), ("Off", "off"), ("auto", "auto"), ("yes", "auto"), (True, "auto"),
                              (1, "auto"), (None, "auto"), (["on"], "auto")):
                with open(cli.CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump({"star": raw}, f)
                self.assertEqual(cli.get_star_mode(), want, raw)

    def test_auto_needs_the_font_in_a_console_whose_font_can_be_read(self):
        with star_font():
            self.assertTrue(cli._refresh_star())
            cli._console_font_face = lambda: "Consolas"
            self.assertFalse(cli._refresh_star())
            self.assertIn("Consolas", cli._star_reason()[1])
            self.assertIn("--install-font", cli._star_reason()[1])
            cli._console_font_face = lambda: ""
            self.assertFalse(cli._refresh_star())
            cli._console_font_face = lambda: cli.STAR_FONT_FAMILY
            cli._console_is_hosted = lambda: True
            self.assertFalse(cli._refresh_star(), "a hosted terminal's reported font is not the one on screen")
            self.assertIn('"star": "on"', cli._star_reason()[1])

    def test_on_and_off_override_the_font_check(self):
        with star_font("on"):
            cli._console_font_face = lambda: "Consolas"
            cli._console_is_hosted = lambda: True
            self.assertTrue(cli._refresh_star())
        with star_font("off"):
            self.assertFalse(cli._refresh_star())

    def test_never_with_the_plain_prompt(self):
        with star_font("on"):
            cli._ui_enabled = lambda: False
            self.assertFalse(cli._refresh_star())

    def test_hosted_terminals_are_recognized_by_their_environment(self):
        real = cli._console_is_hosted
        with sandbox():
            for var in ("WT_SESSION", "TERM_PROGRAM", "ConEmuPID"):
                with mock.patch.dict(os.environ, {var: "x"}):
                    self.assertTrue(real(), var)

    def test_only_the_classic_console_window_counts_as_not_hosted(self):
        real = cli._console_is_hosted
        clean = {k: v for k, v in os.environ.items() if k not in ("WT_SESSION", "TERM_PROGRAM", "ConEmuPID")}
        with sandbox(), mock.patch.dict(os.environ, clean, clear=True):
            # "PseudoConsoleWindow" is what a GitHub runner's console reported, with IsWindowVisible true
            for window_class, hosted in (("ConsoleWindowClass", False), ("PseudoConsoleWindow", True), ("", True),
                                         ("CASCADIA_HOSTING_WINDOW_CLASS", True), ("consolewindowclass", True)):
                with mock.patch.object(cli, "_console_window_class", lambda: window_class):
                    self.assertEqual(real(), hosted, window_class)

    def test_the_window_class_is_empty_when_it_cannot_be_read(self):
        with mock.patch.object(cli, "_kernel32") as k32, mock.patch.object(cli, "_user32") as u32:
            k32.GetConsoleWindow.return_value = None
            self.assertEqual(cli._console_window_class(), "")
            k32.GetConsoleWindow.return_value = 1234
            u32.GetClassNameW.return_value = 0
            self.assertEqual(cli._console_window_class(), "")
            k32.GetConsoleWindow.side_effect = OSError("no console")
            self.assertEqual(cli._console_window_class(), "")

    def test_the_accent_is_the_banner_border_color(self):
        saved = cli.DEV_MODE
        try:
            with sandbox():
                for elevated, dev, color in ((False, False, "cyan"), (False, True, "yellow"), (True, False, "red"),
                                             (True, True, "red")):
                    cli.ELEVATED, cli.DEV_MODE = elevated, dev
                    self.assertEqual(cli._accent(), color)
        finally:
            cli.DEV_MODE = saved


class PromptTests(unittest.TestCase):
    def test_the_prompt_and_the_transcript_keep_the_arrow_and_never_show_the_star(self):
        import inspect
        self.assertIn('[("class:prompt", "> ")]', inspect.getsource(cli._prompt_box))
        self.assertFalse(any(name.startswith("star") for name in cli._UI_STYLE))
        with star_font() as pipe:
            pipe.send_text("what changed?" + ENTER)
            self.assertEqual(cli._read_user_input([{"role": "system", "content": "s"}]), "what changed?")
            self.assertTrue(cli._STAR["on"])
            self.assertIn("> what changed?", output())
            self.assertFalse(any(g in output() for g in GLYPHS))

    def test_a_font_change_is_noticed_at_the_next_message(self):
        with star_font() as pipe:
            pipe.send_text("one" + ENTER + "two" + ENTER)
            cli._read_user_input([])
            self.assertTrue(cli._STAR["on"])
            cli._console_font_face = lambda: "Consolas"
            cli._read_user_input([])
            self.assertFalse(cli._STAR["on"])

    def test_status_says_whether_the_star_is_on_and_why(self):
        with star_font():
            cli._cmd_status({"messages": []}, "")
            self.assertIn("Star:", output())
            self.assertIn(f"font is {cli.STAR_FONT_FAMILY}", " ".join(output().split()))


class ThinkingLineTests(unittest.TestCase):
    MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}]

    def turn(self, *rounds, width=120):
        """Runs one turn with the star on; returns (everything printed, cursor calls)."""
        with star_font():
            cli.client = ScriptedClient(*rounds)
            cli.APPROVAL_HANDLER = RecordingApproval()
            cli.console = cli.Console(file=io.StringIO(), width=width, force_terminal=True, color_system="standard")
            cursor = cursor_log()
            cli._refresh_star()
            cli.run_agent_turn([dict(m) for m in self.MESSAGES])
            self.assertIsNone(cli._STAR["text"])
            return output(), cursor

    @staticmethod
    def slow(seconds, *chunks):
        def stream():
            time.sleep(seconds)
            yield from chunks
        return stream

    def test_without_the_star_the_line_is_unchanged(self):
        with sandbox(client=ScriptedClient([text("ok")]), approval=RecordingApproval()):
            cursor = cursor_log()
            cli.run_agent_turn([dict(m) for m in self.MESSAGES])
            out = output()
        self.assertIn("  Thinking... (press ESC or Ctrl+C to stop)\r", out)
        self.assertFalse(any(g in out for g in GLYPHS))
        self.assertEqual(cursor, LIVE)
        with sandbox(client=ScriptedClient(RuntimeError("boom")), approval=RecordingApproval()):
            cursor = cursor_log()
            cli.run_agent_turn([dict(m) for m in self.MESSAGES])
            self.assertEqual(cursor, [], "the plain line never touches the cursor")

    def test_the_star_sits_where_the_t_was_and_the_text_moves_right(self):
        out, cursor = self.turn([text("ok")])
        self.assertIn(f"  \x1b[36m{cli.STAR_IDLE}\x1b[0m\x1b[2;36m Thinking... (press ESC or Ctrl+C to stop)\x1b[0m\r", out)
        self.assertEqual(cursor, [False, True] + LIVE)

    def test_it_animates_in_order_while_waiting_and_stops_at_the_first_chunk(self):
        out, cursor = self.turn(self.slow(0.55, text("done")))
        shown = [ch for ch in out if ch in GLYPHS]
        self.assertGreaterEqual(len(shown), 4, "several frames in half a second")
        self.assertLessEqual(len(shown), 8, "about one frame per 100 ms, not one per poll")
        self.assertEqual(shown, [cli.star_frame(n) for n in range(len(shown))])
        self.assertLess(out.rindex(shown[-1]), out.index("done"))
        self.assertEqual(cursor, [False, True] + LIVE)

    def test_each_frame_overwrites_the_last_instead_of_scrolling(self):
        out, _ = self.turn(self.slow(0.35, text("done")), width=24)
        waiting = out[:out.index("done")]
        frames = [line for line in waiting.split("\r") if any(g in line for g in GLYPHS)]
        self.assertGreaterEqual(len(frames), 3)
        for frame in frames:
            self.assertNotIn("\n", frame, "a wrapped status line scrolls the window on every frame")
            self.assertLessEqual(cell_len(cli.Text.from_ansi(frame).plain), 23)

    def test_escape_while_waiting_gives_the_cursor_back(self):
        with star_font():
            cli.client = ScriptedClient(self.slow(5, text("never")))
            cli.console = cli.Console(file=io.StringIO(), width=120)
            cursor = cursor_log()
            cli._refresh_star()
            polls = {"n": 0}

            def abort():
                polls["n"] += 1
                if polls["n"] > 4:
                    raise KeyboardInterrupt("Aborted via ESC key")
            cli.check_user_abort = abort
            messages = [dict(m) for m in self.MESSAGES]
            cli.run_agent_turn(messages)
            self.assertIn("Stopped by user", output())
            self.assertIsNone(cli._STAR["text"])
            self.assertEqual(cursor, [False, True])

    def test_an_api_error_gives_the_cursor_back(self):
        out, cursor = self.turn(RuntimeError("boom"))
        self.assertIn("boom", out)
        self.assertEqual(cursor, [False, True])

    def test_a_failure_before_the_stream_starts_still_gives_the_cursor_back(self):
        with star_font():
            cli.console = cli.Console(file=io.StringIO(), width=120)
            cursor = cursor_log()
            cli._refresh_star()
            with mock.patch.object(cli, "_StreamWorker", side_effect=RuntimeError("can't start new thread")):
                cli.run_agent_turn([dict(m) for m in self.MESSAGES])
            self.assertIn("can't start new thread", output())
            self.assertIsNone(cli._STAR["text"])
            self.assertEqual(cursor, [False, True])

    def test_each_round_of_a_turn_starts_again_from_the_idle_star(self):
        from _harness import call
        out, cursor = self.turn([call("list_directory", "{}", id="c1")], [text("ok")])
        self.assertEqual([ch for ch in out if ch in GLYPHS], [cli.STAR_IDLE, cli.STAR_IDLE])
        self.assertEqual(cursor, [False, True] + [False, True] + LIVE)

    def test_the_color_follows_dev_mode_and_elevation(self):
        saved = cli.DEV_MODE
        try:
            with star_font():
                cli.console = cli.Console(file=io.StringIO(), width=120, force_terminal=True, color_system="standard")
                cli._refresh_star()
                for elevated, dev, code in ((False, True, "33"), (True, False, "31")):
                    cli.ELEVATED, cli.DEV_MODE = elevated, dev
                    cli._show_status_line("Thinking...")
                    cli._clear_status_line()
                    self.assertIn(f"\x1b[{code}m{cli.STAR_IDLE}\x1b[0m\x1b[2;36m Thinking...", output())
        finally:
            cli.DEV_MODE = saved

    def test_compacting_uses_the_same_line(self):
        with star_font():
            cli.client = ScriptedClient(self.slow(0.25, text("summary")))
            cursor = cursor_log()
            cli._refresh_star()
            history = self.MESSAGES + [{"role": "assistant", "content": "hi"}]
            cli.compact_messages([dict(m) for m in history])
            self.assertIn(f"{cli.STAR_IDLE} Compacting conversation...", output())
            self.assertIsNone(cli._STAR["text"])
            self.assertEqual(cursor, [False, True])

    def test_ticks_do_nothing_when_no_status_line_is_showing(self):
        with star_font():
            cli._refresh_star()
            cli._star_tick()
            cli._clear_status_line()
            cli._abandon_status_line()
            self.assertFalse(any(g in output() for g in GLYPHS))

    def test_exit_with_the_line_still_showing_restores_the_cursor(self):
        with star_font():
            cursor = cursor_log()
            cli._refresh_star()
            cli._show_status_line("Thinking...")
            cli._abandon_status_line()  # registered with atexit
            self.assertEqual(cursor, [False, True])
            cli._abandon_status_line()
            self.assertEqual(cursor, [False, True])


class FakeRegistry:
    """Stands in for winreg: one key, values in a dict."""
    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

    def __init__(self, fail=None):
        self.values, self.fail, self.opened = {}, fail, []

    @contextlib.contextmanager
    def _key(self, root, path):
        if self.fail:
            raise self.fail
        self.opened.append((root, path))
        yield self

    def CreateKeyEx(self, root, path, reserved, access):
        return self._key(root, path)

    def OpenKey(self, root, path, reserved, access):
        return self._key(root, path)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (kind, value)

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class InstallTests(unittest.TestCase):
    @contextlib.contextmanager
    def machine(self, registry=None, loaded=1):
        """A fake user profile: LOCALAPPDATA in a temp folder, a fake registry, and GDI calls recorded."""
        registry = registry or FakeRegistry()
        announced = []
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                mock.patch.object(cli, "winreg", registry), \
                mock.patch.object(cli, "_announce_font", lambda path, add: announced.append((path, add)) or loaded), \
                mock.patch.object(cli, "SHIPPED_FONT_PATH", FONT), \
                contextlib.redirect_stdout(io.StringIO()) as printed:
            yield os.path.join(tmp, "Microsoft", "Windows", "Fonts", cli.STAR_FONT_FILE), registry, announced, printed

    def test_install_copies_registers_for_the_user_only_and_loads(self):
        with self.machine() as (dest, registry, announced, printed):
            self.assertEqual(cli._install_star_font(), 0)
            with open(dest, "rb") as a, open(FONT, "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertEqual(registry.values, {"Eustace Mono (TrueType)": (registry.REG_SZ, dest)})
            self.assertEqual(registry.opened, [("HKCU", r"Software\Microsoft\Windows NT\CurrentVersion\Fonts")])
            self.assertEqual(announced, [(dest, True)])
            self.assertIn("Properties > Font > Eustace Mono", printed.getvalue())
            self.assertIn("--remove-font", printed.getvalue())
            self.assertNotIn("[!!]", printed.getvalue())

    def test_installing_again_does_not_rewrite_a_font_that_is_in_use(self):
        with self.machine() as (dest, registry, announced, printed):
            cli._install_star_font()
            with mock.patch.object(cli.shutil, "copyfile", side_effect=PermissionError("in use")):
                self.assertEqual(cli._install_star_font(), 0)

    def test_an_older_copy_is_replaced(self):
        with self.machine() as (dest, registry, announced, printed):
            os.makedirs(os.path.dirname(dest))
            with open(dest, "wb") as f:
                f.write(b"old font")
            self.assertEqual(cli._install_star_font(), 0)
            self.assertEqual(os.path.getsize(dest), os.path.getsize(FONT))

    def test_a_copy_that_fails_stops_before_the_registry(self):
        with self.machine() as (dest, registry, announced, printed):
            with mock.patch.object(cli.shutil, "copyfile", side_effect=PermissionError("in use")):
                self.assertEqual(cli._install_star_font(), 1)
            self.assertEqual((registry.values, announced), ({}, []))
            self.assertIn("close it", printed.getvalue())

    def test_a_registry_refusal_is_reported(self):
        with self.machine(FakeRegistry(fail=PermissionError("denied by policy"))) as (dest, registry, announced, printed):
            self.assertEqual(cli._install_star_font(), 1)
            self.assertEqual(announced, [])
            self.assertIn("denied by policy", printed.getvalue())

    def test_windows_refusing_to_load_the_font_is_said_plainly(self):
        with self.machine(loaded=0) as (dest, registry, announced, printed):
            self.assertEqual(cli._install_star_font(), 0)
            self.assertIn("did not load it", printed.getvalue())

    def test_a_missing_font_file_is_an_error(self):
        with self.machine() as (dest, registry, announced, printed):
            with mock.patch.object(cli, "_star_font_source", lambda: None):
                self.assertEqual(cli._install_star_font(), 1)
            self.assertFalse(os.path.exists(dest))

    def test_the_source_checkout_copy_is_found_when_nothing_is_installed(self):
        with mock.patch.object(cli, "SHIPPED_FONT_PATH", os.path.join(ROOT, "nowhere", "x.ttf")):
            self.assertEqual(os.path.normcase(cli._star_font_source()), os.path.normcase(FONT))

    def test_setup_and_updates_install_once_and_then_stay_silent(self):
        with self.machine() as (dest, registry, announced, printed):
            self.assertEqual(cli._install_star_font(brief=True), 0)
            self.assertTrue(os.path.isfile(dest))
            self.assertEqual(registry.values, {"Eustace Mono (TrueType)": (registry.REG_SZ, dest)})
            first = printed.getvalue()
            self.assertIn("Properties > Font > Eustace Mono", first)
            self.assertLessEqual(first.count("\n"), 2)
            self.assertEqual(cli._install_star_font(brief=True), 0)
            self.assertEqual(printed.getvalue(), first, "nothing more is printed once the font is in place")

    def test_star_off_keeps_setup_and_updates_from_installing_but_not_the_explicit_flag(self):
        with self.machine() as (dest, registry, announced, printed), tempfile.TemporaryDirectory() as home, \
                mock.patch.object(cli, "_UPDATE_HOME", home):
            for off in ("off", " OFF "):
                with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
                    json.dump({"star": off}, f)
                self.assertEqual(cli._install_star_font(brief=True), 0)
                self.assertFalse(os.path.exists(dest))
                self.assertEqual((registry.values, announced, printed.getvalue()), ({}, [], ""))
            self.assertEqual(cli._install_star_font(), 0)
            self.assertTrue(os.path.isfile(dest))

    def test_anything_but_off_does_not_opt_out(self):
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "_UPDATE_HOME", home):
            self.assertFalse(cli._star_opted_out())  # no config at all
            for content in ('{"star": "auto"}', '{"star": "on"}', '{"star": false}', '{}', '[]', 'not json'):
                with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertFalse(cli._star_opted_out(), content)

    def test_an_update_installs_the_font_and_survives_its_failure(self):
        from _harness import sandbox as box
        with box(), contextlib.redirect_stdout(io.StringIO()) as printed:
            calls = []
            cli._install_star_font = lambda brief=False: calls.append(brief) or 0
            self.assertEqual(cli._post_update(["1.7.0", "-"]), 0)
            self.assertEqual(calls, [True])

            def broken(brief=False):
                raise OSError("font folder is read-only")
            cli._install_star_font = broken
            self.assertEqual(cli._post_update(["1.7.0", "-"]), 0)
            self.assertIn("font folder is read-only", printed.getvalue())

    def test_setup_installs_the_font_through_the_installed_command(self):
        with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8") as f:
            setup = f.read()
        self.assertIn("--install-font auto", setup)
        self.assertLess(setup.index("pipx install"), setup.index("--install-font auto"))

    def test_the_auto_badge_uses_only_plain_characters(self):
        self.assertTrue(cli.AUTO_BADGE.isascii(), "the arrows used before showed as boxes in the console")
        self.assertIn("AUTO", cli.AUTO_BADGE)

    def test_remove_undoes_install(self):
        with self.machine() as (dest, registry, announced, printed):
            cli._install_star_font()
            self.assertEqual(cli._remove_star_font(), 0)
            self.assertFalse(os.path.exists(dest))
            self.assertEqual(registry.values, {})
            self.assertEqual(announced[-1], (dest, False))

    def test_remove_when_nothing_is_installed(self):
        with self.machine() as (dest, registry, announced, printed):
            self.assertEqual(cli._remove_star_font(), 0)
            self.assertEqual(announced, [])
            self.assertIn("not installed", printed.getvalue())

    def test_remove_with_the_file_still_in_use_says_what_is_left(self):
        with self.machine() as (dest, registry, announced, printed):
            cli._install_star_font()
            with mock.patch.object(cli.os, "remove", side_effect=PermissionError("in use")):
                self.assertEqual(cli._remove_star_font(), 0)
            self.assertEqual(registry.values, {})
            self.assertIn(dest, printed.getvalue())

    def test_the_flags_are_documented_and_never_start_a_session(self):
        cli_py = os.path.join(ROOT, "cli.py")
        shown = subprocess.run([sys.executable, cli_py, "--help"], capture_output=True, text=True, timeout=120)
        self.assertIn("--install-font", shown.stdout)
        self.assertIn("--remove-font", shown.stdout)
        with open(cli_py, encoding="utf-8") as f:
            source = f.read()
        # both are handled where --update is: before the API client, the config or the console exist
        self.assertLess(source.index('if "--install-font" in _ARGS'), source.index("DEV_MODE = "))
        self.assertLess(source.index('if "--remove-font" in _ARGS'), source.index("DEV_MODE = "))


# Runs in a classic console window of its own (started through conhost.exe): installs the font for real, asks Windows (GDI, which the classic console
# draws with) whether it can see the font and the star glyphs, then switches that console to the font and reads
# the face back the way Eustace does.
REAL_CONSOLE_SCRIPT = r'''
import ctypes, json, os, sys
from ctypes import wintypes
sys.argv = ["cli"]
out = {}
try:
    sys.path.insert(0, ROOT)
    import cli
    out["install"] = cli._install_star_font()
    gdi32, user32, k32 = ctypes.WinDLL("gdi32"), ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetDC.restype = wintypes.HDC
    gdi32.CreateFontW.restype = wintypes.HFONT
    gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wintypes.DWORD] * 8 + [wintypes.LPCWSTR]
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetTextFaceW.argtypes = [wintypes.HDC, ctypes.c_int, wintypes.LPWSTR]
    gdi32.GetGlyphIndicesW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.WORD), wintypes.DWORD]
    dc = user32.GetDC(None)
    font = gdi32.CreateFontW(16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 0, 0, cli.STAR_FONT_FAMILY)
    gdi32.SelectObject(dc, font)
    face = ctypes.create_unicode_buffer(64)
    gdi32.GetTextFaceW(dc, 64, face)
    out["gdi_face"] = face.value
    stars = "".join(chr(cli.STAR_BASE + i) for i in range(16))
    indices = (wintypes.WORD * 16)()
    gdi32.GetGlyphIndicesW(dc, stars, 16, indices, 1)  # GGI_MARK_NONEXISTING_GLYPHS: missing ones come back 0xFFFF
    out["gdi_glyphs"] = list(indices)
    sizes = (ctypes.c_int * 16)()
    gdi32.GetCharWidth32W.argtypes = [wintypes.HDC, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_int)]
    gdi32.GetCharWidth32W(dc, cli.STAR_BASE, cli.STAR_BASE + 15, sizes)
    letter = (ctypes.c_int * 1)()
    gdi32.GetCharWidth32W(dc, ord("M"), ord("M"), letter)
    out["gdi_widths"] = [list(sizes), letter[0]]

    class TEXTMETRICW(ctypes.Structure):
        _fields_ = [(n, wintypes.LONG) for n in ("tmHeight", "tmAscent", "tmDescent", "tmInternalLeading",
                    "tmExternalLeading", "tmAveCharWidth", "tmMaxCharWidth", "tmWeight", "tmOverhang",
                    "tmDigitizedAspectX", "tmDigitizedAspectY")] + [(n, wintypes.WCHAR) for n in (
                    "tmFirstChar", "tmLastChar", "tmDefaultChar", "tmBreakChar")] + [(n, wintypes.BYTE) for n in (
                    "tmItalic", "tmUnderlined", "tmStruckOut", "tmPitchAndFamily", "tmCharSet")]
    tm = TEXTMETRICW()
    gdi32.GetTextMetricsW.argtypes = [wintypes.HDC, ctypes.POINTER(TEXTMETRICW)]
    gdi32.GetTextMetricsW(dc, ctypes.byref(tm))
    out["gdi_pitch_and_family"] = tm.tmPitchAndFamily & 0xFF

    out["face_before"] = cli._console_font_face()
    out["hosted"] = cli._console_is_hosted()
    info = cli._ConsoleFontInfoEx()
    info.cbSize = ctypes.sizeof(info)
    handle = cli._kernel32.GetStdHandle(cli._STD_OUTPUT_HANDLE)
    cli._kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info))
    k32.SetCurrentConsoleFontEx.argtypes = [wintypes.HANDLE, wintypes.BOOL, ctypes.POINTER(cli._ConsoleFontInfoEx)]
    out["code_page"] = k32.GetConsoleOutputCP()
    out["window"] = cli._console_window_class()

    def switch_to(face):
        info.FaceName, info.FontFamily, info.FontWeight, info.nFont = face, 0x36, 400, 0
        info.dwFontSize.X, info.dwFontSize.Y = 0, 16
        ok = bool(k32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info)))
        return [ok, cli._console_font_face()]
    out["control"] = switch_to("Courier New")   # a stock font: shows this console can change fonts at all
    out["missing"] = switch_to("No Such Font")  # what the console does with a name it cannot use
    out["set_ok"], out["face_after"] = switch_to(cli.STAR_FONT_FAMILY)
    out["remove"] = cli._remove_star_font()
except BaseException:
    import traceback
    out["error"] = traceback.format_exc()
with open(RESULT, "w", encoding="utf-8") as f:
    json.dump(out, f)
'''


@unittest.skipUnless(os.environ.get("GITHUB_ACTIONS"), "installs a font for the current user; only on a CI runner")
class RealConsoleTests(unittest.TestCase):
    """On a stock Windows runner: a font installed for one user, without admin rights, is usable by the classic
    console, and Eustace reads its name back. (Whether a managed PC's policy allows the same is for that PC to say.)"""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            result = os.path.join(tmp, "result.json")
            script = os.path.join(tmp, "probe.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(f"ROOT = {ROOT!r}\nRESULT = {result!r}\n" + REAL_CONSOLE_SCRIPT)
            # Started plainly, the runner gives the script a pseudo console (window class "PseudoConsoleWindow"),
            # which accepts any font name and changes nothing. conhost.exe opens the classic window.
            conhost = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "conhost.exe")
            subprocess.run([conhost, sys.executable, script], timeout=180)
            for _ in range(120):
                if os.path.exists(result) and os.path.getsize(result):
                    break
                time.sleep(0.5)
            with open(result, encoding="utf-8") as f:
                cls.result = json.load(f)
        print("\nreal console probe:", json.dumps({**cls.result, "gdi_glyphs": len(set(cls.result.get("gdi_glyphs", []))),
                                                   "gdi_widths": str(cls.result.get("gdi_widths"))}))

    def test_the_probe_ran_and_the_install_succeeded(self):
        self.assertNotIn("error", self.result, self.result.get("error"))
        self.assertEqual(self.result["install"], 0)
        self.assertEqual(self.result["remove"], 0)

    def test_windows_resolves_the_font_by_name_and_finds_all_sixteen_stars(self):
        self.assertEqual(self.result["gdi_face"], cli.STAR_FONT_FAMILY)
        self.assertNotIn(0xFFFF, self.result["gdi_glyphs"])
        self.assertEqual(len(set(self.result["gdi_glyphs"])), 16)

    def test_the_stars_are_as_wide_as_a_letter(self):
        stars, letter = self.result["gdi_widths"]
        self.assertEqual(set(stars), {letter})

    def test_the_console_takes_the_font_and_eustace_reads_its_name(self):
        if self.result["window"] != cli._CLASSIC_CONSOLE_WINDOW:
            self.skipTest(f"no classic console on this runner (window class {self.result['window']!r})")
        self.assertFalse(self.result["hosted"])
        self.assertEqual(self.result["control"], [True, "Courier New"], "this console cannot change fonts at all")
        self.assertNotEqual(self.result["missing"][1], "No Such Font")
        self.assertTrue(self.result["set_ok"])
        self.assertEqual(self.result["face_after"], cli.STAR_FONT_FAMILY)
        self.assertNotEqual(self.result["face_before"], cli.STAR_FONT_FAMILY)

    def test_windows_classifies_the_font_the_way_the_console_requires(self):
        value = self.result["gdi_pitch_and_family"]
        self.assertEqual(value & 0xF0, 0x30, "FF_MODERN")
        self.assertEqual(value & 0x01, 0, "the low bit is set for variable-pitch fonts")


if __name__ == "__main__":
    unittest.main()
