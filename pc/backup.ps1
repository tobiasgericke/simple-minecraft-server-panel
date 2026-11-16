# Fetches a fresh backup from the Minecraft server over SSH and keeps the newest N.
#
#   powershell -ExecutionPolicy Bypass -File backup.ps1 -Server myserver -Keep 10 -Dir D:\MC-Backups
#
# -Server: an SSH host (alias from ~/.ssh/config or user@host) that may run `sudo mc-backup`
#          without a password prompt (key login, NOPASSWD sudo or root).
# Writes a log to backup.log in -Dir. Schedule it with Windows Task Scheduler (see README).
param(
    [Parameter(Mandatory)] [string] $Server,
    [int] $Keep = 10,
    [string] $Dir
)
# Not as a param default: Windows PowerShell 5.1 leaves $PSScriptRoot empty there
if (-not $Dir) { $Dir = $PSScriptRoot }

$Name = "mc-" + (Get-Date -Format "yyyy-MM-dd_HHmm") + ".tar.zst"
$Out  = Join-Path $Dir $Name
$Part = "$Out.part"
New-Item -ItemType Directory -Force $Dir | Out-Null

function Log($msg, $color = "Gray") {
    Write-Host $msg -ForegroundColor $color
    Add-Content (Join-Path $Dir "backup.log") ("{0:yyyy-MM-dd HH:mm}  {1}" -f (Get-Date), $msg) -Encoding UTF8
}

Log "Fetching backup from $Server ... this takes a few minutes." Cyan
$sw = [Diagnostics.Stopwatch]::StartNew()
# cmd instead of a PowerShell redirect: Windows PowerShell 5.1 would corrupt binary data
cmd /c "ssh -o BatchMode=yes -o ConnectTimeout=20 $Server sudo -n mc-backup pc > `"$Part`""
if ($LASTEXITCODE -ne 0) {
    Remove-Item $Part -ErrorAction SilentlyContinue
    Log "ERROR: backup failed (exit code $LASTEXITCODE)." Red
    exit 1
}

# Compare checksums: did the file arrive complete and unchanged?
$expected = (ssh -o BatchMode=yes $Server "cat /run/mc-backup.sha256").Trim()
$actual   = (Get-FileHash $Part -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) {
    Remove-Item $Part
    Log "ERROR: checksum mismatch, the download was incomplete." Red
    exit 1
}
Rename-Item $Part $Name

$sizeGB = "{0:N2}" -f ((Get-Item $Out).Length / 1GB)
Log "OK: $Name ($sizeGB GB, $([int]$sw.Elapsed.TotalMinutes) min, checksum verified)" Green

Get-ChildItem $Dir -Filter "mc-*.tar.zst" | Sort-Object Name -Descending |
    Select-Object -Skip $Keep | ForEach-Object {
        Log "Deleting old backup: $($_.Name)"
        Remove-Item $_.FullName
    }
