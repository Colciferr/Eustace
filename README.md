# Electronic Utility for Scripting, Tasks, and Automated Command Execution (EUSTACE)
A local CLI agent powered by **GenAI.mil** models with built-in tools for local files, PowerShell, Microsoft Office (Word, Excel, PowerPoint, PDF), and Classic Outlook.

## Prerequisites
* **Windows Workstation** (No administrator rights required)
* **GenAI.mil API Key** (Wait until first launch—Eustace will prompt you for it)

---

## Installation

### 1. Download Eustace
**Option A - Using Git:**
Open PowerShell and run:
```powershell
git clone <YOUR_GITHUB_REPO_URL>
cd genai-cli
```

**Option B - Without Git (ZIP Download):**
1. Click the green **Code** button on GitHub and select **Download ZIP**.
2. Extract the ZIP folder to your computer (e.g., `Documents\genai-cli`).
3. Open **PowerShell** and navigate into that folder:
   ```powershell
   cd "$HOME\Documents\genai-cli"
   ```

**Option C - Manual Copy & Paste (if Git and ZIP downloads are blocked)
Open PowerShell and run this command to create the folder and empty files:
```powershell
mkdir "$HOME\Documents\genai-cli" -Force; cd "$HOME\Documents\genai-cli"
"cli.py","pyproject.toml","setup.ps1","system_prompt.md" | ForEach-Object { New-Item $_ -ItemType File -Force }
```

On GitHub, click into each of those four files, clikc the "Copy Raw File" button (top-right, next to "Raw"), paste the contents into the matching local file you created, and save. 
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

## AI Disclaimer
Codebase built with Gemini 4 Argon, with some reviews and edits by Claude Fable 5.1.
