# Add "Mask with Maskroom" to the Explorer right-click menu for the file types
# Maskroom can mask, and remove it again with -Uninstall.
#
#   powershell -ExecutionPolicy Bypass -File install-context-menu.ps1
#   powershell -ExecutionPolicy Bypass -File install-context-menu.ps1 -Uninstall
#
# Writes only under HKCU (no admin needed). Picking files and choosing the entry
# runs helper.py with their paths; the masked copies land in the helper's files
# folder (%APPDATA%\Maskroom\files) and the bar reports what was masked.

param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$helper = Join-Path $here "helper.py"
$exts = @(".xlsx", ".xlsm", ".pdf", ".docx", ".pptx", ".csv", ".tsv", ".txt", ".json")
$verb = "MaskWithMaskroom"

function Get-Pythonw {
    $p = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($p) { return $p.Source }
    $p = Get-Command py -ErrorAction SilentlyContinue
    if ($p) { return $p.Source }           # py.exe opens a console window; acceptable fallback
    throw "Python not found on PATH."
}

if ($Uninstall) {
    foreach ($ext in $exts) {
        $key = "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\$verb"
        if (Test-Path $key) { Remove-Item $key -Recurse -Force; Write-Host "removed for $ext" }
    }
    Write-Host "Done. The entry is gone from Explorer."
    exit 0
}

if (-not (Test-Path $helper)) { throw "helper.py not found next to this script ($helper)." }
$py = Get-Pythonw
Write-Host "Using $py and $helper"

foreach ($ext in $exts) {
    $key = "HKCU:\Software\Classes\SystemFileAssociations\$ext\shell\$verb"
    New-Item -Path "$key\command" -Force | Out-Null
    Set-ItemProperty -Path $key -Name "(default)" -Value "Mask with Maskroom"
    Set-ItemProperty -Path $key -Name "Icon" -Value "$py,0"
    # %V is the clicked file; one invocation per file, which the helper handles.
    Set-ItemProperty -Path "$key\command" -Name "(default)" -Value "`"$py`" `"$helper`" `"%V`""
    Write-Host "added for $ext"
}

Write-Host ""
Write-Host "Right-click a supported file in Explorer and choose 'Mask with Maskroom'."
Write-Host "Masked copies go to $env:APPDATA\Maskroom\files (change with filesDir in helper.json)."
