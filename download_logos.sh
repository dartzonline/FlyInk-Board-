#!/usr/bin/env bash
#
# download_logos.sh -- run ONCE to populate ~/logos with airline logos.
#
# Pulls Jxck-S/airline-logos (logos named by ICAO code, e.g. DAL.png, UAL.png)
# and copies the square logo sets into ~/logos so flight_display.py can use them.
#
#   chmod +x download_logos.sh
#   ./download_logos.sh
#
set -e

DEST="$HOME/logos"
TMP="$(mktemp -d)"
REPO="https://github.com/Jxck-S/airline-logos"

# Source folders, in priority order (first match wins for a given ICAO code).
SOURCES="custom_logos radarbox_logos flightaware_logos"

command -v git >/dev/null 2>&1 || { echo "git not found -- run: sudo apt install -y git"; exit 1; }

mkdir -p "$DEST"
echo "Cloning logo repository (shallow)..."
git clone --depth 1 "$REPO" "$TMP/repo"

copied=0
for src in $SOURCES; do
    dir="$TMP/repo/$src"
    [ -d "$dir" ] || continue
    for f in "$dir"/*.png; do
        [ -e "$f" ] || continue
        code="$(basename "$f" .png | tr '[:lower:]' '[:upper:]')"
        if [ ! -f "$DEST/$code.png" ]; then
            cp "$f" "$DEST/$code.png"
            copied=$((copied + 1))
        fi
    done
done

rm -rf "$TMP"
echo "Done. Copied $copied logos into $DEST"
echo "Files are named by ICAO code, e.g. $DEST/DAL.png"
