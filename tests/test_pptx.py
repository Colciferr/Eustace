"""PowerPoint tools: the outline reader, write_pptx and edit_pptx, and what happens when an optional library
cannot be loaded at all."""
import io
import os
import subprocess
import sys
import unittest
import zipfile

from pptx import Presentation
from pptx.util import Inches

from _harness import cli, RecordingApproval, sandbox, typed, output

SLIDES = [
    {"title": "Quarterly Review", "subtitle": "Plans and Programs"},
    {"title": "Status", "bullets": ["Network refresh", "  Phase 1 complete", "  Phase 2 in work", "Funding"], "notes": "Mention the slip."},
    {"title": "Milestones", "table": [["Item", "Due"], ["Site survey", "14 Nov"], ["Install", "5 Dec"]]},
]


def _png(path: str, size=(400, 200)) -> None:
    from PIL import Image
    Image.new("RGB", size, (30, 90, 160)).save(path)


def _open(path: str):
    return Presentation(path)


def _files(folder: str) -> list[str]:
    """Folder listing without the Zone.Identifier marker (a hidden stream on NTFS, a visible file elsewhere)."""
    return sorted(n for n in os.listdir(folder) if ":" not in n)


class WritePptxTests(unittest.TestCase):
    def test_round_trip_through_the_outline(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            result = cli.write_pptx("brief", SLIDES)
            self.assertIn("Success", result)
            self.assertTrue(os.path.isfile(os.path.join(ws, "brief.pptx")))
            outline = cli.read_file("brief.pptx")
        self.assertIn("Presentation: 3 slide(s)", outline)
        self.assertIn("--- Slide 1 (layout: Title Slide) ---", outline)
        self.assertIn("--- Slide 2 (layout: Title and Content) ---", outline)
        self.assertIn("--- Slide 3 (layout: Title Only) ---", outline)
        self.assertRegex(outline, r"\[\d+\] .+ \((center )?title\): Quarterly Review")
        self.assertRegex(outline, r"\(subtitle\): Plans and Programs")
        self.assertIn("\n    Network refresh\n      Phase 1 complete\n      Phase 2 in work\n    Funding", outline)
        self.assertIn("[Speaker Notes]: Mention the slip.", outline)
        self.assertIn("(table):\n    Item | Due\n    Site survey | 14 Nov\n    Install | 5 Dec", outline)

    def test_blank_deck_is_widescreen_with_stretched_placeholders(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            cli.write_pptx("brief.pptx", SLIDES)
            prs = _open(os.path.join(ws, "brief.pptx"))
        self.assertEqual(round(prs.slide_width / prs.slide_height, 3), round(16 / 9, 3))
        title = prs.slides[1].shapes.title
        self.assertGreater(title.width, prs.slide_width * 0.8)
        self.assertLessEqual(title.left + title.width, prs.slide_width)

    def test_bullet_levels_are_real_indent_levels(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            cli.write_pptx("brief.pptx", SLIDES)
            body = [s for s in _open(os.path.join(ws, "brief.pptx")).slides[1].placeholders if s.placeholder_format.idx == 1][0]
        self.assertEqual([(p.text, p.level) for p in body.text_frame.paragraphs],
                         [("Network refresh", 0), ("Phase 1 complete", 1), ("Phase 2 in work", 1), ("Funding", 0)])

    def test_unused_placeholders_are_removed(self):
        with sandbox(approval=RecordingApproval(default=True)):
            cli.write_pptx("brief.pptx", [{"title": "Only a title", "layout": "Title and Content"}])
            self.assertNotIn("object", cli.read_file("brief.pptx").split("Slide 1")[1])

    def test_named_layout_and_unknown_layout(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            cli.write_pptx("a.pptx", [{"title": "Break", "layout": "section header"}])
            self.assertIn("(layout: Section Header)", cli.read_file("a.pptx"))
            calls = len(approval.calls)
            result = cli.write_pptx("b.pptx", [{"title": "x", "layout": "No Such Layout"}])
            self.assertIn("no layout named 'No Such Layout'", result)
            self.assertIn("Title and Content", result)
            self.assertEqual(len(approval.calls), calls)
            self.assertFalse(os.path.exists(os.path.join(ws, "b.pptx")))

    def test_bad_slide_descriptions_are_rejected_before_any_prompt(self):
        approval = RecordingApproval(default=True)
        cases = [
            ([], "non-empty list"),
            ("nope", "non-empty list"),
            ([{"title": "x", "colour": "red"}], "unknown field(s): colour"),
            ([{"title": 5}], "'title' must be text"),
            ([{"bullets": "one string"}], "'bullets' must be a list"),
            ([{"table": [[]]}], "'table' must be"),
            (["just text"], "must be an object"),
            ([{"title": "x"}] * (cli.MAX_PPTX_SLIDES + 1), "more than"),
        ]
        with sandbox(approval=approval) as (ws, _):
            for slides, expect in cases:
                with self.subTest(expect=expect):
                    self.assertIn(expect, cli.write_pptx("x.pptx", slides))
            self.assertEqual(approval.calls, [])
            self.assertEqual(os.listdir(ws), [])

    def test_image_is_placed_inside_the_slide(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            _png(os.path.join(ws, "chart.png"))
            result = cli.write_pptx("img.pptx", [{"title": "Chart", "bullets": ["Up 4%"], "image_path": "chart.png"}])
            self.assertIn("Success", result)
            self.assertIn("chart.png", approval.calls[-1][1])
            prs = _open(os.path.join(ws, "img.pptx"))
        pics = [s for s in prs.slides[0].shapes if s.shape_type is not None and s.shape_type.name == "PICTURE"]
        self.assertEqual(len(pics), 1)
        pic = pics[0]
        self.assertGreaterEqual(pic.left, 0)
        self.assertLessEqual(pic.left + pic.width, prs.slide_width)
        self.assertLessEqual(pic.top + pic.height, prs.slide_height)
        self.assertEqual(round(pic.width / pic.height, 1), 2.0)  # aspect ratio kept

    def test_image_path_follows_the_read_rules(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, home):
            _png(os.path.join(ws, "secret_plan.png"))
            _png(os.path.join(home, "outside.png"))
            with open(os.path.join(ws, "notes.txt"), "w") as f:
                f.write("x")
            self.assertIn("Security Error", cli.write_pptx("a.pptx", [{"image_path": "secret_plan.png"}]))
            self.assertIn("not found", cli.write_pptx("a.pptx", [{"image_path": "missing.png"}]))
            self.assertIn("must be one of", cli.write_pptx("a.pptx", [{"image_path": "notes.txt"}]))
            self.assertEqual(approval.calls, [])
            # outside the workspace: asks first, and that prompt is exempt from approve-all
            self.assertIn("Operation aborted", cli.write_pptx("a.pptx", [{"image_path": os.path.join(home, "outside.png")}]))
            self.assertEqual(approval.always_ask, [True])
            self.assertEqual(_files(ws), ["notes.txt", "secret_plan.png"])

    def test_preview_shows_every_supplied_string(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, _):
            result = cli.write_pptx("brief.pptx", SLIDES)
            self.assertIn("Operation aborted", result)
            self.assertEqual(os.listdir(ws), [])
        title, _, diff = approval.calls[0]
        self.assertIn("Create presentation", title)
        for text in ("Quarterly Review", "Plans and Programs", "Network refresh", "Phase 2 in work", "Funding",
                     "Mention the slip.", "Site survey | 14 Nov", "Install | 5 Dec"):
            with self.subTest(text=text):
                self.assertIn(text, diff)

    def test_overwrite_diff_shows_what_goes_away_and_race_is_caught(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            cli.write_pptx("brief.pptx", SLIDES)
            path = os.path.join(ws, "brief.pptx")

            class Racing(RecordingApproval):
                def request_approval(self, title, detail_text="", diff_text="", always_ask=False):
                    self.calls.append((title, detail_text, diff_text))
                    with open(path, "wb") as f:
                        f.write(b"someone else saved")
                    return True

            cli.APPROVAL_HANDLER = racing = Racing()
            result = cli.write_pptx("brief.pptx", [{"title": "Replacement"}])
            self.assertIn("changed between preview and write", result)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"someone else saved")
            self.assertIn("Overwrite presentation", racing.calls[0][0])
            self.assertIn("Quarterly Review", racing.calls[0][2])
            self.assertIn("+", racing.calls[0][2])

    def test_real_prompt_never_prints_raw_control_characters(self):
        with sandbox() as (ws, _):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            with typed("n"):
                cli.write_pptx("x.pptx", [{"title": "Safe\x1b[2Jtitle", "bullets": ["line\rone"]}])
            self.assertNotIn("\x1b[2J", output())
            self.assertEqual(os.listdir(ws), [])

    def test_template_supplies_layouts_and_its_slides_are_dropped_unless_kept(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            base = Presentation()
            base.slide_layouts[1].name = "Unit Content"
            sample = base.slides.add_slide(base.slide_layouts[1])
            sample.shapes.title.text = "Sample slide from the template"
            base.save(os.path.join(ws, "unit_template.pptx"))

            self.assertIn("Success", cli.write_pptx("a.pptx", [{"title": "Mine", "layout": "unit content", "bullets": ["x"]}],
                                                    template_path="unit_template.pptx"))
            outline = cli.read_file("a.pptx")
            self.assertIn("Presentation: 1 slide(s)", outline)
            self.assertIn("(layout: Unit Content)", outline)
            self.assertNotIn("Sample slide", outline)
            with zipfile.ZipFile(os.path.join(ws, "a.pptx")) as z:  # the dropped slide is really gone from the file
                self.assertEqual(len([n for n in z.namelist() if n.startswith("ppt/slides/slide")]), 1)

            cli.write_pptx("b.pptx", [{"title": "Mine"}], template_path="unit_template.pptx", keep_template_slides=True)
            self.assertIn("Presentation: 2 slide(s)", cli.read_file("b.pptx"))

    def test_potx_template_is_accepted(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            plain = io.BytesIO()
            Presentation().save(plain)
            potx = os.path.join(ws, "unit.potx")
            with zipfile.ZipFile(io.BytesIO(plain.getvalue())) as src, zipfile.ZipFile(potx, "w") as dst:
                for name in src.namelist():
                    data = src.read(name)
                    if name == "[Content_Types].xml":
                        data = data.replace(b"presentationml.presentation.main+xml", b"presentationml.template.main+xml")
                    dst.writestr(name, data)
            with self.assertRaises(Exception):  # the library alone refuses a template file
                Presentation(potx)
            self.assertIn("Success", cli.write_pptx("from_potx.pptx", [{"title": "Built on a .potx"}], template_path="unit.potx"))
            self.assertIn("Built on a .potx", cli.read_file("from_potx.pptx"))


class OutlineTests(unittest.TestCase):
    def test_tables_groups_and_empty_shapes_are_listed(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = "Layout"
            group = slide.shapes.add_group_shape()
            inner = group.shapes.add_textbox(Inches(1), Inches(2), Inches(3), Inches(1))
            inner.text_frame.text = "inside a group"
            table = slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(4), Inches(1)).table
            table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 0).text, table.cell(1, 1).text = "a", "b", "c", "d"
            slide.shapes.add_textbox(Inches(1), Inches(5), Inches(1), Inches(1))
            prs.save(os.path.join(ws, "d.pptx"))
            outline = cli.read_file("d.pptx")
            found = cli.search_files(".", query="inside a group")
        self.assertRegex(outline, r"\[\d+\] .+ \(group\)\n    \[\d+\] .+ \(text box\): inside a group")
        self.assertIn("(table):\n    a | b\n    c | d", outline)
        self.assertRegex(outline, r"\[\d+\] TextBox \d+ \(text box\)\n|\[\d+\] TextBox \d+ \(text box\)$")
        self.assertIn("d.pptx", found)


def _ids(outline: str, slide: int) -> dict:
    """shape kind -> id for one slide of an outline."""
    import re
    block = outline.split(f"--- Slide {slide} ")[1].split("--- Slide ")[0]
    return {kind: int(i) for i, kind in re.findall(r"\[(\d+)\] [^\n(]*\(([^)]+)\)", block)}


class EditPptxTests(unittest.TestCase):
    def _deck(self):
        cli.write_pptx("brief.pptx", SLIDES)
        return cli.read_file("brief.pptx")

    def test_every_operation(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            outline = self._deck()
            body_id = _ids(outline, 2)["object"]
            table_id = _ids(outline, 3)["table"]
            result = cli.edit_pptx("brief.pptx", [
                {"op": "replace_text", "find": "Quarterly", "replace": "Monthly"},
                {"op": "set_text", "slide": 2, "shape": body_id, "text": "First\n  Second level\nThird"},
                {"op": "set_text", "slide": 3, "shape": table_id, "row": 2, "col": 2, "text": "21 Nov"},
                {"op": "set_notes", "slide": 1, "text": "Open with the schedule."},
                {"op": "add_slide", "at": 2, "title": "Inserted", "bullets": ["new point"]},
                {"op": "move_slide", "slide": 4, "to": 1},
                {"op": "delete_slide", "slide": 4},
            ])
            self.assertIn("Success: Applied 7 operation(s)", result)
            after = cli.read_file("brief.pptx")
            with zipfile.ZipFile(os.path.join(ws, "brief.pptx")) as z:
                parts = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        self.assertIn("Presentation: 3 slide(s)", after)
        self.assertEqual(len(parts), 3)  # the deleted slide's part is not left behind in the file
        order = [line.split(": ", 1)[1] for line in after.splitlines() if "(title)" in line or "(center title)" in line]
        self.assertEqual(order, ["Milestones", "Monthly Review", "Inserted"])
        self.assertIn("Site survey | 21 Nov", after)
        self.assertIn("[Speaker Notes]: Open with the schedule.", after)
        self.assertNotIn("Second level", after)  # slide 4 at that step (the edited Status slide) was deleted

    def test_replace_text_keeps_run_formatting(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            para = slide.shapes.title.text_frame.paragraphs[0]
            plain, bold = para.add_run(), para.add_run()
            plain.text, bold.text = "Suspense: ", "7 Nov"
            bold.font.bold = True
            split = slide.shapes.add_textbox(Inches(1), Inches(3), Inches(4), Inches(1)).text_frame.paragraphs[0]
            first, second = split.add_run(), split.add_run()
            first.text, second.text = "Owner: Pl", "ans office"
            first.font.italic = True
            prs.save(os.path.join(ws, "f.pptx"))

            self.assertIn("Success", cli.edit_pptx("f.pptx", [
                {"op": "replace_text", "find": "7 Nov", "replace": "14 Nov"},
                {"op": "replace_text", "find": "Plans office", "replace": "Programs"},
            ]))
            slide = _open(os.path.join(ws, "f.pptx")).slides[0]
        runs = slide.shapes.title.text_frame.paragraphs[0].runs
        self.assertEqual([(r.text, r.font.bold) for r in runs], [("Suspense: ", None), ("14 Nov", True)])
        other = [s for s in slide.shapes if s.has_text_frame and "Owner" in s.text_frame.text][0].text_frame.paragraphs[0]
        self.assertEqual(other.text, "Owner: Programs")  # match spanned two runs
        self.assertTrue(other.runs[0].font.italic)

    def test_replacement_that_contains_the_search_text_is_applied_once(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            prs = Presentation()
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            para = slide.shapes.title.text_frame.paragraphs[0]
            a, b = para.add_run(), para.add_run()
            a.text, b.text = "Fund", "ing and Funding"
            prs.save(os.path.join(ws, "g.pptx"))
            cli.edit_pptx("g.pptx", [{"op": "replace_text", "find": "Funding", "replace": "Funding approved"}])
            text = _open(os.path.join(ws, "g.pptx")).slides[0].shapes.title.text_frame.text
        self.assertEqual(text, "Funding approved and Funding approved")

    def test_numbers_sent_as_text_are_accepted(self):
        with sandbox(approval=RecordingApproval(default=True)):
            body_id = _ids(self._deck(), 2)["object"]
            result = cli.edit_pptx("brief.pptx", [{"op": "set_text", "slide": "2", "shape": str(body_id), "text": "Quoted numbers"}])
            self.assertIn("Success", result)
            self.assertIn("Quoted numbers", cli.read_file("brief.pptx"))

    def test_replace_text_can_be_limited_to_one_slide_and_reaches_tables_and_notes(self):
        with sandbox(approval=RecordingApproval(default=True)):
            self._deck()
            cli.edit_pptx("brief.pptx", [{"op": "set_notes", "slide": 3, "text": "Nov is tight"}])
            cli.edit_pptx("brief.pptx", [{"op": "replace_text", "find": "Nov", "replace": "Dec", "slide": 3}])
            after = cli.read_file("brief.pptx")
        self.assertIn("Site survey | 14 Dec", after)
        self.assertIn("[Speaker Notes]: Dec is tight", after)

    def test_failed_operation_changes_nothing_and_never_prompts(self):
        approval = RecordingApproval(default=True)
        cases = [
            ([{"op": "replace_text", "find": "not in the deck", "replace": "x"}], "operation 1: text not found"),
            ([{"op": "set_notes", "slide": 1, "text": "ok"}, {"op": "delete_slide", "slide": 9}], "operation 2: 'slide' must be a whole number from 1 to 3"),
            ([{"op": "set_text", "slide": 1, "shape": 9999, "text": "x"}], "no shape with id 9999"),
            ([{"op": "explode"}], "'op' must be one of"),
            ([{"op": "delete_slide"}], "delete_slide takes ['slide']"),
            ([{"op": "delete_slide", "slide": 1, "force": True}], "delete_slide takes ['slide']"),
            ([{"op": "delete_slide", "slide": True}], "whole number"),
            ([{"op": "move_slide", "slide": 1, "to": 0}], "'to' must be"),
            ([{"op": "add_slide", "title": 7}], "'title' must be text"),
            ([{"op": "move_slide", "slide": 2, "to": 2}], "did not change anything"),
            ([], "non-empty list"),
        ]
        with sandbox(approval=approval) as (ws, _):
            self._deck()
            prompts = len(approval.calls)
            with open(os.path.join(ws, "brief.pptx"), "rb") as f:
                original = f.read()
            for ops, expect in cases:
                with self.subTest(expect=expect):
                    result = cli.edit_pptx("brief.pptx", ops)
                    self.assertIn(expect, result)
            self.assertIn("File not found", cli.edit_pptx("missing.pptx", [{"op": "delete_slide", "slide": 1}]))
            with open(os.path.join(ws, "brief.pptx"), "rb") as f:
                self.assertEqual(f.read(), original)
            self.assertEqual(len(approval.calls), prompts)

    def test_table_cell_needs_row_and_col(self):
        with sandbox(approval=RecordingApproval(default=True)):
            table_id = _ids(self._deck(), 3)["table"]
            self.assertIn("give 'row' and 'col'", cli.edit_pptx("brief.pptx", [{"op": "set_text", "slide": 3, "shape": table_id, "text": "x"}]))
            self.assertIn("'row' must be a whole number from 1 to 3", cli.edit_pptx(
                "brief.pptx", [{"op": "set_text", "slide": 3, "shape": table_id, "row": 9, "col": 1, "text": "x"}]))

    def test_preview_is_the_exact_outline_change_and_denial_saves_nothing(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            self._deck()
            with open(os.path.join(ws, "brief.pptx"), "rb") as f:
                original = f.read()
            cli.APPROVAL_HANDLER = denying = RecordingApproval(default=False)
            result = cli.edit_pptx("brief.pptx", [{"op": "replace_text", "find": "Funding", "replace": "Funding approved"}])
            self.assertIn("Operation aborted", result)
            with open(os.path.join(ws, "brief.pptx"), "rb") as f:
                self.assertEqual(f.read(), original)
        title, _, diff = denying.calls[0]
        self.assertIn("Edit presentation", title)
        changed = [line for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
        self.assertEqual(changed, ["-    Funding", "+    Funding approved"])

    def test_file_changed_during_approval_aborts(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            self._deck()
            path = os.path.join(ws, "brief.pptx")

            class Racing(RecordingApproval):
                def request_approval(self, *a, **k):
                    with open(path, "ab") as f:
                        f.write(b"x")
                    return True

            cli.APPROVAL_HANDLER = Racing()
            self.assertIn("changed between preview and write", cli.edit_pptx("brief.pptx", [{"op": "delete_slide", "slide": 1}]))

    def test_no_temp_files_left_behind(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            self._deck()
            cli.edit_pptx("brief.pptx", [{"op": "delete_slide", "slide": 1}])
            self.assertEqual(_files(ws), ["brief.pptx"])


class BlockedLibraryTests(unittest.TestCase):
    """Application control can refuse to load a package's compiled code. That must disable the tools that
    need the package, with a clear reason, and nothing else."""

    BLOCK_MESSAGE = "DLL load failed while importing etree: An Application Control policy has blocked this file."

    def _block(self, *prefixes):
        real = cli.importlib.import_module

        def fake(name, *a, **k):
            if name.split(".")[0] in prefixes:
                raise ImportError(self.BLOCK_MESSAGE)
            return real(name, *a, **k)

        cli.importlib.import_module = fake
        self.addCleanup(setattr, cli.importlib, "import_module", real)

    def test_each_tool_reports_unavailable_without_prompting(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            cli.write_pptx("deck.pptx", [{"title": "x"}])
            cli.write_docx("memo.docx", "hello")
            cli.update_excel("book.xlsx", append_rows=[["a"]])
            prompts = len(approval.calls)
            self._block("pptx", "docx", "openpyxl", "pypdf", "win32com")
            calls = {
                "PowerPoint support": [lambda: cli.write_pptx("new.pptx", [{"title": "x"}]),
                                       lambda: cli.edit_pptx("deck.pptx", [{"op": "delete_slide", "slide": 1}]),
                                       lambda: cli.read_file("deck.pptx")],
                "Word support": [lambda: cli.write_docx("new.docx", "x"),
                                 lambda: cli.fill_docx_template("memo.docx", "out.docx", {"hello": "bye"}),
                                 lambda: cli.read_file("memo.docx")],
                "Excel support": [lambda: cli.update_excel("book.xlsx", append_rows=[["b"]]),
                                  lambda: cli.read_file("book.xlsx")],
                "Outlook support": [cli.list_inbox, cli.get_calendar],
            }
            for feature, tools in calls.items():
                for i, run in enumerate(tools):
                    with self.subTest(feature=feature, call=i):
                        result = run()
                        self.assertTrue(result.startswith("Error"), result)
                        self.assertIn(f"{feature} is unavailable on this machine", result)
                        self.assertIn("Application Control policy", result)
            self.assertEqual(len(approval.calls), prompts)
            self.assertEqual(_files(ws), ["book.xlsx", "deck.pptx", "memo.docx"])

    def test_unblocked_tools_keep_working_and_search_says_what_it_skipped(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, _):
            cli.write_pptx("deck.pptx", [{"title": "needle in a deck"}])
            with open(os.path.join(ws, "notes.txt"), "w") as f:
                f.write("needle in a text file")
            self._block("pptx")
            self.assertIn("Success", cli.write_file("other.txt", "still works"))
            found = cli.search_files(".", query="needle")
        self.assertIn("notes.txt", found)
        self.assertNotIn("Match: deck.pptx", found)
        self.assertIn("Note: some documents were not searched: PowerPoint support is unavailable", found)

    @unittest.skipUnless(os.name == "nt", "cli imports only on Windows")
    def test_starting_eustace_loads_no_optional_library(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ("import sys; sys.argv = ['cli']; import cli; "
                "print(sorted(m for m in ('docx', 'pptx', 'openpyxl', 'pypdf', 'lxml', 'PIL', 'win32com') if m in sys.modules))")
        done = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "[]", done.stderr)


if __name__ == "__main__":
    unittest.main()
