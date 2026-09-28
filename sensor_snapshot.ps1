$ErrorActionPreference = 'Stop'
$libDir = Join-Path $PSScriptRoot 'LibreHardwareMonitor'
$dll = Join-Path $libDir 'LibreHardwareMonitorLib.dll'
[Environment]::CurrentDirectory = $libDir
Add-Type -Path $dll

$c = New-Object LibreHardwareMonitor.Hardware.Computer
$c.IsCpuEnabled = $true
$c.IsGpuEnabled = $false
$c.IsMotherboardEnabled = $true
$c.IsMemoryEnabled = $true
$c.IsStorageEnabled = $true
$c.Open()

try {
    $readings = @()
    foreach ($hardware in $c.Hardware) {
        $hardware.Update()
        foreach ($child in $hardware.SubHardware) { $child.Update() }
        foreach ($device in @($hardware) + @($hardware.SubHardware)) {
            foreach ($sensor in $device.Sensors) {
                if ($sensor.SensorType -in @('Temperature','Fan','Power','Clock','Voltage','Control')) {
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
    ConvertTo-Json -InputObject @($readings) -Depth 4 -Compress
}
finally {
    try { $c.Close() } catch {}
}
