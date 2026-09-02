param(
    [Parameter(Mandatory = $true)]
    [string]$Account
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

function Get-LiteralCount {
    param(
        [string]$Text,
        [string]$Literal
    )

    if ([string]::IsNullOrEmpty($Literal)) {
        return 0
    }

    $count = 0
    $offset = 0
    while (($found = $Text.IndexOf($Literal, $offset, [System.StringComparison]::Ordinal)) -ge 0) {
        $count++
        $offset = $found + $Literal.Length
    }

    return $count
}

function Replace-LiteralOnce {
    param(
        [string]$Text,
        [string]$Literal,
        [string]$Replacement,
        [string]$Label
    )

    $count = Get-LiteralCount -Text $Text -Literal $Literal
    if ($count -ne 1) {
        throw "$Label literal replacement count is $count; expected exactly 1."
    }

    $index = $Text.IndexOf($Literal, [System.StringComparison]::Ordinal)
    [pscustomobject]@{
        Before = $Text
        After  = $Text.Substring(0, $index) + $Replacement + $Text.Substring($index + $Literal.Length)
    }
}

$repoRoot = $PSScriptRoot | Split-Path -Parent
$accountsPath = Join-Path $PSScriptRoot '..\config\accounts.yaml'
$allowlistValidator = @'
from pathlib import Path
import sys

try:
    import yaml
except Exception:
    sys.stderr.write("emergency-stop allowlist validator is unavailable\n")
    raise SystemExit(1)

try:
    path = Path(sys.argv[1])
    requested = sys.argv[2]
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    accounts = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(accounts, list):
        raise ValueError
    seen = set()
    eligible = False
    for item in accounts:
        if not isinstance(item, dict):
            raise ValueError
        account_id = item.get("id")
        if not isinstance(account_id, str) or not account_id or account_id in seen:
            raise ValueError
        seen.add(account_id)
        marker = item.get("emergency_stop_eligible", False)
        if not isinstance(marker, bool):
            raise ValueError
        if marker and item.get("mode") != "mock":
            raise ValueError
        if account_id == requested and item.get("mode") == "mock" and marker:
            eligible = True
    if not eligible:
        sys.stderr.write("requested account is not explicitly eligible for emergency stop\n")
        raise SystemExit(1)
except SystemExit:
    raise
except Exception:
    sys.stderr.write("emergency-stop allowlist configuration is unavailable or invalid\n")
    raise SystemExit(1)
'@

$settingsPath = Join-Path $repoRoot "data\dashboard_settings_$Account.json"
$controlPath  = Join-Path $repoRoot "data\control\$Account.control.json"
$controlExists = Test-Path -LiteralPath $controlPath -PathType Leaf
$settingsExists = Test-Path -LiteralPath $settingsPath -PathType Leaf
if (-not $controlExists -and -not $settingsExists) {
    [Console]::Error.WriteLine("Emergency stop did not run: both safety targets are missing: control=$controlPath; settings=$settingsPath")
    exit 1
}

$tempValidatorPath = Join-Path ([System.IO.Path]::GetTempPath()) ("emergency_stop_validator_{0}.py" -f ([guid]::NewGuid().ToString("N")))
try {
    [System.IO.File]::WriteAllText($tempValidatorPath, $allowlistValidator, [System.Text.UTF8Encoding]::new($false))
    $validationOutput = & python $tempValidatorPath $accountsPath $Account 2>&1
    $validationExitCode = $LASTEXITCODE
} finally {
    Remove-Item -LiteralPath $tempValidatorPath -Force -ErrorAction SilentlyContinue
}
if ($validationExitCode -ne 0) {
    $validationDetail = ($validationOutput | Out-String).Trim()
    throw "Emergency stop allowlist rejected the request: $validationDetail"
}

$failures = [System.Collections.Generic.List[string]]::new()
if ($controlExists) {
    $ctrl = Get-Content $controlPath -Raw | ConvertFrom-Json
    $ctrl.auto_trading_enabled = $false
    $ctrlJson = $ctrl | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($controlPath, $ctrlJson, [System.Text.UTF8Encoding]::new($false))
    Write-Output "Control file auto_trading_enabled set to false for $Account"
} else {
    $failures.Add("control target missing: $controlPath")
}

if ($settingsExists) {
    $before = [System.IO.File]::ReadAllText($settingsPath)
    $emptyProfilesLiteral = '{"profiles": [], "auto_remove_closed_positions": true}'

    if ($before -ceq $emptyProfilesLiteral) {
        Write-Output "No profile present for $Account; settings check complete."
    } else {
        $anchors = @(
            [pscustomobject]@{ Label = "profile.enabled"; Text = '"config": {"max_cycles": null}, "enabled": true'; Replacement = '"config": {"max_cycles": null}, "enabled": false' },
            [pscustomobject]@{ Label = "auto_buy.enabled"; Text = '"auto_buy": {"enabled": true'; Replacement = '"auto_buy": {"enabled": false' },
            [pscustomobject]@{ Label = "auto_sell.enabled"; Text = '"auto_sell": {"enabled": true'; Replacement = '"auto_sell": {"enabled": false' }
        )
        foreach ($a in $anchors) {
            $c = Get-LiteralCount -Text $before -Literal $a.Text
            if ($c -ne 1) {
                throw "$($a.Label) anchor count is $c; expected exactly 1."
            }
        }
        foreach ($a in $anchors) {
            $res = Replace-LiteralOnce -Text $before -Literal $a.Text -Replacement $a.Replacement -Label $a.Label
            $before = $res.After
        }
        if ([string]::IsNullOrWhiteSpace($before) -or
            $before.IndexOf('"profiles"', [System.StringComparison]::Ordinal) -lt 0 -or
            $before.IndexOf('"auto_buy": {"enabled": false}', [System.StringComparison]::Ordinal) -lt 0 -or
            $before.IndexOf('"auto_sell": {"enabled": false}', [System.StringComparison]::Ordinal) -lt 0 -or
            $before.IndexOf('"enabled": false', [System.StringComparison]::Ordinal) -lt 0) {
            throw "Settings content validation failed before write for $Account; original file was left untouched."
        }
        [System.IO.File]::WriteAllText($settingsPath, $before, [System.Text.UTF8Encoding]::new($false))
    }
} else {
    $failures.Add("settings target missing: $settingsPath")
}

if ($failures.Count -gt 0) {
    throw "Emergency stop incomplete for ${Account}: $($failures -join '; ')"
}

Write-Output "Emergency stop completed for $Account"
