<#
.SYNOPSIS
    Build, sign and package the SafePII desktop helper into an MSI.

.DESCRIPTION
    Run on a Windows machine with Python 3.12 installed.

        cd <repo>
        powershell -ExecutionPolicy Bypass -File .\desktop\packaging\build.ps1
        powershell -ExecutionPolicy Bypass -File .\desktop\packaging\build.ps1 -Sign

    The ExecutionPolicy prefix is not optional on a machine with the default
    policy, which refuses to run a downloaded script and says so in a way that
    reads like the script is broken.

    First time on a machine:

        py -m pip install pyinstaller uiautomation
        winget install Microsoft.DotNet.SDK.8      # or any .NET SDK
        dotnet tool install --global wix

    -SkipMsi stops after freezing, which is the quickest way to find out whether
    the bundle is right before dealing with WiX at all.

    Produces desktop\packaging\dist\SafePIIHelper-<version>.msi.

    SIGNING. Since 1 June 2023 the CA/Browser Forum has required the private
    key of *every* code signing certificate, organisation-validated as well as
    extended-validation, to live in a FIPS 140-2 Level 2 or Common Criteria
    EAL4+ hardware module. A certificate file on disk is no longer issued, so
    signing means one of:

      a hardware token   plugged into the build machine, signed with signtool;
      a cloud signing    service (Azure Trusted Signing, DigiCert KeyLocker,
                         SSL.com eSigner) driven by its own signtool dlib.

    Set SAFEPII_SIGN_COMMAND to whatever your provider gives you; this script
    calls it with the file to sign appended. Leave it unset to build unsigned,
    which is fine for testing and not for release: an unsigned program that
    installs a global keyboard hook will be stopped by SmartScreen, is likely to
    be quarantined by endpoint protection, and will not pass application
    allowlisting.
#>
[CmdletBinding()]
param(
    [switch]$Sign,
    [switch]$SkipMsi
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Resolve-Path (Join-Path $here "..\..")
$dist = Join-Path $here "dist"
$work = Join-Path $here "build"

function Get-HelperVersion {
    $src = Get-Content (Join-Path $repo "desktop\helper.py") -Raw
    if ($src -notmatch '__version__\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"') {
        throw "No __version__ in desktop\helper.py"
    }
    return $Matches[1]
}

function Invoke-Sign([string]$path) {
    if (-not $Sign) { return }
    if (-not $env:SAFEPII_SIGN_COMMAND) {
        throw "-Sign was given but SAFEPII_SIGN_COMMAND is not set. See the notes at the top of this script."
    }
    Write-Host "signing $([IO.Path]::GetFileName($path))"
    # The provider's command, with the file appended. Kept opaque on purpose:
    # a token, a cloud dlib and a plain signtool call all differ here.
    & cmd /c "$env:SAFEPII_SIGN_COMMAND `"$path`""
    if ($LASTEXITCODE -ne 0) { throw "signing failed with exit code $LASTEXITCODE" }
}

$version = Get-HelperVersion
Write-Host "SafePII desktop helper $version"

# ---- 1. the version resource, so the number is in one place only
Write-Host "writing version_info.txt"
& py (Join-Path $here "write_version_info.py")
if ($LASTEXITCODE -ne 0) { throw "could not write the version resource" }

# ---- 2. freeze
# A helper or bridge left running from the last build holds its own DLLs open,
# and PyInstaller cannot clean the output directory: it fails with "Access is
# denied" on something like _internal\libcrypto-3.dll, which looks like a
# permissions problem and is not. Only processes running from this build's own
# output are stopped -- an installed helper in Program Files is somebody's
# working machine and is none of this script's business.
Get-Process -Name SafePIIHelper -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($dist, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object {
        Write-Host "stopping SafePIIHelper (pid $($_.Id)) left over from the last build"
        Stop-Process -Id $_.Id -Force
    }
Start-Sleep -Milliseconds 300

Write-Host "freezing with PyInstaller"
& py -m PyInstaller (Join-Path $here "safepii-helper.spec") `
    --noconfirm --distpath $dist --workpath $work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$appDir = Join-Path $dist "SafePIIHelper"
$exe = Join-Path $appDir "SafePIIHelper.exe"
if (-not (Test-Path $exe)) { throw "expected $exe" }

# ---- 2a. does the thing we just built actually work?
# A frozen build fails in ways the source never does, and quietly: a module left
# out of the bundle produces a helper that starts and is missing a feature. One
# question answers most of it at once. Asking the bridge for its tools proves the
# executable runs, that Python froze, that broker.py came with it, that
# http.server survived the exclude list, and that the --stdio entry point works.
# --port 1 is closed on purpose: the bridge answers for itself when nothing is
# behind it, so this needs no running helper.
Write-Host "checking the frozen build answers"
# Run through Start-Process with real file redirection rather than a pipeline.
# A native command that writes anything to stderr makes PowerShell raise a
# NativeCommandError, which $ErrorActionPreference = "Stop" turns terminating --
# and `2>$null` does not prevent it, because the record is raised by PowerShell
# rather than written to the stream being redirected. The bridge logs one line
# as it starts, correctly, since stdout carries nothing but JSON-RPC. That line
# was killing the build it had just finished. Redirecting to files sidesteps the
# whole mechanism and keeps the two streams apart for the error message.
$probeIn  = Join-Path $env:TEMP "safepii-probe-in.json"
$probeOut = Join-Path $env:TEMP "safepii-probe-out.txt"
$probeErr = Join-Path $env:TEMP "safepii-probe-err.txt"
Set-Content -Path $probeIn -Value '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' -Encoding ascii
Start-Process -FilePath $exe -ArgumentList "--stdio","--bridge","--port","1" `
    -RedirectStandardInput $probeIn -RedirectStandardOutput $probeOut `
    -RedirectStandardError $probeErr -NoNewWindow -Wait
$answer = if (Test-Path $probeOut) { Get-Content $probeOut -Raw } else { "" }
$said   = if (Test-Path $probeErr) { Get-Content $probeErr -Raw } else { "" }
Remove-Item $probeIn, $probeOut, $probeErr -ErrorAction SilentlyContinue
if ($answer -notmatch 'list_files') {
    throw @"
The frozen build did not answer.

  it wrote to stdout: $answer
  it wrote to stderr: $said

A build that starts and cannot do this is missing something from the bundle --
check the spec's hiddenimports and excludes before shipping it.
"@
}
Write-Host "  the bridge answered; the bundle is complete"

Invoke-Sign $exe

if ($SkipMsi) { Write-Host "built $appDir (no MSI requested)"; exit 0 }

# ---- 3. the MSI. WiX v5 globs the directory itself, so there is no separate
#         harvest step and no generated file list to fall out of step.
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw @"
The WiX toolset is not on PATH. Install it with:

    dotnet tool install --global wix --version 5.*

Pin the version. An unpinned install now fetches WiX v7, which refuses to build
until its Open Source Maintenance Fee EULA is accepted -- a commercial licence
question, not a build step. This project's .wxs is written for v4 anyway.
"@
}
# There is exactly one usable band, and both ways out of it fail in a way that
# reads like this project's .wxs is wrong rather than the toolset. Say which it
# is before the attempt: v4 has no Files element, v6 and later want the fee.
$wixVersion = (& wix --version 2>&1 | Out-String).Trim()
if ($wixVersion -match '^(\d+)\.' -and [int]$Matches[1] -lt 5) {
    throw @"
WiX $wixVersion is installed, and the Files element that harvests the built
folder arrived in v5. On v4 it fails as "ComponentGroup contains an unexpected
child element 'Files'", which reads like this project's .wxs is wrong. It is not:

    dotnet tool uninstall --global wix
    dotnet tool install --global wix --version 5.*
"@
}
if ($wixVersion -match '^(\d+)\.' -and [int]$Matches[1] -ge 6) {
    throw @"
WiX $wixVersion is installed, and v6 and later require the Open Source
Maintenance Fee EULA to be accepted before they will build anything. For a
commercial product that is a licence to buy, not a prompt to click through.

Either settle that (https://wixtoolset.org/osmf/), or use v5, which has the
Files element this .wxs needs and predates the fee:

    dotnet tool uninstall --global wix
    dotnet tool install --global wix --version 5.*

Or skip the MSI altogether: -SkipMsi leaves the frozen build in dist\, and
README.md has the handful of commands that put it on a machine without one.
"@
}
Write-Host "building the MSI"
$msi = Join-Path $dist "SafePIIHelper-$version.msi"
# -bindpath, because WiX resolves a SourceFile relative to the working directory
# rather than to the .wxs that names it -- so building from anywhere but this
# folder failed to find safepii.ico, which sits beside the .wxs. Binding the
# folder keeps the .wxs free of absolute paths and works from any directory.
& wix build (Join-Path $here "SafePIIHelper.wxs") `
    -d ProductVersion=$version -d AppDir=$appDir `
    -bindpath $here `
    -arch x64 -out $msi
if ($LASTEXITCODE -ne 0) { throw "wix build failed" }
Invoke-Sign $msi

Write-Host ""
Write-Host "Built $msi"
Write-Host ""
Write-Host "Install it silently, configured for your fleet, with:"
Write-Host "  msiexec /i `"$msi`" SERVERURL=https://safepii.example.com /qn"
if (-not $Sign) {
    Write-Warning "This build is NOT signed. Do not ship it: a program that installs a global keyboard hook will be blocked."
}
