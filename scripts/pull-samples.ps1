# Copy the last N lines of each syslog file into the instance's samples\raw (never committed).
# Read-only against the share: it never writes, moves, or deletes anything there.
#
# The share path is yours, so it is not stored in this repository. Pass it, or set BRIEF_SYSLOG_SHARE:
#   .\scripts\pull-samples.ps1 -Share \\your-server\your-share\syslog
#   .\scripts\pull-samples.ps1 -Share \\your-server\your-share\syslog -Lines 1000
# The instance folder is BRIEF_INSTANCE_DIR, else a sibling folder named homelab-brief-instance,
# else .dev\instance in this repository (see docs\INSTANCE.md).
param(
    [string]$Share = $env:BRIEF_SYSLOG_SHARE,
    [int]$Lines = 200,
    [string]$Out = ""
)
$ErrorActionPreference = "Stop"

if (-not $Share) { throw "Pass -Share <path to the syslog folder> or set BRIEF_SYSLOG_SHARE." }

if (-not $Out) {
    $repo = Split-Path -Parent $PSScriptRoot
    $instance = $env:BRIEF_INSTANCE_DIR
    if (-not $instance) {
        $sibling = Join-Path (Split-Path -Parent $repo) "homelab-brief-instance"
        $instance = if (Test-Path $sibling) { $sibling } else { Join-Path $repo ".dev\instance" }
    }
    $Out = Join-Path $instance "samples\raw"
}
New-Item -ItemType Directory -Force -Path $Out | Out-Null
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

Get-ChildItem -Path $Share -Filter "syslog-*.log" -File | ForEach-Object {
    # -Tail reads from the end of the file, so even multi-GB files are quick.
    $tail = Get-Content -Path $_.FullName -Tail $Lines -Encoding UTF8
    $dest = Join-Path $Out $_.Name
    [System.IO.File]::WriteAllLines($dest, [string[]]$tail, $utf8NoBom)
    Write-Host ("{0,-28} {1,5} lines  (source last modified {2})" -f $_.Name, $tail.Count, $_.LastWriteTime)
}
Write-Host "Done. $Out is outside the repository; run the sanitizer before using anything from it."
