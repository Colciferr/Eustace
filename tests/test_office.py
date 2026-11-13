"""Office and Outlook tools: macro workbooks survive an update, and the calendar query is built in the
order Outlook requires."""
import io
import os
import types
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


class _Coll:
    """A 1-based Outlook COM collection."""

    def __init__(self, *members):
        self._members = list(members)

    @property
    def Count(self):
        return len(self._members)

    def Item(self, i):
        member = self._members[i - 1]
        if isinstance(member, Exception):
            raise member
        return member


class _Event:
    AllDayEvent, BusyStatus = False, 2

    def __init__(self, subject, start, location=""):
        self.Subject, self.Location = subject, location
        self.Start, self.End = f"{start}:00", f"{start[:11]}23:00:00"


class _Items:
    def __init__(self, events=(), restrict_error=None):
        self._events, self._restrict_error, self._queue = list(events), restrict_error, []
        self.restricted = 0

    def Sort(self, prop, descending=False):
        pass

    def Restrict(self, query):
        self.restricted += 1
        if self._restrict_error:
            raise self._restrict_error
        self._queue = list(self._events)
        return self

    def GetFirst(self):
        return self._queue.pop(0) if self._queue else None

    GetNext = GetFirst


class _Store:
    def __init__(self, name, calendar=None, error=None):
        self.DisplayName, self._calendar, self._error = name, calendar, error

    def GetDefaultFolder(self, kind):
        if self._error:
            raise self._error
        return self._calendar


class _Folder:
    DefaultItemType = 1

    def __init__(self, name, store, entry_id, events=(), subfolders=(), restrict_error=None, item_type=1):
        self.Name, self.Store, self.EntryID = name, store, entry_id
        self.FolderPath = f"\\\\{store.DisplayName}\\{name}"
        self.Items = _Items(events, restrict_error)
        self.Folders = _Coll(*subfolders)
        self.DefaultItemType = item_type


class _Nav:
    def __init__(self, name, folder=None, shown=False, error=None):
        self.DisplayName, self.IsSelected, self._folder, self._error = name, shown, folder, error

    @property
    def Folder(self):
        if self._error:
            raise self._error
        return self._folder


class _App:
    """Outlook with an optional main window whose Calendar pane lists the given navigation folders."""

    def __init__(self, nav=(), window=True, pane_error=None):
        self.asked_for_module = []
        module = types.SimpleNamespace(NavigationGroups=_Coll(types.SimpleNamespace(NavigationFolders=_Coll(*nav))))

        def get_module(kind):
            self.asked_for_module.append(kind)
            if pane_error:
                raise pane_error
            return module

        self._window = types.SimpleNamespace(NavigationPane=types.SimpleNamespace(
            Modules=types.SimpleNamespace(GetNavigationModule=get_module))) if window else None
        self.Explorers = _Coll()

    def ActiveExplorer(self):
        return self._window


class _NS:
    def __init__(self, default, stores, same_ids=()):
        self._default, self.Stores, self._same, self.default_lookups = default, _Coll(*stores), set(same_ids), 0

    def GetDefaultFolder(self, kind):
        self.default_lookups += 1
        return self._default

    def CompareEntryIDs(self, a, b):
        return (a, b) in self._same or (b, a) in self._same


class SharedCalendarTests(unittest.TestCase):
    """get_calendar used to read NameSpace.GetDefaultFolder(9) only: the user's own calendar and nothing else."""

    def setUp(self):
        mine = _Store("cole@example.mil")
        self.mine = _Folder("Calendar", mine, "ID-MINE", [_Event("Staff sync", "2026-11-16 09:00", "Bldg 1")])
        self.birthdays = _Folder("Birthdays", mine, "ID-BDAY", [_Event("Sam's birthday", "2026-11-17 00:00")])
        self.mine.Folders = _Coll(self.birthdays, _Folder("Notes in here", mine, "ID-NOTE", item_type=0))
        mine._calendar = self.mine

        plans_store = _Store("374 CS/SCXP Plans")
        self.plans = _Folder("Calendar", plans_store, "ID-PLANS", [
            _Event("Doe leave", "2026-11-14 00:00"), _Event("Roe TDY", "2026-11-20 00:00", "Kadena")])
        plans_store._calendar = self.plans

        shared_store = _Store("Shared Calendars")
        self.boss = _Folder("Calendar", shared_store, "ID-BOSS", [_Event("Commander's call", "2026-11-18 10:00")])

        self.app = _App(nav=[_Nav("Calendar", self.mine, shown=True), _Nav("Maj Boss", self.boss)])
        self.ns = _NS(self.mine, [mine, plans_store, _Store("Archive"), _Store("Broken", error=OSError("offline"))])

    def _run(self, fn, *args, app="default", **kwargs):
        saved = cli._get_outlook_ns
        cli._get_outlook_ns = lambda: (self.app if app == "default" else app, self.ns)
        try:
            return fn(*args, **kwargs)
        finally:
            cli._get_outlook_ns = saved

    def test_own_calendar_is_still_the_default_and_looks_for_nothing_else(self):
        out = self._run(cli.get_calendar)
        self.assertIn("Staff sync", out)
        self.assertNotIn("Doe leave", out)
        self.assertNotIn("[", out.split("\n", 1)[1])  # no calendar tags
        self.assertEqual(self.app.asked_for_module, [])
        self.assertEqual(self.plans.Items.restricted, 0)

    def test_list_shows_every_calendar_once(self):
        out = self._run(cli.list_calendars)
        self.assertIn("Outlook calendars (4):", out)
        self.assertRegex(out, r"1\. Calendar \[default\] \[checked in Outlook\]")
        self.assertIn("found via: default, navigation pane, mailbox", out)
        self.assertIn("Maj Boss", out)
        self.assertIn("Birthdays", out)
        self.assertIn("374 CS/SCXP Plans / Calendar", out)
        self.assertNotIn("Notes in here", out)  # a mail or notes folder under the calendar is not a calendar
        self.assertEqual(self.app.asked_for_module, [1])  # olModuleCalendar
        self.assertNotIn("Doe leave", out)  # names only

    def test_a_shared_mailbox_calendar_is_found_by_the_mailbox_name(self):
        # its folder is just called "Calendar": only the mailbox carries the name the user knows it by
        for wanted in ("SCXP Plans", "scxp", "374 CS/SCXP Plans / Calendar", "  374 cs/scxp   plans "):
            with self.subTest(wanted=wanted):
                out = self._run(cli.get_calendar, calendar=wanted)
                self.assertIn("Doe leave", out)
                self.assertIn("Roe TDY** @ Kadena", out)
                self.assertNotIn("Staff sync", out)
                self.assertIn("'374 CS/SCXP Plans / Calendar'", out.split("\n")[0])

    def test_a_calendar_shared_by_a_person_is_found_by_its_navigation_pane_name(self):
        out = self._run(cli.get_calendar, calendar="maj boss")
        self.assertIn("Commander's call", out)
        self.assertNotIn("Staff sync", out)

    def test_a_renamed_shared_calendar_is_still_found_by_its_mailbox(self):
        self.plans.FolderPath = "\\\\Shared Calendars\\Calendar"
        self.app = _App(nav=[_Nav("Leave tracker", self.plans)])
        for wanted in ("scxp plans", "leave tracker"):
            with self.subTest(wanted=wanted):
                self.assertIn("Doe leave", self._run(cli.get_calendar, calendar=wanted))

    def test_an_exact_name_wins_over_a_partial_one(self):
        self.app = _App(nav=[_Nav("Plans", self.boss), _Nav("Plans archive", self.plans)])
        out = self._run(cli.get_calendar, calendar="plans")
        self.assertIn("Commander's call", out)
        self.assertNotIn("Doe leave", out)

    def test_a_name_that_fits_several_calendars_reads_them_all_and_tags_each_event(self):
        out = self._run(cli.get_calendar, calendar="cole@example.mil")
        self.assertIn("from 2 calendars", out)
        self.assertIn("**Staff sync** @ Bldg 1 [Calendar]", out)
        self.assertIn("**Sam's birthday** [Birthdays]", out)
        self.assertNotIn("Doe leave", out)

    def test_all_merges_every_calendar_in_time_order(self):
        for wanted in ("all", "ALL", "*"):
            with self.subTest(wanted=wanted):
                out = self._run(cli.get_calendar, calendar=wanted)
                order = [out.index(s) for s in ("Doe leave", "Staff sync", "Sam's birthday", "Commander's call", "Roe TDY")]
                self.assertEqual(order, sorted(order), out)
                self.assertIn("from 4 calendars", out)
                self.assertIn("5 events", out)

    def test_no_match_lists_what_exists_and_reads_nothing(self):
        out = self._run(cli.get_calendar, calendar="Finance")
        self.assertTrue(out.startswith("Error reading Outlook Calendar: no calendar matches 'Finance'"), out)
        self.assertIn("Maj Boss", out)
        self.assertEqual(self.mine.Items.restricted + self.plans.Items.restricted, 0)

    def test_one_calendar_reached_two_ways_is_read_once(self):
        # the navigation pane hands back a different EntryID string for the same folder
        twin = _Folder("Calendar", self.plans.Store, "ID-PLANS-LONG-TERM", [_Event("Doe leave", "2026-11-14 00:00")])
        twin.FolderPath = "\\\\Shared\\Somewhere else"
        self.app = _App(nav=[_Nav("SCXP Plans", twin, shown=True)])
        self.ns._same = {("ID-PLANS-LONG-TERM", "ID-PLANS")}
        listing = self._run(cli.list_calendars)
        self.assertIn("Outlook calendars (3):", listing)
        self.assertIn("SCXP Plans [checked in Outlook]", listing)
        out = self._run(cli.get_calendar, calendar="all")
        self.assertEqual(out.count("Doe leave"), 1, out)

    def test_calendars_without_ids_fall_back_to_the_folder_path(self):
        self.plans.EntryID = ""
        twin = _Folder("Calendar", self.plans.Store, "", [_Event("Doe leave", "2026-11-14 00:00")])
        self.app = _App(nav=[_Nav("SCXP Plans", twin)])
        self.assertEqual(self._run(cli.get_calendar, calendar="all").count("Doe leave"), 1)

    def test_one_unreadable_calendar_does_not_hide_the_others(self):
        self.plans.Items = _Items(restrict_error=RuntimeError("bad filter"))
        self.app = _App(nav=[_Nav("Capt Gone", error=OSError("mailbox unavailable")), _Nav("Maj Boss", self.boss)])
        out = self._run(cli.get_calendar, calendar="all")
        self.assertIn("Staff sync", out)
        self.assertIn("Commander's call", out)
        self.assertIn("Could not read:", out)
        self.assertIn("374 CS/SCXP Plans / Calendar: the date filter was rejected (bad filter)", out)
        self.assertIn("Capt Gone: unreachable (mailbox unavailable)", out)
        listing = self._run(cli.list_calendars)
        self.assertIn("Capt Gone [UNREACHABLE: mailbox unavailable]", listing)

    def test_the_only_calendar_asked_for_failing_is_an_error(self):
        self.plans.Items = _Items(restrict_error=RuntimeError("bad filter"))
        out = self._run(cli.get_calendar, calendar="SCXP Plans")
        self.assertTrue(out.startswith("Error reading Outlook Calendar:"), out)
        self.assertIn("the date filter was rejected", out)

    def test_without_an_outlook_window_the_mailboxes_are_still_searched_and_the_gap_is_reported(self):
        for app in (_App(window=False), None):
            with self.subTest(app=app):
                listing = self._run(cli.list_calendars, app=app)
                self.assertIn("374 CS/SCXP Plans / Calendar", listing)
                self.assertNotIn("Maj Boss", listing)
                self.assertIn("No Outlook window is open", listing)
                self.assertIn("Doe leave", self._run(cli.get_calendar, calendar="scxp", app=app))

    def test_a_second_window_is_used_when_none_is_active(self):
        app = _App(nav=[_Nav("Maj Boss", self.boss)])
        app.Explorers, app._window = _Coll(app._window), None
        self.assertIn("Maj Boss", self._run(cli.list_calendars, app=app))

    def test_a_navigation_pane_that_cannot_be_read_is_reported_not_fatal(self):
        listing = self._run(cli.list_calendars, app=_App(pane_error=RuntimeError("no calendar module")))
        self.assertIn("374 CS/SCXP Plans / Calendar", listing)
        self.assertIn("navigation pane could not be read (no calendar module)", listing)

    def test_discovery_never_creates_a_window_or_touches_protected_members(self):
        class Strict(_Folder):
            def __getattr__(self, name):  # anything the fake does not define: GetExplorer, Display, Body...
                raise AssertionError(f"calendar discovery touched Folder.{name}")

        self.boss.__class__ = Strict
        self.plans.__class__ = Strict
        self.mine.__class__ = Strict
        self._run(cli.list_calendars)
        self._run(cli.get_calendar, calendar="all")

    def test_same_labels_are_made_distinct(self):
        self.app = _App(nav=[_Nav("Team", self.boss), _Nav("Team", self.plans)])
        listing = self._run(cli.list_calendars)
        self.assertIn("Team\n", listing.replace(" [", "\n["))
        self.assertIn("Team (2)", listing)
        out = self._run(cli.get_calendar, calendar="team")
        self.assertIn("[Team]", out)
        self.assertIn("[Team (2)]", out)

    def test_phi_in_a_shared_entry_is_withheld_but_its_time_is_kept(self):
        self.plans.Items = _Items([_Event("Doe AF Form 469 review", "2026-11-14 08:00", "Clinic"),
                                   _Event("Roe leave", "2026-11-15 00:00", "HIPAA room")])
        out = self._run(cli.get_calendar, calendar="scxp")
        self.assertNotIn("469", out)
        self.assertNotIn("Clinic", out)
        self.assertNotIn("HIPAA", out)
        self.assertNotIn("Roe leave", out)
        self.assertIn("2026-11-14 08:00", out)
        self.assertIn("2 entries had the subject withheld", out)

    def test_phi_is_withheld_on_the_users_own_calendar_too(self):
        self.mine.Items = _Items([_Event("Patient diagnosis follow-up", "2026-11-16 09:00")])
        out = self._run(cli.get_calendar)
        self.assertNotIn("diagnosis", out.lower())
        self.assertIn("1 entry had the subject withheld", out)

    def test_a_long_calendar_says_it_was_cut_off(self):
        self.plans.Items = _Items([_Event(f"Event {i:03d}", "2026-11-14 08:00") for i in range(150)])
        out = self._run(cli.get_calendar, calendar="scxp")
        self.assertIn("100 events", out)
        self.assertIn("Event 099", out)
        self.assertNotIn("Event 100", out)
        self.assertIn("Showing the first 100 events", out)
        self.assertNotIn("Showing the first", self._run(cli.get_calendar))

    def test_discovery_stops_at_the_calendar_limit(self):
        store = self.plans.Store
        self.plans.Folders = _Coll(*[_Folder(f"Sub {i}", store, f"ID-SUB-{i}") for i in range(80)])
        listing = self._run(cli.list_calendars)
        self.assertIn(f"Outlook calendars ({cli._MAX_CALENDARS}):", listing)
        self.assertEqual(listing.count("more were not listed"), 1)

    def test_a_collection_member_that_cannot_be_fetched_is_skipped(self):
        self.ns.Stores = _Coll(OSError("store offline"), self.plans.Store)
        self.assertIn("374 CS/SCXP Plans / Calendar", self._run(cli.list_calendars))

    def test_dev_mode_asks_before_listing_calendars(self):
        cli.DEV_MODE = True
        try:
            approval = RecordingApproval(default=False)
            with sandbox(approval=approval):
                self.assertIn("Operation aborted", self._run(cli.list_calendars))
                self.assertIn("Operation aborted", self._run(cli.get_calendar, calendar="all"))
            self.assertEqual(self.ns.default_lookups, 0)
        finally:
            cli.DEV_MODE = False


if __name__ == "__main__":
    unittest.main()
