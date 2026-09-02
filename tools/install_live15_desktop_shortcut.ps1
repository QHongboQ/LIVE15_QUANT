[CmdletBinding()]
param(
    [string]$ProjectRoot = (Resolve-Path "$PSScriptRoot\..").Path,
    [string]$DesktopPath = [Environment]::GetFolderPath("Desktop"),
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$launcher = Join-Path $projectRoot ".local-tools\Start LIVE15.cmd"
$sourceIcon = Join-Path $projectRoot "assets\live15-terminal.ico"
$installedIcon = Join-Path $projectRoot ".local-tools\live15-terminal.ico"
$shortcutPath = Join-Path $DesktopPath "LIVE15.lnk"

foreach ($required in @($launcher, $sourceIcon)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required LIVE15 desktop asset is missing: $required"
    }
}

if ($WhatIf) {
    Write-Output "READY: $shortcutPath -> $launcher (icon: $installedIcon)"
    exit 0
}

New-Item -ItemType Directory -Force -Path $DesktopPath | Out-Null
Copy-Item -LiteralPath $sourceIcon -Destination $installedIcon -Force

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.Arguments = ""
$shortcut.WorkingDirectory = Split-Path -Parent $launcher
$shortcut.IconLocation = "$installedIcon,0"
$shortcut.Save()

Write-Output "INSTALLED: $shortcutPath"
