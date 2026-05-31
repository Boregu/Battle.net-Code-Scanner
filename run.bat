@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
  echo Creating virtual environment...
  python -m venv venv
  call venv\Scripts\activate.bat
  pip install -r requirements.txt
  playwright install chromium
) else (
  call venv\Scripts\activate.bat
)

echo Starting at http://127.0.0.1:8787  ^(catalog^)  and  http://127.0.0.1:8787/scanner
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8787" ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
