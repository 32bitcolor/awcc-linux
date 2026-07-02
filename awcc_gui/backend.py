"""Bridges the GTK main loop to the awccd socket.

A background thread holds a `subscribe` stream and marshals each pushed state
dict onto the GTK main loop via GLib.idle_add. Commands are fired on short-lived
worker threads so the UI never blocks on socket I/O; their effects show up on the
next streamed state.
"""

from __future__ import annotations

import os
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from awcc_client import Client, DaemonUnavailable  # noqa: E402


class Backend:
    def __init__(self, on_state, on_status):
        # on_state(dict): called on the main loop with each fresh state.
        # on_status(bool, str): connection up/down + message.
        self._on_state = on_state
        self._on_status = on_status
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            client = Client(timeout=10.0)
            try:
                for state in client.subscribe():
                    if self._stop.is_set():
                        break
                    GLib.idle_add(self._on_state, state)
                    GLib.idle_add(self._on_status, True, "Connected")
            except DaemonUnavailable as exc:
                GLib.idle_add(self._on_status, False, str(exc))
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_status, False, f"{exc}")
            finally:
                client.close()
            if self._stop.is_set():
                break
            time.sleep(2.0)  # backoff before reconnecting

    def send(self, obj: dict) -> None:
        """Fire-and-forget command on a worker thread."""
        def worker():
            try:
                c = Client(timeout=5.0)
                c.request(obj)
                c.close()
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_status, False, f"command failed: {exc}")
        threading.Thread(target=worker, daemon=True).start()
