"""Live multi-series line graph (temperature history) as a Gtk.DrawingArea."""

from __future__ import annotations

from collections import deque

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class HistoryGraph(Gtk.DrawingArea):
    __gtype_name__ = "AwccHistoryGraph"

    def __init__(self, capacity: int = 120, y_max: float = 100.0):
        super().__init__()
        self.capacity = capacity
        self.y_max = y_max
        # series name -> (deque, rgb)
        self.series: dict[str, tuple[deque, tuple]] = {}
        self.set_hexpand(True)
        self.set_content_height(160)
        self.set_draw_func(self._draw)

    def add_series(self, name: str, rgb: tuple) -> None:
        self.series[name] = (deque(maxlen=self.capacity), rgb)

    def push(self, name: str, value) -> None:
        if name not in self.series:
            self.add_series(name, (0.5, 0.5, 0.5))
        if value is not None:
            self.series[name][0].append(float(value))
        self.queue_draw()

    def _draw(self, area, cr, width, height):
        ml, mt, mr, mb = 34, 10, 10, 18
        pw = max(1, width - ml - mr)
        ph = max(1, height - mt - mb)

        cr.set_source_rgba(1, 1, 1, 0.03)
        cr.rectangle(ml, mt, pw, ph)
        cr.fill()

        cr.select_font_face("Sans", 0, 0)
        cr.set_font_size(9)
        for v in range(0, int(self.y_max) + 1, 25):
            y = mt + (1 - v / self.y_max) * ph
            cr.set_source_rgba(1, 1, 1, 0.07)
            cr.move_to(ml, y)
            cr.line_to(ml + pw, y)
            cr.stroke()
            cr.set_source_rgba(1, 1, 1, 0.4)
            cr.move_to(4, y + 3)
            cr.show_text(f"{v}")

        for name, (data, rgb) in self.series.items():
            if len(data) < 2:
                continue
            n = len(data)
            step = pw / max(1, self.capacity - 1)
            x0 = ml + (self.capacity - n) * step
            cr.set_line_width(2)
            cr.set_source_rgba(*rgb, 0.95)
            for i, val in enumerate(data):
                x = x0 + i * step
                y = mt + (1 - min(val, self.y_max) / self.y_max) * ph
                if i == 0:
                    cr.move_to(x, y)
                else:
                    cr.line_to(x, y)
            cr.stroke()

        # Legend.
        lx = ml + 6
        cr.set_font_size(10)
        for name, (data, rgb) in self.series.items():
            cur = data[-1] if data else None
            label = f"{name} {cur:.0f}°" if cur is not None else name
            cr.set_source_rgba(*rgb, 1.0)
            cr.rectangle(lx, mt + 4, 9, 9)
            cr.fill()
            cr.move_to(lx + 13, mt + 12)
            cr.show_text(label)
            lx += 34 + cr.text_extents(label).width
