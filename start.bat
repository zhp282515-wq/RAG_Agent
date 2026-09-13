@echo off
REM Entry point for one-click start.
REM Real logic lives in start.py -- Windows batch reads UTF-8 Chinese as GBK and
REM would mangle it, so this file is kept ASCII-only on purpose.
REM Uses uv run when available (matches README), else falls back to system python.
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    python "%~dp0start.py"
) else (
    uv run python "%~dp0start.py"
)
