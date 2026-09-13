[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$PytestExecutable,

    [string[]]$PytestArguments = @('-q')
)

$ErrorActionPreference = 'Stop'

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runId = [guid]::NewGuid().ToString('N')
$baseTemp = Join-Path $projectRoot ".pytest-tmp-$runId"
$cacheDirectory = Join-Path $baseTemp '.pytest_cache'

$pytestItem = Get-Item -LiteralPath $PytestExecutable -ErrorAction Stop
if ($pytestItem.PSIsContainer) {
    throw "PytestExecutable must be a file: $PytestExecutable"
}

$effectiveArguments = @(
    $PytestArguments
    "--basetemp=$baseTemp"
    '-o'
    "cache_dir=$cacheDirectory"
)

Write-Output "PYTEST_EXECUTABLE=$($pytestItem.FullName)"
Write-Output "PYTEST_BASETEMP=$baseTemp"
Write-Output "PYTEST_CACHE_DIR=$cacheDirectory"

& $pytestItem.FullName @effectiveArguments
$exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
exit $exitCode
