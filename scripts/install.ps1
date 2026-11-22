#requires -Version 5.1
<#
.SYNOPSIS
  Install astrbot_plugin_ctx_dump into an AstrBot data directory.

.DESCRIPTION
  Copies this plugin folder to <AstrBot data>\plugins\. AstrBot data directory is
  the one that contains cmd_config.json (desktop build: %USERPROFILE%\.astrbot\data).

.EXAMPLE
  .\scripts\install.ps1
  .\scripts\install.ps1 -AstrBotData "D:\AstrBot\data" -Force
#>
[CmdletBinding()]
param(
    [string]$AstrBotData = (Join-Path $env:USERPROFILE ".astrbot\data"),
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$pluginRoot = Split-Path -Parent $PSScriptRoot
$pluginName = Split-Path -Leaf $pluginRoot

if (-not (Test-Path (Join-Path $AstrBotData "cmd_config.json"))) {
    throw "Not an AstrBot data dir: $AstrBotData (cmd_config.json not found). Pass -AstrBotData <path>."
}

$pluginsDir = Join-Path $AstrBotData "plugins"
$target = Join-Path $pluginsDir $pluginName

if (Test-Path $target) {
    if (-not $Force) {
        throw "Already installed: $target  (use -Force to overwrite)"
    }
    Remove-Item $target -Recurse -Force
}

New-Item -ItemType Directory -Path $pluginsDir -Force | Out-Null
Copy-Item $pluginRoot $target -Recurse -Force
Remove-Item (Join-Path $target "__pycache__") -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "Installed to: $target"
Write-Host "Next: open AstrBot -> Plugins -> reload '$pluginName'."
Write-Host "Page : plugin detail -> view, or http://127.0.0.1:6199"
Write-Host "Data : $(Join-Path $AstrBotData 'plugin_data\ctx_dump')"
