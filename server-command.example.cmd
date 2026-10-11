@echo off
rem Copy to server-command.cmd and replace the line below with the command that
rem starts YOUR MCP server over streamable HTTP. It must:
rem   - listen on 127.0.0.1 only (never 0.0.0.0), and
rem   - use the port in proxy-config.json's "upstream" (default 8484).
rem Use full paths: scheduled tasks don't always see your interactive PATH.
rem Examples:
rem   "%USERPROFILE%\.local\bin\uvx.exe" some-mcp-server --transport http --host 127.0.0.1 --port 8484
rem   "C:\Program Files\nodejs\npx.cmd" -y some-mcp-server --port 8484
echo edit server-command.cmd to start your MCP server 1>&2
exit /b 1
