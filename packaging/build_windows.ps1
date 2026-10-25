# ----------------------------------------------------------------------------------
# JARVIS - Windows build script.
#
# Produces, in dist/:
#   1. dist\JARVIS\JARVIS.exe   - the application (onedir build)
#   2. dist\JARVIS-Setup-<ver>.exe - an Inno Setup installer a non-technical user
#                                    can double-click (desktop shortcut included)
#
# Requirements on the build machine:
#   * Windows 10/11, Python 3.10-3.12 on PATH (or use -PythonPath)
#   * Inno Setup 6 (optional; without it the script stops after step 2 and the
#     dist\JARVIS folder is still a complete, runnable install)
#
# Run from a normal PowerShell window:
#   powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
# ----------------------------------------------------------------------------------
[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$SkipInstaller,
    [switch]$SkipVenv,
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Get-Item "$PSScriptRoot\..").FullName
Push-Location $RepoRoot
try {
    # ------------------------------------------------------------------ python
    $python = $PythonPath
    if (-not $python) {
        if (-not $SkipVenv -and (Test-Path ".build\venv\Scripts\python.exe")) {
            $python = ".build\venv\Scripts\python.exe"
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            $python = "python"
        } else {
            throw "Python was not found. Install Python 3.11 from python.org, or pass -PythonPath."
        }
    }

    Write-Host "== JARVIS build ==" -ForegroundColor Cyan
    Write-Host ("repo   : {0}" -f $RepoRoot)
    Write-Host ("python : {0}" -f $python)
    & $python --version
    if ($LASTEXITCODE -ne 0) { throw "Python did not run." }

    if (-not $SkipVenv -and $python -ne ".build\venv\Scripts\python.exe") {
        Write-Host "-- creating a clean build venv (.build\venv)" -ForegroundColor Cyan
        & $python -m venv .build\venv
        $python = ".build\venv\Scripts\python.exe"
    }

    Write-Host "-- upgrading pip" -ForegroundColor Cyan
    & $python -m pip install --upgrade pip wheel setuptools
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }

    Write-Host "-- installing runtime dependencies" -ForegroundColor Cyan
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Installing requirements.txt failed." }

    Write-Host "-- installing PyInstaller" -ForegroundColor Cyan
    & $python -m pip install "pyinstaller>=6.3,<7"
    if ($LASTEXITCODE -ne 0) { throw "Installing PyInstaller failed." }

    # ------------------------------------------------------------------- icon
    Write-Host "-- drawing the application icon (stdlib only)" -ForegroundColor Cyan
    & $python packaging\make_icon.py
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

    # -------------------------------------------------------------- quick test
    Write-Host "-- smoke test: jarvis doctor --no-speech" -ForegroundColor Cyan
    $env:JARVIS_DEMO = "1"
    & $python -m jarvis.cli doctor --no-speech
    Remove-Item Env:JARVIS_DEMO -ErrorAction SilentlyContinue
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "doctor exited $LASTEXITCODE (usually: Ollama not installed on this build machine). Continuing - the exe does not need Ollama to build."
    }

    # -------------------------------------------------------------- pyinstaller
    Write-Host "-- freezing JARVIS (this takes a few minutes)" -ForegroundColor Cyan
    & $python -m PyInstaller packaging\jarvis.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $exePath = "dist\JARVIS\JARVIS.exe"
    if (-not (Test-Path $exePath)) { throw "Build finished but $exePath is missing." }
    $sizeMb = [math]::Round((Get-Item $exePath).Length / 1MB, 1)
    Write-Host ("-- built {0} ({1} MB)" -f $exePath, $sizeMb) -ForegroundColor Green

    if (-not $Version) {
        $Version = & $python -c "import jarvis; print(jarvis.__version__)"
    }

    # ------------------------------------------------------------------ inno
    if ($SkipInstaller) {
        Write-Host "-- skipping the installer (-SkipInstaller)" -ForegroundColor Yellow
    } else {
        $iscc = $null
        foreach ($candidate in @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe",
            "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if (Test-Path $candidate) { $iscc = $candidate; break }
        }
        if (-not $iscc) {
            Write-Warning "Inno Setup 6 was not found; skipping the installer. dist\JARVIS is still a complete install - copy that folder anywhere."
        } else {
            Write-Host "-- building the installer with Inno Setup" -ForegroundColor Cyan
            & $iscc /DAppVersion=$Version /O"dist" /F"JARVIS-Setup-$Version" packaging\installer.iss
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }
            Write-Host ("-- installer: dist\JARVIS-Setup-{0}.exe" -f $Version) -ForegroundColor Green
        }
    }

    Write-Host ""
    Write-Host "Done. Artefacts:" -ForegroundColor Cyan
    Get-ChildItem dist | Select-Object Name, Length | Format-Table -AutoSize
}
finally {
    Pop-Location
}
