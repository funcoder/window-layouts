#!/usr/bin/env bash
# Copy this repo into the Omarchy plugins dir and validate it. The shell does
# not follow symlinked plugin folders, so this copies instead of linking —
# re-run after every edit (saving under ~/.config/omarchy/plugins hot-reloads).
set -euo pipefail

ID="funcoder.window-layouts"
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.config/omarchy/plugins/$ID"

omarchy plugin validate "$SRC"

if [[ -L "$DEST" ]]; then
  rm "$DEST"
fi
mkdir -p "$DEST"

if command -v rsync >/dev/null 2>&1; then
  rsync -a --delete --exclude .git --exclude __pycache__ --exclude deploy-local.sh "$SRC/" "$DEST/"
else
  find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  (cd "$SRC" && find . -mindepth 1 -maxdepth 1 ! -name .git ! -name __pycache__ ! -name deploy-local.sh -exec cp -r {} "$DEST/" \;)
fi
chmod +x "$DEST/layouts.py"
echo "Copied $SRC -> $DEST"

omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
echo "If Panel.qml changes don't show up, run: omarchy restart shell"

if ! omarchy plugin list 2>/dev/null | grep -q "$ID.*enabled"; then
  cat <<EOF

Enable it with:
  omarchy plugin enable $ID left
EOF
fi
