@echo off
setlocal

REM ===========================================================================
REM  start_openswmm.cmd - launch the OpenSWMM MCP server with API-key auth.
REM
REM  Prompts for the key and NEVER stores it: no config file, no setx, nothing
REM  written to disk. setlocal means the key leaves the environment when this
REM  script exits, so it does not linger in your shell after Ctrl+C.
REM
REM  Usage:
REM    start_openswmm.cmd              start on port 8080
REM    start_openswmm.cmd 8081         start on a different port
REM    start_openswmm.cmd /generate    print a new key, then exit
REM
REM  Run setup.cmd first on a new machine - it builds the environment this
REM  script launches and records where its interpreter lives.
REM
REM  See APIKEY_SETUP.md for the full workflow and the security trade-offs.
REM ===========================================================================

REM Pinned to System32 rather than trusting PATH - a trimmed or shadowed PATH
REM would otherwise make the port check silently useless or break the prompt.
set "NETSTAT=%SystemRoot%\System32\netstat.exe"
set "FINDSTR=%SystemRoot%\System32\findstr.exe"
set "PWSH=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

REM %~dp0 is this script's own folder, so BASE follows the file if it moves.
set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

REM --------------------------------------------------- the Python interpreter
REM Resolved rather than hardcoded, so this script runs on any machine that has
REM been through setup.cmd. Order:
REM   1. OPENSWMM_MCP_PYTHON  - explicit override, used for testing a second env
REM   2. .python_path         - written by setup.cmd, the normal path
REM   3. the usual conda locations, for an env named openswmm.mcp
set "PY=%OPENSWMM_MCP_PYTHON%"
if defined PY goto :py_resolved

if not exist "%BASE%\.python_path" goto :py_probe
set /p PY=<"%BASE%\.python_path"
set "PY=%PY:"=%"
if defined PY goto :py_resolved

:py_probe
for %%R in (
    "%USERPROFILE%\AppData\Local\miniconda3"
    "%USERPROFILE%\miniconda3"
    "%USERPROFILE%\AppData\Local\anaconda3"
    "%USERPROFILE%\anaconda3"
    "%ProgramData%\miniconda3"
    "%ProgramData%\anaconda3"
) do (
    if not defined PY if exist "%%~R\envs\openswmm.mcp\python.exe" set "PY=%%~R\envs\openswmm.mcp\python.exe"
)

:py_resolved

REM ---------------------------------------------------------------- preflight
if not defined PY (
    echo ERROR: no Python environment found.
    echo.
    echo Run setup.cmd first - it creates the environment and records the
    echo interpreter path in .python_path next to this script.
    exit /b 1
)
if not exist "%PY%" (
    echo ERROR: Python interpreter not found at:
    echo        %PY%
    echo.
    echo Re-run setup.cmd, or set OPENSWMM_MCP_PYTHON to the right python.exe.
    exit /b 1
)
if not exist "%BASE%\serve_apikey.py" (
    echo ERROR: serve_apikey.py not found in:
    echo        %BASE%
    exit /b 1
)
if not exist "%BASE%\pathjail.py" (
    echo ERROR: pathjail.py not found in:
    echo        %BASE%
    echo        serve_apikey.py imports it to confine file access. Without it the
    echo        server will not start. Re-extract the handoff zip.
    exit /b 1
)

REM ------------------------------------------------------------ generate mode
if /i "%~1"=="/generate" goto :generate
if /i "%~1"=="--generate" goto :generate
if /i "%~1"=="-g" goto :generate
goto :no_generate

:generate
echo Generating a new API key.
echo.
"%PY%" -c "import secrets; print(secrets.token_urlsafe(32))"
echo.
echo Copy it into your password manager NOW - it is not saved anywhere.
echo Do not paste it into chat, a commit, or a shared document.
exit /b 0

:no_generate

REM ----------------------------------------------------------------- the port
set "PORT=8080"
if not "%~1"=="" set "PORT=%~1"

"%NETSTAT%" -ano | "%FINDSTR%" /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
    echo ERROR: port %PORT% is already in use.
    echo.
    echo Something is listening there - possibly a server you forgot to stop.
    echo The last column is the process ID:
    echo.
    call :show_port
    echo.
    echo Stop it, or pass a different port:  start_openswmm.cmd 8081
    exit /b 1
)
goto :port_ok

REM A subroutine, because a pipe inside a parenthesised if-block is parsed as a
REM literal character and prints nothing.
:show_port
"%NETSTAT%" -ano | "%FINDSTR%" /r /c:":%PORT% .*LISTENING"
goto :eof

:port_ok

REM ------------------------------------------------------------- prompt - key
REM If the key is already in the environment, use it and skip the prompt. That
REM makes the script usable from automation, where the hidden prompt would hang
REM forever: Read-Host -AsSecureString reads console keystrokes and ignores
REM redirected stdin.
if defined OPENSWMM_MCP_API_KEY (
    echo Using OPENSWMM_MCP_API_KEY already set in this environment.
    goto :have_key
)

echo.
echo Paste the API key, then press Enter. Input is hidden.
echo   ^(No key yet? Run:  start_openswmm.cmd /generate^)
echo.

REM %PWSH% is deliberately unquoted: for /f mis-parses a command that begins
REM with a quoted path. The System32 PowerShell path contains no spaces, so
REM this is safe.
for /f "usebackq delims=" %%k in (`%PWSH% -NoProfile -Command "$s = Read-Host -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "OPENSWMM_MCP_API_KEY=%%k"

if not defined OPENSWMM_MCP_API_KEY (
    echo.
    echo ERROR: no key entered.
    exit /b 1
)

:have_key
REM Strip any quote characters. Pasting a quoted key is the classic cmd
REM mistake, and with hidden input you cannot see that you have done it.
set "OPENSWMM_MCP_API_KEY=%OPENSWMM_MCP_API_KEY:"=%"

REM serve_apikey.py enforces the 32-character minimum and reports it clearly,
REM so no length check is duplicated here.

REM ----------------------------------------------------------------- env + go
set "OPENSWMM_MCP_TRANSPORT=http"
set "OPENSWMM_MCP_HTTP_PORT=%PORT%"
set "OPENSWMM_MCP_WORKING_DIR=%BASE%\scratch"
set "OPENSWMM_MCP_ENABLE_PYTHON="

REM OPENSWMM_MCP_READ_ROOTS is deliberately NOT set here. It widens what the
REM server may read, so it should be a conscious per-session decision rather than
REM a default baked into the launcher. setlocal inherits the caller's
REM environment, so to grant extra read-only locations for one run:
REM
REM     set OPENSWMM_MCP_READ_ROOTS=C:\Projects;D:\Models
REM     start_openswmm.cmd
REM
REM Writes are always confined to %BASE%\scratch and this cannot be widened.

if not exist "%BASE%\scratch" mkdir "%BASE%\scratch"

echo.
echo Starting. Ctrl+C to stop.
echo If you do NOT see "auth ENABLED" below, stop and do not open the tunnel.
echo.

"%PY%" "%BASE%\serve_apikey.py"

REM endlocal is implicit at exit: the key and every OPENSWMM_MCP_* variable are
REM discarded here and never reach the calling shell.
endlocal
