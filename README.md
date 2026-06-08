# Horizon Network Editor

A PySide6 GUI tool for editing portgroup / network-label assignments on VMware Horizon 2312 instant-clone desktop pools and RDS farms.

## Features

- Browse all instant-clone VDI pools and RDS farms across all pods
- Per-NIC multi-select portgroup assignment (or revert to **From Golden Image**)
- Enable provisioning and optionally delete all machines/servers after a network change so they redeploy with the new portgroup
- Handles stale snapshot references — falls back to the v2 NIC API which only needs the base VM, no snapshot
- Credentials stored via OS keyring; config saved to `hneditor_config.ini`
- Builds to a self-contained `.app` (macOS) or `.exe` folder (Windows) via PyInstaller

## Requirements

- Python 3.11+
- VMware Horizon 2312 or later

## Running from source

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python horizon_network_editor.py
```

## Building a distributable

**macOS**
```bash
bash build_mac.sh
# output: dist/Horizon Network Editor.app
```

**Windows**
```bat
build_win.bat
REM output: dist\Horizon Network Editor\Horizon Network Editor.exe
```

## Configuration

On first run, go to the **Configuration** tab:

1. Enter your Horizon username, domain, and connection server
2. Set your password
3. Click **Test & Save** — the tool discovers all pods and connection servers automatically

## Usage

1. Click **Connect** on the VDI Pools or RDS Farms tab
2. Select a pool / farm from the dropdown
3. The current portgroup assignments are pre-selected in each NIC row
4. Check one or more portgroups (or leave **From Golden Image** to inherit from the base VM)
5. Optionally check **Enable provisioning after saving** and/or **Delete all machines after saving**
6. Click **Apply Network Changes**
