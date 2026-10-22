#!/usr/bin/env bash
# Install Taskling for the current user: a private virtualenv that reuses the system PyQt6,
# the 'taskling' command, a menu entry and, with --autostart, a login autostart entry.
# The install is editable: after a 'git pull' the new code is used without reinstalling.
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")" && pwd)"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
config_home="${XDG_CONFIG_HOME:-$HOME/.config}"
venv="$data_home/taskling/venv"

if ! python3 -c 'import PyQt6.QtWidgets' 2>/dev/null; then
    echo "PyQt6 is missing: sudo apt install python3-pyqt6" >&2
    exit 1
fi

python3 -m venv --system-site-packages "$venv"
"$venv/bin/pip" install --quiet --editable "$repo_dir"

mkdir -p "$HOME/.local/bin" "$data_home/applications" "$data_home/icons/hicolor/scalable/apps"
ln -sf "$venv/bin/taskling" "$HOME/.local/bin/taskling"
install -m 644 "$repo_dir/data/taskling.svg" "$data_home/icons/hicolor/scalable/apps/taskling.svg"
# Absolute Exec path: the desktop session may not have ~/.local/bin in its PATH.
sed "s|^Exec=taskling|Exec=$venv/bin/taskling|" "$repo_dir/data/taskling.desktop" \
    > "$data_home/applications/taskling.desktop"

if [[ "${1:-}" == "--autostart" ]]; then
    mkdir -p "$config_home/autostart"
    cp "$data_home/applications/taskling.desktop" "$config_home/autostart/taskling.desktop"
fi

echo "Taskling installed. Next steps (see README.md):"
echo "  1. save your OAuth client as $config_home/taskling/client_secret.json"
echo "  2. run: taskling auth"
echo "  3. start the tray app from the menu, or run: taskling &"
