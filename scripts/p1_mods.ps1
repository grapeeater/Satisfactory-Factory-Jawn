# P1: install (or remove) exactly SML 3.12.1 + FRM 1.5.6 for Satisfactory CL 520140.
#
#   powershell -ExecutionPolicy Bypass -File scripts\p1_mods.ps1             # install
#   powershell -ExecutionPolicy Bypass -File scripts\p1_mods.ps1 -Uninstall  # back to vanilla
#   powershell -ExecutionPolicy Bypass -File scripts\p1_mods.ps1 -Check      # report only
#
# Packages come from api.ficsit.app and are checked against the SHA-256 published there
# (pinned below). Nothing outside <game>\FactoryGame\Mods is touched. Refuses while the game runs.
param(
    [switch]$Uninstall,
    [switch]$Check,
    [string]$GameDir = "C:\Program Files (x86)\Steam\steamapps\common\Satisfactory"
)
$ErrorActionPreference = "Stop"

$Pins = @(
    @{ Name = "SML"; Version = "3.12.1"; Url = "https://api.ficsit.app/v1/version/CzPqe1eeMDoBUw/Windows/download"
       Sha256 = "33a11d6a70f1476ed572ad06963715ac829d166546c1ceca8080b2a7a8f113aa"; Plugin = "SML.uplugin" },
    @{ Name = "FicsitRemoteMonitoring"; Version = "1.5.6"; Url = "https://api.ficsit.app/v1/version/cYyUPtYod9rYb/Windows/download"
       Sha256 = "0f6e0169c9aebdf491963938d8efd24ae6c24c6968e942864c2c9ce01b1dcab4"; Plugin = "FicsitRemoteMonitoring.uplugin" }
)

function Get-Sha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fs = [System.IO.File]::OpenRead($Path)
    try { return ([System.BitConverter]::ToString($sha.ComputeHash($fs)) -replace "-", "").ToLowerInvariant() }
    finally { $fs.Dispose(); $sha.Dispose() }
}

function Get-InstalledVersion([string]$PluginPath) {
    if (-not (Test-Path $PluginPath)) { return $null }
    return (Get-Content $PluginPath -Raw | ConvertFrom-Json).SemVersion
}

$Mods = Join-Path $GameDir "FactoryGame\Mods"
if (-not (Test-Path (Join-Path $GameDir "FactoryGame"))) { throw "Game not found at $GameDir" }

$running = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -like "FactoryGame*" }
if ($running -and -not $Check) { throw "Satisfactory is running (PID $($running.Id -join ', ')). Quit it first." }

if ($Check) {
    if (-not (Test-Path $Mods)) { "Mods folder: none (vanilla)"; return }
    "Mods folder: $Mods"
    Get-ChildItem $Mods -Directory | ForEach-Object { "  $($_.Name)" }
    foreach ($p in $Pins) {
        $v = Get-InstalledVersion (Join-Path $Mods "$($p.Name)\$($p.Plugin)")
        $ok = if ($v -eq $p.Version) { "OK" } else { "expected $($p.Version)" }
        "  $($p.Name): $v ($ok)"
    }
    return
}

if ($Uninstall) {
    if (-not (Test-Path $Mods)) { "Already vanilla: no Mods folder."; return }
    $extra = Get-ChildItem $Mods | Where-Object { $_.Name -notin ($Pins | ForEach-Object { $_.Name }) }
    if ($extra) { throw "Mods contains other items ($($extra.Name -join ', ')); not removing anything. Remove manually." }
    Remove-Item $Mods -Recurse -Force
    "Removed $Mods. The game is vanilla again."
    return
}

if (Test-Path $Mods) { throw "$Mods already exists. Run with -Check to inspect or -Uninstall first." }

$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("jawn-mods-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory $tmp | Out-Null
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($p in $Pins) {
        $zip = Join-Path $tmp "$($p.Name)-$($p.Version).zip"
        "Downloading $($p.Name) $($p.Version)..."
        Invoke-WebRequest -Uri $p.Url -OutFile $zip -UseBasicParsing
        $h = Get-Sha256 $zip
        if ($h -ne $p.Sha256) { throw "$($p.Name): SHA-256 mismatch ($h); refusing to install." }
        "  sha256 OK"
        $p.Zip = $zip
    }
    New-Item -ItemType Directory $Mods | Out-Null
    foreach ($p in $Pins) {
        $dest = Join-Path $Mods $p.Name
        Expand-Archive -Path $p.Zip -DestinationPath $dest
        $v = Get-InstalledVersion (Join-Path $dest $p.Plugin)
        if ($v -ne $p.Version) { throw "$($p.Name): installed version $v, expected $($p.Version)" }
        "Installed $($p.Name) $v -> $dest"
    }
    "Done. Undo with: scripts\p1_mods.ps1 -Uninstall"
}
catch {
    if ((Test-Path $Mods) -and -not $_.Exception.Message.Contains("already exists")) {
        Remove-Item $Mods -Recurse -Force -ErrorAction SilentlyContinue
        "Install failed; removed partial Mods folder."
    }
    throw
}
finally { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
