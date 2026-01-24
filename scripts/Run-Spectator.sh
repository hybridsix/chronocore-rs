#!/bin/bash
# ChronoCoreRS Spectator Display Launcher
# 
# This script launches the spectator UI in Chrome fullscreen mode.
# Designed to run on a separate display/machine (Debian/Ubuntu Linux).
#
# Usage:
#   ./Run-Spectator.sh [SERVER_IP] [PORT]
#
# Examples:
#   ./Run-Spectator.sh                    # Default: localhost:8000
#   ./Run-Spectator.sh 192.168.1.100      # Custom server, port 8000
#   ./Run-Spectator.sh 192.168.1.100 8080 # Custom server and port

set -e

# Configuration
SERVER="${1:-localhost}"
PORT="${2:-8000}"
URL="http://${SERVER}:${PORT}/ui/spectator/"

echo "=== ChronoCoreRS Spectator Display ==="
echo ""
echo "Server:  ${SERVER}"
echo "Port:    ${PORT}"
echo "URL:     ${URL}"
echo ""

# Find Chrome/Chromium executable
if command -v google-chrome &> /dev/null; then
    CHROME="google-chrome"
elif command -v chromium &> /dev/null; then
    CHROME="chromium"
elif command -v chromium-browser &> /dev/null; then
    CHROME="chromium-browser"
else
    echo "ERROR: Chrome/Chromium not found!"
    echo ""
    echo "Install with:"
    echo "  sudo apt update"
    echo "  sudo apt install chromium-browser"
    echo "    or"
    echo "  sudo apt install google-chrome-stable"
    exit 1
fi

echo "Using browser: ${CHROME}"
echo ""

# Test server connectivity
echo "Testing connection to server..."
if ! curl -s -f -m 5 "http://${SERVER}:${PORT}/healthz" > /dev/null 2>&1; then
    echo ""
    echo "WARNING: Cannot connect to server at ${SERVER}:${PORT}"
    echo "Make sure the ChronoCoreRS server is running."
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    echo "Server is online."
fi

echo ""
echo "Launching spectator display in fullscreen mode..."
echo "Press F11 to exit fullscreen, Ctrl+W to close window"
echo ""

# Launch Chrome in fullscreen (not kiosk) mode
# --start-fullscreen: Opens in fullscreen (can exit with F11)
# --app: Opens in app mode (minimal UI)
# --disable-infobars: Hides info bars
# --noerrdialogs: Suppresses error dialogs
# --disable-session-crashed-bubble: Don't show "Chrome didn't shut down correctly"
# --disable-features=TranslateUI: Disable translate bar

exec "${CHROME}" \
    --start-fullscreen \
    --app="${URL}" \
    --disable-infobars \
    --noerrdialogs \
    --disable-session-crashed-bubble \
    --disable-features=TranslateUI \
    --disable-component-update \
    --no-first-run \
    --no-default-browser-check
