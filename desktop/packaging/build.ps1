<#
.SYNOPSIS
    Build, sign and package the SafePII desktop helper into an MSI.

.DESCRIPTION
    Run on a Windows machine with Python 3.12 installed.

        cd <repo>
        .\desktop\packaging\build.ps1
        .\desktop\packaging\build.ps1 -Sign        # also sign, see SIGNING below

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
Write-Host "freezing with PyInstaller"
& py -m PyInstaller (Join-Path $here "safepii-helper.spec") `
    --noconfirm --distpath $dist --workpath $work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$appDir = Join-Path $dist "SafePIIHelper"
$exe = Join-Path $appDir "SafePIIHelper.exe"
if (-not (Test-Path $exe)) { throw "expected $exe" }
Invoke-Sign $exe

if ($SkipMsi) { Write-Host "built $appDir (no MSI requested)"; exit 0 }

# ---- 3. the file list for the MSI, generated because a frozen build's
#         contents change with the Python version
Write-Host "harvesting files for the MSI"
& wix extension add -g WixToolset.Heat.wixext 2>$null | Out-Null
& wix build --help 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "The WiX toolset is not on PATH. Install it with: dotnet tool install --global wix"
}
& heat dir $appDir -cg HelperFiles -dr INSTALLFOLDER -srd -sreg -scom -gg -sfrag `
    -var var.AppDir -out (Join-Path $here "HelperFiles.wxi") -t (Join-Path $here "harvest.xslt")
if ($LASTEXITCODE -ne 0) { throw "harvesting failed" }

# ---- 4. the MSI
Write-Host "building the MSI"
$msi = Join-Path $dist "SafePIIHelper-$version.msi"
& wix build (Join-Path $here "SafePIIHelper.wxs") `
    -d ProductVersion=$version -d AppDir=$appDir `
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
