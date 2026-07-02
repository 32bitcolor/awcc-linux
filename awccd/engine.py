"""The control engine: the loop that turns config + temperatures into fan action.

Runs as root inside awccd.  A single background thread ticks every
`poll_interval` seconds:

  * reads a hardware snapshot,
  * applies the active mode (follow-profile / custom-curve / manual),
  * caches the resulting state for socket clients.

All hardware writes are serialised through `self._lock` so the control tick and
inbound client commands can never interleave a half-applied change.
"""

from __future__ import annotations

import threading
import time

from . import config as cfgmod
from . import protocol
from .hardware import Hardware


class ControlEngine:
    def __init__(self) -> None:
        self.hw = Hardware()
        self.cfg = cfgmod.Config()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._state: dict = {}
        self._state_lock = threading.Lock()
        # Hysteresis memory: last temp we evaluated the curve at, and the boost
        # we settled on, per group.  Prevents fan hunting on tiny temp wobble.
        self._last_eval_temp: dict[str, float] = {}
        self._last_boost: dict[str, float] = {}
        self._wake = threading.Event()  # nudge the loop after a command
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.apply_control(force=True)
        self._thread = threading.Thread(target=self._run, name="awccd-control",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # never let one bad tick kill the loop
                print(f"[awccd] control tick error: {exc}", flush=True)
            # Sleep, but wake early if a command nudged us.
            self._wake.wait(timeout=self.cfg.poll_interval)
            self._wake.clear()

    def tick(self) -> None:
        with self._lock:
            self.apply_control()
            snap = self.hw.snapshot()
        state = self._build_state(snap)
        with self._state_lock:
            self._state = state

    # -- control policy ----------------------------------------------------

    def apply_control(self, force: bool = False) -> None:
        """Enforce the current mode on the hardware. Caller holds self._lock."""
        mode = self.cfg.mode

        if mode == protocol.MODE_PROFILE:
            self.hw.set_profile(self.cfg.profile)
            # Release any lingering boost so firmware fully owns the fans.
            for g in protocol.GROUPS:
                self._set_group(g, 0.0, force)
            self._last_eval_temp.clear()
            self._last_boost.clear()
            return

        # manual + custom both require the "custom" platform profile for the
        # fanN_boost values to take effect.
        if "custom" in self.hw.get_profile_choices():
            self.hw.set_profile("custom")

        if mode == protocol.MODE_MANUAL:
            for g in protocol.GROUPS:
                self._set_group(g, self.cfg.manual(g), force)
            return

        if mode == protocol.MODE_CUSTOM:
            temps = {protocol.GROUP_CPU: self.hw.cpu_temp(),
                     protocol.GROUP_GPU: self.hw.gpu_temp()}
            for g in protocol.GROUPS:
                temp = temps[g]
                if temp is None:
                    continue
                last = self._last_eval_temp.get(g)
                if (not force and last is not None
                        and abs(temp - last) < self.cfg.hysteresis):
                    boost = self._last_boost.get(g, 0.0)
                else:
                    boost = cfgmod.eval_curve(self.cfg.curve(g), temp)
                    self._last_eval_temp[g] = temp
                    self._last_boost[g] = boost
                self._set_group(g, boost, force)

    def _set_group(self, group: str, pct: float, force: bool) -> None:
        # Skip redundant writes unless forced (reduces WMI chatter).
        if not force and abs(self.hw.get_group_boost_pct(group) - pct) < 1:
            return
        self.hw.set_group_boost(group, pct)

    # -- state for clients -------------------------------------------------

    def _build_state(self, snap: dict) -> dict:
        state = dict(snap)
        state["mode"] = self.cfg.mode
        state["config"] = {
            "mode": self.cfg.mode,
            "profile": self.cfg.profile,
            "poll_interval": self.cfg.poll_interval,
            "hysteresis_c": self.cfg.hysteresis,
            "manual": dict(self.cfg.data["manual"]),
            "curves": {g: self.cfg.curve(g) for g in protocol.GROUPS},
        }
        state["target_boost"] = {g: round(self._last_boost.get(g, 0.0))
                                 for g in protocol.GROUPS}
        state["ok"] = True
        return state

    def get_state(self) -> dict:
        with self._state_lock:
            if self._state:
                return dict(self._state)
        # Cold read before the first tick.
        with self._lock:
            snap = self.hw.snapshot()
        return self._build_state(snap)

    # -- command handling (called from socket handler threads) -------------

    def handle(self, req: dict) -> dict:
        cmd = req.get("cmd")
        try:
            with self._lock:
                if cmd == protocol.CMD_PING:
                    return {"ok": True, "pong": True}
                elif cmd == protocol.CMD_GET_STATE:
                    pass  # fall through to state return
                elif cmd == protocol.CMD_SET_MODE:
                    self.cfg.set_mode(req.get("mode"))
                    self.apply_control(force=True)
                elif cmd == protocol.CMD_SET_PROFILE:
                    prof = req.get("profile")
                    self.cfg.set_profile(prof)
                    # Selecting a profile implies follow-profile mode.
                    self.cfg.set_mode(protocol.MODE_PROFILE)
                    self.apply_control(force=True)
                elif cmd == protocol.CMD_SET_CURVE:
                    self.cfg.set_curve(req.get("group"), req.get("points"))
                    self.apply_control(force=True)
                elif cmd == protocol.CMD_SET_MANUAL:
                    self.cfg.set_manual(req.get("group"), req.get("boost_pct", 0))
                    if self.cfg.mode == protocol.MODE_MANUAL:
                        self.apply_control(force=True)
                elif cmd == protocol.CMD_SET_POLL:
                    self.cfg.set_poll(req.get("interval", 2.0))
                elif cmd == protocol.CMD_SUBSCRIBE:
                    return {"ok": True, "subscribe": True}
                else:
                    return {"ok": False, "error": f"unknown command: {cmd}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        # Refresh cached state immediately so the reply reflects the change.
        self._wake.set()
        return self.get_state()
