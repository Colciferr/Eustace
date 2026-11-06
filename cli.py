import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
import difflib
import fnmatch
import hashlib
import json
import msvcrt
import os
import re
import shutil
import subprocess

import truststore
truststore.inject_into_ssl()

from dotenv import load_dotenv
from openai import OpenAI
from openpyxl import Workbook, load_workbook
from pypdf import PdfReader
from docx import Document
from pptx import Presentation
from rich import box
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# Look for .env in C:\Users\<Username>\.eustace\.env
EUSTACE_HOME = os.path.join(os.path.expanduser("~"), ".eustace")
global_env_path = os.path.join(EUSTACE_HOME, ".env")
if os.path.exists(global_env_path):
    load_dotenv(global_env_path)
else:
    # Fallback to local directory (for local development)
    load_dotenv()

VERSION = "1.0.0"
CONFIG_PATH = os.path.join(EUSTACE_HOME, "config.json")

AVAILABLE_MODELS = [
    ("gemini-3.5-flash-lite-withtools", "Fast & light daily driver (Default)"),
    ("gemini-3.8-flash-withtools",      "Balanced speed & stronger reasoning"),
    ("gemini-4-argon-withtools",        "Flagship model for complex analysis & code"),
]
DEFAULT_MODEL = "gemini-3.5-flash-lite-withtools"

def load_saved_model() -> str:
    """Loads the user's last selected model from ~/.eustace/config.json."""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                m = data.get("model", "").strip()
                if m:
                    return m
    except Exception:
        pass
    return DEFAULT_MODEL

def save_selected_model(model_name: str):
    """Persists the selected model to ~/.eustace/config.json."""
    try:
        os.makedirs(EUSTACE_HOME, exist_ok=True)
        data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data["model"] = model_name
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def short_model_label(model_name: str) -> str:
    """Returns a compact badge name like 'gemini-4-argon' for UI headers."""
    return model_name.replace("-withtools", "")

MODEL = load_saved_model()
console = Console()

# --- Interactive Model Switcher ---
def _save_selected_model(model_id: str):
    """Saves the user's selected model to ~/.eustace/config.json."""
    try:
        os.makedirs(EUSTACE_HOME, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({"model": model_id}, f, indent=2)
    except Exception:
        pass


def select_model_interactive(arg: str = "") -> str:
    """Interactive /model picker or direct shorthand switcher (e.g., '/model argon')."""
    global MODEL
    from rich.table import Table
    from rich import box

    # Build live model choices
    models = list(AVAILABLE_MODELS)
    try:
        # Check GenAI.mil for newly added models
        live_ids = sorted(m.id for m in client.models.list().data)
        for mid in live_ids:
            if "embedding" in mid.lower():
                continue
            if f"{mid}-withtools" in live_ids:
                continue
            if mid not in [m[0] for m in models]:
                label = "Tool-Enabled" if mid.endswith("-withtools") else "Chat Only"
                models.append((mid, label, "Discovered on GenAI.mil endpoint"))
    except Exception:
        pass

    # Shorthand route: '/model argon' or '/model 2'
    if arg.strip():
        q = arg.strip().lower()
        chosen = None
        if q.isdigit():
            idx = int(q) - 1
            if 0 <= idx < len(models):
                chosen = models[idx][0]
        else:
            for mid, _, _ in models:
                if q == mid.lower() or q in mid.lower():
                    chosen = mid
                    break
            if not chosen:
                chosen = arg.strip()
                if not chosen.endswith("-withtools") and any(m[0] == f"{chosen}-withtools" for m in models):
                    chosen = f"{chosen}-withtools"

        if chosen:
            MODEL = chosen
            save_user_config({"model": MODEL})
            console.print(f"  [bold green]✓ Switched active model to:[/bold green] [bold cyan]{escape(MODEL)}[/bold cyan]")
            return MODEL

    # Render pretty Selection Table
    tbl = Table(title="Available GenAI.mil Models", box=box.ROUNDED, border_style="cyan")
    tbl.add_column("#", style="bold yellow", width=3, justify="right")
    tbl.add_column("Model Identifier", style="bold white")
    tbl.add_column("Capability Tier", style="cyan")
    tbl.add_column("Performance Profile", style="dim")

    for i, (mid, profile, desc) in enumerate(models, start=1):
        active_badge = " [bold green]● active[/bold green]" if mid == MODEL else ""
        tbl.add_row(str(i), f"{escape(mid)}{active_badge}", profile, desc)

    console.print()
    console.print(tbl)
    console.print("  [dim]Enter model number, shorthand name (e.g. 'argon'), or press Enter to keep current:[/dim] ", end="")
    choice = input().strip()
    if not choice:
        console.print(f"  [dim]Keeping current model: {escape(MODEL)}[/dim]")
        return MODEL

    return select_model_interactive(choice)

def ensure_api_key() -> str:
    """Returns the API key, prompting once and saving it to ~/.eustace/.env if it is missing."""
    key = os.environ.get("STARK_API_KEY")
    if key:
        return key

    console.print("[bold yellow]No API key found.[/bold yellow] Paste your GenAI.mil key (input hidden):")
    key = console.input("> ", password=True).strip()
    if not key:
        raise SystemExit("An API key is required.")

    os.makedirs(os.path.dirname(global_env_path), exist_ok=True)
    with open(global_env_path, "w", encoding="utf-8") as f:
        f.write(f"STARK_API_KEY={key}\n")
    os.environ["STARK_API_KEY"] = key
    console.print(f"[dim]Saved to {global_env_path}[/dim]")
    return key


client = OpenAI(
    base_url="https://api.genai.mil/v1",
    api_key=ensure_api_key(),
)

# =====================================================================
# STEP 1: SECURITY GUARDRAILS, AUDIT LOG, & DIFF APPROVAL HANDLER
# =====================================================================

IGNORE_DIRS = {".venv", ".git", "__pycache__", "node_modules"}

# Expanded secrets, credentials, key stores, and raw mail archives the model is never allowed to read
SENSITIVE_PATTERNS = {
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12", "*.p7b", "*.der",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.psafe3", "*.ppk",
    "*.gpg", ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials",
    "*.kubeconfig", "*secret*", "*credentials*.json", "*token*.json",
    "login data*", "cookies*", "web data*", "*.pst", "*.ost", "ntds.dit",
}

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
    p for p in (
        os.environ.get("SystemRoot"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramW6432"),
    ) if p
]


def _is_under(target: str, root: str) -> bool:
    """True if target is root itself or lives anywhere beneath it."""
    t = os.path.normcase(os.path.normpath(target))
    r = os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath([t, r]) == r
    except ValueError:  # different drives
        return False


def _load_custom_deny_patterns() -> set[str]:
    """Loads optional user/unit-defined deny patterns from ~/.eustace/deny_paths.txt."""
    if not os.path.exists(DENY_LIST_PATH):
        return set()
    try:
        with open(DENY_LIST_PATH, "r", encoding="utf-8") as f:
            return {
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith("#")
            }
    except OSError:
        return set()


def _is_sensitive(target: str) -> bool:
    """True for secrets files (by name pattern), custom deny-list matches, or anything inside ~/.eustace."""
    name = os.path.basename(target)
    norm_target = os.path.normcase(os.path.normpath(target))
    all_patterns = SENSITIVE_PATTERNS | _load_custom_deny_patterns()
    for pat in all_patterns:
        if fnmatch.fnmatch(name.lower(), pat.lower()) or fnmatch.fnmatch(norm_target, pat.lower()):
            return True
    return _is_under(target, EUSTACE_HOME)


def _is_protected_path(target: str) -> bool:
    """True if target is inside the Windows or Program Files trees."""
    return any(_is_under(target, root) for root in PROTECTED_ROOTS)


def _check_phi_guardrail(target: str, content: str = "") -> str | None:
    """Returns a security error string if the filename or content appears to contain PHI."""
    name = os.path.basename(target).lower()
    if any(fnmatch.fnmatch(name, pat) for pat in PHI_FILENAME_PATTERNS):
        return (
            f"Security Policy Block: '{name}' matches a Protected Health Information (PHI) "
            "filename pattern. Processing PHI (such as medical records) is strictly prohibited."
        )
    if content and PHI_CONTENT_REGEX.search(content[:15000]):
        return (
            f"Security Policy Block: '{name}' contains Protected Health Information (PHI) "
            "markers. While CUI and PII are authorized, processing PHI is strictly prohibited."
        )
    return None


def log_audit_event(tool_name: str, args: dict, decision: str, result: str):
    """Appends a structured audit record to ~/.eustace/audit.jsonl."""
    try:
        os.makedirs(EUSTACE_HOME, exist_ok=True)
        safe_args = {}
        for k, v in args.items():
            if k in ("content", "old_text", "new_text", "text", "html_body", "body") and isinstance(v, str):
                digest = hashlib.sha256(v.encode("utf-8", errors="replace")).hexdigest()[:12]
                safe_args[k] = f"<{len(v)} chars, sha256:{digest}>"
            else:
                safe_args[k] = v

        res_str = str(result)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "cwd": os.getcwd(),
            "tool": tool_name,
            "args": safe_args,
            "decision": decision,
            "status": "error" if res_str.startswith(("Error:", "Security", "Operation aborted")) else "ok",
            "result_preview": res_str[:200],
        }
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def build_unified_diff(old_text: str, new_text: str, path: str) -> str:
    """Generates a unified diff string between old_text and new_text."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{os.path.basename(path)}",
        tofile=f"b/{os.path.basename(path)}",
        n=3,
    )
    return "".join(diff)


class CLIApprovalHandler:
    """Decoupled approval UX supporting unified diffs and 'yes to all for this turn'."""

    def __init__(self):
        self.approve_all_for_turn = False
        self.last_decision = "auto"

    def reset_turn(self):
        self.approve_all_for_turn = False
        self.last_decision = "auto"

    def request_approval(self, action_title: str, detail_text: str = "", diff_text: str = "") -> bool:
        if self.approve_all_for_turn:
            self.last_decision = "approved_all_turn"
            console.print(f"  [dim green]✓ Auto-approved: {escape(action_title)}[/dim green]")
            return True

        body_parts = []
        if detail_text:
            body_parts.append(detail_text)
        if diff_text:
            lines = diff_text.splitlines()
            shown_diff = "\n".join(lines[:100])
            if len(lines) > 100:
                shown_diff += f"\n... ({len(lines) - 100} more diff lines)"

        console.print()
        if diff_text:
            if detail_text:
                console.print(f"  {detail_text}")
            console.print(
                Panel(
                    Syntax(shown_diff, "diff", theme="monokai", word_wrap=True),
                    title=f"[bold yellow]⚠️  Approval Required: {escape(action_title)}[/bold yellow]",
                    subtitle="[bold yellow]\\[Y][/bold yellow] Approve   [bold yellow]\\[N][/bold yellow] Deny   [bold yellow]\\[A][/bold yellow] Approve All This Turn",
                    border_style="yellow",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
        else:
            console.print(
                Panel(
                    detail_text or escape(action_title),
                    title=f"[bold yellow]⚠️  Approval Required: {escape(action_title)}[/bold yellow]",
                    subtitle="[bold yellow]\\[Y][/bold yellow] Approve   [bold yellow]\\[N][/bold yellow] Deny   [bold yellow]\\[A][/bold yellow] Approve All This Turn",
                    border_style="yellow",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )

        console.print("  [bold yellow]Choice (y/n/a) \\[default: N]:[/bold yellow] ", end="")
        choice = input().strip().lower()
        if choice in ("a", "all"):
            self.approve_all_for_turn = True
            self.last_decision = "approved_all_turn"
            return True
        if choice in ("y", "yes"):
            self.last_decision = "approved_user"
            return True

        self.last_decision = "denied_user"
        return False



APPROVAL_HANDLER = CLIApprovalHandler()


# =====================================================================
# 1. LOCAL FILE SYSTEM TOOLS (Plaintext + Excel, PDF, Word, PowerPoint)
# =====================================================================

def list_directory(path: str = ".") -> str:
    """Lists files and directories at the given path."""
    try:
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return f"Error: Path does not exist: {path}"
        if not os.path.isdir(target):
            return f"Error: Path is not a directory: {path}"

        items = []
        for entry in sorted(os.listdir(target)):
            if entry in IGNORE_DIRS:
                continue
            full_path = os.path.join(target, entry)
            kind = "[DIR] " if os.path.isdir(full_path) else "[FILE]"
            if not os.path.isdir(full_path) and _is_sensitive(full_path):
                items.append(f"{kind} {entry} (protected: contents not readable)")
                continue
            size = "" if os.path.isdir(full_path) else f" ({os.path.getsize(full_path)} bytes)"
            items.append(f"{kind} {entry}{size}")

        return f"Contents of {target}:\n" + ("\n".join(items) if items else "(empty directory)")
    except Exception as e:
        return f"Error listing directory '{path}': {e}"


def _read_excel(target: str) -> str:
    """Extracts all sheets and rows from an .xlsx file as Markdown-style tables."""
    wb = load_workbook(target, data_only=True, read_only=True)
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
    reader = PdfReader(target)
    out = [f"PDF Pages: {len(reader.pages)}\n"]
    for i, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or "(No extractable text on this page)"
        out.append(f"--- Page {i} ---\n{text}")
    return "\n".join(out)


def _read_docx(target: str) -> str:
    """Extracts paragraphs and tables from a .docx Word document."""
    doc = Document(target)
    out = []
    for p in doc.paragraphs:
        if p.text.strip():
            out.append(p.text)
    for t_idx, table in enumerate(doc.tables, start=1):
        out.append(f"\n[Table {t_idx}]")
        for row in table.rows:
            out.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n".join(out)


def _read_pptx(target: str) -> str:
    """Extracts slide text and speaker notes from a .pptx PowerPoint file."""
    prs = Presentation(target)
    out = [f"Presentation Slides: {len(prs.slides)}\n"]
    for i, slide in enumerate(prs.slides, start=1):
        out.append(f"--- Slide {i} ---")
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                out.append(shape.text_frame.text.strip())
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            out.append(f"[Speaker Notes]: {slide.notes_slide.notes_text_frame.text.strip()}")
    return "\n".join(out)


def read_file(path: str, max_chars: int = 30000) -> str:
    """Reads plaintext, code, Excel (.xlsx), PDF (.pdf), Word (.docx), and PowerPoint (.pptx) files."""
    try:
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return f"Error: File not found: {path}"
        if not os.path.isfile(target):
            return f"Error: Path is not a file: {path}"
        if _is_sensitive(target):
            return f"Security Error: '{path}' matches a protected secrets or deny-list pattern and will not be read."
        phi_err = _check_phi_guardrail(target)
        if phi_err:
            return phi_err

        ext = os.path.splitext(target)[1].lower()
        if ext in {".xlsx", ".xlsm"}:
            content = _read_excel(target)
        elif ext == ".pdf":
            content = _read_pdf(target)
        elif ext == ".docx":
            content = _read_docx(target)
        elif ext == ".pptx":
            content = _read_pptx(target)
        else:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars + 1)

        phi_err = _check_phi_guardrail(target, content)
        if phi_err:
            return phi_err

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [Truncated at {max_chars} chars]"
        return content
    except Exception as e:
        return f"Error reading file '{path}': {e}"


def write_file(path: str, content: str) -> str:
    """Creates a new file or overwrites an existing file with unified diff preview & confirmation."""
    try:
        target = os.path.abspath(path)
        if _is_protected_path(target):
            return "Security Error: Writing to system folders is strictly prohibited."
        if _is_sensitive(target):
            return f"Security Error: Writing to protected path '{path}' is prohibited."

        old_content = ""
        is_overwrite = os.path.exists(target)
        if is_overwrite and os.path.isfile(target):
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    old_content = f.read()
            except OSError:
                old_content = ""

        diff_str = build_unified_diff(old_content, content, target)
        action = f"Overwrite file: {target}" if is_overwrite else f"Create new file: {target}"
        detail = f"[dim]Size: {len(content)} chars ({len(content.splitlines())} lines)[/dim]"

        if not APPROVAL_HANDLER.request_approval(action, detail_text=detail, diff_text=diff_str):
            return "Operation aborted: User did NOT approve the file write."

        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

        console.print(f"[bold green]✓ File written: {escape(target)}[/bold green]\n")
        return f"Success: File written successfully to {target}"
    except Exception as e:
        return f"Error writing file '{path}': {e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replaces a specific block of text/code inside an existing file with unified diff preview."""
    try:
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return f"Error: File not found: {path}"
        if _is_protected_path(target) or _is_sensitive(target):
            return "Security Error: Editing protected or system files is strictly prohibited."

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            file_content = f.read()

        if old_text not in file_content:
            return "Error: Target text block ('old_text') was not found in the file. No changes made."

        updated_content = file_content.replace(old_text, new_text, 1)
        diff_str = build_unified_diff(file_content, updated_content, target)

        if not APPROVAL_HANDLER.request_approval(f"Edit file: {target}", diff_text=diff_str):
            return "Operation aborted: User did NOT approve the file edit."

        with open(target, "w", encoding="utf-8") as f:
            f.write(updated_content)

        console.print(f"[bold green]✓ File edited: {escape(target)}[/bold green]\n")
        return f"Success: File edited successfully at {target}"
    except Exception as e:
        return f"Error editing file '{path}': {e}"


SHELL_TIMEOUT_SECONDS = 120
MAX_SHELL_OUTPUT_CHARS = 20000


def run_powershell(command: str) -> str:
    """Runs a PowerShell command in the current working directory (requires manual user confirmation)."""
    try:
        if not APPROVAL_HANDLER.request_approval(
            "Run PowerShell command",
            detail_text=f"[bold cyan]{escape(command)}[/bold cyan]",
        ):
            return "Operation aborted: User did NOT approve running the command."

        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            return "Error: Neither 'pwsh' nor 'powershell' was found on PATH."

        try:
            proc = subprocess.run(
                [shell, "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=os.getcwd(),
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

        console.print(f"[dim]{escape(output)}[/dim]")
        return output
    except Exception as e:
        return f"Error running command: {e}"


# =====================================================================
# STEP 2: NATIVE WIN32 CLIPBOARD & OPEN_FILE LAUNCHER
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


def read_clipboard(max_chars: int = 30000) -> str:
    """Reads the current text content from the Windows clipboard."""
    if not _user32.OpenClipboard(None):
        return "Error: Could not open Windows clipboard (it may be locked by another application)."
    try:
        handle = _user32.GetClipboardData(_CF_UNICODETEXT)
        if not handle:
            return "(Clipboard is empty or does not contain text)"
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return "Error: Could not lock clipboard memory."
        try:
            text = ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()

    phi_err = _check_phi_guardrail("clipboard", text)
    if phi_err:
        return phi_err

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [Clipboard truncated at {max_chars} chars]"
    return text if text.strip() else "(Clipboard contains only whitespace)"


def write_clipboard(text: str) -> str:
    """Writes text directly to the Windows clipboard."""
    try:
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


BLOCKED_LAUNCH_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi", ".scr", ".com", ".dll"
}


def open_file(path: str) -> str:
    """Opens a document or folder in its default Windows application (Word, Excel, PDF reader, Explorer)."""
    try:
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return f"Error: Path does not exist: {path}"
        ext = os.path.splitext(target)[1].lower()
        if ext in BLOCKED_LAUNCH_EXTENSIONS:
            return f"Security Error: Launching executable/script extension '{ext}' via open_file is blocked."
        if _is_sensitive(target):
            return f"Security Error: Cannot open protected file '{path}'."

        if not APPROVAL_HANDLER.request_approval(f"Open in default Windows app: {target}"):
            return "Operation aborted: User declined to open the file."

        os.startfile(target)
        console.print(f"[bold green]✓ Launched: {escape(target)}[/bold green]")
        return f"Success: Opened '{target}' in its default application."
    except Exception as e:
        return f"Error opening '{path}': {e}"

# =====================================================================
# STEP 3: OFFICE DOCUMENT GENERATION & EDITING (.docx & .xlsx)
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


def write_docx(path: str, content: str, title: str = "") -> str:
    """Creates or overwrites a Word (.docx) document from Markdown-formatted text (headings, bullets, bold, tables)."""
    try:
        target = os.path.abspath(path)
        if not target.lower().endswith(".docx"):
            target += ".docx"
        if _is_protected_path(target) or _is_sensitive(target):
            return "Security Error: Writing to protected or system paths is prohibited."

        action = f"Overwrite Word document: {target}" if os.path.exists(target) else f"Create Word document: {target}"
        preview_diff = build_unified_diff("", content, target)
        if not APPROVAL_HANDLER.request_approval(action, diff_text=preview_diff):
            return "Operation aborted: User did NOT approve creating the Word document."

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
        console.print(f"[bold green]✓ Word document saved: {escape(target)}[/bold green]\n")
        return f"Success: Word document written to {target}"
    except Exception as e:
        return f"Error writing Word document '{path}': {e}"


def fill_docx_template(template_path: str, output_path: str, replacements: dict) -> str:
    """Fills placeholders (e.g. '{{NAME}}': 'TSgt Davis') in an existing .docx template and saves to output_path."""
    try:
        src = os.path.abspath(template_path)
        dst = os.path.abspath(output_path)
        if not dst.lower().endswith(".docx"):
            dst += ".docx"
        if not os.path.exists(src):
            return f"Error: Template file not found: {template_path}"
        if _is_sensitive(src) or _is_sensitive(dst) or _is_protected_path(dst):
            return "Security Error: Template or output path is restricted."

        summary_lines = [f"Template: {src}", f"Output:   {dst}", "Replacements:"]
        for k, v in replacements.items():
            summary_lines.append(f"  {k}  -->  {v}")

        if not APPROVAL_HANDLER.request_approval(
            f"Fill Word template -> {dst}",
            detail_text="[dim]" + escape("\n".join(summary_lines)) + "[/dim]",
        ):
            return "Operation aborted: User did NOT approve filling the Word template."

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
        console.print(f"[bold green]✓ Template filled ({count} replacements): {escape(dst)}[/bold green]\n")
        return f"Success: Saved filled document to {dst} ({count} replacements made)."
    except Exception as e:
        return f"Error filling Word template: {e}"

def update_excel(
    path: str,
    sheet_name: str = "",
    append_rows: list[list] | None = None,
    cell_updates: dict | None = None,
) -> str:
    """Creates or updates an Excel (.xlsx) workbook by appending rows and/or updating specific cells (e.g. {'B2': 'Done'})."""
    try:
        target = os.path.abspath(path)
        if not target.lower().endswith((".xlsx", ".xlsm")):
            target += ".xlsx"
        if _is_protected_path(target) or _is_sensitive(target):
            return "Security Error: Writing to protected or system paths is prohibited."

        exists = os.path.exists(target)
        preview = [f"Workbook: {target} ({'existing' if exists else 'new file'})"]
        if sheet_name:
            preview.append(f"Sheet:    {sheet_name}")
        if append_rows:
            preview.append(f"\nRows to append ({len(append_rows)}):")
            for r in append_rows[:15]:
                preview.append("  + | " + " | ".join(str(c) for c in r) + " |")
            if len(append_rows) > 15:
                preview.append(f"  ... (+{len(append_rows) - 15} more rows)")
        if cell_updates:
            preview.append("\nCell updates:")
            for cell_ref, val in list(cell_updates.items())[:25]:
                preview.append(f"  {cell_ref} := {val}")

        if not APPROVAL_HANDLER.request_approval(
            f"Update Excel workbook: {target}",
            detail_text="[cyan]" + escape("\n".join(preview)) + "[/cyan]",
        ):
            return "Operation aborted: User did NOT approve the Excel update."

        if exists:
            wb = load_workbook(target)
            if sheet_name:
                ws = wb[sheet_name] if sheet_name in wb.sheetnames else wb.create_sheet(title=sheet_name)
            else:
                ws = wb.active
        else:
            wb = Workbook()
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
# STEP 4: SMART FILE & OFFICE CONTENT SEARCH (search_files)
# =====================================================================

SEARCHABLE_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".py", ".ps1", ".bat", ".cmd",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log", ".xml", ".html", ".js", ".ts",
}
SEARCHABLE_OFFICE_EXTS = {".docx", ".xlsx", ".xlsm", ".pdf", ".pptx"}
MAX_OFFICE_PARSE_FILES = 40
MAX_SEARCH_FILE_BYTES = 15 * 1024 * 1024  # 15 MB


def search_files(
    path: str = ".",
    query: str = "",
    file_pattern: str = "*",
    max_results: int = 30,
) -> str:
    """Searches for files by filename pattern and/or text query (including inside .docx, .xlsx, .pdf, .pptx)."""
    try:
        root_dir = os.path.abspath(path)
        if not os.path.exists(root_dir):
            return f"Error: Search path does not exist: {path}"
        if not os.path.isdir(root_dir):
            return f"Error: Search path is not a directory: {path}"

        q_lower = query.strip().lower()
        pat_lower = (file_pattern.strip() or "*").lower()
        results = []
        office_parsed_count = 0
        files_scanned = 0

        for dirpath, dirnames, filenames in os.walk(root_dir):
            # Prune ignored and protected directories in-place
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in IGNORE_DIRS and not _is_sensitive(os.path.join(dirpath, d))
            ]

            for fname in sorted(filenames):
                check_user_abort()
                full_path = os.path.join(dirpath, fname)
                rel_path = os.path.relpath(full_path, root_dir)

                if _is_sensitive(full_path) or _check_phi_guardrail(full_path):
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
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            text_to_scan = f.read(200_000)
                    except OSError:
                        pass
                elif ext in SEARCHABLE_OFFICE_EXTS and office_parsed_count < MAX_OFFICE_PARSE_FILES:
                    office_parsed_count += 1
                    files_scanned += 1
                    try:
                        if ext in {".xlsx", ".xlsm"}:
                            text_to_scan = _read_excel(full_path)
                        elif ext == ".pdf":
                            text_to_scan = _read_pdf(full_path)
                        elif ext == ".docx":
                            text_to_scan = _read_docx(full_path)
                        elif ext == ".pptx":
                            text_to_scan = _read_pptx(full_path)
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

        if not results:
            return (
                f"No matches found in '{root_dir}' for query='{query}', file_pattern='{file_pattern}' "
                f"(scanned {files_scanned} files)."
            )

        header = (
            f"Found {len(results)} matching file(s) in '{root_dir}' "
            f"(scanned {files_scanned} files, {office_parsed_count} Office/PDF docs):\n\n"
        )
        return header + "\n\n".join(results)
    except Exception as e:
        return f"Error searching files in '{path}': {e}"

# =====================================================================
# STEP 5: CLASSIC OUTLOOK COM TOOLS (STIG-Safe Triage, Calendar, Drafts)
# =====================================================================

def _get_outlook_ns():
    """Returns (Outlook.Application, MAPI Namespace) or raises RuntimeError."""
    try:
        import win32com.client
        app = win32com.client.Dispatch("Outlook.Application")
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


def list_inbox(
    max_items: int = 25,
    unread_only: bool = False,
    subject_filter: str = "",
) -> str:
    """Lists recent emails in the user's Outlook Inbox using STIG-permitted metadata (Subject, ReceivedTime, Unread, EntryID)."""
    try:
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


def get_calendar(days_ahead: int = 7, days_back: int = 0) -> str:
    """Lists Outlook Calendar appointments/meetings within [today - days_back, today + days_ahead]."""
    try:
        _, ns = _get_outlook_ns()
        cal_folder = ns.GetDefaultFolder(9)  # olFolderCalendar = 9
        items = cal_folder.Items
        items.IncludeRecurrences = True
        items.Sort("[Start]")

        now = datetime.now()
        start_dt = (now - timedelta(days=max(0, days_back))).replace(hour=0, minute=0, second=0)
        end_dt = (now + timedelta(days=max(1, min(days_ahead, 60)))).replace(hour=23, minute=59, second=59)

        # Outlook Restrict filter uses US date format MM/DD/YYYY HH:MM AM/PM
        start_str = start_dt.strftime("%m/%d/%Y %I:%M %p")
        end_str = end_dt.strftime("%m/%d/%Y %I:%M %p")
        restriction = f"[Start] >= '{start_str}' AND [Start] <= '{end_str}'"

        try:
            filtered = items.Restrict(restriction)
        except Exception:
            filtered = items

        events = []
        BUSY_MAP = {0: "Free", 1: "Tentative", 2: "Busy", 3: "Out of Office", 4: "Working Elsewhere"}

        ev = filtered.GetFirst()
        while ev is not None and len(events) < 100:
            subj = str(_safe_prop(ev, "Subject", "(Untitled Event)")).strip()
            start_val = str(_safe_prop(ev, "Start", ""))[:16]
            end_val = str(_safe_prop(ev, "End", ""))[:16]
            loc = str(_safe_prop(ev, "Location", "")).strip()
            all_day = bool(_safe_prop(ev, "AllDayEvent", False))
            busy_code = _safe_prop(ev, "BusyStatus", 2)
            busy_str = BUSY_MAP.get(busy_code, "Busy")

            loc_str = f" @ {loc}" if loc else ""
            all_day_str = " [ALL DAY]" if all_day else ""
            events.append(f"- {start_val} to {end_val}{all_day_str} ({busy_str}): **{subj}**{loc_str}")
            ev = filtered.GetNext()

        if not events:
            return f"No calendar events found between {start_dt.strftime('%Y-%m-%d')} and {end_dt.strftime('%Y-%m-%d')}."

        header = f"Outlook Calendar ({start_dt.strftime('%Y-%m-%d')} to {end_dt.strftime('%Y-%m-%d')}, {len(events)} events):\n"
        return header + "\n".join(events)
    except Exception as e:
        return f"Error reading Outlook Calendar: {e}"


def open_email(entry_id: str, open_reply_window: bool = False) -> str:
    """Pops open a specific Outlook email (or a Reply window for it) on the user's screen using its EntryID."""
    try:
        _, ns = _get_outlook_ns()
        item = ns.GetItemFromID(entry_id.strip())
        subj = str(_safe_prop(item, "Subject", "(Email)"))

        action = f"Open Outlook Reply window for: {subj}" if open_reply_window else f"Display Outlook email on screen: {subj}"
        if not APPROVAL_HANDLER.request_approval(action):
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


def create_draft_email(subject: str, body: str, to_hint: str = "") -> str:
    """Creates a new draft email in Classic Outlook and displays it on screen for the user to review and send (never auto-sends)."""
    try:
        app, _ = _get_outlook_ns()
        preview_lines = [f"Subject: {subject}"]
        if to_hint:
            preview_lines.append(f"Intended Recipient(s) (add in Outlook To: line): {to_hint}")
        preview_lines.append("--- Draft Body ---")
        preview_lines.append(body)

        if not APPROVAL_HANDLER.request_approval(
            f"Create Outlook Draft: {subject}",
            detail_text="[cyan]" + escape("\n".join(preview_lines[:40])) + "[/cyan]",
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


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and folders in a directory on the user's local PC. Defaults to current working directory '.'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute directory path to list (default is '.').",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file on the user's local PC. Supports plaintext, code, CSV, Excel (.xlsx/.xlsm), PDF (.pdf), Word (.docx), and PowerPoint (.pptx).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to read.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a new file or completely overwrite an existing file with content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to create/overwrite.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The exact plaintext or code content to write into the file.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Search and replace a specific block of text/code within an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the file to edit.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "The exact string block currently in the file that you want to replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "The replacement string block to insert.",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_powershell",
            "description": (
                "Run a PowerShell command on the user's local PC in the current working directory "
                "(e.g. git, pytest, builds, dir). Returns exit code, stdout and stderr. "
                "The user must manually approve every command, so keep each one purposeful."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command line to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_clipboard",
            "description": (
                "Read the text currently copied to the user's Windows clipboard. "
                "Ideal when the user copies an email, paragraph, table, or snippet and asks you to summarize, fix, or act on it."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_clipboard",
            "description": (
                "Copy text directly onto the user's Windows clipboard so they can immediately Ctrl+V paste it into Outlook, Word, or Teams."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to place onto the Windows clipboard.",
                    }
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": (
                "Launch/open a document or folder in its default Windows application (e.g., Word, Excel, PDF reader, File Explorer) "
                "after creating or updating it so the user can review it immediately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative or absolute path to the document or folder to open.",
                    }
                },
                "required": ["path"],
            },
        },
    },
        {
        "type": "function",
        "function": {
            "name": "write_docx",
            "description": (
                "Create or overwrite a Microsoft Word (.docx) document from Markdown-formatted text. "
                "Supports #/##/### headings, bullet lists (- ), numbered lists (1. ), **bold** text, and Markdown tables (| col | col |)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Output path for the .docx file.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional document title heading at the top of the page.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown-formatted body content to render into the Word document.",
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill_docx_template",
            "description": (
                "Fill placeholders (such as '{{DATE}}', '{{SUBJECT}}', '[NAME]') inside an existing Word (.docx) "
                "template (preserving headers, footers, tables, and formatting) and save to a new .docx file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_path": {
                        "type": "string",
                        "description": "Path to the existing .docx template file.",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Path where the filled .docx file should be saved.",
                    },
                    "replacements": {
                        "type": "object",
                        "description": "Key-value dictionary mapping exact placeholder strings to their replacement text.",
                    },
                },
                "required": ["template_path", "output_path", "replacements"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_excel",
            "description": (
                "Create a new Excel (.xlsx) workbook or update an existing one by appending rows to a sheet "
                "and/or setting specific cell coordinates (e.g. {'B4': 'Complete', 'C4': 100})."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the .xlsx file (created automatically if it does not exist).",
                    },
                    "sheet_name": {
                        "type": "string",
                        "description": "Optional target worksheet name (defaults to active sheet, or creates sheet if missing).",
                    },
                    "append_rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                        "description": "Optional list of rows (each row is a list of cell values) to append to the bottom of the sheet.",
                    },
                    "cell_updates": {
                        "type": "object",
                        "description": "Optional dictionary mapping cell coordinates (like 'A1', 'C5') to new values.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search recursively across a directory tree by filename pattern and/or inside file contents "
                "(searches plaintext, code, CSV, Word .docx, Excel .xlsx, PDF .pdf, and PowerPoint .pptx in one call)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Root directory to search from (default is current directory '.').",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive text or keyword to search for inside files and filenames.",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional filename filter or glob pattern (e.g. '*.docx', '*.xlsx', '*memo*', default '*').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matching files to return (default 30).",
                    },
                },
                "required": [],
            },
        },
    },
        {
        "type": "function",
        "function": {
            "name": "list_inbox",
            "description": (
                "List recent emails in the user's Classic Outlook Inbox (returns Subject, ReceivedTime, Unread status, Categories, and EntryID). "
                "Supports filtering by unread status or subject keywords."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_items": {
                        "type": "integer",
                        "description": "Maximum number of emails to return (default 25, max 100).",
                    },
                    "unread_only": {
                        "type": "boolean",
                        "description": "If true, only return unread emails (default false).",
                    },
                    "subject_filter": {
                        "type": "string",
                        "description": "Optional case-insensitive substring to filter email subjects.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar",
            "description": (
                "Read the user's Classic Outlook Calendar appointments and meetings for today and upcoming/past days "
                "(returns Subject, Start, End, Location, AllDayEvent, and BusyStatus)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {
                        "type": "integer",
                        "description": "Number of days ahead from today to include (default 7).",
                    },
                    "days_back": {
                        "type": "integer",
                        "description": "Number of days in the past to include (default 0).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_email",
            "description": (
                "Pop open a specific Outlook email (or a Reply window for it) on the user's screen using its EntryID from `list_inbox`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": "The exact EntryID string of the email from `list_inbox`.",
                    },
                    "open_reply_window": {
                        "type": "boolean",
                        "description": "If true, opens a Reply draft window for the email instead of just viewing it (default false).",
                    },
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_draft_email",
            "description": (
                "Create a new draft email in Classic Outlook and pop it open on the user's screen ready for review and manual sending. "
                "Never auto-sends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Subject line for the draft email.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Plaintext body of the draft email.",
                    },
                    "to_hint": {
                        "type": "string",
                        "description": "Optional recipient email address or name.",
                    },
                },
                "required": ["subject", "body"],
            },
        },
    },
]

AVAILABLE_FUNCTIONS = {
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_powershell": run_powershell,
    "read_clipboard": read_clipboard,
    "write_clipboard": write_clipboard,
    "open_file": open_file,
    "write_docx": write_docx,
    "fill_docx_template": fill_docx_template,
    "update_excel": update_excel,
    "search_files": search_files,
    "list_inbox": list_inbox,
    "get_calendar": get_calendar,
    "open_email": open_email,
    "create_draft_email": create_draft_email,

}


# =====================================================================
# 2. SYSTEM PROMPT & AGENT STREAMING LOOP
# =====================================================================

DEFAULT_SYSTEM_PROMPT = """# Identity & Role
Your name is **Eustace** (**E**lectronic **U**tility for **S**cripting, **T**asking, and **A**utomated **C**ommand **E**xecution). You are a sharp, personable, and collaborative CLI coding and operational sidekick running locally on the user's Windows workstation.

# Personality & Tone
- **Warm & Approachable:** Be conversational, natural, and friendly, like a trusted teammate at the next desk. A little dry wit is welcome.
- **Action-Oriented on Tasks:** When writing code, debugging, or answering technical questions, be clear, structured, and get straight to the solution (BLUF).

# Core Directives
1. **Use Local Tools Proactively:** You have direct access to the user's PC via `list_directory`, `read_file`, `read_clipboard`, and `write_clipboard`. Inspect files or clipboard contents yourself rather than asking the user to paste them.
2. **Environment Awareness:** The user is on Windows with PowerShell. Tailor shell commands for PowerShell.
3. **Write and Edit Files Safely:** You have `write_file`, `edit_file`, and `open_file`. Explain what you are about to change before calling them. The user sees a unified diff and must approve every write/edit.
4. **Run Commands Deliberately:** You can execute PowerShell via `run_powershell`. The user must approve every command, so prefer single, purposeful commands and state your intent first. Secrets files (`.env`, keys, certificates, credentials) are blocked from `read_file`; do not try to work around that.
"""

GLOBAL_PROMPT_PATH = os.path.join(EUSTACE_HOME, "system_prompt.md")


def _ensure_global_prompt() -> str:
    """Makes sure ~/.eustace/system_prompt.md exists."""
    if not os.path.exists(GLOBAL_PROMPT_PATH):
        os.makedirs(EUSTACE_HOME, exist_ok=True)
        local_prompt_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
        if os.path.exists(local_prompt_file):
            shutil.copyfile(local_prompt_file, GLOBAL_PROMPT_PATH)
        else:
            with open(GLOBAL_PROMPT_PATH, "w", encoding="utf-8") as f:
                f.write(DEFAULT_SYSTEM_PROMPT)
        console.print(f"[dim]Created persona file at {escape(GLOBAL_PROMPT_PATH)} (edit it to customize Eustace)[/dim]")
    return GLOBAL_PROMPT_PATH


def build_system_prompt() -> str:
    """Loads global system_prompt.md from ~/.eustace/ and optional local AGENT.md."""
    cwd = os.getcwd()
    try:
        with open(_ensure_global_prompt(), "r", encoding="utf-8") as f:
            base_prompt = f.read().strip()
    except OSError as e:
        console.print(f"[dim yellow]Could not load persona file ({escape(str(e))}); using built-in default.[/dim yellow]")
        base_prompt = DEFAULT_SYSTEM_PROMPT.strip()

    tool_names = ", ".join(f"`{name}`" for name in AVAILABLE_FUNCTIONS.keys())
    env_context = (
        f"\n\n# Runtime Environment & Security Boundary (IL5 / DoW)\n"
        f"- Current Working Directory: `{cwd}`\n"
        f"- Available Tools: {tool_names}\n"
        f"- Data Handling Policy (IL5): GenAI.mil is authorized to process Controlled Unclassified Information (CUI) "
        f"and Personally Identifiable Information (PII) consistent with DoW IL5 security protocols. If generating "
        f"outputs containing PII, portion-mark appropriately as `(CUI)`. You are strictly PROHIBITED from processing, "
        f"extracting, or generating Protected Health Information (PHI / medical records) or local credential/secret files.\n"
    )

    # Load hierarchical AGENT.md files (global ~/.eustace/AGENT.md + parent folders down to cwd)
    agent_md_paths = []
    global_agent_md = os.path.join(EUSTACE_HOME, "AGENT.md")
    if os.path.isfile(global_agent_md):
        agent_md_paths.append(global_agent_md)

    curr = os.path.abspath(cwd)
    folder_chain = []
    while True:
        candidate = os.path.join(curr, "AGENT.md")
        if os.path.isfile(candidate) and candidate not in agent_md_paths:
            folder_chain.append(candidate)
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent

    # Add from broadest parent folder down to current working directory
    for p in reversed(folder_chain):
        agent_md_paths.append(p)

    for p in agent_md_paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read().strip()
            if text:
                env_context += f"\n# Office / Project Instructions ({p})\n{text}\n"
        except OSError:
            pass

    return base_prompt + env_context


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


MAX_TOOL_RESULT_CHARS = 30000
ARG_PREVIEW_CHARS = 120


TOOL_FRIENDLY_LABELS = {
    "list_directory": "Listing folder",
    "read_file": "Reading file",
    "write_file": "Writing file",
    "edit_file": "Editing file",
    "search_files": "Searching files",
    "open_file": "Opening file",
    "run_powershell": "Running PowerShell",
    "read_clipboard": "Reading clipboard",
    "write_clipboard": "Copying to clipboard",
    "write_docx": "Creating Word document",
    "fill_docx_template": "Filling Word template",
    "update_excel": "Updating Excel workbook",
    "list_inbox": "Checking Outlook Inbox",
    "get_calendar": "Checking Outlook Calendar",
    "open_email": "Opening Outlook email",
    "create_draft_email": "Creating Outlook draft",
}


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

    return f"[bold cyan]{escape(label)}[/bold cyan]" + (f" [dim]({escape(detail)})[/dim]" if detail else "")


def _tail_lines(text: str, max_lines: int) -> str:
    """Returns the last max_lines lines of text (used for the live streaming preview)."""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def run_agent_turn(messages: list[dict]):
    """Streams responses & tool calls with instant ESC or Ctrl+C cancellation."""
    turn_start_len = len(messages)
    partial_reply = ""
    APPROVAL_HANDLER.reset_turn()

    try:
        while True:
            check_user_abort()
            console.print("  [dim cyan]Thinking... (press ESC or Ctrl+C to stop)[/dim cyan]", end="\r")

            stream = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                stream=True,
            )

            print(" " * 60, end="\r", flush=True)

            full_reply = ""
            tool_calls_buffer = {}
            live = None

            try:
                for chunk in stream:
                    check_user_abort()

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue

                    if delta.content:
                        full_reply += delta.content
                        partial_reply = full_reply
                        preview = Text(_tail_lines(full_reply, max(console.size.height - 4, 5)))
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

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {
                                    "id": tc.id or "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.id:
                                tool_calls_buffer[idx]["id"] = tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_buffer[idx]["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls_buffer[idx]["function"]["arguments"] += tc.function.arguments
            finally:
                if live is not None:
                    live.stop()
                stream.close()

            if full_reply:
                console.print()
                console.print(
                    Panel(
                        Markdown(full_reply),
                        title=f"[bold cyan]Eustace[/bold cyan] [dim]({short_model_label(MODEL)})[/dim]",
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
                    tool_calls_list.append({
                        "id": item["id"],
                        "type": "function",
                        "function": {
                            "name": item["function"]["name"],
                            "arguments": item["function"]["arguments"],
                        },
                    })

                assistant_msg = {
                    "role": "assistant", 
                    "content": full_reply if full_reply else None,
                    "tool_calls": tool_calls_list
                }
                messages.append(assistant_msg)

                for tc in tool_calls_list:
                    check_user_abort()
                    fn_name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"] or "{}"

                    try:
                        fn_args = json.loads(raw_args)
                    except json.JSONDecodeError as je:
                        console.print(f"[bold red]Error parsing tool arguments JSON:[/bold red] {escape(str(je))}")
                        fn_args = {}

                    console.print(f"  [yellow]●[/yellow] {_summarize_args(fn_name, fn_args)}")

                    APPROVAL_HANDLER.last_decision = "auto"
                    func = AVAILABLE_FUNCTIONS.get(fn_name)
                    if func:
                        try:
                            result = func(**fn_args)
                        except Exception as fe:
                            console.print(f"[bold red]Tool execution error inside '{escape(fn_name)}':[/bold red] {escape(str(fe))}")
                            result = f"Error: Tool execution failed: {fe}"
                    else:
                        result = f"Error: Unknown tool '{fn_name}'"

                    log_audit_event(fn_name, fn_args, APPROVAL_HANDLER.last_decision, result)

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
            console.print(Markdown(partial_reply))
            console.print("[dim](partial reply)[/dim]")
        del messages[turn_start_len:]
        messages.append({"role": "assistant", "content": "(Response interrupted by the user before completion.)"})
    except Exception as e:
        console.print(f"\n[bold red]System Loop Error:[/bold red] {escape(str(e))}")
        if partial_reply:
            console.print(Markdown(partial_reply))
            console.print("[dim](partial reply)[/dim]")
        del messages[turn_start_len:]
        messages.append({"role": "assistant", "content": f"(Response failed with an error: {e})"})


COMPACT_INSTRUCTION = (
    "Summarize this entire conversation so far for your own future reference. Capture the user's goals, "
    "decisions made, files inspected or changed (with their paths), key facts learned, and any unfinished "
    "work. Be thorough but concise. Reply with the summary only."
)


def compact_messages(messages: list[dict]) -> list[dict]:
    """Replaces the conversation history with a model-written summary to free up context space."""
    if len(messages) <= 1:
        return messages

    console.print("  [dim cyan]Compacting conversation...[/dim cyan]", end="\r")
    response = client.chat.completions.create(
        model=MODEL,
        messages=messages + [{"role": "user", "content": COMPACT_INSTRUCTION}],
    )
    print(" " * 60, end="\r", flush=True)

    summary = (response.choices[0].message.content or "").strip()
    if not summary:
        raise RuntimeError("Model returned an empty summary; history left unchanged.")

    return [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": f"[Context compacted] Summary of our conversation so far:\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the summary and will continue from here."},
    ]

# =====================================================================
# STEP 6: PERSISTENT SESSIONS (/save, /resume, /sessions, + Autosave)
# =====================================================================

SESSIONS_DIR = os.path.join(EUSTACE_HOME, "sessions")


def _sanitize_session_name(name: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip()).strip("_")
    return clean or datetime.now().strftime("session_%Y%m%d_%H%M%S")


def save_session(messages: list[dict], name: str = "autosave", quiet: bool = False) -> str:
    """Saves non-system conversation messages to ~/.eustace/sessions/<name>.json."""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    slug = _sanitize_session_name(name)
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
        "cwd": os.getcwd(),
        "turn_count": sum(1 for m in non_sys if m.get("role") == "user"),
        "preview": str(first_user)[:100],
        "messages": non_sys,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    if not quiet:
        console.print(f"[dim cyan]✓ Session saved as '[bold]{escape(slug)}[/bold]' ({path})[/dim cyan]")
    return path


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


def load_session(target: str = "autosave") -> list[dict] | None:
    """Loads a saved session by name or 1-based index and prepends the fresh system prompt."""
    sessions = list_saved_sessions()
    if not sessions:
        console.print("[dim yellow]No saved sessions found.[/dim yellow]")
        return None

    chosen = None
    t = target.strip()
    if t.isdigit():
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
    """Interactive /model selector that updates global MODEL and saves to ~/.eustace/config.json."""
    global MODEL
    choice = arg.strip()

    # If the user just typed "/model" without a name, show a pretty table of options
    if not choice:
        tbl = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold cyan")
        tbl.add_column("#", style="bold yellow", width=3)
        tbl.add_column("Model ID", style="bold white")
        tbl.add_column("Mode", style="green")
        tbl.add_column("Description", style="dim")

        for idx, (m_id, desc) in enumerate(AVAILABLE_MODELS, start=1):
            active = " (active)" if m_id == MODEL else ""
            mode = "⚡ Tools" if m_id.endswith("-withtools") else "💬 Chat only"
            style = "bold cyan" if m_id == MODEL else None
            tbl.add_row(str(idx), f"{m_id}{active}", mode, desc, style=style)

        console.print(Panel(tbl, title="[bold cyan]Select AI Model[/bold cyan]", border_style="cyan", box=box.ROUNDED))
        console.print("[bold cyan]Enter model # or name (or press Enter to keep current):[/bold cyan] ", end="")
        choice = input().strip()
        if not choice:
            return MODEL

    # If they selected a number (1-6)
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(AVAILABLE_MODELS):
            MODEL = AVAILABLE_MODELS[idx][0]
            _save_selected_model(MODEL)
            console.print(f"  [bold green]✓ Switched model to:[/bold green] [bold cyan]{MODEL}[/bold cyan]")
            return MODEL
        console.print("[bold red]Invalid model number.[/bold red]")
        return MODEL

    # If they typed the exact name or shorthand (like "argon")
    for m_id, _ in AVAILABLE_MODELS:
        if choice.lower() == m_id.lower():
            MODEL = m_id
            _save_selected_model(MODEL)
            console.print(f"  [bold green]✓ Switched model to:[/bold green] [bold cyan]{MODEL}[/bold cyan]")
            return MODEL

    # Suffix matcher: if they type "argon", map it to "gemini-4-argon-withtools" automatically
    for m_id, _ in AVAILABLE_MODELS:
        if choice.lower() in m_id.lower() and m_id.endswith("-withtools"):
            MODEL = m_id
            _save_selected_model(MODEL)
            console.print(f"  [bold green]✓ Switched model to:[/bold green] [bold cyan]{MODEL}[/bold cyan]")
            return MODEL

    # Fallback to whatever string they typed
    MODEL = choice
    _save_selected_model(MODEL)
    console.print(f"  [bold yellow]✓ Switched to custom model ID:[/bold yellow] [bold cyan]{MODEL}[/bold cyan]")
    return MODEL

def main():
    cwd = os.getcwd()
    sessions = list_saved_sessions()
    session_hint = ""
    if sessions:
        latest = sessions[0]
        session_hint = (
            f"\n[dim]Recent Session:[/dim] [bold]{escape(latest['name'])}[/bold] "
            f"({latest['turn_count']} turns, {latest['updated_at']}) — type [cyan]/resume[/cyan] to restore"
        )

    console.print(
        Panel.fit(
            f"[bold cyan]GenAI.mil CLI — Eustace v{VERSION}[/bold cyan] ([dim]{MODEL} | {len(TOOLS)} tools[/dim])\n"
            f"[dim]Workspace:[/dim] {cwd}\n"
            f"[dim]Audit Log:[/dim] {AUDIT_LOG_PATH}\n"
            "[dim]Commands:[/dim] [cyan]/save [name][/cyan], [cyan]/resume [name|#][/cyan], [cyan]/sessions[/cyan], "
            "[cyan]/compact[/cyan], [cyan]/clear[/cyan], [cyan]/exit[/cyan]\n"
            "[dim]Tip:[/dim] Press [bold]ESC[/bold] or [bold]Ctrl+C[/bold] while generating to stop mid-reply"
            f"{session_hint}",
            border_style="cyan",
        )
    )

    messages = [{"role": "system", "content": build_system_prompt()}]

    while True:
        try:
            console.print("\n[bold cyan]>[/bold cyan] ", end="")
            user_input = input().strip()

            if not user_input:
                continue

            cmd_parts = user_input.split(maxsplit=1)
            cmd = cmd_parts[0].lower()
            cmd_arg = cmd_parts[1].strip() if len(cmd_parts) > 1 else ""

            if cmd in ["/exit", "/quit", "exit", "quit"]:
                console.print("[dim]Goodbye![/dim]")
                break

            if cmd == "/clear":
                messages = [{"role": "system", "content": build_system_prompt()}]
                console.print("[dim cyan]✓ Context cleared & system_prompt.md reloaded[/dim cyan]")
                continue
            
            if cmd == "/model":
                select_model_interactive(cmd_arg)
                # Re-initialize the system prompt with the newly selected model name
                messages[0]["content"] = build_system_prompt()
                continue

            if cmd == "/sessions":
                saved = list_saved_sessions()
                if not saved:
                    console.print("[dim yellow]No saved sessions yet.[/dim yellow]")
                else:
                    console.print("[bold cyan]Saved Sessions (~/.eustace/sessions/):[/bold cyan]")
                    for idx, s in enumerate(saved, start=1):
                        prev = escape(s["preview"].replace("\n", " "))
                        console.print(
                            f"  [bold]{idx}.[/bold] [cyan]{escape(s['name'])}[/cyan] "
                            f"[dim]({s['turn_count']} turns | {s['updated_at']})[/dim] — \"{prev}\""
                        )
                continue

            if cmd == "/save":
                save_session(messages, name=cmd_arg or datetime.now().strftime("session_%Y%m%d_%H%M"))
                continue

            if cmd == "/resume":
                restored = load_session(cmd_arg or "autosave")
                if restored is not None:
                    messages = restored
                continue

            if cmd == "/compact":
                try:
                    messages = compact_messages(messages)
                    save_session(messages, name="autosave", quiet=True)
                    console.print("[dim cyan]✓ Context compacted into a summary[/dim cyan]")
                except Exception as e:
                    console.print(f"[bold red]Compact failed:[/bold red] {escape(str(e))}")
                continue

            messages.append({"role": "user", "content": user_input})
            run_agent_turn(messages)
            # Automatically checkpoint every completed turn to autosave.json
            save_session(messages, name="autosave", quiet=True)

        except KeyboardInterrupt:
            console.print("\n[dim yellow]Interrupted. Type /exit to quit, or enter your next prompt.[/dim yellow]")
            continue
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break
        except Exception as e:
            console.print(f"\n[bold red]Error:[/bold red] {escape(str(e))}")
            if messages and messages[-1].get("role") == "user":
                messages.pop()



if __name__ == "__main__":
    main()
