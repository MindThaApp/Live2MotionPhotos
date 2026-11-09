<#
.SYNOPSIS
  Build the Live2Motion desktop exe and the MSIX packages (Microsoft Store + sideload test).

.DESCRIPTION
  Every run writes into a NEW folder <OutDir>\run-<timestamp>\ and never deletes or
  overwrites anything. The only shared file is the verified ExifTool download cache in
  <OutDir>\cache\ (written under a temp name, renamed only after the SHA-256 check).

  Output of a run:
    dist\Live2Motion\Live2Motion.exe          frozen app (+ exiftool\)
    layout\                                   MSIX package layout (AppxManifest.xml, resources.pri)
    packages\<Name>_<Version>_x64.msix         UNSIGNED: upload this (or the .msixupload) to Partner Center
    packages\<Name>_<Version>_x64.msixupload   same, as a Store upload file
    packages\<Name>_<Version>_x64_sideload.msix  signed with a self-signed TEST certificate
    packages\sideload-test-cert.cer            install into Trusted People to sideload the test build

  Identity values come from packaging\windows\store-identity.json (the one config spot).

.PARAMETER OutDir      Build root (default: <repo>\store-build-out).
.PARAMETER Python      Python with requirements-desktop.txt installed (default: python).
.PARAMETER CertPfx     Existing test-signing .pfx to reuse; created in <OutDir>\cert\ if omitted.
.PARAMETER SkipSign    Do not produce the signed sideload package.
.PARAMETER GuiSmoke    Also start the real windowed app for a few seconds (needs a desktop session).
#>
param(
  [string]$OutDir = "",
  [string]$Python = "python",
  [string]$CertPfx = "",
  [switch]$SkipSign,
  [switch]$GuiSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

# Keep in sync with .github/workflows/portable-windows.yml
$ExifToolVersion = if ($env:EXIFTOOL_VERSION) { $env:EXIFTOOL_VERSION } else { "13.59" }
$ExifToolSha256  = if ($env:EXIFTOOL_SHA256)  { $env:EXIFTOOL_SHA256 }  else { "44b512b25af500724ba579d0a53c8fc5851628b692dd5e5d94ae4a15c2cba9ec" }

$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $OutDir) { $OutDir = Join-Path $Repo "store-build-out" }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path

function Invoke-Native([string]$What, [scriptblock]$Cmd) {
  & $Cmd
  if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

function Find-SdkTool([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
  $hit = Get-ChildItem $kits -Recurse -Filter $Name -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\" } | Sort-Object FullName -Descending | Select-Object -First 1
  if (-not $hit) { throw "$Name not found: install the Windows 10/11 SDK" }
  return $hit.FullName
}

function Get-Sha256([string]$Path) {
  # .NET instead of Get-FileHash: works in Windows PowerShell 5.1 and pwsh, whatever PSModulePath says
  $sha = [System.Security.Cryptography.SHA256]::Create()
  $fs = [System.IO.File]::OpenRead($Path)
  try { return ([System.BitConverter]::ToString($sha.ComputeHash($fs)) -replace "-", "").ToUpperInvariant() }
  finally { $fs.Dispose(); $sha.Dispose() }
}

function Escape-Xml([string]$s) { return [System.Security.SecurityElement]::Escape($s) }

# ── Identity ──────────────────────────────────────────────────────────────────
$Id = Get-Content (Join-Path $PSScriptRoot "store-identity.json") -Raw | ConvertFrom-Json
foreach ($k in "IdentityName", "Publisher", "PublisherDisplayName", "DisplayName", "ShortName", "Description", "Version") {
  if (-not $Id.$k) { throw "store-identity.json: $k is empty" }
}
if ($Id.Version -notmatch '^\d+\.\d+\.\d+\.0$') { throw "store-identity.json: Version must look like 1.2.3.0 (Store requires the last part to be 0)" }

# ── Fresh run folder: nothing below is ever overwritten ─────────────────────────
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Run = Join-Path $OutDir "run-$Stamp"
if (Test-Path $Run) { throw "$Run already exists" }
New-Item -ItemType Directory -Path $Run | Out-Null
$Tmp = Join-Path $Run "tmp"; New-Item -ItemType Directory -Path $Tmp | Out-Null
$env:TEMP = $Tmp; $env:TMP = $Tmp      # keep PyInstaller/pip temp files inside the run folder
$env:PYINSTALLER_CONFIG_DIR = Join-Path $Run "pyi-config"   # PyInstaller's cache too (not %LOCALAPPDATA%)
Write-Host "Build folder: $Run"

# ── ExifTool (verified download, cached) ───────────────────────────────────────
$Cache = Join-Path $OutDir "cache"; New-Item -ItemType Directory -Force -Path $Cache | Out-Null
$ExifZip = Join-Path $Cache "exiftool-${ExifToolVersion}_64.zip"
if (-not (Test-Path $ExifZip)) {
  $part = Join-Path $Cache ("exiftool-download-" + [guid]::NewGuid().ToString("N") + ".part")
  $url = "https://sourceforge.net/projects/exiftool/files/exiftool-${ExifToolVersion}_64.zip/download"
  Invoke-Native "ExifTool download" { curl.exe -sSL --fail $url -o $part }
  $actual = Get-Sha256 $part
  if ($actual -ne $ExifToolSha256.ToUpperInvariant()) {
    throw "ExifTool zip hash mismatch! Expected $ExifToolSha256, got $actual (left at $part)"
  }
  Rename-Item $part (Split-Path $ExifZip -Leaf)    # new name in our cache; refuses if it exists
}
$actual = Get-Sha256 $ExifZip
if ($actual -ne $ExifToolSha256.ToUpperInvariant()) { throw "Cached ExifTool zip hash mismatch: $ExifZip" }
$ExifX = Join-Path $Run "exiftool-extracted"
Expand-Archive $ExifZip -DestinationPath $ExifX

# ── Assets (icon, tiles, Store logos) ──────────────────────────────────────────
$Assets = Join-Path $Run "assets"
Invoke-Native "make_assets.py" { & $Python (Join-Path $PSScriptRoot "make_assets.py") $Assets }

# ── Freeze the app ─────────────────────────────────────────────────────────────
$env:L2M_ICON = Join-Path $Assets "app.ico"
$Dist = Join-Path $Run "dist"
Invoke-Native "PyInstaller" {
  & $Python -m PyInstaller (Join-Path $PSScriptRoot "live2motion.spec") --noconfirm `
    --distpath $Dist --workpath (Join-Path $Run "pyi-work")
}
$App = Join-Path $Dist "Live2Motion"
$Exe = Join-Path $App "Live2Motion.exe"

# Bundle ExifTool next to the exe (copy of the extracted folder; the -k exe is renamed in our copy)
$exifSrc = Get-ChildItem $ExifX -Directory | Select-Object -First 1
Copy-Item $exifSrc.FullName (Join-Path $App "exiftool") -Recurse
Rename-Item (Join-Path $App "exiftool\exiftool(-k).exe") "exiftool.exe"

# ── Smoke tests on the frozen app ──────────────────────────────────────────────
Write-Host "Smoke: bundled ExifTool"
$ver = & (Join-Path $App "exiftool\exiftool.exe") -ver
if ($LASTEXITCODE -ne 0 -or "$ver".Trim() -ne $ExifToolVersion) { throw "Bundled exiftool -ver failed: '$ver'" }

Write-Host "Smoke: converter subprocess mode"
$p = Start-Process $Exe -ArgumentList "--motionphoto2", "--help" -Wait -PassThru -NoNewWindow `
  -RedirectStandardOutput (Join-Path $Run "smoke-mp2-help.txt")
if ($p.ExitCode -ne 0) { throw "Live2Motion.exe --motionphoto2 --help exited with $($p.ExitCode)" }
if (-not (Select-String -Path (Join-Path $Run "smoke-mp2-help.txt") -Pattern "input-directory" -Quiet)) {
  throw "--motionphoto2 --help printed no usage text (stdout not reaching the parent?)"
}

Write-Host "Smoke: server serves / on loopback"
$SmokeData = Join-Path $Run "smoke-data"
$PortFile = Join-Path $Run "smoke.port"
$env:LIVE2MOTION_DATA_DIR = $SmokeData
$srv = Start-Process $Exe -ArgumentList "--smoke-test", "--port-file", "`"$PortFile`"" -PassThru
try {
  $deadline = (Get-Date).AddSeconds(60)
  while (-not (Test-Path $PortFile) -and (Get-Date) -lt $deadline -and -not $srv.HasExited) { Start-Sleep -Milliseconds 300 }
  if (-not (Test-Path $PortFile)) { throw "Server did not start (see $SmokeData\logs\desktop.log)" }
  Start-Sleep -Milliseconds 300
  $info = Get-Content $PortFile -Raw | ConvertFrom-Json
  $port = $info.port
  $r = Invoke-WebRequest "http://127.0.0.1:$port/" -UseBasicParsing
  if ($r.StatusCode -ne 200 -or $r.Content -notmatch "Live2Motion") { throw "GET / failed: $($r.StatusCode)" }
  $denied = $false
  try { Invoke-WebRequest "http://127.0.0.1:$port/api/status" -UseBasicParsing | Out-Null }
  catch { $denied = ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 401) }
  if (-not $denied) { throw "/api/status answered without the session token" }
  Write-Host "  GET / -> 200, /api/status without token -> 401 (port $port)"

  # A Preview (dry run) through the server, so the frozen exe starts itself as the
  # converter: generated sample files in this run folder only; a dry run writes nothing.
  Write-Host "Smoke: Preview through the server on generated samples"
  $Samples = Join-Path $Run "smoke-samples"
  Invoke-Native "sample generation" { & $Python -c @'
import sys
from pathlib import Path
from PIL import Image
d = Path(sys.argv[1]); d.mkdir()
for i in range(2):
    with open(d / f'IMG_000{i}.JPG', 'xb') as f:
        Image.new('RGB', (64, 48), (40 * i, 120, 200)).save(f, 'JPEG')
    with open(d / f'IMG_000{i}.MOV', 'xb') as f:
        f.write(b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  ' + b'\x00\x00\x00\x08free')
'@ $Samples }
  $before = Get-ChildItem $Samples | ForEach-Object { "$($_.Name):$($_.Length):$(Get-Sha256 $_.FullName)" }
  $h = @{ "X-Live2Motion-Token" = $info.token }
  $base = "http://127.0.0.1:$port"
  $body = @{ input_directory = $Samples; exif_match = $false } | ConvertTo-Json   # samples carry no EXIF
  Invoke-RestMethod "$base/api/config" -Method Post -Headers $h -ContentType "application/json" -Body $body | Out-Null
  Invoke-RestMethod "$base/api/run?dry_run=true" -Method Post -Headers $h | Out-Null
  $deadline = (Get-Date).AddSeconds(90)
  do { Start-Sleep -Milliseconds 500; $st = Invoke-RestMethod "$base/api/status" -Headers $h } while ($st.running -and (Get-Date) -lt $deadline)
  $res = Invoke-RestMethod "$base/api/results" -Headers $h
  $planned = @($res.results | Where-Object { $_.status -eq "planned" }).Count
  if ($st.running -or $planned -ne 2) {
    throw "Preview through the server failed (running=$($st.running), planned=$planned); see $SmokeData\logs"
  }
  $after = Get-ChildItem $Samples | ForEach-Object { "$($_.Name):$($_.Length):$(Get-Sha256 $_.FullName)" }
  if (Compare-Object $before $after) { throw "A dry run changed the sample folder" }
  Write-Host "  Preview found 2 pairs, sample folder unchanged"
} finally {
  if (-not $srv.HasExited) { Stop-Process -Id $srv.Id -Force }
  Remove-Item Env:\LIVE2MOTION_DATA_DIR
}

if ($GuiSmoke) {
  Write-Host "Smoke: windowed start"
  $GuiData = Join-Path $Run "gui-data"
  $env:LIVE2MOTION_DATA_DIR = $GuiData
  $gui = Start-Process $Exe -PassThru
  try {
    Start-Sleep -Seconds 20
    if ($gui.HasExited) { throw "Windowed app exited early (code $($gui.ExitCode)); see $GuiData\logs\desktop.log" }
    if (-not (Select-String -Path "$GuiData\logs\desktop.log" -Pattern "Serving on" -Quiet)) { throw "Windowed app did not start its server" }
    Write-Host "  window up, server running"
  } finally {
    if (-not $gui.HasExited) { Stop-Process -Id $gui.Id -Force }
    Remove-Item Env:\LIVE2MOTION_DATA_DIR
  }
}

# ── MSIX layout ────────────────────────────────────────────────────────────────
$Layout = Join-Path $Run "layout"
Copy-Item $App $Layout -Recurse
Copy-Item (Join-Path $Assets "Assets") (Join-Path $Layout "Assets") -Recurse
$manifest = Get-Content (Join-Path $PSScriptRoot "AppxManifest.template.xml") -Raw
foreach ($k in "IdentityName", "Publisher", "PublisherDisplayName", "DisplayName", "ShortName", "Description", "Version") {
  $manifest = $manifest.Replace("{{$k}}", (Escape-Xml $Id.$k))
}
if ($manifest -match "\{\{") { throw "Unfilled placeholder left in AppxManifest" }
[System.IO.File]::WriteAllText((Join-Path $Layout "AppxManifest.xml"), $manifest, (New-Object System.Text.UTF8Encoding($false)))

$MakePri = Find-SdkTool "makepri.exe"
$MakeAppx = Find-SdkTool "makeappx.exe"
$PriCfg = Join-Path $Run "priconfig.xml"
Invoke-Native "makepri createconfig" { & $MakePri createconfig /cf $PriCfg /dq en-US /pv 10.0.0 }
# One .msix (no resource packs): drop the auto-split so every scale variant stays in resources.pri
[xml]$pri = Get-Content $PriCfg -Raw
foreach ($n in @($pri.SelectNodes("//packaging"))) { [void]$n.ParentNode.RemoveChild($n) }
$pri.Save($PriCfg)
# Index only the tile/logo assets (not the ~1200 Python runtime files) from a separate root
# that mirrors the layout's Assets\ folder; the resulting resources.pri goes into the layout.
$PriRoot = Join-Path $Run "pri-root"
New-Item -ItemType Directory -Path $PriRoot | Out-Null
Copy-Item (Join-Path $Assets "Assets") (Join-Path $PriRoot "Assets") -Recurse
Copy-Item (Join-Path $Layout "AppxManifest.xml") $PriRoot
Invoke-Native "makepri new" { & $MakePri new /pr $PriRoot /cf $PriCfg /mn (Join-Path $PriRoot "AppxManifest.xml") /of (Join-Path $Layout "resources.pri") }

$Pkgs = Join-Path $Run "packages"; New-Item -ItemType Directory -Path $Pkgs | Out-Null
$Base = "$($Id.IdentityName)_$($Id.Version)_x64"
$Msix = Join-Path $Pkgs "$Base.msix"
Invoke-Native "makeappx pack" { & $MakeAppx pack /d $Layout /p $Msix /nv }

# .msixupload = zip of the .msix (Partner Center accepts either)
$zip = Join-Path $Pkgs "$Base.zip"
Compress-Archive -Path $Msix -DestinationPath $zip
Rename-Item $zip "$Base.msixupload"

# ── Sideload test package, signed with a self-signed cert whose subject = Publisher ──
if (-not $SkipSign) {
  $SignTool = Find-SdkTool "signtool.exe"
  $CertDir = Join-Path $OutDir "cert"
  if (-not $CertPfx) { $CertPfx = Join-Path $CertDir "sideload-test.pfx" }
  $PwdFile = "$CertPfx.password.txt"
  if (-not (Test-Path $CertPfx)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $CertPfx) | Out-Null
    $plain = [guid]::NewGuid().ToString("N")
    $cert = New-SelfSignedCertificate -Type Custom -Subject $Id.Publisher -KeyUsage DigitalSignature `
      -FriendlyName "Live2Motion sideload TEST certificate" -CertStoreLocation "Cert:\CurrentUser\My" `
      -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}") -NotAfter (Get-Date).AddYears(2)
    try {
      $sec = ConvertTo-SecureString $plain -AsPlainText -Force
      Export-PfxCertificate -Cert $cert -FilePath $CertPfx -Password $sec | Out-Null
      [System.IO.File]::WriteAllText($PwdFile, $plain)
    } finally {
      # The certificate was created by this script a moment ago; only the .pfx is kept
      Remove-Item "Cert:\CurrentUser\My\$($cert.Thumbprint)"
    }
  }
  $plain = (Get-Content $PwdFile -Raw).Trim()
  $pfx = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($CertPfx, $plain)
  if ($pfx.Subject -ne $Id.Publisher) { throw "Test cert subject '$($pfx.Subject)' differs from Publisher '$($Id.Publisher)'" }
  [System.IO.File]::WriteAllBytes((Join-Path $Pkgs "sideload-test-cert.cer"), $pfx.Export("Cert"))
  $Signed = Join-Path $Pkgs "${Base}_sideload.msix"
  Copy-Item $Msix $Signed
  Invoke-Native "signtool sign" { & $SignTool sign /fd SHA256 /f $CertPfx /p $plain $Signed }
}

Write-Host ""
Write-Host "Done. Packages in $Pkgs"
Get-ChildItem $Pkgs | ForEach-Object { Write-Host ("  {0}  ({1:N1} MB)" -f $_.Name, ($_.Length / 1MB)) }
