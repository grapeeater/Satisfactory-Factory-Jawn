# P1: keep FRM (TCP 8080) reachable only from this PC.
#
# FRM 1.5.6 has no bind-address setting; its uWebSockets server listens on all interfaces.
# This adds an inbound block rule for TCP 8080 on every profile. Windows Firewall does not
# filter loopback, so the harness on 127.0.0.1 still works. A block rule overrides any
# "Allow" you might click in the Windows prompt when the game first opens the port.
#
# Run from an elevated (Administrator) PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\p1_firewall.ps1           # add rule
#   powershell -ExecutionPolicy Bypass -File scripts\p1_firewall.ps1 -Remove   # remove rule
#   powershell -ExecutionPolicy Bypass -File scripts\p1_firewall.ps1 -Check    # show rule + listener
param([switch]$Remove, [switch]$Check, [int]$Port = 8080)
$Name = "Jawn - block FRM $Port inbound"

if ($Check) {
    netsh advfirewall firewall show rule name="$Name"
    "Listeners on ${Port}:"
    netstat -ano | Select-String ":$Port\s"
    return
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw "Run this from an Administrator PowerShell." }

if ($Remove) {
    netsh advfirewall firewall delete rule name="$Name"
    return
}

$existing = netsh advfirewall firewall show rule name="$Name" 2>$null | Select-String "Rule Name"
if ($existing) { "Rule already present."; return }
netsh advfirewall firewall add rule name="$Name" dir=in action=block protocol=TCP localport=$Port profile=any
netsh advfirewall firewall show rule name="$Name"
