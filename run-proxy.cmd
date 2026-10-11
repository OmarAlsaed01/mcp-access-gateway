@echo off
rem Cloudflare Access JWT check in front of the local MCP server.
rem Started by the "MCP Gateway Proxy" scheduled task (see install-tasks.ps1).
rem Override the uv location with GATEWAY_UV if it isn't in the default place or on PATH.
setlocal
set PYTHONUTF8=1
set "LOGDIR=%~dp0logs"
set "LOG=%LOGDIR%\proxy-console.log"
if not defined GATEWAY_UV set "GATEWAY_UV=%USERPROFILE%\.local\bin\uv.exe"
if not exist "%GATEWAY_UV%" set "GATEWAY_UV=uv"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
for %%F in ("%LOG%") do if %%~zF GTR 5000000 move /y "%LOG%" "%LOG%.old" >nul
echo [%date% %time%] starting >> "%LOG%"
"%GATEWAY_UV%" run --quiet --script "%~dp0access_proxy.py" >> "%LOG%" 2>&1
