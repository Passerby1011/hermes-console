# hermes-console installer (desktop door + enablement), ASCII only on purpose:
# PowerShell 5.1 reads a BOM-less file as ANSI, so a non-ASCII script dies before line 1.
# Never write a bare "<" ">" pair inside a double-quoted string here (it is a redirection).
#
# WHAT IT DOES
#   1. node scripts/build.mjs        -> dashboard/dist/{index.js,style.css} + desktop/plugin.js
#   2. copy desktop/plugin.js        -> <home>/desktop-plugins/hermes-console/plugin.js
#      (the on-disk "door" the Electron runtime loads; hash-verified after copy)
#   3. append "hermes-console" to    plugins.enabled in the ROOT config.yaml, via
#      `hermes config get --json` -> append -> `hermes config set` (whole-array semantics),
#      then read back and throw on any mismatch.
#
# WHAT IT DOES NOT DO
#   - it never restarts the desktop app or the gateway, never deletes any old plugin,
#     never touches desktop-plugins/<other-plugin>/.
#   - code-worker did NOT run this script: writing desktop-plugins/ and enabling a plugin
#     is the operator's step (see the card body: restart is the owner's call).
#
# USAGE (from a shell, unpiped so the exit status is the script's own):
#   powershell -NoProfile -ExecutionPolicy Bypass -File <this file>
#
# Parse check before running (expected: 0):
#   $e=$null; [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path .\install.ps1),[ref]$null,[ref]$e); $e.Count

param(
    [string]$HermesHome = 'D:/hermes-data/hermes',
    [string]$PluginId = 'hermes-console',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$hermes = Join-Path $HermesHome 'hermes-agent/.hermes/bin/hermes'

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -Path $Path).Hash
}

# --- 1. build ---------------------------------------------------------------
$distJs = Join-Path $pluginRoot 'dashboard/dist/index.js'
if (-not $SkipBuild) {
    & node (Join-Path $pluginRoot 'scripts/build.mjs')
    if ($LASTEXITCODE -ne 0) { throw "build.mjs failed with exit code $LASTEXITCODE" }
}
foreach ($f in @($distJs, (Join-Path $pluginRoot 'dashboard/dist/style.css'))) {
    if (-not (Test-Path $f)) { throw "missing build artifact: $f" }
    if ((Get-Item $f).Length -le 0) { throw "empty build artifact: $f" }
}
Write-Host ("build ok: index.js = {0} B, style.css = {1} B" -f (Get-Item $distJs).Length, (Get-Item (Join-Path $pluginRoot 'dashboard/dist/style.css')).Length)

# --- 2. desktop door --------------------------------------------------------
$doorDir = Join-Path $HermesHome "desktop-plugins/$PluginId"
New-Item -ItemType Directory -Force -Path $doorDir | Out-Null
$door = Join-Path $doorDir 'plugin.js'
Copy-Item -Path (Join-Path $pluginRoot 'desktop/plugin.js') -Destination $door -Force
$srcHash = Get-Sha256 (Join-Path $pluginRoot 'desktop/plugin.js')
$doorHash = Get-Sha256 $door
if ($srcHash -ne $doorHash) { throw "door copy mismatch: src=$srcHash door=$doorHash" }
$stamp = [string][int64]((Get-Item (Join-Path $pluginRoot 'desktop/plugin.js')).LastWriteTime.ToUniversalTime() - [datetime]'1970-01-01').TotalMilliseconds
@{ package = $PluginId; source = (Join-Path $pluginRoot 'desktop/plugin.js'); sourceMtimeMs = $stamp } |
    ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $doorDir '.hermes-package.json')
Write-Host "door ok: $door sha256=$doorHash"

# --- 3. plugins.enabled (whole-array write + read-back) ---------------------
$env:HERMES_HOME = $HermesHome
$env:HERMES_PROFILE = ''
$json = (& $hermes config get plugins.enabled --json) | Out-String
$list = @(ConvertFrom-Json $json)
Write-Host ("plugins.enabled before: {0} entries" -f $list.Count)
if ($list -notcontains $PluginId) {
    $newList = @($list) + $PluginId
    $payload = ConvertTo-Json -InputObject @($newList) -Compress
    & $hermes config set plugins.enabled $payload
    if ($LASTEXITCODE -ne 0) { throw "hermes config set failed with exit code $LASTEXITCODE" }
} else {
    Write-Host "plugins.enabled already contains $PluginId (idempotent, no write)"
}
$after = @(ConvertFrom-Json ((& $hermes config get plugins.enabled --json) | Out-String))
if ($after -notcontains $PluginId) { throw "read-back is missing $PluginId" }
$lost = @($list | Where-Object { $after -notcontains $_ })
if ($lost.Count -gt 0) { throw ("read-back lost entries: " + ($lost -join ', ')) }
Write-Host ("plugins.enabled after : {0} entries, added={1}" -f $after.Count, (@($after | Where-Object { $list -notcontains $_ }) -join ','))

Write-Host ''
Write-Host 'DONE. Next steps owned by the operator: restart the desktop app (backend routes mount at startup only),'
Write-Host 'then open /console and confirm the sidebar row and the 9-agent scope chips.'
