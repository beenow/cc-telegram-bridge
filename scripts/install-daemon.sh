#!/usr/bin/env bash
# install-daemon.sh — Install clawd-bridge as a macOS launchd user agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

PLIST_LABEL="com.clawd.bridge"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_LABEL.plist"
PYTHON=$(which python3)
LOG_DIR="$PROJECT_DIR/logs"

echo "Installing clawd-bridge daemon..."
echo "  Project: $PROJECT_DIR"
echo "  Python:  $PYTHON"
echo "  Plist:   $PLIST_PATH"
echo ""

# Validate .env exists
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "ERROR: $PROJECT_DIR/.env not found."
    echo "       Copy .env.example to .env and fill in your values."
    exit 1
fi

mkdir -p "$LOG_DIR"
mkdir -p "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$PROJECT_DIR/src/bridge.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
        <key>PATH</key>
        <string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>StandardOutPath</key>
    <string>$LOG_DIR/bridge.log</string>

    <key>StandardErrorPath</key>
    <string>$LOG_DIR/bridge.err</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF

echo "Plist written to $PLIST_PATH"

# Unload if already loaded
if launchctl list | grep -q "$PLIST_LABEL"; then
    echo "Unloading existing daemon..."
    launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

launchctl load "$PLIST_PATH"
echo ""
echo "Daemon installed and started."
echo ""
echo "Useful commands:"
echo "  launchctl list | grep clawd      — check if running"
echo "  tail -f $LOG_DIR/bridge.log      — view logs"
echo "  launchctl unload $PLIST_PATH     — stop daemon"
echo "  bash scripts/uninstall-daemon.sh — remove daemon"
