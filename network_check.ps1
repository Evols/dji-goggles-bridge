# Read-only Windows startup check. Never elevates, installs drivers or changes
# adapter settings. Match the observed Goggles 3 USB identity rather than just
# "Remote NDIS": phones and other USB devices can use that description too.
$ErrorActionPreference = 'Stop'

function Test-GogglesNetwork($Addresses, $Dhcp, $Gateways) {
    return (@($Addresses).Count -eq 1 -and
        $Addresses[0].IPAddress -eq '192.168.60.1' -and
        $Addresses[0].PrefixLength -eq 24 -and
        $Addresses[0].AddressState -eq 'Preferred' -and
        $Dhcp -eq 'Disabled' -and @($Gateways).Count -eq 0)
}

function Confirm-GogglesNetwork {
    try {
        Write-Host 'Checking the goggles USB network adapter...'
        $devices = @(Get-CimInstance Win32_NetworkAdapter | Where-Object {
            $_.PNPDeviceID -like 'USB\VID_2CA3&PID_0020&MI_00\*'
        })
        $connected = @(foreach ($device in $devices) {
            Get-NetAdapter -IncludeHidden | Where-Object {
                $_.ifIndex -eq $device.InterfaceIndex -and $_.Status -eq 'Up'
            }
        })
        if ($connected.Count -ne 1) {
            Write-Host 'Windows does not report exactly one connected Goggles 3 USB network adapter.'
            Write-Host 'Connect one headset with a USB DATA cable; power it on and enable OTG Wired Connection.'
            Write-Host 'Open Device Manager and look for Remote NDIS under Network adapters.'
            Write-Host 'A warning icon or Unknown device can mean the driver is missing or was replaced.'
            Write-Host 'Windows supplies the RNDIS driver. Restore it using Device Manager if needed.'
            Write-Host 'This checker does not install or replace drivers. Different USB identities are not automatically accepted.'
            return 1
        }
        $adapter = $connected[0]
        $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue)
        $dhcp = (Get-NetIPInterface -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4).Dhcp
        $gateways = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue)
        if (Test-GogglesNetwork $addresses $dhcp $gateways) {
            Write-Host ('Ready: ' + $adapter.Name + ' uses 192.168.60.1 / 255.255.255.0, no gateway.')
                return 0
        }
        Write-Host ('Windows detects the goggles USB adapter: ' + $adapter.Name)
        Write-Host 'Its network settings need one-time setup. This is a separate USB network link to the headset.'
        Write-Host 'Set the PC end to 192.168.60.1 with subnet mask 255.255.255.0 and no gateway.'
        Write-Host 'The headset uses 192.168.60.2. These are local USB addresses, not your Internet/Wi-Fi addresses.'
        Write-Host 'Manual setup:'
        Write-Host '  Win+R -> ncpa.cpl -> right-click the goggles adapter shown above -> Properties'
        Write-Host '  -> Internet Protocol Version 4 (TCP/IPv4) -> Properties -> Use the following IP address.'
        Write-Host '  IP: 192.168.60.1; mask: 255.255.255.0; gateway and DNS: leave blank.'
        Write-Host 'Save, then run the launcher again. This checker changes nothing.'
        return 1
    } catch {
        Write-Host ('Network check failed: ' + $_.Exception.Message)
        Write-Host 'See README.md for manual adapter/driver checks. No settings were changed.'
        return 1
    }
}

# Tests can dot-source these functions without running the real hardware check.
if ($MyInvocation.InvocationName -ne '.') { exit (Confirm-GogglesNetwork) }
