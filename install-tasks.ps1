# Registers the two boot-time scheduled tasks: the upstream MCP server and the auth proxy.
# Run from an ELEVATED PowerShell:  powershell -ExecutionPolicy Bypass -File .\install-tasks.ps1
# Both tasks run as you via S4U ("run whether user is logged on or not", no stored
# password), start at boot, restart on failure, and re-check every 5 minutes.
param([string]$TaskFolder = "\MCP-Gateway\")

$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this from an elevated (Run as administrator) PowerShell." }

$dir = $PSScriptRoot
foreach ($required in "proxy-config.json", "server-command.cmd") {
    if (-not (Test-Path "$dir\$required")) { throw "$required is missing; copy it from the matching *.example file first." }
}
. "$dir\gateway-ports.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$atBoot = New-ScheduledTaskTrigger -AtStartup
# Watchdog: if the process died and restarts ran out, this starts it again (IgnoreNew skips it while running).
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)

$tasks = [ordered]@{
    "MCP Gateway Server" = "$dir\run-server.cmd"
    "MCP Gateway Proxy"  = "$dir\run-proxy.cmd"
}
foreach ($name in $tasks.Keys) {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$($tasks[$name])`"" -WorkingDirectory $dir
    Register-ScheduledTask -TaskName $name -TaskPath $TaskFolder -Action $action -Principal $principal `
        -Trigger @($atBoot, $watchdog) -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskPath $TaskFolder -TaskName $name
    Write-Host "registered and started: $name"
}

Start-Sleep -Seconds 20
foreach ($port in $GatewayPorts) {
    $up = [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    Write-Host ("port {0}: {1}" -f $port, $(if ($up) { "listening" } else { "NOT listening yet (check logs\)" }))
}
