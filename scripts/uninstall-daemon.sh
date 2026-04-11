#!/usr/bin/env bash
# uninstall-daemon.sh — Remove clawd-bridge launchd user agent
set -euo pipefail

PLIST_LABEL="com.clawd.bridge"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "Plist not found at $PLIST_PATH — daemon may not be installed."
    exit 0
fi

echo "Stopping and removing clawd-bridge daemon..."

launchctl unload "$PLIST_PATH" 2>/dev/null || true
rm -f "$PLIST_PATH"

echo "Done. Plist removed from $PLIST_PATH"
echo "Log files (if any) remain in the logs/ directory."
