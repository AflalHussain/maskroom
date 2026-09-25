# Dev sync for the Windows PC: pull desktop/ from the Linux dev box whenever a
# file changes there, and restart the helper on each change.
#
#   On Linux:    ./desktop/dev-serve.sh                 (serves desktop/ on :8765)
#   On Windows:  powershell -ExecutionPolicy Bypass -File dev-sync.ps1 -Source http://10.27.149.168:8765
#
# Files are written next to this script. helper.py is (re)started as a child
# process of this window; close the window (or Ctrl+C) to stop both.
# Dev-only: plain HTTP on the office LAN, no auth.

param(
    [string]$Source = "http://10.27.149.168:8765",
    [string[]]$Files = @("helper.py", "README.md"),
    [int]$IntervalSeconds = 2,
    [switch]$NoRun
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$helper = $null
$hashes = @{}

function Get-RemoteBytes([string]$name) {
    $r = Invoke-WebRequest -Uri "$Source/$name" -UseBasicParsing -TimeoutSec 5
    return $r.Content
}

function Get-Hash([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    return [BitConverter]::ToString($sha.ComputeHash($bytes))
}

function Start-Helper {
    if ($NoRun) { return }
    if ($helper -and -not $helper.HasExited) {
        Write-Host "$(Get-Date -Format HH:mm:ss) stopping helper (pid $($helper.Id))"
        Stop-Process -Id $helper.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command python -ErrorAction SilentlyContinue }
    if (-not $py) { Write-Host "python not found on PATH"; return }
    $script:helper = Start-Process -FilePath $py.Source -ArgumentList "`"$here\helper.py`"" -WorkingDirectory $here -PassThru
    Write-Host "$(Get-Date -Format HH:mm:ss) started helper (pid $($helper.Id))"
}

Write-Host "Syncing $($Files -join ', ') from $Source into $here every $IntervalSeconds s. Ctrl+C to stop."
$first = $true
while ($true) {
    $changed = $false
    foreach ($name in $Files) {
        try {
            $bytes = Get-RemoteBytes $name
        } catch {
            Write-Host "$(Get-Date -Format HH:mm:ss) cannot fetch $name from $Source ($($_.Exception.Message))"
            continue
        }
        $h = Get-Hash $bytes
        if ($hashes[$name] -ne $h) {
            [System.IO.File]::WriteAllBytes((Join-Path $here $name), $bytes)
            if (-not $first) { Write-Host "$(Get-Date -Format HH:mm:ss) updated $name" }
            $hashes[$name] = $h
            if ($name -like "*.py") { $changed = $true }
        }
    }
    if ($first -or $changed) { Start-Helper }
    $first = $false
    Start-Sleep -Seconds $IntervalSeconds
}
