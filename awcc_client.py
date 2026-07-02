"""Thin client for talking to awccd over its Unix socket.

Shared by the CLI (awcc-cli) and the GTK GUI. Two usage styles:

  * request/response — `Client.request({...})` for one-shot commands;
  * streaming — `Client.subscribe()` yields state dicts as the daemon pushes them.

Import path is kept flat (single module, no package) so it works whether the
files are run from a git checkout or from /opt/awcc after install.
"""

from __future__ import annotations

import json
import os
import socket

# Keep these in sync with awccd/protocol.py. Duplicated deliberately so the
# client has zero import dependency on the daemon package. AWCCD_SOCKET allows
# pointing at a dev daemon (must match the daemon's own override).
SOCKET_PATH = os.environ.get(
    "AWCCD_SOCKET",
    os.path.join(os.environ.get("AWCCD_RUN_DIR", "/run/awcc"), "awccd.sock"),
)

CMD_GET_STATE = "get_state"
CMD_SUBSCRIBE = "subscribe"
CMD_SET_MODE = "set_mode"
CMD_SET_PROFILE = "set_profile"
CMD_SET_CURVE = "set_curve"
CMD_SET_MANUAL = "set_manual"
CMD_SET_POLL = "set_poll"
CMD_PING = "ping"


class DaemonUnavailable(Exception):
    pass


class Client:
    def __init__(self, path: str = SOCKET_PATH, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._rfile = None

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            s.connect(self.path)
        except (FileNotFoundError, ConnectionRefusedError, PermissionError, OSError) as exc:
            raise DaemonUnavailable(
                f"cannot reach awccd at {self.path}: {exc}") from exc
        self._sock = s
        self._rfile = s.makefile("r")

    def close(self) -> None:
        try:
            if self._rfile:
                self._rfile.close()
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        self._rfile = self._sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- io ----------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        assert self._sock is not None
        self._sock.sendall((json.dumps(obj) + "\n").encode())

    def _recv(self) -> dict:
        assert self._rfile is not None
        line = self._rfile.readline()
        if not line:
            raise DaemonUnavailable("daemon closed the connection")
        return json.loads(line)

    def request(self, obj: dict) -> dict:
        """One-shot: (re)connect if needed, send, read one reply."""
        if self._sock is None:
            self.connect()
        self._send(obj)
        return self._recv()

    def subscribe(self):
        """Generator yielding state dicts pushed by the daemon. Blocks."""
        if self._sock is None:
            self.connect()
        self._send({"cmd": CMD_SUBSCRIBE})
        # First reply is the ack, then the state stream.
        ack = self._recv()
        if not ack.get("ok"):
            raise DaemonUnavailable(ack.get("error", "subscribe rejected"))
        while True:
            yield self._recv()

    # -- convenience one-shots --------------------------------------------

    def get_state(self) -> dict:
        return self.request({"cmd": CMD_GET_STATE})

    def set_mode(self, mode: str) -> dict:
        return self.request({"cmd": CMD_SET_MODE, "mode": mode})

    def set_profile(self, profile: str) -> dict:
        return self.request({"cmd": CMD_SET_PROFILE, "profile": profile})

    def set_curve(self, group: str, points) -> dict:
        return self.request({"cmd": CMD_SET_CURVE, "group": group, "points": points})

    def set_manual(self, group: str, boost_pct: float) -> dict:
        return self.request({"cmd": CMD_SET_MANUAL, "group": group,
                             "boost_pct": boost_pct})

    def ping(self) -> bool:
        try:
            return bool(self.request({"cmd": CMD_PING}).get("pong"))
        except (DaemonUnavailable, OSError):
            return False


def is_available(path: str = SOCKET_PATH) -> bool:
    try:
        c = Client(path, timeout=1.0)
        ok = c.ping()
        c.close()
        return ok
    except Exception:
        return False
