# READ-ONLY diagnostic - does not touch sensor_bridge.ps1 or any production file.
# Dumps the FULL sensor object (Identifier/Min/Max/Index) for the SuperIO device only.
$ErrorActionPreference = 'Stop'
$libDir = Join-Path (Split-Path $PSScriptRoot -Parent) 'LibreHardwareMonitor'
$dll = Join-Path $libDir 'LibreHardwareMonitorLib.dll'
[Environment]::CurrentDirectory = $libDir
Add-Type -Path $dll

$computer = New-Object LibreHardwareMonitor.Hardware.Computer
$computer.IsMotherboardEnabled = $true
$computer.Open()

foreach ($hardware in $computer.Hardware) {
    Write-Output "=== HARDWARE: $($hardware.HardwareType) / $($hardware.Name) / Identifier=$($hardware.Identifier) ==="
    $hardware.Update()
    foreach ($sub in $hardware.SubHardware) {
        Write-Output "  --- SUBHARDWARE: $($sub.HardwareType) / $($sub.Name) / Identifier=$($sub.Identifier) ---"
        $sub.Update()
        foreach ($s in $sub.Sensors) {
            if ($s.SensorType -eq 'Temperature') {
                [pscustomobject]@{
                    Name = $s.Name
                    Identifier = $s.Identifier.ToString()
                    Index = $s.Index
                    SensorType = $s.SensorType.ToString()
                    Value = $s.Value
                    Min = $s.Min
                    Max = $s.Max
                } | Format-List
            }
        }
    }
    foreach ($s in $hardware.Sensors) {
        if ($s.SensorType -eq 'Temperature') {
            [pscustomobject]@{
                Name = $s.Name
                Identifier = $s.Identifier.ToString()
                Index = $s.Index
                SensorType = $s.SensorType.ToString()
                Value = $s.Value
                Min = $s.Min
                Max = $s.Max
            } | Format-List
        }
    }
}
$computer.Close()
