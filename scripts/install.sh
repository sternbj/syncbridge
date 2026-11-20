#!/bin/bash
# Builds SyncBridge, installs it to ~/Applications, enables start at login and opens it.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$HOME/Applications/SyncBridge.app"
CONFIG_DIR="$HOME/Library/Application Support/SyncBridge"
AGENT="$HOME/Library/LaunchAgents/io.github.sternbj.syncbridge.plist"

"$ROOT/scripts/build-app.sh"

osascript -e 'tell application "SyncBridge" to quit' >/dev/null 2>&1 || true
mkdir -p "$HOME/Applications"
rm -rf "$TARGET"
cp -R "$ROOT/build/SyncBridge.app" "$TARGET"
# Remove the build copy so Spotlight lists only the installed app.
rm -rf "$ROOT/build/SyncBridge.app"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$TARGET"

if [ ! -f "$CONFIG_DIR/config.json" ]; then
    mkdir -p "$CONFIG_DIR"
    cp "$ROOT/config.example.json" "$CONFIG_DIR/config.json"
    echo "Created $CONFIG_DIR/config.json from the example. Edit your folder pairs in Settings."
fi

# Written every time, so older agents get the --background flag.
mkdir -p "$(dirname "$AGENT")"
cat > "$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>io.github.sternbj.syncbridge</string>
    <key>ProgramArguments</key>
    <array><string>/usr/bin/open</string><string>-g</string><string>-a</string><string>$TARGET</string><string>--args</string><string>--background</string></array>
    <key>RunAtLoad</key><true/>
</dict>
</plist>
PLIST

open -g "$TARGET" --args --background
echo "Installed $TARGET"
