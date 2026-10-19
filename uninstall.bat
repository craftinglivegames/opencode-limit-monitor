@echo off
REM ============================================================
REM  OpenCode Limit Monitor - uninstaller
REM ============================================================
setlocal
chcp 65001 >nul
title OpenCode Limit Monitor - uninstall

set "APPNAME=OpenCode Limit Monitor"
set "INSTALLDIR=%LOCALAPPDATA%\OpenCodeLimitMonitor"

echo.
echo  == %APPNAME% uninstaller ==
echo.

REM remove autostart entry
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run';" ^
  "Remove-ItemProperty -Path $p -Name '%APPNAME%' -ErrorAction SilentlyContinue"
echo  [+] Autostart entry removed (if present)

REM remove desktop shortcut
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$d=[Environment]::GetFolderPath('Desktop');" ^
  "Remove-Item (\"$d\%APPNAME%.lnk\") -Force -ErrorAction SilentlyContinue"
echo  [+] Desktop shortcut removed (if present)

REM remove install dir (keeps your config/log if you want: comment the rmdir)
if exist "%INSTALLDIR%" (
    rmdir /s /q "%INSTALLDIR%"
    echo  [+] Installed files removed
)

echo.
echo  Uninstalled. Your config (limit_monitor_config.json) and log, if you
echo  ran the script from another folder, are left untouched.
echo  Press any key to exit.
pause >nul
endlocal
