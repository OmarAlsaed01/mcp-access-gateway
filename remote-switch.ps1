# Kill switch for the gateway. Run from an ELEVATED PowerShell:
#   .\remote-switch.ps1 off      stop + disable tunnel, proxy, and server (nothing reachable)
#   .\remote-switch.ps1 on       re-enable and start everything
#   .\remote-switch.ps1 status   show what is running
param(
    [Parameter(Mandatory)][ValidateSet("off", "on", "status")][string]$mode,
    [string]$TaskFolder = "\MCP-Gateway\"
)

. "$PSScriptRoot\gateway-ports.ps1"
$tasks = "MCP Gateway Proxy", "MCP Gateway Server"

switch ($mode) {
    "off" {
        Stop-Service cloudflared -Force -ErrorAction SilentlyContinue
        Set-Service cloudflared -StartupType Disabled -ErrorAction SilentlyContinue
        foreach ($t in $tasks) {
            Disable-ScheduledTask -TaskPath $TaskFolder -TaskName $t -ErrorAction SilentlyContinue | Out-Null
            Stop-ScheduledTask -TaskPath $TaskFolder -TaskName $t -ErrorAction SilentlyContinue
        }
        # cmd.exe exits but its child processes can linger; kill anything still on the ports.
        Get-NetTCPConnection -LocalPort $GatewayPorts -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    }
    "on" {
        Set-Service cloudflared -StartupType Automatic
        Start-Service cloudflared
        foreach ($t in $tasks) {
            Enable-ScheduledTask -TaskPath $TaskFolder -TaskName $t | Out-Null
            Start-ScheduledTask -TaskPath $TaskFolder -TaskName $t
        }
    }
}

Get-Service cloudflared -ErrorAction SilentlyContinue | Format-Table Name, Status, StartType -AutoSize
Get-ScheduledTask -TaskPath $TaskFolder -ErrorAction SilentlyContinue | Format-Table TaskName, State -AutoSize
foreach ($port in $GatewayPorts) {
    $up = [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    Write-Host ("port {0}: {1}" -f $port, $(if ($up) { "listening" } else { "down" }))
}
