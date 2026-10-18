@echo off
setlocal
set "VERTEX_SCRIPT=%~dp0windows\vertex.ps1"
if defined VERTEX_INSTALL_DIR (
  set "VERTEX_MANAGER_ROOT=%VERTEX_INSTALL_DIR%\manager"
) else (
  set "VERTEX_MANAGER_ROOT=%LOCALAPPDATA%\QuickVertexAPI\manager"
)
if exist "%VERTEX_MANAGER_ROOT%\windows\vertex.ps1" set "VERTEX_SCRIPT=%VERTEX_MANAGER_ROOT%\windows\vertex.ps1"
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
