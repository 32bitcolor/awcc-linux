"""AWCC-Linux GTK4 / libadwaita application."""

from __future__ import annotations

import os
import sys

import gi

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
        for mode, label in ((MODE_PROFILE, "Follow Profile"),
                            (MODE_CUSTOM, "Custom Curves"),
                            (MODE_MANUAL, "Manual")):
            btn = Gtk.ToggleButton(label=label)
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
        self._sync_tray(state)
        return False

    def _sync_profiles(self, state):
        choices = state.get("profile_choices", [])
        cur = state.get("profile")
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
        self._suppress = True
        # In profile mode the selected profile is the active one; in custom/manual
        # the daemon forces "custom", so reflect actual hardware profile.
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
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self._win = None

    def do_activate(self):
        if not self._win:
            self._win = MainWindow(self)
            from gi.repository import Gio
            about = Gio.SimpleAction.new("about", None)
            about.connect("activate", self._on_about)
            self.add_action(about)
            quit_act = Gio.SimpleAction.new("quit", None)
            quit_act.connect("activate", lambda *a: self._win.quit_app())
            self.add_action(quit_act)
        # Relaunching (e.g. clicking the desktop icon) restores from the tray.
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
    app = AwccApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
