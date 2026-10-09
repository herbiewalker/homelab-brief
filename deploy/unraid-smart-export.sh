#!/bin/bash
# Homelab Brief: write one smartctl JSON file per disk, for the Unraid collector's smartctl_files mode.
# Install as a User Script on each Unraid server (Settings, User Scripts), schedule it hourly.
# It only reads: smartctl -n standby never wakes a sleeping disk (that disk keeps its previous file).
#
# Set the two values below. DEST is a folder on a share that is mounted read-only into the
# homelab-brief container at the collector's smart_files_path; SERVER_ID is this server's id in
# collectors.yaml (each server writes into its own folder named after it).
DEST="/mnt/user/<share>/smart"
SERVER_ID="server-1"

folder="$DEST/$SERVER_ID"
mkdir -p "$folder" || exit 1
for device in $(smartctl --scan | awk '{print $1}'); do
  report=$(smartctl --json=o -a -n standby "$device" 2>/dev/null)
  serial=$(printf '%s' "$report" | sed -n 's/.*"serial_number": *"\([^"]*\)".*/\1/p' | head -n 1)
  serial=${serial//[^A-Za-z0-9_.-]/_}
  [ -n "$serial" ] || continue            # standby, or not a SMART device
  printf '%s\n' "$report" > "$folder/$serial.json.tmp" && mv "$folder/$serial.json.tmp" "$folder/$serial.json"
done
