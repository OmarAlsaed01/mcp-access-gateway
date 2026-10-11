# Dot-sourced by install-tasks.ps1 and remote-switch.ps1. Sets $GatewayPorts to
# @(<upstream port>, <proxy listen port>), read from proxy-config.json with the
# same defaults and env overrides as access_proxy.py.
$config = @{}
$configPath = if ($env:GATEWAY_CONFIG) { $env:GATEWAY_CONFIG } else { Join-Path $PSScriptRoot "proxy-config.json" }
if (Test-Path $configPath) {
    (Get-Content $configPath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $config[$_.Name] = $_.Value }
}
$upstream = if ($env:GATEWAY_UPSTREAM) { $env:GATEWAY_UPSTREAM } elseif ($config.upstream) { $config.upstream } else { "http://127.0.0.1:8484" }
$listen = if ($env:GATEWAY_LISTEN_PORT) { $env:GATEWAY_LISTEN_PORT } elseif ($config.listen_port) { $config.listen_port } else { 8485 }
$GatewayPorts = @(([Uri]$upstream).Port, [int]$listen)
