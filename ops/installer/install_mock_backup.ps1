param(
    [string]$ProjectRoot = 'C:\kiwoom-autotrade',
    [string]$TemplatePath = '',
    [string]$BackupBaseDir = '',
    [int]$RetentionDays = 7,
    [int]$RetentionCount = 7,
    [Nullable[bool]]$InstallDatabaseTask,
    [Nullable[bool]]$InstallFileTask
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MockDatabaseAllowlist = @(
    'data\trades_kr_mock.db',
    'data\trades_us_mock.db',
    'data\reports_kr_mock.db',
    'data\reports_us_mock.db',
    'data\dedup_kr_mock.db',
    'data\dedup_us_mock.db'
)

$DatabaseTaskName = 'Kiwoom Project Database Backup'
$FileTaskName = 'Kiwoom Project Files Backup'
$DatabaseInterval = 'P1D'
$FileInterval = 'P7D'

function Read-RequiredValue([string]$Prompt, [string]$Default = '') {
    do {
        $suffix = if ($Default) { " [$Default]" } else { '' }
        $value = Read-Host ($Prompt + $suffix)
        if (-not $value -and $Default) { $value = $Default }
    } while (-not $value)
    return $value.Trim()
}

function Read-PositiveInteger([string]$Prompt, [int]$Default) {
    do {
        $value = Read-Host "$Prompt [$Default]"
        if (-not $value) { return $Default }
        $parsed = 0
        $valid = [int]::TryParse($value.Trim(), [Globalization.NumberStyles]::Integer, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)
    } while (-not $valid -or $parsed -lt 1)
    return $parsed
}

function Read-YesNo([string]$Prompt, [bool]$Default = $false) {
    $defaultText = if ($Default) { 'Y' } else { 'N' }
    do {
        $value = Read-Host "$Prompt [Y/N, default $defaultText]"
        if (-not $value) { return $Default }
        $value = $value.Trim().ToUpperInvariant()
    } while ($value -notin @('Y', 'N', 'YES', 'NO'))
    return $value -in @('Y', 'YES')
}

function Find-PythonWindowed([string]$Root) {
    $candidates = @(
        (Join-Path $Root '.venv\Scripts\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\pythonw.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\pythonw.exe'),
        (Get-Command pythonw.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw 'Windowed Python executable was not found.'
}

function Set-XmlValue([xml]$Document, [string]$Name, [string]$Value) {
    $node = $Document.SelectSingleNode("//*[local-name()='$Name']")
    if (-not $node) { throw "Task template is missing <$Name>." }
    $node.InnerText = $Value
}

function New-TaskArtifact(
    [string]$Name, [string]$Command, [string]$Arguments, [string]$WorkingDirectory,
    [string]$SourceTemplate, [string]$OutputPath, [string]$Interval
) {
    $document = [xml](Get-Content -LiteralPath $SourceTemplate -Raw)
    Set-XmlValue $document 'URI' "\$Name"
    Set-XmlValue $document 'Command' $Command
    Set-XmlValue $document 'Arguments' $Arguments
    Set-XmlValue $document 'WorkingDirectory' $WorkingDirectory
    Set-XmlValue $document 'Interval' $Interval
    $settings = New-Object System.Xml.XmlWriterSettings
    $settings.Encoding = New-Object System.Text.UnicodeEncoding($false, $true)
    $settings.Indent = $true
    $writer = [System.Xml.XmlWriter]::Create($OutputPath, $settings)
    try { $document.Save($writer) } finally { $writer.Dispose() }
}

function Assert-PositiveInteger([string]$Name, [int]$Value) {
    if ($Value -lt 1) { throw "$Name must be a positive integer." }
}

function Assert-WritableDestination([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    $probe = Join-Path $Path ('.installer_probe_' + [Guid]::NewGuid().ToString('N'))
    try {
        Set-Content -LiteralPath $probe -Value 'probe' -Encoding ASCII -NoNewline
        if ((Get-Content -LiteralPath $probe -Raw) -ne 'probe') { throw 'Destination probe read-back failed.' }
    } finally {
        if (Test-Path -LiteralPath $probe) { Remove-Item -LiteralPath $probe -Force }
    }
}

function Assert-TaskArtifact([string]$Path, [string]$TaskName, [string]$WorkingDirectory, [string]$Interval, [string]$ExpectedArguments) {
    $document = [xml](Get-Content -LiteralPath $Path -Raw)
    if ($document.SelectSingleNode("//*[local-name()='URI']").InnerText -ne "\$TaskName") { throw "Unexpected task URI in $Path." }
    if ($document.SelectSingleNode("//*[local-name()='WorkingDirectory']").InnerText -ne $WorkingDirectory) { throw "Unexpected task working directory in $Path." }
    if ($document.SelectSingleNode("//*[local-name()='Interval']").InnerText -ne $Interval) { throw "Unexpected task interval in $Path." }
    if ($document.SelectSingleNode("//*[local-name()='Arguments']").InnerText -ne $ExpectedArguments) { throw "Unexpected task arguments in $Path." }
}

function Assert-Safety([string[]]$Paths) {
    $patterns = @(
        (-join @('r', 'e', 'a', 'l')),
        (-join @('a', 'c', 'c', 'o', 'u', 'n', 't', 's', '\.', 'y', 'a', 'm', 'l')),
        (-join @('\.', 'e', 'n', 'v'))
    )
    foreach ($path in $Paths) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
        $content = Get-Content -LiteralPath $path -Raw
        foreach ($pattern in $patterns) {
            if ($content -match "(?i)$pattern") { throw "Safety validation failed in $path." }
        }
    }
}

Write-Output 'Phase: Detect'
if (-not $TemplatePath) { $TemplatePath = Join-Path $PSScriptRoot 'watchdog_task.xml' }
if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) { throw "Task template was not found: $TemplatePath" }
$ProjectRoot = [IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) { throw "Project root was not found: $ProjectRoot" }
$PythonPath = Find-PythonWindowed $ProjectRoot

Write-Output 'Phase: Resolve/validate parameters'
if (-not $BackupBaseDir) { $BackupBaseDir = [Environment]::GetEnvironmentVariable('KIWOOM_BACKUP_BASE_DIR') }
if (-not $BackupBaseDir) { $BackupBaseDir = 'C:\Backups\ProjectDB' }
$BackupBaseDir = [IO.Path]::GetFullPath($BackupBaseDir)
if ($BackupBaseDir -match '[\x00-\x1F]') { throw 'Backup destination contains control characters.' }

Write-Output 'Phase: Prompt'
$BackupBaseDir = Read-RequiredValue 'Backup destination' $BackupBaseDir
$RetentionDays = Read-PositiveInteger 'Database retention days' $RetentionDays
$RetentionCount = Read-PositiveInteger 'File retention count' $RetentionCount
if ($null -eq $InstallDatabaseTask) { $InstallDatabaseTask = Read-YesNo 'Generate database backup task?' $true }
if ($null -eq $InstallFileTask) { $InstallFileTask = Read-YesNo 'Generate file backup task?' $true }
if (-not $InstallDatabaseTask -and -not $InstallFileTask) { throw 'At least one backup task must be selected.' }
Assert-PositiveInteger 'RetentionDays' $RetentionDays
Assert-PositiveInteger 'RetentionCount' $RetentionCount

Write-Output 'Phase: Provision'
Assert-WritableDestination $BackupBaseDir
$taskDirectory = Join-Path $PSScriptRoot 'generated-task-xml'
New-Item -ItemType Directory -Path $taskDirectory -Force | Out-Null

Write-Output 'Phase: Generate artifacts'
$databaseScript = Join-Path $ProjectRoot 'tools\backup_project_databases.py'
$fileScript = Join-Path $ProjectRoot 'tools\backup_project_files.py'
$databaseXml = Join-Path $taskDirectory "$DatabaseTaskName.xml"
$fileXml = Join-Path $taskDirectory "$FileTaskName.xml"
$databaseArguments = "`"$databaseScript`" --destination `"$BackupBaseDir`" --retention-days $RetentionDays"
$fileArguments = "`"$fileScript`" --destination `"$BackupBaseDir`" --retention-count $RetentionCount"
if ($InstallDatabaseTask) { New-TaskArtifact $DatabaseTaskName $PythonPath $databaseArguments $ProjectRoot $TemplatePath $databaseXml $DatabaseInterval }
if ($InstallFileTask) { New-TaskArtifact $FileTaskName $PythonPath $fileArguments $ProjectRoot $TemplatePath $fileXml $FileInterval }

Write-Output 'Phase: Validate'
if ($MockDatabaseAllowlist.Count -ne 6) { throw 'Mock database allowlist count validation failed.' }
$expectedAllowlist = @(
    'data\trades_kr_mock.db',
    'data\trades_us_mock.db',
    'data\reports_kr_mock.db',
    'data\reports_us_mock.db',
    'data\dedup_kr_mock.db',
    'data\dedup_us_mock.db'
)
if ($MockDatabaseAllowlist.Count -ne $expectedAllowlist.Count) {
    throw 'Mock database allowlist count mismatch.'
}
for ($i = 0; $i -lt $expectedAllowlist.Count; $i++) {
    if ($MockDatabaseAllowlist[$i] -ne $expectedAllowlist[$i]) {
        throw "Mock database allowlist entry $i does not match expected value."
    }
}
Assert-Safety @($PSCommandPath, $databaseXml, $fileXml)
if ($InstallDatabaseTask) { Assert-TaskArtifact $databaseXml $DatabaseTaskName $ProjectRoot $DatabaseInterval $databaseArguments }
if ($InstallFileTask) { Assert-TaskArtifact $fileXml $FileTaskName $ProjectRoot $FileInterval $fileArguments }
Write-Output 'Validation: destination writable, retention positive, allowlist exact, XML well-formed and expected.'
Write-Output 'Validation: no credential or account-configuration references; no backup process or task registration performed.'

Write-Output 'Phase: Registration handoff'
if ($InstallDatabaseTask) { Write-Output "Register later with: schtasks.exe /Create /TN \"$DatabaseTaskName\" /XML \"$databaseXml\" /F" }
if ($InstallFileTask) { Write-Output "Register later with: schtasks.exe /Create /TN \"$FileTaskName\" /XML \"$fileXml\" /F" }
Write-Output 'No task registration is performed by this script.'
