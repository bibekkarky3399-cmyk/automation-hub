# Helix

Local ops launcher for airline workflows: OCR → CSV, Yeti B2B PNR booking, and TBO Series Fare upload. Config lives in `config/scripts.json`; jobs run on this machine through a small Flask UI.

## What you get

- Run workflows and pipelines from the browser
- Active jobs, history, metrics
- Admin Settings (workflows / pipelines); operators can run jobs but not edit Settings
- Shared Active job view — only the starter can stop their own job
- Run in background + inbox notifications while a job continues

## Prerequisites

- Python **3.10+**
- macOS / Linux / Windows
- Playwright Chromium (PNR workflow)
- **Yeti API PNR** (`scripts/pnr/yeti_api_book.py`) — same B2B ASMX calls as the portal, no browser (requires sibling `aggregator` repo client)

## Setup & run locally

### 1. Install

**macOS (one script)**

```bash
cd /path/to/automation-hub
bash deploy/run-macos.sh
```

This clears caches, ensures a macOS `.venv`, installs deps, and starts `app.py` at http://127.0.0.1:5050.  
Flags: `--fresh` (recreate venv), `--no-run` (setup only).

**macOS / Linux (manual)**

```bash
cd /path/to/automation-hub
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
```

**Windows (PowerShell)**

Create a **new** venv on the Windows machine — do not copy `.venv` from a Mac/Linux checkout.

```powershell
cd C:\path\to\automation-hub
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell blocks activation:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

### 2. Point Helix at the venv Python

In `config/scripts.json`, set `"python"` for the OS that will **run** Helix (the host machine):

| OS | `"python"` value |
|----|------------------|
| macOS / Linux | `.venv/bin/python3` |
| Windows | `.venv/Scripts/python.exe` (forward slashes — backslashes break JSON) |

Example on Windows:

```json
"python": ".venv/Scripts/python.exe"
```

Do **not** write `.venv\\Scripts\\python.exe` with single `\` characters in the JSON file — that causes `Invalid \escape` and breaks features like flight city loading.

### 3. Configure auth

**macOS / Linux**

```bash
cp .env.example .env
```

**Windows (PowerShell)**

```powershell
copy .env.example .env
```

Edit `.env` before sharing with anyone:

- `HUB_SECRET_KEY` — long random string
- `HUB_ADMIN_USERNAME` / `HUB_ADMIN_PASSWORD` — Settings + full access
- `HUB_OPERATOR_USERNAME` / `HUB_OPERATOR_PASSWORD` — run jobs, history, metrics (no Settings)

Defaults (change these):

| Role | Username | Password |
|------|----------|----------|
| Admin | `admin` | `admin` |
| Operator | `operator` | `operator` |

### 4. Start

**macOS / Linux**

```bash
python3 app.py
```

**Windows**

```powershell
python app.py
```

Open **http://127.0.0.1:5050** and sign in.

- **Today** (`/`) — shift board: waiting on me, files ready  
- **Workflows** (`/workflows`) — start tasks and chains

### Windows notes

- Use 64-bit Python 3.10+
- PNR needs Playwright Chromium (`python -m playwright install chromium`)
- OCR uses PaddleOCR — first run may download models and take longer
- Keep `output_dir` as a relative folder like `"outputs"` (works on all OSes)
- Team members who only open the Cloudflare link do **not** need Python on their PC — only the host machine does

## Team access via Cloudflare Tunnel

Keep Helix running on one always-on machine (or your laptop while the team needs it). Cloudflare Tunnel exposes it as a temporary HTTPS URL without opening router ports.

### On the host machine

1. Start Helix:

```bash
# macOS / Linux
source .venv/bin/activate
python3 app.py

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
python app.py
```

2. Install Cloudflare’s tunnel client (once):

```bash
# macOS
brew install cloudflared

# Windows (winget)
winget install Cloudflare.cloudflared
```

3. In a **second** terminal, start a quick tunnel:

```bash
cloudflared tunnel --url http://127.0.0.1:5050
```

4. Copy the URL from the box in the logs, for example:

```text
https://xxxx-xxxx-xxxx.trycloudflare.com
```

Share that link with the team. They sign in with the admin or operator accounts from `.env`.

### Team notes

- Leave **both** terminals running (Helix + `cloudflared`). Closing either drops access.
- Quick-tunnel URLs change every time you restart `cloudflared`.
- Anyone with the URL can reach the login page — use strong passwords in `.env`.
- Jobs and Active state live in memory on the host; restarting Helix clears live jobs (history on disk remains).
- For a stable hostname long-term, use a [named Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/) with a Cloudflare account instead of the quick URL.

## Project layout (essentials)

```text
app.py                 # Flask UI
config/scripts.json    # Workflow definitions
launcher/              # Jobs, auth, history, pipelines
scripts/ocr/           # OCR → CSV
scripts/pnr/           # Yeti B2B PNR (Playwright)
scripts/series_fare/   # TBO Series Fare API upload
templates/ static/     # UI
data/                  # Run history / metrics
outputs/               # All run CSVs (UUID filenames; set via scripts.json output_dir)
```

Shared output folder is configured once in `config/scripts.json`:

```json
"output_dir": "outputs"
```

Admins can download a client backup (history, outputs, config, scripts, …) from **Settings → Backup everything**. `.env` secrets and project README are not included.
