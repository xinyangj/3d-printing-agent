[CmdletBinding()]
param(
    [string]$AuthUsername = "printing-agent",
    [switch]$EnableLanAccess,
    [switch]$SkipBambuTools,
    [switch]$SkipPackageInstall,
    [switch]$SkipScheduledTasks,
    [switch]$SkipFunnel
)

. (Join-Path $PSScriptRoot "Common.ps1")

function Install-WinGetPackage {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Id,
        [Parameter(Mandatory = $true)]
        [string]$CommandName,
        [string[]]$Fallbacks = @()
    )

    if ($null -ne (Get-Command $CommandName -ErrorAction SilentlyContinue)) {
        return
    }
    foreach ($candidate in $Fallbacks) {
        $expanded = [Environment]::ExpandEnvironmentVariables($candidate)
        if (Test-Path $expanded -PathType Leaf) {
            return
        }
    }

    $winget = Get-ExecutablePath -Name "winget.exe"
    Invoke-Checked -Executable $winget -Arguments @(
        "install",
        "--id", $Id,
        "--exact",
        "--silent",
        "--disable-interactivity",
        "--accept-source-agreements",
        "--accept-package-agreements"
    )
    Update-ProcessPath
}

function Set-EnvironmentEntry {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Lines,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $prefix = "$Name="
    $result = [System.Collections.Generic.List[string]]::new()
    $found = $false
    foreach ($line in $Lines) {
        if ($line.StartsWith($prefix, [StringComparison]::Ordinal)) {
            $result.Add("$prefix$Value")
            $found = $true
        } else {
            $result.Add($line)
        }
    }
    if (-not $found) {
        $result.Add("$prefix$Value")
    }
    return $result.ToArray()
}

if ($AuthUsername -notmatch "^[A-Za-z0-9._-]+$") {
    throw "AuthUsername may contain only letters, digits, periods, underscores, and hyphens."
}

Initialize-DeploymentDirectories
Set-Location $script:RepoRoot

if (-not $SkipPackageInstall) {
    Install-WinGetPackage -Id "OpenJS.NodeJS.LTS" -CommandName "node.exe"
    Install-WinGetPackage -Id "OpenSCAD.OpenSCAD" -CommandName "openscad.exe"
    Install-WinGetPackage -Id "Tailscale.Tailscale" -CommandName "tailscale.exe"
    Install-WinGetPackage -Id "CaddyServer.Caddy" -CommandName "caddy.exe"
    if (-not $SkipBambuTools) {
        Install-WinGetPackage `
            -Id "Bambulab.Bambustudio" `
            -CommandName "bambu-studio.exe" `
            -Fallbacks @(
                "%ProgramFiles%\Bambu Studio\bambu-studio.exe",
                "%LOCALAPPDATA%\Programs\Bambu Studio\bambu-studio.exe"
            )
    }
}

Update-ProcessPath
$pythonLauncher = Get-ExecutablePath -Name "py.exe" -Fallbacks @("%WINDIR%\py.exe")
$npm = Get-ExecutablePath -Name "npm.cmd" -Fallbacks @("%ProgramFiles%\nodejs\npm.cmd")
$openscad = Get-ExecutablePath -Name "openscad.exe" -Fallbacks @(
    "%ProgramFiles%\OpenSCAD\openscad.exe"
)
$caddy = Get-ExecutablePath -Name "caddy.exe" -Fallbacks @(
    "%LOCALAPPDATA%\Microsoft\WinGet\Links\caddy.exe",
    "%ProgramFiles%\Caddy\caddy.exe"
)
$bambuStudio = $null
if (-not $SkipBambuTools) {
    $bambuStudio = Get-ExecutablePath -Name "bambu-studio.exe" -Fallbacks @(
        "%ProgramFiles%\Bambu Studio\bambu-studio.exe",
        "%LOCALAPPDATA%\Programs\Bambu Studio\bambu-studio.exe"
    )
}

if (-not (Test-Path ".venv\Scripts\python.exe" -PathType Leaf)) {
    Invoke-Checked -Executable $pythonLauncher -Arguments @(
        "-3",
        "-c",
        "import sys; assert sys.version_info >= (3, 11), 'Python 3.11 or newer is required'"
    )
    Invoke-Checked -Executable $pythonLauncher -Arguments @("-3", "-m", "venv", ".venv")
}
$python = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
Invoke-Checked -Executable $python -Arguments @("-m", "pip", "install", "-e", ".[dev]")
Invoke-Checked -Executable $python -Arguments @("-m", "copilot", "download-runtime")

Push-Location (Join-Path $script:RepoRoot "web")
try {
    Invoke-Checked -Executable $npm -Arguments @("ci")
    Invoke-Checked -Executable $npm -Arguments @("run", "build")
} finally {
    Pop-Location
}

$envPath = Join-Path $script:RepoRoot ".env"
if (-not (Test-Path $envPath -PathType Leaf)) {
    Copy-Item (Join-Path $script:RepoRoot ".env.example") $envPath
}
$envLines = @(Get-Content $envPath)
$envLines = Set-EnvironmentEntry `
    -Lines $envLines `
    -Name "PRINTING_AGENT_OPENSCAD_PATH" `
    -Value $openscad
if ($null -ne $bambuStudio) {
    $envLines = Set-EnvironmentEntry `
        -Lines $envLines `
        -Name "PRINTING_AGENT_BAMBU_STUDIO_PATH" `
        -Value $bambuStudio
    $resourceRoot = Join-Path (Split-Path $bambuStudio) "resources\profiles"
    if (Test-Path $resourceRoot -PathType Container) {
        $envLines = Set-EnvironmentEntry `
            -Lines $envLines `
            -Name "PRINTING_AGENT_BAMBU_STUDIO_RESOURCE_DIR" `
            -Value $resourceRoot
    }
}
if ($EnableLanAccess) {
    $envLines = Set-EnvironmentEntry `
        -Lines $envLines `
        -Name "PRINTING_AGENT_API_HOST" `
        -Value "0.0.0.0"
    $existingRule = Get-NetFirewallRule `
        -DisplayName $script:LanFirewallRuleName `
        -ErrorAction SilentlyContinue
    if ($null -eq $existingRule) {
        New-NetFirewallRule `
            -DisplayName $script:LanFirewallRuleName `
            -Direction Inbound `
            -Action Allow `
            -Protocol TCP `
            -LocalPort 8000 `
            -Profile Private | Out-Null
    }
}
$configuredAdminToken = $envLines |
    Where-Object { $_.StartsWith("PRINTING_AGENT_ADMIN_API_TOKEN=") } |
    ForEach-Object { $_.Substring("PRINTING_AGENT_ADMIN_API_TOKEN=".Length).Trim() } |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) -and $_.Length -ge 32 } |
    Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($configuredAdminToken)) {
    $tokenBytes = [byte[]]::new(32)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($tokenBytes)
    } finally {
        $rng.Dispose()
    }
    $adminToken = ([BitConverter]::ToString($tokenBytes) -replace "-", "").ToLowerInvariant()
    $envLines = Set-EnvironmentEntry `
        -Lines $envLines `
        -Name "PRINTING_AGENT_ADMIN_API_TOKEN" `
        -Value $adminToken
    Write-Host "Generated PRINTING_AGENT_ADMIN_API_TOKEN in the protected .env file."
}
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($envPath, $envLines, $utf8WithoutBom)
Protect-DeploymentFile -Path $envPath

Write-Host "Caddy will prompt twice for the public web password."
$passwordHash = (& $caddy hash-password).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($passwordHash)) {
    throw "Caddy did not generate a password hash."
}
$template = Get-Content (Join-Path $PSScriptRoot "Caddyfile.template") -Raw
$runtimeConfig = $template.
    Replace("__AUTH_USERNAME__", $AuthUsername).
    Replace("__AUTH_PASSWORD_HASH__", $passwordHash)
[System.IO.File]::WriteAllText($script:CaddyfilePath, $runtimeConfig, $utf8WithoutBom)
Protect-DeploymentFile -Path $script:CaddyfilePath
Invoke-Checked -Executable $caddy -Arguments @(
    "validate",
    "--config", $script:CaddyfilePath,
    "--adapter", "caddyfile"
)

if (-not $SkipScheduledTasks) {
    & (Join-Path $PSScriptRoot "Register-ScheduledTasks.ps1") -Start
}

if (-not $SkipFunnel) {
    $tailscale = Get-ExecutablePath -Name "tailscale.exe" -Fallbacks @(
        "%ProgramFiles%\Tailscale\tailscale.exe"
    )
    Invoke-Checked -Executable $tailscale -Arguments @(
        "funnel",
        "--bg",
        "--https=443",
        "http://127.0.0.1:8080"
    )
}

Write-Host ""
Write-Host "Deployment installation completed."
Write-Host "Set PRINTING_AGENT_THINGIVERSE_TOKEN in .env before creating live workflows."
Write-Host "Run Get-DeploymentStatus.ps1 to inspect the services and Funnel."
if (-not $SkipBambuTools) {
    Write-Host "Install Bambu Connect from the official Bambu Lab Wiki, log in, and bind the H2D."
}
if ($EnableLanAccess) {
    Write-Host "LAN access is enabled on TCP port 8000 for Private network profiles."
}
