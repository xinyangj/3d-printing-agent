. (Join-Path $PSScriptRoot "Common.ps1")

Write-Host "Scheduled tasks"
foreach ($name in @($script:AppTaskName, $script:CaddyTaskName)) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "  $name`: not registered"
    } else {
        $info = Get-ScheduledTaskInfo -TaskName $name
        Write-Host "  $name`: $($task.State), last result $($info.LastTaskResult)"
    }
}

Write-Host ""
Write-Host "Local endpoints"
try {
    $health = Invoke-RestMethod "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 5
    Write-Host "  FastAPI: $($health.status)"
} catch {
    Write-Host "  FastAPI: unavailable ($($_.Exception.Message))"
}

try {
    Invoke-WebRequest "http://127.0.0.1:8080/api/v1/health" -TimeoutSec 5 | Out-Null
    Write-Host "  Caddy authentication: unexpected unauthenticated success"
} catch {
    $responseProperty = $_.Exception.PSObject.Properties["Response"]
    $statusCode = $null
    if ($null -ne $responseProperty -and $null -ne $responseProperty.Value) {
        $statusCode = [int]$responseProperty.Value.StatusCode
    }
    if ($statusCode -eq 401) {
        Write-Host "  Caddy authentication: enabled (401 without credentials)"
    } elseif ($null -eq $statusCode) {
        Write-Host "  Caddy authentication: unavailable ($($_.Exception.Message))"
    } else {
        Write-Host "  Caddy authentication: unavailable (HTTP $statusCode)"
    }
}

Write-Host ""
Write-Host "Tailscale Funnel"
Update-ProcessPath
$tailscale = Get-Command "tailscale.exe" -ErrorAction SilentlyContinue
if ($null -eq $tailscale) {
    Write-Host "  Tailscale CLI: not installed"
} else {
    & $tailscale.Source funnel status
}
