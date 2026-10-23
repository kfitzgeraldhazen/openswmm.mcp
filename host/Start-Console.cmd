@echo off
setlocal

REM ===========================================================================
REM  Start-Console.cmd - launch the OpenSWMM MCP Host Console.
REM
REM  The console is a WPF window, so PowerShell must be in a single-threaded
REM  apartment (-STA). Windows PowerShell 5.1 defaults to STA when launched this
REM  way, but passing it explicitly means the script cannot be started into the
REM  wrong apartment by a shortcut someone edits later.
REM
REM  -WindowStyle Hidden hides the PowerShell console itself: the two output
REM  panes in the window are the only consoles you need to look at.
REM
REM  This starts nothing on its own. Nothing is launched, and no key is handled,
REM  until you click in the window.
REM ===========================================================================

set "PWSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%PWSH%" (
    echo ERROR: Windows PowerShell not found at:
    echo        %PWSH%
    exit /b 1
)
if not exist "%~dp0OpenSWMM-Host-Console.ps1" (
    echo ERROR: OpenSWMM-Host-Console.ps1 not found in:
    echo        %~dp0
    exit /b 1
)

REM -ExecutionPolicy Bypass applies to this process only - it changes no machine
REM or user policy, and nothing persists after the window closes.
start "" "%PWSH%" -STA -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden ^
    -File "%~dp0OpenSWMM-Host-Console.ps1"

endlocal
