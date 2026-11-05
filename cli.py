import ctypes
import fnmatch
import json
import msvcrt
import os
import shutil
import subprocess
from ctypes import wintypes

import truststore

truststore.inject_into_ssl()

from dotenv import load_dotenv
from openai import OpenAI
from openpyxl import load_workbook
from pypdf import PdfReader
from docx import Document
from pptx import Presentation
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

# Look for .env in C:\Users\<Username>\.eustace\.env
global_env_path = os.path.join(os.path.expanduser("~"), ".eustace", ".env")

if os.path.exists(global_env_path):
    load_dotenv(global_env_path)
else:
    # Fallback to local directory (for local development)
    load_dotenv()

MODEL = "gemini-3.5-flash-lite-withtools"

client = OpenAI(
    base_url="https://api.genai.mil/v1",
    api_key=os.environ.get("STARK_API_KEY"),
)

console = Console()

# =====================================================================
# 1. LOCAL FILE SYSTEM TOOLS (Plaintext + Excel, PDF, Word, PowerPoint)
# =====================================================================

IGNORE_DIRS = {".venv", ".git", "__pycache__", "node_modules"}

# Files the model is never allowed to read (API keys, private keys, password DBs)
SENSITIVE_PATTERNS = {
    ".env", ".env.*", "*.pem", "*.key", "*.pfx", "*.p12",
    "id_rsa*", "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.ppk",
}
EUSTACE_HOME = os.path.join(os.path.expanduser("~"), ".eustace")

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


def _is_sensitive(target: str) -> bool:
    """True for secrets files (by name pattern) or anything inside ~/.eustace."""
    name = os.path.basename(target)
    if any(fnmatch.fnmatch(name, pat) for pat in SENSITIVE_PATTERNS):
        return True
    return _is_under(target, EUSTACE_HOME)


def _is_protected_path(target: str) -> bool:
    """True if target is inside the Windows or Program Files trees."""
    return any(_is_under(target, root) for root in PROTECTED_ROOTS)

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
            return f"Security Error: '{path}' matches a protected secrets pattern and will not be read."

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

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [Truncated at {max_chars} chars]"
        return content
    except Exception as e:
        return f"Error reading file '{path}': {e}"

def write_file(path: str, content: str) -> str:
    """Creates a new file or overwrites an existing file with the provided content (requires manual user confirmation)."""
    try:
        target = os.path.abspath(path)

        # Security Guard: Prevent writing under the Windows / Program Files trees
        if _is_protected_path(target):
            return "Security Error: Writing to system folders is strictly prohibited."

        # Clear, visible prompt
        console.print(f"\n[bold yellow]⚠️  E.U.S.T.A.C.E. wants to write to:[/bold yellow] [bold cyan]{target}[/bold cyan]")
        console.print(f"[dim]Content length: {len(content)} characters ({len(content.splitlines())} lines)[/dim]")
        
        # Flush before input to guarantee prompt is visible in terminal
        console.print("[bold yellow]Allow this write operation? [y/N]:[/bold yellow] ", end="")
        confirm = input().strip().lower()

        if confirm not in ["y", "yes"]:
            return "Operation aborted: User did NOT approve the file write."

        # Ensure directory exists
        os.makedirs(os.path.dirname(target), exist_ok=True)

        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

        console.print(f"[bold green]✓ File created/updated successfully: {target}[/bold green]\n")
        return f"Success: File written successfully to {target}"
    except Exception as e:
        return f"Error writing file '{path}': {e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """Replaces a specific block of text/code inside an existing file (requires manual user confirmation)."""
    try:
        target = os.path.abspath(path)
        if not os.path.exists(target):
            return f"Error: File not found: {path}"
        if _is_protected_path(target):
            return "Security Error: Editing files in system folders is strictly prohibited."

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            file_content = f.read()

        if old_text not in file_content:
            return "Error: Target text block ('old_text') was not found in the file. No changes made."

        console.print(f"\n[bold yellow]⚠️  E.U.S.T.A.C.E. wants to edit:[/bold yellow] [bold cyan]{target}[/bold cyan]")
        console.print(f"[red]- Remove:[/red]\n[dim]{escape(old_text)}[/dim]")
        console.print(f"[green]+ Replace with:[/green]\n[dim]{escape(new_text)}[/dim]")
        
        console.print("[bold yellow]Allow this edit operation? [y/N]:[/bold yellow] ", end="")
        confirm = input().strip().lower()

        if confirm not in ["y", "yes"]:
            return "Operation aborted: User did NOT approve the file edit."

        updated_content = file_content.replace(old_text, new_text, 1)
        with open(target, "w", encoding="utf-8") as f:
            f.write(updated_content)

        console.print(f"[bold green]✓ File edited successfully: {target}[/bold green]\n")
        return f"Success: File edited successfully at {target}"
    except Exception as e:
        return f"Error editing file '{path}': {e}"



SHELL_TIMEOUT_SECONDS = 120
MAX_SHELL_OUTPUT_CHARS = 20000


def run_powershell(command: str) -> str:
    """Runs a PowerShell command in the current working directory (requires manual user confirmation)."""
    try:
        console.print("\n[bold yellow]⚠️  E.U.S.T.A.C.E. wants to run a PowerShell command:[/bold yellow]")
        console.print(f"[bold cyan]{escape(command)}[/bold cyan]")
        console.print("[bold yellow]Allow this command to run? [y/N]:[/bold yellow] ", end="")
        confirm = input().strip().lower()

        if confirm not in ["y", "yes"]:
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
                    }
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
                    }
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
]

AVAILABLE_FUNCTIONS = {
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_powershell": run_powershell,
}


# =====================================================================
# 2. SYSTEM PROMPT & AGENT STREAMING LOOP
# =====================================================================

def build_system_prompt() -> str:
    """Loads global system_prompt.md from ~/.eustace/ and optional local AGENT.md."""
    cwd = os.getcwd()
    
    # Check global config directory first, fallback to script directory
    global_prompt_file = os.path.join(os.path.expanduser("~"), ".eustace", "system_prompt.md")
    local_prompt_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system_prompt.md")
    
    prompt_file = global_prompt_file if os.path.exists(global_prompt_file) else local_prompt_file

    if os.path.exists(prompt_file):
        with open(prompt_file, "r", encoding="utf-8") as f:
            base_prompt = f.read().strip()
    else:
        base_prompt = "You are a local CLI coding and operational assistant running on Windows."

    env_context = (
        f"\n\n# Runtime Environment\n"
        f"- Current Working Directory: `{cwd}`\n"
        f"- Available Tools: `list_directory`, `read_file`, `write_file`, `edit_file`, `run_powershell`\n"
    )

    # Allow local folder projects to override or add directives via a local AGENT.md
    agent_md = os.path.join(cwd, "AGENT.md")
    if os.path.exists(agent_md):
        with open(agent_md, "r", encoding="utf-8") as f:
            env_context += f"\n# Project Instructions (AGENT.md)\n{f.read().strip()}\n"

    return base_prompt + env_context

# --- ESC detection: peek the console input buffer without consuming typed-ahead text ---
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    """Raises KeyboardInterrupt if ESC is waiting in the console input buffer.

    Peeks rather than reads, so anything else the user typed while the model
    was generating stays buffered for the next input() prompt.
    """
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
        # Fallback when stdin is not a real console: original consume-everything behaviour
        while msvcrt.kbhit():
            if msvcrt.getch() == b"\x1b":
                raise KeyboardInterrupt("Aborted via ESC key")


MAX_TOOL_RESULT_CHARS = 30000
ARG_PREVIEW_CHARS = 120


def _summarize_args(fn_name: str, fn_args: dict) -> str:
    """Builds a short, markup-safe preview of tool arguments for the console banner."""
    if fn_name in ("write_file", "edit_file"):
        shown = {"path": fn_args.get("path")}
    else:
        shown = fn_args
    text = repr(shown)
    if len(text) > ARG_PREVIEW_CHARS:
        text = text[: ARG_PREVIEW_CHARS - 3] + "..."
    return escape(text)


def _tail_lines(text: str, max_lines: int) -> str:
    """Returns the last max_lines lines of text (used for the live streaming preview)."""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def run_agent_turn(messages: list[dict]):
    """Streams responses & tool calls with instant ESC or Ctrl+C cancellation."""
    turn_start_len = len(messages)
    partial_reply = ""  # text streamed but not yet rendered as Markdown

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

            # Final render: the live preview was raw text, this is the proper Markdown view
            if full_reply:
                console.print(Markdown(full_reply))
                partial_reply = ""

            # Handle Tool Calls
            if tool_calls_buffer:
                # Convert buffer back to standard list
                tool_calls_list = []
                for i in sorted(tool_calls_buffer.keys()):
                    item = tool_calls_buffer[i]
                    # Ensure arguments are treated as a string, not parsed dict yet
                    tool_calls_list.append({
                        "id": item["id"],
                        "type": "function",
                        "function": {
                            "name": item["function"]["name"],
                            "arguments": item["function"]["arguments"]
                        }
                    })

                assistant_msg = {"role": "assistant", "tool_calls": tool_calls_list}
                if full_reply:
                    assistant_msg["content"] = full_reply
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

                    console.print(
                        f"  [bold yellow]⚡ Tool:[/bold yellow] [cyan]{escape(fn_name)}[/cyan]"
                        f"[dim]({_summarize_args(fn_name, fn_args)})[/dim]"
                    )

                    # Find and run the tool
                    func = AVAILABLE_FUNCTIONS.get(fn_name)
                    if func:
                        try:
                            result = func(**fn_args)
                        except Exception as fe:
                            # Catch and print the exact traceback if the tool crashes
                            console.print(f"[bold red]Tool execution error inside '{escape(fn_name)}':[/bold red] {escape(str(fe))}")
                            result = f"Error: Tool execution failed: {fe}"
                    else:
                        result = f"Error: Unknown tool '{fn_name}'"

                    # Clip oversized results so a single tool call can't blow the context window
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
        # Keep the user's prompt; drop the half-finished assistant/tool entries
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

def main():
    cwd = os.getcwd()
    console.print(
        Panel.fit(
            f"[bold cyan]GenAI.mil CLI — Eustace[/bold cyan] ([dim]{MODEL}[/dim])\n"
            f"[dim]Workspace:[/dim] {cwd}\n"
            "[dim]Commands:[/dim] [cyan]/clear[/cyan] (reset & reload prompt), "
            "[cyan]/compact[/cyan] (summarize history to free context), [cyan]/exit[/cyan] (quit)\n"
            "[dim]Tip:[/dim] Press [bold]ESC[/bold] or [bold]Ctrl+C[/bold] while generating to stop mid-reply",
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

            if user_input.lower() in ["/exit", "/quit", "exit", "quit"]:
                console.print("[dim]Goodbye![/dim]")
                break
            if user_input.lower() == "/clear":
                messages = [{"role": "system", "content": build_system_prompt()}]
                console.print("[dim cyan]✓ Context cleared & system_prompt.md reloaded[/dim cyan]")
                continue
            if user_input.lower() == "/compact":
                try:
                    messages = compact_messages(messages)
                    console.print("[dim cyan]✓ Context compacted into a summary[/dim cyan]")
                except Exception as e:
                    console.print(f"[bold red]Compact failed:[/bold red] {escape(str(e))}")
                continue

            messages.append({"role": "user", "content": user_input})
            run_agent_turn(messages)

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
