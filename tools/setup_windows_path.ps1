# Sync Wordly dependencies onto the Windows User PATH (safe to re-run).
# Run from PowerShell:  powershell -ExecutionPolicy Bypass -File tools\setup_windows_path.ps1

$ErrorActionPreference = "Stop"

function Find-Executable([string]$Name) {
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Find-WinGetBin([string]$Pattern, [string]$ExeName) {
    $packages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if (-not (Test-Path $packages)) { return $null }
    $hit = Get-ChildItem -Path $packages -Recurse -Filter $ExeName -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -match $Pattern } |
        Select-Object -First 1
    if ($hit) { return $hit.DirectoryName }
    return $null
}

$pathsToAdd = [System.Collections.Generic.List[string]]::new()

function Add-PathDir([string]$Dir) {
    if (-not $Dir) { return }
    $resolved = [System.IO.Path]::GetFullPath($Dir)
    if (Test-Path $resolved) {
        if (-not $pathsToAdd.Contains($resolved)) {
            $pathsToAdd.Add($resolved) | Out-Null
        }
    }
}

# Python 3.12
$pyRoot = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
Add-PathDir $pyRoot
Add-PathDir (Join-Path $pyRoot "Scripts")
Add-PathDir (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher")

# FFmpeg (WinGet or common locations)
$ffmpegBin = Find-WinGetBin "Gyan\.FFmpeg" "ffmpeg.exe"
if (-not $ffmpegBin) { $ffmpegBin = Find-WinGetBin "ffmpeg" "ffmpeg.exe" }
Add-PathDir $ffmpegBin
Add-PathDir "C:\ffmpeg\bin"
Add-PathDir "C:\Program Files\ffmpeg\bin"

# WinGet shim links (optional)
Add-PathDir (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links")

$currentUserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$existing = @()
if ($currentUserPath) {
    $existing = $currentUserPath -split ";" | Where-Object { $_ -and $_.Trim() }
}

$merged = [System.Collections.Generic.List[string]]::new()
foreach ($p in $pathsToAdd) {
    if (-not $merged.Contains($p)) { $merged.Add($p) | Out-Null }
}
foreach ($p in $existing) {
    $norm = [System.IO.Path]::GetFullPath($p)
    if (-not $merged.Contains($norm)) { $merged.Add($norm) | Out-Null }
}

$newPath = ($merged -join ";")
[Environment]::SetEnvironmentVariable("Path", $newPath, "User")

# Refresh current session
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + $newPath

Write-Host ""
Write-Host "Wordly Windows PATH updated (User scope)." -ForegroundColor Green
Write-Host ""
Write-Host "Verification (restart terminal if a tool still shows missing):"
$tools = @(
    @{ Label = "python";  Cmd = "python --version" },
    @{ Label = "pip";     Cmd = "pip --version" },
    @{ Label = "ffmpeg";  Cmd = "ffmpeg -version" },
    @{ Label = "ffprobe"; Cmd = "ffprobe -version" }
)
foreach ($t in $tools) {
    Write-Host ("  {0,-8} " -f $t.Label) -NoNewline
    try {
        $out = Invoke-Expression "$($t.Cmd) 2>&1" | Select-Object -First 1
        if ($out) {
            Write-Host $out -ForegroundColor Green
        } else {
            Write-Host "not found" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "not found" -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "Project venv (Wordly app):"
$root = Split-Path $PSScriptRoot -Parent
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -m pip install -r (Join-Path $root "requirements.txt") -q
    $ytdlp = & $venvPy -c "import yt_dlp; print(yt_dlp.version.__version__)" 2>$null
    Write-Host "  yt-dlp (venv)  $ytdlp" -ForegroundColor Green
    Write-Host "  PySide6 (venv) installed" -ForegroundColor Green
} else {
    Write-Host "  .venv missing - run: python -m venv .venv" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Optional (install manually): Filmora 14.2.9"
Write-Host "Restart Cursor / Git Bash so PATH changes apply everywhere."
