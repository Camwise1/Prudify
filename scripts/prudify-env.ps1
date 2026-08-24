# Prepare a PowerShell session for running Prudify natively on Windows.
#
# Dot-source it (note the leading dot and space) so the variables persist in
# your shell:
#
#     . .\scripts\prudify-env.ps1
#
# Then `prudify serve`, `prudify clean ...`, and so on just work.
#
# Run this in a NORMAL PowerShell window, not an Administrator one: mapped
# network drives and cached share credentials do not cross the UAC boundary,
# so an elevated shell cannot see them.

param(
    [string]$Root       = "E:\prudify",
    [string]$Python     = "E:\Apps\Python311\python.exe",
    [string]$FFmpegDir  = "E:\Apps\FFmpeg\bin",
    [int]   $Threads    = 6
)

$ErrorActionPreference = "Stop"

# Keep everything off C:. pip unpacks into TEMP and faster-whisper caches
# models under HF_HOME; both default to C: and there is no room there.
$env:TMP              = "$Root\Temp"
$env:TEMP             = "$Root\Temp"
$env:PIP_CACHE_DIR    = "$Root\Temp\pip-cache"
$env:HF_HOME          = "E:\WhisperModels"
$env:PRUDIFY_DATA_DIR = "$Root\Data"
$env:OMP_NUM_THREADS  = "$Threads"

New-Item -ItemType Directory -Force -Path $env:TMP, $env:PRUDIFY_DATA_DIR, $env:HF_HOME |
    Out-Null

# Locate ffmpeg. Some builds put the binaries in \bin, others at the top level.
$ffmpeg  = Join-Path $FFmpegDir "ffmpeg.exe"
$ffprobe = Join-Path $FFmpegDir "ffprobe.exe"
if (-not (Test-Path $ffmpeg)) {
    $parent = Split-Path $FFmpegDir -Parent
    if (Test-Path (Join-Path $parent "ffmpeg.exe")) {
        $ffmpeg  = Join-Path $parent "ffmpeg.exe"
        $ffprobe = Join-Path $parent "ffprobe.exe"
    }
}
if (Test-Path $ffmpeg) {
    $env:PRUDIFY_FFMPEG  = $ffmpeg
    $env:PRUDIFY_FFPROBE = $ffprobe
    $env:PATH = "$(Split-Path $ffmpeg -Parent);$env:PATH"
} else {
    Write-Warning "ffmpeg not found under $FFmpegDir -- pass -FFmpegDir <path>"
}

# Activate the virtual environment, creating it on first run.
$venv = Join-Path $Root "venv"
if (-not (Test-Path (Join-Path $venv "Scripts\Activate.ps1"))) {
    Write-Host "Creating virtual environment at $venv ..."
    & $Python -m venv $venv
}
& (Join-Path $venv "Scripts\Activate.ps1")

Write-Host ""
Write-Host "Prudify environment ready" -ForegroundColor Green
Write-Host "  python   $(python --version 2>&1)"
Write-Host "  ffmpeg   $(if ($env:PRUDIFY_FFMPEG) { $env:PRUDIFY_FFMPEG } else { 'NOT FOUND' })"
Write-Host "  data     $env:PRUDIFY_DATA_DIR"
Write-Host "  models   $env:HF_HOME"
Write-Host "  threads  $env:OMP_NUM_THREADS"
Write-Host ""
if (-not (Get-Command prudify -ErrorAction SilentlyContinue)) {
    Write-Host "Prudify is not installed in this venv yet. Run:" -ForegroundColor Yellow
    Write-Host '    pip install -e ".[whisper]"'
    Write-Host ""
}
