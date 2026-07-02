"""Unix-socket JSON server exposing the ControlEngine to unprivileged clients.

Protocol: newline-delimited JSON. One request object per line; the daemon
replies with one JSON object per line. A `subscribe` request switches the
connection into a push stream: the daemon emits a fresh state snapshot every
`poll_interval` seconds until the client disconnects.

Security model: the socket lives at /run/awcc/awccd.sock, owned root:wheel with
mode 0660. Membership in `wheel` is the authorisation boundary — the same group
that can already `sudo`. No network exposure.
"""

from __future__ import annotations

import grp
import json
import os
import socket
import socketserver
import threading

from . import protocol
from .engine import ControlEngine


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        engine: ControlEngine = self.server.engine  # type: ignore[attr-defined]
        for raw in self.rfile:
            line = raw.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                self._send({"ok": False, "error": "invalid JSON"})
                continue
            if not isinstance(req, dict):
                self._send({"ok": False, "error": "expected object"})
                continue

            resp = engine.handle(req)
            self._send(resp)

            # Enter streaming mode on subscribe.
            if req.get("cmd") == protocol.CMD_SUBSCRIBE:
                self._stream(engine)
                return

    def _stream(self, engine: ControlEngine) -> None:
        stop = getattr(self.server, "stop_event", None)
        while stop is None or not stop.is_set():
            if not self._send(engine.get_state()):
                return
            interval = engine.cfg.poll_interval
            if stop is not None and stop.wait(timeout=interval):
                return
            elif stop is None:
                import time
                time.sleep(interval)

    def _send(self, obj: dict) -> bool:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, OSError):
            return False


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, engine: ControlEngine):
        # Fresh socket each start.
        os.makedirs(protocol.RUN_DIR, exist_ok=True)
        if os.path.exists(protocol.SOCKET_PATH):
            os.unlink(protocol.SOCKET_PATH)
        super().__init__(protocol.SOCKET_PATH, _Handler)
        self.engine = engine
        self.stop_event = threading.Event()
        self._set_perms()

    def _set_perms(self) -> None:
        if os.geteuid() != 0:
            # Dev/unprivileged run: leave default perms, can't chown.
            return
        try:
            gid = grp.getgrnam(protocol.CONTROL_GROUP).gr_gid
        except KeyError:
            gid = -1
        try:
            os.chown(protocol.SOCKET_PATH, 0, gid)
            os.chmod(protocol.SOCKET_PATH, 0o660)
            os.chmod(protocol.RUN_DIR, 0o755)
        except OSError as exc:
            print(f"[awccd] warning: could not set socket perms: {exc}", flush=True)


def serve(engine: ControlEngine) -> _Server:
    return _Server(engine)
