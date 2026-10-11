@echo off
rem Launches the upstream MCP server defined in server-command.cmd (copy it from
rem server-command.example.cmd). Started by the "MCP Gateway Server" scheduled task.
setlocal
set PYTHONUTF8=1
set "LOGDIR=%~dp0logs"
set "LOG=%LOGDIR%\server.log"
if not exist "%~dp0server-command.cmd" (
    echo refusing to start: server-command.cmd is missing, copy server-command.example.cmd 1>&2
    exit /b 1
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for %%F in ("%LOG%") do if %%~zF GTR 5000000 move /y "%LOG%" "%LOG%.old" >nul
echo [%date% %time%] starting >> "%LOG%"
call "%~dp0server-command.cmd" >> "%LOG%" 2>&1
