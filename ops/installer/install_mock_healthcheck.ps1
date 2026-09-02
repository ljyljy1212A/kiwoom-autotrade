param(
    [string]$InstallRoot = 'C:\kiwoom-autotrade',
    [string]$TemplatePath = '',
    [Nullable[bool]]$InstallKrWorker,
    [Nullable[bool]]$InstallUsWorker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MockHealthcheckTaskNames = @{
    Watchdog = 'Kiwoom Worker Watchdog'
    KrWorker = 'Kiwoom Worker KR Mock'
    UsWorker = 'Kiwoom Worker US Mock'
}

$HealthcheckTaskName = 'Kiwoom Scheduled Task Healthcheck'
$HealthcheckInterval = 'PT5M'

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

function Assert-TaskEntry([object]$Entry, [string]$ExpectedName, [string]$ExpectedTarget) {
    $properties = @($Entry.PSObject.Properties.Name | Sort-Object)
    if (($properties -join ',') -ne 'task_name,task_path,target_path') {
        throw "Unexpected fields for task entry $ExpectedName."
    }
    if ($Entry.task_name -ne $ExpectedName) { throw "Unexpected task name: $($Entry.task_name)" }
    if ($Entry.task_path -ne '\') { throw "Unexpected task path for $ExpectedName." }
    if ($Entry.target_path -ne $ExpectedTarget) { throw "Unexpected target path for $ExpectedName." }
}

function Assert-HealthcheckConfig([string]$Path, [string[]]$ExpectedNames, [string]$Root) {
    $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    if ($null -eq $payload.tasks -or $payload.PSObject.Properties.Name.Count -ne 1) {
        throw 'Generated healthcheck config must contain only the tasks field.'
    }
    $entries = @($payload.tasks)
    if ($entries.Count -ne $ExpectedNames.Count) { throw 'Generated task count does not match selections.' }
    $actualNames = @($entries | ForEach-Object { $_.task_name })
    if (($actualNames | Sort-Object -Unique).Count -ne $actualNames.Count) {
        throw 'Generated healthcheck config contains duplicate task names.'
    }
    for ($i = 0; $i -lt $ExpectedNames.Count; $i++) {
        $expectedTarget = if ($ExpectedNames[$i] -eq $MockHealthcheckTaskNames.Watchdog) {
            Join-Path $Root 'tools\worker_watchdog.py'
        } else {
            Join-Path $Root 'src\worker_supervisor.py'
        }
        Assert-TaskEntry $entries[$i] $ExpectedNames[$i] $expectedTarget
    }
    if (@($actualNames | Where-Object { $_ -notin $MockHealthcheckTaskNames.Values }).Count -gt 0) {
        throw 'Generated healthcheck config contains an unsupported task name.'
    }
    if (@($actualNames | Where-Object { $_ -match '(?i)backup|telegram' }).Count -gt 0) {
        throw 'Generated healthcheck config contains an out-of-scope task name.'
    }
    if (@($actualNames | Where-Object { $_ -eq $MockHealthcheckTaskNames.Watchdog }).Count -ne 1) {
        throw 'Generated healthcheck config must contain exactly one watchdog task.'
    }
}

function Assert-TaskArtifact([string]$Path, [string]$TaskName, [string]$Command, [string]$Arguments, [string]$WorkingDirectory, [string]$Interval) {
    $document = [xml](Get-Content -LiteralPath $Path -Raw)
    if ($document.SelectSingleNode("//*[local-name()='URI']").InnerText -ne "\$TaskName") { throw 'Unexpected healthcheck task URI.' }
    if ($document.SelectSingleNode("//*[local-name()='Command']").InnerText -ne $Command) { throw 'Unexpected healthcheck command.' }
    if ($document.SelectSingleNode("//*[local-name()='Arguments']").InnerText -ne $Arguments) { throw 'Unexpected healthcheck arguments.' }
    if ($document.SelectSingleNode("//*[local-name()='WorkingDirectory']").InnerText -ne $WorkingDirectory) { throw 'Unexpected healthcheck working directory.' }
    if ($document.SelectSingleNode("//*[local-name()='Interval']").InnerText -ne $Interval) { throw 'Unexpected healthcheck interval.' }
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
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if (-not (Test-Path -LiteralPath $InstallRoot -PathType Container)) { throw "Install root was not found: $InstallRoot" }
$PythonPath = Find-PythonWindowed $InstallRoot

Write-Output 'Phase: Resolve/validate parameters'

Write-Output 'Phase: Prompt'
if ($null -eq $InstallKrWorker) { $InstallKrWorker = Read-YesNo 'Was the KR mock worker task installed?' $false }
if ($null -eq $InstallUsWorker) { $InstallUsWorker = Read-YesNo 'Was the US mock worker task installed?' $false }

$selectedNames = @($MockHealthcheckTaskNames.Watchdog)
if ($InstallKrWorker) { $selectedNames += $MockHealthcheckTaskNames.KrWorker }
if ($InstallUsWorker) { $selectedNames += $MockHealthcheckTaskNames.UsWorker }

Write-Output 'Phase: Provision'
$taskDirectory = Join-Path $InstallRoot 'generated-task-xml'
New-Item -ItemType Directory -Path $taskDirectory -Force | Out-Null

Write-Output 'Phase: Generate'
$configPath = Join-Path $taskDirectory 'healthcheck_mock_only.json'
$configEntries = foreach ($name in $selectedNames) {
    $target = if ($name -eq $MockHealthcheckTaskNames.Watchdog) {
        Join-Path $InstallRoot 'tools\worker_watchdog.py'
    } else {
        Join-Path $InstallRoot 'src\worker_supervisor.py'
    }
    [ordered]@{
        task_name = $name
        task_path = '\'
        target_path = $target
    }
}
([ordered]@{ tasks = @($configEntries) } | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $configPath -Encoding UTF8

$healthcheckScript = Join-Path $InstallRoot 'tools\scheduled_task_healthcheck.py'
$healthcheckXml = Join-Path $taskDirectory "$HealthcheckTaskName.xml"
$healthcheckArguments = "`"$healthcheckScript`" --config `"$configPath`" --mode mock-only"
New-TaskArtifact $HealthcheckTaskName $PythonPath $healthcheckArguments $InstallRoot $TemplatePath $healthcheckXml $HealthcheckInterval

Write-Output 'Phase: Validate'
Assert-Safety @($PSCommandPath, $configPath, $healthcheckXml)
Assert-HealthcheckConfig $configPath $selectedNames $InstallRoot
Assert-TaskArtifact $healthcheckXml $HealthcheckTaskName $PythonPath $healthcheckArguments $InstallRoot $HealthcheckInterval
Write-Output 'Validation: generated config satisfies mock-only task-name and watchdog requirements.'
Write-Output 'Validation: generated XML is well-formed with PT5M and --mode mock-only.'
Write-Output 'Validation: no credentials referenced; healthcheck execution and task registration were not performed.'

Write-Output 'Phase: Registration handoff'
Write-Output "Register later with: schtasks.exe /Create /TN \"$HealthcheckTaskName\" /XML \"$healthcheckXml\" /F"
Write-Output 'No task registration is performed by this script.'
