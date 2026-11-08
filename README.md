# Electronic Utility for Scripting, Tasks, and Automated Command Execution (EUSTACE)
A local CLI agent powered by **GenAI.mil** models with built-in tools for local files, PowerShell, Microsoft Office (Word, Excel, PowerPoint, PDF), and Classic Outlook.

## Prerequisites
* **Windows Workstation** (No administrator rights required)
* **GenAI.mil API Key** (Eustace will prompt you for it)

---

## Installation

### 1. Download Eustace
**Option A - Using Git:**
Open PowerShell and run:
```powershell
git clone github.com/Colciferr/genai-cli
cd genai-cli
```

**Option B - Without Git (ZIP Download):**
1. Click the green **Code** button on GitHub and select **Download ZIP**.
2. Extract the ZIP folder to your computer (e.g., `Documents\genai-cli`).
3. Open **PowerShell** and navigate into that folder:
   ```powershell
   cd "$HOME\Documents\genai-cli"
   ```

**Option C - Manual Copy & Paste (if Git and ZIP downloads are blocked)**
Open PowerShell and run this command to create the folder and empty files:
```powershell
mkdir "$HOME\Documents\genai-cli" -Force; cd "$HOME\Documents\genai-cli"
"cli.py","pyproject.toml","setup.ps1","system_prompt.md" | ForEach-Object { New-Item $_ -ItemType File -Force }
```

On GitHub, click into each of those four files, click the "Copy Raw File" button (top-right, next to "Raw"), paste the contents into the matching local file you created, and save.

---

### 2. Run the Installer Script
In your PowerShell window inside the folder, run:
```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
**What this script does automatically:**
* Checks for **Python 3.11+** (and installs Python via `winget` in user-scope if missing).
* Installs `pipx` and registers the `eustace` command globally for your user account.
* Creates your user config folder at `C:\Users\<YourUsername>\.eustace\`.


---

### 3. Launch & Enter Your API Key
1. **Close your current PowerShell window** and open a **new** PowerShell terminal (so your updated `PATH` loads).
2. Navigate to any folder you want to work in and run:
   ```powershell
   eustace
   ```
3. On your first run, Eustace will prompt you to paste your **GenAI.mil API Key** and automatically save it to `~/.eustace/.env` (as `STARK_API_KEY`).

---

## Customization & Updating
* **Custom Persona:** You can edit `~/.eustace/system_prompt.md` at any time to customize Eustace's instructions or tone.
* **Updating:** Pull or download the latest files from GitHub and re-run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`.

## Security Behaviour Worth Knowing
* **Reads outside your working directory prompt for approval.** `read_file` and `search_files` stay approval-free inside the folder you launched `eustace` from; anywhere else shows the same approval panel as a write. Press `A` to approve all for the turn.
* **Project `AGENT.md` files are trusted explicitly.** Only `~/.eustace/AGENT.md` loads silently. An `AGENT.md` in your project (or any parent folder up to the git root) is shown once with its size and hash, and loads only after you answer `y`. The decision is remembered in `~/.eustace/config.json` until the file changes.
* **Paths are canonicalized.** UNC paths (`\\server\share`), device paths (`\\?\`, `\\.\pipe\`), alternate data streams (`file.txt:stream`), 8.3 short names and junctions are resolved or rejected before any guard runs, and re-checked after you approve a write.
* **PowerShell runs from a fixed host path** (`pwsh.exe` under Program Files, else `powershell.exe` under System32), shown in the approval panel, with the API key removed from the child environment. Commands containing hidden control characters are refused.
* **`open_file` launches only known document types** (txt, md, csv, log, json, xml, pdf, docx, xlsx, pptx, common images) and folders. Word and Excel files Eustace creates carry the Mark-of-the-Web so Office opens them in Protected View.

## Testing at Home: `--devmode`
GenAI.mil is reachable only from DODIN/AFNET domain-joined machines. To exercise Eustace's tools elsewhere, run it against Google's public Gemini API:
```powershell
eustace --devmode
```
* Uses the OpenAI-compatible Gemini endpoint with a **`GEMINI_API_KEY`** (prompted on first run and saved to `~/.eustace-dev/.env`).
* Keeps **everything separate** in `~/.eustace-dev\` (config, sessions, audit log, persona), so your GenAI.mil key, model choice and sessions are never touched. Both `~/.eustace` and `~/.eustace-dev` are unreadable by the model in either mode.
* Default model is `gemini-3.1-flash-lite`; override with the `EUSTACE_DEV_MODEL` environment variable or pick from the live list with `/model`. On a free-tier key, preview models such as `gemini-3-flash-preview` allow only 20 requests per day, and `gemini-flash-latest` (2.5 Flash) intermittently 503s or stalls on tool calls.
* Gemini 3 models attach a `thought_signature` to each tool call and reject the follow-up request with a 400 if it is missing; Eustace echoes it back automatically.
* The banner turns yellow and the system prompt tells the model it is in dev mode. **Use synthetic test data only. Never process CUI, PII or PHI through the public API.**
* `eustace --help` and `eustace --version` are also available.

### Running the unit tests
```powershell
& "$env:LOCALAPPDATA\pipx\pipx\venvs\genai-cli\Scripts\python.exe" -I tests\test_guards.py -v
```

## AI Disclaimer
Codebase built with Gemini 4 Argon, with some reviews and edits by Claude Fable 5.1.
