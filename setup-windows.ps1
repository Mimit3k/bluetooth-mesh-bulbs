#Requires -Version 5.1
<#
.SYNOPSIS
    Create the local Python environment for Mesh Bulbs on Windows 10/11.

.DESCRIPTION
    Finds a 64-bit CPython 3.12 or newer, preferring the newest installed
    version, creates .venv next to this script, and installs the runtime
    dependencies. No administrator rights are needed and nothing outside this
    folder (except the venv) is modified.

    The search covers the Python launcher (py -0p), python/python3 commands on
    PATH, the per-user and system Python install folders, and Python versions
    managed by uv. Python 3.15 and newer are accepted automatically; there is
    no hard-coded version list.

    If .venv already exists, the script offers to remove and recreate it so the
    environment matches the selected Python and has no stale packages. If no
    suitable Python is found, the error explains how to install one with uv or
    from python.org.

    Use -Python to force one interpreter, or -PythonVersion to prefer a
    major.minor version when several are installed.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1

.EXAMPLE
    .\setup-windows.ps1 -Python "C:\Python313\python.exe"

.EXAMPLE
    .\setup-windows.ps1 -PythonVersion 3.13

.EXAMPLE
    .\setup-windows.ps1 -PythonVersion 3.15
#>
[CmdletBinding()]
param(
    [string]$Python = "",
    [string]$PythonVersion = ""
)

$ErrorActionPreference = "Stop"
# The Python probes below deliberately exit non-zero; do not turn that into a
# terminating error on PowerShell versions that support native error handling.
$PSNativeCommandUseErrorActionPreference = $false
Set-Location -LiteralPath $PSScriptRoot

$script:candidatePaths = @()
$script:probeNotes = @{}

function Resolve-PythonPath {
    param([string]$Spec)

    if (-not $Spec) {
        return $null
    }
    $command = Get-Command -Name $Spec -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($command -and $command.CommandType -eq "Application") {
        return $command.Path
    }
    if (Test-Path -LiteralPath $Spec) {
        return (Resolve-Path -LiteralPath $Spec).Path
    }
    return $null
}

function Get-PythonInfo {
    param([string]$Path)

    if (-not $Path) {
        return $null
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    # Skip GUI-only interpreters. A Microsoft Store install is kept: when
    # Store Python is installed its execution alias under WindowsApps is the
    # only python.exe on PATH, and probing it runs the real interpreter. With
    # no Store Python the alias is a stub, the probe fails, and the candidate is
    # reported as unrunnable below.
    if ($Path -match "(?i)(^|[\\/])pythonw[^\\/]*\.exe$") {
        return $null
    }

    # One line, and no double quotes: Windows PowerShell 5.1 does not escape
    # quotes when it passes an argument to a native command, so the previous
    # multi-line probe arrived as broken source and every interpreter failed
    # with a SyntaxError before it could report its version.
    $probe = "import json,platform,sys;print(json.dumps({'major':sys.version_info[0],'minor':sys.version_info[1],'micro':sys.version_info[2],'bits':64 if sys.maxsize>2**32 else 32,'arch':platform.machine(),'executable':sys.executable}))"

    # With ErrorActionPreference=Stop, Windows PowerShell 5.1 turns a native
    # command's stderr into a terminating error even when it is redirected, so
    # relax it for the probe and judge the interpreter by its exit code.
    $previous = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $raw = (& $Path -c $probe 2>$null | Out-String).Trim()
        $exitCode = $LASTEXITCODE
    }
    catch {
        $script:probeNotes[$Path] = $_.Exception.Message
        return $null
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($exitCode -ne 0 -or -not $raw) {
        $script:probeNotes[$Path] = "probe exited with code $exitCode"
        return $null
    }

    try {
        $data = $raw | ConvertFrom-Json
    }
    catch {
        $script:probeNotes[$Path] = "probe did not return JSON"
        return $null
    }

    $major = [int]$data.major
    $minor = [int]$data.minor
    $micro = [int]$data.micro
    # Prefer the interpreter the probe actually ran. For a Microsoft Store
    # alias that is the real package python.exe, which can build the venv.
    $exe = [string]$data.executable
    if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        $exe = $Path
    }
    # A windowed interpreter has no console and does not populate the venv
    # synchronously, so setup would move on before .venv\Scripts\python.exe
    # exists. Prefer the console python.exe next to it.
    if ($exe -match "(?i)(^|[\\/])pythonw[^\\/]*\.exe$") {
        $console = Join-Path (Split-Path -Parent $exe) "python.exe"
        if (Test-Path -LiteralPath $console -PathType Leaf) {
            $exe = $console
        }
        else {
            $script:probeNotes[$Path] =
                "windowed interpreter with no console python.exe next to it"
            return $null
        }
    }
    return [pscustomobject]@{
        Path       = $exe
        Executable = $exe
        Major      = $major
        Minor      = $minor
        Micro      = $micro
        Bits       = [int]$data.bits
        Arch       = [string]$data.arch
        Version    = [version]("$major.$minor.$micro")
        Display    = "$major.$minor.$micro"
    }
}

function Add-PythonCandidate {
    param([string]$Path)

    if (-not $Path) {
        return
    }
    $resolved = Resolve-PythonPath $Path
    if (-not $resolved) {
        return
    }
    if ($script:candidatePaths -notcontains $resolved) {
        $script:candidatePaths += $resolved
    }
}

# The launcher knows about every registered Python, including versions that
# are not on PATH and versions newer than this script.
if (Get-Command -Name "py" -ErrorAction SilentlyContinue) {
    $listing = & py -0p 2>$null
    if ($LASTEXITCODE -eq 0) {
        foreach ($line in @($listing)) {
            if ($line -match "(?i)-V:\S+\s+\*?\s*(.+?\.exe)\b") {
                Add-PythonCandidate $Matches[1]
            }
        }
    }
}

# python.exe and any versioned python3.x.exe on PATH.
Get-Command -Name "python*" -CommandType Application -ErrorAction SilentlyContinue |
    ForEach-Object { Add-PythonCandidate $_.Path }

# where.exe lists every match on PATH, including shims that Get-Command
# collapses into a single name.
if (Get-Command -Name "where.exe" -ErrorAction SilentlyContinue) {
    foreach ($name in @("python.exe", "python3.exe")) {
        foreach ($line in @(& where.exe $name 2>$null)) {
            $candidate = "$line".Trim()
            if ($candidate) {
                Add-PythonCandidate $candidate
            }
        }
    }
}

# Standard per-user and system install locations.
$patterns = @()
if ($env:LOCALAPPDATA) {
    $patterns += Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"
}
if ($env:ProgramFiles) {
    $patterns += Join-Path $env:ProgramFiles "Python*\python.exe"
}
if (${env:ProgramFiles(x86)}) {
    $patterns += Join-Path ${env:ProgramFiles(x86)} "Python*\python.exe"
}
$patterns += "C:\Python*\python.exe"

# uv-managed Python installations. uv downloads into a per-user directory by
# default and can also be pointed at an app-local folder with
# UV_PYTHON_INSTALL_DIR. Both layouts are checked.
$uvRoots = @()
if ($env:UV_PYTHON_INSTALL_DIR) {
    $uvRoots += $env:UV_PYTHON_INSTALL_DIR
}
# An app-local uv runtime, as shown in the README.
$uvRoots += (Join-Path $PSScriptRoot ".python")
if ($env:LOCALAPPDATA) {
    $uvRoots += (Join-Path $env:LOCALAPPDATA "uv\python")
}
if ($env:APPDATA) {
    $uvRoots += (Join-Path $env:APPDATA "uv\python")
}
foreach ($root in $uvRoots) {
    $patterns += (Join-Path $root "cpython-*\python.exe")
    $patterns += (Join-Path $root "cpython-*\install\python.exe")
}

foreach ($pattern in $patterns) {
    Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue |
        ForEach-Object { Add-PythonCandidate $_.FullName }
}

$requested = $null
if ($PythonVersion) {
    if ($PythonVersion -notmatch "^\d+\.\d+$") {
        throw "-PythonVersion must look like 3.13 (major.minor)."
    }
    $requested = [version]$PythonVersion
}

if ($Python) {
    $resolved = Resolve-PythonPath $Python
    if (-not $resolved) {
        throw "Could not find '$Python'. Pass the full path to python.exe."
    }
    $chosen = Get-PythonInfo $resolved
    if ($null -eq $chosen) {
        throw "'$Python' did not run as a Python interpreter."
    }
    if ($chosen.Major -lt 3 -or ($chosen.Major -eq 3 -and $chosen.Minor -lt 12)) {
        throw "Python $($chosen.Display) is too old; 3.12 or newer is required."
    }
    if ($chosen.Bits -ne 64) {
        throw "Python $($chosen.Display) is 32-bit; install 64-bit Python."
    }
    if ($requested -and ($chosen.Major -ne $requested.Major -or $chosen.Minor -ne $requested.Minor)) {
        throw "The interpreter is Python $($chosen.Display), not $PythonVersion."
    }
}
else {
    $found = @()
    $rejected = @()
    foreach ($path in $script:candidatePaths) {
        $info = Get-PythonInfo $path
        if ($null -eq $info) {
            $note = $script:probeNotes[$path]
            if ($note) {
                $rejected += "$path (could not run: $note)"
            }
            else {
                $rejected += "$path (could not run)"
            }
            continue
        }
        $label = "$($info.Display) $($info.Bits)-bit ($($info.Arch)) - $path"
        if ($info.Major -lt 3 -or ($info.Major -eq 3 -and $info.Minor -lt 12)) {
            $rejected += "$label (needs 3.12+)"
            continue
        }
        if ($info.Bits -ne 64) {
            $rejected += "$label (needs 64-bit)"
            continue
        }
        $found += $info
    }

    $allUsable = $found
    if ($requested) {
        $found = @($found | Where-Object {
            $_.Major -eq $requested.Major -and $_.Minor -eq $requested.Minor
        })
        if (-not $found) {
            $available = ($allUsable | ForEach-Object { $_.Display } | Sort-Object -Unique) -join ", "
            if (-not $available) {
                $available = "none"
            }
            throw "No 64-bit Python $PythonVersion found. Usable versions: $available"
        }
    }

    if (-not $found) {
        $details = ""
        if ($rejected.Count -gt 0) {
            $details = "`n`nFound but rejected:`n  " + ($rejected -join "`n  ")
        }
        $seen = Get-Command -Name "python" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($seen -and $seen.Source) {
            $details += "`n`nPowerShell resolves 'python' to: $($seen.Source)"
        }
        throw @"
No suitable 64-bit Python 3.12 or newer was found.$details

Install Python with uv (no administrator rights needed):
  1. Install uv:
       winget install astral-sh.uv
     If winget is unavailable:
       powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
  2. Open a new PowerShell window and install Python:
       uv python install 3.13
  3. Run this setup script again.

Alternatively install the current 64-bit Python from
https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".
Do not use the Microsoft Store alias.

If Python is installed in a non-standard place, pass it explicitly:
    .\setup-windows.ps1 -Python "C:\Path\To\python.exe"

If several versions are installed and the newest has no PyQt6 wheel yet:
    .\setup-windows.ps1 -PythonVersion 3.13
"@
    }

    $chosen = $found |
        Sort-Object -Property Version -Descending |
        Select-Object -First 1
}

Write-Host "Using Python $($chosen.Display) ($($chosen.Bits)-bit, $($chosen.Arch))"
Write-Host "  $($chosen.Executable)"

$venvDir = Join-Path $PSScriptRoot ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$recreateVenv = $true

if (Test-Path -LiteralPath $venvDir -PathType Container) {
    $venvInfo = $null
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $venvInfo = Get-PythonInfo $venvPython
    }
    if ($null -eq $venvInfo) {
        Write-Host "An existing .venv was found, but it is incomplete or its Python no longer runs."
    }
    else {
        Write-Host "An existing .venv was found (Python $($venvInfo.Display), $($venvInfo.Bits)-bit)."
    }
    Write-Host "Setup is normally run only when installing or updating. Recreating the"
    Write-Host "environment avoids stale packages and mismatched Python versions."
    $answer = Read-Host "Remove .venv and create a fresh environment? [Y/n]"
    if ($answer -match "^\s*[Nn]") {
        $recreateVenv = $false
    }
}

if ($recreateVenv) {
    if (Test-Path -LiteralPath $venvDir) {
        Write-Host "Removing existing .venv ..."
        try {
            Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction Stop
        }
        catch {
            throw "Could not remove .venv. Close Mesh Bulbs and any terminal using that environment, then run setup again. $_"
        }
    }
    Write-Host "Creating .venv ..."
    & $chosen.Executable -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create .venv with $($chosen.Executable)."
    }
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "Python $($chosen.Display) did not create .venv\Scripts\python.exe. Remove .venv and try again, passing a console interpreter with -Python if it persists."
    }
}
else {
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        throw "The existing .venv is incomplete. Remove .venv and run this script again."
    }
    $venvInfo = Get-PythonInfo $venvPython
    if ($null -eq $venvInfo) {
        throw "The existing .venv is broken. Remove .venv and run this script again."
    }
    if ($venvInfo.Major -lt 3 -or ($venvInfo.Major -eq 3 -and $venvInfo.Minor -lt 12)) {
        throw "The existing .venv uses Python $($venvInfo.Display); remove .venv and run this script again."
    }
    if ($venvInfo.Bits -ne 64) {
        throw "The existing .venv is 32-bit; remove .venv and run this script again."
    }
    Write-Host "Reusing existing .venv (Python $($venvInfo.Display)) ..."
}

Write-Host "Installing dependencies ..."
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not upgrade pip."
}
& $venvPython -m pip install -r "requirements-windows.txt"
if ($LASTEXITCODE -ne 0) {
    throw "Could not install dependencies."
}

& $venvPython -c "import bleak, cryptography, PyQt6; print('Dependencies import correctly.')"
if ($LASTEXITCODE -ne 0) {
    throw "Dependencies were installed but do not import; see the error above."
}

Write-Host ""
Write-Host "Done. Start the app with:  .\bulbgui.cmd"
Write-Host "CLI:                       .\bulbctl.cmd devices"
Write-Host ""
Write-Host "Mesh keys live in %LOCALAPPDATA%\bulbctl\state.json."
Write-Host "Move an existing mesh with the GUI Export/Import buttons, or copy state.json there."
Write-Host "Using two PCs? Give the second one its own address: .\bulbctl.cmd controller-address 0x7fff"
