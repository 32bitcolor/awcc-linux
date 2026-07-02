"""Interactive fan-curve editor widget (temperature -> fan boost %).

A Gtk.DrawingArea with draggable control points:
  * drag a point to move it (temp clamped between its neighbours),
  * double-click empty space to add a point,
  * right-click a point to remove it (minimum two points).

Emits `changed` (a GObject signal) after any edit that commits, carrying the new
point list. A live marker shows the current temperature and where it lands on
the curve.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GObject, Gtk  # noqa: E402

TMIN, TMAX = 20.0, 100.0   # x-axis temperature range (°C)
BMIN, BMAX = 0.0, 100.0    # y-axis boost range (%)
HIT_RADIUS = 14.0          # px; how close a click must be to grab a point
MIN_POINTS = 2


class FanCurveEditor(Gtk.DrawingArea):
    __gtype_name__ = "AwccFanCurveEditor"

    __gsignals__ = {
        # Emitted when the curve changes and should be persisted.
        "changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, accent=(0.20, 0.55, 0.95)):
        super().__init__()
        self.points: list[list[float]] = [[40, 0], [60, 30], [80, 70], [90, 100]]
        self.current_temp: float | None = None
        self.accent = accent
        self._drag_idx: int | None = None
        self._margin = (44, 16, 30, 26)  # left, top, right, bottom

        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_content_height(220)
        self.set_draw_func(self._draw)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.set_button(0)  # any button
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

    # -- public API --------------------------------------------------------

    def set_points(self, points) -> None:
        pts = [[float(t), float(b)] for t, b in points] if points else []
        pts.sort(key=lambda p: p[0])
        if len(pts) >= MIN_POINTS:
            self.points = pts
            self.queue_draw()

    def set_current_temp(self, temp) -> None:
        self.current_temp = temp
        self.queue_draw()

    # -- geometry helpers --------------------------------------------------

    def _plot_rect(self):
        ml, mt, mr, mb = self._margin
        w = self.get_width()
        h = self.get_height()
        return ml, mt, max(1, w - ml - mr), max(1, h - mt - mb)

    def _tx(self, temp):
        ml, _, pw, _ = self._plot_rect()
        return ml + (temp - TMIN) / (TMAX - TMIN) * pw

    def _ty(self, boost):
        _, mt, _, ph = self._plot_rect()
        return mt + (1 - boost / BMAX) * ph

    def _inv_x(self, px):
        ml, _, pw, _ = self._plot_rect()
        return TMIN + (px - ml) / pw * (TMAX - TMIN)

    def _inv_y(self, py):
        _, mt, _, ph = self._plot_rect()
        return BMAX * (1 - (py - mt) / ph)

    def _eval(self, temp):
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        if temp >= pts[-1][0]:
            return pts[-1][1]
        for (t0, b0), (t1, b1) in zip(pts, pts[1:]):
            if t0 <= temp <= t1:
                if t1 == t0:
                    return b1
                return b0 + (temp - t0) / (t1 - t0) * (b1 - b0)
        return pts[-1][1]

    def _nearest(self, px, py):
        best, bd = None, HIT_RADIUS ** 2
        for i, (t, b) in enumerate(self.points):
            dx = self._tx(t) - px
            dy = self._ty(b) - py
            d = dx * dx + dy * dy
            if d <= bd:
                best, bd = i, d
        return best

    # -- interaction -------------------------------------------------------

    def _on_drag_begin(self, gesture, x, y):
        self._drag_idx = self._nearest(x, y)

    def _on_drag_update(self, gesture, ox, oy):
        if self._drag_idx is None:
            return
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        i = self._drag_idx
        new_t = self._inv_x(sx + ox)
        new_b = max(BMIN, min(BMAX, self._inv_y(sy + oy)))
        lo = self.points[i - 1][0] + 1 if i > 0 else TMIN
        hi = self.points[i + 1][0] - 1 if i < len(self.points) - 1 else TMAX
        new_t = max(lo, min(hi, new_t))
        self.points[i] = [round(new_t), round(new_b)]
        self.queue_draw()

    def _on_drag_end(self, gesture, ox, oy):
        if self._drag_idx is not None:
            self._drag_idx = None
            self.emit("changed")

    def _on_pressed(self, gesture, n_press, x, y):
        button = gesture.get_current_button()
        idx = self._nearest(x, y)
        if button == 3 and idx is not None:  # right-click removes
            if len(self.points) > MIN_POINTS:
                del self.points[idx]
                self.queue_draw()
                self.emit("changed")
            return
        if button == 1 and n_press == 2 and idx is None:  # double-click adds
            t = round(max(TMIN, min(TMAX, self._inv_x(x))))
            b = round(max(BMIN, min(BMAX, self._inv_y(y))))
            self.points.append([t, b])
            self.points.sort(key=lambda p: p[0])
            self.queue_draw()
            self.emit("changed")

    # -- drawing -----------------------------------------------------------

    def _draw(self, area, cr, width, height):
        ml, mt, pw, ph = self._plot_rect()
        ar, ag, ab = self.accent

        # Panel background.
        cr.set_source_rgba(1, 1, 1, 0.03)
        cr.rectangle(ml, mt, pw, ph)
        cr.fill()

        # Grid + axis labels.
        cr.set_line_width(1)
        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(10)
        for b in range(0, 101, 25):
            y = self._ty(b)
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.move_to(ml, y)
            cr.line_to(ml + pw, y)
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(6, y + 3)
            cr.show_text(f"{b}%")
        for t in range(int(TMIN), int(TMAX) + 1, 20):
            x = self._tx(t)
            cr.set_source_rgba(1, 1, 1, 0.08)
            cr.move_to(x, mt)
            cr.line_to(x, mt + ph)
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.45)
            cr.move_to(x - 8, mt + ph + 16)
            cr.show_text(f"{t}°")

        # Filled area under the curve.
        cr.move_to(self._tx(self.points[0][0]), self._ty(0))
        for t, b in self.points:
            cr.line_to(self._tx(t), self._ty(b))
        cr.line_to(self._tx(self.points[-1][0]), self._ty(0))
        cr.close_path()
        cr.set_source_rgba(ar, ag, ab, 0.18)
        cr.fill()

        # Curve line.
        cr.set_line_width(2.5)
        cr.set_source_rgba(ar, ag, ab, 1.0)
        cr.move_to(self._tx(self.points[0][0]), self._ty(self.points[0][1]))
        for t, b in self.points[1:]:
            cr.line_to(self._tx(t), self._ty(b))
        cr.stroke()

        # Current-temperature marker.
        if self.current_temp is not None:
            ct = max(TMIN, min(TMAX, self.current_temp))
            cb = self._eval(self.current_temp)
            x = self._tx(ct)
            cr.set_line_width(1.5)
            cr.set_source_rgba(1, 1, 1, 0.30)
            cr.move_to(x, mt)
            cr.line_to(x, mt + ph)
            cr.stroke()
            cr.arc(x, self._ty(cb), 4.5, 0, 6.2832)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.85)
            cr.move_to(min(x + 6, ml + pw - 60), mt + 12)
            # Use ASCII "->": cairo's toy-font path renders U+2192 as tofu.
            cr.show_text(f"{self.current_temp:.0f}° -> {cb:.0f}%")

        # Control points.
        for t, b in self.points:
            x, y = self._tx(t), self._ty(b)
            cr.arc(x, y, 5.5, 0, 6.2832)
            cr.set_source_rgba(ar, ag, ab, 1.0)
            cr.fill()
            cr.arc(x, y, 5.5, 0, 6.2832)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.set_line_width(1.5)
            cr.stroke()
