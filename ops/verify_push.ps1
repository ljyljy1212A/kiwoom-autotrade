param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{40}$")]
    [string]$ExpectedHash
)

$repoRoot = $PSScriptRoot | Split-Path -Parent
$failures = @()

function Invoke-Git {
    param(
        [string[]]$Arguments,
        [string]$Label
    )

    Write-Output "=== $Label ==="
    $output = @(& git @Arguments 2>&1)
    $exitCode = $LASTEXITCODE

    foreach ($line in $output) {
        Write-Output $line
    }

    return [pscustomobject]@{
        Output   = $output
        ExitCode = $exitCode
    }
}

Push-Location $repoRoot
try {
    # Signal 1: remote ref via ls-remote (no push, read-only network call)
    $remote = Invoke-Git -Arguments @("ls-remote", "origin", "refs/heads/master") -Label "git ls-remote origin"
    $remoteLine = $remote.Output |
        Where-Object { "$_".Trim() -match "^[0-9a-fA-F]{40}\s+refs/heads/master$" } |
        Select-Object -First 1

    if ($remote.ExitCode -ne 0) {
        $failures += "git ls-remote failed with exit code $($remote.ExitCode)"
    } elseif (-not $remoteLine) {
        $failures += "refs/heads/master was not returned"
    } elseif (("$remoteLine" -split "\s+")[0] -ine $ExpectedHash) {
        $failures += "remote master hash did not match ExpectedHash"
    }

    # Informational only, not part of pass/fail
    Invoke-Git -Arguments @("log", "--oneline", "-3", "--decorate", "--all") -Label "git log --oneline -3" | Out-Null

    # Signals 2-4: local refs via rev-parse (read-only, no network)
    $refs = @("HEAD", "master", "origin/master", "origin/HEAD")
    foreach ($ref in $refs) {
        $resolved = @(& git rev-parse --verify "$ref^{commit}" 2>&1)
        if ($LASTEXITCODE -ne 0 -or $resolved.Count -eq 0) {
            $failures += "$ref could not be resolved"
        } elseif ($resolved[0].Trim() -ine $ExpectedHash) {
            $failures += "$ref did not match ExpectedHash"
        }
    }
}
finally {
    Pop-Location
}

if ($failures.Count -eq 0) {
    Write-Output "THREE-SIGNAL VERIFICATION: PASS"
    exit 0
}

Write-Output "THREE-SIGNAL VERIFICATION: FAIL - $($failures -join '; ')"
exit 1
