# Local development setup. Run once from the repo root after Task 0 pins the Mosquitto tag.
# Creates the .dev folders and the two Mosquitto users (brief and ha) from secrets.env.
# Dev only: passwords are throwaway values and are passed on the command line here.
$ErrorActionPreference = "Stop"

$dirs = ".dev/state", ".dev/archive", ".dev/out", ".dev/mosquitto-data",
        "samples/raw", "samples/sanitized"
foreach ($d in $dirs) { New-Item -ItemType Directory -Force -Path $d | Out-Null }

if (-not (Test-Path "secrets.env")) { throw "Copy secrets.env.example to secrets.env first." }

$secrets = @{}
Get-Content "secrets.env" | Where-Object { $_ -match '^\s*[^#\s][^=]*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    $secrets[$k.Trim()] = $v.Trim()
}
foreach ($key in "MQTT_USER", "MQTT_PASSWORD", "MQTT_HA_PASSWORD") {
    if (-not $secrets[$key]) { throw "secrets.env is missing $key." }
}

$match = Select-String -Path "docker-compose.yml" -Pattern 'image:\s*(eclipse-mosquitto:\S+)'
if (-not $match) { throw "No eclipse-mosquitto image found in docker-compose.yml." }
$image = $match.Matches[0].Groups[1].Value
if ($image -match '<pin>') { throw "Pin the Mosquitto image tag in docker-compose.yml first (Task 0)." }

$conf = (Resolve-Path "mosquitto").Path
# No Windows docker command? Use the engine inside WSL, with the mount path translated.
$useWsl = -not (Get-Command docker -ErrorAction SilentlyContinue)
if ($useWsl) { $conf = (wsl wslpath -a ($conf -replace '\\', '/')).Trim() }
function Invoke-Docker { if ($useWsl) { wsl docker @args } else { docker @args } }

Invoke-Docker run --rm -v "${conf}:/mosquitto/config" $image `
    mosquitto_passwd -b -c /mosquitto/config/passwd $secrets["MQTT_USER"] $secrets["MQTT_PASSWORD"]
Invoke-Docker run --rm -v "${conf}:/mosquitto/config" $image `
    mosquitto_passwd -b /mosquitto/config/passwd ha $secrets["MQTT_HA_PASSWORD"]

Write-Host "Dev folders and Mosquitto users (brief, ha) created. mosquitto/passwd is gitignored."
