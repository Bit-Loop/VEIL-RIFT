#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
make install
echo "Installed VEIL-RIFT wallpaper suite to ~/.local/bin and ~/.local/share/wallpaper-mode"
echo "Run: wallpaper-mode corrupt-rt"
