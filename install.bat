@echo off
REM ============================================================
REM  OpenCode Limit Monitor - installer
REM  Installs the monitor, creates a Desktop shortcut and
REM  (optionally) an autostart entry. No dependencies beyond
REM  Python 3 + tkinter (standard).
REM ============================================================
setlocal enabledelayedexpansion
chcp 65001 >nul
title OpenCode Limit Monitor - install

set "APPNAME=OpenCode Limit Monitor"
set "INSTALLDIR=%LOCALAPPDATA%\OpenCodeLimitMonitor"
set "SCRIPT=opencode_limit_monitor.py"

echo.
echo  == %APPNAME% installer ==
echo.

REM ---- 1. Locate Python ------------------------------------------------
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python*") do (
        if exist "%%D\python.exe" set "PY=%%D\python.exe"
    )
)
if not defined PY (
    echo  [!] Python not found. Install Python 3 from https://python.org
    echo      and tick "Add to PATH", then run this installer again.
    echo.
    pause
    exit /b 1
)
echo  [+] Python: %PY%

REM ---- 2. Check tkinter ------------------------------------------------
%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo  [!] tkinter is missing. Reinstall Python and enable tcl/tk.
    pause
    exit /b 1
)
echo  [+] tkinter OK

REM ---- 3. Copy files ---------------------------------------------------
if not exist "%~dp0%SCRIPT%" (
    echo  [!] %SCRIPT% not found next to this installer.
    pause
    exit /b 1
)
if not exist "%INSTALLDIR%" mkdir "%INSTALLDIR%"
copy /y "%~dp0%SCRIPT%" "%INSTALLDIR%\%SCRIPT%" >nul
echo  [+] Installed to %INSTALLDIR%

REM ---- 4. Desktop shortcut --------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop');" ^
  "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(\"$d\%APPNAME%.lnk\");" ^
  "$s.TargetPath='%PY%';" ^
  "$s.Arguments='\"%INSTALLDIR%\%SCRIPT%\"';" ^
  "$s.WorkingDirectory='%INSTALLDIR%';" ^
  "$s.Description='%APPNAME%';" ^
  "$s.Save()"
echo  [+] Desktop shortcut created

REM ---- 5. Optional autostart -------------------------------------------
echo.
set /p "AUTOSTART=  Add to startup (start with Windows)? [y/N] "
if /i "%AUTOSTART%"=="y" (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$p='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run';" ^
      "Set-ItemProperty -Path $p -Name '%APPNAME%' -Value ('\"%PY%\" \"%INSTALLDIR%\%SCRIPT%\"')"
    echo  [+] Added to startup
) else (
    echo  [-] Skipped autostart
)

echo.
echo  Done. Launch "%APPNAME%" from the Desktop.
echo  Press any key to exit.
pause >nul
endlocal
