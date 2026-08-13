[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $PSScriptRoot "..\..\var\backups")
)

. (Join-Path $PSScriptRoot "Common.ps1")

$python = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) {
    throw "The application virtual environment is missing."
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupRoot = Join-Path (Resolve-Path (New-Item -ItemType Directory -Force $Destination)) $timestamp
New-Item -ItemType Directory -Force $backupRoot | Out-Null

Set-Location $script:RepoRoot
$settingsCode = @"
import json
from printing_agent.config import get_settings

settings = get_settings()
print(json.dumps({
    "database": str(settings.database_url.resolve()),
    "artifacts": str(settings.artifact_dir.resolve()),
    "simulator": str(settings.simulator_spool_dir.resolve()),
}))
"@
$settingsJson = & $python -c $settingsCode
if ($LASTEXITCODE -ne 0) {
    throw "Failed to load effective application storage settings."
}
$settings = $settingsJson | ConvertFrom-Json
$manifest = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    database = $settings.database
    artifacts = $settings.artifacts
    simulator = $settings.simulator
}
[System.IO.File]::WriteAllText(
    (Join-Path $backupRoot "backup-manifest.json"),
    ($manifest | ConvertTo-Json),
    [System.Text.UTF8Encoding]::new($false)
)

$database = $settings.database
if (Test-Path $database -PathType Leaf) {
    $databaseBackup = Join-Path $backupRoot "printing-agent.db"
    $code = "import sqlite3,sys; src=sqlite3.connect(sys.argv[1]); dst=sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close(); src.close()"
    Invoke-Checked -Executable $python -Arguments @("-c", $code, $database, $databaseBackup)
}

foreach ($directory in @(
    @{ Name = "artifacts"; Path = $settings.artifacts },
    @{ Name = "simulator"; Path = $settings.simulator }
)) {
    $source = $directory.Path
    if (Test-Path $source -PathType Container) {
        Copy-Item $source (Join-Path $backupRoot $directory.Name) -Recurse
    }
}

$archive = "$backupRoot.zip"
Compress-Archive -Path (Join-Path $backupRoot "*") -DestinationPath $archive -Force
Remove-Item $backupRoot -Recurse -Force
Write-Host "Backup created at $archive"
