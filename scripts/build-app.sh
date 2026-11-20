#!/bin/bash
# Builds SyncBridge.app into ./build. Requires the Xcode Command Line Tools.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP="$ROOT/build/SyncBridge.app"
VERSION="${VERSION:-1.0.0}"

# Python is shipped inside the app, so users never install it. The engine only
# uses the standard library, so this is its only dependency.
PYTHON_RELEASE="20261110"
PYTHON_VERSION="3.13.18"
ARCH="$(uname -m)"   # arm64 or x86_64, same as the app binary built below
PYTHON_ARCH="$([ "$ARCH" = arm64 ] && echo aarch64 || echo x86_64)"
PYTHON_TARBALL="cpython-${PYTHON_VERSION}+${PYTHON_RELEASE}-${PYTHON_ARCH}-apple-darwin-install_only_stripped.tar.gz"
PYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_RELEASE}"
CACHE="$ROOT/build-cache"

swift build -c release --package-path "$ROOT/app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"
cp "$ROOT/app/.build/release/SyncBridge" "$APP/Contents/MacOS/SyncBridge"
cp "$ROOT/engine/syncbridge.py" "$APP/Contents/Resources/engine/syncbridge.py"
cp "$ROOT/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

mkdir -p "$CACHE"
if [ ! -f "$CACHE/$PYTHON_TARBALL" ]; then
    curl -fsSL -o "$CACHE/SHA256SUMS" "$PYTHON_URL/SHA256SUMS"
    curl -fsSL -o "$CACHE/$PYTHON_TARBALL.part" "$PYTHON_URL/$PYTHON_TARBALL"
    expected="$(grep " $PYTHON_TARBALL\$" "$CACHE/SHA256SUMS" | cut -d' ' -f1)"
    actual="$(shasum -a 256 "$CACHE/$PYTHON_TARBALL.part" | cut -d' ' -f1)"
    if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
        echo "Checksum mismatch for $PYTHON_TARBALL" >&2
        rm -f "$CACHE/$PYTHON_TARBALL.part"
        exit 1
    fi
    mv "$CACHE/$PYTHON_TARBALL.part" "$CACHE/$PYTHON_TARBALL"
fi
tar -xzf "$CACHE/$PYTHON_TARBALL" -C "$APP/Contents/Resources"
# Parts of the standard library the engine never uses.
PYLIB="$APP/Contents/Resources/python/lib/python${PYTHON_VERSION%.*}"
rm -rf "$PYLIB/test" "$PYLIB/idlelib" "$PYLIB/tkinter" "$PYLIB/turtledemo" "$PYLIB/ensurepip" \
    "$PYLIB/lib2to3" "$APP/Contents/Resources/python/lib/"tcl* "$APP/Contents/Resources/python/lib/"tk* \
    "$APP/Contents/Resources/python/include" "$APP/Contents/Resources/python/share"
"$APP/Contents/Resources/python/bin/python3" -c "import json, zipfile, xml.etree.ElementTree, fcntl, difflib"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>SyncBridge</string>
    <key>CFBundleDisplayName</key><string>SyncBridge</string>
    <key>CFBundleIdentifier</key><string>io.github.sternbj.syncbridge</string>
    <key>CFBundleExecutable</key><string>SyncBridge</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$VERSION</string>
    <key>CFBundleVersion</key><string>$VERSION</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSAppTransportSecurity</key>
    <dict><key>NSAllowsLocalNetworking</key><true/></dict>
    <key>NSLocalNetworkUsageDescription</key>
    <string>SyncBridge connects to a local AI model server (such as Ollama) only if you choose one in Settings › AI.</string>
</dict>
</plist>
PLIST

# Ad-hoc signature so macOS runs the locally built app.
codesign --force --deep --sign - "$APP"
echo "Built $APP"
