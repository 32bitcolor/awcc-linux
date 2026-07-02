"""Shared constants and the JSON socket protocol used by awccd, the GUI and CLI.

The daemon (awccd) runs as root and owns all privileged sysfs writes.  Clients
(the GTK GUI and the CLI) talk to it over a Unix domain socket using
newline-delimited JSON.  This keeps the privileged surface tiny and auditable:
the only thing that ever writes to /sys is the daemon, and the only commands it
accepts are the ones enumerated here.
"""

from __future__ import annotations

import os

# --- Filesystem locations -------------------------------------------------
#
# All three are overridable via environment variables so the daemon can be run
# unprivileged for development/testing (AWCCD_RUN_DIR=/tmp/... etc). Production
# uses the defaults under /run and /var, owned by root.

# Runtime socket.  /run is a tmpfs; the daemon creates the directory, chowns the
# socket to group `wheel` and chmods 0660 so any wheel user can control the
# machine without a password after the one-time install.
RUN_DIR = os.environ.get("AWCCD_RUN_DIR", "/run/awcc")
SOCKET_PATH = os.environ.get("AWCCD_SOCKET", os.path.join(RUN_DIR, "awccd.sock"))

# Persistent configuration (fan curves, selected mode/profile).  Written by the
# daemon only; the GUI mutates it indirectly by sending commands.
STATE_DIR = os.environ.get("AWCCD_STATE_DIR", "/var/lib/awcc")
CONFIG_PATH = os.environ.get("AWCCD_CONFIG", os.path.join(STATE_DIR, "config.json"))

# Group that is granted control access to the socket.
CONTROL_GROUP = "wheel"

# --- Protocol -------------------------------------------------------------

PROTOCOL_VERSION = 1

# Operating modes for the fan subsystem.
MODE_PROFILE = "profile"   # Hand fans to firmware; just pick a platform_profile.
MODE_CUSTOM = "custom"     # Daemon runs temp->boost fan curves (platform_profile=custom).
MODE_MANUAL = "manual"     # Fixed user boost values (platform_profile=custom).
MODES = (MODE_PROFILE, MODE_CUSTOM, MODE_MANUAL)

# Fan groups exposed to clients.  The hardware has 4 fans; the alienware-wmi
# driver labels 1&2 as CPU and 3&4 as GPU, and pwmN_auto_channels_temp confirms
# channel 1 == CPU temp, channel 2 == GPU temp.  We therefore drive them as two
# logical groups.
GROUP_CPU = "cpu"
GROUP_GPU = "gpu"
GROUPS = (GROUP_CPU, GROUP_GPU)

# Commands (client -> daemon).  Every request is one JSON object with a "cmd".
CMD_GET_STATE = "get_state"        # -> full state snapshot
CMD_SUBSCRIBE = "subscribe"        # -> stream of state snapshots until disconnect
CMD_SET_MODE = "set_mode"          # {mode}
CMD_SET_PROFILE = "set_profile"    # {profile}
CMD_SET_CURVE = "set_curve"        # {group, points:[[temp,boost_pct],...]}
CMD_SET_MANUAL = "set_manual"      # {group, boost_pct}
CMD_SET_POLL = "set_poll"          # {interval}
CMD_SET_POWER = "set_power"        # {field, value}  (value None = unmanaged)
CMD_SET_AUTO = "set_auto"          # {auto: {partial rules}}
CMD_PING = "ping"

# Power override fields (None => let firmware/profile manage it).
POWER_FIELDS = ("gpu_limit_w", "cpu_epp", "cpu_governor", "cpu_pl1_w", "cpu_pl2_w")

# Default fan curves (temperature °C -> boost %).  boost % is a percentage of the
# raw 0-255 fanN_boost range.  These are conservative-but-responsive defaults
# roughly mirroring AWCC's "Balanced".
DEFAULT_CURVE = [
    [30, 0],
    [45, 0],
    [55, 15],
    [65, 35],
    [75, 60],
    [85, 90],
    [90, 100],
]

DEFAULT_CONFIG = {
    "version": PROTOCOL_VERSION,
    "mode": MODE_PROFILE,
    "profile": "balanced",
    "poll_interval": 2.0,
    "hysteresis_c": 2.0,          # only re-evaluate a curve after temp moves this much
    "manual": {GROUP_CPU: 0, GROUP_GPU: 0},
    "curves": {GROUP_CPU: list(DEFAULT_CURVE), GROUP_GPU: list(DEFAULT_CURVE)},
    # Power overrides — None means "don't manage; leave to firmware/profile".
    "power": {
        "gpu_limit_w": None,
        "cpu_epp": None,
        "cpu_governor": None,
        "cpu_pl1_w": None,
        "cpu_pl2_w": None,
    },
    # Auto-profiles: apply a profile/mode when AC power state changes.
    "auto": {
        "ac_enabled": False,
        "ac_profile": None,       # None => don't change that field on this event
        "ac_mode": None,
        "battery_profile": None,
        "battery_mode": None,
    },
}
