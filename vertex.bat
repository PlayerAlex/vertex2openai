@echo off
setlocal
set "VERTEX_SCRIPT=%~dp0windows\vertex.ps1"
if not exist "%VERTEX_SCRIPT%" (
  echo [ERROR] Missing windows\vertex.ps1. Please extract the complete ZIP first.
  pause
  exit /b 1
)
if "%~1"=="" (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VERTEX_SCRIPT%" run
) else (
  powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%VERTEX_SCRIPT%" %*
)
set "VERTEX_EXIT=%ERRORLEVEL%"
if not "%VERTEX_EXIT%"=="0" (
  echo.
  echo The operation failed. Read the error above, then press any key to close.
  pause >nul
)
exit /b %VERTEX_EXIT%
