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
        self._last_ac: bool | None = None  # for AC-change auto-profile detection
        self._gpu_managed = False          # did we apply a GPU power override?
        self._pl_managed: dict[str, bool] = {}   # per-PL "we set an override"
        self._rapl_defaults: dict[str, int] = {}  # firmware RAPL limits at startup
        self._epp_managed = False
        self._gov_managed = False
        self._epp_default: str | None = None     # firmware EPP/governor at startup
        self._gov_default: str | None = None
        self._wake = threading.Event()  # nudge the loop after a command
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        # Capture firmware RAPL limits before applying any override, so releasing
        # a CPU power override can restore them.
        cp = self.hw.get_cpu_power()
        if cp.get("available"):
            self._rapl_defaults = {"pl1": cp.get("pl1"), "pl2": cp.get("pl2")}
        self._epp_default = self.hw.get_epp()
        self._gov_default = self.hw.get_governor()
        self._check_auto(force=True)
        self.apply_control(force=True)
        self.apply_power(force=True)
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
            self._check_auto()      # may change profile/mode when AC toggles
            self.apply_control()
            self.apply_power()
            snap = self.hw.snapshot()
        state = self._build_state(snap)
        with self._state_lock:
            self._state = state

    # -- control policy ----------------------------------------------------

    def apply_control(self, force: bool = False) -> None:
        """Enforce profile + fan mode on the hardware. Caller holds self._lock.

        Power profile and fan mode are orthogonal: we always keep the chosen
        platform_profile applied, then layer fan boost on top according to the
        mode. `fanN_boost` is additive and honoured regardless of profile
        (verified on Alienware m18 R1), so we never hijack the user's profile.

        Note: a few Alienware models only honour fanN_boost while the "custom"
        platform_profile is selected. On those, pick the "Custom" profile in
        addition to a fan mode. See README.
        """
        # Always enforce the chosen base power profile.
        self.hw.set_profile(self.cfg.profile)
        mode = self.cfg.mode

        if mode == protocol.MODE_PROFILE:
            # Release any lingering boost so firmware fully owns the fans.
            for g in protocol.GROUPS:
                self._set_group(g, 0.0, force)
            self._last_eval_temp.clear()
            self._last_boost.clear()
            return

        if mode == protocol.MODE_MANUAL:
            for g in protocol.GROUPS:
                boost = self.cfg.manual(g)
                self._last_boost[g] = boost
                self._set_group(g, boost, force)
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

    # -- power overrides ---------------------------------------------------

    def apply_power(self, force: bool = False) -> None:
        """Enforce the user's power overrides. Fields set to None are left to
        the firmware/platform_profile. Caller holds self._lock."""
        p = self.cfg.power

        gl = p.get("gpu_limit_w")
        if gl is not None and self.hw.gpu_power_settable():
            cur = (self.hw.get_gpu_power() or {}).get("limit")
            if force or cur != gl:
                self.hw.set_gpu_power_limit(gl)
            self._gpu_managed = True
        elif self._gpu_managed:
            # Override released: restore the firmware default limit once.
            gp = self.hw.get_gpu_power() or {}
            if gp.get("default"):
                self.hw.set_gpu_power_limit(gp["default"])
            self._gpu_managed = False

        epp = p.get("cpu_epp")
        if epp:
            if force or self.hw.get_epp() != epp:
                self.hw.set_epp(epp)
            self._epp_managed = True
        elif self._epp_managed:
            if self._epp_default and self.hw.get_epp() != self._epp_default:
                self.hw.set_epp(self._epp_default)
            self._epp_managed = False

        gov = p.get("cpu_governor")
        if gov:
            if force or self.hw.get_governor() != gov:
                self.hw.set_governor(gov)
            self._gov_managed = True
        elif self._gov_managed:
            if self._gov_default and self.hw.get_governor() != self._gov_default:
                self.hw.set_governor(self._gov_default)
            self._gov_managed = False

        need_pl = (p.get("cpu_pl1_w") or p.get("cpu_pl2_w")
                   or any(self._pl_managed.values()))
        cpu_pwr = self.hw.get_cpu_power() if need_pl else {}
        for which, field in (("pl1", "cpu_pl1_w"), ("pl2", "cpu_pl2_w")):
            w = p.get(field)
            if w is not None:
                if force or cpu_pwr.get(which) != w:
                    self.hw.set_cpu_power(which, w)
                self._pl_managed[which] = True
            elif self._pl_managed.get(which):
                # Override released: restore the firmware limit captured at start.
                dflt = self._rapl_defaults.get(which)
                if dflt and cpu_pwr.get(which) != dflt:
                    self.hw.set_cpu_power(which, dflt)
                self._pl_managed[which] = False

    # -- auto-profiles (AC / battery) -------------------------------------

    def _check_auto(self, force: bool = False) -> None:
        """When enabled, apply a profile/mode as the AC power state changes."""
        auto = self.cfg.auto
        if not auto.get("ac_enabled"):
            self._last_ac = None
            return
        ac = self.hw.ac_online()
        if ac is None:
            return
        if not force and ac == self._last_ac:
            return
        self._last_ac = ac
        prof = auto.get("ac_profile") if ac else auto.get("battery_profile")
        mode = auto.get("ac_mode") if ac else auto.get("battery_mode")
        if prof:
            self.cfg.set_profile(prof)
        if mode:
            self.cfg.set_mode(mode)

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
            "power": dict(self.cfg.power),
            "auto": dict(self.cfg.auto),
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
                    # Profile (power) is orthogonal to fan mode; only change the
                    # profile and re-apply. The active fan mode is preserved.
                    self.cfg.set_profile(req.get("profile"))
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
                elif cmd == protocol.CMD_SET_POWER:
                    self.cfg.set_power(req.get("field"), req.get("value"))
                    self.apply_power(force=True)
                elif cmd == protocol.CMD_SET_AUTO:
                    self.cfg.set_auto(req.get("auto") or {})
                    self._check_auto(force=True)
                    self.apply_control(force=True)
                elif cmd == protocol.CMD_SUBSCRIBE:
                    return {"ok": True, "subscribe": True}
                else:
                    return {"ok": False, "error": f"unknown command: {cmd}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        # Build a fresh state so the reply reflects the change immediately
        # (the cached state is from the previous tick), and refresh the cache.
        self._wake.set()
        with self._lock:
            snap = self.hw.snapshot()
        fresh = self._build_state(snap)
        with self._state_lock:
            self._state = fresh
        return fresh
