# Electronic Utility for Scripting, Tasks, and Automated Command Execution (EUSTACE)
A local CLI agent powered by **GenAI.mil** models with built-in tools for local files, PowerShell, Microsoft Office (Word, Excel, PowerPoint, PDF), and Classic Outlook.
Although this was built with GenAI.mil in mind, it can technically access any OpenAI-compatible API endpoint and isn't strictly a DoD tool.

## Prerequisites
* **Windows Workstation** (No administrator rights required)
* **GenAI.mil API Key** (Eustace will prompt you for it)

---

## Installation

### 1. Download Eustace
**Option A - Using Git:**
Open PowerShell and run: (use http to bypass signing in)
```powershell
git clone https://github.com/Colciferr/eustace
```
or
```powershell
git clone https://gitlab.com/Colciferr/eustace
```

**Option B - Without Git (ZIP Download):**
1. Click the green **Code** button on GitHub and select **Download ZIP**.
2. Extract the ZIP folder to your computer (e.g., `Documents\eustace`).
3. Open **PowerShell** and navigate into that folder:
   ```powershell
   cd "$HOME\Documents\eustace"
   ```

**Option C - Manual Copy & Paste (if Git and ZIP downloads are blocked)**
Open PowerShell and run this command to create the folder and empty files:
```powershell
mkdir "$HOME\Documents\eustace" -Force; cd "$HOME\Documents\eustace"
"cli.py","pyproject.toml","setup.ps1","system_prompt.md" | ForEach-Object { New-Item $_ -ItemType File -Force }
```

On GitHub, click into each of those four files, click the "Copy Raw File" button (top-right, next to "Raw"), paste the contents into the matching local file you created, and save.

---

### 2. Run the Installer Script
In your PowerShell window, from the `\eustace` folder, run:
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

## Agent Skills
Eustace supports [Agent Skills](https://agentskills.io): folders of instructions it loads only when a task needs them. Each skill is a folder with a `SKILL.md` file. That file starts with a `name` (lowercase letters, digits, and hyphens, matching the folder name) and a `description` of when to use it.

* **Where to put skills:**
  * `~/.eustace/skills/<name>/` loads without asking.
  * `~/.agents/skills/<name>/` and `.agents/skills/<name>/` (in the folder you start Eustace in, or any folder above it up to the repo root) ask once before loading.
  * If a project skill has the same name as one of yours, yours wins.
* **Trust:** at startup, untrusted skills are listed together. Type `a` for all, numbers like `1,3` for some, or press Enter for none. Your approval covers that exact version of every file in the skill's folder. If any file changes, Eustace asks again.
* **Using skills:** only each skill's name and description go into Eustace's instructions. When a request matches one, Eustace loads the full skill itself. You can also run one directly with `/<name> [request]`. `/skills` lists what's loaded.
* **Still asks in every mode:** writing anything under a `.agents` folder, and any shell command that touches one. That includes running a skill's scripts.

## Approvals, Auto Mode & Undo
By default, Eustace asks before it writes a file, runs a command, or reads outside the folder you started it in.

* **Auto mode:** press **Shift+Tab** in the input box, type `/auto`, or start with `eustace --auto`. The status line shows `⏵⏵ AUTO`. File reads (anywhere), file writes, and PowerShell commands then run without asking. Auto mode is off each time Eustace starts.
* **Still asks in auto mode:**
  * Shell commands on the deny-list: deleting, registry, accounts, downloads, scheduled tasks and services, remoting, security settings, encoded or dynamically built commands, other interpreters, script files, `git push`/`reset --hard`/`clean`, and any script Eustace itself wrote this session. The prompt says which pattern matched. Add your own patterns (regular expressions) under `"shell_deny"` in `~/.eustace/config.json`; the built-in list can't be shortened.
  * Writes to `AGENT.md`, `system_prompt.md`, `.git`, `.github`, and `.eustace` (these change Eustace's instructions or run automatically).
  * Outlook actions, the clipboard, opening files, and dev-mode data.
* **Never allowed in any mode:** system folders, secrets and credential files, PHI.
* **Running as Administrator:** if Eustace runs elevated, the banner and status line show a red `⚠ ADMIN` warning, auto mode is unavailable (`--auto`, Shift+Tab, and `/auto` are refused with a reason), and every approval prompt says the action gets admin rights. Approvals and allow rules still work. Start Eustace from a normal PowerShell window to use auto mode.
* **Undo:** `/undo` restores every file Eustace's file tools changed in the most recent turn that wrote anything (and deletes files that turn created), after one confirmation. Run it again to step back further. Changes made by shell commands aren't covered. Checkpoints are kept for 7 days, up to 200 MB.

## Customization & Updating
* **Custom Persona:** You can edit `~/.eustace/system_prompt.md` at any time to customize Eustace's instructions or tone.
* **Updating:** Pull or download the latest files from GitHub and re-run `powershell -ExecutionPolicy Bypass -File .\setup.ps1`.

## AI Disclaimer
Codebase built with Claude Fable 5.1/2 and Gemini 4 Argon. Hardening and security audits mostly done by [Cloudflare](https://github.com/cloudflare/security-audit-skill).
