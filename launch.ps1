$ErrorActionPreference = 'Stop'
$app = Join-Path $PSScriptRoot 'app.py'
$bridge = Join-Path $PSScriptRoot 'sensor_bridge.ps1'
$pythonw = (Get-Command pythonw.exe).Source
$statusPath = Join-Path $env:ProgramData 'ThermalWatch\bridge_status.json'

# Cheap, UNELEVATED health check - reading ProgramData and querying whether a PID exists needs
# no privilege. Used purely to decide whether to bother the user with a UAC prompt at all; the
# bridge's own named mutex (see sensor_bridge.ps1) remains the actual race-free guarantee against
# duplicate instances regardless of what this check concludes.
function Test-BridgeHealthy {
    if (-not (Test-Path -LiteralPath $statusPath)) { return $false }
    try {
        $status = Get-Content -LiteralPath $statusPath -Raw | ConvertFrom-Json
    } catch {
        return $false
    }
    if (-not $status.pid) { return $false }
    $proc = Get-Process -Id $status.pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $false }
    $ageSeconds = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $status.last_poll_utc
    return $ageSeconds -lt 10
}

# Only the read-only hardware bridge is elevated; the UI stays unprivileged. Skip the elevation
# prompt entirely if a healthy bridge is already running (session-persistent by design - see
# app.py's App.close() docstring - so this is the common case on every launch after the first).
if (Test-BridgeHealthy) {
    Write-Output "Sensor bridge already healthy, skipping elevation."
} else {
    Start-Process -FilePath 'powershell.exe' `
        -ArgumentList ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $bridge) `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -Verb RunAs
    Start-Sleep -Milliseconds 800
}

Start-Process -FilePath $pythonw -ArgumentList ('"{0}"' -f $app) -WorkingDirectory $PSScriptRoot
