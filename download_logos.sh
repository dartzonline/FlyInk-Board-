#!/usr/bin/env bash
#
# download_logos.sh -- populate (or refresh) ~/logos with airline logos.
#
# Pulls Jxck-S/airline-logos (logos named by ICAO code, e.g. DAL.png, UAL.png)
# and copies the square logo sets into ~/logos so the display can use them.
# Safe to re-run: pass --refresh to overwrite existing files with whatever
# the upstream repo currently has, since airline-logos gets periodic art
# updates for codes that were already covered (e.g. its "July 2026 Update").
# Without --refresh only codes we don't already have are added, same as the
# original one-shot behaviour.
#
#   chmod +x download_logos.sh
#   ./download_logos.sh            # add anything new, leave existing files alone
#   ./download_logos.sh --refresh  # also overwrite existing files with the latest art
#
set -e

REFRESH=0
[ "${1:-}" = "--refresh" ] && REFRESH=1

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
updated=0
for src in $SOURCES; do
    dir="$TMP/repo/$src"
    [ -d "$dir" ] || continue
    for f in "$dir"/*.png; do
        [ -e "$f" ] || continue
        code="$(basename "$f" .png | tr '[:lower:]' '[:upper:]')"
        if [ ! -f "$DEST/$code.png" ]; then
            cp "$f" "$DEST/$code.png"
            copied=$((copied + 1))
        elif [ "$REFRESH" = "1" ] && ! cmp -s "$f" "$DEST/$code.png"; then
            cp "$f" "$DEST/$code.png"
            updated=$((updated + 1))
        fi
    done
done

rm -rf "$TMP"
echo "Done. Added $copied new logos, updated $updated existing ones in $DEST"
echo "Files are named by ICAO code, e.g. $DEST/DAL.png"
