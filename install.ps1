# HireLoop installer for Windows.
#
#   irm https://raw.githubusercontent.com/RahulSankhla312/hireloop/main/install.ps1 | iex
#
# Installs HireLoop with pipx (its own isolated Python environment), sets up the
# browser it fills forms with, adds a "HireLoop" desktop shortcut, and opens it.
# Your profile, jobs and settings are kept in %USERPROFILE%\.hireloop.

$ErrorActionPreference = "Stop"
$Source = "git+https://github.com/RahulSankhla312/hireloop.git"

function Say($text) { Write-Host "==> $text" -ForegroundColor Cyan }

# 1. Python 3.11 or newer.
$python = $null
foreach ($candidate in @("py -3.13", "py -3.12", "py -3.11", "python")) {
    $exe, $arg = $candidate.Split(" ", 2)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $ok = & $exe $arg -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        if ($ok -eq "True") { $python = $candidate; break }
    } catch { }
}
if (-not $python) {
    Write-Host "HireLoop needs Python 3.11 or newer." -ForegroundColor Yellow
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Write-Host "or run:  winget install -e --id Python.Python.3.12"
    Write-Host "then open a new PowerShell window and run this installer again."
    exit 1
}
$exe, $arg = $python.Split(" ", 2)
Say "Using $(& $exe $arg --version)"

# 2. pipx, which keeps HireLoop's packages separate from everything else.
Say "Installing pipx"
& $exe $arg -m pip install --user --upgrade --quiet pipx
& $exe $arg -m pipx ensurepath | Out-Null

# 3. HireLoop itself.
Say "Installing HireLoop (a few minutes the first time)"
& $exe $arg -m pipx install --force $Source

# 4. The browser used to fill application forms. HireLoop falls back to your
#    installed Chrome or Edge if this step fails, so it is not fatal.
Say "Setting up the form-filling browser"
try {
    $venvs = (& $exe $arg -m pipx environment --value PIPX_LOCAL_VENVS).Trim()
    & (Join-Path $venvs "hireloop\Scripts\python.exe") -m playwright install chromium
} catch {
    Write-Host "Skipped - HireLoop will use your installed Chrome or Edge instead." -ForegroundColor Yellow
}

# 5. A desktop shortcut that starts the dashboard.
$bin = (& $exe $arg -m pipx environment --value PIPX_BIN_DIR).Trim()
$hireloop = Join-Path $bin "hireloop.exe"
try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "HireLoop.lnk"))
    $link.TargetPath = $hireloop
    $link.Arguments = "web"
    $link.WorkingDirectory = $env:USERPROFILE
    $link.Description = "HireLoop - your AI job-search team"
    $link.Save()
    Say "Added a HireLoop shortcut to your desktop"
} catch { }

Write-Host ""
Write-Host "HireLoop is installed." -ForegroundColor Green
Write-Host "  Start it any time: double-click HireLoop on your desktop, or run 'hireloop web'."
Write-Host "  Just looking?      run 'hireloop demo' to try it with made-up data."
Write-Host ""
Say "Opening HireLoop - keep this window open while you use it (Ctrl+C to stop)"
& $hireloop web
