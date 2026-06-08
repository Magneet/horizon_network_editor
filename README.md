# Horizon Network Editor

A PySide6 GUI tool for editing portgroup / network-label assignments on VMware Horizon 2312 or later instant-clone desktop pools and RDS farms.

## Features

- Browse all instant-clone VDI pools and RDS farms across all pods
- Per-NIC multi-select portgroup assignment (or revert to **From Golden Image**)
- Enable provisioning and optionally delete all machines/servers after a network change so they redeploy with the new portgroup
- Handles stale snapshot references — falls back to the v2 NIC API which only needs the base VM, no snapshot
- Credentials stored via OS keyring; config saved to `hneditor_config.ini`
- Builds to a self-contained `.app` (macOS) or `.exe` folder (Windows) via PyInstaller

## Pre-built binaries

If you just want to run the tool without Python or any build steps, grab the pre-built binary for your platform from the [Releases](../../releases) page:

| Platform | Download | Location after extracting |
|----------|----------|---------------------------|
| macOS    | `Horizon.Network.Editor-mac.zip` | `Horizon Network Editor.app` — double-click to open |
| Windows  | `Horizon.Network.Editor-win.zip` | `Horizon Network Editor\Horizon Network Editor.exe` — double-click to run |

> **macOS note:** on first launch macOS may show a security warning because the app is not notarized. Right-click (or Control-click) the `.app` and choose **Open**, then confirm in the dialog. You only need to do this once.

## Requirements (running from source)

- Python 3.11+
- VMware Horizon 2312 or later

## Running from source

```bash
# 1. Clone the repo
git clone https://github.com/Magneet/horizon_network_editor.git
cd horizon_network_editor

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Launch
python horizon_network_editor.py
```

## Building a distributable

The build scripts create a self-contained package (no Python installation required on the target machine). Run from the project root after cloning.

### macOS — produces `dist/Horizon Network Editor.app`

**Prerequisites:** Python 3.11+ installed (e.g. from [python.org](https://www.python.org/downloads/) or Homebrew).

```bash
bash build_mac.sh
```

The script will:
1. Create a `.venv` virtual environment (if one doesn't exist)
2. Install all dependencies including PyInstaller
3. Run PyInstaller with the bundled spec file

Output: `dist/Horizon Network Editor.app`

To run immediately after building:
```bash
open "dist/Horizon Network Editor.app"
```

To distribute, zip the `.app` bundle:
```bash
cd dist
zip -r "Horizon.Network.Editor-mac.zip" "Horizon Network Editor.app"
```

### Windows — produces `dist\Horizon Network Editor\Horizon Network Editor.exe`

**Prerequisites:** Python 3.11+ installed from [python.org](https://www.python.org/downloads/) (make sure **Add Python to PATH** is checked during install).

```bat
build_win.bat
```

The script will:
1. Create a `.venv` virtual environment (if one doesn't exist)
2. Install all dependencies including PyInstaller
3. Run PyInstaller with the bundled spec file

Output: `dist\Horizon Network Editor\` folder containing `Horizon Network Editor.exe` and all required DLLs.

To run immediately after building:
```bat
"dist\Horizon Network Editor\Horizon Network Editor.exe"
```

To distribute, zip the entire output folder (the `.exe` alone will not work without the other files in that folder):
```bat
powershell Compress-Archive -Path "dist\Horizon Network Editor" -DestinationPath "Horizon.Network.Editor-win.zip"
```

## Configuration

On first run, go to the **Configuration** tab:

1. Enter your Horizon **username**, **domain**, and **connection server** (hostname or IP)
2. Click **Set Password** and enter your password — it is stored in the OS keyring (not written to disk)
3. Click **Test & Save** — the tool connects, discovers all pods and connection servers, and saves the config to `hneditor_config.ini`

> If you later change the username, domain, or server, any cached pool/farm data is automatically cleared so you start fresh with the new environment.

## Usage

### VDI Pools tab

1. Click **Connect** — the tool logs in and lists all instant-clone desktop pools
2. Select a pool from the dropdown
3. The current portgroup assignments are pre-selected in each NIC row
4. Click the NIC dropdown and check one or more portgroups:
   - **From Golden Image** — inherit the portgroup from the base VM (exclusive with named portgroups)
   - Any named portgroup — Horizon supports assigning multiple portgroups per NIC for load-balancing
5. Optionally check:
   - **Enable provisioning after saving** — re-enables provisioning on the pool if it was disabled
   - **Delete all machines after saving** — force-deletes all machines in the pool so they redeploy with the new network assignment
6. Click **Apply Network Changes**

### RDS Farms tab

Works the same way as VDI Pools but targets RDS server farms. The **Delete all machines** option removes all RDS servers, which forces Horizon to recreate them on the new portgroup.

### Status area

The text area at the bottom of each tab shows progress and any warnings:

- **NIC warning** — shown when the golden image or base VM is missing or misconfigured in vCenter. The network assignment cannot be loaded; fix the pool/farm configuration first.
- **Rate limited** — shown when Horizon's REST API returns too many requests (HTTP 429). The tool retries automatically up to 5 times; if it still fails, wait a moment and click Connect again.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| NIC dropdown is empty / shows warning | Golden image VM or snapshot deleted from vCenter | Reconfigure the pool's golden image in Horizon Console |
| "Rate limited" message | Too many API requests in a short window | Wait 30–60 seconds and click Connect again |
| macOS security warning on first launch | App is unsigned / not notarized | Right-click → Open → Open (one-time) |
| Machines not redeploying after network change | Provisioning is disabled on the pool | Check **Enable provisioning after saving** before applying |
