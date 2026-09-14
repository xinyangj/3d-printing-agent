. (Join-Path $PSScriptRoot "Common.ps1")

Initialize-DeploymentDirectories
Set-Location $script:RepoRoot

if (-not (Test-Path $script:CaddyfilePath -PathType Leaf)) {
    throw "Caddy runtime configuration is missing. Run Install-Deployment.ps1 first."
}

Update-ProcessPath
$caddy = Get-ExecutablePath -Name "caddy.exe" -Fallbacks @(
    "%LOCALAPPDATA%\Microsoft\WinGet\Links\caddy.exe",
    "%ProgramFiles%\Caddy\caddy.exe"
)
$logPath = Join-Path $script:LogDir "caddy.log"
Rotate-DeploymentLog -Path $logPath
$ErrorActionPreference = "Continue"
& $caddy run --config $script:CaddyfilePath --adapter caddyfile *>> $logPath
exit $LASTEXITCODE
