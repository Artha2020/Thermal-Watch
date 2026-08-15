$ErrorActionPreference = 'Stop'

# Only one bridge is needed, even if Thermal Watch is opened more than once. This mutex is the
# authoritative, race-free single-instance guarantee (unlike a PID-file check, which has an
# inherent check-then-act race). Callers should still prefer NOT to trigger UAC at all when a
# healthy bridge is already running - see Test-BridgeHealthy in launch.ps1 - but even if they do,
# this guarantees only one bridge ever actually runs.
$created = $false
$mutex = New-Object Threading.Mutex($true, 'Global\ThermalWatchSensorBridge', [ref]$created)
if (-not $created) { exit 0 }

$libDir = Join-Path $PSScriptRoot 'LibreHardwareMonitor'
$dll = Join-Path $libDir 'LibreHardwareMonitorLib.dll'
$dataDir = Join-Path $env:ProgramData 'ThermalWatch'
$output = Join-Path $dataDir 'sensors.json'
$temporary = Join-Path $dataDir 'sensors.tmp'
$statusPath = Join-Path $dataDir 'bridge_status.json'
$statusTemp = Join-Path $dataDir 'bridge_status.tmp'
$errorLog = Join-Path $dataDir 'bridge_errors.log'
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

# Small, timestamp-free-of-spam diagnostics: only written on a HEALTHY<->ERROR state transition,
# never once per poll, and capped so it can't grow unbounded over a long-running session.
function Write-BridgeLog([string]$message) {
    try {
        $line = "[{0:yyyy-MM-dd HH:mm:ss}Z] {1}" -f ([DateTime]::UtcNow), $message
        Add-Content -LiteralPath $errorLog -Value $line -Encoding UTF8
        $lines = Get-Content -LiteralPath $errorLog -ErrorAction SilentlyContinue
        if ($lines -and $lines.Count -gt 200) {
            Set-Content -LiteralPath $errorLog -Value ($lines[-200..-1]) -Encoding UTF8
        }
    } catch {
        # Diagnostics must never be able to take the bridge down.
    }
}

# Health/status file for the (unelevated) Python UI and launch.ps1 to read - a companion file,
# NOT a change to sensors.json's schema, which stays exactly {timestamp, sensors}.
function Write-BridgeStatus([string]$state, $lastSuccessUtc, $consecutiveErrors, $lastErrorMessage) {
    try {
        $obj = [pscustomobject]@{
            pid                = $PID
            start_utc          = $script:startUtc
            state              = $state
            last_poll_utc      = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            last_success_utc   = $lastSuccessUtc
            consecutive_errors = $consecutiveErrors
            last_error         = $lastErrorMessage
        }
        $obj | ConvertTo-Json -Compress | Set-Content -LiteralPath $statusTemp -Encoding UTF8
        Move-Item -LiteralPath $statusTemp -Destination $statusPath -Force
    } catch {
        # Status reporting is best-effort; a failure here must not affect the main loop.
    }
}

$script:startUtc = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()

# Startup itself can fail transiently (DLL still being written by an AV scan, driver momentarily
# busy, etc.) - retry a few times before giving up, rather than a single Add-Type/Open() failure
# permanently preventing the bridge from ever starting this session.
$computer = $null
$startupAttempts = 0
while (-not $computer) {
    $startupAttempts++
    try {
        [Environment]::CurrentDirectory = $libDir
        Add-Type -Path $dll
        $c = New-Object LibreHardwareMonitor.Hardware.Computer
        $c.IsCpuEnabled = $true
        $c.IsGpuEnabled = $true
        $c.IsMotherboardEnabled = $true
        $c.IsMemoryEnabled = $true
        $c.IsStorageEnabled = $true
        $c.Open()
        $computer = $c
    } catch {
        Write-BridgeLog "startup attempt $startupAttempts failed: $($_.Exception.Message)"
        if ($startupAttempts -ge 5) {
            Write-BridgeStatus 'ERROR' $null 0 "startup failed after $startupAttempts attempts: $($_.Exception.Message)"
            $mutex.ReleaseMutex()
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}

$consecutiveErrors = 0
$lastSuccessUtc = $null
$wasErroring = $false

try {
    while ($true) {
        try {
            $readings = @()
            foreach ($hardware in $computer.Hardware) {
                $hardware.Update()
                foreach ($child in $hardware.SubHardware) { $child.Update() }
                foreach ($device in @($hardware) + @($hardware.SubHardware)) {
                    foreach ($sensor in $device.Sensors) {
                        if ($sensor.SensorType -in @('Temperature', 'Fan', 'Power', 'Clock', 'Voltage', 'Control')) {
                            # Identifier is LHM's own stable per-sensor path (e.g.
                            # "/lpc/nct6687d/0/temperature/5") - survives Name changes across
                            # BIOS/LHM versions. Added alongside the existing fields, which are
                            # unchanged, so older app.py builds (or Tier 2/3, which don't emit
                            # this field) keep working exactly as before.
                            $readings += [pscustomobject]@{
                                Name = $sensor.Name
                                Identifier = $sensor.Identifier.ToString()
                                SensorType = $sensor.SensorType.ToString()
                                Value = $sensor.Value
                                Parent = $device.HardwareType.ToString() + ' ' + $device.Name
                            }
                        }
                    }
                }
            }

            # Atomic write: full snapshot to a temp file, then a same-volume rename (atomic on
            # NTFS) over the real path - Thermal Watch can never observe a half-written file.
            [pscustomobject]@{
                timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                sensors = $readings
            } | ConvertTo-Json -Depth 4 -Compress | Set-Content -LiteralPath $temporary -Encoding UTF8
            Move-Item -LiteralPath $temporary -Destination $output -Force

            $lastSuccessUtc = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
            if ($wasErroring) {
                Write-BridgeLog "recovered after $consecutiveErrors failed poll(s)"
                $wasErroring = $false
            }
            $consecutiveErrors = 0
            Write-BridgeStatus 'HEALTHY' $lastSuccessUtc 0 $null
        } catch {
            # A single bad poll (a sensor throwing, a locked temp file, whatever) must not kill
            # the bridge: log concisely, skip writing sensors.json this cycle (its timestamp just
            # ages by one interval, which Thermal Watch already treats as staleness), and retry
            # next cycle.
            $consecutiveErrors++
            if (-not $wasErroring) {
                Write-BridgeLog "poll failed: $($_.Exception.Message)"
                $wasErroring = $true
            }
            Write-BridgeStatus 'ERROR' $lastSuccessUtc $consecutiveErrors $_.Exception.Message
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    $computer.Close()
    $mutex.ReleaseMutex()
}
