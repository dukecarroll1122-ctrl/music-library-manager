#!/bin/zsh
# Build a standalone "Music Library Manager.app" into dist/ with PyInstaller.
cd "$(dirname "$0")"
.venv/bin/pip install -q pyinstaller || exit 1
.venv/bin/pyinstaller --noconfirm --windowed --name "Music Library Manager" \
  --osx-bundle-identifier com.local.musiclibrarymanager \
  --collect-data certifi \
  --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.Qt3DCore --exclude-module PySide6.QtQuick \
  --paths . mlm/__main__.py
echo "Built: dist/Music Library Manager.app"
