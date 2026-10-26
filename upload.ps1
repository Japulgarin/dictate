# Uploads local changes: stage everything (.gitignore still applies), commit, push the current branch.
# Usage: upload.bat "short description of the change"
# Refuses to run on main so a push never lands there by accident.
param([string]$Message = "")

Set-Location $PSScriptRoot

$branch = git branch --show-current
if ($branch -eq "main") {
    Write-Host "You are on main. Switch to a branch first: git switch -c <name>"
    exit 1
}

git add -A
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Host "Nothing to upload."
    exit 0
}

if (-not $Message) { $Message = "Update " + (Get-Date -Format "yyyy-MM-dd HH:mm") }
$msgFile = Join-Path $env:TEMP "dictate-commit-msg.txt"
Set-Content -Path $msgFile -Encoding utf8 -Value "$Message`n`nCo-Authored-By: Claude Haiku 5.5 <noreply@anthropic.com>"
git commit -q -F $msgFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

git push -u origin $branch
exit $LASTEXITCODE
