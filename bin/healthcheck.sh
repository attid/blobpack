#!/bin/sh
# Freshness probe, deliberately shell-only (no python, no sqlite3 CLI).
#
# Healthy when the database file is non-empty and the marker written by the last
# *fully successful* collection is younger than BACKUP_MAX_AGE (default 24h).
set -eu

db="${BACKUP_DB:-/data/collection.sqlite}"
marker_dir="$(dirname "$db")"
marker="$marker_dir/${BACKUP_HEALTH_FILE:-.last_ok}"
max_raw="${BACKUP_MAX_AGE:-24h}"

if [ ! -s "$db" ]; then
    echo "unhealthy: database missing or empty: $db"
    exit 1
fi

if [ ! -f "$marker" ]; then
    echo "unhealthy: no successful collection marker: $marker"
    exit 1
fi

# 24h -> 86400 seconds, pure shell.
value=$(printf '%s' "$max_raw" | sed -n 's/^[[:space:]]*\([0-9][0-9]*\)[[:space:]]*\([smhdSMHD]\{0,1\}\)[[:space:]]*$/\1/p')
unit=$(printf '%s' "$max_raw" | sed -n 's/^[[:space:]]*[0-9][0-9]*[[:space:]]*\([smhdSMHD]\{0,1\}\)[[:space:]]*$/\1/p')
if [ -z "$value" ]; then
    echo "unhealthy: cannot parse BACKUP_MAX_AGE='$max_raw' (use e.g. 24h)"
    exit 1
fi
case "$(printf '%s' "$unit" | tr 'SMHD' 'smhd')" in
    ""|s) factor=1 ;;
    m) factor=60 ;;
    h) factor=3600 ;;
    d) factor=86400 ;;
    *) echo "unhealthy: unknown BACKUP_MAX_AGE unit in '$max_raw'"; exit 1 ;;
esac
max=$((value * factor))

now=$(date +%s)
stamp=$(stat -c %Y "$marker" 2>/dev/null || echo 0)
if [ "$stamp" -eq 0 ]; then
    echo "unhealthy: cannot read marker timestamp: $marker"
    exit 1
fi
age=$((now - stamp))

if [ "$age" -lt "$max" ]; then
    echo "healthy: data is ${age}s old (limit ${max}s)"
    exit 0
fi

echo "unhealthy: data is ${age}s old (limit ${max}s)"
exit 1
