#!/bin/zsh
# Build "Music Library Manager.app", then a DMG and a ZIP in dist/ that you can share or upload as a GitHub release.
set -e
cd "$(dirname "$0")"
NAME="Music Library Manager"
VERSION=$(.venv/bin/python -c "from mlm import __version__; print(__version__)")
OUT="Music-Library-Manager-$VERSION-$(uname -m)"   # built for this Mac's chip (arm64 = Apple Silicon)
.venv/bin/pip install -q pyinstaller
rm -rf build "dist/$NAME" "dist/$NAME.app"
.venv/bin/pyinstaller --noconfirm --windowed --name "$NAME" --icon assets/AppIcon.icns \
  --osx-bundle-identifier com.local.musiclibrarymanager \
  --add-data "mlm/gui/icon.png:mlm/gui" \
  --collect-data certifi --collect-all pykakasi \
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick \
  --paths . mlm/__main__.py
PLIST="dist/$NAME.app/Contents/Info.plist"
for key in CFBundleShortVersionString CFBundleVersion; do
  /usr/libexec/PlistBuddy -c "Set :$key $VERSION" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Add :$key string $VERSION" "$PLIST"
done
codesign --force --deep --sign - "dist/$NAME.app"   # ad-hoc: lets it run on Apple Silicon (not notarised, see README)
STAGE=$(mktemp -d)
cp -R "dist/$NAME.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"          # drag the app onto this to install
cat > "$STAGE/READ ME FIRST.txt" <<'TXT'
Music Library Manager - install
1. Drag "Music Library Manager" onto the Applications folder.
2. This app is not notarised by Apple, so macOS blocks the first launch of a downloaded copy.
   Unblock it once, either way:
   - Open Terminal and run:   xattr -dr com.apple.quarantine "/Applications/Music Library Manager.app"
   - or double-click the app, then System Settings > Privacy & Security > scroll down > "Open Anyway".
3. Optional, for audio conversion and checks:   brew install ffmpeg chromaprint   (Homebrew: brew.sh)
Apple Silicon Macs only (M1 or newer). Your music files are never changed unless you switch on Edit Mode.
TXT
rm -f "dist/$OUT.dmg" "dist/$OUT.zip"
hdiutil create -quiet -volname "$NAME" -srcfolder "$STAGE" -ov -format UDZO "dist/$OUT.dmg"
ditto -c -k --keepParent "dist/$NAME.app" "dist/$OUT.zip"
rm -rf "$STAGE" build *.spec
(cd dist && shasum -a 256 "$OUT.dmg" "$OUT.zip" > "$OUT.sha256")
echo "Built: dist/$NAME.app, dist/$OUT.dmg, dist/$OUT.zip"
