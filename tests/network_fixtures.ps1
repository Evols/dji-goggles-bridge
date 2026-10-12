# All hardware reads are synthetic. No live adapters are queried by these tests.
function Get-CimInstance {
    param($ClassName)
    switch ($ClassName) {
        'Win32_PnPEntity' { return $script:PnpDevices }
        'Win32_NetworkAdapter' { return $script:Devices }
        default { throw ('Unexpected CIM read: ' + $ClassName) }
    }
}
function Get-NetAdapter { param([switch]$IncludeHidden) return $script:Adapters }
function Get-NetIPAddress { param($InterfaceIndex, $AddressFamily, $ErrorAction, $PolicyStore) return $script:Addresses }
function Get-NetIPInterface { param($InterfaceIndex, $AddressFamily) return [pscustomobject]@{ Dhcp = $script:Dhcp } }
function Get-NetRoute { param($InterfaceIndex, $AddressFamily, $DestinationPrefix, $ErrorAction, $PolicyStore) return $script:Gateways }

function Reset-Fixture {
    $script:PnpDevices = @([pscustomobject]@{
        PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\TEST'
        Present = $true
        ConfigManagerErrorCode = 0
        Service = 'usbrndis6'
        HardwareID = @('USB\VID_2CA3&PID_0020&MI_00')
        CompatibleID = @('USB\Class_E0&SubClass_01&Prot_03')
    })
    $script:Devices = @([pscustomobject]@{ PNPDeviceID = $script:PnpDevices[0].PNPDeviceID; InterfaceIndex = 11 })
    $script:Adapters = @([pscustomobject]@{ ifIndex = 11; Name = 'Test goggles'; Status = 'Up' },
                        [pscustomobject]@{ ifIndex = 7; Name = 'Internet WiFi'; Status = 'Up' })
    $script:Addresses = @([pscustomobject]@{ IPAddress = '192.168.60.1'; PrefixLength = 24; AddressState = 'Preferred' })
    $script:Dhcp = 'Disabled'
    $script:Gateways = @()
}
