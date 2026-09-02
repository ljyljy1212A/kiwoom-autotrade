param(
    [string]$InstallRoot = 'C:\kiwoom-autotrade',
    [string]$TemplatePath = '',
    [switch]$UseSystemTask,
    [switch]$InstallKrWorker,
    [switch]$InstallUsWorker
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$MockAccounts = @{
    'kr_mock' = @{ Market = 'KR'; EnvPrefix = 'ACCOUNT_D' }
    'us_mock' = @{ Market = 'US'; EnvPrefix = 'ACCOUNT_B' }
}

function Read-RequiredValue([string]$Prompt, [string]$Default = '') {
    do {
        $suffix = if ($Default) { " [$Default]" } else { '' }
        $value = Read-Host ($Prompt + $suffix)
        if (-not $value -and $Default) { $value = $Default }
    } while (-not $value)
    return $value.Trim()
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

function Convert-SecureValue([Security.SecureString]$Value) {
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Read-Secret([string]$Prompt) {
    $secure = Read-Host -Prompt $Prompt -AsSecureString
    return Convert-SecureValue $secure
}

function Find-Python {
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe'),
        (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Source)
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    throw 'Python executable was not found. Supply a valid PythonPath in the operator handoff.'
}

function Set-XmlValue([xml]$Document, [string]$Name, [string]$Value) {
    $node = $Document.SelectSingleNode("//*[local-name()='$Name']")
    if (-not $node) { throw "Task template is missing <$Name>." }
    $node.InnerText = $Value
}

function New-TaskArtifact(
    [string]$Name, [string]$Command, [string]$Arguments, [string]$WorkingDirectory,
    [string]$UserId, [string]$SourceTemplate, [string]$OutputPath
) {
    $document = [xml](Get-Content -LiteralPath $SourceTemplate -Raw)
    Set-XmlValue $document 'URI' "\$Name"
    Set-XmlValue $document 'UserId' $UserId
    Set-XmlValue $document 'Command' $Command
    Set-XmlValue $document 'Arguments' $Arguments
    Set-XmlValue $document 'WorkingDirectory' $WorkingDirectory
    $settings = New-Object System.Xml.XmlWriterSettings
    $settings.Encoding = New-Object System.Text.UnicodeEncoding($false, $true)
    $settings.Indent = $true
    $writer = [System.Xml.XmlWriter]::Create($OutputPath, $settings)
    try { $document.Save($writer) } finally { $writer.Dispose() }
}

function Assert-EnvKeys([string]$Path, [string[]]$RequiredKeys) {
    $present = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([A-Z0-9_]+)=(.*)$') { $present[$Matches[1]] = [bool]$Matches[2] }
    }
    foreach ($key in $RequiredKeys) {
        if (-not $present.ContainsKey($key) -or -not $present[$key]) { throw "Generated environment is missing a value for $key." }
    }
}

function Assert-TaskArtifact([string]$Path, [string]$TaskName, [string]$WorkingDirectory) {
    $document = [xml](Get-Content -LiteralPath $Path -Raw)
    if (($document.SelectSingleNode("//*[local-name()='URI']")).InnerText -ne "\$TaskName") { throw "Unexpected task URI in $Path." }
    if (($document.SelectSingleNode("//*[local-name()='WorkingDirectory']")).InnerText -ne $WorkingDirectory) { throw "Unexpected task working directory in $Path." }
}

Write-Output 'Phase: Detect'
if (-not $TemplatePath) { $TemplatePath = Join-Path $PSScriptRoot 'watchdog_task.xml' }
if (-not (Test-Path -LiteralPath $TemplatePath -PathType Leaf)) { throw "Task template was not found: $TemplatePath" }
$PythonPath = Find-Python

Write-Output 'Phase: Resolve/validate parameters'
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
if ($InstallRoot -match '[\x00-\x1F]') { throw 'InstallRoot contains control characters.' }
$currentUser = "$env:USERDOMAIN\$env:USERNAME"

Write-Output 'Phase: Prompt'
if ($UseSystemTask) {
    $taskUserId = 'S-1-5-18'
    $taskUserLabel = 'SYSTEM'
} elseif (Read-YesNo 'Run generated tasks as SYSTEM?' $false) {
    $taskUserId = 'S-1-5-18'
    $taskUserLabel = 'SYSTEM'
} else {
    $taskUserId = Read-RequiredValue 'Task identity' $currentUser
    $taskUserLabel = $taskUserId
}
$krNo = Read-RequiredValue 'KR mock account number'
$krAppKey = Read-Secret 'KR mock app key'
$krSecretKey = Read-Secret 'KR mock secret key'
$usNo = Read-RequiredValue 'US mock account number'
$usAppKey = Read-Secret 'US mock app key'
$usSecretKey = Read-Secret 'US mock secret key'
$telegramToken = Read-Secret 'Telegram bot token'
$telegramChatId = Read-RequiredValue 'Telegram chat id'
$installKr = if ($InstallKrWorker) { $true } else { Read-YesNo 'Generate KR mock worker task?' $false }
$installUs = if ($InstallUsWorker) { $true } else { Read-YesNo 'Generate US mock worker task?' $false }

Write-Output 'Phase: Provision'
$directories = @('data', 'logs', 'diagnostics', 'generated-task-xml', '.venv')
foreach ($directory in $directories) { New-Item -ItemType Directory -Path (Join-Path $InstallRoot $directory) -Force | Out-Null }
$venvPath = Join-Path $InstallRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { & $PythonPath -m venv $venvPath }
$requirementsPath = Join-Path $InstallRoot 'requirements.txt'
if (-not (Test-Path -LiteralPath $requirementsPath -PathType Leaf)) { throw "Requirements file was not found: $requirementsPath" }
& $venvPython -m pip install -r $requirementsPath

Write-Output 'Phase: Generate artifacts'
$envPath = Join-Path $InstallRoot '.env'
$krPrefix = $MockAccounts['kr_mock'].EnvPrefix
$usPrefix = $MockAccounts['us_mock'].EnvPrefix
$envLines = @(
    "${krPrefix}_NO=$krNo", "${krPrefix}_APPKEY=$krAppKey", "${krPrefix}_SECRETKEY=$krSecretKey",
    "${usPrefix}_NO=$usNo", "${usPrefix}_APPKEY=$usAppKey", "${usPrefix}_SECRETKEY=$usSecretKey",
    "TELEGRAM_BOT_TOKEN=$telegramToken", "TELEGRAM_CHAT_ID=$telegramChatId",
    'ACCOUNT_MODE=mock', 'KIWOOM_MOCK=true'
)
Set-Content -LiteralPath $envPath -Value $envLines -Encoding UTF8
$krAppKey = $null; $krSecretKey = $null; $usAppKey = $null; $usSecretKey = $null; $telegramToken = $null
$pythonw = Join-Path $venvPath 'Scripts\pythonw.exe'
$taskDirectory = Join-Path $InstallRoot 'generated-task-xml'
$watchdogScript = Join-Path $InstallRoot 'tools\worker_watchdog.py'
$watchdogXml = Join-Path $taskDirectory 'Kiwoom Worker Watchdog.xml'
New-TaskArtifact 'Kiwoom Worker Watchdog' $pythonw "`"$watchdogScript`"" $InstallRoot $taskUserId $TemplatePath $watchdogXml
$workerSelections = @(
    @{ Enabled = $installKr; Account = 'kr_mock'; Market = $MockAccounts['kr_mock'].Market; File = 'Kiwoom Worker KR Mock.xml' },
    @{ Enabled = $installUs; Account = 'us_mock'; Market = $MockAccounts['us_mock'].Market; File = 'Kiwoom Worker US Mock.xml' }
)
foreach ($selection in $workerSelections) {
    if ($selection.Enabled) {
        $workerXml = Join-Path $taskDirectory $selection.File
        $arguments = "-m src.worker_supervisor start --account $($selection.Account) --market $($selection.Market)"
        New-TaskArtifact "Kiwoom Worker $($selection.Market) Mock" $pythonw $arguments $InstallRoot $taskUserId $TemplatePath $workerXml
    }
}

Write-Output 'Phase: Validate'
$requiredEnv = @("${krPrefix}_NO", "${krPrefix}_APPKEY", "${krPrefix}_SECRETKEY", "${usPrefix}_NO", "${usPrefix}_APPKEY", "${usPrefix}_SECRETKEY", 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID', 'ACCOUNT_MODE', 'KIWOOM_MOCK')
Assert-EnvKeys $envPath $requiredEnv
if ((Get-Content -LiteralPath $envPath -Raw) -notmatch '(?m)^ACCOUNT_MODE=mock$') { throw 'Environment mode validation failed.' }
if ((Get-Content -LiteralPath $envPath -Raw) -notmatch '(?m)^KIWOOM_MOCK=true$') { throw 'Mock flag validation failed.' }
Assert-TaskArtifact $watchdogXml 'Kiwoom Worker Watchdog' $InstallRoot
foreach ($selection in $workerSelections) {
    if ($selection.Enabled) { Assert-TaskArtifact (Join-Path $taskDirectory $selection.File) "Kiwoom Worker $($selection.Market) Mock" $InstallRoot }
}
if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) { throw 'Virtual-environment interpreter validation failed.' }
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'Windowed interpreter validation failed.' }

Write-Output 'Phase: Registration handoff'
Write-Output "Task identity: $taskUserLabel"
Write-Output "Register later with: schtasks.exe /Create /TN \"Kiwoom Worker Watchdog\" /XML \"$watchdogXml\" /F"
foreach ($selection in $workerSelections) {
    if ($selection.Enabled) { Write-Output "Register later with: schtasks.exe /Create /TN \"Kiwoom Worker $($selection.Market) Mock\" /XML \"$(Join-Path $taskDirectory $selection.File)\" /F" }
}
Write-Output 'No task registration is performed by this script.'
