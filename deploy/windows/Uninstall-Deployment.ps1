[CmdletBinding()]
param(
    [switch]$ResetFunnel
)

. (Join-Path $PSScriptRoot "Common.ps1")

foreach ($name in @($script:CaddyTaskName, $script:AppTaskName)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
}
Stop-DeploymentProcesses

if ($ResetFunnel) {
    Update-ProcessPath
    $tailscale = Get-ExecutablePath -Name "tailscale.exe" -Fallbacks @(
        "%ProgramFiles%\Tailscale\tailscale.exe"
    )
    Invoke-Checked -Executable $tailscale -Arguments @("funnel", "reset")
}

Write-Host "Scheduled tasks removed. Application data, secrets, dependencies, and backups were preserved."
