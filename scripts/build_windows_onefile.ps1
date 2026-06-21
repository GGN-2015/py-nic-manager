param(
    [string]$OutputName = "PyNICManager",
    [string]$Python = "python",
    [string]$DistDir = "dist_exe",
    [string]$WorkDir = "build/pyinstaller-windows",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Invoke-Checked {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($ArgumentList -join ' ')"
    }
}

if ($Clean) {
    Remove-Item -LiteralPath $DistDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
}

$VenvDir = Join-Path $WorkDir ".venv"
if (-not (Test-Path $VenvDir)) {
    Invoke-Checked $Python @("-m", "venv", $VenvDir)
}

$VenvPython = Join-Path $VenvDir "Scripts/python.exe"
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $VenvPython @("-m", "pip", "install", "--upgrade", "pyinstaller")
Invoke-Checked $VenvPython @("-m", "pip", "install", ".")

$Separator = ";"
$FontsPath = (Resolve-Path "py_nic_manager\assets\fonts").Path
$TapPath = (Resolve-Path "py_nic_manager\assets\tap-windows6").Path
$WintunPath = (Resolve-Path "py_nic_manager\assets\wintun").Path
$DataArgs = @(
    "--add-data", "${FontsPath}${Separator}py_nic_manager/assets/fonts",
    "--add-data", "${TapPath}${Separator}py_nic_manager/assets/tap-windows6",
    "--add-data", "${WintunPath}${Separator}py_nic_manager/assets/wintun"
)

$HiddenImports = @(
    "--hidden-import", "py_admin_launch",
    "--hidden-import", "py_nic_manager.app",
    "--hidden-import", "py_nic_manager.qt_app",
    "--hidden-import", "py_nic_manager.windows_loopback",
    "--hidden-import", "py_nic_manager.windows_virtual",
    "--hidden-import", "py_nic_manager.windows_wintun",
    "--hidden-import", "py_nic_manager.ttl_exceeded",
    "--hidden-import", "py_nic_manager.nat_persistence",
    "--hidden-import", "py_nic_manager.global_forwarding",
    "--hidden-import", "py_nic_manager.macos_forwarding"
)

$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $OutputName,
    "--distpath", $DistDir,
    "--workpath", $WorkDir,
    "--specpath", $WorkDir
) + $DataArgs + $HiddenImports + @("py_nic_manager/frozen_entry.py")

Invoke-Checked $VenvPython (@("-m", "PyInstaller") + $PyInstallerArgs)

Write-Host "Built $DistDir/${OutputName}.exe"
