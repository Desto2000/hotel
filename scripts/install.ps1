# Reproducible, system-wide install of the `hotel` CLI (Windows PowerShell).
# Python pinned by .python-version, deps pinned by constraints.txt (from uv.lock),
# installed as an isolated uv tool -> `hotel` on PATH.
# Usage: scripts\install.ps1 [-LobbyDir C:\Users\me\lobby]
param([string]$LobbyDir = "$HOME\lobby")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $PSScriptRoot
$py = (Get-Content "$here\.python-version").Trim()
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw "uv is required: https://docs.astral.sh/uv/" }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "git is required" }

uv tool install --force --python $py --constraints "$here\constraints.txt" $here
uv tool update-shell | Out-Null

if (-not (Test-Path "$LobbyDir\.hotel")) { hotel init $LobbyDir }
hotel home $LobbyDir | Out-Null
hotel doctor
Write-Host "done: run 'hotel' from any directory (open a new terminal if PATH changed)"
