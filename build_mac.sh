#!/usr/bin/env bash
# Build the Horizon Network Editor .app for macOS.
# Run from the project root directory.
set -euo pipefail

VENV=".venv"
SPEC="Horizon Network Editor-mac.spec"
DIST="dist"

echo "=== Horizon Network Editor — macOS build ==="

# Create venv if it doesn't exist
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
fi

echo "Installing / upgrading dependencies..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r requirements-dev.txt --quiet

echo "Running PyInstaller..."
"$VENV/bin/pyinstaller" "$SPEC" --clean --noconfirm

echo ""
echo "Done. Output: $DIST/Horizon Network Editor.app"
echo "To run:  open \"$DIST/Horizon Network Editor.app\""
