[CmdletBinding()]
param(
    [ValidateRange(1025, 65535)]
    [int]$Port = 18081,
    [ValidatePattern('^[A-Fa-f0-9]{64}$')]
    [string]$ExpectedSha256,
    [Parameter(Mandatory)]
    [string]$EvidenceLog
)

$ErrorActionPreference = "Stop"

function Write-ShadowEvidence([string]$Message) {
    $directory = Split-Path -Parent $EvidenceLog
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    [IO.File]::AppendAllText(
        $EvidenceLog,
        "$(Get-Date -Format o) $Message$([Environment]::NewLine)",
        [Text.UTF8Encoding]::new($false)
    )
}

function Get-ArtifactSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace("-", "")
    }
    finally {
        $hasher.Dispose()
        $stream.Dispose()
    }
}

function Write-JsonResponse(
    [System.Net.Sockets.NetworkStream]$Stream,
    [int]$StatusCode,
    [hashtable]$Body
) {
    $payload = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Compress -Depth 4))
    $statusText = switch ($StatusCode) {
        200 { "OK" }
        404 { "Not Found" }
        405 { "Method Not Allowed" }
        503 { "Service Unavailable" }
        default { "Internal Server Error" }
    }
    $header = "HTTP/1.1 $StatusCode $statusText`r`nContent-Type: application/json; charset=utf-8`r`nContent-Length: $($payload.Length)`r`nConnection: close`r`n`r`n"
    $headerBytes = [Text.Encoding]::ASCII.GetBytes($header)
    $Stream.Write($headerBytes, 0, $headerBytes.Length)
    $Stream.Write($payload, 0, $payload.Length)
    $Stream.Flush()
}

$actualSha256 = Get-ArtifactSha256 $PSCommandPath
if (-not [string]::Equals($actualSha256, $ExpectedSha256, [StringComparison]::OrdinalIgnoreCase)) {
    throw "shadow artifact SHA-256 mismatch; refusing to listen"
}

$listener = [System.Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
$listener.Start()
Write-ShadowEvidence "event=started port=$Port production=false read_only=true artifact_sha256=$actualSha256"

try {
    while ($true) {
        $client = $listener.AcceptTcpClient()
        try {
            $stream = $client.GetStream()
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::ASCII, $false, 4096, $true)
            $requestLine = $reader.ReadLine()
            while ($true) {
                $headerLine = $reader.ReadLine()
                if ($null -eq $headerLine -or [string]::IsNullOrEmpty($headerLine)) {
                    break
                }
            }
            $parts = if ($null -eq $requestLine) { @() } else { $requestLine.Split(' ') }
            $method = if ($parts.Count -ge 1) { $parts[0] } else { "" }
            $path = if ($parts.Count -ge 2) { $parts[1] } else { "" }

            if ($method -ne "GET") {
                Write-JsonResponse $stream 405 @{ error = "read-only shadow"; production = $false }
            }
            elseif ($path -eq "/_nomad/healthz") {
                Write-JsonResponse $stream 200 @{
                    service = "LIVE15 Control Center Shadow"
                    scope = "nomad_non_production_shadow"
                    status = "ok"
                    production = $false
                    read_only = $true
                }
            }
            elseif ($path -eq "/api/health" -or $path -eq "/api/markets") {
                Write-JsonResponse $stream 503 @{
                    availability = "unavailable"
                    reason = "no non-production projection source configured"
                    fail_closed = $true
                    production = $false
                    read_only = $true
                }
            }
            else {
                Write-JsonResponse $stream 404 @{ error = "not found"; production = $false }
            }
        }
        catch {
            Write-ShadowEvidence "event=request_error error=$($_.Exception.GetType().Name)"
        }
        finally {
            $client.Dispose()
        }
    }
}
finally {
    $listener.Stop()
    Write-ShadowEvidence "event=stopped"
}
