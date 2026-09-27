@echo off
rem ---------------------------------------------------------------------------
rem Launcher for the MotionWorks IEC MCP server.
rem
rem Why this exists: a venv's python.exe is an ordinary executable that reads
rem PYTHONHOME and PYTHONPATH from the environment. If a host application sets
rem either to a path that is not a valid Python home, the interpreter dies during
rem startup — before any of this project's code runs — and the MCP client just
rem reports a server that will not connect. Some agent harnesses set PYTHONHOME
rem for their own bundled Python, which is exactly that situation.
rem
rem Clearing both here makes the server independent of how it was launched, so an
rem MCP client can point at this file with no environment configuration at all.
rem
rem Usage as an MCP server command:
rem     scripts\motionworks-iec-mcp-server.cmd
rem Stdio transport takes no arguments, so leave the client's Arguments field empty.
rem
rem Note on style: this deliberately avoids `if ... ( ... )` blocks. Inside a
rem parenthesised block, %ERRORLEVEL% is expanded when the block is *parsed*, not
rem when it runs, which silently reports a stale status and hides a crashed server
rem from the client. Labels and goto have no such trap.
rem ---------------------------------------------------------------------------

set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="

set "HERE=%~dp0"
set "EXE=%HERE%..\.venv\Scripts\motionworks-iec-mcp-server.exe"

if not exist "%EXE%" goto no_venv

"%EXE%" %*
exit /b %ERRORLEVEL%

:no_venv
rem No virtual environment beside the checkout: fall back to whatever is on PATH.
where motionworks-iec-mcp-server.exe >nul 2>nul
if not %ERRORLEVEL% EQU 0 goto not_found

motionworks-iec-mcp-server.exe %*
exit /b %ERRORLEVEL%

:not_found
echo motionworks-iec-mcp-server: cannot find the server executable. 1>&2
echo Expected a virtual environment at: %HERE%..\.venv 1>&2
echo Create one with:  python -m venv .venv ^&^& .venv\Scripts\python -m pip install -e . 1>&2
exit /b 1
