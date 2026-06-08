@echo off
REM Build the Horizon Network Editor .exe for Windows.
REM Run from the project root directory.

SET VENV=.venv
SET SPEC=Horizon Network Editor.spec
SET DIST=dist

echo === Horizon Network Editor - Windows build ===

REM Create venv if it doesn't exist
IF NOT EXIST "%VENV%\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv %VENV%
)

echo Installing / upgrading dependencies...
%VENV%\Scripts\pip.exe install --upgrade pip --quiet
%VENV%\Scripts\pip.exe install -r requirements-dev.txt --quiet

echo Running PyInstaller...
%VENV%\Scripts\pyinstaller.exe "%SPEC%" --clean --noconfirm

echo.
echo Done. Output: %DIST%\Horizon Network Editor\
echo To run:  %DIST%\Horizon Network Editor\Horizon Network Editor.exe
pause
