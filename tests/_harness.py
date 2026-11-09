"""Shared test helpers: a scripted streaming client, an approval recorder, and an isolated Eustace home.

Not a test module itself (unittest discovery only picks up test*.py).
"""
import contextlib
import copy
import os
import shutil
import sys
import tempfile
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
        self.approve_all_for_turn = False
        self.last_decision = "auto"
        self.current_tool = ""

    def request_approval(self, action_title, detail_text="", diff_text=""):
        self.calls.append((action_title, detail_text, diff_text))
        ok = self.answers.pop(0) if self.answers else self.default
        self.last_decision = "approved_user" if ok else "denied_user"
        return ok

    def reset_turn(self):
        pass


@contextlib.contextmanager
def sandbox(client=None, approval=None):
    """Points every Eustace-home path at a temp dir, chdirs into a temp workspace, and swaps in fakes.
    Yields (workspace_dir, home_dir)."""
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="eustace-test-"))
    home, ws = os.path.join(tmp, "home"), os.path.join(tmp, "ws")
    os.makedirs(home)
    os.makedirs(ws)
    names = ("EUSTACE_HOME", "CONFIG_PATH", "AUDIT_LOG_PATH", "DENY_LIST_PATH", "SESSIONS_DIR",
             "GLOBAL_PROMPT_PATH", "client", "APPROVAL_HANDLER", "check_user_abort")
    saved = {n: getattr(cli, n) for n in names if hasattr(cli, n)}
    cwd = os.getcwd()
    cli.EUSTACE_HOME = home
    cli.CONFIG_PATH = os.path.join(home, "config.json")
    cli.AUDIT_LOG_PATH = os.path.join(home, "audit.jsonl")
    cli.DENY_LIST_PATH = os.path.join(home, "deny_paths.txt")
    cli.SESSIONS_DIR = os.path.join(home, "sessions")
    cli.GLOBAL_PROMPT_PATH = os.path.join(home, "system_prompt.md")
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
        shutil.rmtree(tmp, ignore_errors=True)
