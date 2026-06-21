from __future__ import annotations

import platform
import subprocess

from .backends import decode_command_output
from .subprocess_utils import run_no_window


NET_SETUP_CLASS_GUID = "{4d36e972-e325-11ce-bfc1-08002be10318}"
DEVICE_INSTALL_RESTRICTIONS = r"HKLM:\SOFTWARE\Policies\Microsoft\Windows\DeviceInstall\Restrictions"


def ensure_ndis_device_install_policy() -> None:
    """Allow Py NIC Manager's Windows Net/NDIS adapter installs in local policy."""
    _ensure_windows()
    script = rf"""
$ErrorActionPreference = "Stop"
$base = "{DEVICE_INSTALL_RESTRICTIONS}"
$netClass = "{NET_SETUP_CLASS_GUID}"
$deviceIds = @("*MSLOOP", "ROOT\MSLOOP", "ROOT\NET", "ROOT\TAP0901", "TAP0901", "WINTUN", "WireGuard")

function Ensure-Key {{
  param([string]$Path)
  if (-not (Test-Path $Path)) {{
    New-Item -Path $Path -Force | Out-Null
  }}
}}

function Get-PolicyProperties {{
  param([string]$Path)
  if (-not (Test-Path $Path)) {{ return @() }}
  $item = Get-ItemProperty -Path $Path
  return @($item.PSObject.Properties | Where-Object {{ $_.Name -notlike "PS*" }})
}}

function Add-PolicyString {{
  param([string]$Path, [string]$Value)
  Ensure-Key $Path
  $existing = Get-PolicyProperties $Path
  foreach ($property in $existing) {{
    if ([string]$property.Value -ieq $Value) {{ return }}
  }}
  $index = 1
  $used = @{{}}
  foreach ($property in $existing) {{ $used[[string]$property.Name] = $true }}
  while ($used.ContainsKey([string]$index)) {{ $index++ }}
  New-ItemProperty -Path $Path -Name ([string]$index) -PropertyType String -Value $Value -Force | Out-Null
}}

function Remove-MatchingPolicyStrings {{
  param([string]$Path, [scriptblock]$Matcher)
  foreach ($property in (Get-PolicyProperties $Path)) {{
    $value = [string]$property.Value
    if (& $Matcher $value) {{
      Remove-ItemProperty -Path $Path -Name $property.Name -ErrorAction SilentlyContinue
    }}
  }}
}}

Ensure-Key $base
New-ItemProperty -Path $base -Name "AllowAdminInstall" -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $base -Name "AllowDenyLayered" -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $base -Name "AllowDeviceClasses" -PropertyType DWord -Value 1 -Force | Out-Null
New-ItemProperty -Path $base -Name "AllowDeviceIDs" -PropertyType DWord -Value 1 -Force | Out-Null

$allowClasses = Join-Path $base "AllowDeviceClasses"
$allowDeviceIds = Join-Path $base "AllowDeviceIDs"
Add-PolicyString $allowClasses $netClass
foreach ($deviceId in $deviceIds) {{ Add-PolicyString $allowDeviceIds $deviceId }}

$denyClasses = Join-Path $base "DenyDeviceClasses"
$denyDeviceIds = Join-Path $base "DenyDeviceIDs"
Remove-MatchingPolicyStrings $denyClasses {{ param($value) $value -ieq $netClass }}
Remove-MatchingPolicyStrings $denyDeviceIds {{
  param($value)
  $upper = $value.ToUpperInvariant()
    $upper -eq "*MSLOOP" -or
    $upper -eq "ROOT\MSLOOP" -or
    $upper -eq "ROOT\NET" -or
    $upper -eq "ROOT\TAP0901" -or
    $upper -eq "TAP0901" -or
    $upper -like "*WINTUN*" -or
    $upper -like "*WIREGUARD*"
}}

$remainingClassDeny = @(
  Get-PolicyProperties $denyClasses |
    Where-Object {{ [string]$_.Value -ieq $netClass }}
)
$remainingDeviceDeny = @(
  Get-PolicyProperties $denyDeviceIds |
    Where-Object {{
      $upper = ([string]$_.Value).ToUpperInvariant()
      $upper -eq "*MSLOOP" -or
        $upper -eq "ROOT\MSLOOP" -or
        $upper -eq "ROOT\NET" -or
        $upper -eq "ROOT\TAP0901" -or
        $upper -eq "TAP0901" -or
        $upper -like "*WINTUN*" -or
        $upper -like "*WIREGUARD*"
    }}
)

if ($remainingClassDeny.Count -or $remainingDeviceDeny.Count) {{
  throw "Windows device installation policy still explicitly blocks Net/NDIS adapters. If this computer is domain-managed, allow the Net setup class $netClass in Group Policy."
}}

[pscustomobject]@{{
  allow_admin_install = $true
  layered_evaluation = $true
  net_setup_class = $netClass
}} | ConvertTo-Json -Depth 3
"""
    _run_powershell(script)


def assert_ndis_net_adapter(
    *, name: str = "", interface_index: object | None = None, pnp_device_id: str = ""
) -> None:
    _ensure_windows()
    index_text = ""
    try:
        if interface_index is not None:
            index_text = str(int(interface_index))
    except (TypeError, ValueError):
        index_text = ""
    script = rf"""
$ErrorActionPreference = "Stop"
$name = "{_ps_escape(name)}"
$interfaceIndex = "{_ps_escape(index_text)}"
$pnpDeviceId = "{_ps_escape(pnp_device_id)}"

$adapter = $null
if ($interfaceIndex) {{
  $adapter = Get-NetAdapter -IncludeHidden -InterfaceIndex ([int]$interfaceIndex) -ErrorAction SilentlyContinue
}}
if (-not $adapter -and $name) {{
  $adapter = Get-NetAdapter -IncludeHidden -Name $name -ErrorAction SilentlyContinue
}}
if (-not $adapter -and $pnpDeviceId) {{
  $adapter = Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue |
    Where-Object {{ $_.PnPDeviceID -eq $pnpDeviceId }} |
    Select-Object -First 1
}}
if (-not $adapter) {{
  throw "The created adapter was not exposed through the Windows NetAdapter/NDIS stack."
}}

$device = $null
if ($adapter.PnPDeviceID) {{
  $device = Get-PnpDevice -InstanceId $adapter.PnPDeviceID -ErrorAction SilentlyContinue
}}
if ($device -and $device.Class -and $device.Class -ne "Net") {{
  throw "The created adapter is class '$($device.Class)', not the Windows Net/NDIS class."
}}

[pscustomobject]@{{
  name = $adapter.Name
  interface_index = $adapter.InterfaceIndex
  interface_description = $adapter.InterfaceDescription
  pnp_device_id = $adapter.PnPDeviceID
  class = if ($device) {{ $device.Class }} else {{ "Net" }}
}} | ConvertTo-Json -Depth 3
"""
    _run_powershell(script)


def _run_powershell(script: str) -> str:
    completed = run_no_window(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        check=False,
    )
    stdout = decode_command_output(completed.stdout).strip()
    stderr = decode_command_output(completed.stderr).strip()
    if completed.returncode != 0:
        raise RuntimeError(stderr or stdout or f"PowerShell failed with exit code {completed.returncode}.")
    return stdout


def _ps_escape(value: str) -> str:
    return value.replace("`", "``").replace('"', '`"').replace("$", "`$")


def _ensure_windows() -> None:
    if platform.system().lower() != "windows":
        raise RuntimeError("Windows device installation policy management is only available on Windows.")
