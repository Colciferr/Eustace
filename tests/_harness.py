"""Shared test helpers: a scripted streaming client, an approval recorder, and an isolated Eustace home.

Not a test module itself (unittest discovery only picks up test*.py).
"""
import builtins
import contextlib
import copy
import io
import os
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace as NS

sys.argv = ["cli"]  # never let unittest flags reach cli's flag parser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cli  # noqa: E402


def text(s: str):
    """One streamed chunk carrying assistant text."""
    return NS(choices=[NS(delta=NS(content=s, tool_calls=None))])


_UNSET = object()


def call(name=None, args=None, id=None, index=_UNSET):
    """One streamed chunk carrying a tool-call delta. Pass index=None to imitate endpoints that omit it."""
    idx = 0 if index is _UNSET else index
    fn = NS(name=name, arguments=args)
    return NS(choices=[NS(delta=NS(content=None, tool_calls=[NS(index=idx, id=id, function=fn)]))])


class ScriptedClient:
    """Stands in for the OpenAI client. Each create() call consumes the next scripted round:
    a list of chunks to stream, an exception to raise, or a callable returning an iterable."""

    def __init__(self, *rounds):
        self.rounds = list(rounds)
        self.requests = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        if not self.rounds:
            raise AssertionError("ScriptedClient ran out of scripted rounds")
        r = self.rounds.pop(0)
        if isinstance(r, BaseException):
            raise r
        return iter(r() if callable(r) else r)


class RecordingApproval:
    """Stand-in for APPROVAL_HANDLER: records every request and answers from a queue (default: deny)."""

    def __init__(self, *answers, default=False):
        self.calls = []
        self.answers = list(answers)
        self.default = default
        self.always_ask = []  # parallel to calls: whether each request was exempt from "approve all"
        self.last_decision = "auto"
        self.current_tool = ""

    def request_approval(self, action_title, detail_text="", diff_text="", always_ask=False):
        self.calls.append((action_title, detail_text, diff_text))
        self.always_ask.append(always_ask)
        ok = self.answers.pop(0) if self.answers else self.default
        self.last_decision = "approved_user" if ok else "denied_user"
        return ok

    def reset_turn(self):
        pass


@contextlib.contextmanager
def sandbox(client=None, approval=None):
    """Points every Eustace-home path at a temp dir, chdirs into a temp workspace, and swaps in fakes.
    Console output is captured rather than printed. Yields (workspace_dir, home_dir)."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))
    home, ws = os.path.join(tmp, "home"), os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws)
    names = ("console", "EUSTACE_HOME", "CONFIG_PATH", "AUDIT_LOG_PATH", "DENY_LIST_PATH", "SESSIONS_DIR",
             "GLOBAL_PROMPT_PATH", "client", "APPROVAL_HANDLER", "check_user_abort", "ELEVATED", "PERMISSION_MODE",
             "AGENTS_SKILLS_HOME", "SHIPPED_PROMPT_PATH", "_UPDATE_HOME", "_console_font_face", "_console_is_hosted",
             "_install_star_font")
    saved = {n: getattr(cli, n) for n in names if hasattr(cli, n)}
    cwd = os.getcwd()
    cli.console = cli.Console(file=io.StringIO(), width=120)  # keep test logs readable; inspect via cli.console.file
    cli.EUSTACE_HOME = home
    cli.CONFIG_PATH = os.path.join(home, "config.json")
    cli.AUDIT_LOG_PATH = os.path.join(home, "audit.jsonl")
    cli.DENY_LIST_PATH = os.path.join(home, "deny_paths.txt")
    cli.SESSIONS_DIR = os.path.join(home, "sessions")
    cli.GLOBAL_PROMPT_PATH = os.path.join(home, "system_prompt.md")
    # an installed Eustace ships a persona under sys.prefix; tests must not see the machine's own copy or home
    cli.SHIPPED_PROMPT_PATH = os.path.join(home, "shipped", "system_prompt.md")
    cli._UPDATE_HOME = home
    cli.ELEVATED = False  # CI runners are often elevated; tests opt in to the elevated behavior explicitly
    cli.PERMISSION_MODE = "default"
    # the star depends on the font of the window the tests happen to run in; tests that want it say so (star_font)
    cli._console_font_face = lambda: "Consolas"
    cli._console_is_hosted = lambda: False
    cli._install_star_font = lambda brief=False: 0  # an update installs the font; never on the machine running tests
    cli._STAR.update(on=False, text=None, frame=0, drawn=0.0)
    cli.AGENTS_SKILLS_HOME = os.path.join(home, ".agents", "skills")
    cli.SKILLS.clear()
    cli._SKILL_SESSION_DENIED.clear()
    if client is not None:
        cli.client = client
    if approval is not None:
        cli.APPROVAL_HANDLER = approval
    os.chdir(ws)
    try:
        yield ws, home
    finally:
        os.chdir(cwd)
        for n, v in saved.items():
            setattr(cli, n, v)
        cli.SKILLS.clear()
        cli._STAR.update(on=False, text=None, frame=0, drawn=0.0)
        shutil.rmtree(tmp, ignore_errors=True)


@contextlib.contextmanager
def typed(*lines):
    """Feeds the given lines to input(), one per call; fails the test if more are requested."""
    queue = list(lines)

    def fake_input(prompt=""):
        if not queue:
            raise AssertionError("input() called more times than the test scripted")
        return queue.pop(0)

    saved = builtins.input
    builtins.input = fake_input
    try:
        yield queue
    finally:
        builtins.input = saved


def output() -> str:
    """Everything printed to the sandboxed console so far."""
    return cli.console.file.getvalue()


@contextlib.contextmanager
def keys(*chunks):
    """Runs the interactive UI (input box, menus) against scripted keystrokes instead of a console.
    Useful sequences: Enter "\\r", Down "\\x1b[B", Up "\\x1b[A", Alt+Enter "\\x1b\\r", Ctrl+C "\\x03", Ctrl+D "\\x04".
    If a test's keys never finish the prompt, a watchdog sends Ctrl+C so the test fails instead of hanging."""
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    saved_io, saved_enabled = dict(cli._UI_IO), cli._ui_enabled
    with create_pipe_input() as pipe:
        for chunk in chunks:
            pipe.send_text(chunk)
        watchdog = threading.Timer(5.0, lambda: pipe.send_text("\x03\x03\x03\x04"))
        watchdog.daemon = True
        watchdog.start()
        cli._UI_IO.update(input=pipe, output=DummyOutput())
        cli._ui_enabled = lambda: True
        try:
            yield pipe
        finally:
            watchdog.cancel()
            cli._UI_IO.clear()
            cli._UI_IO.update(saved_io)
            cli._ui_enabled = saved_enabled
