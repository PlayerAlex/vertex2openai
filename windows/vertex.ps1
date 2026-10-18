[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("run", "start", "stop", "restart", "status", "logs", "update", "open")]
    [string]$Command = "start",

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$ProductName = "Quick Vertex API"
$Port = 8050
$ConsoleUrl = "http://127.0.0.1:$Port/"
$DefaultSourceUrl = "https://codeload.github.com/PlayerAlex/vertex2openai/zip/refs/heads/main"
$PythonInstallerUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
}

$InstallRoot = if ($env:VERTEX_INSTALL_DIR) { $env:VERTEX_INSTALL_DIR } else { Join-Path $env:LOCALAPPDATA "QuickVertexAPI" }
$InstallRoot = Get-FullPath $InstallRoot
$RepoDir = Join-Path $InstallRoot "repo"
$VenvDir = Join-Path $InstallRoot "venv"
$DataDir = Join-Path $InstallRoot "data"
$LogsDir = Join-Path $InstallRoot "logs"
$BackupsDir = Join-Path $InstallRoot "backups"
$PidFile = Join-Path $DataDir "service.pid"
$EnvFile = Join-Path $DataDir "service.env"
$RequirementsHashFile = Join-Path $DataDir "requirements.sha256"
$StdoutLog = Join-Path $LogsDir "service.log"
$StderrLog = Join-Path $LogsDir "service-error.log"

function Write-Info([string]$Message) { Write-Host "[INFO] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[OK] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[WARN] $Message" -ForegroundColor Yellow }

function Assert-ManagedPath([string]$Path) {
    $full = Get-FullPath $Path
    $prefix = $InstallRoot + [System.IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe managed path was rejected: $full"
    }
    return $full
}

function Initialize-Directories {
    [void](New-Item -ItemType Directory -Force -Path $InstallRoot)
    foreach ($path in @($DataDir, $LogsDir, $BackupsDir)) {
        [void](New-Item -ItemType Directory -Force -Path (Assert-ManagedPath $path))
    }
}

function Remove-ManagedItem([string]$Path) {
    $safe = Assert-ManagedPath $Path
    if (Test-Path -LiteralPath $safe) {
        Remove-Item -LiteralPath $safe -Recurse -Force
    }
}

function Move-ManagedItem([string]$Source, [string]$Destination) {
    $safeSource = Assert-ManagedPath $Source
    $safeDestination = Assert-ManagedPath $Destination
    Move-Item -LiteralPath $safeSource -Destination $safeDestination -Force
}

function Protect-CurrentUserFile([string]$Path) {
    try {
        $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")
        $acl.AddAccessRule($rule)
        Set-Acl -LiteralPath $Path -AclObject $acl
    } catch {
        Write-Warn "Could not tighten service.env ACL: $($_.Exception.Message)"
    }
}

function New-ApiKey {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    return "ksv-$token"
}

function Get-Sha256File([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $sha.ComputeHash($stream)
        return ([BitConverter]::ToString($bytes)).Replace('-', '')
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Ensure-ServiceEnvironment {
    Initialize-Directories
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        $key = New-ApiKey
        $lines = @("CONSOLE_AUTH=local", "API_KEY=$key", "VERTEX_EXPRESS_API_KEY=")
        [System.IO.File]::WriteAllLines($EnvFile, $lines, (New-Object System.Text.UTF8Encoding($false)))
        Protect-CurrentUserFile $EnvFile
        Write-Ok "Created a private ksv- API key. It is available from the console home page."
    }
}

function Import-ServiceEnvironment {
    Ensure-ServiceEnvironment
    $allowed = @("CONSOLE_AUTH", "API_KEY", "VERTEX_EXPRESS_API_KEY")
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith('#')) { continue }
        $index = $line.IndexOf('=')
        if ($index -lt 1) { continue }
        $name = $line.Substring(0, $index).Trim()
        if ($allowed -notcontains $name) { continue }
        $value = $line.Substring($index + 1)
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
    [Environment]::SetEnvironmentVariable("STATE_DIR", $DataDir, "Process")
    [Environment]::SetEnvironmentVariable("CONSOLE_AUTH", "local", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONUNBUFFERED", "1", "Process")
    [Environment]::SetEnvironmentVariable("PYTHONIOENCODING", "utf-8", "Process")
}

function Get-VerifiedPython([string]$Candidate) {
    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $null }
    $candidatePath = $Candidate.Trim().Trim('"')
    try {
        $commandInfo = Get-Command $candidatePath -ErrorAction Stop
        $resolved = $commandInfo.Source
        if (-not $resolved) { $resolved = $commandInfo.Path }
        if (-not $resolved -or $resolved -match "\\WindowsApps\\") { return $null }
        $probe = & $resolved -c "import struct,sys; print(sys.executable); print('%d.%d' % sys.version_info[:2]); print(struct.calcsize('P') * 8)" 2>$null
        if ($LASTEXITCODE -ne 0 -or $probe.Count -lt 3) { return $null }
        if (($probe[1] -ne "3.11" -and $probe[1] -ne "3.12") -or $probe[2] -ne "64") { return $null }
        return (Get-FullPath ([string]$probe[0]))
    } catch { return $null }
}

function Find-Python {
    if ($env:VERTEX_PYTHON) {
        $override = Get-VerifiedPython $env:VERTEX_PYTHON
        if (-not $override) { throw "VERTEX_PYTHON must point to 64-bit CPython 3.11 or 3.12." }
        return $override
    }

    $managedPython = Join-Path $InstallRoot "python\python.exe"
    $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    foreach ($candidate in @($managedPython, $localPython)) {
        $found = Get-VerifiedPython $candidate
        if ($found) { return $found }
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py -and $py.Source -notmatch "\\WindowsApps\\") {
        foreach ($selector in @("-3.12", "-3.11")) {
            try {
                $path = & $py.Source $selector -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $found = Get-VerifiedPython ([string]$path[0])
                    if ($found) { return $found }
                }
            } catch { }
        }
    }

    foreach ($candidate in @("python.exe", "python3.exe")) {
        $found = Get-VerifiedPython $candidate
        if ($found) { return $found }
    }
    return $null
}

function Install-Python {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Automatic Python installation supports x64 Windows only. Install 64-bit CPython 3.11/3.12 manually and set VERTEX_PYTHON."
    }
    if ($env:VERTEX_NO_PYTHON_INSTALL -eq "1") {
        throw "No supported Python was found and automatic installation is disabled for this run."
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Info "Python is missing. Installing official CPython 3.12 with winget..."
        & $winget.Source install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        if ($LASTEXITCODE -eq 0) {
            $found = Find-Python
            if ($found) { return $found }
        }
        Write-Warn "winget did not provide a usable Python. Trying the official signed installer."
    }

    Initialize-Directories
    $downloadDir = Join-Path $InstallRoot ("python-download-" + [Guid]::NewGuid().ToString('N'))
    [void](New-Item -ItemType Directory -Path (Assert-ManagedPath $downloadDir))
    $installer = Join-Path $downloadDir "python-installer.exe"
    try {
        Write-Info "Downloading official CPython 3.12..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing -Uri $PythonInstallerUrl -OutFile $installer
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "The downloaded Python installer signature is invalid or not signed by Python Software Foundation."
        }
        $target = Assert-ManagedPath (Join-Path $InstallRoot "python")
        $arguments = "/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 PrependPath=0 SimpleInstall=1 TargetDir=`"$target`""
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Python installer exited with code $($process.ExitCode)." }
        $found = Get-VerifiedPython (Join-Path $target "python.exe")
        if (-not $found) { throw "Python installation completed but no supported interpreter was found." }
        return $found
    } finally {
        Remove-ManagedItem $downloadDir
    }
}

function Ensure-Python {
    $python = Find-Python
    if ($python) { return $python }
    return Install-Python
}

function Ensure-Venv([string]$Python) {
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    $verified = Get-VerifiedPython $venvPython
    if ($verified) { return $verified }
    if (Test-Path -LiteralPath $VenvDir) { Remove-ManagedItem $VenvDir }
    if (Test-Path -LiteralPath $RequirementsHashFile) { Remove-Item -LiteralPath $RequirementsHashFile -Force }
    Write-Info "Creating the private Python environment..."
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python virtual environment." }
    $verified = Get-VerifiedPython $venvPython
    if (-not $verified) { throw "The new Python virtual environment is not usable." }
    return $verified
}

function Install-Dependencies([string]$VenvPython, [string]$SourceRepo) {
    $requirements = Join-Path $SourceRepo "app\requirements.txt"
    if (-not (Test-Path -LiteralPath $requirements)) { throw "Missing app\requirements.txt in the downloaded source." }
    $hash = Get-Sha256File $requirements
    $oldHash = if (Test-Path -LiteralPath $RequirementsHashFile) { ([System.IO.File]::ReadAllText($RequirementsHashFile)).Trim() } else { "" }
    if ($hash -eq $oldHash) { return }
    Write-Info "Installing Python dependencies. This can take a few minutes the first time..."
    & $VenvPython -m pip install --disable-pip-version-check -r $requirements
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network and free disk space, then try again." }
    [System.IO.File]::WriteAllText($RequirementsHashFile, $hash, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-ArchiveEntryPath([string]$Root, [string]$EntryName) {
    $normalizedName = $EntryName.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    $target = [System.IO.Path]::GetFullPath((Join-Path $Root $normalizedName))
    $prefix = (Get-FullPath $Root) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path in source archive: $EntryName"
    }
}

function Get-StagedSource {
    Initialize-Directories
    $stage = Join-Path $InstallRoot ("staging-" + [Guid]::NewGuid().ToString('N'))
    $extract = Join-Path $stage "extract"
    [void](New-Item -ItemType Directory -Force -Path (Assert-ManagedPath $extract))
    $zipPath = Join-Path $stage "source.zip"
    try {
        if ($env:VERTEX_SOURCE_ZIP) {
            $sourceZip = Get-FullPath $env:VERTEX_SOURCE_ZIP
            if (-not (Test-Path -LiteralPath $sourceZip)) { throw "VERTEX_SOURCE_ZIP does not exist: $sourceZip" }
            Copy-Item -LiteralPath $sourceZip -Destination $zipPath
        } else {
            $sourceUrl = if ($env:VERTEX_SOURCE_URL) { $env:VERTEX_SOURCE_URL } else { $DefaultSourceUrl }
            Write-Info "Downloading the latest PlayerAlex/vertex2openai source..."
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -Uri $sourceUrl -OutFile $zipPath
        }

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
        try {
            foreach ($entry in $archive.Entries) { Test-ArchiveEntryPath $extract $entry.FullName }
        } finally { $archive.Dispose() }
        [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $extract)
        $roots = @(Get-ChildItem -LiteralPath $extract -Directory)
        if ($roots.Count -ne 1) { throw "The source ZIP must contain exactly one root directory." }
        $sourceRoot = $roots[0].FullName
        foreach ($required in @("app\main.py", "app\requirements.txt", "vertexModels.json")) {
            if (-not (Test-Path -LiteralPath (Join-Path $sourceRoot $required))) { throw "Downloaded source is incomplete: missing $required" }
        }
        return @{ Stage = $stage; Source = $sourceRoot }
    } catch {
        Remove-ManagedItem $stage
        throw
    }
}

function Get-ManagedProcess {
    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $raw = ([System.IO.File]::ReadAllText($PidFile)).Trim()
    $pidValue = 0
    if (-not [int]::TryParse($raw, [ref]$pidValue)) { return $null }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction Stop
        if (-not $process) { return $null }
        $expectedPython = Get-FullPath (Join-Path $VenvDir "Scripts\python.exe")
        $actualExe = if ($process.ExecutablePath) { Get-FullPath $process.ExecutablePath } else { "" }
        $cmd = [string]$process.CommandLine
        if ($actualExe -ne $expectedPython -or $cmd -notmatch "(?i)-m\s+uvicorn\s+main:app") { return $null }
        return $process
    } catch { return $null }
}

function Test-PortOpen {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch { return $false } finally { $client.Dispose() }
}

function Test-Healthy {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch { return $false }
}

function Open-Console {
    if ($env:VERTEX_NO_OPEN -ne "1") { Start-Process $ConsoleUrl }
    Write-Host "Console: $ConsoleUrl"
}

function Stop-Service {
    Initialize-Directories
    $process = Get-ManagedProcess
    if (-not $process) {
        if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
        Write-Info "The managed service is not running."
        return
    }
    Write-Info "Stopping $ProductName (PID $($process.ProcessId))..."
    # The Windows venv redirector may have a child Python process.
    & "$env:SystemRoot\System32\taskkill.exe" /PID $process.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Get-ManagedProcess)) { throw "Could not stop the managed service process tree." }
    try { Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction Stop } catch {
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    Write-Ok "Service stopped."
}

function Start-Service([bool]$OpenBrowser = $true) {
    Initialize-Directories
    if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "app\main.py"))) {
        Install-Or-Update $false
        return
    }
    $running = Get-ManagedProcess
    if ($running) {
        if (Test-Healthy) {
            Write-Ok "Service is already running (PID $($running.ProcessId))."
            if ($OpenBrowser) { Open-Console }
            return
        }
        throw "The managed process exists but health check failed. Run vertex.bat logs, then vertex.bat restart."
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    if (Test-PortOpen) { throw "Port $Port is already in use by another program. Close it or stop the old service, then try again." }

    $python = Ensure-Python
    $venvPython = Ensure-Venv $python
    Install-Dependencies $venvPython $RepoDir
    Import-ServiceEnvironment
    $appDir = Join-Path $RepoDir "app"
    $arguments = @("-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$Port")
    Write-Info "Starting $ProductName..."
    $process = Start-Process -FilePath $venvPython -ArgumentList $arguments -WorkingDirectory $appDir -WindowStyle Hidden -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog -PassThru
    [System.IO.File]::WriteAllText($PidFile, [string]$process.Id, (New-Object System.Text.UTF8Encoding($false)))
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Healthy) {
            Write-Ok "Service started (PID $($process.Id))."
            if ($OpenBrowser) { Open-Console }
            return
        }
        if ($process.HasExited) { break }
    }
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        try { Wait-Process -Id $process.Id -Timeout 5 -ErrorAction SilentlyContinue } catch { }
    }
    if (Test-Path -LiteralPath $PidFile) { Remove-Item -LiteralPath $PidFile -Force }
    Write-Host "Recent error log:" -ForegroundColor Red
    if (Test-Path -LiteralPath $StderrLog) { Get-Content -LiteralPath $StderrLog -Tail 30 }
    throw "Service did not become healthy. Run vertex.bat logs for details."
}

function Install-Or-Update([bool]$IsUpdate) {
    Initialize-Directories
    $staged = Get-StagedSource
    $stagePath = $staged.Stage
    $sourcePath = $staged.Source
    $backup = $null
    $venvPython = $null
    $hadRepo = Test-Path -LiteralPath $RepoDir
    $wasRunning = [bool](Get-ManagedProcess)
    $sourceReplaced = $false
    try {
        $python = Ensure-Python
        $venvPython = Ensure-Venv $python
        if ($hadRepo) { Stop-Service }
        if ($hadRepo) {
            $backup = Join-Path $BackupsDir ("repo-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [Guid]::NewGuid().ToString('N').Substring(0, 6))
            Move-ManagedItem $RepoDir $backup
            $sourceReplaced = $true
        }
        Move-ManagedItem $sourcePath $RepoDir
        $sourceReplaced = $true
        Install-Dependencies $venvPython $RepoDir
        Ensure-ServiceEnvironment
        Start-Service $true
        if ($IsUpdate) { Write-Ok "Update completed. Settings and API keys were preserved." } else { Write-Ok "Installation completed." }
    } catch {
        $failure = $_
        if ($sourceReplaced) {
            Write-Warn "Installation/update failed. Restoring the previous managed source..."
            try {
                if (Test-Path -LiteralPath $RepoDir) { Remove-ManagedItem $RepoDir }
                if ($backup -and (Test-Path -LiteralPath $backup)) {
                    Move-ManagedItem $backup $RepoDir
                    if ($venvPython -and (Test-Path -LiteralPath $venvPython)) {
                        if (Test-Path -LiteralPath $RequirementsHashFile) { Remove-Item -LiteralPath $RequirementsHashFile -Force }
                        Install-Dependencies $venvPython $RepoDir
                        if ($wasRunning) { Start-Service $false }
                    }
                }
            } catch {
                Write-Warn "Automatic rollback also had a problem: $($_.Exception.Message)"
            }
        } else {
            Write-Warn "Installation/update failed before source replacement; the existing source was preserved."
            if ($hadRepo -and $wasRunning -and (Test-Path -LiteralPath $RepoDir) -and -not (Get-ManagedProcess)) {
                try { Start-Service $false } catch { Write-Warn "Could not restart the previous service: $($_.Exception.Message)" }
            }
        }
        throw $failure
    } finally {
        if (Test-Path -LiteralPath $stagePath) { Remove-ManagedItem $stagePath }
    }
}

function Run-Console {
    Initialize-Directories
    if (-not (Test-Path -LiteralPath (Join-Path $RepoDir "app\main.py"))) { Install-Or-Update $false }
    Stop-Service
    if (Test-PortOpen) { throw "Port $Port is already in use by another program." }
    $python = Ensure-Python
    $venvPython = Ensure-Venv $python
    Install-Dependencies $venvPython $RepoDir
    Import-ServiceEnvironment
    if (-not ("VertexConsoleProcess" -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot "ConsoleProcess.cs")
    }
    $owned = $null
    try {
        $owned = New-Object VertexConsoleProcess($venvPython, (Join-Path $RepoDir "app"), $StdoutLog, $StderrLog)
        [System.IO.File]::WriteAllText($PidFile, [string]$owned.ProcessId, (New-Object System.Text.UTF8Encoding($false)))
        Write-Info "Starting $ProductName. Closing this window stops the service."
        $healthy = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            if ($owned.HasExited) { break }
            if (Test-Healthy) { $healthy = $true; break }
        }
        if (-not $healthy) {
            Show-Logs $false
            throw "Service did not become healthy. Review the log above."
        }
        Write-Ok "Service started (PID $($owned.ProcessId)). Close this window or press Ctrl+C to stop."
        Open-Console
        Show-Logs $true $owned
    } finally {
        if ($owned) {
            $owned.Dispose()
            if ((Test-Path -LiteralPath $PidFile) -and ([System.IO.File]::ReadAllText($PidFile)).Trim() -eq [string]$owned.ProcessId) {
                Remove-Item -LiteralPath $PidFile -Force
            }
        }
    }
}

function Show-Status {
    $process = Get-ManagedProcess
    if ($process) {
        if (Test-Healthy) { Write-Ok "Running: PID $($process.ProcessId), health check passed." }
        else { Write-Warn "Running: PID $($process.ProcessId), but health check failed." }
    } else {
        Write-Info "Not running."
        if (Test-PortOpen) { Write-Warn "Port $Port is occupied by another program." }
    }
    Write-Host "Install directory: $InstallRoot"
    Write-Host "Console: $ConsoleUrl"
}

function Show-Logs([bool]$Follow, $OwnedProcess = $null) {
    Initialize-Directories
    if (-not (Test-Path -LiteralPath $StdoutLog) -and -not (Test-Path -LiteralPath $StderrLog)) {
        Write-Info "No service logs yet."
        return
    }
    $readers = @()
    try {
        foreach ($path in @($StdoutLog, $StderrLog)) {
            if (-not (Test-Path -LiteralPath $path)) { continue }
            $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
            $reader = New-Object System.IO.StreamReader($stream, [System.Text.Encoding]::UTF8, $true)
            $readers += $reader
            Write-Host "--- $([System.IO.Path]::GetFileName($path)) ---"
            # Keep the reader open so messages arriving after the initial snapshot are not lost.
            $lines = $reader.ReadToEnd() -split "`n"
            $lines | Select-Object -Last 100 | ForEach-Object { Write-Host $_ }
        }
        if (-not $Follow) { return }
        while ($true) {
            foreach ($reader in $readers) {
                if ($reader.BaseStream.Length -lt $reader.BaseStream.Position) {
                    $reader.DiscardBufferedData()
                    [void]$reader.BaseStream.Seek(0, [System.IO.SeekOrigin]::Begin)
                }
                $chunk = $reader.ReadToEnd()
                if ($chunk) { Write-Host -NoNewline $chunk }
            }
            if ($OwnedProcess -and $OwnedProcess.HasExited) { break }
            Start-Sleep -Milliseconds 250
        }
    } finally {
        foreach ($reader in $readers) { $reader.Dispose() }
    }
}

try {
    switch ($Command) {
        "run" { Run-Console }
        "start" { Start-Service $true }
        "stop" { Stop-Service }
        "restart" { Stop-Service; Start-Service $true }
        "status" { Show-Status }
        "logs" { Show-Logs ($CommandArgs -contains "-f") }
        "update" { Install-Or-Update $true }
        "open" {
            if (-not (Test-Healthy)) { throw "Service is not healthy. Run vertex.bat start first." }
            Open-Console
        }
    }
    exit 0
} catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
