# Packaging single-file GUI executables

Py NIC Manager includes PyInstaller helper scripts for building a single-file
GUI executable on Windows and Linux. The scripts do not change the application
logic; they create an isolated build virtual environment, install the current
checkout, bundle package assets, and write the executable to `dist_exe/`.

Build on the target operating system. PyInstaller does not reliably
cross-compile Windows executables from Linux or Linux executables from Windows.

## Windows

From a PowerShell prompt at the repository root:

```powershell
.\scripts\build_windows_onefile.ps1
```

Optional parameters:

```powershell
.\scripts\build_windows_onefile.ps1 -Clean
.\scripts\build_windows_onefile.ps1 -OutputName PyNICManager
.\scripts\build_windows_onefile.ps1 -Python C:\Path\To\python.exe
.\scripts\build_windows_onefile.ps1 -DistDir dist_exe
```

The default output is:

```text
dist_exe\PyNICManager.exe
```

The Windows script passes PyInstaller's `--onefile` and `--windowed` options,
so the GUI executable does not open a console window. When the program needs
administrator access, the frozen executable relaunches itself through
the bundled `py-admin-launch` module instead of relying on an external
`py-admin-launch` command or the `py-nic-manager` console-script entry point.

## Linux

From a shell at the repository root:

```bash
chmod +x scripts/build_linux_onefile.sh
./scripts/build_linux_onefile.sh
```

Optional environment variables:

```bash
CLEAN=1 ./scripts/build_linux_onefile.sh
OUTPUT_NAME=py-nic-manager ./scripts/build_linux_onefile.sh
PYTHON=/usr/bin/python3 ./scripts/build_linux_onefile.sh
DIST_DIR=dist_exe ./scripts/build_linux_onefile.sh
```

The default output is:

```text
dist_exe/py-nic-manager
```

The Linux script also passes PyInstaller's `--onefile` and `--windowed`
options. On Linux this means the executable is a GUI program and does not
create its own terminal window when launched from a desktop entry or file
manager. If you start it from an existing shell, that shell remains visible
because the parent terminal belongs to the caller.

## Bundled assets

Both scripts bundle the package assets required by the GUI and network helper
features:

- JetBrains Mono font files used by the Tkinter interface.
- TAP-Windows6 driver assets used by Windows virtual NIC creation.
- Wintun DLL assets used by the Windows fallback virtual NIC path.

The frozen entry point also supports the internal helper-module calls that the
GUI uses for privileged operations, such as Windows loopback creation, Windows
virtual NIC creation, Linux NAT persistence, Linux global forwarding
persistence, and TTL-exceeded ICMP rules.

## Notes

- Build output directories are ignored by Git through the existing `build/` and
  `dist/` rules; `dist_exe/` is also ignored by this project.
- The generated executable still needs administrator/root privileges for
  mutating network operations.
- Linux builds require system GUI libraries for Tkinter. Install your
  distribution's Tk package if Python was built without Tk support.
