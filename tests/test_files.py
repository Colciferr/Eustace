"""write_file / edit_file must change only the bytes shown in the approved diff: line endings, BOMs and
encodings of existing files survive, and files that cannot round-trip are refused rather than corrupted."""
import os
import random
import unittest

from _harness import cli, RecordingApproval, sandbox


def _put(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _get(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


class EditFileExactnessTests(unittest.TestCase):
    def _edit(self, data: bytes, old: str, new: str, approve=True):
        approval = RecordingApproval(default=approve)
        with sandbox(approval=approval) as (ws, _):
            p = os.path.join(ws, "f.txt")
            _put(p, data)
            result = cli.edit_file("f.txt", old, new)
            return result, _get(p), approval

    def test_crlf_file_stays_crlf(self):
        result, out, _ = self._edit(b"one\r\ntwo\r\nthree\r\n", "two", "TWO")
        self.assertIn("Success", result)
        self.assertEqual(out, b"one\r\nTWO\r\nthree\r\n")

    def test_lf_file_stays_lf(self):
        result, out, _ = self._edit(b"one\ntwo\nthree\n", "two", "TWO")
        self.assertIn("Success", result)
        self.assertEqual(out, b"one\nTWO\nthree\n")

    def test_mixed_endings_untouched_lines_keep_theirs(self):
        result, out, _ = self._edit(b"a\r\nb\nc\r\nd\re\r\n", "c", "C")
        self.assertIn("Success", result)
        self.assertEqual(out, b"a\r\nb\nC\r\nd\re\r\n")

    def test_multiline_match_and_insert_in_crlf_file(self):
        result, out, _ = self._edit(b"a\r\nb\r\nc\r\n", "a\nb", "x\ny\nz")
        self.assertIn("Success", result)
        self.assertEqual(out, b"x\r\ny\r\nz\r\nc\r\n")

    def test_model_supplied_crlf_is_not_doubled(self):
        result, out, _ = self._edit(b"a\r\nb\r\n", "a\r\nb", "x\r\ny")
        self.assertIn("Success", result)
        self.assertEqual(out, b"x\r\ny\r\n")

    def test_utf8_bom_and_non_ascii_survive(self):
        data = "﻿café\r\nnaïve\r\n".encode("utf-8")
        result, out, _ = self._edit(data, "naïve", "naive")
        self.assertIn("Success", result)
        self.assertEqual(out, "﻿café\r\nnaive\r\n".encode("utf-8"))

    def test_utf16_file_round_trips(self):
        for enc in ("utf-16-le", "utf-16-be"):
            with self.subTest(enc=enc):
                data = "﻿Get-Process\r\nGet-Service\r\n".encode(enc)
                result, out, _ = self._edit(data, "Get-Service", "Get-Date")
                self.assertIn("Success", result)
                self.assertEqual(out, "﻿Get-Process\r\nGet-Date\r\n".encode(enc))

    def test_non_utf8_file_is_refused_untouched(self):
        data = b"caf\xe9 au lait\r\nline two\r\n"  # cp1252
        result, out, approval = self._edit(data, "line two", "LINE TWO")
        self.assertIn("not valid UTF-8", result)
        self.assertEqual(out, data)
        self.assertEqual(approval.calls, [])

    def test_ambiguous_old_text_is_refused(self):
        data = b"x = 1\nx = 1\n"
        result, out, approval = self._edit(data, "x = 1", "x = 2")
        self.assertIn("matches 2 places", result)
        self.assertEqual(out, data)
        self.assertEqual(approval.calls, [])

    def test_empty_missing_and_identical_old_text(self):
        data = b"abc\n"
        for old, new, expect in (("", "x", "empty"), ("zzz", "x", "not found"), ("abc", "abc", "identical")):
            with self.subTest(old=old):
                result, out, _ = self._edit(data, old, new)
                self.assertIn(expect, result)
                self.assertEqual(out, data)

    def test_denied_edit_changes_nothing(self):
        data = b"a\r\nb\r\n"
        result, out, approval = self._edit(data, "a", "A", approve=False)
        self.assertIn("Operation aborted", result)
        self.assertEqual(out, data)
        self.assertEqual(len(approval.calls), 1)

    def test_diff_shows_only_the_logical_change(self):
        _, _, approval = self._edit(b"one\r\ntwo\r\nthree\r\n", "two", "TWO")
        diff = approval.calls[0][2]
        self.assertIn("-two", diff)
        self.assertIn("+TWO", diff)
        self.assertNotIn("-one", diff)
        self.assertNotIn("\r", diff)

    def test_file_changed_during_approval_aborts(self):
        class Racing(RecordingApproval):
            def request_approval(self, *a, **k):
                _put(self.path, b"someone else wrote this\n")
                return True

        approval = Racing()
        with sandbox(approval=approval) as (ws, _):
            approval.path = os.path.join(ws, "f.txt")
            _put(approval.path, b"a\nb\n")
            result = cli.edit_file("f.txt", "a", "A")
            self.assertIn("changed between preview and write", result)
            self.assertEqual(_get(approval.path), b"someone else wrote this\n")

    def test_random_files_only_the_target_span_changes(self):
        rng = random.Random(20261110)
        endings = ["\n", "\r\n", "\r"]
        for i in range(300):
            lines = ["".join(rng.choice("abc xyz\t") for _ in range(rng.randrange(0, 12))) for _ in range(rng.randrange(1, 15))]
            at = rng.randrange(len(lines))
            lines[at:at] = ["FIRST@@", "SECOND@@"]
            raw = "".join(line + rng.choice(endings) for line in lines)
            if rng.random() < 0.2:
                raw = "﻿" + raw
            multi = rng.random() < 0.5 and "FIRST@@\rSECOND@@" not in raw  # a bare CR is not a line ending the model would write
            old = "FIRST@@\nSECOND@@" if multi else "FIRST@@"
            new = rng.choice(["", "one", "one\ntwo", "one\ntwo\n\nthree"])
            start = raw.index("FIRST@@")
            end = raw.index("SECOND@@") + len("SECOND@@") if multi else start + len("FIRST@@")
            expected = raw[:start] + new.replace("\n", cli._dominant_newline(raw)) + raw[end:]
            with self.subTest(i=i):
                result, out, _ = self._edit(raw.encode("utf-8"), old, new)
                self.assertIn("Success", result)
                self.assertEqual(out, expected.encode("utf-8"))


class WriteFileExactnessTests(unittest.TestCase):
    def _write(self, existing, content, approve=True, name="f.txt"):
        approval = RecordingApproval(default=approve)
        with sandbox(approval=approval) as (ws, _):
            p = os.path.join(ws, name)
            if existing is not None:
                _put(p, existing)
            result = cli.write_file(name, content)
            return result, (_get(p) if os.path.exists(p) else None), approval

    def test_overwrite_keeps_existing_line_endings(self):
        _, out, _ = self._write(b"old\r\nfile\r\n", "new\nfile\n")
        self.assertEqual(out, b"new\r\nfile\r\n")
        _, out, _ = self._write(b"old\nfile\n", "new\nfile\n")
        self.assertEqual(out, b"new\nfile\n")

    def test_overwrite_keeps_bom_and_utf16(self):
        _, out, _ = self._write("﻿old\r\n".encode("utf-8"), "new\n")
        self.assertEqual(out, "﻿new\r\n".encode("utf-8"))
        _, out, _ = self._write("﻿old\r\n".encode("utf-16-le"), "new\n")
        self.assertEqual(out, "﻿new\r\n".encode("utf-16-le"))

    def test_new_file_uses_platform_default_and_never_doubles_cr(self):
        _, out, _ = self._write(None, "a\r\nb\n")
        self.assertEqual(out, ("a" + os.linesep + "b" + os.linesep).encode("utf-8"))

    def test_non_text_existing_file_is_flagged_in_the_prompt(self):
        result, out, approval = self._write(b"\xff\x00\x80binary", "text\n")
        self.assertIn("Success", result)
        self.assertIn("not UTF-8 text", approval.calls[0][1])
        self.assertEqual(out, ("text" + os.linesep).encode("utf-8"))

    def test_denied_write_creates_nothing(self):
        result, out, _ = self._write(None, "x", approve=False)
        self.assertIn("Operation aborted", result)
        self.assertIsNone(out)

    def test_directory_target_is_an_error(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            os.mkdir(os.path.join(ws, "d"))
            self.assertIn("is a directory", cli.write_file("d", "x"))
            self.assertEqual(approval.calls, [])

    def test_file_changed_or_created_during_approval_aborts(self):
        class Racing(RecordingApproval):
            def request_approval(self, *a, **k):
                _put(self.path, b"raced\n")
                return True

        for existing in (b"orig\n", None):
            with self.subTest(existing=existing):
                approval = Racing()
                with sandbox(approval=approval) as (ws, _):
                    approval.path = os.path.join(ws, "f.txt")
                    if existing is not None:
                        _put(approval.path, existing)
                    result = cli.write_file("f.txt", "mine\n")
                    self.assertIn("changed between preview and write", result)
                    self.assertEqual(_get(approval.path), b"raced\n")


class ReadFileEncodingTests(unittest.TestCase):
    def _read(self, data: bytes) -> str:
        with sandbox(approval=RecordingApproval()) as (ws, _):
            _put(os.path.join(ws, "f.txt"), data)
            return cli.read_file("f.txt")

    def test_utf16_is_decoded(self):
        self.assertEqual(self._read("﻿Get-Process\n".encode("utf-16-le")), "Get-Process\n")

    def test_utf8_bom_is_dropped_and_bad_bytes_do_not_crash(self):
        self.assertEqual(self._read("﻿hello".encode("utf-8")), "hello")
        self.assertIn("caf", self._read(b"caf\xe9"))


if __name__ == "__main__":
    unittest.main()
