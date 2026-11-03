# Fix the Windows-side ADB interface binding for an Android phone.
#
# WHY (2026-10-20, three hours of debugging, see FACTS.md 101):
#   After the phone re-enumerated with a new USB function set, its ADB
#   interface node was bound to the generic winusb.inf / WINUSB service
#   with ProblemCode 0 -- which looks perfectly healthy -- but the node
#   never registered a WinUSB device interface GUID. Two independent
#   enumerators (adb.exe's own backend and our own SetupDi probe) both
#   reported zero devices. PnP Status=OK is a STALE node and proves
#   nothing; the registry value below is the only reliable signal.
#
# WHAT IT DOES:
#   1. dumps current state (instance id / inf / service / problem / GUIDs)
#   2. pnputil /remove-device   (unbind the stale node)
#   3. pnputil /scan-devices    (let Windows reinstall it)
#   4. if the GUID is still missing, writes DeviceInterfaceGUIDs and
#      restarts the node
#
# RUN (Administrator PowerShell -- needed for pnputil and for HKLM writes):
#   powershell -ExecutionPolicy Bypass -File tools\fix_adb_winusb.ps1
#
# Dry run (read-only, works WITHOUT admin -- use it to check the diagnosis
# before asking anyone to elevate):
#   powershell -ExecutionPolicy Bypass -File tools\fix_adb_winusb.ps1 -DryRun
#
# PARAMETERS:
#   -Vid   <hex>   vendor id to look for (default 339B = HONOR)
#   -DryRun        only report, never modify
#   -Log   <path>  log file (default tmp\_fix_adb_winusb.log)
#
# AFTER IT SUCCEEDS, verify with BOTH paths (do not trust one):
#   D:\platform-tools\adb.exe devices -l      -> USB serial channel appears
#   python tmp\_adbd_handshake.py --probe     -> "cannot open (share=3)" is
#                                                NORMAL: it means adb grabbed
#                                                the interface exclusively.

param(
    [string]$Vid = '339B',
    [switch]$DryRun,
    [string]$Log = ''
)

$ErrorActionPreference = 'Continue'

if (-not $Log) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $Log = Join-Path (Split-Path -Parent $here) 'tmp\_fix_adb_winusb.log'
}
$logDir = Split-Path -Parent $Log
if ($logDir -and -not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
Remove-Item $Log -ErrorAction SilentlyContinue

function W($m) {
    $line = [string]$m
    Add-Content -Path $Log -Value $line -Encoding UTF8
    Write-Host $line
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

W ('=== start ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
W ('IsAdmin=' + $isAdmin + '  DryRun=' + [bool]$DryRun + '  Vid=' + $Vid + '  Log=' + $Log)

if ($DryRun) {
    W 'DRY RUN: read-only. No pnputil, no registry writes.'
}
elseif (-not $isAdmin) {
    W 'ERROR: not elevated. Right click Start -> Terminal (Admin), then re-run.'
    W '       (Use -DryRun first if you only want the diagnosis.)'
    exit 1
}

function FindAdb {
    Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -like ('*VID_' + $Vid + '*') -and $_.FriendlyName -like '*ADB*' } |
        Select-Object -First 1
}

function GetGuid($instanceId) {
    if (-not $instanceId) { return $null }
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Enum\' + $instanceId + '\Device Parameters'
    return (Get-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -ErrorAction SilentlyContinue).DeviceInterfaceGUIDs
}

# Defensive normalisation only: REG_MULTI_SZ is supposed to be clean, but a
# stray control character in it would silently corrupt one log line and make
# grep-based diagnosis unreliable. Trimming costs nothing. (No evidence that
# this specific value carries one -- do not cite this as a finding.)
function FmtGuid($g) {
    if (-not $g) { return '<none>' }
    return ((@($g) | ForEach-Object { ([string]$_).Trim() }) -join ' , ')
}

function Dump($tag, $dev) {
    if (-not $dev) { W ($tag + ' : NOT FOUND'); return }
    $inf = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_DriverInfPath' -ErrorAction SilentlyContinue).Data
    $svc = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_Service' -ErrorAction SilentlyContinue).Data
    $prb = (Get-PnpDeviceProperty -InstanceId $dev.InstanceId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction SilentlyContinue).Data
    W ($tag + ' : ' + $dev.InstanceId + ' | status=' + $dev.Status + ' | inf=' + $inf + ' | svc=' + $svc + ' | problem=' + $prb)
    $g = GetGuid $dev.InstanceId
    if ($g) { W ($tag + ' : DeviceInterfaceGUIDs = ' + (FmtGuid $g)) }
    else { W ($tag + ' : DeviceInterfaceGUIDs = <none>  <-- this is the bug') }
}

$dev = FindAdb
Dump 'BEFORE' $dev

if (-not $dev) {
    W ('abort: no ADB interface node matching VID_' + $Vid + ' -- check cable / USB debugging switch first.')
    exit 2
}

if ($DryRun) {
    $g = GetGuid $dev.InstanceId
    W ''
    if ($g) {
        W 'DRY RUN verdict: GUID already registered. This script is NOT your problem.'
        W '                 Look elsewhere: cable, port, adb version, phone-side function set.'
        W '                 Phone side check: adb shell dumpsys usb | grep -A4 current_functions='
        exit 0
    }
    W 'DRY RUN verdict: GUID MISSING -> this script would fix it. Re-run as Administrator.'
    W '                 Also confirm the phone side first:'
    W '                   adb shell dumpsys usb | grep -A4 current_functions=   (must list ADB)'
    exit 3
}

$oldId = $dev.InstanceId

W ('remove-device ' + $oldId)
$out = & pnputil /remove-device $oldId 2>&1
W ($out -join ' | ')
Start-Sleep -Seconds 2

W 'scan-devices'
$out = & pnputil /scan-devices 2>&1
W ($out -join ' | ')
Start-Sleep -Seconds 6

$dev2 = FindAdb
Dump 'AFTER-RESCAN' $dev2

if (-not $dev2) {
    W 'abort: node did not come back after rescan. Unplug and replug the cable, then re-run.'
    exit 4
}

if (GetGuid $dev2.InstanceId) {
    W 'GUID present after rescan -- no registry write needed.'
}
else {
    W 'DeviceInterfaceGUIDs missing -> writing WinUSB interface GUID'
    $key = 'HKLM:\SYSTEM\CurrentControlSet\Enum\' + $dev2.InstanceId + '\Device Parameters'
    New-ItemProperty -Path $key -Name 'DeviceInterfaceGUIDs' -PropertyType MultiString `
        -Value '{dee824ef-729b-4a0e-9c14-b7117d33a817}' -Force | Out-Null
    W ('restart-device ' + $dev2.InstanceId)
    $out = & pnputil /restart-device $dev2.InstanceId 2>&1
    W ($out -join ' | ')
    Start-Sleep -Seconds 5
    Dump 'AFTER-GUID' (FindAdb)
}

# Final check: does the path adb actually uses now see it? Do not trust the
# registry alone -- the whole point of this script is that registry-looking-
# healthy was not enough before.
$adb = 'D:\platform-tools\adb.exe'
if (Test-Path $adb) {
    W 'final: adb devices -l'
    $out = & $adb devices -l 2>&1
    W ($out -join ' | ')
}

W ('=== done ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
