#!/bin/zsh
# Build "Footage Offload.app" — a double-clickable launcher for gui.py.
# The app points at this folder, so re-run this script if the folder is moved.
set -euo pipefail
DIR="${0:A:h}"
APP="$DIR/Footage Offload.app"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$DIR/assets/icon.icns" "$APP/Contents/Resources/icon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Footage Offload</string>
  <key>CFBundleDisplayName</key><string>Footage Offload</string>
  <key>CFBundleIdentifier</key><string>com.footage-offload.app</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/launcher" <<LAUNCH
#!/bin/zsh
DIR="$DIR"
LOG="\$HOME/Library/Logs/Footage Offload.log"
alert() { osascript -e "display alert \"Footage Offload\" message \"\$1\"" >/dev/null; exit 1; }
UV=""
for c in /opt/homebrew/bin/uv /usr/local/bin/uv "\$HOME/.local/bin/uv" "\$HOME/.cargo/bin/uv"; do
  [ -x "\$c" ] && UV="\$c" && break
done
[ -n "\$UV" ] || alert "uv is not installed. Install it with: brew install uv"
[ -f "\$DIR/gui.py" ] || alert "The project folder was moved. Run make_app.sh again from its new location."
exec "\$UV" run --script "\$DIR/gui.py" >>"\$LOG" 2>&1
LAUNCH
chmod +x "$APP/Contents/MacOS/launcher"

# Download the app's Python dependencies now, so the first launch is instant.
uv run --script "$DIR/gui.py" --help >/dev/null
touch "$APP"
echo "Built: $APP"
