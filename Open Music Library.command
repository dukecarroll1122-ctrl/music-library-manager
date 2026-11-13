#!/bin/zsh
# Double-click to open Music Library Manager. It first re-scans your music folders (fast: only new or changed
# files are read), so the library is up to date every time you open it.
cd "$HOME/MLMDA" || { echo "Cannot find ~/MLMDA"; read -k1; exit 1; }
exec ./run.sh --rescan
