#!/usr/bin/env bash
# Install Proton Porter for the current user (no root needed).
#   ./install.sh            install or update
#   ./install.sh --remove   uninstall
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
share=${XDG_DATA_HOME:-$HOME/.local/share}
app=$share/proton-porter/app
bin=$HOME/.local/bin/proton-porter

if [ "${1:-}" = --remove ]; then
  rm -rf "$app" "$bin" "$share/applications/io.github.xmilon.ProtonPorter.desktop" \
    "$share/icons/hicolor/scalable/apps/io.github.xmilon.ProtonPorter.svg"
  echo "Removed. Backups stay in ~/.local/state/proton-porter."
  exit 0
fi

missing=()
python3 -c 'import vdf' 2>/dev/null || missing+=(python-vdf)
python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null \
  || missing+=(python-gobject gtk4 libadwaita)
if [ ${#missing[@]} -gt 0 ]; then
  echo "Installing: ${missing[*]}"
  sudo pacman -S --needed --noconfirm "${missing[@]}"
fi

rm -rf "$app"
mkdir -p "$app" "$(dirname "$bin")" "$share/applications" "$share/icons/hicolor/scalable/apps"
cp -r "$here/proton_porter" "$app/"
cat > "$bin" <<SH
#!/bin/sh
PYTHONPATH="$app\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m proton_porter "\$@"
SH
chmod +x "$bin"
cp "$here/data/io.github.xmilon.ProtonPorter.desktop" "$share/applications/"
cp "$here/data/io.github.xmilon.ProtonPorter.svg" "$share/icons/hicolor/scalable/apps/"
update-desktop-database -q "$share/applications" 2>/dev/null || true
gtk-update-icon-cache -q "$share/icons/hicolor" 2>/dev/null || true
echo "Installed. Open “Proton Porter” from the app launcher, or run: proton-porter"
