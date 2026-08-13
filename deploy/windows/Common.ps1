Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:RuntimeDir = Join-Path $script:RepoRoot "var\deployment"
$script:LogDir = Join-Path $script:RepoRoot "var\logs"
$script:CaddyfilePath = Join-Path $script:RuntimeDir "Caddyfile"
$script:AppTaskName = "3D Printing Agent"
$script:CaddyTaskName = "3D Printing Agent Proxy"

function Update-ProcessPath {
    $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machinePath;$userPath"
}

function Get-ExecutablePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [string[]]$Fallbacks = @()
    )

    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    foreach ($candidate in $Fallbacks) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if (Test-Path $expanded -PathType Leaf) {
            return (Resolve-Path $expanded).Path
        }
    }

    throw "Required executable '$Name' was not found."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [string[]]$Arguments = @()
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "'$Executable' exited with code $LASTEXITCODE."
    }
}

function Initialize-DeploymentDirectories {
    New-Item -ItemType Directory -Force -Path $script:RuntimeDir, $script:LogDir | Out-Null
}

function Rotate-DeploymentLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [long]$MaximumBytes = 10MB,
        [int]$Keep = 5
    )

    if (-not (Test-Path $Path -PathType Leaf)) {
        return
    }
    if ((Get-Item $Path).Length -lt $MaximumBytes) {
        return
    }

    Remove-Item "$Path.$Keep" -Force -ErrorAction SilentlyContinue
    for ($index = $Keep - 1; $index -ge 1; $index--) {
        $source = "$Path.$index"
        if (Test-Path $source -PathType Leaf) {
            Move-Item $source "$Path.$($index + 1)" -Force
        }
    }
    Move-Item $Path "$Path.1" -Force
}

function Protect-DeploymentFile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /inheritance:r /grant:r "${identity}:(F)" "SYSTEM:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to restrict permissions on '$Path'."
    }
}

function Get-TaskPrincipalId {
    return [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
}

function Stop-DeploymentProcesses {
    $expectedByPort = @{
        8000 = (Join-Path $script:RepoRoot ".venv\Scripts\printing-agent-api.exe")
        8080 = $script:CaddyfilePath
    }

    foreach ($port in $expectedByPort.Keys) {
        $connections = @(
            Get-NetTCPConnection `
                -LocalPort $port `
                -State Listen `
                -ErrorAction SilentlyContinue
        )
        foreach ($connection in $connections) {
            $process = Get-CimInstance `
                -ClassName Win32_Process `
                -Filter "ProcessId = $($connection.OwningProcess)"
            $expectedCommand = $expectedByPort[$port]
            if (
                $null -eq $process -or
                [string]::IsNullOrEmpty($process.CommandLine) -or
                $process.CommandLine.IndexOf(
                    $expectedCommand,
                    [StringComparison]::OrdinalIgnoreCase
                ) -lt 0
            ) {
                throw "Port $port is owned by a process outside this deployment."
            }
            Stop-Process -Id $connection.OwningProcess -Force
        }
    }
}
