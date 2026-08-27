[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^S-1-5-21-(\d+-){2}\d+-\d+$')][string]$TargetSid,
    [string]$BackupPath = "$env:ProgramData\LIVE15\service-acl-backup\service-sddl-original.json"
)
$ErrorActionPreference = "Stop"
$services = @("LIVE15Recorder","LIVE15ControlCenter","LIVE15RuntimeSupervisor")
$ace = "(A;;LCRPWPLO;;;$TargetSid)"
$original = [ordered]@{}
New-Item -ItemType Directory -Force -Path (Split-Path $BackupPath) | Out-Null
foreach ($svc in $services) {
    $sddl = (((sc.exe sdshow $svc) -join "") -replace '\s','')
    if ($sddl -notmatch '^D:') { throw "Invalid live SDDL: $svc" }
    $original[$svc] = $sddl
}
$original | ConvertTo-Json | Set-Content -LiteralPath $BackupPath -Encoding UTF8
foreach ($svc in $services) {
    $sddl = $original[$svc]
    if ($sddl -notmatch [regex]::Escape($ace)) {
        $i = $sddl.IndexOf('S:')
        $updated = if ($i -ge 0) { $sddl.Insert($i,$ace) } else { $sddl + $ace }
        sc.exe sdset $svc $updated | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "sdset failed: $svc" }
    }
    $live = (((sc.exe sdshow $svc) -join "") -replace '\s','')
    if ($live -notmatch [regex]::Escape($ace)) { throw "ACE persistence verification failed: $svc" }
}
Write-Output "LIVE15 delegation verified; backup=$BackupPath"
