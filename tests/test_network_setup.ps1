# Synthetic privileged-setup tests. All PnP/network writes and UAC are mocked.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../src/apps/goggles_resources/network_setup.ps1')
. (Join-Path $PSScriptRoot 'network_fixtures.ps1')

# The native entry point is replaced with an in-process fake: no driver binding.
Add-Type -TypeDefinition @'
using System;
public static class GogglesDriverInstaller {
    public static int Calls;
    public static string HardwareId;
    public static string InfPath;
    public static uint Flags;
    public static bool Reboot;
    public static bool UpdateDriverForPlugAndPlayDevicesW(
        IntPtr parent, string hardwareId, string infPath, uint flags, out bool reboot) {
        Calls++; HardwareId=hardwareId; InfPath=infPath; Flags=flags;
        reboot=Reboot; return true;
    }
}
'@
$nativeRestore = ${function:Restore-GogglesDriver}
$nativeAddress = ${function:Set-GogglesAddress}
$script:PassedSetups = 0
$script:Writes = @()

function Record-Write($Name, $Index) {
    if ($Index -ne 11) { throw ('Write escaped the goggles interface: ' + $Index) }
    $script:Writes += $Name
}
function Set-NetIPInterface { param($InterfaceIndex, $AddressFamily, $Dhcp) Record-Write 'dhcp' $InterfaceIndex }
function Remove-NetIPAddress { param($InterfaceIndex, $AddressFamily, $IPAddress, $PolicyStore, $Confirm) Record-Write 'remove-ip' $InterfaceIndex }
function Remove-NetRoute { param($InterfaceIndex, $AddressFamily, $DestinationPrefix, $NextHop, $PolicyStore, $Confirm) Record-Write 'remove-route' $InterfaceIndex }
function New-NetIPAddress {
    param($InterfaceIndex, $IPAddress, $PrefixLength)
    Record-Write 'new-ip' $InterfaceIndex
    if ($IPAddress -ne '192.168.60.1' -or $PrefixLength -ne 24) { throw 'Incorrect goggles IP setup.' }
}
function Set-DnsClientServerAddress { param($InterfaceIndex, [switch]$ResetServerAddresses) Record-Write 'dns' $InterfaceIndex }
function Enable-NetAdapter { param($InputObject, $Confirm) Record-Write 'enable-adapter' $InputObject.ifIndex; $script:Adapters[0].Status = 'Up' }
function Enable-PnpDevice {
    param($InstanceId, $Confirm)
    if ($InstanceId -ne $script:PnpDevices[0].PNPDeviceID) { throw 'Wrong PnP device enabled.' }
    $script:PnpDevices[0].ConfigManagerErrorCode = 0
    $script:Writes += 'enable-pnp'
}
function Start-Sleep { param($Milliseconds) }
function Confirm-GogglesNetwork { return 0 }
function Restore-GogglesDriver {
    param($Device)
    $script:Writes += 'restore-driver'
    $script:PnpDevices[0].Service = 'usbrndis6'
    $script:PnpDevices[0].ConfigManagerErrorCode = 0
    return $script:RequireReboot
}
function Test-GogglesAdministrator { return $script:IsAdmin }
function Start-Process {
    param($FilePath, $Verb, $WindowStyle, [switch]$Wait, [switch]$PassThru, $ArgumentList)
    $script:Writes += 'uac'
    if ($Verb -ne 'RunAs' -or $WindowStyle -ne 'Hidden' -or -not $Wait -or -not $PassThru) { throw 'Incorrect UAC request.' }
    if ($ArgumentList -notcontains '-Elevated' -or
        $ArgumentList -notcontains ('"' + $script:NetworkSetupPath + '"')) { throw 'Wrong elevated helper.' }
    if ($script:CancelUac) { throw 'UAC cancelled' }
    return [pscustomobject]@{ ExitCode = 0 }
}

function Reset-Setup {
    Reset-Fixture
    $script:Writes = @()
    $script:RequireReboot = $false
    $script:IsAdmin = $true
    $script:CancelUac = $false
    [GogglesDriverInstaller]::Calls = 0
    [GogglesDriverInstaller]::Reboot = $false
}
function Assert-Setup($Name, $Expected = 0) {
    if ((Invoke-GogglesSetup) -ne $Expected) { throw ('Unexpected result: ' + $Name) }
    $script:PassedSetups++
    Write-Host ('PASS: ' + $Name)
}
function Assert-Refused($Name) {
    $refused = $false
    try { $null = Invoke-GogglesSetup } catch { $refused = $true }
    if (-not $refused -or $script:Writes.Count) { throw ('Setup was not safely refused: ' + $Name) }
    $script:PassedSetups++
    Write-Host ('PASS: ' + $Name)
}

Reset-Setup
Assert-Setup 'IP setup affects only goggles, not Internet WiFi'
if ($script:Writes -contains 'restore-driver' -or $script:Writes -notcontains 'new-ip') { throw 'Healthy driver was changed.' }
Reset-Setup
$script:PnpDevices[0].Service = 'WinUSB'
Assert-Setup 'Wrong driver is restored before IP setup'
if ($script:Writes[0] -ne 'restore-driver') { throw 'IP changed before driver restoration.' }
Reset-Setup
$script:Adapters[0].Status = 'Disabled'
Assert-Setup 'Disabled adapter is enabled'
Reset-Setup
$script:PnpDevices[0].ConfigManagerErrorCode = 22
Assert-Setup 'Disabled PnP device is enabled'
Reset-Setup
$script:PnpDevices = @()
Assert-Refused 'Missing headset'
Reset-Setup
$script:PnpDevices += [pscustomobject]@{ Present = $true; PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\SECOND' }
Assert-Refused 'Multiple headsets'
Reset-Setup
$script:PnpDevices[0].PNPDeviceID = 'USB\VID_2CA3&PID_001F&MI_03\V2'
Assert-Refused 'V2 never receives RNDIS driver or IP changes'
Reset-Setup
$script:PnpDevices += [pscustomobject]@{ Present = $true; PNPDeviceID = 'USB\VID_2CA3&PID_001F&MI_03\V2' }
Assert-Refused 'Mixed V2 and Goggles 3 rejected before setup writes'
Reset-Setup
$script:PnpDevices[0].PNPDeviceID = 'USB\VID_1234&PID_5678\PHONE'
Assert-Refused 'Unrelated phone'
Reset-Setup
$script:PnpDevices[0].Service = 'WinUSB'
$script:RequireReboot = $true
Assert-Setup 'Reboot requirement stops before IP writes' 3010
if ($script:Writes -contains 'new-ip') { throw 'IP changed despite reboot requirement.' }
Reset-Setup
$script:IsAdmin = $false
if ((Start-GogglesSetup) -ne 0 -or $script:Writes -notcontains 'uac') { throw 'Setup did not request UAC.' }
$script:PassedSetups++
Write-Host 'PASS: UAC request uses the bundled helper'
Reset-Setup
$script:IsAdmin = $false
$script:CancelUac = $true
if ((Start-GogglesSetup) -eq 0 -or $script:Writes -contains 'new-ip') { throw 'Cancelled UAC continued.' }
$script:PassedSetups++
Write-Host 'PASS: UAC cancellation makes no network writes'

# Exercise the actual inbox-INF selection/staging with a fake native installer.
Reset-Setup
$null = & $nativeRestore $script:PnpDevices[0]
if ([GogglesDriverInstaller]::Calls -ne 1 -or
    [GogglesDriverInstaller]::HardwareId -ne 'USB\VID_2CA3&PID_0020&MI_00' -or
    [IO.Path]::GetFileName([GogglesDriverInstaller]::InfPath) -ne 'wceisvista.inf' -or
    [GogglesDriverInstaller]::Flags -ne 1) { throw 'Driver selection was not scoped to the goggles.' }
if (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName([GogglesDriverInstaller]::InfPath))) { throw 'Driver staging was not cleaned up.' }
$script:PassedSetups++
Write-Host 'PASS: correct Windows inbox INF and exact hardware ID'
Reset-Setup
$script:PnpDevices[0].CompatibleID = @('USB\Class_EF&SubClass_04&Prot_01')
$null = & $nativeRestore $script:PnpDevices[0]
if ([IO.Path]::GetFileName([GogglesDriverInstaller]::InfPath) -ne 'rndiscmp.inf') { throw 'Wrong compatible RNDIS driver selected.' }
$script:PassedSetups++
Write-Host 'PASS: alternate RNDIS compatible ID uses its matching inbox INF'
Reset-Setup
$script:PnpDevices[0].CompatibleID = @('USB\UNSUPPORTED')
$rejected = $false
try { $null = & $nativeRestore $script:PnpDevices[0] } catch { $rejected = $true }
if (-not $rejected -or [GogglesDriverInstaller]::Calls) { throw 'Unsupported driver was installed.' }
$script:PassedSetups++
Write-Host 'PASS: unsupported compatible IDs never invoke driver installation'

$tokens = $null
$parseErrors = $null
$null = [Management.Automation.Language.Parser]::ParseFile($script:NetworkSetupPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Setup helper contains syntax errors.' }
Write-Host ($script:PassedSetups.ToString() + ' network-setup tests passed; all writes and UAC mocked.')
