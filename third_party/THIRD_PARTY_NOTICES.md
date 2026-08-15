# Thermal Watch third-party components

Thermal Watch itself has no project license at this time. The notices in this
directory apply only to the identified third-party components distributed with
the packaged Windows application.

The v1.0.1 Windows package contains unmodified binary releases and runtime
components from the projects below. Corresponding license texts are in
`third_party/licenses/` and are installed beside the application.

## Hardware-monitoring components

| Component and shipped files | Version | License | Source and redistribution information |
| --- | --- | --- | --- |
| LibreHardwareMonitor (`LibreHardwareMonitor.exe`, `LibreHardwareMonitorLib.dll`) | 0.9.6, commit `3d331e3370efb858411f19511373eff65a218701` | Mozilla Public License 2.0 | [Upstream source at the exact release tag](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6). The binaries are unmodified. See `licenses/LibreHardwareMonitor-MPL-2.0.txt` and `licenses/LibreHardwareMonitor-THIRD-PARTY-NOTICES.txt`. Source for the MPL-covered executable form is available from that exact upstream tag. |
| Aga.Controls (`Aga.Controls.dll`) | 1.7.0.0, built by LibreHardwareMonitor 0.9.6 | BSD 3-Clause-style license | Included in LibreHardwareMonitor's exact v0.9.6 third-party notice file. Source is in the [LibreHardwareMonitor v0.9.6 tree](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6/Aga.Controls). |
| PawnIO.Modules material embedded by LibreHardwareMonitor | LibreHardwareMonitor 0.9.6 release | GNU LGPL 2.1 | Included in LibreHardwareMonitor's exact v0.9.6 third-party notice file. Source is in the [LibreHardwareMonitor v0.9.6 tree](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6/LibreHardwareMonitorLib/Resources/PawnIo). |
| DiskInfoToolkit (`DiskInfoToolkit.dll`) | 1.1.2, commit `25319eae5781e75bcf141e844ceab2afe94d40ea` | Mozilla Public License 2.0 | [Exact upstream source](https://github.com/Blacktempel/DiskInfoToolkit/tree/25319eae5781e75bcf141e844ceab2afe94d40ea). The binary is unmodified. The MPL text is `licenses/LibreHardwareMonitor-MPL-2.0.txt`. |
| RAMSPDToolkit-NDD (`RAMSPDToolkit-NDD.dll`) | 1.4.2, commit `3b47b960e0830fef344624ad5e389675d5f0a1ce` | Mozilla Public License 2.0 | [Exact upstream source](https://github.com/Blacktempel/RAMSPDToolkit/tree/3b47b960e0830fef344624ad5e389675d5f0a1ce). The binary is unmodified. The MPL text is `licenses/LibreHardwareMonitor-MPL-2.0.txt`. |
| BlackSharp.Core (`BlackSharp.Core.dll`) | 1.0.7, commit `c70b735c6cec123ee8a046ac4a0bc6c606f52cf0` | Mozilla Public License 2.0 | [Exact upstream source](https://github.com/Blacktempel/BlackSharp/tree/c70b735c6cec123ee8a046ac4a0bc6c606f52cf0). The binary is unmodified. The MPL text is `licenses/LibreHardwareMonitor-MPL-2.0.txt`. |
| HidSharp (`HidSharp.dll`) | 2.6.4 | Apache License 2.0 | [Upstream project](https://github.com/IntergatedCircuits/HidSharp). See `licenses/HidSharp-2.6.4-Apache-2.0.txt`, copied verbatim from the 2.6.4 NuGet package. |
| OxyPlot (`OxyPlot.dll`, `OxyPlot.WindowsForms.dll`) | 2.2.0, commit `74d1600e66199bbf8630c79929e1d0fa46e4101d` | MIT | [Exact upstream source](https://github.com/oxyplot/oxyplot/tree/74d1600e66199bbf8630c79929e1d0fa46e4101d). See `licenses/OxyPlot-2.2.0-MIT.txt`. |
| TaskScheduler (`Microsoft.Win32.TaskScheduler.dll` and localized resource assemblies) | 2.12.2, commit `8f4803cf060b35f8299db26b45bfd6ff0f599c3c` | MIT | [Exact upstream source](https://github.com/dahall/TaskScheduler/tree/8f4803cf060b35f8299db26b45bfd6ff0f599c3c). See `licenses/TaskScheduler-2.12.2-MIT.md`. |
| Microsoft .NET runtime support assemblies (`Microsoft.Bcl.*.dll`, `System.*.dll`) | Versions recorded in each assembly | MIT | [dotnet/runtime source](https://github.com/dotnet/runtime). See `licenses/Microsoft-dotnet-runtime-MIT.txt`. |

## Packaged Python runtime

| Component and shipped files | Version | License | Notice |
| --- | --- | --- | --- |
| Python and its standard library (`python314.dll`, `base_library.zip`, extension modules) | 3.14.6 | Python Software Foundation License and bundled historical licenses | `licenses/Python-3.14.6-LICENSE.txt`, copied from the exact interpreter used for the build. |
| PyInstaller bootloader (`ThermalWatch.exe` launcher) | 6.22.0 | GPL-2.0-or-later with the PyInstaller bootloader exception | See `licenses/PyInstaller-6.22.0-COPYING.txt` and `licenses/PyInstaller-6.22.0-license-and-bootloader-exception.rst`. The exception permits distribution of generated executables without imposing the GPL on the bundled application. |
| OpenSSL (`libcrypto-3.dll`, `libssl-3.dll`) | 3.5.7 | Apache License 2.0 | `licenses/OpenSSL-3.5.7-Apache-2.0.txt`, from the exact upstream release. |
| libffi (`libffi-8.dll`) | 3.4.4 | MIT-style license | `licenses/libffi-3.4.4-LICENSE.txt`, from the exact upstream release. |
| Tcl/Tk (`tcl86t.dll`, `tk86t.dll`, Tcl/Tk data files) | 8.6.15 | Tcl/Tk license | `licenses/Tcl-Tk-8.6.15-license.terms`. Tk's own installed `_tk_data/license.terms` is also retained. |
| zlib (`zlib1.dll`) | 1.3.1 | zlib License | `licenses/zlib-1.3.1-LICENSE.txt`, from the exact upstream release. |
| SQLite (`sqlite3.dll`, Python SQLite extension) | 3.50.4 | Public domain | [SQLite copyright statement](https://www.sqlite.org/copyright.html). SQLite imposes no license-notice requirement. |
| Microsoft Universal CRT and Visual C++ runtime (`ucrtbase.dll`, `VCRUNTIME140.dll`, `api-ms-win-*.dll`) | Versions recorded in each file | Microsoft redistributable runtime components | Distributed as runtime prerequisites under Microsoft's Visual Studio licensing terms; no Thermal Watch ownership is asserted. |

No third-party component listed here has been modified by the Thermal Watch
project. Source links identify the corresponding upstream source for
source-availability obligations. This index is informational and is not a
license grant for Thermal Watch itself.
