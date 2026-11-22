#requires -Version 5.1
<#
.SYNOPSIS
  Uninstall astrbot_plugin_ctx_dump from an AstrBot data directory.

.DESCRIPTION
  Removes the plugin folder. The recorded context data is kept unless -PurgeData
  is passed, because it may be what you are analyzing.

.EXAMPLE
  .\scripts\uninstall.ps1
  .\scripts\uninstall.ps1 -PurgeData
#>
[CmdletBinding()]
param(
    [string]$AstrBotData = (Join-Path $env:USERPROFILE ".astrbot\data"),
    [switch]$PurgeData
)

$ErrorActionPreference = "Stop"

$pluginName = "astrbot_plugin_ctx_dump"
$target = Join-Path $AstrBotData "plugins\$pluginName"

if (Test-Path $target) {
    Remove-Item $target -Recurse -Force
    Write-Host "Removed plugin: $target"
} else {
    Write-Host "Plugin folder not found: $target"
}

if ($PurgeData) {
    $dataDir = Join-Path $AstrBotData "plugin_data\ctx_dump"
    if (Test-Path $dataDir) {
        Remove-Item $dataDir -Recurse -Force
        Write-Host "Removed data: $dataDir"
    } else {
        Write-Host "Data folder not found: $dataDir"
    }
} else {
    Write-Host "Recorded data kept at: $(Join-Path $AstrBotData 'plugin_data\ctx_dump')"
    Write-Host "Pass -PurgeData to delete it as well."
}

Write-Host "Next: restart AstrBot (or reload plugins) so the hooks and local page stop."
