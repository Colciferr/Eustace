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


if __name__ == "__main__":
    unittest.main()
