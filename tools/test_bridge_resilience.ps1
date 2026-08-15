# Isolated test of sensor_bridge.ps1's per-iteration try/catch pattern: proves a forced
# exception on one iteration does NOT kill the loop, gets logged once (no spam), and the
# loop recovers and keeps running on the next iteration. Mirrors the real script's structure
# without touching LibreHardwareMonitor/hardware or the real mutex/output files.
$ErrorActionPreference = 'Stop'
$tempDir = Join-Path $env:TEMP 'tw_bridge_resilience_test'
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null
$errorLog = Join-Path $tempDir 'bridge_errors.log'
Remove-Item -LiteralPath $errorLog -ErrorAction SilentlyContinue

function Write-BridgeLog([string]$message) {
    $line = "[{0:yyyy-MM-dd HH:mm:ss}Z] {1}" -f ([DateTime]::UtcNow), $message
    Add-Content -LiteralPath $errorLog -Value $line -Encoding UTF8
}

$consecutiveErrors = 0
$wasErroring = $false
$iterations = 0
$survivedAfterFault = $false

while ($iterations -lt 5) {
    $iterations++
    try {
        if ($iterations -eq 2) {
            throw "simulated sensor read failure (iteration $iterations)"
        }
        # simulated successful poll
        if ($wasErroring) {
            Write-BridgeLog "recovered after $consecutiveErrors failed poll(s)"
            $wasErroring = $false
        }
        $consecutiveErrors = 0
        if ($iterations -gt 2) { $survivedAfterFault = $true }
    } catch {
        $consecutiveErrors++
        if (-not $wasErroring) {
            Write-BridgeLog "poll failed: $($_.Exception.Message)"
            $wasErroring = $true
        }
    }
    Start-Sleep -Milliseconds 100
}

Write-Output "iterations completed: $iterations (loop did not terminate on the forced exception)"
Write-Output "survived and succeeded AFTER the fault: $survivedAfterFault"
Write-Output "--- error log contents (should be exactly 2 lines: one failure, one recovery) ---"
Get-Content -LiteralPath $errorLog
$lineCount = (Get-Content -LiteralPath $errorLog | Measure-Object -Line).Lines
Write-Output "log line count: $lineCount (must be 2, not 5, i.e. no per-iteration spam)"
if ($iterations -eq 5 -and $survivedAfterFault -and $lineCount -eq 2) {
    Write-Output "PASS"
} else {
    Write-Output "FAIL"
    exit 1
}
