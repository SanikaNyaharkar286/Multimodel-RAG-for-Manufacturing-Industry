@echo off
setlocal
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Create backend/.venv and install requirements.txt first.
    popd
    exit /b 1
)
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app --reload-dir auth_system --reload-dir scripts
set "backendExit=%errorlevel%"
popd
exit /b %backendExit%
