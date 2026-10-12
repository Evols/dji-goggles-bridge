# Read-only startup inspection. Setup/elevation lives in network_setup.ps1.
# Exit codes: 0 ready; 1 no usable unique headset; 2 IP/disabled adapter;
# 3 missing/wrong RNDIS driver; 4 inspection error.
$ErrorActionPreference = 'Stop'

function Get-GogglesDevices {
    Get-CimInstance Win32_PnPEntity | Where-Object {
        $_.Present -and $_.PNPDeviceID -like 'USB\VID_2CA3&PID_0020&MI_00\*'
    }
}

function Get-GogglesAdapters($Device) {
    $networkDevices = @(Get-CimInstance Win32_NetworkAdapter | Where-Object {
        $_.PNPDeviceID -eq $Device.PNPDeviceID
    })
    Get-NetAdapter -IncludeHidden | Where-Object {
        $networkDevices.InterfaceIndex -contains $_.ifIndex
    }
}

function Test-GogglesNetwork($Addresses, $Dhcp, $Gateways) {
    return (@($Addresses).Count -eq 1 -and
        $Addresses[0].IPAddress -eq '192.168.60.1' -and
        $Addresses[0].PrefixLength -eq 24 -and
        $Addresses[0].AddressState -eq 'Preferred' -and
        $Dhcp -eq 'Disabled' -and @($Gateways).Count -eq 0)
}

function Confirm-GogglesNetwork {
    try {
        Write-Host 'Checking the goggles RNDIS driver and USB network adapter...'
        $devices = @(Get-GogglesDevices)
        if ($devices.Count -ne 1) {
            if ($devices.Count -eq 0) { Write-Host '[WAIT] USB: no goggles RNDIS interface detected.' }
            else { Write-Host '[WARN] USB: multiple goggles detected; connect only one.' }
            Write-Host 'Connect exactly one Goggles 3 with a USB DATA cable and enable OTG Wired Connection.'
            Write-Host 'No unique supported headset is present; setup will not change anything.'
            return 1
        }
        $device = $devices[0]
        Write-Host '[OK] USB: plugged in (Goggles 3 detected).'
        if ($device.ConfigManagerErrorCode -ne 0 -or $device.Service -notmatch 'rndis') {
            Write-Host 'The goggles are detected, but their Windows RNDIS driver is not ready.'
            Write-Host 'Setup can enable the device or restore the built-in RNDIS driver.'
            return 3
        }
        $adapters = @(Get-GogglesAdapters $device)
        if ($adapters.Count -ne 1) {
            Write-Host 'The detected goggles do not have exactly one RNDIS network adapter.'
            return 3
        }
        $adapter = $adapters[0]
        if ($adapter.Status -eq 'Disabled') {
            Write-Host 'The goggles USB network adapter is disabled.'
            return 2
        }
        if ($adapter.Status -ne 'Up') {
            Write-Host 'The goggles USB network link is disconnected. Check power, the data cable and OTG.'
            return 1
        }
        $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
        $dhcp = (Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4).Dhcp
        $gateways = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
        if (Test-GogglesNetwork $addresses $dhcp $gateways) {
            Write-Host ('Ready: ' + $adapter.Name + ' uses 192.168.60.1 / 255.255.255.0, no gateway.')
            return 0
        }
        Write-Host ('Windows detects the goggles USB adapter: ' + $adapter.Name)
        Write-Host 'Its IPv4 settings need setup: 192.168.60.1 / 255.255.255.0, no gateway.'
        Write-Host 'Manual setup: Win+R -> ncpa.cpl -> goggles adapter -> Properties -> IPv4.'
        return 2
    } catch {
        Write-Host ('Network check failed: ' + $_.Exception.Message)
        Write-Host 'See README.md for manual adapter/driver checks. No settings were changed.'
        return 4
    }
}

if ($MyInvocation.InvocationName -ne '.') { exit (Confirm-GogglesNetwork) }
