@echo off
setlocal DisableDelayedExpansion

rem Single Windows entry point. PowerShell handles the per-user DPAPI token,
rem dependency bootstrap, and application launch without printing the token.
set "POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%POWERSHELL%" (
  echo ERROR: Windows PowerShell is required.
  exit /b 1
)

"%POWERSHELL%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
