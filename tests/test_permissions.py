"""Permission layer: the decision point, auto mode, the shell deny-list, allow rules, batch edits, and /undo."""
import contextlib
import json
import os
import tempfile
import unittest

from _harness import cli, ScriptedClient, RecordingApproval, sandbox, text, call, typed, keys, output

ENTER, SHIFT_TAB = "\r", "\x1b[Z"


@contextlib.contextmanager
def mode(m):
    cli.set_permission_mode(m)
    try:
        yield
    finally:
        cli.set_permission_mode("default")


def write_config(home, **data):
    with open(os.path.join(home, "config.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_config(home):
    with open(os.path.join(home, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


class DecideTests(unittest.TestCase):
    def test_default_mode_asks_for_everything(self):
        with sandbox():
            for kind in ("read_outside", "write", "shell", "write_instructions", "outlook", "clipboard_read",
                         "clipboard_write", "open_file", "dev_data", "something_new"):
                self.assertEqual(cli.decide(kind, "t", target="x", command="git status")[0], "ask", kind)

    def test_auto_mode_allows_reads_writes_and_shell_only(self):
        with sandbox(), mode("auto"):
            for kind in ("read_outside", "write", "shell"):
                self.assertEqual(cli.decide(kind, "t", target="x", command="git status"), ("allow", "auto_mode"), kind)
            for kind in ("write_instructions", "outlook", "clipboard_read", "clipboard_write", "open_file", "dev_data",
                         "something_new"):
                self.assertEqual(cli.decide(kind, "t", target="x")[0], "ask", kind)

    def test_deny_listed_shell_asks_even_in_auto_mode(self):
        with sandbox(), mode("auto"):
            verdict, note = cli.decide("shell", "run_powershell", command="Remove-Item .\\build -Recurse")
        self.assertEqual(verdict, "ask")
        self.assertIn("deletes files", note)

    def test_instruction_files(self):
        ws = os.path.realpath(tempfile.gettempdir())
        for rel in ("AGENT.md", "AGENT.md.", "AGENT.md . ", os.path.join("sub", "agent.md"), "system_prompt.md", os.path.join(".git", "hooks", "pre-commit"),
                    os.path.join(".github", "workflows", "ci.yml"), os.path.join(".eustace", "commands", "x.md")):
            self.assertTrue(cli._is_instruction_file(os.path.join(ws, rel)), rel)
        for rel in ("README.md", "agent.md.bak", os.path.join("docs", "github.md"), "gitignore.txt"):
            self.assertFalse(cli._is_instruction_file(os.path.join(ws, rel)), rel)


class ShellDenyListTests(unittest.TestCase):
    DENIED = [
        "Remove-Item x", "remove-item x", "REMOVE-ITEM x", "Re`move-Item x", "rm -r build", "del a.txt", "rd /s x",
        "ri x", "Get-ChildItem | Remove-Item", "[IO.File]::Delete('x')", "Clear-Content log.txt",
        "Stop-Computer", "shutdown /s", "reg add HKLM\\x", "Set-ItemProperty HKCU:\\x -Name a -Value 1",
        "net user bob /add", "Add-LocalGroupMember Administrators bob", "Invoke-WebRequest http://x -OutFile y",
        "iwr http://x", "curl http://x", "curl.exe -o x http://y", "certutil.exe -urlcache -f http://x y", "mshta.exe x", "powershell -enc AAAA", "pwsh -Command 'x'", "iex $s", "Invoke-Expression $s",
        "& $cmd", "& ('Rem'+'ove-Item') x", "cmd /c del x", "Start-Process notepad -Verb RunAs", "schtasks /create",
        "Register-ScheduledTask x", "Invoke-Command -ComputerName y {}", "Set-ExecutionPolicy Bypass", "icacls x /grant",
        "Stop-Process -Id 4", "taskkill /pid 4", "Invoke-Item x.lnk", "Copy-Item x \"$env:APPDATA\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\"",
        "Add-Content $PROFILE 'x'", "git push origin main", "git reset --hard HEAD~1", "git clean -fdx",
        "git checkout -- .", "git branch -D feature", "Set-Content AGENT.md 'x'", "Copy-Item x .git\\hooks\\pre-commit",
        ".\\build.ps1", "Set-Content run.bat ('a'+'b')", "& .\\deploy.cmd", "Get-ChildItem *.tmp `\n  | Remove-Item",
        # installers and package managers download and run outside code
        "npx skills add foo/bar", "npm install left-pad", "npm i x", "pnpm add x", "yarn dlx x", "pip install requests",
        "pip3.11 install x", "py -m pip install x", "python -m ensurepip", "uv pip install x", "pipx run x",
        "winget install foo", "choco install foo", "scoop install foo", "Install-Module PSReadLine",
        "Install-PSResource x", "dotnet tool install x", "git clone https://github.com/x/y",
        # inline code gets past every other pattern (urllib can do what iwr does)
        "python -c \"import urllib.request\"", "py -3.11 -c \"print(1)\"", "python3 -c print(1)",
        "python -X utf8 -c 1", "node -e 1", "node --eval \"1\"", "Get-Content a.py | python -",
        # a full path doesn't hide the command name
        "C:\\Windows\\System32\\curl.exe http://x", "C:\\Python314\\python.exe -c 1",
        "& \"C:\\Program Files\\Python314\\python.exe\" -c \"1\"", ".\\node_modules\\.bin\\npx foo",
        "\"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -Command x", "/usr/bin/curl x",
    ]
    ALLOWED = [
        "git status", "git log --oneline -5", "git diff", "pytest -q", "python -m unittest discover -s tests",
        "python cli.py --version", "Get-ChildItem", "dir", "Get-Content log.txt | Select-String error",
        "npm test", "Get-Content a.txt -Encoding utf8", "Where-Object { $_ -eq 1 }", "git commit -m 'remove dead code'",
        "Select-String -Path *.py -Pattern model", "Get-Date", "Measure-Object", "git add .",
        "npm run build", "python -m pytest -q", "python cli.py -c config", "pip list", "pip show rich",
        "py -3 build.py", "git pull", "git fetch origin", "node app.js", "Get-Content C:\\logs\\curl-output.txt",
        "Get-ChildItem C:\\tools\\rm\\x.txt", "Copy-Item .\\del\\a.txt b", "Get-Content docs/node.md",
        "python .\\tools\\py_helper.py",
    ]

    def setUp(self):
        cli._SESSION_WRITTEN_SCRIPTS.clear()

    def test_listed_commands_match(self):
        with sandbox():
            for cmd in self.DENIED:
                self.assertIsNotNone(cli.shell_deny_match(cmd), cmd)

    def test_everyday_commands_do_not_match(self):
        with sandbox():
            for cmd in self.ALLOWED:
                self.assertIsNone(cli.shell_deny_match(cmd), cmd)

    def test_user_patterns_add_to_the_list_and_bad_ones_are_ignored(self):
        with sandbox() as (_, home):
            write_config(home, shell_deny=["^npm publish", "([unclosed"])
            self.assertIn("your pattern", cli.shell_deny_match("npm publish"))
            self.assertIsNone(cli.shell_deny_match("npm test"))

    def test_running_a_script_eustace_wrote_always_asks(self):
        with sandbox(approval=RecordingApproval(default=True)), mode("auto"):
            self.assertIsNone(cli.shell_deny_match("python build.py"))
            cli.write_file("build.py", "print(1)")
            self.assertIn("build.py", cli.shell_deny_match("python build.py"))
            self.assertIn("build.py", cli.shell_deny_match("python .\\BUILD.PY --fast"))
            self.assertIsNone(cli.shell_deny_match("python other.py"))
        cli._SESSION_WRITTEN_SCRIPTS.clear()


class AutoModeToolTests(unittest.TestCase):
    def test_writes_and_outside_reads_run_without_a_prompt(self):
        approval = RecordingApproval(default=False)
        outside = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        outside.write("outside text")
        outside.close()
        try:
            with sandbox(approval=approval) as (ws, _), mode("auto"):
                approval.current_tool = "write_file"
                self.assertTrue(cli.write_file("out.txt", "hello").startswith("Success"))
                approval.current_tool = "read_file"
                self.assertEqual(cli.read_file(outside.name), "outside text")
                self.assertIn("✓ auto", output())
                self.assertEqual(approval.last_decision, "auto_mode")
        finally:
            os.remove(outside.name)
        self.assertEqual(approval.calls, [])

    def test_instruction_files_still_ask_in_auto_mode(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, _), mode("auto"):
            approval.current_tool = "write_file"
            result = cli.write_file("AGENT.md", "be evil")
            self.assertFalse(os.path.exists(os.path.join(ws, "AGENT.md")))
        self.assertIn("Operation aborted", result)
        self.assertEqual(len(approval.calls), 1)
        self.assertEqual(approval.always_ask, [True])  # not covered by per-turn "approve all" either

    def test_hard_blocks_hold_in_auto_mode(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval), mode("auto"):
            self.assertIn("Security Error", cli.write_file(".env", "X=1"))
            self.assertIn("Security Error", cli.write_file(os.path.join(cli.PROTECTED_ROOTS[0], "x.txt"), "x")
                          if cli.PROTECTED_ROOTS else "Security Error")
        self.assertEqual(approval.calls, [])

    def test_shell_permission_in_auto_mode(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval), mode("auto"):
            approval.current_tool = "run_powershell"
            self.assertTrue(cli._permission("shell", "Run PowerShell command", command="git status"))
            self.assertEqual(approval.calls, [])
            self.assertFalse(cli._permission("shell", "Run PowerShell command", command="git push"))
        self.assertEqual(len(approval.calls), 1)
        self.assertIn("deny-list", approval.calls[0][1])

    def test_auto_decision_is_written_to_the_audit_log(self):
        client = ScriptedClient([call("write_file", '{"path": "a.txt", "content": "x"}', id="c1")], [text("done")])
        with sandbox(client=client, approval=RecordingApproval(default=False)) as (ws, home), mode("auto"):
            cli.run_agent_turn([{"role": "system", "content": "s"}, {"role": "user", "content": "go"}])
            with open(os.path.join(home, "audit.jsonl"), encoding="utf-8") as f:
                record = json.loads(f.readlines()[-1])
        self.assertEqual((record["tool"], record["decision"], record["status"]), ("write_file", "auto_mode", "ok"))


class AllowRuleTests(unittest.TestCase):
    def test_shell_rules(self):
        with sandbox() as (_, home):
            write_config(home, allow=["run_powershell(git status)", "run_powershell(pytest*)"])
            m = lambda c: cli.matching_allow_rule("shell", "run_powershell", command=c)
            self.assertEqual(m("git status"), "run_powershell(git status)")
            self.assertEqual(m("GIT STATUS"), "run_powershell(git status)")
            self.assertIsNone(m("git status --porcelain"))  # exact rule
            self.assertEqual(m("pytest -q tests"), "run_powershell(pytest*)")
            for chained in ("pytest; Remove-Item x", "pytest | Out-File x", "pytest && evil", "pytest > out.txt",
                            "pytest $(evil)", "pytest `\nevil", "pytest\nevil"):
                self.assertIsNone(m(chained), chained)
            self.assertIsNone(cli.matching_allow_rule("shell", "read_file", command="git status"))

    def test_deny_list_beats_a_rule(self):
        with sandbox() as (_, home):
            write_config(home, allow=["run_powershell(git push*)"])
            self.assertEqual(cli.decide("shell", "run_powershell", command="git push")[0], "ask")

    def test_path_rules(self):
        with sandbox() as (ws, home):
            os.makedirs(os.path.join(ws, "out", "deep"))
            write_config(home, allow=["write_docx(./out/**)", f"read({ws.replace(os.sep, '/')}/notes.txt)", "garbage"])
            m = cli.matching_allow_rule
            self.assertTrue(m("write", "write_docx", target=os.path.join(ws, "out", "deep", "a.docx")))
            self.assertTrue(m("write", "write_docx", target=os.path.join(ws, "out")))
            self.assertIsNone(m("write", "write_docx", target=os.path.join(ws, "outside.docx")))
            self.assertIsNone(m("write", "write_docx", target=os.path.join(ws, "out2", "a.docx")))
            self.assertIsNone(m("write", "write_file", target=os.path.join(ws, "out", "a.txt")))
            self.assertTrue(m("read_outside", "read_file", target=os.path.join(ws, "notes.txt")))
            self.assertTrue(m("read_outside", "search_files", target=os.path.join(ws, "notes.txt")))  # any reading tool
            self.assertIsNone(m("read_outside", "read_file", target=os.path.join(ws, "notes.txt.bak")))
            self.assertEqual(cli.invalid_allow_rules(), ["garbage"])

    def test_a_read_rule_never_covers_a_write(self):
        with sandbox() as (ws, home):
            folder = ws.replace(os.sep, "/")
            write_config(home, allow=[f"read({folder}/**)"])
            self.assertTrue(cli.matching_allow_rule("read_outside", "write_pptx", target=os.path.join(ws, "t.potx")))
            self.assertIsNone(cli.matching_allow_rule("write", "write_pptx", target=os.path.join(ws, "deck.pptx")))
            self.assertIsNone(cli.matching_allow_rule("write", "read", target=os.path.join(ws, "deck.pptx")))
            self.assertEqual(cli.rule_candidate("read_outside", "write_pptx", target=os.path.join(ws, "t.potx")),
                             f"read({folder}/**)")

    def test_rules_never_cover_instruction_files(self):
        approval = RecordingApproval(default=False)
        with sandbox(approval=approval) as (ws, home):
            write_config(home, allow=["write_file(./**)"])
            approval.current_tool = "write_file"
            self.assertTrue(cli.write_file("notes.txt", "x").startswith("Success"))
            self.assertIn("Operation aborted", cli.write_file("AGENT.md", "x"))
        self.assertEqual(len(approval.calls), 1)
        self.assertEqual(approval.last_decision, "denied_user")

    def test_candidates(self):
        with sandbox() as (ws, _):
            self.assertEqual(cli.rule_candidate("shell", "run_powershell", command="git status"), "run_powershell(git status)")
            for cmd in ("git status; rm x", "dir *", "git push", "a\nb"):
                self.assertIsNone(cli.rule_candidate("shell", "run_powershell", command=cmd), cmd)
            target = os.path.join(ws, "sub", "a.txt")
            self.assertEqual(cli.rule_candidate("write", "write_file", target=target),
                             f"write_file({os.path.join(ws, 'sub').replace(os.sep, '/')}/**)")
            self.assertIsNone(cli.rule_candidate("outlook", "create_draft_email", target=target))
            # a saved candidate matches the request it was made from
            rule = cli.rule_candidate("write", "write_file", target=target)
            name, pattern = cli._RULE_RE.match(rule).groups()
            self.assertTrue(cli._rule_path_matches(pattern, target))

    def test_always_allow_from_the_menu_saves_the_rule(self):
        with sandbox() as (ws, home):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            cli.APPROVAL_HANDLER.current_tool = "write_file"
            # Yes / Yes for this turn / No / Yes, and always allow: the new option is last, so "3" is still "No"
            with keys("4", ENTER):
                self.assertTrue(cli.write_file("a.txt", "x").startswith("Success"))
            rules = read_config(home)["allow"]
            self.assertEqual(rules, [f"write_file({ws.replace(os.sep, '/')}/**)"])
            self.assertEqual(cli.APPROVAL_HANDLER.last_decision, "approved_rule_saved")
            cli.APPROVAL_HANDLER.reset_turn()
            self.assertTrue(cli.write_file("b.txt", "y").startswith("Success"))  # no keys left: must not prompt

    def test_no_still_sits_at_three(self):
        with sandbox() as (ws, home):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            cli.APPROVAL_HANDLER.current_tool = "write_file"
            with keys("3", ENTER, ENTER):
                self.assertIn("Operation aborted", cli.write_file("a.txt", "x"))
            self.assertFalse(os.path.exists(os.path.join(home, "config.json")))

    def test_typed_r_saves_the_rule(self):
        with sandbox() as (ws, home), typed("r"):
            cli.APPROVAL_HANDLER = cli.CLIApprovalHandler()
            cli.APPROVAL_HANDLER.current_tool = "run_powershell"
            self.assertTrue(cli._permission("shell", "Run PowerShell command", command="git status"))
            self.assertEqual(read_config(home)["allow"], ["run_powershell(git status)"])

    def test_permissions_command_lists_and_removes(self):
        with sandbox() as (_, home):
            write_config(home, allow=["run_powershell(git status)", "bad rule"], model="m")
            cli._cmd_permissions({}, "")
            self.assertIn("run_powershell(git status)", output())
            self.assertIn("not a valid rule", output())
            cli._cmd_permissions({}, "remove 1")
            self.assertEqual(read_config(home), {"allow": ["bad rule"], "model": "m"})
            cli._cmd_permissions({}, "remove 9")
            self.assertIn("No rule with that number", output())


class ModeSwitchTests(unittest.TestCase):
    def test_shift_tab_switches_auto_mode_in_the_input_box(self):
        with sandbox(), mode("default"):
            with keys(SHIFT_TAB, "hi", ENTER):
                self.assertEqual(cli._prompt_box("m · ~", "~0 tokens"), "hi")
            self.assertEqual(cli.PERMISSION_MODE, "auto")
            with keys(SHIFT_TAB, "hi", ENTER):
                cli._prompt_box("m · ~", "~0 tokens")
            self.assertEqual(cli.PERMISSION_MODE, "default")

    def test_auto_command(self):
        with sandbox(), mode("default"):
            cli._cmd_auto({}, "")
            self.assertEqual(cli.PERMISSION_MODE, "auto")
            cli._cmd_auto({}, "on")
            self.assertEqual(cli.PERMISSION_MODE, "auto")
            cli._cmd_auto({}, "off")
            self.assertEqual(cli.PERMISSION_MODE, "default")
            cli._cmd_auto({}, "maybe")
            self.assertEqual(cli.PERMISSION_MODE, "default")
            self.assertIn("Usage", output())


class ElevatedTests(unittest.TestCase):
    """Running as Administrator: auto mode is unavailable and every prompt says the action gets admin rights."""

    def test_detection_returns_a_bool(self):
        self.assertIsInstance(cli._process_is_elevated(), bool)

    def test_auto_mode_cannot_be_switched_on(self):
        with sandbox():
            cli.ELEVATED = True
            self.assertFalse(cli.set_permission_mode("auto"))
            self.assertEqual(cli.PERMISSION_MODE, "default")
            self.assertTrue(cli.set_permission_mode("default"))
            cli._cmd_auto({}, "on")
            self.assertEqual(cli.PERMISSION_MODE, "default")
            self.assertIn("isn't available while Eustace runs as Administrator", output())
            cli._cmd_permissions({}, "")
            self.assertIn("auto mode unavailable", output())

    def test_even_a_forced_auto_mode_asks(self):
        with sandbox():
            cli.ELEVATED = True
            cli.PERMISSION_MODE = "auto"  # however it got set, elevated means ask
            self.assertEqual(cli.decide("write", "write_file", target="x")[0], "ask")

    def test_shift_tab_does_not_switch(self):
        with sandbox():
            cli.ELEVATED = True
            with keys(SHIFT_TAB, "hi", ENTER):
                self.assertEqual(cli._prompt_box("m · ~", "~0 tokens"), "hi")
            self.assertEqual(cli.PERMISSION_MODE, "default")

    def test_prompts_say_admin_and_rule_approvals_are_marked(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, home):
            cli.ELEVATED = True
            approval.current_tool = "run_powershell"
            cli._permission("shell", "Run PowerShell command", command="git log")
            self.assertIn("this command gets admin rights", approval.calls[-1][1])
            approval.current_tool = "write_file"
            cli.write_file("a.txt", "x")
            self.assertIn("this action gets admin rights", approval.calls[-1][1])
            write_config(home, allow=["run_powershell(git status)"])
            approval.current_tool = "run_powershell"
            self.assertTrue(cli._permission("shell", "Run PowerShell command", command="git status"))
            self.assertIn("[admin]", output())
        self.assertEqual(len(approval.calls), 2)


class BatchEditTests(unittest.TestCase):
    def edit(self, data: bytes, approve=True, **kwargs):
        approval = RecordingApproval(default=approve)
        with sandbox(approval=approval) as (ws, _):
            path = os.path.join(ws, "f.txt")
            with open(path, "wb") as f:
                f.write(data)
            result = cli.edit_file("f.txt", **kwargs)
            with open(path, "rb") as f:
                return result, f.read(), approval

    def test_several_edits_one_approval(self):
        result, after, approval = self.edit(b"alpha\r\nbeta\r\ngamma\r\n",
                                            edits=[{"old_text": "alpha", "new_text": "ALPHA"}, {"old_text": "gamma", "new_text": "G1\nG2"}])
        self.assertTrue(result.startswith("Success"), result)
        self.assertEqual(after, b"ALPHA\r\nbeta\r\nG1\r\nG2\r\n")
        self.assertEqual(len(approval.calls), 1)
        self.assertIn("2 edits", approval.calls[0][0])

    def test_later_edits_see_earlier_ones(self):
        result, after, _ = self.edit(b"one two",
                                     edits=[{"old_text": "one", "new_text": "three"}, {"old_text": "three two", "new_text": "done"}])
        self.assertEqual(after, b"done")

    def test_one_bad_edit_writes_nothing(self):
        for bad, why in (({"old_text": "missing", "new_text": "x"}, "edit 2: Target text"),
                         ({"old_text": "b", "new_text": "b"}, "edit 2: 'old_text' and 'new_text' are identical"),
                         ({"old_text": "", "new_text": "x"}, "edit 2: 'old_text' is empty"),
                         ({"old_text": "a"}, "edit 2 must be an object")):
            result, after, approval = self.edit(b"a b a2", edits=[{"old_text": "b", "new_text": "B"}, bad])
            self.assertIn(why, result)
            self.assertEqual(after, b"a b a2")
            self.assertEqual(approval.calls, [])

    def test_argument_shapes(self):
        self.assertIn("not both", self.edit(b"a", old_text="a", new_text="b", edits=[{"old_text": "a", "new_text": "b"}])[0])
        self.assertIn("non-empty list", self.edit(b"a", edits=[])[0])
        self.assertIn("is empty", self.edit(b"a")[0])
        self.assertIn("at most", self.edit(b"a", edits=[{"old_text": "a", "new_text": "b"}] * 51)[0])

    def test_single_edit_still_works(self):
        result, after, _ = self.edit(b"x = 1\n", old_text="1", new_text="2")
        self.assertTrue(result.startswith("Success"))
        self.assertEqual(after, b"x = 2\n")


class UndoTests(unittest.TestCase):
    def setUp(self):
        self.saved_turn = cli._TURN["id"]

    def tearDown(self):
        cli._TURN["id"] = self.saved_turn
        cli._SESSION_WRITTEN_SCRIPTS.clear()

    def turn(self, fn):
        cli._TURN["id"] += 1
        fn()
        cli._finalize_checkpoints(cli._TURN["id"])

    def test_undo_restores_edits_and_deletes_created_files_turn_by_turn(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            with open(os.path.join(ws, "keep.txt"), "wb") as f:
                f.write(b"original\r\n")
            self.turn(lambda: (cli.edit_file("keep.txt", old_text="original", new_text="first"),
                               cli.edit_file("keep.txt", old_text="first", new_text="second")))
            self.turn(lambda: cli.write_file("new.txt", "made later"))
            self.assertIsNotNone(cli.undo_last_turn())
            self.assertFalse(os.path.exists(os.path.join(ws, "new.txt")))
            with open(os.path.join(ws, "keep.txt"), "rb") as f:
                self.assertEqual(f.read(), b"second\r\n")
            note = cli.undo_last_turn()
            with open(os.path.join(ws, "keep.txt"), "rb") as f:
                self.assertEqual(f.read(), b"original\r\n")  # the state before the turn, not between its two edits
            self.assertIn("restored", note)
            self.assertIsNone(cli.undo_last_turn())
            self.assertIn("Nothing to undo", output())

    def test_undo_warns_when_a_file_changed_afterwards_and_can_be_cancelled(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            self.turn(lambda: cli.write_file("a.txt", "eustace"))
            with open(os.path.join(ws, "a.txt"), "w", encoding="utf-8") as f:
                f.write("user edit")
            approval.answers = [False]
            self.assertIsNone(cli.undo_last_turn())
            self.assertIn("changed after Eustace wrote them", approval.calls[-1][1])
            self.assertTrue(os.path.exists(os.path.join(ws, "a.txt")))
            self.assertIsNotNone(cli.undo_last_turn())
            self.assertFalse(os.path.exists(os.path.join(ws, "a.txt")))
        self.assertTrue(approval.always_ask[-1])

    def test_undo_command_passes_a_note_to_the_next_message(self):
        approval = RecordingApproval(default=True)
        client = ScriptedClient([text("ok")])
        with sandbox(client=client, approval=approval):
            self.turn(lambda: cli.write_file("a.txt", "x"))
            state = {"messages": [{"role": "system", "content": "s"}]}
            cli._cmd_undo(state, "")
            self.assertIn("reverted", state["pending_note"])

    def test_office_files_round_trip(self):
        approval = RecordingApproval(default=True)
        with sandbox(approval=approval) as (ws, _):
            cli.write_docx("r.docx", "# One")
            with open(os.path.join(ws, "r.docx"), "rb") as f:
                first = f.read()
            self.turn(lambda: cli.write_docx("r.docx", "# Two"))
            cli.undo_last_turn()
            with open(os.path.join(ws, "r.docx"), "rb") as f:
                self.assertEqual(f.read(), first)

    def test_prune(self):
        with sandbox(approval=RecordingApproval(default=True)) as (ws, home):
            self.turn(lambda: cli.write_file("a.txt", "x" * 10))
            self.turn(lambda: cli.write_file("a.txt", "y" * 10))
            base = os.path.join(home, "checkpoints")
            self.assertEqual(len(os.listdir(os.path.join(base, cli.AUTOSAVE_NAME))), 2)
            cli.prune_checkpoints(max_total_bytes=0)
            self.assertFalse(os.listdir(base))
            self.turn(lambda: cli.write_file("b.txt", "z"))
            old = os.path.join(base, cli.AUTOSAVE_NAME, os.listdir(os.path.join(base, cli.AUTOSAVE_NAME))[0])
            os.utime(old, (0, 0))
            cli.prune_checkpoints()
            self.assertFalse(os.path.exists(old))


if __name__ == "__main__":
    unittest.main()
