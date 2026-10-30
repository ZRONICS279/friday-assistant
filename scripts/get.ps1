# One-line install for Windows 11 (22H2 or newer). In PowerShell:
#
#   irm https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.ps1 | iex
#
# Friday runs inside WSL, Windows' built-in Linux. This script:
#   1. installs WSL with Ubuntu (first time only; may need a restart),
#   2. turns on "mirrored" networking so phones and TVs on your Wi-Fi can
#      reach Friday, and opens port 8787 in the WSL firewall,
#   3. runs Friday's normal installer inside Ubuntu,
#   4. optionally starts Friday whenever you sign in to Windows.
# Your NVIDIA graphics card works inside WSL with the normal Windows driver.

$ErrorActionPreference = "Stop"
$Distro = "Ubuntu"
$GetSh = "https://raw.githubusercontent.com/knightlie-code/friday/main/scripts/get.sh"

function Say($text) { Write-Host "`n$text" -ForegroundColor Cyan }

# Administrator rights are needed for WSL and the firewall rule.
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Say "Friday's installer needs administrator rights. Approve the prompt to continue."
    $self = if ($PSCommandPath) { "-File `"$PSCommandPath`"" } else { "-Command `"irm $($GetSh -replace 'get.sh$','get.ps1') | iex`"" }
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit -ExecutionPolicy Bypass $self"
    return
}

# 1. WSL + Ubuntu --------------------------------------------------------------
$installed = (wsl.exe -l -q 2>$null) -replace "`0", "" | Where-Object { $_.Trim() -eq $Distro }
if (-not $installed) {
    Say "Installing WSL and Ubuntu. When Ubuntu opens, pick a username and password."
    wsl.exe --install -d $Distro
    Say "If Windows asks you to restart, restart, then run the same install command again."
    Say "Otherwise, once Ubuntu has finished setting up your user, run the install command again."
    return
}

# 2. Networking --------------------------------------------------------------
$wslconfig = Join-Path $env:USERPROFILE ".wslconfig"
$text = if (Test-Path $wslconfig) { Get-Content $wslconfig -Raw } else { "" }
if ($text -notmatch "(?m)^\s*networkingMode\s*=\s*mirrored") {
    Say "Turning on mirrored networking for WSL (so your phone can reach Friday)."
    if ($text -match "(?m)^\[wsl2\]") {
        $text = $text -replace "(?m)^\[wsl2\]\s*$", "[wsl2]`r`nnetworkingMode=mirrored"
    } else {
        $text = $text.TrimEnd() + "`r`n[wsl2]`r`nnetworkingMode=mirrored`r`n"
    }
    Set-Content -Path $wslconfig -Value $text -Encoding ASCII
    wsl.exe --shutdown
}
try {
    if (-not (Get-NetFirewallHyperVRule -Name "Friday" -ErrorAction SilentlyContinue)) {
        New-NetFirewallHyperVRule -Name "Friday" -DisplayName "Friday home assistant" -Direction Inbound `
            -VMCreatorId "{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}" -Protocol TCP -LocalPorts 8787 | Out-Null
    }
} catch {
    Write-Warning "Couldn't add the WSL firewall rule (needs Windows 11 22H2+). Phones may not reach Friday."
}

# systemd inside Ubuntu, for autostart and Docker.
wsl.exe -d $Distro -u root -- sh -c "grep -q '^systemd=true' /etc/wsl.conf 2>/dev/null"
if ($LASTEXITCODE -ne 0) {
    wsl.exe -d $Distro -u root -- sh -c "printf '[boot]\nsystemd=true\n' >> /etc/wsl.conf"
    wsl.exe --shutdown
}

# 3. Friday's installer inside Ubuntu -----------------------------------------
Say "Installing Friday inside Ubuntu. It will ask a few questions."
wsl.exe -d $Distro -- bash -lc "curl -fsSL $GetSh | bash"

# 4. Start with Windows --------------------------------------------------------
$answer = Read-Host "`nStart Friday automatically when you sign in to Windows? [Y/n]"
if ($answer -eq "" -or $answer -match "^[Yy]") {
    # Keeping one WSL process alive keeps Ubuntu (and Friday's service in it) running.
    # Launched through a hidden PowerShell so no console window stays open.
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -Command wsl.exe -d $Distro --exec sleep infinity"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName "Friday" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName "Friday"
    Say "Friday will start whenever you sign in. To stop that: Unregister-ScheduledTask -TaskName Friday"
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred |
    Where-Object { $_.InterfaceAlias -notmatch "Loopback|vEthernet" -and $_.PrefixOrigin -ne "WellKnown" } |
    Select-Object -First 1).IPAddress
Say "Done! Open https://${ip}:8787 on any phone or computer on your Wi-Fi."
