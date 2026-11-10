"""Office and Outlook tools: macro workbooks survive an update, and the calendar query is built in the
order Outlook requires."""
import io
import os
import unittest
import zipfile

from openpyxl import Workbook

from _harness import cli, RecordingApproval, sandbox

MACRO_MAIN = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"
PLAIN_MAIN = b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"


def make_xlsm(path: str) -> None:
    """A minimal macro-enabled workbook: a normal workbook plus a vbaProject.bin part and the macro content type."""
    plain = io.BytesIO()
    wb = Workbook()
    wb.active.append(["name", "status"])
    wb.save(plain)
    with zipfile.ZipFile(io.BytesIO(plain.getvalue())) as src, zipfile.ZipFile(path, "w") as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "[Content_Types].xml":
                data = data.replace(PLAIN_MAIN, MACRO_MAIN).replace(
                    b"</Types>",
                    b'<Override PartName="/xl/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
                )
            dst.writestr(name, data)
        dst.writestr("xl/vbaProject.bin", b"FAKE-VBA-PROJECT")


class ExcelMacroTests(unittest.TestCase):
    def test_updating_xlsm_keeps_the_macro_project(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            p = os.path.join(ws, "tracker.xlsm")
            make_xlsm(p)
            result = cli.update_excel("tracker.xlsm", append_rows=[["a", "open"]], cell_updates={"B2": "done"})
            self.assertIn("Success", result)
            with zipfile.ZipFile(p) as z:
                self.assertEqual(z.read("xl/vbaProject.bin"), b"FAKE-VBA-PROJECT")
                self.assertIn(MACRO_MAIN, z.read("[Content_Types].xml"))
            self.assertIn("a | done", cli.read_file("tracker.xlsm"))

    def test_new_xlsm_is_refused_before_approval(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            result = cli.update_excel("new.xlsm", append_rows=[["a"]])
            self.assertIn("cannot be created", result)
            self.assertEqual(approval.calls, [])
            self.assertEqual(os.listdir(ws), [])

    def test_existing_workbook_prompt_warns_about_rewrite(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval):
            cli.update_excel("book.xlsx", append_rows=[["a"]])
            self.assertNotIn("rewritten", approval.calls[0][1])
            cli.update_excel("book.xlsx", append_rows=[["b"]])
            self.assertIn("rewritten", approval.calls[1][1])


class _FakeEvent:
    Subject, Location, AllDayEvent, BusyStatus = "Staff sync", "Bldg 1", False, 2
    Start, End = "2026-11-10 09:00:00", "2026-11-10 09:30:00"


class _FakeItems:
    """Records the order of the calls Outlook is sensitive to."""

    def __init__(self, restrict_error=None):
        self.log = []
        self.restrict_error = restrict_error
        self._queue = []

    def Sort(self, prop, descending=False):
        self.log.append(("Sort", prop))

    def __setattr__(self, name, value):
        if name == "IncludeRecurrences":
            self.log.append(("IncludeRecurrences", value))
        object.__setattr__(self, name, value)

    def Restrict(self, query):
        self.log.append(("Restrict", query))
        if self.restrict_error:
            raise self.restrict_error
        self._queue = [_FakeEvent()]
        return self

    def GetFirst(self):
        self.log.append(("GetFirst",))
        return self._queue.pop(0) if self._queue else None

    GetNext = GetFirst


class CalendarQueryTests(unittest.TestCase):
    def _calendar(self, items):
        folder = type("Folder", (), {"Items": items})()
        ns = type("NS", (), {"GetDefaultFolder": lambda self, n: folder})()
        saved = cli._get_outlook_ns
        cli._get_outlook_ns = lambda: (None, ns)
        try:
            return cli.get_calendar(days_ahead=7)
        finally:
            cli._get_outlook_ns = saved

    def test_sort_then_recurrences_then_restrict(self):
        items = _FakeItems()
        out = self._calendar(items)
        self.assertEqual([c[0] for c in items.log[:3]], ["Sort", "IncludeRecurrences", "Restrict"])
        self.assertEqual(items.log[0], ("Sort", "[Start]"))
        self.assertEqual(items.log[1], ("IncludeRecurrences", True))
        self.assertIn("Staff sync", out)

    def test_filter_selects_overlapping_events(self):
        items = _FakeItems()
        self._calendar(items)
        query = items.log[2][1]
        self.assertRegex(query, r"^\[End\] >= '.+' AND \[Start\] <= '.+'$")

    def test_rejected_filter_is_an_error_not_the_whole_calendar(self):
        items = _FakeItems(restrict_error=RuntimeError("bad filter"))
        out = self._calendar(items)
        self.assertIn("Error reading Outlook Calendar", out)
        self.assertNotIn(("GetFirst",), items.log)


if __name__ == "__main__":
    unittest.main()
