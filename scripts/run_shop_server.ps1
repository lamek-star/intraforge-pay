# Start the IntraForge Pay pilot shop server over HTTPS (the Android wallet app needs HTTPS).
#
#   .\scripts\run_shop_server.ps1          this PC only (and the Android emulator, as https://10.0.2.2:8643)
#   .\scripts\run_shop_server.ps1 -Lan     also phones on the shop's network, as https://<this PC's address>:8643
#
# -Lan needs Windows Firewall to admit port 8643 on the PRIVATE network profile only. Run this once yourself in an
# administrator PowerShell (it is not done automatically):
#   New-NetFirewallRule -DisplayName "IntraForge Pay pilot (8643)" -Direction Inbound -Protocol TCP -LocalPort 8643 -Profile Private -Action Allow
# and make sure the shop Wi-Fi is set to "Private" in Windows. Remove the rule after the pilot:
#   Remove-NetFirewallRule -DisplayName "IntraForge Pay pilot (8643)"
param([switch]$Lan, [int]$Port = 8643)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not (Test-Path "secrets\tls\server.crt")) { & .\.venv\Scripts\python scripts\make_tls.py }
$env:IFPAY_COOKIE_SECURE = "true"
$bind = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }
$env:IFPAY_PUBLIC_PORT = "$Port"
if ($Lan) {
    # The shop network = the adapter(s) with a default gateway (Wi-Fi/Ethernet), not WSL/VirtualBox/Hyper-V adapters.
    $shopIps = @(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
        ForEach-Object { $_.IPv4Address.IPAddress })
    $env:IFPAY_PUBLIC_HOSTS = ($shopIps -join ",")
    Write-Host "Shop network address(es) in the customer-app connect QR: $($shopIps -join ', ')"
    Write-Host "If an address is missing from the certificate, run: .\.venv\Scripts\python scripts\make_tls.py --ip <address>"
}
& .\.venv\Scripts\python -m uvicorn ifpay.web.app:app --host $bind --port $Port `
    --ssl-keyfile secrets\tls\server.key --ssl-certfile secrets\tls\server.crt
