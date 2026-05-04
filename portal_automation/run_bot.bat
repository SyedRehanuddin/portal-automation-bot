@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m portal_automation.telegram_bot --config config.json
) else (
  python -m portal_automation.telegram_bot --config config.json
)
endlocal
