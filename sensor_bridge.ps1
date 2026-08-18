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
$netProcOutput = Join-Path $dataDir 'network_processes.json'
$netProcTemp = Join-Path $dataDir 'network_processes.tmp'
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

# --- Per-process network capture (ETW Kernel-Network) --------------------------------------
# Optional, additive layer: a per-PID send/receive byte accumulator fed by a real-time ETW
# session on the Kernel-Network provider. Requires elevation (EnableTraceEx2 on this provider
# is denied unprivileged - already confirmed) which this bridge already has, so no separate
# elevation request is needed. Everything here is isolated from sensor polling on purpose: if
# the type fails to compile, capture fails to start, or the capture thread dies, the bridge
# must keep serving sensors.json exactly as before. Struct layouts are translated field-for-
# field from tools/_perprocess_network_etw_elevated_probe.py's independently size-verified
# ctypes structs (TRACE_LOGFILE_HEADER = 280 bytes; EVENT_PROPERTY_INFO = 24 bytes with
# NameOffset at byte 4), not re-derived here.
$netProcCaptureAvailable = $false
$netProcCaptureError = $null
try {
    $netCaptureSrc = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;

namespace ThermalWatchNet {

[StructLayout(LayoutKind.Sequential)]
public struct GUID {
    public uint Data1; public ushort Data2; public ushort Data3;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 8)] public byte[] Data4;
}

[StructLayout(LayoutKind.Sequential)]
public struct WNODE_HEADER {
    public uint BufferSize; public uint ProviderId; public ulong HistoricalContext;
    public long TimeStamp; public GUID Guid; public uint ClientContext; public uint Flags;
}

[StructLayout(LayoutKind.Sequential)]
public struct EVENT_TRACE_PROPERTIES {
    public WNODE_HEADER Wnode;
    public uint BufferSize; public uint MinimumBuffers; public uint MaximumBuffers;
    public uint MaximumFileSize; public uint LogFileMode; public uint FlushTimer;
    public uint EnableFlags; public int AgeLimit; public uint NumberOfBuffers;
    public uint FreeBuffers; public uint EventsLost; public uint BuffersWritten;
    public uint LogBuffersLost; public uint RealTimeBuffersLost; public IntPtr LoggerThreadId;
    public uint LogFileNameOffset; public uint LoggerNameOffset;
}

[StructLayout(LayoutKind.Sequential)]
public struct ENABLE_TRACE_PARAMETERS {
    public uint Version; public uint EnableProperty; public uint ControlFlags;
    public GUID SourceId; public IntPtr EnableFilterDesc; public uint FilterDescCount;
}

[StructLayout(LayoutKind.Sequential)]
public struct EVENT_HEADER {
    public ushort Size; public ushort HeaderType; public ushort Flags; public ushort EventProperty;
    public uint ThreadId; public uint ProcessId; public long TimeStamp; public GUID ProviderId;
    public ushort Id; public byte Version; public byte Channel; public byte Level;
    public byte Opcode; public ushort Task; public ulong Keyword;
    public uint KernelTime; public uint UserTime; public GUID ActivityId;
}

[StructLayout(LayoutKind.Sequential)]
public struct ETW_BUFFER_CONTEXT {
    public byte ProcessorNumber; public byte Alignment; public ushort LoggerId;
}

// Legacy EVENT_TRACE is still embedded in EVENT_TRACE_LOGFILEW even when the modern
// EventRecordCallback mode is selected. Omitting it shifts every following callback pointer.
[StructLayout(LayoutKind.Sequential)]
public struct EVENT_TRACE_HEADER {
    public ushort Size; public ushort FieldTypeFlags; public uint Version;
    public uint ThreadId; public uint ProcessId; public long TimeStamp;
    public GUID Guid; public ulong ProcessorTime;
}

[StructLayout(LayoutKind.Sequential)]
public struct EVENT_TRACE {
    public EVENT_TRACE_HEADER Header;
    public uint InstanceId; public uint ParentInstanceId; public GUID ParentGuid;
    public IntPtr MofData; public uint MofLength; public uint ClientContext;
}

[StructLayout(LayoutKind.Sequential)]
public struct EVENT_RECORD {
    public EVENT_HEADER EventHeader; public ETW_BUFFER_CONTEXT BufferContext;
    public ushort ExtendedDataCount; public ushort UserDataLength;
    public IntPtr ExtendedData; public IntPtr UserData; public IntPtr UserContext;
}

[StructLayout(LayoutKind.Sequential)]
public struct SYSTEMTIME {
    public ushort wYear, wMonth, wDayOfWeek, wDay, wHour, wMinute, wSecond, wMilliseconds;
}

[StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
public struct TIME_ZONE_INFORMATION {
    public int Bias;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string StandardName;
    public SYSTEMTIME StandardDate; public int StandardBias;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DaylightName;
    public SYSTEMTIME DaylightDate; public int DaylightBias;
}

[StructLayout(LayoutKind.Sequential)]
public struct TRACE_LOGFILE_HEADER {
    public uint BufferSize; public uint VersionUnion; public uint ProviderVersion;
    public uint NumberOfProcessors; public long EndTime; public uint TimerResolution;
    public uint MaximumFileSize; public uint LogFileMode; public uint BuffersWritten;
    [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)] public byte[] LogInstanceGuidUnion;
    public IntPtr LoggerName; public IntPtr LogFileName;
    public TIME_ZONE_INFORMATION TimeZone; public long BootTime; public long PerfFreq;
    public long StartTime; public uint ReservedFlags; public uint BuffersLost;
}

public delegate void EventRecordCallback(IntPtr pEvent);
public delegate uint BufferCallback(IntPtr pLogfile);

[StructLayout(LayoutKind.Sequential)]
public struct EVENT_TRACE_LOGFILEW {
    [MarshalAs(UnmanagedType.LPWStr)] public string LogFileName;
    [MarshalAs(UnmanagedType.LPWStr)] public string LoggerName;
    public long CurrentTime; public uint BuffersRead; public uint ProcessTraceMode;
    public EVENT_TRACE CurrentEvent;
    public TRACE_LOGFILE_HEADER LogfileHeader;
    [MarshalAs(UnmanagedType.FunctionPtr)] public BufferCallback BufferCallback;
    public uint BufferSize; public uint Filled; public uint EventsLost;
    [MarshalAs(UnmanagedType.FunctionPtr)] public EventRecordCallback EventRecordCallback;
    public uint IsKernelTrace; public IntPtr Context;
}

[StructLayout(LayoutKind.Sequential)]
public struct TDH_TRACE_EVENT_INFO_HEAD {
    public GUID ProviderGuid; public GUID EventGuid;
    public ushort Id; public byte Version; public byte Channel; public byte Level;
    public byte Opcode; public ushort Task; public ulong Keyword;
    public int DecodingSource;
    public uint ProviderNameOffset, LevelNameOffset, ChannelNameOffset, KeywordsNameOffset,
                TaskNameOffset, OpcodeNameOffset, EventMessageOffset, ProviderMessageOffset,
                BinaryXMLOffset, BinaryXMLSize, ActivityIDNameOffset, RelatedActivityIDNameOffset,
                PropertyCount, TopLevelPropertyCount;
    public int Flags;
}

[StructLayout(LayoutKind.Sequential)]
public struct PROPERTY_DATA_DESCRIPTOR {
    public ulong PropertyName; public uint ArrayIndex; public uint Reserved;
}

public static class NativeMethods {
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    public static extern uint StartTraceW(out ulong sessionHandle, string sessionName, ref EVENT_TRACE_PROPERTIES props);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    public static extern uint ControlTraceW(ulong sessionHandle, string sessionName, ref EVENT_TRACE_PROPERTIES props, uint controlCode);
    [DllImport("advapi32.dll")]
    public static extern uint EnableTraceEx2(ulong sessionHandle, ref GUID providerId, uint controlCode,
        byte level, ulong matchAnyKeyword, ulong matchAllKeyword, uint timeout, ref ENABLE_TRACE_PARAMETERS enableParams);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)]
    public static extern ulong OpenTraceW(ref EVENT_TRACE_LOGFILEW logfile);
    [DllImport("advapi32.dll")]
    public static extern uint ProcessTrace(ulong[] handleArray, uint handleCount, IntPtr startTime, IntPtr endTime);
    [DllImport("advapi32.dll")]
    public static extern uint CloseTrace(ulong traceHandle);
    [DllImport("tdh.dll")]
    public static extern uint TdhGetEventInformation(IntPtr pEvent, uint tdhContextCount, IntPtr tdhContext, IntPtr buffer, ref uint bufferSize);
    [DllImport("tdh.dll")]
    public static extern uint TdhGetPropertySize(IntPtr pEvent, uint tdhContextCount, IntPtr tdhContext,
        uint descriptorCount, ref PROPERTY_DATA_DESCRIPTOR descriptor, ref uint propertySize);
    [DllImport("tdh.dll")]
    public static extern uint TdhGetProperty(IntPtr pEvent, uint tdhContextCount, IntPtr tdhContext,
        uint descriptorCount, ref PROPERTY_DATA_DESCRIPTOR descriptor, uint bufferSize, byte[] buffer);
}

public static class NetworkProcessCapture {
    public const uint WNODE_FLAG_TRACED_GUID = 0x00020000;
    public const uint EVENT_TRACE_REAL_TIME_MODE = 0x00000100;
    public const uint EVENT_TRACE_CONTROL_STOP = 1;
    public const uint EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1;
    public const byte TRACE_LEVEL_INFORMATION = 4;
    public const uint PROCESS_TRACE_MODE_REAL_TIME = 0x00000100;
    public const uint PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000;
    public const uint ERROR_SUCCESS = 0;
    public const uint ERROR_INSUFFICIENT_BUFFER = 122;
    public const ulong INVALID_HANDLE = 0xFFFFFFFFFFFFFFFF;
    public static readonly Guid KernelNetworkGuid = new Guid("7DD42A49-5329-4832-8DFD-43D979153A88");
    const string SessionName = "ThermalWatchNetCapture";

    static readonly object _lock = new object();
    static Dictionary<uint, ulong> _bytesIn = new Dictionary<uint, ulong>();
    static Dictionary<uint, ulong> _bytesOut = new Dictionary<uint, ulong>();
    static ulong _sessionHandle = 0;
    static ulong _traceHandle = 0;
    static Thread _thread;
    static EventRecordCallback _callbackRef;  // kept alive so the GC never collects the delegate
                                               // while native code still holds the function
                                               // pointer, which would corrupt the callback.

    public static bool Started = false;
    public static string LastError = null;

    static GUID ToGUID(Guid g) {
        var b = g.ToByteArray();
        var r = new GUID();
        r.Data1 = BitConverter.ToUInt32(b, 0);
        r.Data2 = BitConverter.ToUInt16(b, 4);
        r.Data3 = BitConverter.ToUInt16(b, 6);
        r.Data4 = new byte[8];
        Array.Copy(b, 8, r.Data4, 0, 8);
        return r;
    }

    static EVENT_TRACE_PROPERTIES MakeProperties(out uint totalSize) {
        int nameBytes = (SessionName.Length + 1) * 2;
        totalSize = (uint)(Marshal.SizeOf(typeof(EVENT_TRACE_PROPERTIES)) + nameBytes + 2);
        var props = new EVENT_TRACE_PROPERTIES();
        props.Wnode.BufferSize = totalSize;
        props.Wnode.Flags = WNODE_FLAG_TRACED_GUID;
        props.LogFileMode = EVENT_TRACE_REAL_TIME_MODE;
        props.BufferSize = 64;
        props.MinimumBuffers = 4;
        props.MaximumBuffers = 32;
        props.LoggerNameOffset = (uint)Marshal.SizeOf(typeof(EVENT_TRACE_PROPERTIES));
        props.LogFileNameOffset = 0;
        return props;
    }

    static IntPtr AllocProperties(out uint totalSize) {
        var props = MakeProperties(out totalSize);
        IntPtr buf = Marshal.AllocHGlobal((int)totalSize);
        for (int i = 0; i < totalSize; i++) Marshal.WriteByte(buf, i, 0);
        Marshal.StructureToPtr(props, buf, false);
        return buf;
    }

    static void DecodePropertyNames(IntPtr pEvent, out List<string> names, out IntPtr infoBuf, out uint infoSize) {
        names = new List<string>();
        infoBuf = IntPtr.Zero;
        infoSize = 0;
        uint size = 0;
        uint ret = NativeMethods.TdhGetEventInformation(pEvent, 0, IntPtr.Zero, IntPtr.Zero, ref size);
        if (ret != ERROR_INSUFFICIENT_BUFFER) return;
        infoBuf = Marshal.AllocHGlobal((int)size);
        infoSize = size;
        ret = NativeMethods.TdhGetEventInformation(pEvent, 0, IntPtr.Zero, infoBuf, ref size);
        if (ret != ERROR_SUCCESS) { Marshal.FreeHGlobal(infoBuf); infoBuf = IntPtr.Zero; return; }
        var head = (TDH_TRACE_EVENT_INFO_HEAD)Marshal.PtrToStructure(infoBuf, typeof(TDH_TRACE_EVENT_INFO_HEAD));
        const int EPI_SIZE = 24;
        int arrayOff = Marshal.SizeOf(typeof(TDH_TRACE_EVENT_INFO_HEAD));
        for (int i = 0; i < head.TopLevelPropertyCount; i++) {
            int recOff = arrayOff + i * EPI_SIZE;
            uint nameOffset = (uint)Marshal.ReadInt32(infoBuf, recOff + 4);
            string name = Marshal.PtrToStringUni(IntPtr.Add(infoBuf, (int)nameOffset));
            names.Add(name);
        }
    }

    static byte[] GetProperty(IntPtr pEvent, string name) {
        IntPtr pName = Marshal.StringToHGlobalUni(name);
        try {
            var desc = new PROPERTY_DATA_DESCRIPTOR();
            desc.PropertyName = (ulong)pName.ToInt64();
            desc.ArrayIndex = 0xFFFFFFFF;
            uint size = 0;
            uint ret = NativeMethods.TdhGetPropertySize(pEvent, 0, IntPtr.Zero, 1, ref desc, ref size);
            if (ret != ERROR_SUCCESS || size == 0) return null;
            byte[] buf = new byte[size];
            ret = NativeMethods.TdhGetProperty(pEvent, 0, IntPtr.Zero, 1, ref desc, size, buf);
            if (ret != ERROR_SUCCESS) return null;
            return buf;
        } finally {
            Marshal.FreeHGlobal(pName);
        }
    }

    static void OnEvent(IntPtr pEventPtr) {
        // Runs on ETW's own thread for every network send/receive event system-wide - must
        // never let an exception escape across the native->managed boundary and must stay fast.
        try {
            var ev = (EVENT_RECORD)Marshal.PtrToStructure(pEventPtr, typeof(EVENT_RECORD));
            byte opcode = ev.EventHeader.Opcode;
            // Kernel-Network opcodes: 10=send, 11=receive (actual data movement only - connect/
            // disconnect/retransmit events are not counted here).
            if (opcode != 10 && opcode != 11) return;

            List<string> names; IntPtr infoBuf; uint infoSize;
            DecodePropertyNames(pEventPtr, out names, out infoBuf, out infoSize);
            try {
                if (names.Count == 0) return;
                uint pid = 0; ulong size = 0;
                bool havePid = false, haveSize = false;
                if (names.Contains("PID")) {
                    var raw = GetProperty(pEventPtr, "PID");
                    if (raw != null && raw.Length == 4) { pid = BitConverter.ToUInt32(raw, 0); havePid = true; }
                }
                if (names.Contains("size")) {
                    var raw = GetProperty(pEventPtr, "size");
                    if (raw != null && raw.Length == 4) { size = BitConverter.ToUInt32(raw, 0); haveSize = true; }
                    else if (raw != null && raw.Length == 2) { size = BitConverter.ToUInt16(raw, 0); haveSize = true; }
                }
                if (!havePid || !haveSize) return;
                lock (_lock) {
                    var table = (opcode == 10) ? _bytesOut : _bytesIn;
                    ulong cur;
                    table.TryGetValue(pid, out cur);
                    table[pid] = cur + size;
                }
            } finally {
                if (infoBuf != IntPtr.Zero) Marshal.FreeHGlobal(infoBuf);
            }
        } catch {
            // A bad event decode is simply skipped - it must never take down the capture
            // thread or, via the native boundary, the whole bridge process.
        }
    }

    public static void StartCapture() {
        if (Started) return;
        try {
            uint stopSize;
            IntPtr stopBuf = AllocProperties(out stopSize);
            try {
                var stopProps = (EVENT_TRACE_PROPERTIES)Marshal.PtrToStructure(stopBuf, typeof(EVENT_TRACE_PROPERTIES));
                NativeMethods.ControlTraceW(0, SessionName, ref stopProps, EVENT_TRACE_CONTROL_STOP);
            } finally { Marshal.FreeHGlobal(stopBuf); }

            uint size;
            IntPtr propsBuf = AllocProperties(out size);
            var props = (EVENT_TRACE_PROPERTIES)Marshal.PtrToStructure(propsBuf, typeof(EVENT_TRACE_PROPERTIES));
            ulong sessionHandle;
            uint startRet = NativeMethods.StartTraceW(out sessionHandle, SessionName, ref props);
            Marshal.FreeHGlobal(propsBuf);
            if (startRet != ERROR_SUCCESS) {
                LastError = "StartTraceW failed: " + startRet;
                return;
            }
            _sessionHandle = sessionHandle;

            var providerGuid = ToGUID(KernelNetworkGuid);
            var enableParams = new ENABLE_TRACE_PARAMETERS();
            enableParams.Version = 2;
            uint enableRet = NativeMethods.EnableTraceEx2(sessionHandle, ref providerGuid, EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                TRACE_LEVEL_INFORMATION, 0, 0, 0, ref enableParams);
            if (enableRet != ERROR_SUCCESS) {
                LastError = "EnableTraceEx2 failed (needs elevation): " + enableRet;
                StopSession();
                return;
            }

            _callbackRef = new EventRecordCallback(OnEvent);
            var logfile = new EVENT_TRACE_LOGFILEW();
            logfile.LoggerName = SessionName;
            logfile.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD;
            logfile.EventRecordCallback = _callbackRef;

            ulong traceHandle = NativeMethods.OpenTraceW(ref logfile);
            if (traceHandle == INVALID_HANDLE) {
                LastError = "OpenTraceW failed: " + Marshal.GetLastWin32Error();
                StopSession();
                return;
            }
            _traceHandle = traceHandle;

            _thread = new Thread(() => {
                try {
                    var handles = new ulong[] { traceHandle };
                    NativeMethods.ProcessTrace(handles, 1, IntPtr.Zero, IntPtr.Zero);
                } catch (Exception e) {
                    LastError = "ProcessTrace thread error: " + e.Message;
                }
            });
            _thread.IsBackground = true;
            _thread.Start();
            Started = true;
            LastError = null;
        } catch (Exception e) {
            LastError = "StartCapture exception: " + e.Message;
            Started = false;
        }
    }

    static void StopSession() {
        try {
            uint size;
            IntPtr buf = AllocProperties(out size);
            try {
                var props = (EVENT_TRACE_PROPERTIES)Marshal.PtrToStructure(buf, typeof(EVENT_TRACE_PROPERTIES));
                NativeMethods.ControlTraceW(_sessionHandle, SessionName, ref props, EVENT_TRACE_CONTROL_STOP);
            } finally { Marshal.FreeHGlobal(buf); }
        } catch { }
    }

    public static void Stop() {
        try {
            if (_traceHandle != 0) NativeMethods.CloseTrace(_traceHandle);
            StopSession();
        } catch { }
        Started = false;
    }

    // Snapshot-and-reset: returns bytes accumulated since the last snapshot (a rate window),
    // then clears - the caller (the PowerShell side) is responsible for keeping a running total.
    public static Dictionary<uint, ulong[]> Snapshot() {
        lock (_lock) {
            var result = new Dictionary<uint, ulong[]>();
            foreach (var pid in new List<uint>(_bytesIn.Keys)) result[pid] = new ulong[] { _bytesIn[pid], 0 };
            foreach (var pid in new List<uint>(_bytesOut.Keys)) {
                if (result.ContainsKey(pid)) result[pid][1] = _bytesOut[pid];
                else result[pid] = new ulong[] { 0, _bytesOut[pid] };
            }
            _bytesIn.Clear();
            _bytesOut.Clear();
            return result;
        }
    }

}
}
'@
    Add-Type -TypeDefinition $netCaptureSrc -Language CSharp
    [ThermalWatchNet.NetworkProcessCapture]::StartCapture()
    $netProcCaptureAvailable = [ThermalWatchNet.NetworkProcessCapture]::Started
    $netProcCaptureError = [ThermalWatchNet.NetworkProcessCapture]::LastError
    if (-not $netProcCaptureAvailable) {
        Write-BridgeLog "network process capture unavailable: $netProcCaptureError"
    }
} catch {
    # Compile or start-up failure of the optional capture layer must never prevent the bridge
    # from doing its primary job (serving sensors.json).
    $netProcCaptureAvailable = $false
    $netProcCaptureError = $_.Exception.Message
    Write-BridgeLog "network process capture failed to initialize: $netProcCaptureError"
}
$netProcTotals = @{}  # running cumulative totals per PID, survives across polls; the C# side
                       # only ever hands back the delta since the last Snapshot() call.
$netProcNames = @{}   # short-lived PID -> name cache, cleared each poll alongside totals lookup

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

        # Network process capture is entirely separate from sensor polling above: its own
        # try/catch, its own file, and a failure here never touches $consecutiveErrors (which
        # drives sensors.json staleness detection in app.py) or bridge_status.json.
        if ($netProcCaptureAvailable) {
            try {
                $delta = [ThermalWatchNet.NetworkProcessCapture]::Snapshot()
                foreach ($pidKey in $delta.Keys) {
                    $pair = $delta[$pidKey]
                    if ($netProcTotals.ContainsKey($pidKey)) {
                        $netProcTotals[$pidKey][0] += $pair[0]
                        $netProcTotals[$pidKey][1] += $pair[1]
                    } else {
                        $netProcTotals[$pidKey] = @($pair[0], $pair[1])
                    }
                }
                $procList = @()
                foreach ($pidKey in $netProcTotals.Keys) {
                    $name = $netProcNames[$pidKey]
                    if (-not $name) {
                        try { $name = (Get-Process -Id $pidKey -ErrorAction Stop).ProcessName } catch { $name = $null }
                        if ($name) { $netProcNames[$pidKey] = $name }
                    }
                    $totals = $netProcTotals[$pidKey]
                    $procList += [pscustomobject]@{
                        pid = [int64]$pidKey
                        name = $name
                        bytes_in = [int64]$totals[0]
                        bytes_out = [int64]$totals[1]
                    }
                }
                [pscustomobject]@{
                    timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                    capture_active = $true
                    capture_error = $null
                    processes = $procList
                } | ConvertTo-Json -Depth 4 -Compress | Set-Content -LiteralPath $netProcTemp -Encoding UTF8
                Move-Item -LiteralPath $netProcTemp -Destination $netProcOutput -Force
            } catch {
                # One bad network-capture write cycle is skipped, exactly like a bad sensor poll -
                # it must not stop sensor polling or crash the bridge.
                Write-BridgeLog "network process write failed: $($_.Exception.Message)"
            }
        } elseif (-not (Test-Path -LiteralPath $netProcOutput)) {
            try {
                [pscustomobject]@{
                    timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                    capture_active = $false
                    capture_error = $netProcCaptureError
                    processes = @()
                } | ConvertTo-Json -Depth 4 -Compress | Set-Content -LiteralPath $netProcTemp -Encoding UTF8
                Move-Item -LiteralPath $netProcTemp -Destination $netProcOutput -Force
            } catch { }
        }

        Start-Sleep -Seconds 2
    }
}
finally {
    if ($netProcCaptureAvailable) {
        try { [ThermalWatchNet.NetworkProcessCapture]::Stop() } catch { }
    }
    $computer.Close()
    $mutex.ReleaseMutex()
}
