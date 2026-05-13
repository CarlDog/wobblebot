# One-time setup: point this repo's git hooks at .githooks/ instead of
# the default .git/hooks/. Run once per fresh clone.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
git config core.hooksPath .githooks
Write-Host "core.hooksPath set to .githooks"
