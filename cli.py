import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import difflib
import fnmatch
import hashlib
import importlib
import inspect
import io
import json
import msvcrt
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import types
import typing
import zipfile

import truststore
truststore.inject_into_ssl()

from dotenv import load_dotenv
from openai import OpenAI, Timeout as APITimeout
from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

VERSION = "1.7.0"

# When output is redirected to a file or pipe, Windows uses the ANSI code page, which cannot encode the symbols
# printed here; degrade to "?" instead of crashing mid-turn.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

# --- Command-line flags (read before any path or client is derived from them) ---
_ARGS = sys.argv[1:]
if "--help" in _ARGS or "-h" in _ARGS:
    print(
        "usage: eustace [--auto] [--devmode] [--version]\n\n"
        "  --auto      Start in auto mode: file reads, file writes and shell commands run without asking,\n"
        "              except shell commands on the deny-list. Shift+Tab or /auto switches it off.\n"
        "              Ignored when Eustace runs as Administrator (elevated).\n"
        "  --devmode   Use the PUBLIC Google Gemini API (GEMINI_API_KEY, config in ~/.eustace-dev)\n"
        "              for functional testing off DODIN. Synthetic test data only; never CUI/PII/PHI.\n"
        "  --version   Print the version and exit.\n"
    )
    raise SystemExit(0)
if "--version" in _ARGS:
    print(f"Eustace v{VERSION}")
    raise SystemExit(0)
DEV_MODE = "--devmode" in _ARGS
START_IN_AUTO_MODE = "--auto" in _ARGS

# Look for .env in C:\Users\<Username>\.eustace\.env  (dev mode: ~\.eustace-dev\.env)
EUSTACE_HOME = os.path.join(os.path.expanduser("~"), ".eustace-dev" if DEV_MODE else ".eustace")
global_env_path = os.path.join(EUSTACE_HOME, ".env")
if os.path.exists(global_env_path):
    load_dotenv(global_env_path)
else:
    # Fallback to the .env beside cli.py (find_dotenv anchors at this module, not the cwd)
    load_dotenv()

CONFIG_PATH = os.path.join(EUSTACE_HOME, "config.json")

# --- Backend selection ---
if DEV_MODE:
    BACKEND_NAME = "Gemini API (DEV)"
    BACKEND_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
    API_KEY_ENV = "GEMINI_API_KEY"
    DEFAULT_MODEL = os.environ.get("EUSTACE_DEV_MODEL", "").strip() or "gemini-3.1-flash-lite"
    AVAILABLE_MODELS = [
        (DEFAULT_MODEL, "Dev-mode default (override with EUSTACE_DEV_MODEL); /model lists live IDs"),
    ]
else:
    BACKEND_NAME = "GenAI.mil"
    BACKEND_BASE_URL = "https://api.genai.mil/v1"
    API_KEY_ENV = "STARK_API_KEY"
    AVAILABLE_MODELS = [
        ("gemini-3.5-flash-lite-withtools", "Fast & light daily driver (Default)"),
        ("gemini-3.8-flash-withtools",      "Balanced speed & stronger reasoning"),
        ("gemini-4-argon-withtools",        "Flagship model for complex analysis & code"),
    ]
    DEFAULT_MODEL = "gemini-3.5-flash-lite-withtools"

def _write_json_atomic(path: str, data) -> None:
    """Writes JSON through a temp file and os.replace, so a crash mid-write cannot leave a truncated file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_config() -> dict:
    """<EUSTACE_HOME>/config.json as a dict ({} when missing or unreadable)."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _update_config(**changes) -> None:
    """Merges the given keys into config.json; a failed write is not fatal (the setting just does not persist)."""
    try:
        _write_json_atomic(CONFIG_PATH, {**_read_config(), **changes})
    except OSError:
        pass


def load_saved_model() -> str:
    """The user's last selected model, or the backend default."""
    return str(_read_config().get("model") or "").strip() or DEFAULT_MODEL


def short_model_label(model_name: str) -> str:
    """Returns a compact badge name like 'gemini-4-argon' for UI headers."""
    return model_name.replace("-withtools", "")

MODEL = load_saved_model()
console = Console()


def _merge_env_line(path: str, key: str, value: str) -> None:
    """Sets KEY=value in a dotenv file, keeping every other line in place."""
    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if not ln.startswith(f"{key}=")]
    lines.append(f"{key}={value}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _clean_api_key(raw: str | None) -> str:
    """Keeps only printable, non-space ASCII; anything else (control codes, stray whitespace) cannot be part of a key."""
    return "".join(c for c in (raw or "") if 32 < ord(c) < 127)


def ensure_api_key() -> str:
    """Returns the backend API key, prompting and saving it to <EUSTACE_HOME>/.env if it is missing or malformed."""
    stored = (os.environ.get(API_KEY_ENV) or "").strip()
    if stored and _clean_api_key(stored) == stored:
        return stored

    console.print(f"[bold yellow]No valid API key found.[/bold yellow] Paste your {BACKEND_NAME} key ({API_KEY_ENV}, input hidden):")
    key = ""
    for _ in range(3):
        typed = console.input("> ", password=True)
        if "\x16" in typed:
            # The legacy console host delivers Ctrl+V as a literal 0x16 instead of pasting; fetch the clipboard
            # ourselves. The raw reader returns None on failure, so an error message can never be saved as a key.
            typed = _clipboard_text_raw()
            if typed is None:
                console.print("[yellow]Could not read the clipboard. Right-click to paste, or type the key.[/yellow]")
                continue
        key = _clean_api_key(typed)
        if key:
            break
        console.print("[yellow]That was empty. Try again.[/yellow]")
    if not key:
        raise SystemExit("An API key is required.")

    _merge_env_line(global_env_path, API_KEY_ENV, key)
    console.print(f"[dim]Saved to {escape(global_env_path)}[/dim]")
    return key



# The client is created lazily in main() so that importing this module (tests, --help) needs no key.
client = None


# Without an explicit timeout the SDK waits 10 minutes per attempt and retries twice; a stalled connection
# would then hang the prompt for half an hour, and on Windows a blocking socket read also swallows Ctrl+C.
API_CONNECT_TIMEOUT_SECONDS = 20.0
API_READ_TIMEOUT_SECONDS = 180.0   # between bytes; "thinking" models can take a while before the first token
API_MAX_RETRIES = 1


def _init_client() -> None:
    global client
    client = OpenAI(
        base_url=BACKEND_BASE_URL,
        api_key=ensure_api_key(),
        timeout=APITimeout(API_READ_TIMEOUT_SECONDS, connect=API_CONNECT_TIMEOUT_SECONDS),
        max_retries=API_MAX_RETRIES,
    )
    # The client holds the key from here on. Drop it from the process environment so nothing this process
    # launches (os.startfile handlers, PowerShell) can inherit it.
    for name in ("STARK_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(name, None)


class _StreamWorker:
    """Runs a streaming completion on a daemon thread so the main thread can honour ESC / Ctrl+C at once.

    The network call and chunk iteration happen off the main thread and chunks arrive through a queue; the
    main thread polls the queue in 100 ms slices, checking the ESC key between polls. cancel() stops the
    worker and closes the HTTP stream; an orphaned worker can never block process exit because it is a
    daemon thread.
    """

    def __init__(self, **create_kwargs):
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._stream = None
        self._kwargs = create_kwargs
        self.started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="eustace-stream", daemon=True)
        self._thread.start()

    def _run(self):
        try:
            self._stream = client.chat.completions.create(**self._kwargs)
            for chunk in self._stream:
                if self._stop.is_set():
                    break
                self._q.put(("chunk", chunk))
            self._q.put(("done", None))
        except BaseException as e:  # surfaced on the main thread
            self._q.put(("error", e))
        finally:
            self.close()

    def close(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    def cancel(self):
        self._stop.set()
        self.close()

    def __iter__(self):
        while True:
            check_user_abort()  # raises KeyboardInterrupt on ESC
            try:
                kind, payload = self._q.get(timeout=0.1)  # returns every 100 ms so a pending Ctrl+C is delivered
            except queue.Empty:
                continue
            if kind == "chunk":
                yield payload
            elif kind == "done":
                return
            else:
                raise payload


def _tool_call_extra(tc) -> dict | None:
    """Provider-specific payload attached to a streamed tool call, e.g. Gemini's
    extra_content.google.thought_signature. Gemini 3 rejects the follow-up request (400) unless the
    signature is echoed back verbatim on the assistant tool call, so it is carried through untouched.
    GenAI.mil sends nothing here, in which case this returns None and the message shape is unchanged."""
    extra = getattr(tc, "extra_content", None)
    if extra is None:
        extra = (getattr(tc, "model_extra", None) or {}).get("extra_content")
    return extra if isinstance(extra, dict) and extra else None


def _format_api_error(e: Exception) -> str:
    """One readable line for an API failure: HTTP status, the provider's message, and (for quota errors)
    the quota id and retry delay. Set EUSTACE_DEBUG=1 to append the full response (URL, headers, body)."""
    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    err = {}
    if isinstance(body, list) and body and isinstance(body[0], dict):   # Gemini: [{"error": {...}}]
        err = body[0].get("error") or {}
    elif isinstance(body, dict):                                         # OpenAI-style: {"error": {...}} or {...}
        err = body.get("error") or body
    if not isinstance(err, dict):
        err = {}
    msg = str(err.get("message") or "").strip()
    for cut in (" For more information", " * Quota exceeded", " Please refer to"):
        msg = msg.split(cut)[0].strip()
    parts = [f"API error {status}" if status else f"API error ({type(e).__name__})"]
    if msg:
        parts.append(msg[:300])
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        for v in d.get("violations") or []:
            if isinstance(v, dict) and v.get("quotaId"):
                parts.append(f"quota {v['quotaId']} = {v.get('quotaValue', '?')}")
        if d.get("retryDelay"):
            parts.append(f"retry after {d['retryDelay']}")
    if status == 429:
        parts.append("Switch models with /model" + (" or set EUSTACE_DEV_MODEL." if DEV_MODE else ", or wait and retry."))
    resp = getattr(e, "response", None)
    if not msg:
        # Nothing parseable: show the start of whatever the server actually sent (HTML error page, proxy text...)
        raw = ""
        try:
            raw = (resp.text if resp is not None else "") or ""
        except Exception:
            pass
        parts.append((raw.strip() or str(e))[:300])
    if resp is not None and os.environ.get("EUSTACE_DEBUG", "").strip() not in ("", "0"):
        try:
            parts.append(f"URL: {resp.url} | Headers: {dict(resp.headers)} | Body: {resp.text!r}")
        except Exception:
            pass
    return " | ".join(parts)


def _live_models() -> list[tuple[str, str]]:
    """Dev mode only: chat-capable model IDs discovered on the Gemini endpoint (Google IDs change often)."""
    if not DEV_MODE or client is None:
        return []
    try:
        found = []
        for m in client.models.list().data:
            mid = str(m.id).removeprefix("models/")
            low = mid.lower()
            if any(x in low for x in ("embedding", "imagen", "veo", "tts", "aqa", "image", "audio", "live", "robotics")):
                continue
            found.append(mid)
        return [(mid, "Discovered on Gemini API") for mid in sorted(set(found))]
    except Exception:
        return []

# =====================================================================
# SECURITY GUARDRAILS, AUDIT LOG, APPROVAL HANDLER
# =====================================================================

IGNORE_DIRS = {".venv", ".git", "__pycache__", "node_modules"}

# Expanded secrets, credentials, key stores, and raw mail archives the model is never allowed to read
SENSITIVE_PATTERNS = {
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.p7b", "*.der",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.psafe3", "*.ppk",
    "*.gpg", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    "*.kubeconfig", "*secret*", "*credentials*.json", "*token*.json",
    "login data*", "cookies*", "web data*", "*.pst", "*.ost", "ntds.dit",
    # Extension-less and tool-specific credential stores
    "credentials", ".gitconfig", ".boto", ".s3cfg", ".pgpass", ".my.cnf", ".vault-token", "hosts.yml",
    "id_dsa*", "*.rdp", "*.tfstate", "*.tfstate.*", "*.tfvars", "*.jks", "*.keystore",
    "winscp.ini", "local state", "consolehost_history.txt", "appsettings*.json", "web.config",
}

_HOME_DIR = os.path.expanduser("~")

# Well-known credential directories under the user profile: everything inside is off limits regardless of file name
SENSITIVE_DIRS = [
    os.path.normcase(os.path.realpath(os.path.join(_HOME_DIR, d)))
    for d in (
        ".aws", ".kube", ".docker", ".azure", ".ssh", ".gnupg",
        os.path.join(".config", "gh"),
        os.path.join("AppData", "Roaming", "GitHub CLI"),
        os.path.join("AppData", "Roaming", "Microsoft", "Windows", "PowerShell", "PSReadLine"),
    )
]

# Both Eustace homes are protected in both modes, so dev mode can never read the GenAI.mil key, sessions or audit log
PROTECTED_HOMES = [
    os.path.normcase(os.path.realpath(os.path.join(_HOME_DIR, d))) for d in (".eustace", ".eustace-dev")
]

AUDIT_LOG_PATH = os.path.join(EUSTACE_HOME, "audit.jsonl")
DENY_LIST_PATH = os.path.join(EUSTACE_HOME, "deny_paths.txt")

# Protected Health Information (PHI) patterns — strictly prohibited on GenAI.mil
PHI_FILENAME_PATTERNS = {
    "*medical_record*", "*hipaa*", "*patient*", "*af469*", "*af422*", "*dd2870*", "*pha_*"
}
PHI_CONTENT_REGEX = re.compile(
    r"\b(PROTECTED HEALTH INFORMATION|HIPAA|DD FORM 2870|AF FORM 469|AF FORM 422|"
    r"MEDICAL RECORD NUMBER|PATIENT DIAGNOSIS|TRICARE BENEFICIARY)\b",
    re.IGNORECASE,
)

# Folders the model is never allowed to write into
PROTECTED_ROOTS = [
    os.path.normcase(os.path.realpath(p)) for p in (
        os.environ.get("SystemRoot"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
        os.path.join(os.environ.get("APPDATA") or os.path.join(_HOME_DIR, "AppData", "Roaming"),
                     "Microsoft", "Windows", "Start Menu", "Programs", "Startup"),
        os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData",
                     "Microsoft", "Windows", "Start Menu", "Programs", "StartUp"),
    ) if p
]


def _is_under(target: str, root: str) -> bool:
    """True if target is root itself or lives anywhere beneath it (lexical; pass canonical paths)."""
    t = os.path.normcase(os.path.normpath(target))
    r = os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath([t, r]) == r
    except ValueError:  # different drives
        return False


# ---------------------------------------------------------------------
# Path canonicalization: every model-supplied path is reduced to the single
# identity the filesystem call will use before any guard looks at it.
# ---------------------------------------------------------------------

_DEVICE_PREFIXES = ("\\\\?\\", "\\\\.\\", "//?/", "//./")


def _canonical_path(raw: str) -> str:
    """Returns the real, resolved path for a model-supplied spelling, or raises ValueError.

    Rejects NT device prefixes (\\\\?\\, \\\\.\\), every UNC spelling (\\\\server\\share, incl. loopback admin
    shares and \\\\.\\pipe\\), and NTFS alternate data streams (':' after the drive). realpath then
    re-anchors 8.3 short names, directory junctions and symlinks. A result that still carries a device
    prefix (reserved names such as 'nul', or paths too long to canonicalize) is rejected. A result on a
    UNC share is accepted only when the caller used a mapped drive letter and the result stays under
    that drive's real root.
    """
    s = str(raw)
    if not s.strip():
        raise ValueError("empty path")
    if s.startswith(("\\\\", "//")) or s.startswith(_DEVICE_PREFIXES):
        raise ValueError("UNC and device-namespace paths (\\\\server\\share, \\\\?\\, \\\\.\\) are not permitted")
    absolute = os.path.abspath(s)
    drive, rest = os.path.splitdrive(absolute)
    if ":" in rest:
        raise ValueError("NTFS alternate data streams (':' inside the path) are not permitted")
    canon = os.path.realpath(absolute)
    if canon.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValueError("path is unsupported (reserved device name, or too long to canonicalize)")
    if canon.startswith("\\\\"):
        # Mapped network drive (H:\x resolves to \\server\share\x). Stay within the drive the caller used.
        if not (len(drive) == 2 and drive[1] == ":"):
            raise ValueError("path resolves to a network location")
        drive_root = os.path.realpath(drive + "\\")
        if not drive_root.startswith("\\\\") or not _is_under(canon, drive_root):
            raise ValueError("path resolves to a network location outside the mapped drive")
    return canon


def _resolve_model_path(raw: str) -> tuple[str | None, str | None]:
    """(canonical_path, None) or (None, 'Security Error: ...') for a model-supplied path."""
    try:
        return _canonical_path(raw), None
    except ValueError as e:
        return None, f"Security Error: path '{raw}' rejected: {e}."


def _is_link_or_junction(path: str) -> bool:
    """True only for NTFS symlinks and directory junctions (not OneDrive placeholders or AppExecLink stubs)."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) in (stat.IO_REPARSE_TAG_SYMLINK, stat.IO_REPARSE_TAG_MOUNT_POINT)


def _has_link_component(canon: str) -> bool:
    """True if the path or any ancestor (down to the drive root) is a symlink or junction."""
    curr = canon
    while True:
        if _is_link_or_junction(curr):
            return True
        parent = os.path.dirname(curr)
        if parent == curr:
            return False
        curr = parent


def _workspace_root() -> str:
    return os.path.realpath(os.getcwd())


def _visible_controls(text: str) -> str:
    """Makes every control character except newline and tab visible (\\xNN) so what is shown is what runs."""
    out = []
    for ch in str(text):
        o = ord(ch)
        if ch in "\n\t" or (0x20 <= o < 0x7F) or o >= 0xA0:
            out.append(ch)
        else:
            out.append(f"\\x{o:02x}")
    return "".join(out)


def _sanitize_for_display(text: str) -> str:
    """Normalizes CRLF then exposes any remaining control/escape characters before rendering model or tool text."""
    return _visible_controls(str(text).replace("\r\n", "\n"))


def _mark_of_the_web(path: str) -> None:
    """Best-effort Zone.Identifier (ZoneId=3) so Office opens model-authored documents in Protected View."""
    try:
        with open(path + ":Zone.Identifier", "wb") as f:
            f.write(b"[ZoneTransfer]\r\nZoneId=3\r\n")
    except OSError:
        pass


def _head_lines(text: str, limit: int) -> str:
    """The first `limit` lines of text, with an explicit count of what was left out. Approval previews use
    this so a cut-off preview never looks like the whole content."""
    lines = str(text).replace("\r\n", "\n").split("\n")
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[:limit]) + f"\n... (+{len(lines) - limit} more lines not shown)"


def _trusted_powershell() -> str | None:
    """Locates a PowerShell host by absolute path, never from the current directory."""
    candidates = []
    pf = os.environ.get("ProgramFiles")
    if pf:
        candidates.append(os.path.join(pf, "PowerShell", "7", "pwsh.exe"))
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    candidates.append(os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    for c in candidates:
        if os.path.isabs(c) and os.path.isfile(c):
            return c
    # Last resort: PATH lookup. shutil.which on Windows searches the cwd first unless
    # NoDefaultCurrentDirectoryInExePath is set, so refuse anything relative or inside the workspace.
    ws = _workspace_root()
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found and os.path.isabs(found) and not _is_under(os.path.realpath(found), ws):
            return found
    return None


_deny_cache: tuple = (None, None, frozenset())  # (path, mtime, patterns)


def _load_custom_deny_patterns() -> frozenset[str]:
    """Optional user/unit-defined deny patterns from ~/.eustace/deny_paths.txt. Called for every path checked
    (thousands of times per search), so the parsed file is reused until its modification time changes."""
    global _deny_cache
    try:
        mtime = os.stat(DENY_LIST_PATH).st_mtime_ns
    except OSError:
        return frozenset()
    if _deny_cache[:2] != (DENY_LIST_PATH, mtime):
        try:
            with open(DENY_LIST_PATH, "r", encoding="utf-8-sig") as f:
                lines = (line.strip() for line in f)
                patterns = frozenset(line for line in lines if line and not line.startswith("#"))
        except OSError:
            return frozenset()
        _deny_cache = (DENY_LIST_PATH, mtime, patterns)
    return _deny_cache[2]


def _is_sensitive_canon(canon: str) -> bool:
    """Lexical guard for an ALREADY canonical path: secrets patterns, deny list, credential dirs, Eustace homes."""
    name = os.path.basename(canon)
    norm_target = os.path.normcase(os.path.normpath(canon))
    all_patterns = SENSITIVE_PATTERNS | _load_custom_deny_patterns()
    for pat in all_patterns:
        if fnmatch.fnmatch(name.lower(), pat.lower()) or fnmatch.fnmatch(norm_target, pat.lower()):
            return True
    if any(_is_under(canon, d) for d in SENSITIVE_DIRS):
        return True
    return any(_is_under(canon, h) for h in PROTECTED_HOMES)


def _is_protected_path(target: str) -> bool:
    """True if the model must never write here: the Windows and Program Files trees, the Startup folders,
    and PowerShell profile scripts (the last two run code automatically at logon or shell start)."""
    if any(_is_under(target, root) for root in PROTECTED_ROOTS):
        return True
    folder, name = os.path.split(os.path.normpath(target).lower())
    return fnmatch.fnmatch(name, "*profile.ps1") and os.path.basename(folder) in ("windowspowershell", "powershell")


def _recheck_after_approval(canon: str, writing: bool = True) -> str | None:
    """Re-validates an approved destination immediately before the sink; returns an error string to abort."""
    try:
        again = _canonical_path(canon)
    except ValueError as e:
        return f"Security Error: destination no longer canonicalizes after approval ({e}); operation aborted."
    if os.path.normcase(again) != os.path.normcase(canon):
        return "Security Error: destination changed between approval and execution; operation aborted."
    if (writing and _is_protected_path(canon)) or _is_sensitive_canon(canon):
        return "Security Error: destination became protected after approval; operation aborted."
    if _has_link_component(canon):
        return "Security Error: destination passes through a symlink or junction; operation aborted."
    return None


def _guard_write(path: str, ext: tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """(canonical target, None) when the model may write to `path`, else (None, error for the model).
    `ext` lists the accepted extensions; the first is appended when the path has none of them. Every tool
    that writes goes through here, so they all apply the same checks to the same canonical path."""
    target, err = _resolve_model_path(path)
    if not err and ext and not target.lower().endswith(ext):
        target, err = _resolve_model_path(target + ext[0])  # resolve again: the final name may be a link
    if err:
        return None, err
    if _is_protected_path(target):
        return None, "Security Error: Writing to system folders is strictly prohibited."
    if _is_sensitive_canon(target):
        return None, f"Security Error: Writing to protected path '{path}' is prohibited."
    return target, None


def _approve_and_recheck(target: str, title: str, detail: str = "", diff: str = "", writing: bool = True) -> str | None:
    """Asks for permission, then re-validates the target right before the caller acts on it. A write is
    checkpointed for /undo once it is cleared. Returns None to go ahead or the message to hand back to the model."""
    if not _permission("write" if writing else "open_file", title, detail, diff, target=target):
        return "Operation aborted: User did NOT approve this action."
    err = _recheck_after_approval(target, writing)
    if err is None and writing:
        _checkpoint(target)
    return err


def _confirm_outside_workspace(canon: str, action: str) -> bool:
    """Reads inside the working directory stay approval-free; anything else needs permission like a write."""
    ws = _workspace_root()
    if _is_under(canon, ws):
        return True
    return _permission(
        "read_outside",
        f"{action} outside the working directory",
        f"[dim]Path:      {escape(_visible_controls(canon))}\nWorkspace: {escape(ws)}[/dim]",
        target=canon,
    )


def _check_phi_guardrail(target: str, content: str = "") -> str | None:
    """Returns a security error string if the filename or content appears to contain PHI."""
    name = os.path.basename(target).lower()
    if any(fnmatch.fnmatch(name, pat) for pat in PHI_FILENAME_PATTERNS):
        return (
            f"Security Policy Block: '{name}' matches a Protected Health Information (PHI) "
            "filename pattern. Processing PHI (such as medical records) is strictly prohibited."
        )
    if content and PHI_CONTENT_REGEX.search(content):
        return (
            f"Security Policy Block: '{name}' contains Protected Health Information (PHI) "
            "markers. While CUI and PII are authorized, processing PHI is strictly prohibited."
        )
    return None


_AUDIT_CONTENT_KEYS = {"content", "old_text", "new_text", "text", "html_body", "body"}


def _audit_digest(value) -> str:
    """Size and short hash of a value: enough to correlate log entries without storing the data itself."""
    if isinstance(value, str):
        blob, size = value, f"{len(value)} chars"
    else:
        blob, size = json.dumps(value, sort_keys=True, default=str), f"{type(value).__name__}, {len(value)} items"
    return f"<{size}, sha256:{hashlib.sha256(blob.encode('utf-8', errors='replace')).hexdigest()[:12]}>"


def log_audit_event(tool_name: str, args: dict, decision: str, result: str):
    """Appends a structured audit record to ~/.eustace/audit.jsonl.

    The log records what was done, not the data it touched: file contents, document values and tool output
    appear only as size + hash. Paths, commands, and error text are kept verbatim."""
    try:
        safe_args = {}
        for k, v in args.items():
            if isinstance(v, (dict, list)) or (isinstance(v, str) and (k in _AUDIT_CONTENT_KEYS or (len(v) > 200 and k != "command"))):
                safe_args[k] = _audit_digest(v)
            else:
                safe_args[k] = v

        res_str = str(result)
        failed = res_str.startswith(("Error", "Security", "Operation aborted"))
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "backend": BACKEND_NAME,
            "model": MODEL,
            "cwd": os.getcwd(),
            "tool": tool_name,
            "args": safe_args,
            "decision": decision,
            "status": "error" if failed else "ok",
            "result_preview": res_str[:200] if failed or res_str.startswith("Success") else _audit_digest(res_str),
        }
        os.makedirs(EUSTACE_HOME, exist_ok=True)
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def build_unified_diff(old_text: str, new_text: str, path: str) -> str:
    """Generates a unified diff string between old_text and new_text."""
    lines = list(difflib.unified_diff(
        old_text.splitlines(),
        new_text.splitlines(),
        fromfile=f"a/{os.path.basename(path)}",
        tofile=f"b/{os.path.basename(path)}",
        n=3,
        lineterm="",
    ))
    # Lines are compared without their endings (joining them with endings ran the last two together when the
    # text had no final newline), so say explicitly when the only difference left is that final newline.
    if old_text and new_text and old_text.endswith("\n") != new_text.endswith("\n"):
        lines.append("\\ Final newline " + ("removed" if old_text.endswith("\n") else "added"))
    return "\n".join(lines)


class CLIApprovalHandler:
    """Approval prompt with a diff preview: an arrow-key menu on a real console, a typed letter otherwise.
    "Approve all" covers further calls of the *same tool* for the rest of the turn; requests marked always_ask (shell commands, access outside the working directory, dev-mode data
    warnings, instruction files) are never covered by it. Callers go through _permission(), which decides first
    whether the prompt is needed at all (auto mode, allow rules)."""

    def __init__(self):
        self.approved_tools: set[str] = set()
        self.current_tool = ""   # set by the agent loop before each tool runs
        self.last_decision = "auto"
        self.last_feedback = ""  # what the user typed when denying with "tell Eustace what to do instead"
        self.rule_candidate = None  # set by _permission(): the allow rule "Yes, and always allow" would save

    def reset_turn(self):
        self.approved_tools.clear()
        self.last_decision = "auto"

    def request_approval(self, action_title: str, detail_text: str = "", diff_text: str = "", always_ask: bool = False) -> bool:
        # Bind the display to the bytes that will act: control characters (CR, ESC, VT, FF...) that rich
        # would silently drop or the terminal would interpret are made visible before anything is rendered.
        action_title = _visible_controls(action_title)
        if not always_ask and self.current_tool in self.approved_tools:
            self.last_decision = "approved_all_turn"
            console.print(f"  [dim green]✓ Auto-approved ({escape(self.current_tool)} this turn): {escape(action_title)}[/dim green]")
            return True

        self.last_feedback = ""
        rows = _diff_rows(_visible_controls(diff_text.replace("\r\n", "\n"))) if diff_text else []
        hidden = len(rows) - DIFF_PREVIEW_LINES
        width = max(console.width - 4, 40)
        if rows:
            body = _render_diff(rows[:DIFF_PREVIEW_LINES], width)
            if hidden > 0:
                body.append(f"\n... ({hidden} more diff lines; press V to view all of them)", style="dim")
        else:
            body = detail_text or escape(action_title)

        can_approve_all = not always_ask and bool(self.current_tool)
        rule = self.rule_candidate
        interactive = _ui_enabled()
        keys = "[bold yellow]\\[Y][/bold yellow] Approve   [bold yellow]\\[N][/bold yellow] Deny"
        if can_approve_all:
            keys += f"   [bold yellow]\\[A][/bold yellow] Approve all {escape(self.current_tool)} this turn"
        if rule:
            keys += f"   [bold yellow]\\[R][/bold yellow] Always allow {escape(_visible_controls(rule))}"
        if hidden > 0:
            keys += "   [bold yellow]\\[V][/bold yellow] View Full Diff"

        console.print()
        if rows and detail_text:
            console.print(f"  {detail_text}")
        console.print(
            Panel(
                body,
                title=f"[bold yellow]⚠️  Approval Required: {escape(action_title)}[/bold yellow]",
                subtitle=None if interactive else keys,
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

        while True:
            choice = (self._ask_menu(can_approve_all, hidden > 0, rule) if interactive
                      else self._ask_typed(can_approve_all, hidden > 0, rule))
            if choice != "view":
                break
            console.print(_render_diff(rows, max(console.width, 40)))

        if choice == "rule":
            saved = add_allow_rule(rule)
            console.print(f"  [dim green]✓ Approved, and {'saved' if saved else 'could not save'} the rule "
                          f"{escape(_visible_controls(rule))}[/dim green]")
            self.last_decision = "approved_rule_saved" if saved else "approved_user"
            return True
        if choice == "all":
            self.approved_tools.add(self.current_tool)
            self.last_decision = "approved_all_turn"
            return True
        if choice == "yes":
            self.last_decision = "approved_user"
            return True

        self.last_decision = "denied_user"
        return False

    def _ask_typed(self, can_approve_all: bool, can_view: bool, rule: str | None = None) -> str:
        """Plain typed prompt (no real console, EUSTACE_PLAIN=1, or prompt_toolkit unavailable)."""
        options = ["y", "n"] + (["a"] if can_approve_all else []) + (["r"] if rule else []) + (["v"] if can_view else [])
        console.print(f"  [bold yellow]Choice ({'/'.join(options)}) \\[default: N]:[/bold yellow] ", end="")
        # Whole-word answers only: stray typed-ahead text that merely starts with "y" must not approve
        answer = input().strip().lower()
        if answer in ("v", "view") and can_view:
            return "view"
        if answer in ("a", "all") and can_approve_all:
            return "all"
        if answer in ("r", "rule") and rule:
            return "rule"
        return "yes" if answer in ("y", "yes") else "no"

    def _ask_menu(self, can_approve_all: bool, can_view: bool, rule: str | None = None) -> str:
        """Arrow-key menu. Choosing "tell Eustace what to do instead" denies the action and records the user's
        words in last_feedback, which the agent loop passes to the model with the tool result."""
        options = [("yes", "Yes")]
        if can_approve_all:
            options.append(("all", f"Yes, and don't ask again for {self.current_tool} this turn"))
        options.append(("tell", "No, and tell Eustace what to do instead"))
        if rule:  # last, so the numbers of the existing choices never move (3 stays "No" where it was)
            options.append(("rule", f"Yes, and always allow {_visible_controls(rule)}"))
        choice = _ask_menu("Do you want to proceed?", options, allow_view=can_view)
        if choice == "tell":
            self.last_feedback = _ask_line("What should Eustace do instead? ")
            console.print(f"  [dim]✗ Denied{': ' + escape(_sanitize_for_display(self.last_feedback)) if self.last_feedback else ''}[/dim]")
            return "no"
        console.print({"yes": "  [dim green]✓ Approved[/dim green]", "all": f"  [dim green]✓ Approved, and for further {escape(self.current_tool)} calls this turn[/dim green]",
                       "view": "", None: "  [dim]✗ Denied[/dim]"}.get(choice, ""))
        return choice or "no"


DIFF_PREVIEW_LINES = 100

APPROVAL_HANDLER = CLIApprovalHandler()


# ---------------------------------------------------------------------
# PERMISSIONS: one decision point in front of the approval prompt.
#
# Every request has a kind. decide() says whether it may go ahead without a prompt (auto mode, an allow rule)
# or must ask. Hard blocks (protected and secrets paths, PHI, links, the post-approval recheck) run inside the
# tools before and after this and are never affected by it.
# ---------------------------------------------------------------------

ALWAYS_ASK_KINDS = {"read_outside", "shell", "dev_data", "write_instructions"}  # never covered by per-turn "approve all"
NEVER_AUTOMATIC_KINDS = {"write_instructions", "dev_data", "outlook", "clipboard_read", "clipboard_write", "open_file"}
AUTO_KINDS = {"read_outside", "write", "shell"}  # what auto mode approves, and what allow rules may cover

def _process_is_elevated() -> bool:
    """True when this process runs with an elevated (administrator) token. A non-elevated session of an admin
    account cannot use admin rights without a UAC prompt, so it does not count. Any failure reads as False."""
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        adv.OpenProcessToken.restype = wintypes.BOOL
        adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD)]
        adv.GetTokenInformation.restype = wintypes.BOOL
        token = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        try:
            elevation, size = wintypes.DWORD(0), wintypes.DWORD(0)
            ok = adv.GetTokenInformation(token, 20, ctypes.byref(elevation), ctypes.sizeof(elevation), ctypes.byref(size))  # TokenElevation
            return bool(ok) and bool(elevation.value)
        finally:
            k32.CloseHandle(token)
    except Exception:
        return False


# Running elevated, everything Eustace runs gets admin rights, so auto mode is unavailable and every prompt says so.
ELEVATED = _process_is_elevated()
PERMISSION_MODE = "auto" if START_IN_AUTO_MODE and not ELEVATED else "default"
ELEVATED_AUTO_REFUSAL = "Auto mode is off while running as Administrator"


def set_permission_mode(mode: str) -> bool:
    """Switches between "auto" and "default". Returns False (and stays in default) when auto mode is asked for
    in an elevated session."""
    global PERMISSION_MODE
    if mode == "auto" and ELEVATED:
        PERMISSION_MODE = "default"
        return False
    PERMISSION_MODE = "auto" if mode == "auto" else "default"
    return True


# Files that change Eustace's own instructions, or that run automatically later (git hooks, CI workflows), are
# always shown and asked about, in every mode.
_INSTRUCTION_FILE_NAMES = {"agent.md", "system_prompt.md"}
_INSTRUCTION_DIRS = {".eustace", ".eustace-dev", ".git", ".github", ".agents"}  # .agents holds skills


def _is_instruction_file(canon: str) -> bool:
    # Windows drops trailing dots and spaces from a name when it creates the file, so "AGENT.md." is AGENT.md
    parts = [p.rstrip(" .") for p in os.path.normpath(canon).lower().split(os.sep) if p.rstrip(" .")]
    return bool(parts) and (parts[-1] in _INSTRUCTION_FILE_NAMES or any(p in _INSTRUCTION_DIRS for p in parts[:-1]))


def _alias(name: str) -> str:
    """Regex for a short command name or alias (optionally with .exe) that is not part of a longer word or file
    name. A leading folder path is allowed, so C:\\Windows\\System32\\curl.exe matches like curl does."""
    return r"(?<![\w.\\/-])(?:[^\s\"'|;&(){}]*[\\/])?(?:" + name + r")(?:\.exe)?(?![\w.\\/:-])"


# (pattern, label). Matched case-insensitively against the command after line continuations and backticks are
# removed. A match never blocks a command; it only means the command always asks, even in auto mode.
SHELL_DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"remove-item|clear-content|clear-item|clear-recyclebin", "deletes files"),
    ("|".join(_alias(a) for a in ("rm", "del", "erase", "rd", "rmdir", "ri", "clc")), "deletes files"),
    (r"::delete|\.delete\(", "deletes files"),
    (r"format-volume|initialize-disk|clear-disk|stop-computer|restart-computer|" + _alias("shutdown")
     + "|" + _alias("bcdedit") + "|" + _alias("diskpart"), "changes the system"),
    (_alias(r"reg(\.exe)?") + r"\s|(set|new|remove|rename)-itemproperty|hklm:|hkcu:|registry::", "changes the registry"),
    (_alias(r"net(\.exe)?") + r"\s+(user|localgroup|group|accounts)|-localuser|-localgroupmember", "changes accounts"),
    (r"invoke-webrequest|invoke-restmethod|start-bitstransfer|bitsadmin|net\.webclient|system\.net\.http|"
     r"downloadstring|downloadfile|" + "|".join(_alias(a) for a in ("iwr", "irm", "curl", "wget", "certutil")),
     "downloads or uploads"),
    (_alias("npx|pnpx|pipx|winget|choco|scoop|nuget") + r"|" + _alias(r"npm|pnpm|yarn") + r"\s+(install|i|ci|add|exec|x|"
     r"dlx|update|up|upgrade)\b|" + _alias(r"pip[0-9.]*") + r"\s+(install|download)\b|(?<![\w-])-m\s+(pip|ensurepip)\b|"
     + _alias("uv") + r"\s+(pip|tool|add|run)\b|(install|save|update)-(module|package|script|psresource)|"
     r"dotnet\s+tool\s+install|git\s+clone", "installs or downloads software"),
    (_alias(r"python[0-9.]*|pythonw|py|node") + r"[\"']?(\s+-[XW]\s*\S+|\s+-[\w.:-]+)*?\s+(-c|-e|-p|--eval|--print|-)"
     r"(?=\s|$|[\"'])", "runs inline code"),
    (r"(?<![\w-])-e(c|n|nc|ncodedcommand)?\s|frombase64string|invoke-expression|" + _alias("iex") + r"|\[scriptblock\]|"
     r"invokecommand|add-type|&\s*[\$(]|(?<![\w.])\.\s*[\$(]", "hides what runs"),
    ("|".join(_alias(a) for a in (r"(powershell|pwsh)(\.exe)?", r"cmd(\.exe)?", "mshta", "rundll32", "regsvr32",
                                  "wscript", "cscript", "msiexec")), "starts another interpreter"),
    (r"-verb\s+runas|" + _alias("runas"), "runs elevated"),
    (r"schtasks|-scheduledtask|new-service|set-service|" + _alias(r"sc(\.exe)?") + r"\s+(create|config|delete)",
     "creates tasks or services"),
    (r"invoke-command|enter-pssession|new-pssession|" + _alias("icm"), "runs on other computers"),
    (r"set-executionpolicy|set-mppreference|add-mppreference", "changes security settings"),
    (r"icacls|takeown|set-acl", "changes permissions"),
    (r"stop-process|taskkill|" + _alias("kill"), "stops processes"),
    (r"invoke-item|" + _alias("ii"), "opens files with their default program"),
    # COM reaches Outlook (mail, calendars) and the other Office programs without the Outlook tools' own approvals
    (r"comobject|new-object\b[^|;\n]*?\s-c(o(m(o(b(j(e(c(t)?)?)?)?)?)?)?)?(?![\w-])|outlook\.application|"
     r"gettypefromprogid|getactiveobject|activator\]|createobject|getobject\(", "drives another program through COM"),
    (r"startup|\$profile|currentversion\\run", "touches startup locations"),
    (r"git\s+(push|clean)|git\s+reset\s+--hard|git\s+checkout\s+--\s|git\s+branch\s+-d", "changes git history or remotes"),
    (r"agent\.md|system_prompt\.md|\.git[\\/]|\.github|\.eustace|\.agents[\\/]",
     "touches Eustace's instruction files, skills or git internals"),
    (r"[\w-]\.(ps1|psm1|bat|cmd|vbs|vbe|wsf|hta)(?![\w.-])", "runs or writes a script file"),
)
_SHELL_DENY_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in SHELL_DENY_PATTERNS]

# File types that run as code: a command that runs one Eustace wrote this session always asks, so auto mode
# cannot be used to write a script around the deny-list and then run it.
_SCRIPT_EXTENSIONS = (".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".py", ".pyw", ".js", ".vbs", ".wsf", ".hta", ".sh", ".exe")
_SESSION_WRITTEN_SCRIPTS: set[str] = set()


def _normalize_command(command: str) -> str:
    """The command as the deny-list sees it: continuations joined and backticks dropped (PowerShell lets a
    backtick sit inside a word, so Re`move-Item runs Remove-Item)."""
    return re.sub(r"`\s*\n", " ", str(command)).replace("`", "")


def _user_shell_deny() -> list[tuple[re.Pattern, str]]:
    """Extra deny patterns (regular expressions) from config.json "shell_deny"; invalid ones are skipped."""
    out = []
    for pat in _config_list("shell_deny"):
        try:
            out.append((re.compile(pat, re.IGNORECASE), f"your pattern {pat}"))
        except re.error:
            continue
    return out


def shell_deny_match(command: str) -> str | None:
    """Why this command must always ask, or None. The built-in list cannot be shortened from config."""
    norm = _normalize_command(command)
    for rx, label in _SHELL_DENY_COMPILED + _user_shell_deny():
        m = rx.search(norm)
        if m:
            return f"{label}: {m.group(0).strip()}"
    low = norm.lower()
    for name in _SESSION_WRITTEN_SCRIPTS:
        if re.search(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])", low):  # a path may come before it
            return f"runs a file Eustace wrote this session: {name}"
    return None


# --- Allow rules: entries in config.json "allow". run_powershell(<command>) covers a command, read(<path>) covers
# reading by any tool outside the working directory, and <write tool>(<path>) covers that tool's writes. A read
# rule never covers a write, even by a tool that also reads (write_pptx reading its template, for example).
READ_RULE_NAME = "read"

_RULE_RE = re.compile(r"^([a-z_]+)\((.*)\)$", re.DOTALL)
# A shell rule never matches a command that chains, pipes, redirects, or substitutes another command
_SHELL_RULE_FORBIDDEN = re.compile(r"[;|&\n\r`<>]|[$@]\(")


def _config_list(key: str) -> list[str]:
    value = _read_config().get(key, [])
    return [x.strip() for x in value if isinstance(x, str) and x.strip()] if isinstance(value, list) else []


def get_allow_rules() -> list[str]:
    return [r for r in _config_list("allow") if _RULE_RE.match(r)]


def invalid_allow_rules() -> list[str]:
    return [r for r in _config_list("allow") if not _RULE_RE.match(r)]


def add_allow_rule(rule: str) -> bool:
    """Saves a rule (once). Returns False if config.json could not be written."""
    rules = _config_list("allow")
    if rule not in rules:
        rules.append(rule)
    try:
        _write_json_atomic(CONFIG_PATH, {**_read_config(), "allow": rules})
        return True
    except OSError:
        return False


def remove_allow_rule(index: int) -> str | None:
    """Removes the index-th (1-based) entry of "allow" as listed by /permissions; returns it, or None."""
    rules = _config_list("allow")
    if not 1 <= index <= len(rules):
        return None
    removed = rules.pop(index - 1)
    _update_config(allow=rules)
    return removed


def _rule_path_matches(pattern: str, target: str) -> bool:
    """`folder/**` covers everything under folder; anything else must name the exact path. A leading ./ is
    relative to the working directory at the time of the call."""
    p = pattern.strip().replace("/", os.sep)
    subtree = p.endswith(os.sep + "**")
    if subtree:
        p = p[:-3]
    if not p or "*" in p or "?" in p:
        return False
    base = os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(p)))
    if subtree:
        return _is_under(target, base)
    return os.path.normcase(os.path.normpath(target)) == os.path.normcase(base)


def _shell_rule_matches(pattern: str, command: str) -> bool:
    """Exact command, or a prefix when the rule ends in *. Chained or redirected commands never match."""
    cmd, pat = command.strip(), pattern.strip()
    if not cmd or not pat or _SHELL_RULE_FORBIDDEN.search(cmd):
        return False
    if pat.endswith("*"):
        return cmd.casefold().startswith(pat[:-1].casefold())
    return cmd.casefold() == pat.casefold()


def _rule_name_for(kind: str, tool_name: str) -> str:
    return READ_RULE_NAME if kind == "read_outside" else tool_name


def matching_allow_rule(kind: str, tool_name: str, target: str | None = None, command: str | None = None) -> str | None:
    if kind not in AUTO_KINDS or not tool_name:
        return None
    wanted = _rule_name_for(kind, tool_name)
    if kind == "write" and wanted == READ_RULE_NAME:
        return None
    for rule in get_allow_rules():
        name, pattern = _RULE_RE.match(rule).groups()
        if name != wanted:
            continue
        if kind == "shell" and command is not None and _shell_rule_matches(pattern, command):
            return rule
        if kind != "shell" and target and _rule_path_matches(pattern, target):
            return rule
    return None


def rule_candidate(kind: str, tool_name: str, target: str | None = None, command: str | None = None) -> str | None:
    """The rule "Yes, and always allow" would save: the exact command, or the containing folder."""
    if kind not in AUTO_KINDS or not tool_name:
        return None
    if kind == "shell":
        cmd = (command or "").strip()
        if not cmd or len(cmd) > 300 or cmd.endswith("*") or _SHELL_RULE_FORBIDDEN.search(cmd) or shell_deny_match(cmd):
            return None
        return f"{tool_name}({cmd})"
    if not target:
        return None
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    return f"{_rule_name_for(kind, tool_name)}({folder.replace(os.sep, '/')}/**)"


def decide(kind: str, tool_name: str = "", target: str | None = None, command: str | None = None) -> tuple[str, str]:
    """("allow", reason) when the request may go ahead without a prompt, else ("ask", note). Unknown kinds ask."""
    if kind in NEVER_AUTOMATIC_KINDS or kind not in AUTO_KINDS:
        return "ask", ""
    if kind == "shell":
        hit = shell_deny_match(command or "")
        if hit:
            return "ask", f"deny-list ({hit})"
    if PERMISSION_MODE == "auto" and not ELEVATED:
        return "allow", "auto_mode"
    rule = matching_allow_rule(kind, tool_name, target, command)
    if rule:
        return "allow", f"allow_rule:{rule}"
    return "ask", ""


def _permission(kind: str, title: str, detail: str = "", diff: str = "", target: str | None = None,
                command: str | None = None) -> bool:
    """The single entry point tools use to ask for permission: decides, then prompts only when needed."""
    handler = APPROVAL_HANDLER
    tool_name = getattr(handler, "current_tool", "") or ""
    if kind == "write" and target and _is_instruction_file(target):
        kind = "write_instructions"
    verdict, reason = decide(kind, tool_name, target, command)
    if verdict == "allow":
        handler.last_decision = reason
        how = "auto" if reason == "auto_mode" else f"allowed by rule {reason.split(':', 1)[1]}"
        what = f"{title}: {command}" if command else title if not target or target in title else f"{title} ({target})"
        admin = " [bold red]\\[admin][/bold red]" if ELEVATED else ""
        console.print(f"  [dim green]✓ {escape(_visible_controls(how))}: {escape(_visible_controls(what))}[/dim green]{admin}")
        return True
    if reason:
        note = f"[bold yellow]Always asks: matches the shell {escape(_visible_controls(reason))}[/bold yellow]"
        detail = f"{detail}\n{note}" if detail else note
    if ELEVATED:
        note = f"[bold red]Runs as Administrator: this {'command' if kind == 'shell' else 'action'} gets admin rights.[/bold red]"
        detail = f"{detail}\n{note}" if detail else note
    try:
        handler.rule_candidate = None if reason else rule_candidate(kind, tool_name, target, command)
    except AttributeError:
        pass
    try:
        return handler.request_approval(title, detail_text=detail, diff_text=diff, always_ask=kind in ALWAYS_ASK_KINDS)
    finally:
        try:
            handler.rule_candidate = None
        except AttributeError:
            pass


# ---------------------------------------------------------------------
# CHECKPOINTS (/undo): the bytes of every file a write tool is about to change, saved per turn.
# ---------------------------------------------------------------------

_TURN = {"id": 0}  # incremented at the start of each agent turn
MAX_CHECKPOINT_FILE_BYTES = 100 * 1024 * 1024
CHECKPOINT_MAX_AGE_DAYS = 7
CHECKPOINT_MAX_TOTAL_BYTES = 200 * 1024 * 1024


def _checkpoints_base() -> str:
    return os.path.join(EUSTACE_HOME, "checkpoints")


def _checkpoints_root() -> str:
    """This run's checkpoints (one folder per Eustace run, named like its autosave)."""
    return os.path.join(_checkpoints_base(), AUTOSAVE_NAME)


def _turn_dir(turn: int) -> str:
    return os.path.join(_checkpoints_root(), f"turn_{turn:05d}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest(turn_dir: str) -> dict | None:
    try:
        with open(os.path.join(turn_dir, "manifest.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) and isinstance(data.get("files"), list) else None
    except (OSError, ValueError):
        return None


def _checkpoint(target: str) -> None:
    """Saves target's current bytes (or the fact that it does not exist yet) before a write tool changes it.
    Only the first write to a file in a turn is recorded, which is the state /undo should return to. Failure
    to save is reported, never fatal."""
    if target.lower().endswith(_SCRIPT_EXTENSIONS):
        _SESSION_WRITTEN_SCRIPTS.add(os.path.basename(target).lower())
    if os.path.isdir(target):
        return
    turn_dir = _turn_dir(_TURN["id"])
    try:
        manifest = _load_manifest(turn_dir) or {
            "turn": _TURN["id"], "created": datetime.now(timezone.utc).isoformat(), "files": []}
        key = os.path.normcase(target)
        if any(os.path.normcase(f.get("path", "")) == key for f in manifest["files"]):
            return
        entry = {"path": target, "existed": os.path.isfile(target)}
        if entry["existed"]:
            if os.path.getsize(target) > MAX_CHECKPOINT_FILE_BYTES:
                raise OSError(f"larger than {MAX_CHECKPOINT_FILE_BYTES // (1024 * 1024)} MB")
            data = _read_bytes_or_none(target)
            if data is None:
                raise OSError("could not read it")
            entry["blob"] = f"{len(manifest['files']):04d}.bin"
            entry["sha256_before"] = _sha256(data)
            os.makedirs(turn_dir, exist_ok=True)
            with open(os.path.join(turn_dir, entry["blob"]), "wb") as f:
                f.write(data)
        manifest["files"].append(entry)
        _write_json_atomic(os.path.join(turn_dir, "manifest.json"), manifest)
    except (OSError, ValueError) as e:
        console.print(f"  [yellow]Could not save an undo checkpoint for {escape(target)} ({escape(str(e))}); "
                      "/undo will not restore this file.[/yellow]")


def _finalize_checkpoints(turn: int) -> None:
    """Records what each checkpointed file looks like after the turn, so /undo can tell when it was changed
    again afterwards."""
    turn_dir = _turn_dir(turn)
    manifest = _load_manifest(turn_dir)
    if not manifest:
        return
    for entry in manifest["files"]:
        data = _read_bytes_or_none(entry["path"]) if os.path.isfile(entry["path"]) else None
        entry["sha256_after"] = _sha256(data) if data is not None else None
    try:
        _write_json_atomic(os.path.join(turn_dir, "manifest.json"), manifest)
    except OSError:
        pass


def _latest_checkpoint_turn() -> tuple[str, dict] | None:
    root = _checkpoints_root()
    try:
        names = sorted((n for n in os.listdir(root) if re.fullmatch(r"turn_\d{5}", n)), reverse=True)
    except OSError:
        return None
    for name in names:
        manifest = _load_manifest(os.path.join(root, name))
        if manifest and manifest["files"]:
            return os.path.join(root, name), manifest
    return None


def undo_last_turn() -> str | None:
    """Restores the files the most recent writing turn changed, after one confirmation. Returns a note for
    the model describing what was reverted, or None when nothing was undone."""
    found = _latest_checkpoint_turn()
    if not found:
        console.print("[dim yellow]Nothing to undo: no file changes from Eustace are on record for this run.[/dim yellow]")
        return None
    turn_dir, manifest = found
    lines, changed_since = [], 0
    for entry in reversed(manifest["files"]):
        now = _read_bytes_or_none(entry["path"]) if os.path.isfile(entry["path"]) else None
        now_sha = _sha256(now) if now is not None else None
        drifted = "sha256_after" in entry and now_sha != entry["sha256_after"]
        changed_since += drifted
        action = "restore" if entry["existed"] else "delete (created that turn)" if now is not None else "already gone"
        lines.append(f"{action:<26} {entry['path']}" + ("   [changed since; those changes will be lost]" if drifted else ""))
    detail = "[dim]" + escape(_visible_controls("\n".join(lines))) + "[/dim]"
    if changed_since:
        detail += f"\n[bold yellow]{changed_since} file(s) changed after Eustace wrote them.[/bold yellow]"
    previous_tool = getattr(APPROVAL_HANDLER, "current_tool", "")
    APPROVAL_HANDLER.current_tool = ""
    try:
        ok = APPROVAL_HANDLER.request_approval(f"Undo file changes from turn {manifest.get('turn', '?')}",
                                               detail_text=detail, always_ask=True)
    finally:
        APPROVAL_HANDLER.current_tool = previous_tool
    if not ok:
        console.print("[dim]Undo cancelled.[/dim]")
        return None

    done, failed = [], []
    for entry in reversed(manifest["files"]):
        path = entry["path"]
        try:
            if _has_link_component(path) or _is_protected_path(path) or _is_sensitive_canon(path):
                raise OSError("the location is now protected or passes through a link")
            if entry["existed"]:
                with open(os.path.join(turn_dir, entry["blob"]), "rb") as f:
                    data = f.read()
                if _sha256(data) != entry.get("sha256_before"):
                    raise OSError("the saved copy is damaged")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.{os.getpid()}.undo.tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                done.append(f"restored {path}")
            elif os.path.isfile(path):
                os.remove(path)
                done.append(f"deleted {path}")
        except OSError as e:
            failed.append(f"{path}: {e}")
    if failed:
        # Keep the checkpoint so the files that could not be restored can be tried again after fixing the cause
        manifest["files"] = [e for e in manifest["files"] if any(e["path"] in f for f in failed)]
        try:
            _write_json_atomic(os.path.join(turn_dir, "manifest.json"), manifest)
        except OSError:
            pass
    else:
        shutil.rmtree(turn_dir, ignore_errors=True)
    for line in done:
        console.print(f"  [green]✓ {escape(_visible_controls(line))}[/green]")
    for line in failed:
        console.print(f"  [bold red]✗ {escape(_visible_controls(line))}[/bold red]")
    if not done:
        return None
    return "The user ran /undo, which reverted these file changes: " + "; ".join(done) + "."


def prune_checkpoints(max_age_days: int = CHECKPOINT_MAX_AGE_DAYS, max_total_bytes: int = CHECKPOINT_MAX_TOTAL_BYTES) -> None:
    """Deletes turn checkpoints older than max_age_days, then the oldest ones until the rest fit in max_total_bytes."""
    base = _checkpoints_base()
    try:
        runs = [os.path.join(base, d) for d in os.listdir(base)]
    except OSError:
        return
    turns = []
    for run in runs:
        try:
            for name in os.listdir(run):
                path = os.path.join(run, name)
                size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
                turns.append((os.path.getmtime(path), size, path))
        except OSError:
            continue
    cutoff = time.time() - max_age_days * 86400
    turns.sort()
    total = sum(t[1] for t in turns)
    for mtime, size, path in turns:
        if mtime < cutoff or total > max_total_bytes:
            shutil.rmtree(path, ignore_errors=True)
            total -= size
    for run in runs:
        try:
            if not os.listdir(run):
                os.rmdir(run)
        except OSError:
            pass


# ---------------------------------------------------------------------
# Tool registry: @tool is the single place a tool is declared. The model-facing schema is derived from the
# function itself (name, docstring, annotations, defaults) plus the per-parameter descriptions given here.
# Parameters without a description stay internal: they are not in the schema and the model cannot pass them.
# ---------------------------------------------------------------------

TOOLS: list[dict] = []                    # schemas sent to the model, in declaration order
AVAILABLE_FUNCTIONS: dict = {}            # tool name -> function
TOOL_FRIENDLY_LABELS: dict[str, str] = {} # tool name -> short progress label
_TOOL_SCHEMAS: dict[str, dict] = {}       # tool name -> its JSON-schema "parameters" object
_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object"}


def _json_type(annotation) -> dict:
    """JSON-schema fragment for a parameter annotation: X | None is treated as X, list[X] as an array of X."""
    origin, args = typing.get_origin(annotation), [a for a in typing.get_args(annotation) if a is not type(None)]
    if origin in (typing.Union, types.UnionType):
        return _json_type(args[0])
    if annotation is list or origin is list:
        return {"type": "array", "items": _json_type(args[0])} if args else {"type": "array"}
    return {"type": _JSON_TYPES[origin or annotation]}


def tool(label: str, **param_docs):
    """Registers the decorated function as a model-callable tool. `label` is the progress text shown while it
    runs; each keyword documents one parameter and exposes it to the model."""
    def register(fn):
        sig, hints = inspect.signature(fn), typing.get_type_hints(fn)
        unknown = set(param_docs) - set(sig.parameters)
        if unknown:
            raise TypeError(f"@tool on {fn.__name__}: no such parameter(s) {sorted(unknown)}")
        params = {
            "type": "object",
            # a plain string documents the parameter; a dict is a complete schema for it (nested structures)
            "properties": {n: d if isinstance(d, dict) else {**_json_type(hints[n]), "description": d}
                           for n, d in param_docs.items()},
            "required": [n for n, p in sig.parameters.items() if n in param_docs and p.default is p.empty],
        }
        description = " ".join(inspect.getdoc(fn).split())
        TOOLS.append({"type": "function", "function": {"name": fn.__name__, "description": description, "parameters": params}})
        _TOOL_SCHEMAS[fn.__name__] = params
        AVAILABLE_FUNCTIONS[fn.__name__] = fn
        TOOL_FRIENDLY_LABELS[fn.__name__] = label
        return fn
    return register


# =====================================================================
# LOCAL FILE SYSTEM TOOLS (plaintext, Excel, PDF, Word, PowerPoint)
# =====================================================================

@tool("Listing folder", path="Relative or absolute directory path to list (default is '.').")
def list_directory(path: str = ".") -> str:
    """List files and folders in a directory on the user's local PC. Defaults to current working directory '.'."""
    try:
        target, err = _resolve_model_path(path)
        if err:
            return err
        if not os.path.exists(target):
            return f"Error: Path does not exist: {path}"
        if not os.path.isdir(target):
            return f"Error: Path is not a directory: {path}"
        if _is_sensitive_canon(target):
            return f"Security Error: '{path}' is a protected location and will not be listed."
        if not _confirm_outside_workspace(target, "List folder"):
            return "Operation aborted: User did NOT approve listing a folder outside the working directory."

        items = []
        for entry in sorted(os.listdir(target)):
            if entry in IGNORE_DIRS:
                continue
            full_path = os.path.join(target, entry)
            if _is_link_or_junction(full_path):
                items.append(f"[LINK] {entry} (symlink/junction: not followed)")
                continue
            kind = "[DIR] " if os.path.isdir(full_path) else "[FILE]"
            if not os.path.isdir(full_path) and _is_sensitive_canon(full_path):
                items.append(f"{kind} {entry} (protected: contents not readable)")
                continue
            size = "" if os.path.isdir(full_path) else f" ({os.path.getsize(full_path)} bytes)"
            items.append(f"{kind} {entry}{size}")

        return f"Contents of {target}:\n" + ("\n".join(items) if items else "(empty directory)")
    except Exception as e:
        return f"Error listing directory '{path}': {e}"


class ToolUnavailable(Exception):
    """An optional library could not be loaded on this machine."""


def _need(module: str, feature: str):
    """Imports an optional library the first time a tool needs it and returns the module.

    Word, PowerPoint, Excel, PDF and Outlook support each rest on a separate package, and some of those contain
    compiled code that application control may refuse to load. Importing here instead of at startup means a
    missing or blocked package disables only the tools that need it; Eustace itself still starts."""
    try:
        return importlib.import_module(module)
    except Exception as e:
        raise ToolUnavailable(f"{feature} is unavailable on this machine (could not load '{module}': {e})") from e


def _read_excel(target: str) -> str:
    """Extracts all sheets and rows from an .xlsx file as Markdown-style tables."""
    wb = _need("openpyxl", "Excel support").load_workbook(target, data_only=True, read_only=True)
    out = []
    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]
        out.append(f"### Sheet: {sheet_name}")
        rows_added = 0
        for row in sheet.iter_rows(values_only=True):
            if any(cell is not None for cell in row):
                row_str = " | ".join("" if cell is None else str(cell).strip() for cell in row)
                out.append(row_str)
                rows_added += 1
                if rows_added >= 500:
                    out.append("... [Sheet truncated at 500 rows]")
                    break
        out.append("")
    wb.close()
    return "\n".join(out)


def _read_pdf(target: str) -> str:
    """Extracts text page-by-page from a .pdf file."""
    reader = _need("pypdf", "PDF support").PdfReader(target)
    out = [f"PDF Pages: {len(reader.pages)}\n"]
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or "(No extractable text on this page)"
        out.append(f"--- Page {i} ---\n{text}")
    return "\n".join(out)


def _read_docx(target: str) -> str:
    """Extracts paragraphs and tables from a .docx Word document."""
    doc = _need("docx", "Word support").Document(target)
    out = []
    for p in doc.paragraphs:
        if p.text.strip():
            out.append(p.text)
    for t_idx, table in enumerate(doc.tables, start=1):
        out.append(f"\n[Table {t_idx}]")
        for row in table.rows:
            out.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(out)


_DOC_READERS = {".xlsx": _read_excel, ".xlsm": _read_excel, ".pdf": _read_pdf, ".docx": _read_docx,
                ".pptx": lambda target: _read_pptx(target)}  # defined with the PowerPoint tools below


@tool("Reading file", path="Relative or absolute path to the file to read.")
def read_file(path: str, max_chars: int = 30000) -> str:
    """Read the contents of a file on the user's local PC. Supports plaintext, code, CSV, Excel (.xlsx/.xlsm),
    PDF (.pdf), Word (.docx), and PowerPoint (.pptx)."""
    try:
        target, err = _resolve_model_path(path)
        if err:
            return err
        if not os.path.exists(target):
            return f"Error: File not found: {path}"
        if not os.path.isfile(target):
            return f"Error: Path is not a file: {path}"
        if _is_sensitive_canon(target):
            return f"Security Error: '{path}' matches a protected secrets or deny-list pattern and will not be read."
        phi_err = _check_phi_guardrail(target)
        if phi_err:
            return phi_err
        if not _confirm_outside_workspace(target, "Read file"):
            return "Operation aborted: User did NOT approve reading a file outside the working directory."

        reader = _DOC_READERS.get(os.path.splitext(target)[1].lower())
        if reader:
            content = reader(target)
        else:
            with _open_text_lenient(target) as f:
                content = f.read(max_chars + 1)

        phi_err = _check_phi_guardrail(target, content)
        if phi_err:
            return phi_err

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [Truncated at {max_chars} chars]"
        return content
    except Exception as e:
        return f"Error reading file '{path}': {e}"


# ---------------------------------------------------------------------
# Exact text I/O: an edit must change only the bytes shown in the approved diff.
# Text-mode open() would rewrite every line ending and turn undecodable bytes into U+FFFD.
# ---------------------------------------------------------------------

def _decode_exact(raw: bytes) -> tuple[str, str] | None:
    """(text, encoding) such that text.encode(encoding) == raw, or None when the bytes are not UTF-8 or
    BOM-marked UTF-16. A BOM stays in the text as U+FEFF so it survives the round trip."""
    enc = "utf-16-le" if raw.startswith(b"\xff\xfe") else "utf-16-be" if raw.startswith(b"\xfe\xff") else "utf-8"
    try:
        return raw.decode(enc), enc
    except UnicodeDecodeError:
        return None


def _dominant_newline(text: str) -> str:
    """The line ending a file mostly uses; the platform default when it has none."""
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if not crlf and not lf:
        return os.linesep
    return "\r\n" if crlf > lf else "\n"


def _raw_index(text: str, norm_index: int) -> int:
    """Index in text that corresponds to norm_index in text.replace('\r\n', '\n')."""
    i = n = 0
    while n < norm_index:
        i += 2 if text.startswith("\r\n", i) else 1
        n += 1
    return i


def _read_bytes_or_none(path: str) -> bytes | None:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def _open_text_lenient(path: str):
    """Text stream for reading only: UTF-16 when BOM-marked, otherwise UTF-8 (BOM dropped), bad bytes replaced."""
    with open(path, "rb") as f:
        head = f.read(2)
    enc = "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    return open(path, "r", encoding=enc, errors="replace")


@tool(
    "Writing file",
    path="Relative or absolute path to the file to create/overwrite.",
    content="The exact plaintext or code content to write into the file.",
)
def write_file(path: str, content: str) -> str:
    """Create a new file or completely overwrite an existing file with content."""
    try:
        target, err = _guard_write(path)
        if err:
            return err
        if os.path.isdir(target):
            return f"Error: '{path}' is a directory."

        # An existing text file keeps its own encoding, BOM and line endings; a new one gets the platform default.
        before = _read_bytes_or_none(target) if os.path.isfile(target) else None
        decoded = _decode_exact(before) if before is not None else None
        old_text, enc = decoded if decoded else ("", "utf-8")
        body = content.replace("\r\n", "\n")
        if old_text.startswith("﻿") and not body.startswith("﻿"):
            body = "﻿" + body
        out = body.replace("\n", _dominant_newline(old_text)).encode(enc)

        action = f"Overwrite file: {target}" if before is not None else f"Create new file: {target}"
        detail = f"[dim]Size: {len(content)} chars ({len(content.splitlines())} lines)[/dim]"
        if before is not None and decoded is None:
            detail += "\n  [bold yellow]The existing file is not UTF-8 text; all of it will be replaced.[/bold yellow]"
        diff_str = build_unified_diff(old_text.replace("\r\n", "\n"), body, target)

        err = _approve_and_recheck(target, action, detail, diff_str)
        if err:
            return err
        if (_read_bytes_or_none(target) if os.path.isfile(target) else None) != before:
            return "Security Error: file changed between preview and write; write aborted. Re-run to see the new diff."
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(out)

        console.print(f"[bold green]✓ File written: {escape(target)}[/bold green]\n")
        return f"Success: File written successfully to {target}"
    except Exception as e:
        return f"Error writing file '{path}': {e}"


MAX_EDITS_PER_CALL = 50
_EDITS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "old_text": {"type": "string", "description": "Exact text currently in the file (after earlier edits in this list)."},
            "new_text": {"type": "string", "description": "Replacement text."},
        },
        "required": ["old_text", "new_text"],
    },
    "description": "Several replacements in one file, applied in order and approved as one diff. If any edit fails, "
                   "nothing is written. Use this instead of old_text/new_text when changing more than one place.",
}


@tool(
    "Editing file",
    path="Relative or absolute path to the file to edit.",
    old_text="The exact string block currently in the file that you want to replace (single edit).",
    new_text="The replacement string block to insert (single edit).",
    edits=_EDITS_SCHEMA,
)
def edit_file(path: str, old_text: str = "", new_text: str = "", edits: list[dict] | None = None) -> str:
    """Search and replace specific blocks of text/code within an existing file: one block with old_text and
    new_text, or several at once with edits."""
    try:
        if edits is not None and (old_text or new_text):
            return "Error: pass either old_text/new_text or edits, not both. No changes made."
        if edits is None:
            pairs = [(old_text, new_text)]
        else:
            if not isinstance(edits, list) or not edits:
                return "Error: 'edits' must be a non-empty list of {old_text, new_text} objects. No changes made."
            if len(edits) > MAX_EDITS_PER_CALL:
                return f"Error: at most {MAX_EDITS_PER_CALL} edits per call. No changes made."
            pairs = []
            for n, e in enumerate(edits, start=1):
                if not (isinstance(e, dict) and isinstance(e.get("old_text"), str) and isinstance(e.get("new_text"), str)):
                    return f"Error: edit {n} must be an object with string old_text and new_text. No changes made."
                pairs.append((e["old_text"], e["new_text"]))

        target, err = _guard_write(path)
        if err:
            return err
        if not os.path.isfile(target):
            return f"Error: File not found: {path}"

        before = _read_bytes_or_none(target)
        decoded = _decode_exact(before) if before is not None else None
        if decoded is None:
            return (
                f"Error: '{path}' is not valid UTF-8 (or BOM-marked UTF-16) text, so it cannot be edited "
                "without corrupting it. No changes made."
            )
        text, enc = decoded
        newline = _dominant_newline(text)
        original_norm = text.replace("\r\n", "\n")

        # Match with line endings normalized (the model writes \n), then splice into the original text so every
        # byte outside the replaced span, including each untouched line ending, is preserved. Each edit sees the
        # result of the ones before it.
        for n, (old, new) in enumerate(pairs, start=1):
            which = f"edit {n}: " if len(pairs) > 1 else ""
            norm = text.replace("\r\n", "\n")
            old_n, new_n = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
            if not old_n:
                return f"Error: {which}'old_text' is empty. No changes made."
            if old_n == new_n:
                return f"Error: {which}'old_text' and 'new_text' are identical. No changes made."
            hits = norm.count(old_n)
            if hits == 0:
                return f"Error: {which}Target text block ('old_text') was not found in the file. No changes made."
            if hits > 1:
                return (
                    f"Error: {which}'old_text' matches {hits} places in the file. Include more surrounding lines so it "
                    "identifies exactly one. No changes made."
                )
            at = norm.index(old_n)
            start, end = _raw_index(text, at), _raw_index(text, at + len(old_n))
            text = text[:start] + new_n.replace("\n", newline) + text[end:]

        updated = text
        diff_str = build_unified_diff(original_norm, updated.replace("\r\n", "\n"), target)
        title = f"Edit file: {target}" + (f" ({len(pairs)} edits)" if len(pairs) > 1 else "")

        err = _approve_and_recheck(target, title, diff=diff_str)
        if err:
            return err
        # The approved diff must describe the bytes actually on disk; re-read and compare before writing.
        if _read_bytes_or_none(target) != before:
            return "Security Error: file changed between preview and write; edit aborted. Re-run to see the new diff."

        with open(target, "wb") as f:
            f.write(updated.encode(enc))

        console.print(f"[bold green]✓ File edited: {escape(target)}[/bold green]\n")
        return f"Success: File edited successfully at {target}" + (f" ({len(pairs)} edits)" if len(pairs) > 1 else "")
    except Exception as e:
        return f"Error editing file '{path}': {e}"


SHELL_TIMEOUT_SECONDS = 120
MAX_SHELL_OUTPUT_CHARS = 20000


@tool("Running PowerShell", command="The PowerShell command line to execute.")
def run_powershell(command: str) -> str:
    """Run a PowerShell command on the user's local PC in the current working directory (e.g. git, pytest,
    builds, dir). Returns exit code, stdout and stderr. The user must manually approve every command, so keep
    each one purposeful."""
    try:
        # Windows PowerShell treats a bare CR (and other C0 controls rich would drop from the panel) as a
        # statement separator, so a hidden second statement could ride behind the displayed one. Normalize
        # CRLF, then refuse anything but printable text, newline and tab.
        command = str(command).replace("\r\n", "\n")
        if any(ord(c) < 0x20 and c not in "\n\t" for c in command) or "\x7f" in command:
            return "Security Error: command contains hidden control characters; aborted."

        shell = _trusted_powershell()
        if not shell:
            return "Error: No trusted PowerShell host found (pwsh.exe under Program Files or powershell.exe under System32)."

        if not _permission(
            "shell",  # every command is its own decision; "approve all" never covers the shell
            "Run PowerShell command",
            f"[bold cyan]{escape(_visible_controls(command))}[/bold cyan]\n[dim]Interpreter: {escape(shell)}[/dim]",
            command=command,
        ):
            return "Operation aborted: User did NOT approve running the command."

        child_env = {k: v for k, v in os.environ.items() if k not in ("STARK_API_KEY", "GEMINI_API_KEY")}
        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.getcwd(),
                env=child_env,
                timeout=SHELL_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return f"Error: Command timed out after {SHELL_TIMEOUT_SECONDS} seconds."

        parts = [f"Exit code: {proc.returncode}"]
        if proc.stdout.strip():
            parts.append(f"--- stdout ---\n{proc.stdout.rstrip()}")
        if proc.stderr.strip():
            parts.append(f"--- stderr ---\n{proc.stderr.rstrip()}")

        output = "\n".join(parts)
        if len(output) > MAX_SHELL_OUTPUT_CHARS:
            output = output[:MAX_SHELL_OUTPUT_CHARS] + f"\n... [Truncated at {MAX_SHELL_OUTPUT_CHARS} chars]"

        console.print(f"[dim]{escape(_sanitize_for_display(output))}[/dim]")
        return output
    except Exception as e:
        return f"Error running command: {e}"


# =====================================================================
# WIN32 CLIPBOARD AND OPEN_FILE LAUNCHER
# =====================================================================

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.CloseClipboard.argtypes = []
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.EmptyClipboard.argtypes = []
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype = wintypes.HANDLE

_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL

_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


def _clipboard_text_raw() -> str | None:
    """The clipboard's text exactly as stored ("" when it holds no text), or None if it could not be read."""
    if not _user32.OpenClipboard(None):
        return None
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


@tool("Reading clipboard")
def read_clipboard(max_chars: int = 30000) -> str:
    """Read the text currently copied to the user's Windows clipboard. Ideal when the user copies an email,
    paragraph, table, or snippet and asks you to summarize, fix, or act on it."""
    text = _clipboard_text_raw()
    if text is None:
        return "Error: Could not read the Windows clipboard (it may be locked by another application)."
    if not text:
        return "(Clipboard is empty or does not contain text)"

    phi_err = _check_phi_guardrail("clipboard", text)
    if phi_err:
        return phi_err

    # The clipboard often holds something copied for another purpose (a password, a private message), so
    # the user sees what is about to be sent before the model does.
    lines = text.splitlines()
    peek = [ln[:80] + ("..." if len(ln) > 80 else "") for ln in lines[:3]]
    if len(lines) > 3:
        peek.append(f"... (+{len(lines) - 3} more lines)")
    if not _permission(
        "clipboard_read",
        "Send clipboard text to the model",
        f"[dim]{len(text)} characters:[/dim]\n[cyan]" + escape(_visible_controls("\n".join(peek))) + "[/cyan]",
    ):
        return "Operation aborted: User did NOT approve reading the clipboard."

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [Clipboard truncated at {max_chars} chars]"
    return text if text.strip() else "(Clipboard contains only whitespace)"


@tool("Copying to clipboard", text="The text to place onto the Windows clipboard.")
def write_clipboard(text: str) -> str:
    """Copy text directly onto the user's Windows clipboard so they can immediately Ctrl+V paste it into
    Outlook, Word, or Teams."""
    try:
        # What lands on the clipboard gets pasted somewhere later, possibly into a terminal
        if not _permission(
            "clipboard_write",
            "Copy to clipboard",
            "[cyan]" + escape(_visible_controls(_head_lines(text, 40))) + "[/cyan]",
        ):
            return "Operation aborted: User did NOT approve writing to the clipboard."
        data = text.encode("utf-16-le") + b"\x00\x00"
        h_mem = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not h_mem:
            return "Error: GlobalAlloc failed for clipboard."
        ptr = _kernel32.GlobalLock(h_mem)
        if not ptr:
            _kernel32.GlobalFree(h_mem)
            return "Error: GlobalLock failed for clipboard."
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            _kernel32.GlobalUnlock(h_mem)

        if not _user32.OpenClipboard(None):
            _kernel32.GlobalFree(h_mem)
            return "Error: Could not open Windows clipboard."
        try:
            _user32.EmptyClipboard()
            if not _user32.SetClipboardData(_CF_UNICODETEXT, h_mem):
                _kernel32.GlobalFree(h_mem)
                return "Error: SetClipboardData failed."
        finally:
            _user32.CloseClipboard()

        console.print(f"  [bold green]✓ Copied {len(text)} chars to clipboard[/bold green]")
        return f"Success: Copied {len(text)} characters to the Windows clipboard."
    except Exception as e:
        return f"Error writing to clipboard: {e}"


# Only non-executing document, image and folder types may be handed to os.startfile. A denylist cannot
# keep up with every registered handler that executes content (.hta, .pif, .msc, .reg, .url, .xll, ...).
ALLOWED_OPEN_EXTENSIONS = {
    ".txt", ".md", ".csv", ".log", ".json", ".xml",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
}


@tool("Opening file", path="Relative or absolute path to the document or folder to open.")
def open_file(path: str) -> str:
    """Launch/open a document or folder in its default Windows application (e.g., Word, Excel, PDF reader, File
    Explorer) after creating or updating it so the user can review it immediately."""
    try:
        target, err = _resolve_model_path(path)
        if err:
            return err
        if not os.path.exists(target):
            return f"Error: Path does not exist: {path}"
        if not os.path.isdir(target):
            # Windows ignores trailing dots/spaces when resolving, so strip them before reading the extension.
            ext = os.path.splitext(target.rstrip(" ."))[1].lower()
            if ext not in ALLOWED_OPEN_EXTENSIONS:
                return f"Security Error: open_file only launches known document types; '{ext or '(none)'}' is not allowed."
        if _is_sensitive_canon(target):
            return f"Security Error: Cannot open protected file '{path}'."

        # Opening is not a write, so a document under Program Files may be opened
        err = _approve_and_recheck(target, f"Open in default Windows app: {target}", writing=False)
        if err:
            return err
        os.startfile(target)
        console.print(f"[bold green]✓ Launched: {escape(target)}[/bold green]")
        return f"Success: Opened '{target}' in its default application."
    except Exception as e:
        return f"Error opening '{path}': {e}"

# =====================================================================
# OFFICE DOCUMENT GENERATION AND EDITING (.docx, .xlsx)
# =====================================================================

def _add_formatted_runs(paragraph, text: str):
    """Parses basic **bold** markdown spans into Word paragraph runs."""
    parts = re.split(r"(\*\*.*?\*\*)", text)
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part:
            paragraph.add_run(part)


@tool(
    "Creating Word document",
    path="Output path for the .docx file.",
    title="Optional document title heading at the top of the page.",
    content="Markdown-formatted body content to render into the Word document.",
)
def write_docx(path: str, content: str, title: str = "") -> str:
    """Create or overwrite a Microsoft Word (.docx) document from Markdown-formatted text. Supports #/##/###
    headings, bullet lists (- ), numbered lists (1. ), **bold** text, and Markdown tables (| col | col |)."""
    try:
        Document = _need("docx", "Word support").Document  # before any prompt: do not ask for what cannot be done
        target, err = _guard_write(path, ext=(".docx",))
        if err:
            return err

        action = f"Overwrite Word document: {target}" if os.path.exists(target) else f"Create Word document: {target}"
        # The approved preview must contain every model-controlled element that will be written, title included.
        preview_body = f"# {title.strip()}\n\n{content}" if title.strip() else content
        preview_diff = build_unified_diff("", preview_body, target)
        err = _approve_and_recheck(target, action, diff=preview_diff)
        if err:
            return err

        doc = Document()
        if title.strip():
            doc.add_heading(title.strip(), 0)

        lines = content.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            # Markdown Table block detection
            if stripped.startswith("|") and stripped.endswith("|"):
                table_rows = []
                while i < len(lines) and lines[i].strip().startswith("|") and lines[i].strip().endswith("|"):
                    row_line = lines[i].strip()[1:-1]
                    cells = [c.strip() for c in row_line.split("|")]
                    # Skip markdown separator row like |---|---|
                    if not all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
                        table_rows.append(cells)
                    i += 1
                if table_rows:
                    cols = max(len(r) for r in table_rows)
                    tbl = doc.add_table(rows=len(table_rows), cols=cols)
                    tbl.style = "Table Grid"
                    for r_idx, r_data in enumerate(table_rows):
                        for c_idx, val in enumerate(r_data):
                            cell_p = tbl.cell(r_idx, c_idx).paragraphs[0]
                            _add_formatted_runs(cell_p, val)
                            if r_idx == 0:
                                for run in cell_p.runs:
                                    run.bold = True
                continue

            # Headings
            if stripped.startswith("### "):
                doc.add_heading(stripped[4:].strip(), level=3)
            elif stripped.startswith("## "):
                doc.add_heading(stripped[3:].strip(), level=2)
            elif stripped.startswith("# "):
                doc.add_heading(stripped[2:].strip(), level=1)
            # Bullet lists
            elif stripped.startswith(("- ", "* ")):
                p = doc.add_paragraph(style="List Bullet")
                _add_formatted_runs(p, stripped[2:].strip())
            # Numbered lists
            elif re.match(r"^\d+\.\s+", stripped):
                clean_item = re.sub(r"^\d+\.\s+", "", stripped)
                p = doc.add_paragraph(style="List Number")
                _add_formatted_runs(p, clean_item)
            else:
                p = doc.add_paragraph()
                _add_formatted_runs(p, stripped)

            i += 1

        os.makedirs(os.path.dirname(target), exist_ok=True)
        doc.save(target)
        _mark_of_the_web(target)
        console.print(f"[bold green]✓ Word document saved: {escape(target)}[/bold green]\n")
        return f"Success: Word document written to {target}"
    except Exception as e:
        return f"Error writing Word document '{path}': {e}"


@tool(
    "Filling Word template",
    template_path="Path to the existing .docx template file.",
    output_path="Path where the filled .docx file should be saved.",
    replacements="Key-value dictionary mapping exact placeholder strings to their replacement text.",
)
def fill_docx_template(template_path: str, output_path: str, replacements: dict) -> str:
    """Fill placeholders (such as '{{DATE}}', '{{SUBJECT}}', '[NAME]') inside an existing Word (.docx) template
    (preserving headers, footers, tables, and formatting) and save to a new .docx file."""
    try:
        Document = _need("docx", "Word support").Document
        src, err = _resolve_model_path(template_path)
        if err:
            return err
        dst, err = _guard_write(output_path, ext=(".docx",))
        if err:
            return err
        if not os.path.isfile(src):
            return f"Error: Template file not found: {template_path}"
        if _is_sensitive_canon(src):
            return f"Security Error: Template '{template_path}' is a protected file."

        summary_lines = [f"Template: {src}", f"Output:   {dst}", "Replacements:"]
        for k, v in replacements.items():
            summary_lines.append(f"  {k}  -->  {v}")

        err = _approve_and_recheck(
            dst, f"Fill Word template -> {dst}",
            detail="[dim]" + escape(_visible_controls("\n".join(summary_lines))) + "[/dim]",
        )
        if err:
            return err

        doc = Document(src)
        count = 0

        def _replace_in_paragraphs(paragraphs):
            nonlocal count
            for p in paragraphs:
                for key, val in replacements.items():
                    if key in p.text:
                        count += p.text.count(key)
                        # Try run-level replacement first to preserve styling; fallback to paragraph text
                        replaced_in_run = False
                        for run in p.runs:
                            if key in run.text:
                                run.text = run.text.replace(key, str(val))
                                replaced_in_run = True
                        if not replaced_in_run:
                            p.text = p.text.replace(key, str(val))

        _replace_in_paragraphs(doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    _replace_in_paragraphs(cell.paragraphs)
        for section in doc.sections:
            _replace_in_paragraphs(section.header.paragraphs)
            _replace_in_paragraphs(section.footer.paragraphs)

        os.makedirs(os.path.dirname(dst), exist_ok=True)
        doc.save(dst)
        _mark_of_the_web(dst)
        console.print(f"[bold green]✓ Template filled ({count} replacements): {escape(dst)}[/bold green]\n")
        return f"Success: Saved filled document to {dst} ({count} replacements made)."
    except Exception as e:
        return f"Error filling Word template: {e}"

@tool(
    "Updating Excel workbook",
    path="Path to the .xlsx file (created automatically if it does not exist).",
    sheet_name="Optional target worksheet name (defaults to active sheet, or creates sheet if missing).",
    append_rows="Optional list of rows (each row is a list of cell values) to append to the bottom of the sheet.",
    cell_updates="Optional dictionary mapping cell coordinates (like 'A1', 'C5') to new values.",
)
def update_excel(
    path: str,
    sheet_name: str = "",
    append_rows: list[list[str]] | None = None,
    cell_updates: dict | None = None,
) -> str:
    """Create a new Excel (.xlsx) workbook or update an existing one by appending rows to a sheet and/or setting
    specific cell coordinates (e.g. {'B4': 'Complete', 'C4': 100})."""
    try:
        openpyxl = _need("openpyxl", "Excel support")
        target, err = _guard_write(path, ext=(".xlsx", ".xlsm"))
        if err:
            return err

        exists = os.path.exists(target)
        is_macro = target.lower().endswith(".xlsm")
        if is_macro and not exists:
            return (
                "Error: a new macro-enabled workbook (.xlsm) cannot be created by this tool; use .xlsx. "
                "Existing .xlsm workbooks can be updated."
            )
        preview = [f"Workbook: {target} ({'existing' if exists else 'new file'})"]
        if exists:
            preview.append(
                "Note:     the whole workbook is rewritten. Content the library does not model (shapes, form "
                "controls, some formatting) can be lost; keep a copy of anything important."
            )
        if sheet_name:
            preview.append(f"Sheet:    {sheet_name}")
        if append_rows:
            preview.append(f"\nRows to append ({len(append_rows)}):")
            for r in append_rows[:15]:
                preview.append("  + | " + " | ".join(str(c) for c in r) + " |")
            if len(append_rows) > 15:
                preview.append(f"  ... (+{len(append_rows) - 15} more rows)")
        if cell_updates:
            cell_items = list(cell_updates.items())
            preview.append(f"\nCell updates ({len(cell_items)}):")
            for cell_ref, val in cell_items[:25]:
                preview.append(f"  {cell_ref} := {val}")
            if len(cell_items) > 25:
                preview.append(f"  ... (+{len(cell_items) - 25} more cells)")

        err = _approve_and_recheck(
            target, f"Update Excel workbook: {target}",
            detail="[cyan]" + escape(_visible_controls("\n".join(preview))) + "[/cyan]",
        )
        if err:
            return err

        if exists:
            # Without keep_vba the macro project is dropped and the file is saved as a plain workbook under an
            # .xlsm name, which Excel then refuses to open.
            wb = openpyxl.load_workbook(target, keep_vba=is_macro)
            if sheet_name:
                ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.create_sheet(title=sheet_name)
            else:
                ws = wb.active
        else:
            wb = openpyxl.Workbook()
            ws = wb.active
            if sheet_name:
                ws.title = sheet_name

        rows_added = 0
        if append_rows:
            for row in append_rows:
                ws.append(list(row))
                rows_added += 1

        cells_changed = 0
        if cell_updates:
            for cell_ref, val in cell_updates.items():
                ws[cell_ref.upper()] = val
                cells_changed += 1

        os.makedirs(os.path.dirname(target), exist_ok=True)
        wb.save(target)
        wb.close()
        _mark_of_the_web(target)

        console.print(
            f"[bold green]✓ Excel updated ({rows_added} rows appended, {cells_changed} cells set): {escape(target)}[/bold green]\n"
        )
        return (
            f"Success: Workbook '{target}' (sheet '{ws.title}') updated "
            f"({rows_added} rows appended, {cells_changed} cells updated)."
        )
    except Exception as e:
        return f"Error updating Excel file '{path}': {e}"

# =====================================================================
# POWERPOINT (.pptx): outline reader, write_pptx, edit_pptx
# =====================================================================

PPTX_FEATURE = "PowerPoint support"
MAX_PPTX_SLIDES = 200
_PPTX_SLIDE_KEYS = {"layout", "title", "subtitle", "bullets", "notes", "table", "image_path"}
_PPTX_SLIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "layout": {"type": "string", "description": "Slide layout name from the deck or template, e.g. 'Title Slide', 'Title and Content', 'Title Only'. Omit to choose automatically."},
        "title": {"type": "string", "description": "Slide title."},
        "subtitle": {"type": "string", "description": "Subtitle (title slides)."},
        "bullets": {"type": "array", "items": {"type": "string"}, "description": "Body text, one entry per paragraph. Indent an entry with two leading spaces per level for sub-bullets."},
        "notes": {"type": "string", "description": "Speaker notes."},
        "table": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}, "description": "Table rows; the first row is the header."},
        "image_path": {"type": "string", "description": "Path to a .png or .jpg file to place on the slide."},
    },
}


def _potx_as_pptx(data: bytes) -> bytes:
    """A PowerPoint template (.potx) is a normal deck whose main part is declared as a template, which the
    library refuses to open. Re-declare it as a presentation in an in-memory copy."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            blob = src.read(item.filename)
            if item.filename == "[Content_Types].xml":
                blob = blob.replace(b"presentationml.template.main+xml", b"presentationml.presentation.main+xml")
            dst.writestr(item, blob)
    return out.getvalue()


def _pptx_make_widescreen(prs) -> None:
    """The library's built-in blank deck is 4:3. Widen it to 16:9 and stretch the master and layout
    placeholders to match, so new decks fill a modern slide."""
    ratio = (prs.slide_height * 16 / 9) / prs.slide_width
    prs.slide_width = int(prs.slide_height * 16 / 9)
    for holder in [prs.slide_master, *prs.slide_layouts]:
        for shape in holder.shapes:
            if shape._element.xpath("./p:spPr/a:xfrm"):
                shape.left, shape.width = int(shape.left * ratio), int(shape.width * ratio)


def _open_presentation(source=None):
    """Opens a deck from a path or bytes (.pptx or .potx), or a blank widescreen deck when source is None."""
    pptx = _need("pptx", PPTX_FEATURE)
    if source is None:
        prs = pptx.Presentation()
        _pptx_make_widescreen(prs)
        return prs
    if isinstance(source, str):
        with open(source, "rb") as f:
            data = f.read()
        if source.lower().endswith(".potx"):
            data = _potx_as_pptx(data)
    else:
        data = source
    return pptx.Presentation(io.BytesIO(data))


def _pptx_iter_shapes(shapes, depth: int = 0):
    """Yields (shape, depth) for every shape, descending into groups."""
    for shape in shapes:
        yield shape, depth
        if hasattr(shape, "shapes"):
            yield from _pptx_iter_shapes(shape.shapes, depth + 1)


def _pptx_shape_kind(shape) -> str:
    try:
        if shape.is_placeholder:
            return shape.placeholder_format.type.name.lower().replace("_", " ")
        if getattr(shape, "has_table", False):
            return "table"
        return shape.shape_type.name.lower().replace("_", " ")
    except Exception:
        return "shape"


def _pptx_text_lines(text_frame) -> list[str]:
    """Non-empty paragraphs, indented two spaces per bullet level; in-paragraph line breaks shown as ' / '."""
    return [("  " * (p.level or 0)) + p.text.replace("\v", " / ").replace("\n", " / ")
            for p in text_frame.paragraphs if p.text.strip()]


def _pptx_outline(prs) -> str:
    """Text outline of a deck: every slide's layout, each shape as '[id] name (kind): text', tables row by row,
    grouped shapes nested, and speaker notes. read_file returns it, and the before/after diff of this outline is
    what the user approves for write_pptx and edit_pptx."""
    slides = list(prs.slides)
    out = [f"Presentation: {len(slides)} slide(s)"]
    for n, slide in enumerate(slides, start=1):
        out.append(f"--- Slide {n} (layout: {slide.slide_layout.name}) ---")
        for shape, depth in _pptx_iter_shapes(slide.shapes):
            pad = "    " * depth
            head = f"{pad}[{shape.shape_id}] {shape.name} ({_pptx_shape_kind(shape)})"
            if getattr(shape, "has_table", False):
                out.append(head + ":")
                for row in shape.table.rows:
                    out.append(pad + "    " + " | ".join(" / ".join(c.text.split("\n")).strip() for c in row.cells))
            elif shape.has_text_frame and shape.text_frame.text.strip():
                lines = _pptx_text_lines(shape.text_frame)
                if len(lines) == 1:
                    out.append(f"{head}: {lines[0].strip()}")
                else:
                    out.append(head + ":")
                    out.extend(pad + "    " + line for line in lines)
            else:
                out.append(head)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append("[Speaker Notes]: " + slide.notes_slide.notes_text_frame.text.strip().replace("\n", " / "))
    return "\n".join(out)


def _read_pptx(target: str) -> str:
    """Outline of a .pptx PowerPoint file (see _pptx_outline)."""
    return _pptx_outline(_open_presentation(target))


def _pptx_set_paragraphs(text_frame, lines: list[str]) -> None:
    """Replaces a text frame's content with one paragraph per line. Two leading spaces mean one indent level;
    a leading '- ' or '* ' marker is dropped, since the layout supplies the bullet."""
    text_frame.text = ""
    for i, raw in enumerate(lines or [""]):
        raw = str(raw)
        stripped = raw.lstrip(" ")
        para = text_frame.paragraphs[0] if i == 0 else text_frame.add_paragraph()
        para.text = re.sub(r"^[-*•]\s+", "", stripped)
        para.level = min((len(raw) - len(stripped)) // 2, 8)


def _pptx_validate_slide(spec, where: str) -> str | None:
    """Checks one slide description; returns an error for the model or None."""
    if not isinstance(spec, dict):
        return f"Error: {where} must be an object."
    unknown = sorted(set(spec) - _PPTX_SLIDE_KEYS)
    if unknown:
        return f"Error: {where} has unknown field(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(_PPTX_SLIDE_KEYS))}."
    for key in ("layout", "title", "subtitle", "notes", "image_path"):
        if key in spec and not isinstance(spec[key], str):
            return f"Error: {where} field '{key}' must be text."
    if "bullets" in spec and not (isinstance(spec["bullets"], list) and all(isinstance(b, str) for b in spec["bullets"])):
        return f"Error: {where} field 'bullets' must be a list of text entries."
    if "table" in spec:
        rows = spec["table"]
        if not (isinstance(rows, list) and rows and all(isinstance(r, list) and r for r in rows)):
            return f"Error: {where} field 'table' must be a non-empty list of non-empty rows."
        if len(rows) > 50 or max(len(r) for r in rows) > 20:
            return f"Error: {where} table is larger than 50 rows by 20 columns."
    return None


def _pptx_readable(raw: str, exts: tuple[str, ...], what: str) -> tuple[str | None, str | None]:
    """(canonical path, None) for a template or image the model may have read into a deck, else (None, error).
    Same rules as read_file: canonical path, never a protected file, ask before leaving the workspace."""
    canon, err = _resolve_model_path(raw)
    if err:
        return None, err
    if _is_sensitive_canon(canon):  # first, so a protected path gets the same answer whether or not it exists
        return None, f"Security Error: {what} '{raw}' is a protected file."
    if not os.path.isfile(canon):
        return None, f"Error: {what} not found: {raw}"
    if not canon.lower().endswith(exts):
        return None, f"Error: {what} must be one of: {', '.join(exts)}"
    if os.path.getsize(canon) > 50 * 1024 * 1024:
        return None, f"Error: {what} is larger than 50 MB."
    if not _confirm_outside_workspace(canon, f"Use {what}"):
        return None, f"Operation aborted: User did NOT approve using a {what} outside the working directory."
    return canon, None


def _pptx_pick_layout(prs, spec: dict, where: str):
    layouts = list(prs.slide_layouts)
    by_name = {layout.name.strip().lower(): layout for layout in layouts}
    wanted = spec.get("layout", "").strip()
    if wanted:
        if wanted.lower() in by_name:
            return by_name[wanted.lower()]
        raise ValueError(f"{where}: no layout named '{wanted}'. Available: {', '.join(layout.name for layout in layouts)}")
    content = bool(spec.get("bullets"))
    extras = bool(spec.get("table") or spec.get("image_path"))
    if not content and not extras:
        prefer = ("title slide",) if spec.get("subtitle") or len(prs.slides) == 0 else ("title only", "section header")
    else:
        prefer = ("title and content",) if content else ("title only", "title and content")
    for name in prefer:
        if name in by_name:
            return by_name[name]
    # Template with its own layout names: fall back on structure (a title plus a body area), then on the first layout
    PH = _need("pptx.enum.shapes", PPTX_FEATURE).PP_PLACEHOLDER
    for layout in layouts:
        kinds = {p.placeholder_format.type for p in layout.placeholders}
        if (content or extras) and kinds & {PH.BODY, PH.OBJECT} and kinds & {PH.TITLE, PH.CENTER_TITLE}:
            return layout
    return layouts[0]


def _pptx_add_slide(prs, spec: dict, where: str, image: str | None = None):
    """Appends one slide built from a validated slide description and returns it."""
    PH = _need("pptx.enum.shapes", PPTX_FEATURE).PP_PLACEHOLDER
    slide = prs.slides.add_slide(_pptx_pick_layout(prs, spec, where))
    width, height = prs.slide_width, prs.slide_height

    title = spec.get("title", "").strip()
    if title:
        if slide.shapes.title is not None:
            slide.shapes.title.text_frame.text = title
        else:
            box = slide.shapes.add_textbox(int(width * 0.06), int(height * 0.06), int(width * 0.88), int(height * 0.14))
            box.text_frame.text = title

    text_holders = [p for p in slide.placeholders
                    if p.placeholder_format.type in (PH.SUBTITLE, PH.BODY, PH.OBJECT) and p.has_text_frame]
    subtitle = spec.get("subtitle", "").strip()
    if subtitle:
        holder = next((p for p in text_holders if p.placeholder_format.type == PH.SUBTITLE), None) or (text_holders[0] if text_holders else None)
        if holder is None:
            raise ValueError(f"{where}: layout '{slide.slide_layout.name}' has no place for a subtitle")
        holder.text_frame.text = subtitle
        text_holders.remove(holder)

    # The main area: the layout's body placeholder if it has one, otherwise a default box under the title
    body = next((p for p in text_holders if p.placeholder_format.type in (PH.BODY, PH.OBJECT)), None) or (text_holders[0] if text_holders else None)
    if body is not None:
        left, top, w, h = body.left, body.top, body.width, body.height
        text_holders.remove(body)
    else:
        left, top, w, h = int(width * 0.06), int(height * 0.24), int(width * 0.88), int(height * 0.64)

    bullets, table = spec.get("bullets") or [], spec.get("table")
    if image:  # picture takes the right-hand side whenever it shares the slide
        shares = bool(bullets or table)
        img_left, img_w = (left + int(w * 0.58), int(w * 0.42)) if shares else (left, w)
        if shares:
            w = int(w * 0.55)
        pic = slide.shapes.add_picture(image, img_left, top)
        scale = min(img_w / pic.width, h / pic.height)
        pic.width, pic.height = int(pic.width * scale), int(pic.height * scale)
        pic.left, pic.top = img_left + (img_w - pic.width) // 2, top + (h - pic.height) // 2
    text_h = int(h * 0.45) if (bullets and table) else h
    if bullets:
        if body is None:
            body = slide.shapes.add_textbox(left, top, w, text_h)
            body.text_frame.word_wrap = True
        else:
            body.left, body.top, body.width, body.height = left, top, w, text_h
        _pptx_set_paragraphs(body.text_frame, bullets)
    elif body is not None:
        body._element.getparent().remove(body._element)  # an empty "Click to add text" box helps nobody
    if table:
        cols = max(len(r) for r in table)
        t_top, t_h = (top + int(h * 0.5), int(h * 0.5)) if bullets else (top, min(h, int(height * 0.08) * len(table)))
        frame = slide.shapes.add_table(len(table), cols, left, t_top, w, t_h)
        for r, row in enumerate(table):
            for c in range(cols):
                frame.table.cell(r, c).text = str(row[c]) if c < len(row) else ""
    for leftover in text_holders:
        leftover._element.getparent().remove(leftover._element)

    if spec.get("notes", "").strip():
        slide.notes_slide.notes_text_frame.text = spec["notes"].strip()
    return slide


def _pptx_delete_slide(prs, index: int) -> None:
    """Removes the slide at a zero-based position (the library has no public call for this)."""
    id_list = prs.slides._sldIdLst
    entry = id_list[index]
    prs.part.drop_rel(entry.rId)
    id_list.remove(entry)


def _pptx_move_slide(prs, index: int, to: int) -> None:
    id_list = prs.slides._sldIdLst
    entry = id_list[index]
    id_list.remove(entry)
    id_list.insert(to, entry)


def _pptx_save(prs, target: str) -> None:
    """Serializes fully in memory first, then swaps the file in, so a failed save cannot leave half a deck."""
    buf = io.BytesIO()
    prs.save(buf)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _mark_of_the_web(target)


def _pptx_approve_and_save(prs, target: str, before: bytes | None, old_outline: str, action: str, detail: str = "") -> str | None:
    """Shared tail of write_pptx and edit_pptx: show the outline diff, re-validate, confirm the file on disk is
    still the one previewed, save. Returns an error/abort message or None on success."""
    diff = build_unified_diff(old_outline, _pptx_outline(prs), target)
    err = _approve_and_recheck(target, action, detail, diff)
    if err:
        return err
    if (_read_bytes_or_none(target) if os.path.isfile(target) else None) != before:
        return "Security Error: file changed between preview and write; nothing was saved. Re-run to see the new diff."
    _pptx_save(prs, target)
    return None


@tool(
    "Building presentation",
    path="Relative or absolute path for the .pptx file to create or overwrite.",
    slides={"type": "array", "items": _PPTX_SLIDE_SCHEMA, "description": "The slides, in order."},
    template_path="Optional .pptx or .potx file whose theme, master and layouts the new deck should use.",
    keep_template_slides="Keep the slides already in the template and add the new ones after them (default false: start empty).",
)
def write_pptx(path: str, slides: list[dict], template_path: str = "", keep_template_slides: bool = False) -> str:
    """Create or overwrite a PowerPoint (.pptx) presentation from a list of slides. Each slide can have a title,
    subtitle, bullets, speaker notes, a table, and an image. Pass template_path to build on an existing deck or
    template so the result uses its theme and layouts. To change an existing deck, use edit_pptx instead."""
    try:
        _need("pptx", PPTX_FEATURE)
        target, err = _guard_write(path, ext=(".pptx",))
        if err:
            return err
        if not isinstance(slides, list) or not slides:
            return "Error: 'slides' must be a non-empty list."
        if len(slides) > MAX_PPTX_SLIDES:
            return f"Error: more than {MAX_PPTX_SLIDES} slides in one call."
        for n, spec in enumerate(slides, start=1):
            err = _pptx_validate_slide(spec, f"slide {n}")
            if err:
                return err

        template = None
        if template_path.strip():
            template, err = _pptx_readable(template_path, (".pptx", ".potx"), "template")
            if err:
                return err
        images = {}
        for n, spec in enumerate(slides, start=1):
            if spec.get("image_path", "").strip():
                images[n], err = _pptx_readable(spec["image_path"], (".png", ".jpg", ".jpeg"), "image")
                if err:
                    return err

        before = _read_bytes_or_none(target) if os.path.isfile(target) else None
        old_outline = ""
        if before is not None:
            try:
                old_outline = _pptx_outline(_open_presentation(before))
            except Exception:
                old_outline = "(the existing file is not a readable presentation; all of it will be replaced)"

        # Build the whole deck in memory before asking, so the approved outline is the deck that gets saved
        prs = _open_presentation(template)
        if template and not keep_template_slides:
            for _ in range(len(prs.slides)):
                _pptx_delete_slide(prs, 0)
        for n, spec in enumerate(slides, start=1):
            _pptx_add_slide(prs, spec, f"slide {n}", images.get(n))

        detail = [f"Template: {template}"] if template else []
        detail += [f"Image on slide {n}: {p}" for n, p in sorted(images.items())]
        action = f"Overwrite presentation: {target}" if before is not None else f"Create presentation: {target}"
        err = _pptx_approve_and_save(prs, target, before, old_outline, action,
                                     "[dim]" + escape(_visible_controls("\n".join(detail))) + "[/dim]" if detail else "")
        if err:
            return err
        console.print(f"[bold green]✓ Presentation saved ({len(prs.slides)} slides): {escape(target)}[/bold green]\n")
        return f"Success: Presentation with {len(prs.slides)} slide(s) written to {target}"
    except ToolUnavailable as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error writing presentation '{path}': {e}"


def _pptx_replace_in_frame(text_frame, find: str, replacement: str) -> int:
    """Replaces text paragraph by paragraph. A match inside one run keeps that run's formatting; a match that
    spans runs collapses the paragraph into its first run (keeping that run's formatting)."""
    count = 0
    for para in text_frame.paragraphs:
        runs = para.runs
        whole = "".join(r.text for r in runs)
        if find not in whole:
            continue
        count += whole.count(find)
        wanted = whole.replace(find, replacement)
        for run in runs:
            if find in run.text:
                run.text = run.text.replace(find, replacement)
        if "".join(r.text for r in runs) != wanted:  # some match straddled a run boundary
            runs[0].text = wanted
            for run in runs[1:]:
                run.text = ""
    return count


def _pptx_text_frames(slide):
    """Every text frame on a slide: shapes (inside groups too), table cells, and the notes."""
    for shape, _ in _pptx_iter_shapes(slide.shapes):
        if getattr(shape, "has_table", False):
            for row in shape.table.rows:
                for cell in row.cells:
                    yield cell.text_frame
        elif shape.has_text_frame:
            yield shape.text_frame
    if slide.has_notes_slide:
        yield slide.notes_slide.notes_text_frame


_PPTX_OPS = {
    "replace_text": ({"find", "replace"}, {"slide"}),
    "set_text": ({"slide", "shape", "text"}, {"row", "col"}),
    "set_notes": ({"slide", "text"}, set()),
    "add_slide": (set(), {"at"} | _PPTX_SLIDE_KEYS),
    "delete_slide": ({"slide"}, set()),
    "move_slide": ({"slide", "to"}, set()),
}


def _pptx_apply(prs, op, image: str | None) -> None:
    """Applies one edit operation in place. Raises ValueError with a message for the model when it cannot."""
    if not isinstance(op, dict) or op.get("op") not in _PPTX_OPS:
        raise ValueError(f"'op' must be one of: {', '.join(_PPTX_OPS)}")
    kind = op["op"]
    required, optional = _PPTX_OPS[kind]
    fields = set(op) - {"op"}
    if required - fields or fields - required - optional:
        raise ValueError(f"{kind} takes {sorted(required)} plus optional {sorted(optional)}; got {sorted(fields)}")
    slides = list(prs.slides)

    def position(key: str, upper: int) -> int:
        value = op[key]
        if isinstance(value, str) and value.strip().isdigit():  # models sometimes quote numbers
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"'{key}' must be a whole number from 1 to {upper}")
        return value - 1

    if kind == "replace_text":
        find, replacement = op["find"], op["replace"]
        if not isinstance(find, str) or not find or not isinstance(replacement, str):
            raise ValueError("'find' must be non-empty text and 'replace' must be text")
        scope = [slides[position("slide", len(slides))]] if "slide" in op else slides
        if not sum(_pptx_replace_in_frame(frame, find, replacement) for s in scope for frame in _pptx_text_frames(s)):
            raise ValueError(f"text not found: {find!r}")
    elif kind == "set_text":
        slide = slides[position("slide", len(slides))]
        shape = next((s for s, _ in _pptx_iter_shapes(slide.shapes) if str(s.shape_id) == str(op["shape"]).strip()), None)
        if shape is None:
            raise ValueError(f"no shape with id {op['shape']!r} on that slide (ids are the [numbers] in the outline)")
        lines = str(op["text"]).split("\n")
        if getattr(shape, "has_table", False):
            if "row" not in op or "col" not in op:
                raise ValueError("that shape is a table: give 'row' and 'col' (both starting at 1)")
            table = shape.table
            _pptx_set_paragraphs(table.cell(position("row", len(table.rows)), position("col", len(table.columns))).text_frame, lines)
        elif shape.has_text_frame:
            _pptx_set_paragraphs(shape.text_frame, lines)
        else:
            raise ValueError("that shape cannot hold text")
    elif kind == "set_notes":
        slides[position("slide", len(slides))].notes_slide.notes_text_frame.text = str(op["text"])
    elif kind == "add_slide":
        spec = {k: v for k, v in op.items() if k in _PPTX_SLIDE_KEYS}
        err = _pptx_validate_slide(spec, "the new slide")
        if err:
            raise ValueError(err.removeprefix("Error: "))
        at = position("at", len(slides) + 1) if "at" in op else len(slides)
        _pptx_add_slide(prs, spec, "the new slide", image)
        _pptx_move_slide(prs, len(slides), at)
    elif kind == "delete_slide":
        _pptx_delete_slide(prs, position("slide", len(slides)))
    elif kind == "move_slide":
        _pptx_move_slide(prs, position("slide", len(slides)), position("to", len(slides)))


@tool(
    "Editing presentation",
    path="Path to the existing .pptx file to change.",
    operations={
        "type": "array",
        "description": (
            "Edits applied in order; slide numbers refer to the deck as it stands at that step. Each is an object "
            "with 'op' and its fields: "
            "replace_text {find, replace, slide?} replaces text everywhere (or on one slide), keeping formatting; "
            "set_text {slide, shape, text, row?, col?} sets a shape's text by the [id] shown in the outline "
            "(row and col for a table cell; one paragraph per line, two leading spaces per indent level); "
            "set_notes {slide, text}; "
            "add_slide {at?, layout?, title?, subtitle?, bullets?, notes?, table?, image_path?}; "
            "delete_slide {slide}; move_slide {slide, to}."
        ),
        "items": {"type": "object", "properties": {"op": {"type": "string", "enum": list(_PPTX_OPS)}}, "required": ["op"]},
    },
)
def edit_pptx(path: str, operations: list[dict]) -> str:
    """Change an existing PowerPoint (.pptx) presentation: replace text, set a shape's or table cell's text, set
    speaker notes, and add, delete or reorder slides. Read the file first: read_file shows each slide's layout
    and every shape's [id]."""
    try:
        _need("pptx", PPTX_FEATURE)
        target, err = _guard_write(path, ext=(".pptx",))
        if err:
            return err
        if not os.path.isfile(target):
            return f"Error: File not found: {path}"
        if not isinstance(operations, list) or not operations:
            return "Error: 'operations' must be a non-empty list."
        if len(operations) > 100:
            return "Error: more than 100 operations in one call."

        before = _read_bytes_or_none(target)
        prs = _open_presentation(before)
        old_outline = _pptx_outline(prs)
        images = []
        for n, op in enumerate(operations, start=1):
            image = None
            if isinstance(op, dict) and op.get("op") == "add_slide" and str(op.get("image_path", "")).strip():
                image, err = _pptx_readable(op["image_path"], (".png", ".jpg", ".jpeg"), "image")
                if err:
                    return err
                images.append(f"Image (operation {n}): {image}")
            try:
                _pptx_apply(prs, op, image)
            except ValueError as e:
                return f"Error in operation {n}: {e}. No changes were made."
        if _pptx_outline(prs) == old_outline:
            return "Error: the operations did not change anything. No changes were made."

        err = _pptx_approve_and_save(prs, target, before, old_outline, f"Edit presentation: {target}",
                                     "[dim]" + escape(_visible_controls("\n".join(images))) + "[/dim]" if images else "")
        if err:
            return err
        console.print(f"[bold green]✓ Presentation edited ({len(operations)} operations): {escape(target)}[/bold green]\n")
        return f"Success: Applied {len(operations)} operation(s) to {target}"
    except ToolUnavailable as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error editing presentation '{path}': {e}"


# =====================================================================
# FILE AND DOCUMENT CONTENT SEARCH
# =====================================================================

SEARCHABLE_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".py", ".ps1", ".bat", ".cmd",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".xml", ".html", ".js", ".ts",
}
MAX_OFFICE_PARSE_FILES = 40
MAX_SEARCH_FILE_BYTES = 15 * 1024 * 1024  # 15 MB


@tool(
    "Searching files",
    path="Root directory to search from (default is current directory '.').",
    query="Optional case-insensitive text or keyword to search for inside files and filenames.",
    file_pattern="Optional filename filter or glob pattern (e.g. '*.docx', '*.xlsx', '*memo*', default '*').",
    max_results="Maximum number of matching files to return (default 30).",
)
def search_files(
    path: str = ".",
    query: str = "",
    file_pattern: str = "*",
    max_results: int = 30,
) -> str:
    """Search recursively across a directory tree by filename pattern and/or inside file contents (searches
    plaintext, code, CSV, Word .docx, Excel .xlsx, PDF .pdf, and PowerPoint .pptx in one call)."""
    try:
        root_dir, err = _resolve_model_path(path)
        if err:
            return err
        if not os.path.exists(root_dir):
            return f"Error: Search path does not exist: {path}"
        if not os.path.isdir(root_dir):
            return f"Error: Search path is not a directory: {path}"
        if _is_sensitive_canon(root_dir):
            return f"Security Error: '{path}' is a protected location and will not be searched."
        if not _confirm_outside_workspace(root_dir, "Search files"):
            return "Operation aborted: User did NOT approve searching outside the working directory."

        max_results = max(1, min(int(max_results), 100))
        q_lower = query.strip().lower()
        pat_lower = (file_pattern.strip() or "*").lower()
        results = []
        office_parsed_count = 0
        files_scanned = 0
        unreadable: set[str] = set()  # reasons a document type could not be read at all

        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Prune ignored, protected and reparse-point directories in-place. os.walk(followlinks=False)
            # still descends into junctions, so they are pruned explicitly; everything left under the
            # canonical root is therefore canonical and the lexical guard is sufficient.
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in IGNORE_DIRS
                and not _is_link_or_junction(os.path.join(dirpath, d))
                and not _is_sensitive_canon(os.path.join(dirpath, d))
            ]

            for fname in sorted(filenames):
                check_user_abort()
                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root_dir)

                if _is_link_or_junction(full_path) or _is_sensitive_canon(full_path) or _check_phi_guardrail(full_path):
                    continue

                # Check filename pattern match (supports both glob like '*.docx' and substring like 'memo')
                fname_lower = fname.lower()
                if pat_lower != "*":
                    if not (fnmatch.fnmatch(fname_lower, pat_lower) or pat_lower in fname_lower):
                        continue

                # If no content query was given, just return matching filenames
                if not q_lower:
                    try:
                        sz = os.path.getsize(full_path)
                        results.append(f"[FILE] {rel_path} ({sz} bytes)")
                    except OSError:
                        results.append(f"[FILE] {rel_path}")
                    if len(results) >= max_results:
                        break
                    continue

                # If query matches the filename itself, record that immediately
                matched_lines = []
                if q_lower in fname_lower:
                    matched_lines.append("  (Matched in filename)")

                ext = os.path.splitext(fname_lower)[1]
                try:
                    file_size = os.path.getsize(full_path)
                except OSError:
                    continue
                if file_size > MAX_SEARCH_FILE_BYTES:
                    continue

                text_to_scan = ""
                if ext in SEARCHABLE_TEXT_EXTS:
                    files_scanned += 1
                    try:
                        with _open_text_lenient(full_path) as f:
                            text_to_scan = f.read(200_000)
                    except OSError:
                        pass
                elif ext in _DOC_READERS and office_parsed_count < MAX_OFFICE_PARSE_FILES:
                    office_parsed_count += 1
                    files_scanned += 1
                    try:
                        text_to_scan = _DOC_READERS[ext](full_path)
                    except ToolUnavailable as e:
                        unreadable.add(str(e))
                    except Exception:
                        pass

                if text_to_scan and not _check_phi_guardrail(full_path, text_to_scan):
                    for line_num, line in enumerate(text_to_scan.splitlines(), start=1):
                        if q_lower in line.lower():
                            snippet = line.strip()
                            if len(snippet) > 140:
                                idx = snippet.lower().find(q_lower)
                                start = max(0, idx - 40)
                                snippet = ("..." if start > 0 else "") + snippet[start : start + 140] + "..."
                            matched_lines.append(f"  L{line_num}: {snippet}")
                            if len(matched_lines) >= 4:
                                break

                if matched_lines:
                    results.append(f"Match: {rel_path}\n" + "\n".join(matched_lines))
                    if len(results) >= max_results:
                        break

            if len(results) >= max_results:
                break

        skipped = "".join(f"\nNote: some documents were not searched: {reason}" for reason in sorted(unreadable))
        if not results:
            return (
                f"No matches found in '{root_dir}' for query='{query}', file_pattern='{file_pattern}' "
                f"(scanned {files_scanned} files)." + skipped
            )

        header = (
            f"Found {len(results)} matching file(s) in '{root_dir}' "
            f"(scanned {files_scanned} files, {office_parsed_count} Office/PDF docs):\n\n"
        )
        return header + "\n\n".join(results) + skipped
    except Exception as e:
        return f"Error searching files in '{path}': {e}"

# =====================================================================
# CLASSIC OUTLOOK COM TOOLS (inbox triage, calendar, drafts)
# =====================================================================

def _dev_mode_gate(what: str) -> str | None:
    """Dev mode talks to the public Gemini API, and mailbox data is real operational data. Returns an abort
    message unless the user explicitly allows this one read (always None outside dev mode)."""
    if not DEV_MODE:
        return None
    ok = _permission(
        "dev_data",
        f"DEV MODE: send {what} to the public Gemini API",
        "[bold yellow]This session is NOT GenAI.mil.[/bold yellow] "
        f"Approving sends {escape(what)} from this machine's Outlook profile to Google's public API. "
        "Approve only on a machine with test data.",
    )
    return None if ok else f"Operation aborted: User did NOT approve sending {what} to the public API in dev mode."


def _get_outlook_ns():
    """Returns (Outlook.Application, MAPI Namespace) or raises RuntimeError."""
    try:
        app = _need("win32com.client", "Outlook support").Dispatch("Outlook.Application")
        ns = app.GetNamespace("MAPI")
        return app, ns
    except Exception as e:
        raise RuntimeError(f"Classic Outlook COM is unavailable ({e}). Ensure Classic Outlook is open.")


def _safe_prop(item, prop: str, default=""):
    """Reads a COM property safely without crashing if Outlook STIG blocks it."""
    try:
        val = getattr(item, prop)
        return val if val is not None else default
    except Exception:
        return default


@tool(
    "Checking Outlook Inbox",
    max_items="Maximum number of emails to return (default 25, max 100).",
    unread_only="If true, only return unread emails (default false).",
    subject_filter="Optional case-insensitive substring to filter email subjects.",
)
def list_inbox(
    max_items: int = 25,
    unread_only: bool = False,
    subject_filter: str = "",
) -> str:
    """List recent emails in the user's Classic Outlook Inbox (returns Subject, ReceivedTime, Unread status,
    Categories, and EntryID). Supports filtering by unread status or subject keywords."""
    try:
        gate = _dev_mode_gate("inbox subject lines")
        if gate:
            return gate
        _, ns = _get_outlook_ns()
        inbox = ns.GetDefaultFolder(6)  # olFolderInbox = 6
        items = inbox.Items
        items.Sort("[ReceivedTime]", True)

        subj_q = subject_filter.strip().lower()
        max_items = max(1, min(max_items, 100))
        rows = []
        scanned = 0

        item = items.GetFirst()
        while item is not None and len(rows) < max_items and scanned < 400:
            scanned += 1
            is_unread = bool(_safe_prop(item, "UnRead", False))
            if unread_only and not is_unread:
                item = items.GetNext()
                continue

            subj = str(_safe_prop(item, "Subject", "(No Subject)")).strip()
            if subj_q and subj_q not in subj.lower():
                item = items.GetNext()
                continue

            received = str(_safe_prop(item, "ReceivedTime", ""))[:19]
            msg_class = str(_safe_prop(item, "MessageClass", "IPM.Note"))
            categories = str(_safe_prop(item, "Categories", "")).strip()
            entry_id = str(_safe_prop(item, "EntryID", ""))

            status_tag = "[UNREAD]" if is_unread else "[READ]  "
            cat_tag = f" [Cat: {categories}]" if categories else ""
            rows.append(
                f"{len(rows) + 1}. {status_tag} {received} | {subj}{cat_tag}\n"
                f"   Class: {msg_class} | EntryID: {entry_id}"
            )
            item = items.GetNext()

        if not rows:
            return "No matching emails found in Outlook Inbox."

        header = (
            f"Outlook Inbox ({len(rows)} shown, scanned {scanned} recent items):\n"
            "Note: DoW Outlook STIG blocks programmatic reading of email bodies/senders via COM. "
            "To read an email's body, either call `open_email(entry_id)` so the user can Ctrl+A/Ctrl+C it and then call `read_clipboard()`, "
            "or ask the user to copy the email text to the clipboard.\n\n"
        )
        return header + "\n".join(rows)
    except Exception as e:
        return f"Error listing Outlook Inbox: {e}"


_OL_FOLDER_CALENDAR = 9   # OlDefaultFolders.olFolderCalendar
_OL_APPOINTMENT_ITEM = 1  # OlItemType.olAppointmentItem
_OL_MODULE_CALENDAR = 1   # OlNavigationModuleType.olModuleCalendar
_MAX_CALENDARS = 50       # discovery stops here, so a huge profile cannot stall a call
_MAX_CALENDAR_EVENTS = 100
_ALL_CALENDARS = {"all", "*"}
_BUSY_MAP = {0: "Free", 1: "Tentative", 2: "Busy", 3: "Out of Office", 4: "Working Elsewhere"}


def _com_items(collection):
    """The members of a 1-based Outlook COM collection. A member that cannot be fetched is skipped."""
    try:
        count = int(collection.Count)
    except Exception:
        return
    for i in range(1, count + 1):
        try:
            item = collection.Item(i)
        except Exception:
            continue
        if item is not None:
            yield item


def _outlook_window(app):
    """An open Outlook window (Explorer), or None. Never creates one: Folder.GetExplorer() would."""
    try:
        explorer = app.ActiveExplorer()
        if explorer is not None:
            return explorer
    except Exception:
        pass
    try:
        return next(_com_items(app.Explorers), None)
    except Exception:
        return None


def _calendar_folders(app, ns) -> tuple[list[dict], list[str]]:
    """Every calendar this Outlook profile can reach, the default calendar first, plus notes on what could not
    be searched.

    NameSpace.GetDefaultFolder(9) is only the calendar of the default mailbox. The others are found in two
    places: the Calendar navigation pane (what the user sees in Outlook, including shared calendars), and each
    store's own default calendar with its sub-calendars (shared and additional mailboxes). Only folder names,
    paths and IDs are read here, none of which the Outlook Object Model Guard protects."""
    cals: list[dict] = []
    notes: list[str] = []

    def same_folder(cal: dict, entry_id: str, path: str) -> bool:
        if entry_id and cal["entry_id"]:
            if cal["entry_id"] == entry_id:
                return True
            try:  # one folder can have a short-term and a long-term EntryID, which differ as strings
                return bool(ns.CompareEntryIDs(cal["entry_id"], entry_id))
            except Exception:
                return False
        return bool(path) and cal["path"] == path

    def add(folder, source: str, nav_name: str = "", shown=None, error: str = "", is_default: bool = False):
        entry_id = str(_safe_prop(folder, "EntryID", "")) if folder is not None else ""
        path = str(_safe_prop(folder, "FolderPath", "")) if folder is not None else ""
        if folder is not None and (entry_id or path):
            for cal in cals:
                if cal["folder"] is not None and same_folder(cal, entry_id, path):
                    if source not in cal["sources"]:
                        cal["sources"].append(source)
                    cal["nav_name"] = cal["nav_name"] or nav_name
                    if shown is not None:
                        cal["shown"] = shown
                    return
        if len(cals) >= _MAX_CALENDARS:
            if not any("more were not listed" in n for n in notes):
                notes.append(f"Stopped at {_MAX_CALENDARS} calendars; more were not listed.")
            return
        folder_name = str(_safe_prop(folder, "Name", "")).strip() if folder is not None else ""
        store_name = ""
        if folder is not None:
            try:
                store_name = str(_safe_prop(folder.Store, "DisplayName", "")).strip()
            except Exception:
                pass
        cals.append({"folder": folder, "nav_name": nav_name, "folder_name": folder_name, "store": store_name,
                     "path": path, "entry_id": entry_id, "sources": [source], "shown": shown, "error": error,
                     "default": is_default})

    try:
        add(ns.GetDefaultFolder(_OL_FOLDER_CALENDAR), "default", is_default=True)
    except Exception as e:
        notes.append(f"The default calendar could not be opened ({e}).")

    window = _outlook_window(app) if app is not None else None
    if window is None:
        notes.append("No Outlook window is open, so the Calendar navigation pane (where calendars shared by other "
                     "people are listed) could not be searched. Open Classic Outlook's main window and try again.")
    else:
        try:
            module = window.NavigationPane.Modules.GetNavigationModule(_OL_MODULE_CALENDAR)
            for group in _com_items(module.NavigationGroups):
                for nav in _com_items(group.NavigationFolders):
                    nav_name = str(_safe_prop(nav, "DisplayName", "")).strip()
                    shown = _safe_prop(nav, "IsSelected", None)  # checked in the navigation pane
                    shown = bool(shown) if isinstance(shown, (bool, int)) else None
                    try:
                        folder = nav.Folder
                        if folder is None:
                            raise RuntimeError("Outlook returned no folder")
                    except Exception as e:  # a shared calendar whose owner or server cannot be reached right now
                        add(None, "navigation pane", nav_name or "(unnamed calendar)", shown, error=str(e))
                        continue
                    add(folder, "navigation pane", nav_name, shown)
        except Exception as e:
            notes.append(f"The Calendar navigation pane could not be read ({e}).")

    def walk(folder, depth: int):
        if depth > 3:
            return
        try:
            subfolders = folder.Folders
        except Exception:
            return
        for sub in _com_items(subfolders):
            if _safe_prop(sub, "DefaultItemType", None) == _OL_APPOINTMENT_ITEM:
                add(sub, "mailbox")
                walk(sub, depth + 1)

    try:
        stores = list(_com_items(ns.Stores))
    except Exception as e:
        stores = []
        notes.append(f"The list of mailboxes could not be read ({e}).")
    for store in stores:
        try:
            # Store.GetDefaultFolder returns None when the store has no calendar. (The NameSpace method of the
            # same name can create a missing folder; this one does not.)
            root = store.GetDefaultFolder(_OL_FOLDER_CALENDAR)
        except Exception:
            continue
        if root is None:
            continue
        add(root, "mailbox")
        walk(root, 1)

    # One label per calendar, unique, which is what get_calendar(calendar=...) and the event tags use
    used: set[str] = set()
    default_store = next((c["store"] for c in cals if c["default"]), "")
    for cal in cals:
        label = cal["nav_name"] or cal["folder_name"] or "(unnamed calendar)"
        if not cal["nav_name"] and cal["store"] and cal["store"] != default_store:
            label = f"{cal['store']} / {label}"
        base, n = label, 2
        while label.casefold() in used:
            label, n = f"{base} ({n})", n + 1
        used.add(label.casefold())
        cal["label"] = label
    return cals, notes


def _match_calendars(cals: list[dict], wanted: str) -> list[dict]:
    """Calendars a name refers to. An exact name wins over a partial one; a mailbox name selects its calendars."""
    q = " ".join(wanted.split()).casefold()
    if not q:
        return []

    def names(cal):
        return [n.casefold() for n in (cal["label"], cal["nav_name"], cal["folder_name"], cal["path"]) if n]

    exact = [c for c in cals if q in names(c)]
    if exact:
        return exact
    return [c for c in cals if any(q in n for n in names(c) + [c["store"].casefold()])]


def _describe_calendars(cals: list[dict]) -> str:
    rows = []
    for i, cal in enumerate(cals, 1):
        marks = "".join([" [default]" if cal["default"] else "",
                         " [checked in Outlook]" if cal["shown"] else "",
                         f" [UNREACHABLE: {cal['error']}]" if cal["folder"] is None else ""])
        where = " | ".join(part for part in (f"mailbox: {cal['store']}" if cal["store"] else "",
                                             f"path: {cal['path']}" if cal["path"] else "",
                                             "found via: " + ", ".join(cal["sources"])) if part)
        rows.append(f"{i}. {cal['label']}{marks}\n   {where}")
    return "\n".join(rows)


def _calendar_window(days_ahead: int, days_back: int):
    now = datetime.now()
    start_dt = (now - timedelta(days=max(0, min(days_back, 60)))).replace(hour=0, minute=0, second=0)
    end_dt = (now + timedelta(days=max(1, min(days_ahead, 60)))).replace(hour=23, minute=59, second=59)
    return start_dt, end_dt


def _calendar_events(folder, start_dt, end_dt, limit: int) -> tuple[list[tuple[str, str]], int]:
    """(start, line) for each appointment of one calendar folder that overlaps the window, and how many subjects
    were withheld by the PHI guardrail. Raises if Outlook rejects the date filter."""
    # Outlook only expands recurring meetings when the collection is sorted by Start *before*
    # IncludeRecurrences is set, and the filter is applied last.
    items = folder.Items
    items.Sort("[Start]")
    items.IncludeRecurrences = True

    # Outlook Restrict filter uses US date format MM/DD/YYYY HH:MM AM/PM
    start_str = start_dt.strftime("%m/%d/%Y %I:%M %p")
    end_str = end_dt.strftime("%m/%d/%Y %I:%M %p")
    # Overlap test, so a meeting already in progress at the start of the window is included
    restriction = f"[End] >= '{start_str}' AND [Start] <= '{end_str}'"
    try:
        filtered = items.Restrict(restriction)
    except Exception as e:
        # Never fall back to the unfiltered collection: with recurrences expanded it has no end, and its
        # first entries would be reported under the requested date range.
        raise RuntimeError(f"the date filter was rejected ({e})")

    events, withheld = [], 0
    ev = filtered.GetFirst()
    while ev is not None and len(events) < limit:
        subj = str(_safe_prop(ev, "Subject", "(Untitled Event)")).strip()
        start_val = str(_safe_prop(ev, "Start", ""))[:16]
        end_val = str(_safe_prop(ev, "End", ""))[:16]
        loc = str(_safe_prop(ev, "Location", "")).strip()
        all_day = bool(_safe_prop(ev, "AllDayEvent", False))
        busy_str = _BUSY_MAP.get(_safe_prop(ev, "BusyStatus", 2), "Busy")

        # A shared calendar holds other people's entries; one that names PHI keeps its time and loses its text
        if _check_phi_guardrail("calendar entry", f"{subj}\n{loc}"):
            subj, loc, withheld = "[withheld: PHI marker in this entry]", "", withheld + 1

        loc_str = f" @ {loc}" if loc else ""
        all_day_str = " [ALL DAY]" if all_day else ""
        events.append((start_val, f"- {start_val} to {end_val}{all_day_str} ({busy_str}): **{subj}**{loc_str}"))
        ev = filtered.GetNext()
    return events, withheld


@tool("Listing Outlook calendars")
def list_calendars() -> str:
    """List every calendar in the user's Classic Outlook profile: their own, sub-calendars, shared mailboxes'
    calendars, and calendars other people shared (for example a unit leave or plans calendar). Returns the
    names to pass to `get_calendar(calendar=...)`. Names and locations only, no appointments."""
    try:
        gate = _dev_mode_gate("calendar names")
        if gate:
            return gate
        app, ns = _get_outlook_ns()
        cals, notes = _calendar_folders(app, ns)
        if not cals:
            return "Error listing Outlook calendars: none were found. " + " ".join(notes)
        out = f"Outlook calendars ({len(cals)}):\n" + _describe_calendars(cals)
        out += "\n\nPass a name (or part of one) as `calendar` to `get_calendar`, or \"all\" for every calendar."
        if notes:
            out += "\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes)
        return out
    except Exception as e:
        return f"Error listing Outlook calendars: {e}"


@tool(
    "Checking Outlook Calendar",
    days_ahead="Number of days ahead from today to include (default 7).",
    days_back="Number of days in the past to include (default 0).",
    calendar="Which calendar to read. Empty (default) is the user's own calendar. Otherwise a calendar or "
             "mailbox name or part of one (for example \"SCXP Plans\"), or \"all\" for every calendar. "
             "`list_calendars` shows the names.",
)
def get_calendar(days_ahead: int = 7, days_back: int = 0, calendar: str = "") -> str:
    """Read appointments and meetings from the user's Classic Outlook calendars for today and upcoming/past days
    (returns Subject, Start, End, Location, AllDayEvent, and BusyStatus). Reads the user's own calendar unless
    `calendar` names a shared or additional one."""
    try:
        gate = _dev_mode_gate("calendar entries")
        if gate:
            return gate
        app, ns = _get_outlook_ns()
        start_dt, end_dt = _calendar_window(days_ahead, days_back)
        span = f"{start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}"
        wanted = str(calendar or "").strip()

        notes: list[str] = []  # what discovery could not search; reported only when nothing matches
        if not wanted:
            # The user's own calendar, without looking for any other: the common case stays one folder lookup
            chosen = [{"folder": ns.GetDefaultFolder(_OL_FOLDER_CALENDAR), "label": "", "error": ""}]
        else:
            cals, notes = _calendar_folders(app, ns)
            chosen = cals if wanted.casefold() in _ALL_CALENDARS else _match_calendars(cals, wanted)
            if not chosen:
                listing = _describe_calendars(cals) if cals else "(none found)"
                return (f"Error reading Outlook Calendar: no calendar matches '{wanted}'. Available calendars:\n"
                        f"{listing}" + ("\n\nNotes:\n" + "\n".join(f"- {n}" for n in notes) if notes else ""))

        tagged = len(chosen) > 1
        events: list[tuple[str, str]] = []
        failures: list[str] = []
        withheld = 0
        for cal in chosen:
            name = cal["label"] or "Outlook Calendar"
            if cal["folder"] is None:
                failures.append(f"{name}: unreachable ({cal['error']})")
                continue
            try:
                # one more than is shown, so a cut-off list can say that it was cut off
                found, hidden = _calendar_events(cal["folder"], start_dt, end_dt, _MAX_CALENDAR_EVENTS + 1)
            except Exception as e:  # one calendar that cannot be read must not hide the others
                failures.append(f"{name}: {e}")
                continue
            withheld += hidden
            events += [(start, f"{line} [{name}]" if tagged else line) for start, line in found]

        if failures and len(failures) == len(chosen):
            return "Error reading Outlook Calendar: " + "; ".join(failures) + "."

        events.sort(key=lambda pair: pair[0])  # stable, so each calendar's own order survives ties
        truncated = len(events) > _MAX_CALENDAR_EVENTS
        shown = events[:_MAX_CALENDAR_EVENTS]

        which = ""
        if tagged:
            which = f" from {len(chosen)} calendars"
        elif chosen[0]["label"]:
            which = f" '{chosen[0]['label']}'"
        tail = ""
        if truncated:
            tail += (f"\n[Showing the first {_MAX_CALENDAR_EVENTS} events. Use a shorter date range or a single "
                     "calendar for the rest.]")
        if withheld:
            tail += f"\n[{withheld} entr{'y' if withheld == 1 else 'ies'} had the subject withheld: PHI is not processed.]"
        if failures:
            tail += "\nCould not read: " + "; ".join(failures) + "."
        if not shown:
            return (f"No calendar events found{which} between {start_dt.strftime('%Y-%m-%d')} and "
                    f"{end_dt.strftime('%Y-%m-%d')}.") + tail
        header = f"Outlook Calendar{which} ({span}, {len(shown)} events):\n"
        return header + "\n".join(line for _, line in shown) + tail
    except Exception as e:
        return f"Error reading Outlook Calendar: {e}"


@tool(
    "Opening Outlook email",
    entry_id="The exact EntryID string of the email from `list_inbox`.",
    open_reply_window="If true, opens a Reply draft window for the email instead of just viewing it (default false).",
)
def open_email(entry_id: str, open_reply_window: bool = False) -> str:
    """Pop open a specific Outlook email (or a Reply window for it) on the user's screen using its EntryID from
    `list_inbox`."""
    try:
        _, ns = _get_outlook_ns()
        item = ns.GetItemFromID(entry_id.strip())
        subj = str(_safe_prop(item, "Subject", "(Email)"))

        action = f"Open Outlook Reply window for: {subj}" if open_reply_window else f"Display Outlook email on screen: {subj}"
        if not _permission("outlook", action):
            return "Operation aborted: User declined to open the Outlook item."

        if open_reply_window:
            reply = item.Reply()
            reply.Display()
            console.print(f"[bold green]✓ Opened Reply window in Outlook: {escape(subj)}[/bold green]")
            return f"Success: Opened Reply window in Outlook for '{subj}'."
        else:
            item.Display()
            console.print(f"[bold green]✓ Displayed email in Outlook: {escape(subj)}[/bold green]")
            return (
                f"Success: Displayed '{subj}' on the user's screen in Outlook. "
                "(Tip: If you need to read its body, ask the user to press Ctrl+A, Ctrl+C in the window and then call `read_clipboard`.)"
            )
    except Exception as e:
        return f"Error opening Outlook email '{entry_id}': {e}"


@tool(
    "Creating Outlook draft",
    subject="Subject line for the draft email.",
    body="Plaintext body of the draft email.",
    to_hint="Optional recipient email address or name.",
)
def create_draft_email(subject: str, body: str, to_hint: str = "") -> str:
    """Create a new draft email in Classic Outlook and pop it open on the user's screen ready for review and
    manual sending. Never auto-sends."""
    try:
        app, _ = _get_outlook_ns()
        preview_lines = [f"Subject: {subject}"]
        if to_hint:
            preview_lines.append(f"Intended Recipient(s) (add in Outlook To: line): {to_hint}")
        preview_lines.append("--- Draft Body ---")
        preview_lines.append(_head_lines(body, 40))

        if not _permission(
            "outlook",
            f"Create Outlook Draft: {subject}",
            "[cyan]" + escape(_visible_controls("\n".join(preview_lines))) + "[/cyan]",
        ):
            return "Operation aborted: User did NOT approve creating the Outlook draft."

        mail = app.CreateItem(0)  # olMailItem = 0
        mail.Subject = subject
        # Try setting To if STIG permits on new items; if blocked by Object Model Guard, prepend recipient hint to body
        if to_hint.strip():
            try:
                mail.To = to_hint.strip()
                mail.Body = body
            except Exception:
                mail.Body = f"[Suggested To: {to_hint.strip()}]\n\n{body}"
        else:
            mail.Body = body

        mail.Display()
        console.print(f"[bold green]✓ Outlook draft opened on screen: {escape(subject)}[/bold green]\n")
        return f"Success: Draft email '{subject}' opened in Outlook for user review. (Never auto-sent)."
    except Exception as e:
        return f"Error creating Outlook draft: {e}"


# =====================================================================
# SKILLS
# =====================================================================

def _skill_file_blocked(canon: str) -> bool:
    """Secrets patterns and the custom deny list still apply inside a skill folder. (The Eustace-home check in
    _is_sensitive_canon is skipped: ~/.eustace/skills is where your own skills live, and only this tool reads it.)"""
    name = os.path.basename(canon).lower()
    norm = os.path.normcase(os.path.normpath(canon))
    return any(fnmatch.fnmatch(name, p.lower()) or fnmatch.fnmatch(norm, p.lower())
               for p in SENSITIVE_PATTERNS | _load_custom_deny_patterns())


@tool(
    "Loading skill",
    name="The skill's name, from the Skills list in your instructions.",
    file="Optional: a text file inside the skill's folder to read instead of SKILL.md, as a relative path such as 'references/forms.md'.",
)
def load_skill(name: str, file: str = "") -> str:
    """Load a skill's full instructions (its SKILL.md), or a file the skill points to. Call it when a request
    matches a skill in the Skills list, before starting the work."""
    skill = SKILLS.get(str(name).strip().lower())
    if not skill:
        available = ", ".join(SKILLS) or "none in this session"
        return f"Error: no skill named '{name}'. Available skills: {available}."
    digest, err = _skill_digest(skill["dir"])
    if err or digest != skill["digest"]:
        return (f"Error: the files of skill '{skill['name']}' changed after it was loaded at startup"
                f"{f' ({err})' if err else ''}, so it was not read. Tell the user; it loads again after they run "
                "/clear or restart Eustace and approve the new version.")
    rel = str(file or "SKILL.md").strip().replace("\\", "/")
    target = os.path.realpath(os.path.join(skill["dir"], *[p for p in rel.split("/") if p]))
    inside = os.path.normcase(target).startswith(os.path.normcase(skill["dir"]) + os.sep)
    if not inside or os.path.isabs(rel) or ":" in rel:
        return f"Error: '{rel}' is not inside the skill's folder."
    if not os.path.isfile(target):
        return f"Error: skill '{skill['name']}' has no file '{rel}'."
    if _skill_file_blocked(target):
        return f"Security Policy Block: '{rel}' matches a secrets or deny-list pattern."
    try:
        with open(target, "rb") as f:
            raw = f.read(MAX_SKILL_FILE_CHARS * 4 + 1)
    except OSError as e:
        return f"Error reading '{rel}': {e.strerror or e}"
    if b"\0" in raw[:8192]:
        return f"Error: '{rel}' is not a text file. Use read_file with the path {target} for documents."
    text = raw.decode("utf-8-sig", errors="replace")
    phi = _check_phi_guardrail(target, text)
    if phi:
        return phi
    if rel.lower() == "skill.md":
        text = (_parse_frontmatter(text) or ({}, text))[1]
        header = (f"[Skill '{skill['name']}' from {skill['dir']}. Relative paths in it are relative to that folder: "
                  f"read files with load_skill(name, file=...); run scripts by their full path.]\n\n")
    else:
        header = f"[File '{rel}' of skill '{skill['name']}']\n\n"
    if len(text) > MAX_SKILL_FILE_CHARS:
        text = text[:MAX_SKILL_FILE_CHARS] + f"\n\n[Truncated at {MAX_SKILL_FILE_CHARS} characters.]"
    return header + text


# =====================================================================
# SYSTEM PROMPT AND AGENT STREAMING LOOP
# =====================================================================

# The persona lives in system_prompt.md: the user's copy in the Eustace home (setup.ps1 puts it there and it is
# theirs to edit), else the one shipped beside this file when running from a source checkout.
GLOBAL_PROMPT_PATH = os.path.join(EUSTACE_HOME, "system_prompt.md")
BUNDLED_PROMPT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
FALLBACK_SYSTEM_PROMPT = (
    "You are Eustace, a CLI assistant running locally on the user's Windows workstation. Be clear and direct. "
    "Use your tools to inspect local files yourself instead of asking the user to paste them, and say what you "
    "are about to change before writing files or running commands."
)


def _load_persona() -> str:
    for path in (GLOBAL_PROMPT_PATH, BUNDLED_PROMPT_PATH):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read().strip()
            if text:
                return text
        except OSError:
            continue
    console.print(f"[dim yellow]No persona file at {escape(GLOBAL_PROMPT_PATH)}; using the built-in default.[/dim yellow]")
    return FALLBACK_SYSTEM_PROMPT


def build_system_prompt() -> str:
    """Loads global system_prompt.md from ~/.eustace/ and optional local AGENT.md."""
    cwd = os.getcwd()
    base_prompt = _load_persona()

    tool_names = ", ".join(f"`{name}`" for name in AVAILABLE_FUNCTIONS.keys())
    if DEV_MODE:
        data_policy = (
            "- Data Handling Policy (DEV MODE): This session uses the PUBLIC Google Gemini API, not GenAI.mil. It exists "
            "only for functional testing of the tool. Never read, process, or generate CUI, PII, PHI, or any real "
            "operational data; work only with synthetic test files and text.\n"
        )
        heading = "Runtime Environment & Security Boundary (DEV MODE - public Gemini API)"
    else:
        data_policy = (
            "- Data Handling Policy (IL5): GenAI.mil is authorized to process Controlled Unclassified Information (CUI) "
            "and Personally Identifiable Information (PII) consistent with DoW IL5 security protocols. If generating "
            "outputs containing PII, portion-mark appropriately as `(CUI)`. You are strictly PROHIBITED from processing, "
            "extracting, or generating Protected Health Information (PHI / medical records) or local credential/secret files.\n"
        )
        heading = "Runtime Environment & Security Boundary (IL5 / DoW)"
    env_context = (
        f"\n\n# {heading}\n"
        f"- Current Working Directory: `{cwd}`\n"
        f"- Available Tools: {tool_names}\n"
        f"{data_policy}"
    )

    # Instruction files: ~/.eustace/AGENT.md loads silently (it lives in the protected home). Project AGENT.md
    # files from cwd up to the enclosing git root load only after an explicit, displayed trust decision that is
    # pinned to the file's hash, so a swapped or appended file prompts again.
    global_agent_md = os.path.join(EUSTACE_HOME, "AGENT.md")
    if os.path.isfile(global_agent_md):
        try:
            with open(global_agent_md, "rb") as f:
                raw = f.read(MAX_AGENT_MD_BYTES + 1)
            if len(raw) > MAX_AGENT_MD_BYTES:
                console.print(f"[bold yellow]Skipped oversized instruction file (> {MAX_AGENT_MD_BYTES // 1024} KiB): {escape(global_agent_md)}[/bold yellow]")
            else:
                text = raw.decode("utf-8", errors="replace").strip()
                if text:
                    env_context += f"\n# Office / Project Instructions ({global_agent_md})\n{text}\n"
                    console.print(f"  [dim]Loaded instructions: {escape(global_agent_md)}[/dim]")
        except OSError:
            pass

    for p in _agent_md_candidates(cwd):
        text = _load_trusted_agent_md(p)
        if text:
            env_context += f"\n# Office / Project Instructions ({p})\n{text}\n"
            console.print(f"  [dim]Loaded instructions: {escape(p)}[/dim]")

    SKILLS.clear()
    SKILLS.update(discover_skills(cwd))
    if SKILLS:
        console.print(f"  [dim]Loaded {len(SKILLS)} skill{'s' if len(SKILLS) != 1 else ''}: {escape(', '.join(SKILLS))}[/dim]")
    env_context += _skills_prompt_section(SKILLS)

    return base_prompt + env_context


MAX_AGENT_MD_BYTES = 16 * 1024
_AGENT_MD_SESSION_DENIED: dict[str, str] = {}  # realpath -> sha256 declined for this process


def _git_root(start: str) -> str | None:
    probe = start
    while True:
        if os.path.isdir(os.path.join(probe, ".git")) or os.path.isfile(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def _agent_md_candidates(cwd: str) -> list[str]:
    """AGENT.md in cwd plus ancestors up to the enclosing git root (broadest first); never up to a drive or share root."""
    curr = os.path.realpath(cwd)
    root = _git_root(curr)
    chain = []
    while True:
        candidate = os.path.join(curr, "AGENT.md")
        if os.path.isfile(candidate) and not _is_link_or_junction(candidate):
            chain.append(candidate)
        if root is None or os.path.normcase(curr) == os.path.normcase(root):
            break
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return list(reversed(chain))


def _load_trusted_agent_md(path: str) -> str | None:
    """Reads an instruction file once (bounded) and returns its text only if the user trusts these exact bytes."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_AGENT_MD_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_AGENT_MD_BYTES:
        console.print(f"[bold yellow]Skipped oversized instruction file (> {MAX_AGENT_MD_BYTES // 1024} KiB): {escape(path)}[/bold yellow]")
        return None
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    key = os.path.normcase(os.path.realpath(path))

    trusted = _read_config().get("trusted_agent_md")
    if not isinstance(trusted, dict):
        trusted = {}
    if trusted.get(key) == digest:
        return text
    if _AGENT_MD_SESSION_DENIED.get(key) == digest:
        return None

    preview_lines = text.splitlines()[:40]
    preview = _visible_controls("\n".join(preview_lines))
    if len(text.splitlines()) > 40:
        preview += f"\n... ({len(text.splitlines()) - 40} more lines)"
    console.print()
    console.print(
        Panel(
            escape(preview),
            title=f"[bold yellow]⚠️  Untrusted instruction file: {escape(path)}[/bold yellow]",
            subtitle=f"[dim]{len(raw)} bytes | sha256 {digest[:16]}... | will be loaded as system instructions[/dim]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    console.print("  [bold yellow]Trust this file and load it as instructions? (y/N):[/bold yellow] ", end="")
    choice = input().strip().lower()
    if choice in ("y", "yes"):
        _update_config(trusted_agent_md={**trusted, key: digest})
        return text
    _AGENT_MD_SESSION_DENIED[key] = digest
    console.print(f"  [dim]Skipped {escape(path)} for this session.[/dim]")
    return None


# ---------------------------------------------------------------------
# AGENT SKILLS (the open SKILL.md format): <skills dir>/<name>/SKILL.md, whose frontmatter has a name and a
# description. Only the name and description go in the system prompt; load_skill returns the rest on demand.
# Skills in ~/.eustace/skills load silently (that folder is protected, like ~/.eustace/AGENT.md). Skills in
# ~/.agents/skills and in a project's .agents/skills load only after a trust decision pinned to a hash of the
# whole skill folder, so a changed SKILL.md, reference file or script prompts again.
# ---------------------------------------------------------------------

SKILL_NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_SKILL_NAME = 64
MAX_SKILL_DESCRIPTION = 1024
MAX_SKILL_MD_BYTES = 32 * 1024
MAX_SKILL_FILE_CHARS = 30000
MAX_SKILL_FOLDER_BYTES = 4 * 1024 * 1024
MAX_SKILL_FILES = 200
MAX_SKILLS = 50
AGENTS_SKILLS_HOME = os.path.join(os.path.expanduser("~"), ".agents", "skills")
SKILLS: dict[str, dict] = {}  # name -> {name, description, dir, source, digest}; rebuilt with the system prompt
_SKILL_SESSION_DENIED: dict[str, str] = {}  # normcased skill folder -> digest declined for this process


def _parse_frontmatter(text: str) -> tuple[dict, str] | None:
    """The top-level `key: value` pairs between the opening and closing --- lines, and the body after them.
    Handles quoted values and | or > block values; nested keys (such as under metadata:) are ignored."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    meta: dict[str, str] = {}
    key, block, sep = None, None, " "
    for line in lines[1:end] + ["end:"]:
        if block is not None:
            if line[:1] in (" ", "\t") or not line.strip():
                block.append(line.strip())
                continue
            meta[key] = sep.join(block).strip()
            key, block = None, None
        m = re.match(r"([A-Za-z0-9_-]+):(?:\s+(.*))?$", line)
        if not m:
            continue
        k, v = m.group(1).lower(), (m.group(2) or "").strip()
        if v in ("", "|", "|-", "|+", ">", ">-", ">+"):
            key, block, sep = k, [], "\n" if v.startswith("|") else " "
            continue
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        meta[k] = v
    return meta, "\n".join(lines[end + 1:]).strip()


def _skill_digest(folder: str) -> tuple[str | None, str | None]:
    """(sha256 over every file's relative path and bytes, None), or (None, why the folder can't be used).
    A link or junction anywhere inside makes the whole skill unusable rather than being followed."""
    h = hashlib.sha256()
    count = total = 0
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for d in dirs:
            if _is_link_or_junction(os.path.join(root, d)):
                return None, f"contains a link or junction ({d})"
        for name in sorted(files):
            path = os.path.join(root, name)
            if _is_link_or_junction(path):
                return None, f"contains a link ({name})"
            count += 1
            if count > MAX_SKILL_FILES:
                return None, f"has more than {MAX_SKILL_FILES} files"
            try:
                with open(path, "rb") as f:
                    data = f.read(MAX_SKILL_FOLDER_BYTES + 1)
            except OSError as e:
                return None, f"can't read {name} ({e.strerror or e})"
            total += len(data)
            if total > MAX_SKILL_FOLDER_BYTES:
                return None, f"is larger than {MAX_SKILL_FOLDER_BYTES // (1024 * 1024)} MiB"
            rel = os.path.relpath(path, folder).replace(os.sep, "/")
            h.update(rel.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.hexdigest(), None


def _read_skill(folder: str, source: str) -> tuple[dict | None, str | None]:
    """The skill in `folder` as a dict, or (None, why it was skipped)."""
    dirname = os.path.basename(os.path.normpath(folder))
    md = os.path.join(folder, "SKILL.md")
    if not os.path.isfile(md):
        return None, None  # not a skill folder; skipped without a warning
    if _is_link_or_junction(folder) or _is_link_or_junction(md):
        return None, "is a link or junction"
    try:
        with open(md, "rb") as f:
            raw = f.read(MAX_SKILL_MD_BYTES + 1)
    except OSError as e:
        return None, f"can't be read ({e.strerror or e})"
    if len(raw) > MAX_SKILL_MD_BYTES:
        return None, f"SKILL.md is larger than {MAX_SKILL_MD_BYTES // 1024} KiB"
    parsed = _parse_frontmatter(raw.decode("utf-8-sig", errors="replace"))
    if parsed is None:
        return None, "SKILL.md has no --- frontmatter"
    meta, body = parsed
    name, description = meta.get("name", "").strip(), " ".join(meta.get("description", "").split())
    if not (SKILL_NAME_RE.fullmatch(name) and len(name) <= MAX_SKILL_NAME):
        return None, "name must be lowercase letters, digits and single hyphens (at most 64)"
    if name != dirname:
        return None, f"name '{name}' doesn't match its folder name '{dirname}'"
    if not description:
        return None, "has no description"
    if not body:
        return None, "SKILL.md has no instructions after the frontmatter"
    digest, err = _skill_digest(folder)
    if err:
        return None, err
    return {"name": name, "description": _visible_controls(description[:MAX_SKILL_DESCRIPTION]),
            "dir": os.path.realpath(folder), "source": source, "digest": digest}, None


def _skill_roots(cwd: str) -> list[tuple[str, str, bool]]:
    """(skills folder, source label, needs trust) in precedence order: your own skills before a project's, so a
    repository can't replace one of yours by reusing its name. Project folders run from cwd up to the git root."""
    roots = [(os.path.join(EUSTACE_HOME, "skills"), "~/.eustace/skills", False),
             (AGENTS_SKILLS_HOME, "~/.agents/skills", True)]
    curr = os.path.realpath(cwd)
    top = _git_root(curr)
    while True:
        roots.append((os.path.join(curr, ".agents", "skills"), "project", True))
        if top is None or os.path.normcase(curr) == os.path.normcase(top):
            break
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return roots


def _confirm_skills(pending: list[dict]) -> list[dict]:
    """Shows the untrusted skills and returns the ones the user trusts; trust is saved per folder hash."""
    lines = []
    for n, s in enumerate(pending, start=1):
        lines.append(f"{n}. {s['name']}  ({s['dir']})\n   {s['description']}")
    console.print()
    console.print(Panel(
        escape(_visible_controls("\n".join(lines))),
        title=f"[bold yellow]⚠️  {len(pending)} untrusted skill{'s' if len(pending) != 1 else ''}[/bold yellow]",
        subtitle="[dim]a trusted skill's instructions are followed like AGENT.md; its scripts still ask before running[/dim]",
        border_style="yellow", box=box.ROUNDED, padding=(0, 1),
    ))
    console.print("  [bold yellow]Trust which? (a = all, numbers like 1,3, or Enter for none):[/bold yellow] ", end="")
    choice = input().strip().lower()
    if choice in ("a", "all", "y", "yes"):
        picked = list(range(len(pending)))
    else:
        picked = sorted({int(x) - 1 for x in re.findall(r"\d+", choice) if 0 < int(x) <= len(pending)})
    trusted = _read_config().get("trusted_skills")
    trusted = trusted if isinstance(trusted, dict) else {}
    chosen = [pending[i] for i in picked]
    for i, s in enumerate(pending):
        if i not in picked:
            _SKILL_SESSION_DENIED[os.path.normcase(s["dir"])] = s["digest"]
    if chosen:
        _update_config(trusted_skills={**trusted, **{os.path.normcase(s["dir"]): s["digest"] for s in chosen}})
    if len(chosen) < len(pending):
        console.print(f"  [dim]Skipped {len(pending) - len(chosen)} skill(s) for this session.[/dim]")
    return chosen


def discover_skills(cwd: str) -> dict[str, dict]:
    """Finds, validates and (where needed) asks about skills; returns name -> skill for this session."""
    found: dict[str, dict] = {}
    pending: list[dict] = []
    trusted = _read_config().get("trusted_skills")
    trusted = trusted if isinstance(trusted, dict) else {}
    seen_roots: set[str] = set()
    full = False
    for root, source, needs_trust in _skill_roots(cwd):
        if full:
            break
        real = os.path.normcase(os.path.realpath(root))
        if real in seen_roots or not os.path.isdir(root) or _is_link_or_junction(root):
            continue
        seen_roots.add(real)
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for entry in entries:
            folder = os.path.join(root, entry)
            if not os.path.isdir(folder):
                continue
            skill, why = _read_skill(folder, source)
            if why:
                console.print(f"  [dim yellow]Skipped skill {escape(folder)}: {escape(why)}[/dim yellow]")
            if not skill:
                continue
            name = skill["name"]
            if f"/{name}" in COMMANDS or name in _COMMAND_ALIASES:
                console.print(f"  [dim yellow]Skipped skill {escape(name)}: /{escape(name)} is a built-in command.[/dim yellow]")
                continue
            if name in found or any(p["name"] == name for p in pending):
                console.print(f"  [dim yellow]Skipped skill {escape(folder)}: a skill named {escape(name)} is already loaded.[/dim yellow]")
                continue
            if len(found) + len(pending) >= MAX_SKILLS:
                console.print(f"  [dim yellow]More than {MAX_SKILLS} skills; the rest were skipped.[/dim yellow]")
                full = True
                break
            key = os.path.normcase(skill["dir"])
            if not needs_trust or trusted.get(key) == skill["digest"]:
                found[name] = skill
            elif _SKILL_SESSION_DENIED.get(key) != skill["digest"]:
                pending.append(skill)
    if pending:
        for s in _confirm_skills(pending):
            found[s["name"]] = s
    return dict(sorted(found.items()))


def _skills_prompt_section(skills: dict[str, dict]) -> str:
    if not skills:
        return ""
    rows = "\n".join(f"- `{s['name']}`: {s['description']}" for s in skills.values())
    return (
        "\n# Skills\n"
        "Skills are instruction packages for particular kinds of work. When a request matches a skill's description, "
        "call `load_skill` with its name before starting, then follow what it says. Load only skills that match. "
        "Files a skill mentions (such as references/...) are read with `load_skill(name, file=...)`; scripts it "
        "mentions are run with `run_powershell` using the folder path that `load_skill` reports.\n"
        f"{rows}\n"
    )


# --- ESC detection: peek the console input buffer without consuming typed-ahead text ---
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]

_STD_INPUT_HANDLE = wintypes.DWORD(-10 & 0xFFFFFFFF)
_KEY_EVENT = 0x0001
_VK_ESCAPE = 0x1B


class _KeyEventRecord(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("UnicodeChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", _KeyEventRecord), ("_raw", ctypes.c_byte * 16)]


class _InputRecord(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]


_kernel32.PeekConsoleInputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(_InputRecord), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
]


def check_user_abort():
    """Raises KeyboardInterrupt if ESC is waiting in the console input buffer."""
    try:
        handle = _kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        pending = wintypes.DWORD(0)
        if not _kernel32.GetNumberOfConsoleInputEvents(handle, ctypes.byref(pending)) or pending.value == 0:
            return
        records = (_InputRecord * pending.value)()
        read = wintypes.DWORD(0)
        if not _kernel32.PeekConsoleInputW(handle, records, pending.value, ctypes.byref(read)):
            return
        for i in range(read.value):
            rec = records[i]
            key = rec.Event.KeyEvent
            if rec.EventType == _KEY_EVENT and key.bKeyDown and key.wVirtualKeyCode == _VK_ESCAPE:
                _kernel32.FlushConsoleInputBuffer(handle)
                raise KeyboardInterrupt("Aborted via ESC key")
    except KeyboardInterrupt:
        raise
    except Exception:
        while msvcrt.kbhit():
            if msvcrt.getch() == b"\x1b":
                raise KeyboardInterrupt("Aborted via ESC key")


def _render_markdown(text: str) -> Markdown:
    """Model text as Markdown with control characters exposed. hyperlinks=False prints each link's URL beside its
    text instead of hiding it behind a clickable label, so a link cannot display one destination and open another."""
    return Markdown(_sanitize_for_display(text), hyperlinks=False)


MAX_TOOL_RESULT_CHARS = 30000
MAX_TOOL_ROUNDS = 40
ARG_PREVIEW_CHARS = 120


def _summarize_args(fn_name: str, fn_args: dict) -> str:
    """Builds a clean, user-friendly summary of what a tool call is doing."""
    label = TOOL_FRIENDLY_LABELS.get(fn_name, fn_name)
    if "path" in fn_args:
        detail = os.path.basename(str(fn_args["path"])) or str(fn_args["path"])
    elif "template_path" in fn_args:
        detail = f"{os.path.basename(str(fn_args['template_path']))} -> {os.path.basename(str(fn_args.get('output_path', '')))}"
    elif "query" in fn_args and fn_args["query"]:
        detail = f"\"{fn_args['query']}\""
    elif "subject" in fn_args:
        detail = f"\"{fn_args['subject']}\""
    elif "command" in fn_args:
        cmd = str(fn_args["command"])
        detail = cmd[:60] + ("..." if len(cmd) > 60 else "")
    elif fn_name == "write_clipboard":
        detail = f"{len(fn_args.get('text', ''))} chars"
    else:
        parts = [f"{k}={v}" for k, v in fn_args.items() if v not in ("", None, False)]
        detail = ", ".join(parts)

    return f"[bold cyan]{escape(label)}[/bold cyan]" + (f" [dim]({escape(_visible_controls(detail))})[/dim]" if detail else "")


def _tail_lines(text: str, max_lines: int) -> str:
    """Returns the last max_lines lines of text (used for the live streaming preview)."""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def _accumulate_tool_call(buffer: dict[int, dict], tc) -> None:
    """Folds one streamed tool-call delta into buffer (position -> call under construction).

    OpenAI-style streams tag every delta with an index. Some compatible endpoints omit it; then a delta that
    carries a different id, or a function name when the current call already has one, starts a new call and
    anything else continues the latest one (so arguments split across chunks stay in one call)."""
    idx = getattr(tc, "index", None)
    fn = getattr(tc, "function", None)
    if idx is None:
        last = max(buffer) if buffer else None
        if last is None:
            idx = 0
        else:
            cur = buffer[last]
            new_id = bool(tc.id) and bool(cur["id"]) and tc.id != cur["id"]
            new_name = bool(fn and fn.name) and bool(cur["function"]["name"])
            idx = last + 1 if (new_id or new_name) else last
    entry = buffer.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if tc.id:
        entry["id"] = tc.id
    if fn:
        if fn.name:
            entry["function"]["name"] += fn.name
        if fn.arguments:
            entry["function"]["arguments"] += fn.arguments
    extra = _tool_call_extra(tc)
    if extra:
        entry["extra_content"] = extra  # Gemini thought_signature; must be echoed back verbatim


def _parse_tool_args(fn_name: str, raw_args: str | None) -> tuple[dict | None, str | None]:
    """(arguments, None) when the call is well formed for a known tool, else (None, error for the model).
    Rejects unknown tools, invalid JSON (typically output cut off mid-call), non-object arguments, parameters
    the tool's schema does not declare, and missing required parameters."""
    schema = _TOOL_SCHEMAS.get(fn_name)
    if schema is None:
        return None, f"Error: Unknown tool '{fn_name}'"
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError as je:
        return None, f"Error: the arguments for '{fn_name}' were not valid JSON ({je}). Nothing was run; send the call again."
    if not isinstance(args, dict):
        return None, f"Error: the arguments for '{fn_name}' must be a JSON object. Nothing was run."
    unknown = sorted(set(args) - set(schema.get("properties", {})))
    if unknown:
        return None, f"Error: '{fn_name}' does not accept the parameter(s): {', '.join(unknown)}. Nothing was run."
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return None, f"Error: '{fn_name}' is missing required parameter(s): {', '.join(missing)}. Nothing was run."
    return args, None


INTERRUPTED_TOOL_NOTE = (
    "Not completed: the user interrupted the turn. If this call had already started it may have partly run; "
    "check its effects before retrying."
)


def _close_open_tool_calls(messages: list[dict], turn_start: int) -> None:
    """Appends a stub result for each tool call of this turn that has none, so every call stays paired with a
    result (the API rejects a history where one is missing). Tools run in order, so the unanswered calls are
    always the tail of the last assistant message that requested any."""
    for i in range(len(messages) - 1, turn_start - 1, -1):
        calls = messages[i].get("tool_calls") if messages[i].get("role") == "assistant" else None
        if calls:
            answered = sum(1 for m in messages[i + 1:] if m.get("role") == "tool")
            for tc in calls[answered:]:
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": INTERRUPTED_TOOL_NOTE})
            return


def _ran_tool_calls(messages: list[dict], turn_start: int) -> list[str]:
    """One line per tool call of this turn that produced a result: '- name(args) -> start of result'."""
    lines, pending = [], []
    for m in messages[turn_start:]:
        if m.get("role") == "assistant":
            pending = list(m.get("tool_calls") or [])
        elif m.get("role") == "tool" and pending:
            fn = pending.pop(0)["function"]
            result = str(m.get("content", "")).replace("\n", " ")
            lines.append(f"- {fn['name']}({fn['arguments'][:150]}) -> {result[:150]}")
    return lines


def _clear_status_line() -> None:
    """Blanks the transient 'Thinking...' line written with a carriage return."""
    print(" " * 70, end="\r", flush=True)


def run_agent_turn(messages: list[dict]):
    """Streams responses & tool calls with instant ESC or Ctrl+C cancellation."""
    turn_start_len = len(messages)
    partial_reply = ""
    APPROVAL_HANDLER.reset_turn()
    _TURN["id"] += 1
    turn = _TURN["id"]

    try:
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            if round_no == MAX_TOOL_ROUNDS:
                # Tools that need no approval could otherwise loop without the user ever being asked anything
                note = f"(Paused after {MAX_TOOL_ROUNDS} rounds of tool calls in one turn. Say \"continue\" to keep going.)"
                console.print(f"\n  [bold yellow]{escape(note)}[/bold yellow]")
                messages.append({"role": "assistant", "content": note})
                break
            check_user_abort()
            console.print("  [dim cyan]Thinking... (press ESC or Ctrl+C to stop)[/dim cyan]", end="\r")

            stream = _StreamWorker(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
            )

            full_reply = ""
            tool_calls_buffer = {}
            live = None
            first_chunk = True

            try:
                for chunk in stream:
                    if first_chunk:
                        first_chunk = False
                        _clear_status_line()

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue

                    if delta.content:
                        full_reply += delta.content
                        partial_reply = full_reply
                        preview = Text(_tail_lines(_sanitize_for_display(full_reply), max(console.size.height - 4, 5)))
                        if live is None:
                            print()
                            live = Live(
                                preview,
                                console=console,
                                transient=True,
                                refresh_per_second=10,
                                vertical_overflow="crop",
                            )
                            live.start()
                        else:
                            live.update(preview)

                    for tc in delta.tool_calls or []:
                        _accumulate_tool_call(tool_calls_buffer, tc)
            finally:
                if live is not None:
                    live.stop()
                stream.cancel()
                if first_chunk:
                    _clear_status_line()

            if full_reply:
                console.print()
                console.print(
                    Panel(
                        _render_markdown(full_reply),
                        title=f"[bold cyan]Eustace[/bold cyan] [dim]({short_model_label(MODEL)}{' | DEV' if DEV_MODE else ''})[/dim]",
                        title_align="left",
                        border_style="cyan",
                        box=box.ROUNDED,
                        padding=(0, 1),
                    )
                )
                partial_reply = ""

            if tool_calls_buffer:
                tool_calls_list = []
                for i in sorted(tool_calls_buffer.keys()):
                    item = tool_calls_buffer[i]
                    entry = {
                        "id": item["id"],
                        "type": "function",
                        "function": {
                            "name": item["function"]["name"],
                            "arguments": item["function"]["arguments"],
                        },
                    }
                    if item.get("extra_content"):
                        entry["extra_content"] = item["extra_content"]  # Gemini thought_signature round-trip
                    tool_calls_list.append(entry)

                assistant_msg = {
                    "role": "assistant", 
                    "content": full_reply if full_reply else None,
                    "tool_calls": tool_calls_list
                }
                messages.append(assistant_msg)

                for tc in tool_calls_list:
                    check_user_abort()
                    fn_name = tc["function"]["name"]
                    fn_args, arg_err = _parse_tool_args(fn_name, tc["function"]["arguments"])

                    console.print(f"  [yellow]●[/yellow] {_summarize_args(fn_name, fn_args or {})}")

                    APPROVAL_HANDLER.last_decision = "auto"
                    APPROVAL_HANDLER.current_tool = fn_name
                    if arg_err:
                        # Never run a tool on arguments the model did not actually send
                        console.print(f"  [bold red]{escape(arg_err)}[/bold red]")
                        result = arg_err
                    else:
                        try:
                            result = AVAILABLE_FUNCTIONS[fn_name](**fn_args)
                        except Exception as fe:
                            console.print(f"[bold red]Tool execution error inside '{escape(fn_name)}':[/bold red] {escape(str(fe))}")
                            result = f"Error: Tool execution failed: {fe}"

                    feedback = getattr(APPROVAL_HANDLER, "last_feedback", "")
                    if feedback:
                        result = f"{result}\nThe user said: {feedback}"
                        APPROVAL_HANDLER.last_feedback = ""

                    log_audit_event(fn_name, fn_args or {}, APPROVAL_HANDLER.last_decision, result)

                    content = str(result)
                    if len(content) > MAX_TOOL_RESULT_CHARS:
                        content = content[:MAX_TOOL_RESULT_CHARS] + "\n... [Tool output truncated for context]"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": content,
                    })

                continue

            print()
            messages.append({"role": "assistant", "content": full_reply})
            break

    except KeyboardInterrupt:
        console.print("\n  [bold red]⏹ Stopped by user[/bold red]")
        if partial_reply:
            console.print(_render_markdown(partial_reply))
            console.print("[dim](partial reply)[/dim]")
        # Keep what already happened: tool calls that ran changed real files, and the model must still know.
        _close_open_tool_calls(messages, turn_start_len)
        note = "(Response interrupted by the user before completion.)"
        messages.append({"role": "assistant", "content": f"{partial_reply}\n\n{note}" if partial_reply else note})
    except Exception as e:
        err_line = _format_api_error(e)
        console.print(f"\n[bold red]System Loop Error:[/bold red] {escape(err_line)}")
        if partial_reply:
            console.print(_render_markdown(partial_reply))
            console.print("[dim](partial reply)[/dim]")
        # Roll the turn back so a request the API rejected cannot poison every later one, but carry a plain-text
        # record of the tool calls that did run.
        ran = _ran_tool_calls(messages, turn_start_len)
        del messages[turn_start_len:]
        note = f"(Response failed with an error: {err_line})"
        if ran:
            note += "\nTool calls that completed before the failure:\n" + "\n".join(ran)
        messages.append({"role": "assistant", "content": note})
    finally:
        _finalize_checkpoints(turn)


COMPACT_INSTRUCTION = (
    "Summarize this entire conversation so far for your own future reference. Capture the user's goals, "
    "decisions made, files inspected or changed (with their paths), key facts learned, and any unfinished "
    "work. Be thorough but concise. Reply with the summary only."
)


def compact_messages(messages: list[dict]) -> list[dict]:
    """Replaces the conversation history with a model-written summary to free up context space."""
    if len(messages) <= 1:
        return messages

    console.print("  [dim cyan]Compacting conversation... (press ESC or Ctrl+C to cancel)[/dim cyan]", end="\r")
    # Same worker as a normal turn, so ESC cancels. The history contains tool calls, so the tool list must
    # accompany it; tool_choice "none" makes the model answer in text.
    worker = _StreamWorker(
        model=MODEL,
        messages=messages + [{"role": "user", "content": COMPACT_INSTRUCTION}],
        tools=TOOLS,
        tool_choice="none",
        stream=True,
    )
    summary = ""
    try:
        for chunk in worker:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta is not None and delta.content:
                summary += delta.content
    finally:
        worker.cancel()
        _clear_status_line()

    summary = summary.strip()
    if not summary:
        raise RuntimeError("Model returned an empty summary; history left unchanged.")

    return [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": f"[Context compacted] Summary of our conversation so far:\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the summary and will continue from here."},
    ]

# =====================================================================
# PERSISTENT SESSIONS (/save, /resume, /sessions, autosave)
# =====================================================================

SESSIONS_DIR = os.path.join(EUSTACE_HOME, "sessions")
MAX_AUTOSAVES = 10
# Each run autosaves to its own file, so starting a new conversation never overwrites the previous one.
AUTOSAVE_NAME = datetime.now().strftime("auto_%Y%m%d_%H%M%S")


def _sanitize_session_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_")
    return clean or datetime.now().strftime("session_%Y%m%d_%H%M%S")


def save_session(messages: list[dict], name: str = "", quiet: bool = False) -> str:
    """Saves non-system conversation messages to ~/.eustace/sessions/<name>.json (default: this run's autosave)."""
    slug = _sanitize_session_name(name or AUTOSAVE_NAME)
    path = os.path.join(SESSIONS_DIR, f"{slug}.json")

    non_sys = [m for m in messages if m.get("role") != "system"]
    if not non_sys:
        if not quiet:
            console.print("[dim yellow]Nothing to save yet (conversation is empty).[/dim yellow]")
        return ""

    first_user = next((m.get("content", "") for m in non_sys if m.get("role") == "user"), "")
    payload = {
        "name": slug,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backend": BACKEND_NAME,
        "cwd": os.getcwd(),
        "turn_count": sum(1 for m in non_sys if m.get("role") == "user"),
        "preview": str(first_user)[:100],
        "messages": non_sys,
    }
    _write_json_atomic(path, payload)
    if slug == AUTOSAVE_NAME:
        _prune_autosaves()

    if not quiet:
        console.print(f"[dim cyan]✓ Session saved as '[bold]{escape(slug)}[/bold]' ({escape(path)})[/dim cyan]")
    return path


def _prune_autosaves() -> None:
    """Keeps the newest MAX_AUTOSAVES per-run autosave files; named sessions are never pruned."""
    try:
        autos = sorted(f for f in os.listdir(SESSIONS_DIR) if re.fullmatch(r"auto_\d{8}_\d{6}\.json", f))
        for stale in autos[:-MAX_AUTOSAVES]:  # the timestamped names sort chronologically
            os.remove(os.path.join(SESSIONS_DIR, stale))
    except OSError:
        pass


def list_saved_sessions() -> list[dict]:
    """Returns metadata for all saved sessions in ~/.eustace/sessions/, newest first."""
    if not os.path.exists(SESSIONS_DIR):
        return []
    items = []
    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(SESSIONS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            items.append({
                "name": data.get("name", fname[:-5]),
                "updated_at": data.get("updated_at", ""),
                "turn_count": data.get("turn_count", 0),
                "preview": data.get("preview", ""),
                "mtime": os.path.getmtime(fpath),
                "path": fpath,
            })
        except Exception:
            continue
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def load_session(target: str = "") -> list[dict] | None:
    """Loads a saved session by name or 1-based index (default: the most recent one from an earlier run)
    and prepends the fresh system prompt."""
    sessions = list_saved_sessions()
    if not sessions:
        console.print("[dim yellow]No saved sessions found.[/dim yellow]")
        return None

    chosen = None
    t = target.strip()
    if not t:
        chosen = next((s for s in sessions if s["name"] != AUTOSAVE_NAME), None)
        if chosen is None:
            console.print("[dim yellow]No earlier session to resume.[/dim yellow]")
            return None
    elif t.isdigit():
        idx = int(t) - 1
        if 0 <= idx < len(sessions):
            chosen = sessions[idx]
    else:
        slug = _sanitize_session_name(t)
        for s in sessions:
            if s["name"].lower() == slug.lower():
                chosen = s
                break

    if not chosen:
        console.print(f"[bold red]Session '{escape(t)}' not found.[/bold red] Use [cyan]/sessions[/cyan] to list available sessions.")
        return None

    with open(chosen["path"], "r", encoding="utf-8") as f:
        data = json.load(f)

    restored = [{"role": "system", "content": build_system_prompt()}] + data.get("messages", [])
    console.print(
        f"[dim cyan]✓ Resumed session '[bold]{escape(chosen['name'])}[/bold]' "
        f"({chosen['turn_count']} turns, last active {chosen['updated_at']})[/dim cyan]"
    )
    return restored

def select_model_interactive(arg: str = "") -> str:
    """Interactive /model selector that updates global MODEL and saves to <EUSTACE_HOME>/config.json."""
    choice = arg.strip()

    models = list(AVAILABLE_MODELS)
    known = {m[0] for m in models}
    models += [m for m in _live_models() if m[0] not in known]  # dev mode only

    # If the user just typed "/model" without a name, show a pretty table of options
    if not choice:
        tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        tbl.add_column("#", style="bold yellow", width=3)
        tbl.add_column("Model ID", style="bold white")
        tbl.add_column("Mode", style="green")
        tbl.add_column("Description", style="dim")

        for idx, (m_id, desc) in enumerate(models, start=1):
            active = " (active)" if m_id == MODEL else ""
            mode = "⚡ Tools" if (DEV_MODE or m_id.endswith("-withtools")) else "💬 Chat only"
            style = "bold cyan" if m_id == MODEL else None
            tbl.add_row(str(idx), f"{escape(m_id)}{active}", mode, desc, style=style)

        title = f"[bold cyan]Select AI Model ({escape(BACKEND_NAME)})[/bold cyan]"
        console.print(Panel(tbl, title=title, border_style="cyan", box=box.ROUNDED))
        console.print("[bold cyan]Enter model # or name (or press Enter to keep current):[/bold cyan] ", end="")
        choice = input().strip()
        if not choice:
            return MODEL

    def _switch(new_id: str, note: str = "") -> str:
        global MODEL
        MODEL = new_id
        _update_config(model=MODEL)
        label = "✓ Switched to custom model ID:" if note == "custom" else "✓ Switched model to:"
        console.print(f"  [bold {'yellow' if note == 'custom' else 'green'}]{label}[/bold {'yellow' if note == 'custom' else 'green'}] [bold cyan]{escape(MODEL)}[/bold cyan]")
        return MODEL

    # If they selected a number
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            return _switch(models[idx][0])
        console.print("[bold red]Invalid model number.[/bold red]")
        return MODEL

    # If they typed the exact name
    for m_id, _ in models:
        if choice.lower() == m_id.lower():
            return _switch(m_id)

    # Suffix matcher: "argon" -> "gemini-4-argon-withtools" (prod requires a tool-enabled model)
    for m_id, _ in models:
        if choice.lower() in m_id.lower() and (DEV_MODE or m_id.endswith("-withtools")):
            return _switch(m_id)

    # Fallback to whatever string they typed
    return _switch(choice, note="custom")

# =====================================================================
# TERMINAL UI: diff rendering, approval menu, input box, slash commands
# =====================================================================

UI_FEATURE = "The interactive input box"
_UI_IO: dict = {"input": None, "output": None}  # tests substitute a pipe input and a dummy output
_DEL_STYLE, _ADD_STYLE = "#ffc1c5 on #4a1519", "#b9f0c0 on #12361a"
_UI_STYLE = {
    "frame.border": "#6a6a6a", "prompt": "bold ansicyan", "status": "#8f8f8f",
    "menu.current": "bg:#1f3a3a #ffffff", "menu.meta": "#8f8f8f", "question": "bold", "current": "ansicyan",
    "auto": "bold ansiyellow", "admin": "bg:ansired #0c0c0c bold", "flash": "ansiyellow",
}
ADMIN_BADGE = " ⚠ ADMIN "
AUTO_BADGE = "⏵⏵ AUTO "


def _ui_enabled() -> bool:
    """True when the input box and arrow-key menus can be used: a real console on both ends, not switched off
    with EUSTACE_PLAIN=1, and prompt_toolkit loadable. Otherwise Eustace uses plain typed prompts."""
    if os.environ.get("EUSTACE_PLAIN", "").strip() not in ("", "0"):
        return False
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return False
        _need("prompt_toolkit", UI_FEATURE)
        return True
    except Exception:
        return False


def _flush_console_input() -> None:
    """Discards keystrokes typed before a prompt appeared, so typed-ahead text cannot answer it."""
    try:
        _kernel32.FlushConsoleInputBuffer(_kernel32.GetStdHandle(_STD_INPUT_HANDLE))
    except Exception:
        pass


def _diff_rows(diff_text: str) -> list[tuple]:
    """Parses a unified diff into display rows (kind, old_no, old_text, new_no, new_text):
    'ctx' is an unchanged line, 'chg' pairs a removed line with an added one (either side may be None),
    'sep' separates hunks, and 'note' carries a remark in old_text."""
    rows, dels, adds = [], [], []
    old_no = new_no = 1
    seen_hunk = False

    def flush():
        for i in range(max(len(dels), len(adds))):
            d = dels[i] if i < len(dels) else (None, None)
            a = adds[i] if i < len(adds) else (None, None)
            rows.append(("chg", d[0], d[1], a[0], a[1]))
        dels.clear()
        adds.clear()

    for line in diff_text.split("\n"):
        hunk = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            flush()
            if seen_hunk:
                rows.append(("sep", None, None, None, None))
            seen_hunk = True
            old_no, new_no = max(int(hunk.group(1)), 1), max(int(hunk.group(2)), 1)
        elif not seen_hunk and line.startswith(("--- ", "+++ ")):
            continue
        elif line.startswith("-"):
            dels.append((old_no, line[1:]))
            old_no += 1
        elif line.startswith("+"):
            adds.append((new_no, line[1:]))
            new_no += 1
        elif line.startswith("\\"):
            flush()
            rows.append(("note", None, line[1:].strip(), None, None))
        elif line:
            flush()
            text = line[1:] if line.startswith(" ") else line
            rows.append(("ctx", old_no, text, new_no, text))
            old_no += 1
            new_no += 1
    flush()
    return rows


def _render_diff(rows: list[tuple], width: int) -> Text:
    """Draws diff rows. Side by side (before | after) when the window is at least 100 columns, there is a
    'before' to show, and every changed line fits in half the width; otherwise stacked with line numbers."""
    numbers = [n for r in rows for n in (r[1], r[3]) if n is not None]
    num_w = max(len(str(max(numbers, default=0))), 2)
    col_w = (width - 2 * (num_w + 1) - 3) // 2
    changed = [r for r in rows if r[0] == "chg"]
    has_before = any(r[0] == "ctx" for r in rows) or any(r[2] is not None for r in changed)
    fits = all(cell_len(t) <= col_w for r in changed for t in (r[2], r[4]) if t is not None)
    out = Text(no_wrap=True, overflow="crop")

    def number(n) -> str:
        return f"{'' if n is None else n:>{num_w}} "

    if width >= 100 and has_before and fits:
        out.append(f"{'':>{num_w}} {set_cell_size('before', col_w)} │ {'':>{num_w}} after\n", style="dim")
        for kind, old_no, old, new_no, new in rows:
            if kind == "sep":
                out.append(f"{'':>{num_w}} {set_cell_size('⋯', col_w)} │ {'':>{num_w}} ⋯\n", style="dim")
            elif kind == "note":
                out.append(f"{'':>{num_w}} {old}\n", style="dim")
            else:
                left_style = _DEL_STYLE if kind == "chg" and old is not None else ""
                right_style = _ADD_STYLE if kind == "chg" and new is not None else ""
                out.append(number(old_no), style=left_style or "dim")
                out.append(set_cell_size(old or "", col_w), style=left_style)
                out.append(" │ ", style="dim")
                out.append(number(new_no), style=right_style or "dim")
                out.append(set_cell_size(new or "", col_w), style=right_style)
                out.append("\n")
    else:
        out.no_wrap, out.overflow = False, "fold"
        pad = max(width - num_w - 3, 1)
        for kind, old_no, old, new_no, new in rows:
            if kind == "sep":
                out.append(f"{'':>{num_w}}   ⋯\n", style="dim")
            elif kind == "note":
                out.append(f"{'':>{num_w}}   {old}\n", style="dim")
            elif kind == "ctx":
                out.append(number(new_no), style="dim")
                out.append(f"  {new}\n")
            else:
                if old is not None:
                    out.append(f"{number(old_no)}- {old}".ljust(pad + num_w + 3) + "\n", style=_DEL_STYLE)
                if new is not None:
                    out.append(f"{number(new_no)}+ {new}".ljust(pad + num_w + 3) + "\n", style=_ADD_STYLE)
    out.rstrip()
    return out


def _ui_app(root, key_bindings, focus=None):
    """A small inline (not full-screen) prompt_toolkit application that erases itself when it finishes."""
    pt = _need("prompt_toolkit.application", UI_FEATURE)
    layout = _need("prompt_toolkit.layout", UI_FEATURE).Layout(root, focused_element=focus)
    style = _need("prompt_toolkit.styles", UI_FEATURE).Style.from_dict(_UI_STYLE)
    app = pt.Application(layout=layout, key_bindings=key_bindings, style=style, full_screen=False,
                         erase_when_done=True, input=_UI_IO["input"], output=_UI_IO["output"])
    app.ttimeoutlen = 0.05  # a lone Esc is recognized quickly
    return app


def _ask_menu(question: str, options: list[tuple[str, str]], allow_view: bool = False) -> str | None:
    """Arrow-key menu. Returns the chosen option's key, "view" (V, when allowed), or None when cancelled.

    Nothing is highlighted at first and only Enter confirms, so neither a stray key nor text typed before the
    menu appeared can choose an option; pending keystrokes are discarded before it is shown."""
    kb = _need("prompt_toolkit.key_binding", UI_FEATURE).KeyBindings()
    layout = _need("prompt_toolkit.layout", UI_FEATURE)
    controls = _need("prompt_toolkit.layout.controls", UI_FEATURE)
    chosen = {"index": None}

    @kb.add("up")
    def _(event):
        chosen["index"] = len(options) - 1 if chosen["index"] is None else (chosen["index"] - 1) % len(options)

    @kb.add("down")
    def _(event):
        chosen["index"] = 0 if chosen["index"] is None else (chosen["index"] + 1) % len(options)

    for n in range(len(options)):
        kb.add(str(n + 1))(lambda event, n=n: chosen.update(index=n))

    @kb.add("enter")
    def _(event):
        if chosen["index"] is not None:
            event.app.exit(result=options[chosen["index"]][0])

    @kb.add("escape")
    @kb.add("c-c")
    def _(event):
        event.app.exit(result=None)

    if allow_view:
        @kb.add("v")
        def _(event):
            event.app.exit(result="view")

    def render():
        parts = [("class:question", question + "\n")]
        for i, (_, label) in enumerate(options):
            mark, style = ("❯ ", "class:current") if i == chosen["index"] else ("  ", "")
            parts.append((style, f"{mark}{i + 1}. {label}\n"))
        hint = "↑↓ to move · Enter to confirm · Esc to cancel" + (" · V to view the full diff" if allow_view else "")
        parts.append(("class:status", "\n" + hint))
        return parts

    window = layout.Window(controls.FormattedTextControl(render, focusable=True, show_cursor=False), dont_extend_height=True)
    _flush_console_input()
    return _ui_app(window, kb, focus=window).run()


def _ask_line(prompt_text: str) -> str:
    """One line of free text ("" if cancelled)."""
    shortcuts = _need("prompt_toolkit.shortcuts", UI_FEATURE)
    try:
        return shortcuts.PromptSession(input=_UI_IO["input"], output=_UI_IO["output"]).prompt(prompt_text).strip()
    except (KeyboardInterrupt, EOFError):
        return ""


def _complete_paths(fragment: str, limit: int = 50) -> list[str]:
    """Files and folders under the working directory matching a partly typed relative path. Shown to the user
    only, but protected files and ignored folders are still left out so they are never suggested."""
    fragment = fragment.replace("/", os.sep)
    folder, _, prefix = fragment.rpartition(os.sep)
    if os.path.isabs(fragment) or ".." in fragment.split(os.sep) or ":" in fragment:
        return []
    base = os.path.join(os.getcwd(), folder)
    found = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.lower().startswith(prefix.lower()) or entry.name in IGNORE_DIRS:
            continue
        try:
            if _is_sensitive_canon(_canonical_path(entry.path)):
                continue
        except ValueError:
            continue
        shown = os.path.join(folder, entry.name) if folder else entry.name
        found.append(shown + (os.sep if entry.is_dir() else ""))
        if len(found) >= limit:
            break
    return found


def _completions_for(text: str) -> list[tuple[str, int, str]]:
    """Completions for the text before the cursor as (replacement, start offset, description): slash commands
    while typing the first word, file paths after an @."""
    if text.startswith("/") and not any(c.isspace() for c in text):
        typed_text = text.lower()
        rows = [(name, -len(text), desc) for name, (desc, _) in COMMANDS.items() if name.startswith(typed_text)]
        rows += [(f"/{n}", -len(text), f"Skill: {s['description'][:70]}") for n, s in SKILLS.items()
                 if f"/{n}".startswith(typed_text)]
        return rows
    at = re.search(r"(?:^|\s)@([^\s@]*)$", text)
    if at:
        return [(path, -len(at.group(1)), "") for path in _complete_paths(at.group(1))]
    return []


def _prompt_box(status_left: str, status_right: str) -> str:
    """The boxed, multi-line input with a status line (or the completion list) beneath it.
    Returns the submitted text. Raises KeyboardInterrupt for Ctrl+C on an empty box, EOFError for Ctrl+D."""
    layout = _need("prompt_toolkit.layout", UI_FEATURE)
    controls = _need("prompt_toolkit.layout.controls", UI_FEATURE)
    completion = _need("prompt_toolkit.completion", UI_FEATURE)
    filters = _need("prompt_toolkit.filters", UI_FEATURE)
    kb = _need("prompt_toolkit.key_binding", UI_FEATURE).KeyBindings()
    Buffer = _need("prompt_toolkit.buffer", UI_FEATURE).Buffer
    Frame = _need("prompt_toolkit.widgets", UI_FEATURE).Frame
    FileHistory = _need("prompt_toolkit.history", UI_FEATURE).FileHistory

    class Completer(completion.Completer):
        def get_completions(self, document, complete_event):
            for value, start, meta in _completions_for(document.text_before_cursor):
                yield completion.Completion(value, start_position=start, display_meta=meta)

    os.makedirs(EUSTACE_HOME, exist_ok=True)
    buf = Buffer(multiline=True, completer=Completer(), complete_while_typing=True,
                 history=FileHistory(os.path.join(EUSTACE_HOME, "history")))
    flash = {"text": ""}
    buf.on_text_changed += lambda _: flash.update(text="")

    @kb.add("enter")
    def _(event):
        state = buf.complete_state
        if state and state.current_completion:       # a highlighted suggestion: take it, do not send yet
            buf.apply_completion(state.current_completion)
        elif buf.text.endswith("\\") and buf.cursor_position == len(buf.text):
            buf.delete_before_cursor(1)              # trailing backslash: continue on a new line
            buf.insert_text("\n")
        elif buf.text.strip():
            buf.append_to_history()
            event.app.exit(result=buf.text)

    @kb.add("escape", "enter")
    @kb.add("c-j")
    def _(event):
        buf.insert_text("\n")

    @kb.add("tab")
    def _(event):
        buf.complete_next() if buf.complete_state else buf.start_completion(select_first=True)

    @kb.add("s-tab")
    def _(event):
        # Steps back through an open completion list; otherwise switches auto mode (shown in the status line)
        if buf.complete_state:
            buf.complete_previous()
        elif not set_permission_mode("default" if PERMISSION_MODE == "auto" else "auto"):
            flash["text"] = ELEVATED_AUTO_REFUSAL  # shown in the status line until the next key

    @kb.add("c-c")
    def _(event):
        buf.reset() if buf.text else event.app.exit(exception=KeyboardInterrupt())

    @kb.add("c-d")
    def _(event):
        buf.delete() if buf.text else event.app.exit(exception=EOFError())

    def line_prefix(lineno, wrap_count):
        return [("class:prompt", "> ")] if lineno == 0 and wrap_count == 0 else [("", "  ")]

    def status():
        # The right side (token estimate) always stays visible; a long folder path loses its beginning instead
        admin = ADMIN_BADGE + " " if ELEVATED else ""
        badge = AUTO_BADGE if PERMISSION_MODE == "auto" else ""
        text = flash["text"] or status_left
        room = max(event_size().columns - cell_len(status_right) - cell_len(badge) - cell_len(admin) - 3, 0)
        left = text if cell_len(text) <= room else "…" + text[-max(room - 1, 0):] if room else ""
        parts = [("class:status", " ")]
        if admin:
            parts += [("class:admin", ADMIN_BADGE), ("class:status", " ")]
        if badge:
            parts.append(("class:auto", badge))
        parts.append(("class:flash" if flash["text"] else "class:status", set_cell_size(left, room)))
        parts.append(("class:status", f" {status_right}"))
        return parts

    def suggestions():
        state = buf.complete_state
        items = state.completions if state else []
        current = state.complete_index if state and state.complete_index is not None else -1
        first = max(0, min(current - 7, len(items) - 8))
        width = max((cell_len(c.text) for c in items), default=0)
        parts = []
        for i, c in enumerate(items[first:first + 8], start=first):
            style = "class:menu.current" if i == current else ""
            parts.append((style, f"  {set_cell_size(c.text, width)}  "))
            parts.append((style or "class:menu.meta", f"{c.display_meta_text}\n"))
        parts.append(("class:status", "  ↑↓ to move · Tab to complete · Enter to choose"))
        return parts

    def event_size():
        return _need("prompt_toolkit.application", UI_FEATURE).get_app().output.get_size()

    has_suggestions = filters.Condition(lambda: bool(buf.complete_state and buf.complete_state.completions))
    editor = layout.Window(controls.BufferControl(buffer=buf), get_line_prefix=line_prefix, wrap_lines=True,
                           height=layout.Dimension(min=1, max=10), dont_extend_height=True)
    root = layout.HSplit([
        Frame(editor),
        layout.ConditionalContainer(layout.Window(controls.FormattedTextControl(suggestions), dont_extend_height=True), filter=has_suggestions),
        layout.ConditionalContainer(layout.Window(controls.FormattedTextControl(status), height=1), filter=~has_suggestions),
    ])
    return _ui_app(root, kb, focus=editor).run()


def _estimate_tokens(messages: list[dict]) -> int:
    """Rough size of the conversation: about four characters per token. An estimate, not the provider's count."""
    chars = sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages)
    return chars // 4


def _short_count(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _read_user_input(messages: list[dict]) -> str:
    """One message from the user: the input box on a real console, a plain prompt otherwise."""
    if not _ui_enabled():
        badge = ("[bold red]ADMIN[/bold red] " if ELEVATED else "") + ("[bold yellow]AUTO[/bold yellow] " if PERMISSION_MODE == "auto" else "")
        console.print(f"\n{badge}[bold cyan]>[/bold cyan] ", end="")
        return input()
    home = os.path.expanduser("~")
    cwd = os.getcwd()
    folder = "~" + cwd[len(home):] if os.path.normcase(cwd).startswith(os.path.normcase(home)) else cwd
    left = f"{short_model_label(MODEL)}{' · DEV' if DEV_MODE else ''} · {folder}"
    console.print()
    text = _prompt_box(left, f"~{_short_count(_estimate_tokens(messages))} tokens")
    # The box erases itself; leave the message in the transcript the way a plain prompt would
    console.print("[bold cyan]>[/bold cyan] " + escape(_sanitize_for_display(text)).replace("\n", "\n  "))
    return text


class _Quit(Exception):
    """Raised by /exit to leave the main loop."""


def _cmd_help(state: dict, arg: str) -> None:
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 2))
    for name, (desc, _) in COMMANDS.items():
        table.add_row(f"[cyan]{escape(name)}[/cyan]", escape(desc))  # "[name]" in a description is text, not markup
    for name in SKILLS:
        table.add_row(f"[cyan]/{escape(name)}[/cyan]", "Run this skill: /" + escape(name) + " [request]")
    console.print(table)
    console.print(
        "  [dim]Enter sends · Alt+Enter or a trailing \\ adds a line · ↑↓ history · @ completes a file path\n"
        "  Shift+Tab switches auto mode · ESC or Ctrl+C stops a reply · EUSTACE_PLAIN=1 switches back to the plain prompt\n"
        "  /undo restores files changed by Eustace's file tools; changes made by shell commands are not covered[/dim]"
    )


def _cmd_status(state: dict, arg: str) -> None:
    turns = sum(1 for m in state["messages"] if m.get("role") == "user")
    console.print(
        f"  [dim]Model:[/dim]    {escape(MODEL)} [dim]({escape(BACKEND_NAME)})[/dim]\n"
        f"  [dim]Folder:[/dim]   {escape(os.getcwd())}\n"
        f"  [dim]Session:[/dim]  {escape(AUTOSAVE_NAME)} [dim]({turns} turns, about {_short_count(_estimate_tokens(state['messages']))} tokens)[/dim]\n"
        f"  [dim]Mode:[/dim]     {'auto' if PERMISSION_MODE == 'auto' else 'default (asks before writes and commands)'} "
        f"[dim]· {len(get_allow_rules())} allow rule(s)[/dim]"
        + (" [bold red]· running as Administrator: auto mode unavailable[/bold red]" if ELEVATED else "") + "\n"
        f"  [dim]Audit log:[/dim] {escape(AUDIT_LOG_PATH)}"
    )


def _cmd_save(state: dict, arg: str) -> None:
    save_session(state["messages"], name=arg or datetime.now().strftime("session_%Y%m%d_%H%M"))


def _cmd_resume(state: dict, arg: str) -> None:
    restored = load_session(arg)
    if restored is not None:
        state["messages"] = restored


def _cmd_sessions(state: dict, arg: str) -> None:
    saved = list_saved_sessions()
    if not saved:
        console.print("[dim yellow]No saved sessions yet.[/dim yellow]")
        return
    console.print("[bold cyan]Saved Sessions (~/.eustace/sessions/):[/bold cyan]")
    for idx, s in enumerate(saved, start=1):
        prev = escape(s["preview"].replace("\n", " "))
        console.print(
            f"  [bold]{idx}.[/bold] [cyan]{escape(s['name'])}[/cyan] "
            f"[dim]({s['turn_count']} turns | {s['updated_at']})[/dim] — \"{prev}\""
        )


def _cmd_compact(state: dict, arg: str) -> None:
    try:
        state["messages"] = compact_messages(state["messages"])
        save_session(state["messages"], quiet=True)
        console.print("[dim cyan]✓ Context compacted into a summary[/dim cyan]")
    except Exception as e:
        console.print(f"[bold red]Compact failed:[/bold red] {escape(str(e))}")


def _cmd_clear(state: dict, arg: str) -> None:
    state["messages"] = [{"role": "system", "content": build_system_prompt()}]
    state.pop("pending_note", None)
    console.print("[dim cyan]✓ Context cleared & system_prompt.md reloaded[/dim cyan]")


def _cmd_model(state: dict, arg: str) -> None:
    select_model_interactive(arg)
    state["messages"][0]["content"] = build_system_prompt()


def _cmd_exit(state: dict, arg: str) -> None:
    raise _Quit


AUTO_MODE_SUMMARY = (
    "Runs without asking: file reads anywhere, file writes, and shell commands.\n"
    "  Still asks: shell commands on the deny-list (deleting, registry, downloads, and similar), writes to\n"
    "  AGENT.md, system_prompt.md, .git, .github and .eustace, Outlook, clipboard, opening files, and dev-mode data.\n"
    "  Never allowed in any mode: protected folders, secrets files, PHI."
)


def _cmd_auto(state: dict, arg: str) -> None:
    a = arg.strip().lower()
    if a not in ("", "on", "off"):
        console.print("[dim yellow]Usage: /auto [on|off][/dim yellow]")
        return
    wanted = ("default" if PERMISSION_MODE == "auto" else "auto") if not a else "auto" if a == "on" else "default"
    if not set_permission_mode(wanted):
        console.print("[bold red]Auto mode isn't available while Eustace runs as Administrator.[/bold red]\n"
                      "[dim]Restart Eustace from a normal (non-elevated) PowerShell window to use it.\n"
                      "Approvals, \"approve all this turn\", and allow rules still work as usual.[/dim]")
        return
    if PERMISSION_MODE == "auto":
        console.print(f"[bold yellow]Auto mode on.[/bold yellow] [dim]{AUTO_MODE_SUMMARY}[/dim]")
    else:
        console.print("[dim cyan]Auto mode off: Eustace asks before writes and commands again.[/dim cyan]")


def _cmd_undo(state: dict, arg: str) -> None:
    note = undo_last_turn()
    if note:
        state["pending_note"] = (state.get("pending_note", "") + "\n" + note).strip()


def _cmd_permissions(state: dict, arg: str) -> None:
    word, _, rest = arg.strip().partition(" ")
    if word.lower() == "remove":
        if not rest.strip().isdigit():
            console.print("[dim yellow]Usage: /permissions remove <number>[/dim yellow]")
            return
        removed = remove_allow_rule(int(rest.strip()))
        if removed is None:
            console.print("[bold red]No rule with that number.[/bold red] [dim]/permissions lists them.[/dim]")
        else:
            console.print(f"[dim cyan]✓ Removed {escape(_visible_controls(removed))}[/dim cyan]")
        return
    if word:
        console.print("[dim yellow]Usage: /permissions, or /permissions remove <number>[/dim yellow]")
        return
    switch = ("  [red](auto mode unavailable: running as Administrator)[/red]" if ELEVATED
              else "  [dim](Shift+Tab or /auto to switch)[/dim]")
    console.print(f"  [dim]Mode:[/dim] {'[bold yellow]auto[/bold yellow]' if PERMISSION_MODE == 'auto' else 'default'}{switch}")
    rules = _config_list("allow")
    if not rules:
        console.print("  [dim]No allow rules. Choose \"Yes, and always allow\" in an approval to add one.[/dim]")
    for i, rule in enumerate(rules, start=1):
        bad = "" if _RULE_RE.match(rule) else "  [bold red](not a valid rule; ignored)[/bold red]"
        console.print(f"  [bold]{i}.[/bold] [cyan]{escape(_visible_controls(rule))}[/cyan]{bad}")
    extra = _config_list("shell_deny")
    if extra:
        valid = {p for p, _ in ((rx.pattern, l) for rx, l in _user_shell_deny())}
        console.print("  [dim]Your extra shell deny patterns:[/dim]")
        for pat in extra:
            note = "" if pat in valid else "  [bold red](invalid regular expression; ignored)[/bold red]"
            console.print(f"    {escape(_visible_controls(pat))}{note}")
    console.print(f"  [dim]Rules live in {escape(CONFIG_PATH)} under \"allow\". Remove one with /permissions remove <number>.[/dim]")


# The one list of slash commands: the main loop dispatches from it and the input box's "/" menu shows it.
def _cmd_skills(state: dict, arg: str) -> None:
    if not SKILLS:
        console.print("[dim]No skills loaded. Put skill folders (each with a SKILL.md) in .agents/skills in your project, "
                      f"~/.agents/skills, or {escape(os.path.join(EUSTACE_HOME, 'skills'))}.[/dim]")
        return
    table = Table(box=None, show_header=False, padding=(0, 2, 0, 2))
    for s in SKILLS.values():
        table.add_row(f"[cyan]/{escape(s['name'])}[/cyan]", escape(s["description"]), f"[dim]{escape(s['dir'])}[/dim]")
    console.print(table)


def _skill_invocation(name: str, request: str) -> str | None:
    """The user message for /<skill> [request]: the skill's instructions, then the request."""
    loaded = load_skill(name)
    if not loaded.startswith("[Skill "):
        console.print(f"[bold red]{escape(loaded)}[/bold red]")
        return None
    console.print(f"  [dim]Loaded skill {escape(name)}[/dim]")
    ask = request or "(no further details; follow the skill)"
    return f"{loaded}\n\n[The user ran /{name}. Follow the skill above for this request.]\n\n{ask}"


COMMANDS: dict[str, tuple[str, typing.Callable[[dict, str], None]]] = {
    "/help": ("Show commands and keys", _cmd_help),
    "/status": ("Model, folder, session and token estimate", _cmd_status),
    "/save": ("Save this session: /save [name]", _cmd_save),
    "/resume": ("Restore a saved session: /resume [name or number]", _cmd_resume),
    "/sessions": ("List saved sessions", _cmd_sessions),
    "/compact": ("Summarize the conversation to free up context", _cmd_compact),
    "/clear": ("Start over with an empty conversation", _cmd_clear),
    "/model": ("Choose the model: /model [id]", _cmd_model),
    "/auto": ("Switch auto mode (also Shift+Tab): /auto [on|off]", _cmd_auto),
    "/undo": ("Restore the files changed in the last turn that wrote any", _cmd_undo),
    "/permissions": ("List allow rules: /permissions [remove N]", _cmd_permissions),
    "/skills": ("List the skills loaded for this folder", _cmd_skills),
    "/exit": ("Quit Eustace", _cmd_exit),
}
_COMMAND_ALIASES = {"/quit": "/exit", "exit": "/exit", "quit": "/exit"}


def main():
    _init_client()
    cwd = os.getcwd()
    sessions = list_saved_sessions()
    session_hint = ""
    if sessions:
        latest = sessions[0]
        session_hint = (
            f"\n[dim]Recent Session:[/dim] [bold]{escape(latest['name'])}[/bold] "
            f"({latest['turn_count']} turns, {latest['updated_at']}) — type [cyan]/resume[/cyan] to restore"
        )

    if DEV_MODE:
        title_line = (
            f"[bold yellow]Eustace v{VERSION} — DEV MODE: public Gemini API[/bold yellow] "
            f"([dim]{escape(MODEL)} | {len(TOOLS)} tools[/dim])\n"
            f"[bold yellow]Test data only. Never CUI / PII / PHI. Config: {escape(EUSTACE_HOME)}[/bold yellow]\n"
        )
    else:
        title_line = f"[bold cyan]GenAI.mil CLI — Eustace v{VERSION}[/bold cyan] ([dim]{escape(MODEL)} | {len(TOOLS)} tools[/dim])\n"
    if ELEVATED:
        title_line += ("[bold red]⚠  Running as Administrator (elevated)[/bold red]\n"
                       "[red]   Everything Eustace runs gets admin rights. Auto mode is off for this session.[/red]\n")
    console.print(
        Panel.fit(
            title_line
            + f"[dim]Workspace:[/dim] {escape(cwd)}\n"
            f"[dim]Audit Log:[/dim] {escape(AUDIT_LOG_PATH)}\n"
            "[dim]Commands:[/dim] [cyan]/help[/cyan], [cyan]/save [name][/cyan], [cyan]/resume [name|#][/cyan], [cyan]/sessions[/cyan], "
            "[cyan]/compact[/cyan], [cyan]/clear[/cyan], [cyan]/model[/cyan], [cyan]/auto[/cyan], [cyan]/undo[/cyan], "
            "[cyan]/permissions[/cyan], [cyan]/skills[/cyan], [cyan]/exit[/cyan]\n"
            "[dim]Tip:[/dim] Press [bold]ESC[/bold] or [bold]Ctrl+C[/bold] while generating to stop mid-reply · "
            "[bold]Shift+Tab[/bold] switches auto mode"
            + ("\n[bold yellow]Auto mode is on[/bold yellow] [dim](started with --auto)[/dim]" if PERMISSION_MODE == "auto" else "")
            + session_hint,
            border_style="red" if ELEVATED else "yellow" if DEV_MODE else "cyan",
        )
    )
    if ELEVATED and START_IN_AUTO_MODE:
        console.print("[yellow]--auto was ignored: auto mode isn't available while Eustace runs as Administrator.\n"
                      "Start Eustace from a normal (non-elevated) PowerShell window to use it.[/yellow]")

    state = {"messages": [{"role": "system", "content": build_system_prompt()}]}
    prune_checkpoints()

    while True:
        try:
            user_input = _read_user_input(state["messages"]).strip()
            if not user_input:
                continue

            word, _, arg = user_input.partition(" ")
            name = _COMMAND_ALIASES.get(word.lower(), word.lower())
            if name in COMMANDS:
                COMMANDS[name][1](state, arg.strip())
                continue
            if name.startswith("/") and name[1:] in SKILLS and "\n" not in word:
                user_input = _skill_invocation(name[1:], arg.strip())
                if user_input is None:
                    continue
            if re.fullmatch(r"/[a-z]+", name) and "\n" not in user_input:
                console.print(f"[dim yellow]Unknown command {escape(word)}. Type /help for the list.[/dim yellow]")
                continue

            note = state.pop("pending_note", "")
            content = f"[Note from Eustace: {note}]\n\n{user_input}" if note else user_input
            state["messages"].append({"role": "user", "content": content})
            run_agent_turn(state["messages"])
            # Checkpoint every completed turn to this run's autosave file
            save_session(state["messages"], quiet=True)

        except _Quit:
            console.print("[dim]Goodbye![/dim]")
            break
        except KeyboardInterrupt:
            console.print("\n[dim yellow]Interrupted. Type /exit to quit, or enter your next prompt.[/dim yellow]")
            continue
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break
        except Exception as e:
            console.print(f"\n[bold red]Error:[/bold red] {escape(str(e))}")
            if state["messages"] and state["messages"][-1].get("role") == "user":
                state["messages"].pop()


if __name__ == "__main__":
    main()
