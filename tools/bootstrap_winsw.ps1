[CmdletBinding()]param([string]$ProjectRoot=(Resolve-Path "$PSScriptRoot\\..").Path)
$ErrorActionPreference='Stop';$m=Get-Content -Raw "$ProjectRoot\deploy\windows\winsw-v2.12.0.json"|ConvertFrom-Json;$d="$ProjectRoot\.local-tools\winsw\WinSW-x64.exe";New-Item -ItemType Directory -Force (Split-Path $d)|Out-Null
if(Test-Path $d){if((Get-FileHash $d -Algorithm SHA256).Hash -eq $m.sha256){exit 0}};$t="$d.download";try{Invoke-WebRequest -UseBasicParsing $m.url -OutFile $t;if((Get-FileHash $t -Algorithm SHA256).Hash -ne $m.sha256){throw 'WinSW SHA256 mismatch'};Move-Item $t $d -Force}finally{Remove-Item $t -Force -ErrorAction SilentlyContinue}

