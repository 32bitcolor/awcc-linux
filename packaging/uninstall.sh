#!/usr/bin/env bash
# Remove AWCC-Linux. Run: ./uninstall.sh  (re-execs with pkexec/sudo).
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    if command -v pkexec >/dev/null 2>&1 && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
        exec pkexec bash "$(readlink -f "$0")" "$@"
    else
        exec sudo bash "$(readlink -f "$0")" "$@"
    fi
fi

echo "==> Removing AWCC-Linux"
systemctl disable --now awccd.service 2>/dev/null || true
rm -f /etc/systemd/system/awccd.service
systemctl daemon-reload

rm -f /usr/local/bin/awcc /usr/local/bin/awcc-cli
rm -f /usr/local/share/applications/io.github.awcclinux.Awcc.desktop
rm -f /usr/local/share/icons/hicolor/scalable/apps/io.github.awcclinux.Awcc.svg
rm -rf /opt/awcc

echo "==> Removed. (Kept /var/lib/awcc config; delete it manually if you want.)"
