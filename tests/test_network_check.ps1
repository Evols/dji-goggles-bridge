# Synthetic Windows network-check tests. These mock all hardware reads and
# never touch actual adapters. Run: powershell -File tests/test_network_check.ps1
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../src/apps/goggles_resources/network_check.ps1')

. (Join-Path $PSScriptRoot 'network_fixtures.ps1')
$script:PassedChecks = 0

function Assert-Result([string]$Name, [int]$Expected) {
    $actual = Confirm-GogglesNetwork
    if ($actual -ne $Expected) { throw "$Name returned $actual, expected $Expected" }
    Write-Host ('PASS: ' + $Name)
    $script:PassedChecks++
}

Reset-Fixture
Assert-Result 'Already configured' 0
Reset-Fixture
$script:Addresses[0].IPAddress = '169.254.1.2'
Assert-Result 'Automatic fallback IP offers setup' 2
Reset-Fixture
$script:Addresses[0].PrefixLength = 16
Assert-Result 'Wrong subnet mask' 2
Reset-Fixture
$script:Dhcp = 'Enabled'
Assert-Result 'DHCP address is not a persistent static setup' 2
Reset-Fixture
$script:Gateways = @([pscustomobject]@{ NextHop = '192.168.60.2' })
Assert-Result 'Unexpected gateway' 2
Reset-Fixture
$script:PnpDevices = @()
Assert-Result 'Missing headset never offers elevation' 1
Reset-Fixture
$script:PnpDevices[0].PNPDeviceID = 'USB\VID_1234&PID_5678\PHONE'
Assert-Result 'Unrelated phone RNDIS adapter rejected' 1
Reset-Fixture
$script:Adapters[0].Status = 'Disconnected'
Assert-Result 'Disconnected headset rejected' 1
Reset-Fixture
$script:Devices += [pscustomobject]@{ PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\SECOND'; InterfaceIndex = 12 }
$script:Adapters += [pscustomobject]@{ ifIndex = 12; Name = 'Second goggles'; Status = 'Up' }
$script:PnpDevices += [pscustomobject]@{ PNPDeviceID = 'USB\VID_2CA3&PID_0020&MI_00\SECOND'; Present = $true }
Assert-Result 'Ambiguous multiple headsets rejected' 1
Reset-Fixture
$script:PnpDevices[0].ConfigManagerErrorCode = 28
Assert-Result 'Detected headset with missing driver offers setup' 3
Reset-Fixture
$script:PnpDevices[0].Service = 'WinUSB'
Assert-Result 'Wrong driver offers RNDIS restoration' 3
Reset-Fixture
$script:Adapters[0].Status = 'Disabled'
Assert-Result 'Disabled goggles adapter offers setup' 2
Reset-Fixture
$script:PnpDevices[0].Present = $false
Assert-Result 'Unplugged device retained in Windows inventory is ignored' 1
Reset-Fixture
$script:Devices = @()
Assert-Result 'RNDIS device without network adapter offers repair' 3

# Guard the helper's read-only contract: it must never gain elevation,
# driver-installation or adapter-changing commands without an explicit redesign.
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot '../src/apps/goggles_resources/network_check.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'PowerShell helper contains syntax errors' }
$banned = @('Start-Process', 'Enable-PnpDevice', 'Enable-NetAdapter', 'Set-NetIPInterface', 'New-NetIPAddress', 'Remove-NetIPAddress',
            'Remove-NetRoute', 'Set-NetRoute', 'Set-DnsClientServerAddress', 'pnputil', 'netsh')
$commands = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)
foreach ($command in $commands) {
    if ($banned -contains $command.GetCommandName()) { throw 'Network checker is no longer read-only' }
}
Write-Host 'PASS: no elevation or network-mutation commands'
Write-Host (($script:PassedChecks + 1).ToString() + ' network-check tests passed')
