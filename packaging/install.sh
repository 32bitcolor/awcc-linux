#!/usr/bin/env bash
#
# AWCC-Linux installer. Copies the app to /opt/awcc, installs and starts the
# root daemon (systemd), and registers the GUI launcher + desktop entry.
#
# Run it via:   ./install.sh          (it will re-exec itself with pkexec/sudo)
# or directly:  sudo ./install.sh
#
# Works on immutable/atomic distros (Bazzite, Silverblue, Kinoite): everything
# lands in writable locations (/opt -> /var/opt, /etc, /usr/local -> /var/usrlocal).

set -euo pipefail

PREFIX=/opt/awcc
UNIT=/etc/systemd/system/awccd.service
BINDIR=/usr/local/bin
APPDIR=/usr/local/share/applications
ICONDIR=/usr/local/share/icons/hicolor/scalable/apps

# Resolve the repo root (parent of this script's dir).
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

# Re-exec as root if needed.
if [[ $EUID -ne 0 ]]; then
    echo "AWCC-Linux install needs root to install the system service."
    if command -v pkexec >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        exec pkexec bash "$(readlink -f "$0")" "$@"
    else
        exec sudo bash "$(readlink -f "$0")" "$@"
    fi
fi

echo "==> Installing AWCC-Linux from: $SRC"

# 1. App files -> /opt/awcc
install -d "$PREFIX"
cp -a "$SRC/awccd"       "$PREFIX/"
cp -a "$SRC/awcc_gui"    "$PREFIX/"
cp -a "$SRC/awcc_client.py" "$PREFIX/"
cp -a "$SRC/awcc"        "$PREFIX/"
cp -a "$SRC/awcc-cli"    "$PREFIX/"
cp -a "$SRC/data"        "$PREFIX/"
chmod +x "$PREFIX/awcc" "$PREFIX/awcc-cli"
# Drop any stale bytecode from the source tree.
find "$PREFIX" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# 2. CLI/GUI launchers in PATH
install -d "$BINDIR"
ln -sf "$PREFIX/awcc"     "$BINDIR/awcc"
ln -sf "$PREFIX/awcc-cli" "$BINDIR/awcc-cli"

# 3. Desktop entry + icon
install -d "$APPDIR" "$ICONDIR"
install -m 0644 "$SRC/packaging/awcc.desktop" "$APPDIR/io.github.awcclinux.Awcc.desktop"
install -m 0644 "$SRC/data/io.github.awcclinux.Awcc.svg" \
    "$ICONDIR/io.github.awcclinux.Awcc.svg"
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPDIR" 2>/dev/null || true

# 4. systemd service
install -m 0644 "$SRC/packaging/awccd.service" "$UNIT"
systemctl daemon-reload
systemctl enable --now awccd.service

echo
echo "==> Done."
systemctl --no-pager --lines=0 status awccd.service || true
echo
echo "Launch the GUI from your app menu (AWCC-Linux) or run:  awcc"
echo "Terminal control:  awcc-cli status"
