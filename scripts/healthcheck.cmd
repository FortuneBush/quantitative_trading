@echo off
setlocal
rem Windows-friendly wrapper; the probe itself uses only Python's standard library.
set "PROJECT_ROOT=%~dp0.."
set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo ERROR: virtual environment not found. Run scripts\start.cmd first.
  exit /b 1
)
"%PYTHON%" "%~dp0healthcheck.py" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
