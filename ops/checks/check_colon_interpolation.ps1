$pattern = '"\$(?<name>[A-Za-z_]\w*):[^"]*"'
$allowedScopes = @(
    "env",
    "global",
    "local",
    "script",
    "private",
    "using",
    "variable",
    "function",
    "filter",
    "alias"
)

$violations = @(
    Get-ChildItem -Path ops -Filter "*.ps1" -Recurse |
        Select-String -Pattern $pattern |
        ForEach-Object {
            $match = [regex]::Match($_.Line, $pattern)
            if ($match.Success -and $match.Groups["name"].Value -notin $allowedScopes) {
                [pscustomobject]@{
                    Path       = $_.Path
                    LineNumber = $_.LineNumber
                    Line       = $_.Line
                }
            }
        }
)

if ($violations.Count -gt 0) {
    $violations | Format-Table -AutoSize
    exit 1
}

exit 0
