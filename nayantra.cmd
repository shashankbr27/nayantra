@echo off
rem nayantra.cmd - run nayantra.sh from cmd.exe or PowerShell (needs Git for Windows).
rem   nayantra setup [--sim auto|gazebo|isaac] | detect | start | stop | restart | status | logs | doctor
setlocal
set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%GITBASH%" set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not exist "%GITBASH%" (
  echo Git for Windows was not found. Install it, or run inside WSL:  wsl ./nayantra.sh %*
  exit /b 1
)
"%GITBASH%" "%~dp0nayantra.sh" %*
exit /b %errorlevel%
