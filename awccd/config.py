"""Persistent daemon configuration and fan-curve evaluation."""

from __future__ import annotations

import copy
import json
import os
import tempfile

from . import protocol


def _clamp_points(points) -> list[list[float]]:
    """Sanitise a curve: pairs of [temp(0-110), boost_pct(0-100)], sorted by temp."""
    cleaned: list[list[float]] = []
    for p in points or []:
        try:
            t = max(0.0, min(110.0, float(p[0])))
            b = max(0.0, min(100.0, float(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
        cleaned.append([t, b])
    cleaned.sort(key=lambda pt: pt[0])
    if not cleaned:
        cleaned = [[c[0], c[1]] for c in protocol.DEFAULT_CURVE]
    return cleaned


def eval_curve(points: list[list[float]], temp: float) -> float:
    """Linear-interpolate boost % for a temperature. Flat beyond the endpoints."""
    if not points:
        return 0.0
    if temp <= points[0][0]:
        return points[0][1]
    if temp >= points[-1][0]:
        return points[-1][1]
    for (t0, b0), (t1, b1) in zip(points, points[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return b1
            frac = (temp - t0) / (t1 - t0)
            return b0 + frac * (b1 - b0)
    return points[-1][1]


class Config:
    def __init__(self, path: str = protocol.CONFIG_PATH):
        self.path = path
        self.data = copy.deepcopy(protocol.DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path) as fh:
                loaded = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return
        # Merge shallowly onto defaults so new keys survive upgrades.
        merged = copy.deepcopy(protocol.DEFAULT_CONFIG)
        for k, v in loaded.items():
            if k in merged:
                merged[k] = v
        merged["curves"] = {
            protocol.GROUP_CPU: _clamp_points(
                (loaded.get("curves") or {}).get(protocol.GROUP_CPU)),
            protocol.GROUP_GPU: _clamp_points(
                (loaded.get("curves") or {}).get(protocol.GROUP_GPU)),
        }
        if merged.get("mode") not in protocol.MODES:
            merged["mode"] = protocol.MODE_PROFILE
        self.data = merged

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)  # atomic
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- typed accessors ---------------------------------------------------

    @property
    def mode(self) -> str:
        return self.data["mode"]

    @property
    def profile(self) -> str:
        return self.data["profile"]

    @property
    def poll_interval(self) -> float:
        return float(self.data.get("poll_interval", 2.0))

    @property
    def hysteresis(self) -> float:
        return float(self.data.get("hysteresis_c", 2.0))

    def curve(self, group: str) -> list[list[float]]:
        return self.data["curves"][group]

    def manual(self, group: str) -> float:
        return float(self.data["manual"].get(group, 0))

    # -- mutations (return the applied value) ------------------------------

    def set_mode(self, mode: str) -> str:
        if mode in protocol.MODES:
            self.data["mode"] = mode
            self.save()
        return self.data["mode"]

    def set_profile(self, profile: str) -> str:
        self.data["profile"] = profile
        self.save()
        return profile

    def set_curve(self, group: str, points) -> list[list[float]]:
        if group in protocol.GROUPS:
            self.data["curves"][group] = _clamp_points(points)
            self.save()
        return self.data["curves"].get(group, [])

    def set_manual(self, group: str, pct: float) -> float:
        if group in protocol.GROUPS:
            self.data["manual"][group] = max(0.0, min(100.0, float(pct)))
            self.save()
        return self.data["manual"].get(group, 0)

    def set_poll(self, interval: float) -> float:
        self.data["poll_interval"] = max(0.5, min(10.0, float(interval)))
        self.save()
        return self.data["poll_interval"]
