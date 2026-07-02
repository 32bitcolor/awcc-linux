"""Hardware abstraction over sysfs / nvidia-smi for Alienware/Dell laptops.

Everything that reads or writes hardware state lives here.  hwmon numbers are
*not* stable across reboots, so we always locate devices by their `name`
attribute rather than a fixed hwmonN path.

Discovered/verified on an Alienware m18 R1 (kernel 6.19, mainline
alienware-wmi):

  hwmon `alienware_wmi`  -> fan{1..4}_input/_label/_min/_max, fan{1..4}_boost (RW),
                           temp1(CPU)/temp2(GPU).  fanN_boost is 0-255 and is
                           honoured while platform_profile == "custom".
  hwmon `dell_smm`       -> fan RPMs + pwmN (RW) direct control (advanced/fallback).
  hwmon `dell_ddv`       -> extra labelled temps (Ambient, SODIMM, Video, ...).
  hwmon `coretemp`       -> per-core + package CPU temps.
  /sys/firmware/acpi/platform_profile (RW) + _choices.
  nvidia-smi             -> discrete GPU temp/util/power.
"""

from __future__ import annotations

import glob
import os
import subprocess
import time
from dataclasses import dataclass, field

HWMON_ROOT = "/sys/class/hwmon"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"
PLATFORM_PROFILE_CHOICES = "/sys/firmware/acpi/platform_profile_choices"

# Power / performance interfaces.
CPU_BASE = "/sys/devices/system/cpu"
RAPL_ROOT = "/sys/class/powercap"
AC_ONLINE = "/sys/class/power_supply/AC/online"
# RAPL constraint index -> logical name (verified: 0=long-term/PL1, 1=short/PL2).
RAPL_PL = {"pl1": 0, "pl2": 1}

# Raw fanN_boost range exposed by alienware-wmi.
BOOST_RAW_MAX = 255


def _read(path: str) -> str | None:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return None


def _read_int(path: str) -> int | None:
    v = _read(path)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def _write(path: str, value) -> bool:
    """Write to a sysfs attribute.  Returns True on success.

    Only the root daemon calls this.  Failures are swallowed and reported via
    the return value so a single bad attribute never takes the loop down.
    """
    try:
        with open(path, "w") as fh:
            fh.write(str(value))
        return True
    except OSError:
        return False


def pct_to_raw(pct: float) -> int:
    pct = max(0.0, min(100.0, float(pct)))
    return round(pct / 100.0 * BOOST_RAW_MAX)


def raw_to_pct(raw: int) -> int:
    return round(max(0, min(BOOST_RAW_MAX, int(raw))) / BOOST_RAW_MAX * 100)


@dataclass
class HwmonDev:
    path: str
    name: str
    attrs: set[str] = field(default_factory=set)

    def p(self, attr: str) -> str:
        return os.path.join(self.path, attr)

    def has(self, attr: str) -> bool:
        return attr in self.attrs


def _discover_hwmon() -> dict[str, list[HwmonDev]]:
    """Map hwmon `name` -> list of devices (some names appear more than once)."""
    out: dict[str, list[HwmonDev]] = {}
    for d in sorted(glob.glob(os.path.join(HWMON_ROOT, "hwmon*"))):
        name = _read(os.path.join(d, "name"))
        if not name:
            continue
        try:
            attrs = set(os.listdir(d))
        except OSError:
            attrs = set()
        out.setdefault(name, []).append(HwmonDev(path=d, name=name, attrs=attrs))
    return out


class Hardware:
    """Locates control/sensor devices and exposes read/apply helpers."""

    def __init__(self) -> None:
        self.refresh_devices()

    # -- discovery ---------------------------------------------------------

    def refresh_devices(self) -> None:
        devs = _discover_hwmon()
        self.aw = devs.get("alienware_wmi", [None])[0]      # primary fan control
        self.dell_smm = devs.get("dell_smm", [None])[0]      # pwm fallback
        self.dell_ddv = devs.get("dell_ddv", [None])[0]      # labelled temps
        self.coretemp = devs.get("coretemp", [None])[0]
        self._all = devs
        self.has_nvidia = self._detect_nvidia()
        self._nv_cache: dict | None = None
        self._nv_cache_ts = 0.0
        self._gpu_pwr_cache: dict | None = None
        self._gpu_pwr_ts = 0.0
        self._gpu_settable: bool | None = None  # probed lazily

        # Map logical fan groups -> alienware fan indices (1-based, per _label).
        self.group_fans: dict[str, list[int]] = {"cpu": [], "gpu": []}
        if self.aw:
            for i in range(1, 5):
                lbl = (_read(self.aw.p(f"fan{i}_label")) or "").lower()
                if "cpu" in lbl:
                    self.group_fans["cpu"].append(i)
                elif "gpu" in lbl or "video" in lbl:
                    self.group_fans["gpu"].append(i)
            # Fallback if labels were empty: 1,2 -> cpu; 3,4 -> gpu.
            if not any(self.group_fans.values()):
                self.group_fans = {"cpu": [1, 2], "gpu": [3, 4]}

    @staticmethod
    def _detect_nvidia() -> bool:
        from shutil import which
        return which("nvidia-smi") is not None

    @property
    def fan_control_available(self) -> bool:
        return self.aw is not None and self.aw.has("fan1_boost")

    # -- platform profile --------------------------------------------------

    def get_profile(self) -> str | None:
        return _read(PLATFORM_PROFILE)

    def get_profile_choices(self) -> list[str]:
        raw = _read(PLATFORM_PROFILE_CHOICES)
        return raw.split() if raw else []

    def set_profile(self, profile: str) -> bool:
        if profile not in self.get_profile_choices():
            return False
        if self.get_profile() == profile:
            return True
        return _write(PLATFORM_PROFILE, profile)

    # -- fan boost (alienware-wmi, additive, safe) -------------------------

    def get_fan_boost(self, index: int) -> int | None:
        if not self.aw:
            return None
        return _read_int(self.aw.p(f"fan{index}_boost"))

    def set_fan_boost(self, index: int, raw: int) -> bool:
        if not self.aw:
            return False
        raw = max(0, min(BOOST_RAW_MAX, int(raw)))
        return _write(self.aw.p(f"fan{index}_boost"), raw)

    def set_group_boost(self, group: str, pct: float) -> bool:
        raw = pct_to_raw(pct)
        ok = True
        for idx in self.group_fans.get(group, []):
            ok = self.set_fan_boost(idx, raw) and ok
        return ok

    def get_group_boost_pct(self, group: str) -> int:
        vals = [self.get_fan_boost(i) for i in self.group_fans.get(group, [])]
        vals = [v for v in vals if v is not None]
        return raw_to_pct(max(vals)) if vals else 0

    # -- sensor snapshot ---------------------------------------------------

    def read_fans(self) -> list[dict]:
        """All fans from alienware-wmi with RPM + boost + limits."""
        fans: list[dict] = []
        if not self.aw:
            return fans
        for i in range(1, 5):
            if not self.aw.has(f"fan{i}_input"):
                continue
            rpm = _read_int(self.aw.p(f"fan{i}_input"))
            label = _read(self.aw.p(f"fan{i}_label")) or f"Fan {i}"
            fmax = _read_int(self.aw.p(f"fan{i}_max")) or 0
            boost = _read_int(self.aw.p(f"fan{i}_boost"))
            group = next((g for g, idxs in self.group_fans.items() if i in idxs), None)
            fans.append({
                "index": i,
                "label": f"{label} {i}",
                "rpm": rpm or 0,
                "max_rpm": fmax,
                "boost_pct": raw_to_pct(boost) if boost is not None else 0,
                "group": group,
            })
        return fans

    def read_temps(self) -> list[dict]:
        """Curated set of temperatures, most useful first."""
        temps: list[dict] = []

        def add(label, value, source, crit=None):
            if value is not None:
                temps.append({"label": label, "temp": round(value, 1),
                              "source": source, "crit": crit})

        # CPU package (coretemp temp1) is the definitive CPU reading.
        if self.coretemp:
            pkg = _read_int(self.coretemp.p("temp1_input"))
            crit = _read_int(self.coretemp.p("temp1_crit"))
            add("CPU Package", pkg / 1000 if pkg is not None else None, "coretemp",
                crit / 1000 if crit else None)

        # NVIDIA discrete GPU.
        g = self.read_nvidia()
        if g and g.get("temp") is not None:
            add("GPU (NVIDIA)", g["temp"], "nvidia-smi", 90)

        # Alienware-reported CPU/GPU (EC view).
        if self.aw:
            for i in (1, 2):
                lbl = _read(self.aw.p(f"temp{i}_label"))
                val = _read_int(self.aw.p(f"temp{i}_input"))
                if lbl and val is not None:
                    add(f"{lbl} (EC)", val / 1000, "alienware_wmi")

        # Rich Dell DDV temps (Ambient, SODIMM, Video, ...).
        if self.dell_ddv:
            seen: dict[str, int] = {}
            for i in range(1, 12):
                lbl = _read(self.dell_ddv.p(f"temp{i}_label"))
                val = _read_int(self.dell_ddv.p(f"temp{i}_input"))
                if not lbl or val is None:
                    continue
                seen[lbl] = seen.get(lbl, 0) + 1
                name = lbl if seen[lbl] == 1 else f"{lbl} {seen[lbl]}"
                add(name, val / 1000, "dell_ddv")

        # Storage.
        for dev in self._all.get("nvme", []):
            val = _read_int(os.path.join(dev.path, "temp1_input"))
            add("NVMe", val / 1000 if val is not None else None, "nvme")

        return temps

    def cpu_temp(self) -> float | None:
        if self.coretemp:
            v = _read_int(self.coretemp.p("temp1_input"))
            if v is not None:
                return v / 1000
        if self.aw:
            v = _read_int(self.aw.p("temp1_input"))
            if v is not None:
                return v / 1000
        return None

    def gpu_temp(self) -> float | None:
        g = self.read_nvidia()
        if g and g.get("temp") is not None:
            return g["temp"]
        if self.aw:
            v = _read_int(self.aw.p("temp2_input"))
            if v is not None:
                return v / 1000
        return None

    # -- nvidia ------------------------------------------------------------

    def read_nvidia(self) -> dict | None:
        if not self.has_nvidia:
            return None
        # nvidia-smi is comparatively expensive and read_nvidia() is hit several
        # times per snapshot; cache for ~1s to collapse those into one call.
        now = time.monotonic()
        if self._nv_cache is not None and (now - self._nv_cache_ts) < 1.0:
            return self._nv_cache
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=temperature.gpu,utilization.gpu,power.draw,clocks.gr",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            if out.returncode != 0 or not out.stdout.strip():
                return None
            first = out.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]

            def num(x):
                try:
                    return float(x)
                except ValueError:
                    return None
            result = {
                "temp": num(parts[0]) if len(parts) > 0 else None,
                "util": num(parts[1]) if len(parts) > 1 else None,
                "power": num(parts[2]) if len(parts) > 2 else None,
                "clock": num(parts[3]) if len(parts) > 3 else None,
            }
            self._nv_cache = result
            self._nv_cache_ts = now
            return result
        except (subprocess.SubprocessError, OSError):
            return None

    # -- power / performance ----------------------------------------------

    def ac_online(self) -> bool | None:
        v = _read_int(AC_ONLINE)
        return None if v is None else bool(v)

    # CPU energy-performance preference (intel_pstate active mode).
    def _epp_paths(self) -> list[str]:
        return glob.glob(os.path.join(
            CPU_BASE, "cpu[0-9]*", "cpufreq", "energy_performance_preference"))

    def get_epp(self) -> str | None:
        p = os.path.join(CPU_BASE, "cpu0", "cpufreq",
                         "energy_performance_preference")
        return _read(p)

    def epp_choices(self) -> list[str]:
        raw = _read(os.path.join(
            CPU_BASE, "cpu0", "cpufreq",
            "energy_performance_available_preferences"))
        return raw.split() if raw else []

    def set_epp(self, value: str) -> bool:
        if value not in self.epp_choices():
            return False
        ok = True
        for p in self._epp_paths():
            ok = _write(p, value) and ok
        return ok

    # CPU frequency governor.
    def get_governor(self) -> str | None:
        return _read(os.path.join(CPU_BASE, "cpu0", "cpufreq", "scaling_governor"))

    def governor_choices(self) -> list[str]:
        raw = _read(os.path.join(
            CPU_BASE, "cpu0", "cpufreq", "scaling_available_governors"))
        return raw.split() if raw else []

    def set_governor(self, value: str) -> bool:
        if value not in self.governor_choices():
            return False
        ok = True
        for p in glob.glob(os.path.join(CPU_BASE, "cpu[0-9]*", "cpufreq",
                                        "scaling_governor")):
            ok = _write(p, value) and ok
        return ok

    # Intel RAPL package power limits (watts).
    def _rapl_pkg(self) -> str | None:
        for d in sorted(glob.glob(os.path.join(RAPL_ROOT, "intel-rapl:[0-9]*"))):
            if _read(os.path.join(d, "name")) == "package-0":
                return d
        return None

    def get_cpu_power(self) -> dict:
        d = self._rapl_pkg()
        out: dict = {"available": d is not None}
        if not d:
            return out
        for key, idx in RAPL_PL.items():
            uw = _read_int(os.path.join(d, f"constraint_{idx}_power_limit_uw"))
            mx = _read_int(os.path.join(d, f"constraint_{idx}_max_power_uw"))
            out[key] = round(uw / 1_000_000) if uw else None
            out[f"{key}_max"] = round(mx / 1_000_000) if mx else None
        return out

    def set_cpu_power(self, which: str, watts: float) -> bool:
        d = self._rapl_pkg()
        if not d or which not in RAPL_PL:
            return False
        uw = int(max(1, watts) * 1_000_000)
        return _write(os.path.join(d, f"constraint_{RAPL_PL[which]}_power_limit_uw"), uw)

    # NVIDIA GPU power limit (watts) via nvidia-smi.
    def get_gpu_power(self) -> dict | None:
        if not self.has_nvidia:
            return None
        now = time.monotonic()
        if self._gpu_pwr_cache is not None and (now - self._gpu_pwr_ts) < 1.5:
            return self._gpu_pwr_cache
        # The enforced limit is N/A in the CSV query on this driver, so parse the
        # verbose POWER block for the "Current/Default/Min/Max Power Limit" lines.
        try:
            out = subprocess.run(["nvidia-smi", "-q", "-d", "POWER"],
                                 capture_output=True, text=True, timeout=4)
            if out.returncode != 0:
                return None
        except (subprocess.SubprocessError, OSError):
            return None

        def first(label):
            for line in out.stdout.splitlines():
                if label in line:
                    val = line.split(":", 1)[-1].strip().split()[0]
                    try:
                        return round(float(val))
                    except (ValueError, IndexError):
                        continue
            return None
        result = {
            "limit": first("Current Power Limit"),
            "default": first("Default Power Limit"),
            "min": first("Min Power Limit"),
            "max": first("Max Power Limit"),
        }
        self._gpu_pwr_cache = result
        self._gpu_pwr_ts = now
        return result

    def _pl_write(self, watts: int):
        """Run nvidia-smi -pl and classify the result. Returns (ok, unsupported)."""
        try:
            out = subprocess.run(["nvidia-smi", "-pl", str(int(watts))],
                                 capture_output=True, text=True, timeout=8)
        except (subprocess.SubprocessError, OSError):
            return False, False
        txt = (out.stdout + out.stderr).lower()
        # nvidia-smi prints "not supported in current scope" and still exits 0 on
        # laptop GPUs whose limit is locked by firmware/Dynamic Boost.
        unsupported = "not supported" in txt
        ok = out.returncode == 0 and not unsupported and "insufficient" not in txt
        return ok, unsupported

    def gpu_power_settable(self) -> bool:
        """Whether the GPU power limit can actually be changed (probed once by
        writing the current value, which is a no-op when it succeeds)."""
        if self._gpu_settable is not None:
            return self._gpu_settable
        if not self.has_nvidia:
            self._gpu_settable = False
            return False
        gp = self.get_gpu_power()
        if not gp or not gp.get("limit"):
            self._gpu_settable = False
            return False
        ok, unsupported = self._pl_write(gp["limit"])
        self._gpu_settable = ok and not unsupported
        return self._gpu_settable

    def set_gpu_power_limit(self, watts: float) -> bool:
        if not self.has_nvidia:
            return False
        ok, unsupported = self._pl_write(int(watts))
        if unsupported:
            self._gpu_settable = False
        self._gpu_pwr_cache = None  # force re-read next time
        return ok

    def power_snapshot(self) -> dict:
        return {
            "ac_online": self.ac_online(),
            "epp": self.get_epp(),
            "epp_choices": self.epp_choices(),
            "governor": self.get_governor(),
            "governor_choices": self.governor_choices(),
            "cpu_power": self.get_cpu_power(),
            "gpu_power": self.get_gpu_power(),
            "gpu_settable": self.gpu_power_settable() if self.has_nvidia else False,
        }

    # -- summary for clients ----------------------------------------------

    def snapshot(self) -> dict:
        return {
            "profile": self.get_profile(),
            "profile_choices": self.get_profile_choices(),
            "fan_control_available": self.fan_control_available,
            "has_nvidia": self.has_nvidia,
            "temps": self.read_temps(),
            "fans": self.read_fans(),
            "nvidia": self.read_nvidia(),
            "cpu_temp": self.cpu_temp(),
            "gpu_temp": self.gpu_temp(),
            "group_fans": self.group_fans,
            "power": self.power_snapshot(),
        }
