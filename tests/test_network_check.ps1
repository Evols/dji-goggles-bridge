# Synthetic Windows network-check tests. These mock all hardware reads and
# never touch actual adapters. Run: powershell -File tests/test_network_check.ps1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../src/apps/goggles_resources/network_check.ps1')

function Get-CimInstance { param($ClassName) return $script:Devices }
function Get-NetAdapter { param([switch]$IncludeHidden) return $script:Adapters }
function Get-NetIPAddress { param($InterfaceIndex, $AddressFamily, $ErrorAction) return $script:Addresses }
function Get-NetIPInterface { param($InterfaceIndex, $AddressFamily) return [pscustomobject]@{ Dhcp = $script:Dhcp } }
function Get-NetRoute { param($InterfaceIndex, $DestinationPrefix, $ErrorAction) return $script:Gateways }

function Reset-Fixture {
    $script:Devices = @([pscustomobject]@{ PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\TEST'; InterfaceIndex = 11 })
    $script:Adapters = @([pscustomobject]@{ ifIndex = 11; Name = 'Test goggles'; Status = 'Up' })
    $script:Addresses = @([pscustomobject]@{ IPAddress = '192.168.60.1'; PrefixLength = 24; AddressState = 'Preferred' })
    $script:Dhcp = 'Disabled'
    $script:Gateways = @()
}

function Assert-Result([string]$Name, [int]$Expected) {
    $actual = Confirm-GogglesNetwork
    if ($actual -ne $Expected) { throw "$Name returned $actual, expected $Expected" }
    Write-Host ('PASS: ' + $Name)
}

Reset-Fixture
Assert-Result 'Already configured' 0
Reset-Fixture
$script:Addresses[0].IPAddress = '169.254.1.2'
Assert-Result 'Automatic fallback IP requires manual setup' 1
Reset-Fixture
$script:Addresses[0].PrefixLength = 16
Assert-Result 'Wrong subnet mask' 1
Reset-Fixture
$script:Dhcp = 'Enabled'
Assert-Result 'DHCP address is not a persistent static setup' 1
Reset-Fixture
$script:Gateways = @([pscustomobject]@{ NextHop = '192.168.60.2' })
Assert-Result 'Unexpected gateway' 1
Reset-Fixture
$script:Devices = @()
Assert-Result 'Missing adapter or driver' 1
Reset-Fixture
$script:Devices[0].PNPDeviceID = 'USB\VID_1234&PID_5678\PHONE'
Assert-Result 'Unrelated phone RNDIS adapter rejected' 1
Reset-Fixture
$script:Adapters[0].Status = 'Disconnected'
Assert-Result 'Disconnected headset rejected' 1
Reset-Fixture
$script:Devices += [pscustomobject]@{ PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\SECOND'; InterfaceIndex = 12 }
$script:Adapters += [pscustomobject]@{ ifIndex = 12; Name = 'Second goggles'; Status = 'Up' }
Assert-Result 'Ambiguous multiple headsets rejected' 1

# Guard the helper's read-only contract: it must never gain elevation,
# driver-installation or adapter-changing commands without an explicit redesign.
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '../src/apps/goggles_resources/network_check.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'PowerShell helper contains syntax errors' }
$banned = @('Start-Process', 'Set-NetIPInterface', 'New-NetIPAddress', 'Remove-NetIPAddress',
            'Remove-NetRoute', 'Set-NetRoute', 'Set-DnsClientServerAddress', 'pnputil', 'netsh')
$commands = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)
foreach ($command in $commands) {
    if ($banned -contains $command.GetCommandName()) { throw 'Network checker is no longer read-only' }
}
Write-Host 'PASS: no elevation or network-mutation commands'
Write-Host '10 network-check tests passed'
