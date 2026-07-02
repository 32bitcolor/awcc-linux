"""Per-user GUI preferences: autostart and the tray temperature label.

These are user-session concerns (not system/daemon state), so they live under
~/.config rather than /var/lib/awcc. Autostart is represented by the presence of
a freedesktop .desktop file in ~/.config/autostart; the label preference is a
small JSON file.
"""

from __future__ import annotations

import json
import os
import shutil
import sys

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "awcc")
GUI_CONFIG = os.path.join(CONFIG_DIR, "gui.json")

AUTOSTART_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, "io.github.awcclinux.Awcc.desktop")

# Tray label options.
LABEL_OFF, LABEL_CPU, LABEL_GPU, LABEL_BOTH = "off", "cpu", "gpu", "both"
LABEL_CHOICES = [LABEL_OFF, LABEL_CPU, LABEL_GPU, LABEL_BOTH]
LABEL_NAMES = {LABEL_OFF: "Off", LABEL_CPU: "CPU", LABEL_GPU: "GPU",
               LABEL_BOTH: "CPU + GPU"}

DEFAULTS = {"tray_label": LABEL_CPU}


def _exec_command() -> str:
    """Best path to relaunch the GUI in autostart (hidden to tray)."""
    exe = shutil.which("awcc")
    if not exe:
        exe = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else "awcc"
    return f"{exe} --hidden"


class Settings:
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(GUI_CONFIG) as fh:
                loaded = json.load(fh)
            for k in DEFAULTS:
                if k in loaded:
                    self.data[k] = loaded[k]
        except (OSError, json.JSONDecodeError):
            pass

    def save(self):
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(GUI_CONFIG, "w") as fh:
                json.dump(self.data, fh, indent=2)
        except OSError as exc:
            print(f"[awcc] could not save settings: {exc}", flush=True)

    # -- tray label --------------------------------------------------------

    @property
    def tray_label(self) -> str:
        v = self.data.get("tray_label", LABEL_CPU)
        return v if v in LABEL_CHOICES else LABEL_CPU

    @tray_label.setter
    def tray_label(self, value: str):
        if value in LABEL_CHOICES:
            self.data["tray_label"] = value
            self.save()

    def format_label(self, cpu, gpu) -> str:
        """Single-line text for the tooltip / XAyatanaLabel."""
        pref = self.tray_label
        if pref == LABEL_OFF:
            return ""
        if pref == LABEL_CPU:
            return f"{cpu:.0f}°" if cpu is not None else ""
        if pref == LABEL_GPU:
            return f"{gpu:.0f}°" if gpu is not None else ""
        # both
        c = f"{cpu:.0f}" if cpu is not None else "–"
        g = f"{gpu:.0f}" if gpu is not None else "–"
        return f"{c}/{g}°"

    def label_entries(self, cpu, gpu):
        """Lines for the tray icon pixmap: list of (temp, kind).

        `kind` is "cpu" or "gpu". `both` yields two stacked entries (CPU then
        GPU) so the icon can stay square and readable rather than a wide, shrunk
        single line; the tray adds C/G prefixes to disambiguate them.
        """
        pref = self.tray_label
        out = []
        if pref == LABEL_OFF:
            return out
        if pref in (LABEL_CPU, LABEL_BOTH) and cpu is not None:
            out.append((cpu, "cpu"))
        if pref in (LABEL_GPU, LABEL_BOTH) and gpu is not None:
            out.append((gpu, "gpu"))
        return out

    # -- autostart ---------------------------------------------------------

    @staticmethod
    def is_autostart_enabled() -> bool:
        return os.path.isfile(AUTOSTART_FILE)

    @staticmethod
    def set_autostart(enabled: bool) -> bool:
        try:
            if enabled:
                os.makedirs(AUTOSTART_DIR, exist_ok=True)
                content = (
                    "[Desktop Entry]\n"
                    "Type=Application\n"
                    "Name=AWCC-Linux\n"
                    "GenericName=Thermal & Fan Control\n"
                    f"Exec={_exec_command()}\n"
                    "Icon=io.github.awcclinux.Awcc\n"
                    "Terminal=false\n"
                    "Categories=System;Settings;\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
                with open(AUTOSTART_FILE, "w") as fh:
                    fh.write(content)
            else:
                if os.path.isfile(AUTOSTART_FILE):
                    os.unlink(AUTOSTART_FILE)
            return True
        except OSError as exc:
            print(f"[awcc] autostart change failed: {exc}", flush=True)
            return False
