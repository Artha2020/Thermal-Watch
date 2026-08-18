$ErrorActionPreference = 'Stop'

# Get-Command, for a plain application/exe lookup (unlike its behavior for cmdlets/functions),
# returns EVERY match found on PATH, not just the first. On a machine with more than one
# pythonw.exe on PATH - several separate Python installs, or an unrelated project's venv ahead of
# the intended interpreter - (Get-Command pythonw.exe).Source silently becomes an array instead
# of a single path string, and Start-Process -FilePath with an array argument can end up starting
# more than one Thermal Watch UI process from a single launch. Select-Object -First 1 makes the
# choice explicit: the SAME interpreter a bare `pythonw.exe` invocation from a shell would already
# resolve to (highest PATH-priority match), so existing selection behavior is unchanged - just
# guaranteed to be a single value regardless of how many pythonw.exe installs exist on this
# machine. Broken out as its own function so a test can call it in isolation without triggering
# this script's real elevation/launch side effects (see the dot-source guard below).
function Resolve-ThermalWatchPythonw {
    $found = Get-Command pythonw.exe -ErrorAction Stop
    return ($found | Select-Object -First 1).Source
}

if ($MyInvocation.InvocationName -eq '.') {
    # Dot-sourced (e.g. by tools/verify_launch_interpreter_resolution.py) - only
    # Resolve-ThermalWatchPythonw is needed; skip the real launcher body below (elevation prompt,
    # Start-Process) so this file is safe to load for testing without side effects.
    return
}

$app = Join-Path $PSScriptRoot 'app.py'
$bridge = Join-Path $PSScriptRoot 'sensor_bridge.ps1'
$pythonw = Resolve-ThermalWatchPythonw
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
