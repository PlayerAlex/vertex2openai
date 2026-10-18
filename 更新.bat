@echo off
call "%~dp0vertex.bat" update
if errorlevel 1 exit /b %errorlevel%
call "%~dp0vertex.bat" run
