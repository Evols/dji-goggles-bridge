# Called after launcher confirmation. Elevate this helper, never the receiver.
param([switch]$Elevated)
$ErrorActionPreference = 'Stop'
$script:NetworkSetupPath = $PSCommandPath
. (Join-Path $PSScriptRoot 'network_check.ps1')

function Get-UniqueGogglesDevice {
    $devices = @(Get-GogglesDevices)
    if ($devices.Count -ne 1) { throw 'Connect exactly one supported headset before setup.' }
    return $devices[0]
}

function Restore-GogglesDriver($Device) {
    $hardwareId = 'USB\VID_2CA3&PID_0020&MI_00'
    if ($Device.HardwareID -notcontains $hardwareId) { throw 'Unsupported goggles hardware ID.' }
    $compatible = @($Device.CompatibleID)
    if ($compatible -contains 'USB\Class_E0&SubClass_01&Prot_03') {
        $infName = 'wceisvista.inf'
    } elseif ($compatible -contains 'USB\Class_EF&SubClass_04&Prot_01' -or
              $compatible -contains 'USB\MS_COMP_RNDIS&MS_SUBCOMP_5162001') {
        $infName = 'rndiscmp.inf'
    } else { throw 'No supported RNDIS compatible ID. Restore the driver using Device Manager.' }
    $source = Join-Path $env:windir ('INF\' + $infName)
    if (-not (Test-Path -LiteralPath $source)) { throw 'The Windows inbox RNDIS driver is missing.' }
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $staging = [IO.Path]::GetFullPath((Join-Path $tempRoot ('dji-rndis-' + [guid]::NewGuid())))
    if (-not $staging.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Driver staging must stay in the temporary directory.'
    }
    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        $inf = Join-Path $staging $infName
        Copy-Item -LiteralPath $source -Destination $inf
        if (-not ('GogglesDriverInstaller' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GogglesDriverInstaller {
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [DllImport("newdev.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool UpdateDriverForPlugAndPlayDevicesW(
        IntPtr parent, string hardwareId, string infPath, uint flags,
        [MarshalAs(UnmanagedType.Bool)] out bool rebootRequired);
}
'@
        }
        $current = Get-UniqueGogglesDevice
        if ($current.PNPDeviceID -ne $Device.PNPDeviceID) { throw 'The connected headset changed.' }
        $reboot = $false
        $installed = [GogglesDriverInstaller]::UpdateDriverForPlugAndPlayDevicesW(
            [IntPtr]::Zero, $hardwareId, $inf, 1, [ref]$reboot)
        if (-not $installed) {
            throw [ComponentModel.Win32Exception]::new([Runtime.InteropServices.Marshal]::GetLastWin32Error())
        }
        return $reboot
    } finally {
        $resolved = [IO.Path]::GetFullPath($staging)
        if ($resolved.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolved).StartsWith('dji-rndis-')) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
}

function Set-GogglesAddress($Adapter) {
    $index = [int]$Adapter.ifIndex
    if ($index -le 0) { throw 'Invalid goggles interface index.' }
    Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled
    foreach ($store in @('PersistentStore', 'ActiveStore')) {
        $routes = @(Get-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -PolicyStore $store -ErrorAction SilentlyContinue)
        foreach ($route in $routes) {
            Remove-NetRoute -InterfaceIndex $index -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' -NextHop $route.NextHop -PolicyStore $store -Confirm:$false
        }
        $addresses = @(Get-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -PolicyStore $store -ErrorAction SilentlyContinue)
        foreach ($address in $addresses) {
            Remove-NetIPAddress -InterfaceIndex $index -AddressFamily IPv4 -IPAddress $address.IPAddress -PolicyStore $store -Confirm:$false
        }
    }
    # Omitting PolicyStore creates entries in both active and persistent stores.
    New-NetIPAddress -InterfaceIndex $index -IPAddress '192.168.60.1' -PrefixLength 24 | Out-Null
    Set-DnsClientServerAddress -InterfaceIndex $index -ResetServerAddresses
}

function Invoke-GogglesSetup {
    $device = Get-UniqueGogglesDevice
    if ($device.ConfigManagerErrorCode -eq 22) {
        Enable-PnpDevice -InstanceId $device.PNPDeviceID -Confirm:$false
        $device = Get-UniqueGogglesDevice
    }
    $adapters = @(Get-GogglesAdapters $device)
    if ($device.ConfigManagerErrorCode -ne 0 -or $device.Service -notmatch 'rndis' -or $adapters.Count -ne 1) {
        if (Restore-GogglesDriver $device) { return 3010 }
        $adapters = @()
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            $current = Get-UniqueGogglesDevice
            if ($current.PNPDeviceID -ne $device.PNPDeviceID) { throw 'The connected headset changed.' }
            $adapters = @(Get-GogglesAdapters $current)
            if ($adapters.Count -eq 1 -and $current.ConfigManagerErrorCode -eq 0 -and
                $current.Service -match 'rndis') { break }
            Start-Sleep -Milliseconds 250
        }
    }
    if ($adapters.Count -ne 1) { throw 'Windows did not create a unique goggles network adapter.' }
    $adapter = $adapters[0]
    if ($adapter.Status -eq 'Disabled') {
        Enable-NetAdapter -InputObject $adapter -Confirm:$false
    } elseif ($adapter.Status -ne 'Up') { throw 'The goggles USB link is disconnected. Check OTG and the cable.' }
    $current = Get-UniqueGogglesDevice
    if ($current.PNPDeviceID -ne $device.PNPDeviceID -or $current.ConfigManagerErrorCode -ne 0 -or
        $current.Service -notmatch 'rndis' -or
        @((Get-GogglesAdapters $current).ifIndex) -notcontains $adapter.ifIndex) {
        throw 'The goggles driver or adapter changed before IP setup.'
    }
    Set-GogglesAddress $adapter
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ((Confirm-GogglesNetwork) -eq 0) { return 0 }
        Start-Sleep -Milliseconds 250
    }
    throw 'The RNDIS settings were applied, but Windows still reports the link is not ready.'
}

function Test-GogglesAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return [Security.Principal.WindowsPrincipal]::new($identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Start-GogglesSetup {
    if (Test-GogglesAdministrator) {
        try { return (Invoke-GogglesSetup) } catch { Write-Host ('Setup failed: ' + $_.Exception.Message); return 1 }
    }
    if ($Elevated) { Write-Host 'Administrator permission was not granted.'; return 1 }
    Write-Host 'Waiting for the Windows administrator (UAC) system prompt and setup...'
    try {
        $process = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $script:NetworkSetupPath + '"'), '-Elevated')
        return $process.ExitCode
    } catch { Write-Host 'Setup was cancelled or the administrator helper could not start.'; return 1 }
}

if ($MyInvocation.InvocationName -ne '.') { exit (Start-GogglesSetup) }
