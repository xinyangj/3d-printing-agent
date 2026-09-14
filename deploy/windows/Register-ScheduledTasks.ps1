[CmdletBinding()]
param(
    [switch]$Start
)

. (Join-Path $PSScriptRoot "Common.ps1")

Initialize-DeploymentDirectories
$principalId = Get-TaskPrincipalId
$principal = New-ScheduledTaskPrincipal `
    -UserId $principalId `
    -LogonType Interactive `
    -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $principalId
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

if ($Start) {
    foreach ($name in @($script:CaddyTaskName, $script:AppTaskName)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $existing -and $existing.State -eq "Running") {
            Stop-ScheduledTask -TaskName $name
        }
    }
    Stop-DeploymentProcesses
}

function Register-DeploymentTask {
    param(
        [Parameter(Mandatory = $true)]
        [string]$TaskName,
        [Parameter(Mandatory = $true)]
        [string]$ScriptPath,
        [Parameter(Mandatory = $true)]
        [string]$Description
    )

    $arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument $arguments `
        -WorkingDirectory $script:RepoRoot
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force | Out-Null
}

Register-DeploymentTask `
    -TaskName $script:AppTaskName `
    -ScriptPath (Join-Path $PSScriptRoot "Start-PrintingAgent.ps1") `
    -Description "Runs the loopback-only 3D Printing Agent API and durable worker."
Register-DeploymentTask `
    -TaskName $script:CaddyTaskName `
    -ScriptPath (Join-Path $PSScriptRoot "Start-Caddy.ps1") `
    -Description "Runs the loopback-only authenticated reverse proxy for the 3D Printing Agent."

if ($Start) {
    Start-ScheduledTask -TaskName $script:AppTaskName
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $script:CaddyTaskName
}

Write-Host "Registered scheduled tasks '$($script:AppTaskName)' and '$($script:CaddyTaskName)'."
