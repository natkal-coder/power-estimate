#!/bin/sh
# Usage: ./install.sh --psu-w 1300 [--tariff 0.31] [--currency '$']
# --tariff is needed where no open national rate is held for your timezone.
set -eu
DIR=$(cd "$(dirname "$0")" && pwd)
PY=/usr/bin/python3
APPLET="$PY $DIR/power_applet.py"

command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
[ -x "$PY" ] || { echo "$PY is required" >&2; exit 1; }
"$PY" "$DIR/power.py" setup "$@"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
rm -f "$UNIT_DIR/power-log.service"
cat > "$UNIT_DIR/power-log.service" <<UNIT
[Unit]
Description=PC power logger

[Service]
ExecStart=$PY -u $DIR/power.py log
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable power-log.service
systemctl --user restart power-log.service
loginctl enable-linger "$USER" || echo "warning: no linger, logging stops when you log out"

if "$PY" -c 'import gi; gi.require_version("Gtk", "3.0"); gi.require_version("AppIndicator3", "0.1")' 2>/dev/null; then
    mkdir -p "$HOME/.config/autostart"
    cat > "$HOME/.config/autostart/power-applet.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Power applet
Exec=$APPLET
X-GNOME-Autostart-enabled=true
DESKTOP
    pkill -xf "$APPLET" || true
    (setsid $APPLET >/dev/null 2>&1 &)
else
    echo "top-bar applet skipped: needs python3-gi and AppIndicator3"
fi

if ls /sys/class/powercap/intel-rapl:*/energy_uj >/dev/null 2>&1; then
    if ! cat /sys/class/powercap/intel-rapl:0/energy_uj >/dev/null 2>&1; then
        echo "CPU uses an estimate until RAPL is readable. To measure it, run:"
        echo "  sudo cp $DIR/99-rapl.rules /etc/udev/rules.d/ && sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=powercap"
        echo "This lets every local user read CPU energy, a known side channel."
    fi
else
    echo "no RAPL on this PC: CPU power is estimated from utilisation"
fi
echo "installed. Run: $PY $DIR/power.py report"
