$ErrorActionPreference = "Stop"

function Write-LauncherMessage {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ("{0} INFO launcher: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss,fff"), $Message)
}

# Keep start.cmd as the single user-facing entry point while doing secret
# handling in one PowerShell process. All arguments except credential-reset flags are
# forwarded to ``python -m quant_monitor`` as an array, never as evaluated text.
$ResetCredential = $false
$SmokeOnly = $false
$SelectedProvider = "asharehub"
$ProviderExplicit = $false
# A non-empty provider environment value is an intentional override.  Keep an
# empty inherited variable from poisoning config loading (the Python loader
# quite correctly rejects an empty provider name).
if (-not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_DATA_PROVIDER)) {
    $SelectedProvider = ([string]$env:QUANT_MONITOR_DATA_PROVIDER).Trim().ToLowerInvariant()
    $ProviderExplicit = $true
}
$CustomConfigSupplied = $false
$ConfigPath = $null
# ``load_settings`` also accepts QUANT_MONITOR_CONFIG_PATH.  Treat it as a
# custom profile here so provider credential and per-provider database routing
# agree with the configuration Python will load, even when no --config flag is
# passed to the launcher.
if (-not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_CONFIG_PATH)) {
    $CustomConfigSupplied = $true
    $ConfigPath = [string]$env:QUANT_MONITOR_CONFIG_PATH
}
$AppArguments = New-Object System.Collections.Generic.List[string]
for ($Index = 0; $Index -lt $args.Count; $Index++) {
    $Argument = [string]$args[$Index]
    switch -CaseSensitive ($Argument) {
        "--reset-token" {
            $ResetCredential = $true
            continue
        }
        "-ResetToken" {
            $ResetCredential = $true
            continue
        }
        "--reset-key" {
            $ResetCredential = $true
            continue
        }
        "--smoke" {
            $SmokeOnly = $true
            continue
        }
        "-Smoke" {
            $SmokeOnly = $true
            continue
        }
        "-ResetKey" {
            $ResetCredential = $true
            continue
        }
        "--provider" {
            if ($Index + 1 -ge $args.Count) {
                throw "--provider requires one of: asharehub, alltick, akshare, mock."
            }
            $Index++
            $SelectedProvider = ([string]$args[$Index]).Trim().ToLowerInvariant()
            $ProviderExplicit = $true
            continue
        }
        "-Provider" {
            if ($Index + 1 -ge $args.Count) {
                throw "-Provider requires one of: asharehub, alltick, akshare, mock."
            }
            $Index++
            $SelectedProvider = ([string]$args[$Index]).Trim().ToLowerInvariant()
            $ProviderExplicit = $true
            continue
        }
        "-NoBrowser" {
            $AppArguments.Add("--no-browser")
            continue
        }
        "-Port" {
            if ($Index + 1 -ge $args.Count) {
                throw "-Port requires a value."
            }
            $Index++
            $AppArguments.Add("--port")
            $AppArguments.Add([string]$args[$Index])
            continue
        }
        "--config" {
            if ($Index + 1 -ge $args.Count) {
                throw "--config requires a JSON configuration path."
            }
            $Index++
            $CustomConfigSupplied = $true
            $ConfigPath = [string]$args[$Index]
            $AppArguments.Add("--config")
            $AppArguments.Add([string]$args[$Index])
            continue
        }
        default {
            if ($Argument.StartsWith("--config=", [StringComparison]::Ordinal)) {
                $CustomConfigSupplied = $true
                $ConfigPath = $Argument.Substring("--config=".Length)
            }
            $AppArguments.Add($Argument)
        }
    }
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
# With no explicit provider override, let a custom JSON profile select its
# provider.  This is intentionally done in PowerShell so Git Bash/CMD and the
# native Windows entry point share exactly the same credential and DB routing.
if (-not $ProviderExplicit -and $CustomConfigSupplied) {
    if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
        throw "--config requires a JSON configuration path."
    }
    $ConfigFilePath = $ConfigPath
    if (-not [IO.Path]::IsPathRooted($ConfigFilePath)) {
        $ConfigFilePath = Join-Path $ProjectRoot $ConfigFilePath
    }
    if (-not (Test-Path -LiteralPath $ConfigFilePath -PathType Leaf)) {
        throw "configuration file does not exist: $ConfigPath"
    }
    try {
        $ConfigPayload = Get-Content -LiteralPath $ConfigFilePath -Raw -ErrorAction Stop |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "configuration file is not valid JSON: $ConfigPath"
    }
    $ConfigProvider = if ($null -ne $ConfigPayload) {
        [string]$ConfigPayload.data_provider
    }
    else {
        ""
    }
    if (-not [string]::IsNullOrWhiteSpace($ConfigProvider)) {
        $SelectedProvider = $ConfigProvider.Trim().ToLowerInvariant()
    }
}

if (@("asharehub", "alltick", "akshare", "mock") -notcontains $SelectedProvider) {
    throw "Unsupported provider '$SelectedProvider'. Use asharehub, alltick, akshare, or mock."
}
if ($SmokeOnly -and $SelectedProvider -ne "asharehub") {
    throw "--smoke currently supports only the asharehub provider."
}
if ($ResetCredential -and $SelectedProvider -notin @("asharehub", "alltick")) {
    throw "Credential reset is available only for asharehub or alltick."
}

$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
# Only an explicit CLI/environment override is exported.  Otherwise the
# selected value came from the JSON profile (or the default) and Python should
# load the same profile without an environment value taking precedence.
if ($ProviderExplicit) {
    $env:QUANT_MONITOR_DATA_PROVIDER = $SelectedProvider
}
else {
    Remove-Item Env:\QUANT_MONITOR_DATA_PROVIDER -ErrorAction SilentlyContinue
}
# The current market-data primary keys do not include ``source``.  Keep every
# provider in its own database unless the caller deliberately supplied a path;
# otherwise a mock/compatibility launch could overwrite same-key AShareHub rows.
$ProcessDatabasePathWasInjected = $false
if (-not $CustomConfigSupplied -and
    [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_DATABASE_PATH) -and
    [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_DB_PATH)) {
    $env:QUANT_MONITOR_DATABASE_PATH = Join-Path $ProjectRoot (
        "data\quant_monitor_{0}.db" -f $SelectedProvider
    )
    $ProcessDatabasePathWasInjected = $true
}
$NeedsCredential = ($SelectedProvider -in @("asharehub", "alltick"))
$CredentialAlreadyPresent = $false
if ($NeedsCredential) {
    $CredentialAlreadyPresent = if ($SelectedProvider -ieq "asharehub") {
        -not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_ASHAREHUB_API_KEY) -or
        -not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_ASHAREHUB_KEY)
    } else {
        -not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_ALLTICK_TOKEN) -or
        -not [string]::IsNullOrWhiteSpace($env:QUANT_MONITOR_ALLTICK_API_KEY)
    }
}
$InjectedCredential = $false
$SecureCredential = $null
$PlainValue = $null

function Read-NewProviderCredential {
    param([Parameter(Mandatory = $true)][string]$Provider)
    $Label = if ($Provider -ieq "asharehub") { "AShareHub API key" } else { "AllTick token" }
    $Value = Read-Host "$Label (input is hidden)" -AsSecureString
    if ($null -eq $Value -or $Value.Length -eq 0) {
        throw "$Label cannot be empty."
    }
    return $Value
}

function Save-ProviderCredential {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $Directory = Split-Path -Parent $Path
    [IO.Directory]::CreateDirectory($Directory) | Out-Null
    $TemporaryPath = Join-Path $Directory ([IO.Path]::GetRandomFileName())
    try {
        $CipherText = ConvertFrom-SecureString $Value
        $CipherText | Set-Content -LiteralPath $TemporaryPath -Encoding ASCII
        Move-Item -LiteralPath $TemporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $TemporaryPath) {
            Remove-Item -LiteralPath $TemporaryPath -Force
        }
    }
}

function Read-SavedProviderCredential {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    try {
        $CipherText = (Get-Content -LiteralPath $Path -Raw).Trim()
        if ([string]::IsNullOrWhiteSpace($CipherText)) {
            throw "empty credential"
        }
        return ConvertTo-SecureString $CipherText
    }
    catch {
        throw "The encrypted provider credential cannot be decrypted. Run scripts\start.cmd --reset-key."
    }
}

function Set-ProviderProcessCredential {
    param(
        [Parameter(Mandatory = $true)]
        [Security.SecureString]$Value,
        [Parameter(Mandatory = $true)]
        [string]$Provider
    )

    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        $PlainValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
        if ($Provider -ieq "asharehub") {
            $env:QUANT_MONITOR_ASHAREHUB_API_KEY = $PlainValue
        }
        else {
            $env:QUANT_MONITOR_ALLTICK_TOKEN = $PlainValue
        }
    }
    finally {
        $PlainValue = $null
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
}

try {
    Set-Location -LiteralPath $ProjectRoot
    Write-LauncherMessage ("Starting market data provider: {0}" -f $SelectedProvider)

    if ($NeedsCredential -and ($ResetCredential -or -not $CredentialAlreadyPresent)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "LOCALAPPDATA is unavailable; the encrypted credential cannot be stored."
        }

        $CredentialFile = if ($SelectedProvider -ieq "asharehub") { "asharehub-api-key.dpapi" } else { "alltick-token.dpapi" }
        $CredentialPath = Join-Path $env:LOCALAPPDATA "a-share-quant-monitor\$CredentialFile"
        if ($ResetCredential -or -not (Test-Path -LiteralPath $CredentialPath)) {
            Write-LauncherMessage ("First-time setup: enter the {0} credential once." -f $SelectedProvider)
            $SecureCredential = Read-NewProviderCredential -Provider $SelectedProvider
            Save-ProviderCredential -Value $SecureCredential -Path $CredentialPath
            Write-LauncherMessage "Encrypted credential saved for this Windows account."
        }
        else {
            $SecureCredential = Read-SavedProviderCredential -Path $CredentialPath
        }

        Set-ProviderProcessCredential -Value $SecureCredential -Provider $SelectedProvider
        $InjectedCredential = $true
    }

    if (-not (Test-Path -LiteralPath $Python)) {
        & python -m venv (Join-Path $ProjectRoot ".venv")
        if ($LASTEXITCODE -ne 0) {
            throw "Python failed to create .venv. Python 3.9 or newer is required."
        }
    }
    if (-not (Test-Path -LiteralPath $Python)) {
        throw "The virtual environment Python executable was not created."
    }

    if ($SelectedProvider -ieq "asharehub" -and -not $CredentialAlreadyPresent -and -not $InjectedCredential) {
        throw "AShareHub API key is unavailable. Run scripts\start.cmd --reset-key."
    }

    # Keep the readiness hint aligned with the port forwarded to the Python
    # CLI. This is display-only; the CLI remains the source of truth for
    # argument validation and binding.
    $DisplayPort = "8000"
    for ($DisplayIndex = 0; $DisplayIndex -lt $AppArguments.Count; $DisplayIndex++) {
        $DisplayArgument = [string]$AppArguments[$DisplayIndex]
        if ($DisplayArgument -eq "--port" -and $DisplayIndex + 1 -lt $AppArguments.Count) {
            $DisplayPort = [string]$AppArguments[$DisplayIndex + 1]
            break
        }
        if ($DisplayArgument.StartsWith("--port=", [StringComparison]::Ordinal)) {
            $DisplayPort = $DisplayArgument.Substring("--port=".Length)
            break
        }
    }

    $DependencyProbe = if ($SelectedProvider -in @("akshare", "alltick")) {
        "import fastapi, uvicorn, akshare, websockets"
    }
    else {
        "import fastapi, uvicorn"
    }
    & $Python -c $DependencyProbe 2>$null
    if ($LASTEXITCODE -ne 0) {
        & $Python -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) {
            throw "ensurepip failed."
        }
        & $Python -m pip install -r (Join-Path $ProjectRoot "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
    }

    if ($SmokeOnly) {
        Write-LauncherMessage "Running one-request AShareHub credential smoke check."
        & $Python (Join-Path $ProjectRoot "scripts\smoke_asharehub.py") "--symbols" "600519.SH"
    }
    else {
        Write-LauncherMessage (
            "Server runs in this window. Wait for 'Application startup complete'; " +
            ("open http://127.0.0.1:{0}/ready for readiness, and press Ctrl+C to stop." -f $DisplayPort)
        )
        & $Python "-m" "quant_monitor" @AppArguments
    }
    $ExitCode = $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine("ERROR: {0}", $_.Exception.Message)
    $ExitCode = 1
}
finally {
    $PlainValue = $null
    if ($InjectedCredential) {
        Remove-Item Env:\QUANT_MONITOR_ASHAREHUB_API_KEY -ErrorAction SilentlyContinue
        Remove-Item Env:\QUANT_MONITOR_ALLTICK_TOKEN -ErrorAction SilentlyContinue
    }
    if ($ProcessDatabasePathWasInjected) {
        Remove-Item Env:\QUANT_MONITOR_DATABASE_PATH -ErrorAction SilentlyContinue
    }
    if ($null -ne $SecureCredential) {
        $SecureCredential.Dispose()
    }
}

exit $ExitCode
