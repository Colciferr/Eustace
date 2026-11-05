import json
import msvcrt
import os
import truststore

truststore.inject_into_ssl()

from dotenv import load_dotenv
from openai import OpenAI
from openpyxl import load_workbook
from pypdf import PdfReader
from docx import Document
from pptx import Presentation
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

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

        # Security Guard: Prevent writing to system folders
        if any(part.lower() in ["windows", "system32"] for part in target.split(os.sep)):
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

        with open(target, "r", encoding="utf-8", errors="replace") as f:
            file_content = f.read()

        if old_text not in file_content:
            return "Error: Target text block ('old_text') was not found in the file. No changes made."

        console.print(f"\n[bold yellow]⚠️  E.U.S.T.A.C.E. wants to edit:[/bold yellow] [bold cyan]{target}[/bold cyan]")
        console.print(f"[red]- Remove:[/red]\n[dim]{old_text}[/dim]")
        console.print(f"[green]+ Replace with:[/green]\n[dim]{new_text}[/dim]")
        
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
]

AVAILABLE_FUNCTIONS = {
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file
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
        f"- Available Tools: `list_directory`, `read_file`, `write_file`, `edit_file`\n"
    )

    # Allow local folder projects to override or add directives via a local AGENT.md
    agent_md = os.path.join(cwd, "AGENT.md")
    if os.path.exists(agent_md):
        with open(agent_md, "r", encoding="utf-8") as f:
            env_context += f"\n# Project Instructions (AGENT.md)\n{f.read().strip()}\n"

    return base_prompt + env_context

def check_user_abort():
    """Checks if the user pressed ESC on Windows to abort generation."""
    while msvcrt.kbhit():
        key = msvcrt.getch()
        if key == b"\x1b":  # ESC key
            raise KeyboardInterrupt("Aborted via ESC key")


def run_agent_turn(messages: list[dict]):
    """Streams responses & tool calls with instant ESC or Ctrl+C cancellation."""
    turn_start_len = len(messages)

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
            printed_header = False

            try:
                for chunk in stream:
                    check_user_abort()
                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        if not printed_header:
                            print()
                            printed_header = True
                        print(delta.content, end="", flush=True)
                        full_reply += delta.content

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
                stream.close()

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
                        console.print(f"[bold red]Error parsing tool arguments JSON:[/bold red] {je}")
                        fn_args = {}

                    console.print(f"  [bold yellow]⚡ Tool:[/bold yellow] [cyan]{fn_name}[/cyan][dim]({fn_args})[/dim]")

                    # Find and run the tool
                    func = AVAILABLE_FUNCTIONS.get(fn_name)
                    if func:
                        try:
                            result = func(**fn_args)
                        except Exception as fe:
                            # Catch and print the exact traceback if the tool crashes
                            console.print(f"[bold red]Tool execution error inside '{fn_name}':[/bold red] {fe}")
                            result = f"Error: Tool execution failed: {fe}"
                    else:
                        result = f"Error: Unknown tool '{fn_name}'"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result),
                    })
                continue

            print()
            messages.append({"role": "assistant", "content": full_reply})
            break

    except KeyboardInterrupt:
        console.print("\n  [bold red]⏹ Stopped by user[/bold red]")
        del messages[turn_start_len - 1 :]
    except Exception as e:
        console.print(f"\n[bold red]System Loop Error:[/bold red] {e}")
        del messages[turn_start_len - 1 :]


def main():
    cwd = os.getcwd()
    console.print(
        Panel.fit(
            f"[bold cyan]GenAI.mil CLI — Eustace[/bold cyan] ([dim]{MODEL}[/dim])\n"
            f"[dim]Workspace:[/dim] {cwd}\n"
            "[dim]Commands:[/dim] [cyan]/clear[/cyan] (reset & reload prompt), [cyan]/exit[/cyan] (quit)\n"
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

            messages.append({"role": "user", "content": user_input})
            run_agent_turn(messages)

        except KeyboardInterrupt:
            console.print("\n[dim yellow]Interrupted. Type /exit to quit, or enter your next prompt.[/dim yellow]")
            continue
        except EOFError:
            console.print("\n[dim]Goodbye![/dim]")
            break
        except Exception as e:
            console.print(f"\n[bold red]Error:[/bold red] {e}")
            if messages and messages[-1].get("role") == "user":
                messages.pop()


if __name__ == "__main__":
    main()
