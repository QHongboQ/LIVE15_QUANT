[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][ValidatePattern('^S-1-5-21-(\d+-){2}\d+-\d+$')][string]$TargetSid,
    [string]$BackupPath = "$env:ProgramData\LIVE15\service-acl-backup\service-sddl-original.json"
)
$ErrorActionPreference = "Stop"
$services = @("LIVE15Recorder","LIVE15ControlCenter")
$ace = "(A;;LCRPWPLO;;;$TargetSid)"
$original = [ordered]@{}
$rawOriginal = [ordered]@{}
New-Item -ItemType Directory -Force -Path (Split-Path $BackupPath) | Out-Null
foreach ($svc in $services) {
    $raw = ((sc.exe sdshow $svc) -join "")
    $sddl = ($raw -replace '\s','')
    if ($sddl -notmatch '^D:') { throw "Invalid live SDDL: $svc" }
    $saclIndex = $sddl.IndexOf('S:')
    $dacl = $sddl.Substring(2, $(if ($saclIndex -ge 0) { $saclIndex - 2 } else { $sddl.Length - 2 }))
    $opens = ([regex]::Matches($dacl, '\(')).Count
    $closes = ([regex]::Matches($dacl, '\)')).Count
    if (-not $dacl -or $opens -eq 0 -or $opens -ne $closes) {
        throw "Malformed DACL: $svc"
    }
    $original[$svc] = $sddl
    $rawOriginal[$svc] = $raw
}
([ordered]@{ sddl = $original; raw = $rawOriginal }) | ConvertTo-Json | Set-Content -LiteralPath $BackupPath -Encoding UTF8
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
    $beforeAces = [regex]::Matches($sddl, '\([^)]*\)') | ForEach-Object Value
    $afterAces = [regex]::Matches($live, '\([^)]*\)') | ForEach-Object Value
    foreach ($other in $beforeAces) {
        if ($other -ne $ace -and $afterAces -notcontains $other) {
            throw "Unrelated ACE changed: $svc"
        }
    }
}
Write-Output "LIVE15 delegation verified; backup=$BackupPath"
