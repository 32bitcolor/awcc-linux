"""AWCC-Linux GTK4 / libadwaita application."""

from __future__ import annotations

import os
import sys

# Work around a compositor damage-tracking artifact (observed on NVIDIA + KWin):
# a stray 1px pixel appears next to group titles and only clears on window
# resize. The app's own render is clean — it's introduced during partial
# repaints — so the fix is to (a) use the software renderer, which is immune to
# GPU-renderer damage quirks, and (b) force full-scene redraws so no partial
# damage region can leave a stale pixel. Both are cheap for this lightweight UI
# and must be set before GTK initialises. Respect explicit overrides.
os.environ.setdefault("GSK_RENDERER", "cairo")
os.environ.setdefault("GSK_DEBUG", "full-redraw")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from awcc_client import (  # noqa: E402
    CMD_SET_CURVE, CMD_SET_MANUAL, CMD_SET_MODE, CMD_SET_PROFILE,
)
from awcc_gui import __version__  # noqa: E402
from awcc_gui.backend import Backend  # noqa: E402
from awcc_gui.curve_editor import FanCurveEditor  # noqa: E402
from awcc_gui.graph import HistoryGraph  # noqa: E402
from awcc_gui.settings import (  # noqa: E402
    LABEL_CHOICES, LABEL_NAMES, Settings,
)
from awcc_gui.tray import Tray  # noqa: E402

APP_ID = "io.github.awcclinux.Awcc"

MODE_PROFILE, MODE_CUSTOM, MODE_MANUAL = "profile", "custom", "manual"

# Friendly names/descriptions for the firmware thermal profiles.
PROFILE_INFO = {
    "quiet": ("Quiet", "Lowest fan noise, reduced power"),
    "cool": ("Cool", "Keep surfaces cool"),
    "balanced": ("Balanced", "Default day-to-day profile"),
    "balanced-performance": ("Balanced+", "Leans toward performance"),
    "performance": ("Performance", "Max performance / G-Mode fans"),
    "custom": ("Custom", "Driven by AWCC-Linux fan curves"),
}

ACCENT_CPU = (0.20, 0.55, 0.95)
ACCENT_GPU = (0.35, 0.78, 0.45)


def stat_card(title: str) -> tuple[Gtk.Widget, Gtk.Label, Gtk.Label]:
    """A big-number card. Returns (widget, value_label, sub_label)."""
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    card.add_css_class("card")
    card.set_size_request(150, 96)
    card.set_hexpand(True)
    card.set_valign(Gtk.Align.CENTER)
    card.set_margin_top(6)
    card.set_margin_bottom(6)

    t = Gtk.Label(label=title)
    t.add_css_class("dim-label")
    t.add_css_class("caption")
    t.set_margin_top(10)

    value = Gtk.Label(label="—")
    value.add_css_class("title-1")

    sub = Gtk.Label(label="")
    sub.add_css_class("dim-label")
    sub.add_css_class("caption")
    sub.set_margin_bottom(10)

    card.append(t)
    card.append(value)
    card.append(sub)
    return card, value, sub


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="AWCC-Linux")
        self.set_default_size(760, 720)

        self.state: dict = {}
        self.settings = Settings()
        self._curves_loaded = False
        self._profile_buttons: dict[str, Gtk.ToggleButton] = {}
        self._mode_buttons: dict[str, Gtk.ToggleButton] = {}
        self._suppress = False  # guard against feedback when setting toggles

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self._switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self._switcher)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gtk.PopoverMenu()
        from gi.repository import Gio
        model = Gio.Menu()
        model.append("About AWCC-Linux", "app.about")
        model.append("Quit", "app.quit")
        menu.set_menu_model(model)
        menu_btn.set_popover(menu)
        header.pack_end(menu_btn)

        toolbar.add_top_bar(header)

        self._banner = Adw.Banner(revealed=False)
        toolbar.add_top_bar(self._banner)

        self._stack = Adw.ViewStack()
        self._switcher.set_stack(self._stack)
        self._stack.add_titled_with_icon(
            self._build_dashboard(), "dash", "Dashboard", "speedometer-symbolic")
        self._stack.add_titled_with_icon(
            self._build_fans(), "fans", "Fans", "weather-windy-symbolic")
        self._stack.add_titled_with_icon(
            self._build_curves(), "curves", "Curves", "network-cellular-signal-good-symbolic")
        self._stack.add_titled_with_icon(
            self._build_power(), "power", "Power", "power-profile-performance-symbolic")
        self._stack.add_titled_with_icon(
            self._build_settings(), "settings", "Settings", "emblem-system-symbolic")

        toolbar.set_content(self._stack)

        switcher_bar = Adw.ViewSwitcherBar()
        switcher_bar.set_stack(self._stack)
        switcher_bar.set_reveal(True)
        toolbar.add_bottom_bar(switcher_bar)

        self.set_content(toolbar)

        self._last_prof = None
        self._last_mode = None
        self._quitting = False

        self.backend = Backend(self._on_state, self._on_status)
        self.backend.start()

        # System-tray icon (StatusNotifierItem). Lets the app keep running in
        # the tray after the window is closed.
        self.tray = Tray(self)
        self.connect("close-request", self._on_close)

    # -- dashboard page ----------------------------------------------------

    def _build_dashboard(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        # Profile selector.
        gprof = Adw.PreferencesGroup(title="Thermal Profile")
        self._profile_box = Gtk.Box(spacing=0)
        self._profile_box.add_css_class("linked")
        self._profile_box.set_halign(Gtk.Align.CENTER)
        self._profile_box.set_margin_top(4)
        self._profile_box.set_margin_bottom(4)
        gprof.add(self._profile_box)
        page.add(gprof)

        # Stat cards.
        gstat = Adw.PreferencesGroup()
        grid = Gtk.Box(spacing=10, homogeneous=True)
        self._cards = {}
        for key, title in (("cpu", "CPU"), ("gpu", "GPU"),
                           ("gutil", "GPU Load"), ("gpow", "GPU Power")):
            card, val, sub = stat_card(title)
            self._cards[key] = (val, sub)
            grid.append(card)
        gstat.add(grid)
        page.add(gstat)

        # History graph.
        ggraph = Adw.PreferencesGroup(title="Temperature history")
        self._graph = HistoryGraph(y_max=100)
        self._graph.add_series("CPU", ACCENT_CPU)
        self._graph.add_series("GPU", ACCENT_GPU)
        gframe = Gtk.Frame()
        gframe.add_css_class("view")
        gframe.set_child(self._graph)
        ggraph.add(gframe)
        page.add(ggraph)

        # Fan readouts.
        self._fan_group = Adw.PreferencesGroup(title="Fans")
        self._fan_rows: dict[int, Adw.ActionRow] = {}
        page.add(self._fan_group)

        # All sensors expander.
        self._sensors_group = Adw.PreferencesGroup(title="All sensors")
        self._sensor_rows: dict[str, Adw.ActionRow] = {}
        page.add(self._sensors_group)

        return page

    # -- fans page ---------------------------------------------------------

    def _build_fans(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        gmode = Adw.PreferencesGroup(
            title="Fan control mode",
            description="Follow Profile hands fans to firmware. "
                        "Custom Curves and Manual use additive boost "
                        "(the EC keeps its own safety floor — boost never "
                        "makes cooling worse).")
        self._mode_box = Gtk.Box(spacing=0)
        self._mode_box.add_css_class("linked")
        self._mode_box.set_halign(Gtk.Align.CENTER)
        self._mode_box.set_margin_top(4)
        self._mode_box.set_margin_bottom(4)
        mode_group = None
        for mode, label in ((MODE_PROFILE, "Follow Profile"),
                            (MODE_CUSTOM, "Custom Curves"),
                            (MODE_MANUAL, "Manual")):
            btn = Gtk.ToggleButton(label=label)
            if mode_group is None:
                mode_group = btn
            else:
                btn.set_group(mode_group)  # radio behaviour: mutually exclusive
            btn.connect("toggled", self._on_mode_toggled, mode)
            self._mode_buttons[mode] = btn
            self._mode_box.append(btn)
        gmode.add(self._mode_box)
        page.add(gmode)

        gman = Adw.PreferencesGroup(
            title="Manual fan boost",
            description="Active in Manual mode. 0% = firmware default, "
                        "100% = maximum boost.")
        self._manual_scales = {}
        for group, label in (("cpu", "CPU fans"), ("gpu", "GPU fans")):
            row = Adw.ActionRow(title=label)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 5)
            scale.set_hexpand(True)
            scale.set_size_request(280, -1)
            scale.set_draw_value(True)
            scale.set_value_pos(Gtk.PositionType.RIGHT)
            for mark in (0, 25, 50, 75, 100):
                scale.add_mark(mark, Gtk.PositionType.BOTTOM, None)
            scale.connect("value-changed", self._on_manual_changed, group)
            self._manual_scales[group] = scale
            row.add_suffix(scale)
            gman.add(row)
        self._manual_group = gman
        page.add(gman)

        return page

    # -- curves page -------------------------------------------------------

    def _build_curves(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self._editors = {}
        for group, title, accent in (("cpu", "CPU fan curve", ACCENT_CPU),
                                     ("gpu", "GPU fan curve", ACCENT_GPU)):
            g = Adw.PreferencesGroup(
                title=title,
                description="Drag points to shape. Double-click to add, "
                            "right-click to remove. Applies in Custom Curves mode.")
            editor = FanCurveEditor(accent=accent)
            editor.connect("changed", self._on_curve_changed, group)
            self._editors[group] = editor
            frame = Gtk.Frame()
            frame.add_css_class("view")
            frame.set_child(editor)
            g.add(frame)

            reset = Gtk.Button(label="Reset to default")
            reset.set_halign(Gtk.Align.END)
            reset.set_margin_top(6)
            reset.add_css_class("flat")
            reset.connect("clicked", self._on_curve_reset, group)
            g.add(reset)
            page.add(g)
        return page

    # -- power page --------------------------------------------------------

    def _build_power(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self._gpu_range_set = False
        self._gpu_send_id = 0
        self._epp_values: list = []      # index -> epp string (index 0 = Auto)
        self._gov_values: list = []

        # GPU power limit.
        self._gpu_group = Adw.PreferencesGroup(
            title="Graphics power (NVIDIA)",
            description="Cap the GPU board power. Lower = cooler and quieter; "
                        "higher = more performance headroom.")
        self._gpu_override = Adw.SwitchRow(
            title="Limit GPU power",
            subtitle="Off = firmware default")
        self._gpu_override.connect("notify::active", self._on_gpu_override)
        self._gpu_group.add(self._gpu_override)

        self._gpu_row = Adw.ActionRow(title="Power limit")
        self._gpu_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 5, 175, 5)
        self._gpu_scale.set_size_request(300, -1)
        self._gpu_scale.set_hexpand(True)
        self._gpu_scale.set_draw_value(True)
        self._gpu_scale.set_value_pos(Gtk.PositionType.RIGHT)
        self._gpu_scale.connect("value-changed", self._on_gpu_scale)
        self._gpu_row.add_suffix(self._gpu_scale)
        self._gpu_group.add(self._gpu_row)
        page.add(self._gpu_group)

        # CPU performance.
        gcpu = Adw.PreferencesGroup(
            title="CPU performance",
            description="Energy-performance preference and frequency governor. "
                        "'Auto' leaves them to the selected thermal profile.")
        self._epp_row = Adw.ComboRow(title="Energy performance preference")
        self._epp_row.connect("notify::selected", self._on_epp)
        gcpu.add(self._epp_row)
        self._gov_row = Adw.ComboRow(title="Frequency governor")
        self._gov_row.connect("notify::selected", self._on_gov)
        gcpu.add(self._gov_row)
        page.add(gcpu)

        # Advanced: CPU package power limits.
        gadv = Adw.PreferencesGroup(
            title="CPU package power (advanced)",
            description="Intel RAPL limits. PL1 is sustained power, PL2 is the "
                        "short turbo burst. Lower to reduce heat; raising may "
                        "increase temperatures.")
        self._pl_override = Adw.SwitchRow(
            title="Override CPU power limits", subtitle="Off = firmware default")
        self._pl_override.connect("notify::active", self._on_pl_override)
        gadv.add(self._pl_override)
        self._pl1_row = Adw.SpinRow.new_with_range(10, 250, 5)
        self._pl1_row.set_title("PL1 — sustained (W)")
        self._pl1_row.connect("notify::value", self._on_pl_value, "cpu_pl1_w")
        gadv.add(self._pl1_row)
        self._pl2_row = Adw.SpinRow.new_with_range(10, 300, 5)
        self._pl2_row.set_title("PL2 — turbo (W)")
        self._pl2_row.connect("notify::value", self._on_pl_value, "cpu_pl2_w")
        gadv.add(self._pl2_row)
        self._pl_group = gadv
        page.add(gadv)

        return page

    # -- power handlers ----------------------------------------------------

    def _on_gpu_override(self, row, _p):
        if self._suppress:
            return
        if row.get_active():
            self.backend.send({"cmd": "set_power", "field": "gpu_limit_w",
                               "value": int(self._gpu_scale.get_value())})
        else:
            self.backend.send({"cmd": "set_power", "field": "gpu_limit_w",
                               "value": None})
        self._gpu_row.set_sensitive(row.get_active())

    def _on_gpu_scale(self, scale):
        if self._suppress or not self._gpu_override.get_active():
            return
        # nvidia-smi is slow, so debounce: only send after the value settles.
        if self._gpu_send_id:
            GLib.source_remove(self._gpu_send_id)
        val = int(scale.get_value())
        self._gpu_send_id = GLib.timeout_add(
            350, lambda: self._send_gpu(val))

    def _send_gpu(self, val):
        self._gpu_send_id = 0
        self.backend.send({"cmd": "set_power", "field": "gpu_limit_w", "value": val})
        return False

    def _on_epp(self, row, _p):
        if self._suppress:
            return
        idx = row.get_selected()
        val = None if idx == 0 else self._epp_values[idx]
        self.backend.send({"cmd": "set_power", "field": "cpu_epp", "value": val})

    def _on_gov(self, row, _p):
        if self._suppress:
            return
        idx = row.get_selected()
        val = None if idx == 0 else self._gov_values[idx]
        self.backend.send({"cmd": "set_power", "field": "cpu_governor", "value": val})

    def _on_pl_override(self, row, _p):
        if self._suppress:
            return
        on = row.get_active()
        self._pl1_row.set_sensitive(on)
        self._pl2_row.set_sensitive(on)
        if on:
            self.backend.send({"cmd": "set_power", "field": "cpu_pl1_w",
                               "value": int(self._pl1_row.get_value())})
            self.backend.send({"cmd": "set_power", "field": "cpu_pl2_w",
                               "value": int(self._pl2_row.get_value())})
        else:
            for f in ("cpu_pl1_w", "cpu_pl2_w"):
                self.backend.send({"cmd": "set_power", "field": f, "value": None})

    def _on_pl_value(self, row, _p, field):
        if self._suppress or not self._pl_override.get_active():
            return
        self.backend.send({"cmd": "set_power", "field": field,
                           "value": int(row.get_value())})

    # -- settings page -----------------------------------------------------

    def _build_settings(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(
            title="Startup and Tray",
            description="These preferences are per-user and take effect "
                        "immediately.")

        self._autostart_row = Adw.SwitchRow(
            title="Start on login",
            subtitle="Launch automatically at login, minimized to the tray")
        self._autostart_row.set_active(Settings.is_autostart_enabled())
        self._autostart_row.connect("notify::active", self._on_autostart_toggled)
        g.add(self._autostart_row)

        self._label_row = Adw.ComboRow(
            title="Tray temperature label",
            subtitle="Show live temperatures next to the tray icon")
        model = Gtk.StringList()
        for key in LABEL_CHOICES:
            model.append(LABEL_NAMES[key])
        self._label_row.set_model(model)
        self._label_row.set_selected(LABEL_CHOICES.index(self.settings.tray_label))
        self._label_row.connect("notify::selected", self._on_label_changed)
        g.add(self._label_row)

        page.add(g)

        # Automation (daemon-side): switch profile on AC/battery.
        self._auto_profiles: list = []   # index -> profile name (0 = No change)
        ga = Adw.PreferencesGroup(
            title="Automation",
            description="Automatically switch the thermal profile when you plug "
                        "in or unplug the charger.")
        self._auto_switch = Adw.SwitchRow(
            title="Switch profile on AC / battery")
        self._auto_switch.connect("notify::active", self._on_auto_switch)
        ga.add(self._auto_switch)
        self._auto_ac = Adw.ComboRow(title="When plugged in")
        self._auto_ac.connect("notify::selected", self._on_auto_ac)
        ga.add(self._auto_ac)
        self._auto_bat = Adw.ComboRow(title="On battery")
        self._auto_bat.connect("notify::selected", self._on_auto_bat)
        ga.add(self._auto_bat)
        self._auto_group = ga
        page.add(ga)

        info = Adw.PreferencesGroup(
            description="Closing the window keeps AWCC-Linux running in the "
                        "system tray. Use the tray menu or Quit to exit fully.")
        page.add(info)
        return page

    def _on_autostart_toggled(self, row, _param):
        Settings.set_autostart(row.get_active())

    # -- automation handlers ----------------------------------------------

    def _on_auto_switch(self, row, _p):
        if self._suppress:
            return
        self.backend.send({"cmd": "set_auto",
                           "auto": {"ac_enabled": row.get_active()}})
        self._auto_ac.set_sensitive(row.get_active())
        self._auto_bat.set_sensitive(row.get_active())

    def _auto_combo_value(self, idx):
        return None if idx == 0 else self._auto_profiles[idx]

    def _on_auto_ac(self, row, _p):
        if self._suppress:
            return
        self.backend.send({"cmd": "set_auto",
                           "auto": {"ac_profile": self._auto_combo_value(row.get_selected())}})

    def _on_auto_bat(self, row, _p):
        if self._suppress:
            return
        self.backend.send({"cmd": "set_auto",
                           "auto": {"battery_profile": self._auto_combo_value(row.get_selected())}})

    def _on_label_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(LABEL_CHOICES):
            self.settings.tray_label = LABEL_CHOICES[idx]
            self.tray.update()

    # -- event handlers (user -> daemon) -----------------------------------

    def _on_profile_clicked(self, btn, profile):
        if self._suppress or not btn.get_active():
            return
        self.backend.send({"cmd": CMD_SET_PROFILE, "profile": profile})

    def _on_mode_toggled(self, btn, mode):
        if self._suppress or not btn.get_active():
            return
        self.backend.send({"cmd": CMD_SET_MODE, "mode": mode})
        self._update_manual_sensitivity(mode)

    def _on_manual_changed(self, scale, group):
        if self._suppress:
            return
        self.backend.send({"cmd": CMD_SET_MANUAL, "group": group,
                           "boost_pct": scale.get_value()})

    def _on_curve_changed(self, editor, group):
        self.backend.send({"cmd": CMD_SET_CURVE, "group": group,
                           "points": editor.points})

    def _on_curve_reset(self, btn, group):
        default = [[30, 0], [45, 0], [55, 15], [65, 35], [75, 60], [85, 90], [90, 100]]
        self._editors[group].set_points(default)
        self.backend.send({"cmd": CMD_SET_CURVE, "group": group, "points": default})

    # -- state update (daemon -> UI) ---------------------------------------

    def _on_status(self, connected, message):
        if connected:
            self._banner.set_revealed(False)
        else:
            self._banner.set_title(
                f"Not connected to awccd — {message}. "
                "Start it with: systemctl start awccd")
            self._banner.set_revealed(True)
        return False

    def _on_state(self, state):
        self.state = state
        self._sync_profiles(state)
        self._sync_mode(state)
        self._sync_cards(state)
        self._sync_fans(state)
        self._sync_sensors(state)
        self._sync_curves(state)
        self._sync_power(state)
        self._sync_automation(state)
        self._sync_tray(state)
        return False

    def _sync_profiles(self, state):
        choices = state.get("profile_choices", [])
        cur = state.get("profile")
        # Guard the whole sync: building/grouping radio buttons or setting the
        # active one must never be mistaken for a user click that sends a command.
        self._suppress = True
        if not self._profile_buttons and choices:
            group_btn = None
            for prof in choices:
                name, _desc = PROFILE_INFO.get(prof, (prof.title(), ""))
                btn = Gtk.ToggleButton(label=name)
                btn.set_tooltip_text(PROFILE_INFO.get(prof, ("", ""))[1])
                if group_btn is None:
                    group_btn = btn
                else:
                    btn.set_group(group_btn)
                btn.connect("toggled", self._on_profile_clicked, prof)
                self._profile_buttons[prof] = btn
                self._profile_box.append(btn)
        # Reflect the actual hardware profile (orthogonal to fan mode).
        btn = self._profile_buttons.get(cur)
        if btn and not btn.get_active():
            btn.set_active(True)
        self._suppress = False

    def _sync_mode(self, state):
        mode = (state.get("config") or {}).get("mode", MODE_PROFILE)
        self._suppress = True
        for m, btn in self._mode_buttons.items():
            btn.set_active(m == mode)
        # manual scale values
        manual = (state.get("config") or {}).get("manual", {})
        for group, scale in self._manual_scales.items():
            v = manual.get(group, 0)
            if abs(scale.get_value() - v) > 0.5:
                scale.set_value(v)
        self._suppress = False
        self._update_manual_sensitivity(mode)

    def _update_manual_sensitivity(self, mode):
        self._manual_group.set_sensitive(mode == MODE_MANUAL)

    def _sync_cards(self, state):
        cpu = state.get("cpu_temp")
        gpu = state.get("gpu_temp")
        nv = state.get("nvidia") or {}
        self._cards["cpu"][0].set_text(f"{cpu:.0f}°" if cpu is not None else "—")
        self._cards["gpu"][0].set_text(f"{gpu:.0f}°" if gpu is not None else "—")
        self._cards["gutil"][0].set_text(
            f"{nv.get('util'):.0f}%" if nv.get("util") is not None else "—")
        self._cards["gpow"][0].set_text(
            f"{nv.get('power'):.0f}W" if nv.get("power") is not None else "—")
        tb = state.get("target_boost", {})
        self._cards["cpu"][1].set_text(f"target {tb.get('cpu', 0)}% boost")
        self._cards["gpu"][1].set_text(f"target {tb.get('gpu', 0)}% boost")
        if cpu is not None:
            self._graph.push("CPU", cpu)
        if gpu is not None:
            self._graph.push("GPU", gpu)

    def _sync_fans(self, state):
        for f in state.get("fans", []):
            idx = f["index"]
            row = self._fan_rows.get(idx)
            if row is None:
                row = Adw.ActionRow(title=f["label"])
                lbl = Gtk.Label()
                lbl.add_css_class("numeric")
                lbl.set_xalign(1.0)
                row.add_suffix(lbl)
                row._value_label = lbl  # type: ignore[attr-defined]
                self._fan_group.add(row)
                self._fan_rows[idx] = row
            pct = f["rpm"] / f["max_rpm"] * 100 if f["max_rpm"] else 0
            row._value_label.set_text(  # type: ignore[attr-defined]
                f"{f['rpm']} RPM  ·  {pct:.0f}% of max  ·  boost {f['boost_pct']}%")

    def _sync_sensors(self, state):
        for t in state.get("temps", []):
            key = t["label"]
            row = self._sensor_rows.get(key)
            if row is None:
                row = Adw.ActionRow(title=key, subtitle=t.get("source", ""))
                lbl = Gtk.Label()
                lbl.add_css_class("numeric")
                row.add_suffix(lbl)
                row._value_label = lbl  # type: ignore[attr-defined]
                self._sensors_group.add(row)
                self._sensor_rows[key] = row
            row._value_label.set_text(f"{t['temp']:.0f}°C")  # type: ignore[attr-defined]

    def _sync_curves(self, state):
        cfg = state.get("config") or {}
        curves = cfg.get("curves") or {}
        if not self._curves_loaded and curves:
            for group, editor in self._editors.items():
                if curves.get(group):
                    editor.set_points(curves[group])
            self._curves_loaded = True
        # Always update the live temp marker.
        self._editors["cpu"].set_current_temp(state.get("cpu_temp"))
        self._editors["gpu"].set_current_temp(state.get("gpu_temp"))

    def _sync_power(self, state):
        pw = state.get("power") or {}
        cfg = (state.get("config") or {}).get("power") or {}
        self._suppress = True

        # GPU power limit.
        gp = pw.get("gpu_power") or {}
        has_gpu = bool(state.get("has_nvidia") and gp.get("max"))
        settable = bool(pw.get("gpu_settable"))
        self._gpu_group.set_visible(has_gpu)
        if has_gpu and not self._gpu_range_set:
            self._gpu_scale.set_range(gp["min"], gp["max"])
            for m in {gp["min"], gp.get("default") or gp["min"], gp["max"]}:
                self._gpu_scale.add_mark(m, Gtk.PositionType.BOTTOM, f"{m}W")
            self._gpu_range_set = True
        self._gpu_override.set_sensitive(settable)
        if not settable:
            self._gpu_override.set_subtitle(
                "Locked by firmware / Dynamic Boost on this GPU")
            self._gpu_override.set_active(False)
            self._gpu_row.set_sensitive(False)
        else:
            self._gpu_override.set_subtitle("Off = firmware default")
            gov_ov = cfg.get("gpu_limit_w")
            self._gpu_override.set_active(gov_ov is not None)
            self._gpu_row.set_sensitive(gov_ov is not None)
            val = gov_ov if gov_ov is not None else (
                gp.get("limit") or gp.get("default") or gp.get("min"))
            if val is not None:
                self._gpu_scale.set_value(val)

        # EPP / governor combos (Auto + hardware choices).
        self._sync_combo(self._epp_row, "_epp_values", pw.get("epp_choices"),
                         cfg.get("cpu_epp"), f"currently: {pw.get('epp')}")
        self._sync_combo(self._gov_row, "_gov_values", pw.get("governor_choices"),
                         cfg.get("cpu_governor"), f"currently: {pw.get('governor')}")

        # CPU package power limits.
        cp = pw.get("cpu_power") or {}
        self._pl_group.set_visible(bool(cp.get("available")))
        pl1, pl2 = cfg.get("cpu_pl1_w"), cfg.get("cpu_pl2_w")
        pl_on = pl1 is not None or pl2 is not None
        self._pl_override.set_active(pl_on)
        self._pl1_row.set_sensitive(pl_on)
        self._pl2_row.set_sensitive(pl_on)
        if (pl1 or cp.get("pl1")) is not None:
            self._pl1_row.set_value(pl1 if pl1 is not None else cp.get("pl1"))
        if (pl2 or cp.get("pl2")) is not None:
            self._pl2_row.set_value(pl2 if pl2 is not None else cp.get("pl2"))
        self._suppress = False

    def _sync_combo(self, row, attr, choices, current, subtitle):
        """Populate a ComboRow with 'Auto' + choices and select `current`."""
        choices = list(choices or [])
        values = getattr(self, attr, [])
        if values[1:] != choices:
            values = [None] + choices
            setattr(self, attr, values)
            model = Gtk.StringList()
            model.append("Auto (profile)")
            for c in choices:
                model.append(c)
            row.set_model(model)
        row.set_selected(values.index(current) if current in values else 0)
        row.set_subtitle(subtitle)

    def _sync_automation(self, state):
        auto = (state.get("config") or {}).get("auto") or {}
        choices = list(state.get("profile_choices") or [])
        self._suppress = True
        if self._auto_profiles[1:] != choices:
            self._auto_profiles = [None] + choices
            for combo in (self._auto_ac, self._auto_bat):
                model = Gtk.StringList()
                model.append("No change")
                for c in choices:
                    model.append(PROFILE_INFO.get(c, (c.title(), ""))[0])
                combo.set_model(model)
        en = bool(auto.get("ac_enabled"))
        self._auto_switch.set_active(en)
        self._auto_ac.set_sensitive(en)
        self._auto_bat.set_sensitive(en)
        acp, bap = auto.get("ac_profile"), auto.get("battery_profile")
        self._auto_ac.set_selected(
            self._auto_profiles.index(acp) if acp in self._auto_profiles else 0)
        self._auto_bat.set_selected(
            self._auto_profiles.index(bap) if bap in self._auto_profiles else 0)
        self._suppress = False

    def _sync_tray(self, state):
        self.tray.update()
        prof = state.get("profile")
        mode = (state.get("config") or {}).get("mode")
        if prof != self._last_prof or mode != self._last_mode:
            self._last_prof, self._last_mode = prof, mode
            self.tray.notify_layout_changed()

    # -- tray-driven actions ----------------------------------------------

    def show_from_tray(self):
        self.set_visible(True)
        self.present()
        return False

    def quit_app(self):
        self._quitting = True
        self.tray.shutdown()
        self.backend.stop()
        app = self.get_application()
        if app:
            app.quit()
        return False

    def _on_close(self, *_a):
        # Minimize to the tray instead of quitting, when a tray host is present.
        if not self._quitting and getattr(self, "tray", None) and self.tray.available:
            self.set_visible(False)
            return True  # stop the default destroy
        self.backend.stop()
        return False


class AwccApp(Adw.Application):
    def __init__(self, start_hidden=False):
        super().__init__(application_id=APP_ID)
        self._win = None
        self._start_hidden = start_hidden

    def do_activate(self):
        first = self._win is None
        if first:
            self._win = MainWindow(self)
            from gi.repository import Gio
            about = Gio.SimpleAction.new("about", None)
            about.connect("activate", self._on_about)
            self.add_action(about)
            quit_act = Gio.SimpleAction.new("quit", None)
            quit_act.connect("activate", lambda *a: self._win.quit_app())
            self.add_action(quit_act)
        # On a --hidden autostart, stay in the tray: the window exists (so the
        # app keeps running and the tray is live) but is not shown.
        if first and self._start_hidden:
            self._start_hidden = False
            return
        # Any activation (including relaunching the desktop icon) restores it.
        self._win.show_from_tray()

    def _on_about(self, *_a):
        dlg = Adw.AboutWindow(
            transient_for=self._win,
            application_name="AWCC-Linux",
            application_icon="io.github.awcclinux.Awcc",
            version=__version__,
            developer_name="32bitcolor",
            comments="Alienware Command Center-style thermal & fan control for Linux.",
            website="https://github.com/32bitcolor/awcc-linux",
            license_type=Gtk.License.MIT_X11,
        )
        dlg.present()


def main():
    # Parse our own flags; don't forward them to GApplication (which would warn
    # about unknown options and try to treat them as files to open).
    start_hidden = "--hidden" in sys.argv[1:]
    app = AwccApp(start_hidden=start_hidden)
    return app.run([sys.argv[0]] if sys.argv else [])


if __name__ == "__main__":
    raise SystemExit(main())
