. (Join-Path $PSScriptRoot "Common.ps1")

Initialize-DeploymentDirectories
Set-Location $script:RepoRoot

$executable = Join-Path $script:RepoRoot ".venv\Scripts\printing-agent-api.exe"
if (-not (Test-Path $executable -PathType Leaf)) {
    throw "The application is not installed. Run Install-Deployment.ps1 first."
}

$logPath = Join-Path $script:LogDir "printing-agent.log"
Rotate-DeploymentLog -Path $logPath
$ErrorActionPreference = "Continue"
& $executable *>> $logPath
exit $LASTEXITCODE
