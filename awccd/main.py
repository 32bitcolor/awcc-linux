"""awccd entry point. Run as root (systemd service or `sudo python -m awccd.main`)."""

from __future__ import annotations

import os
import signal
import sys

# Allow running both as `python -m awccd.main` and `python awccd/main.py`.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from awccd.engine import ControlEngine
    from awccd import server as server_mod
else:
    from .engine import ControlEngine
    from . import server as server_mod


def main() -> int:
    if os.geteuid() != 0 and os.environ.get("AWCCD_DEV") != "1":
        print("awccd must run as root (it writes to /sys). Try: sudo python -m awccd.main",
              file=sys.stderr)
        print("(set AWCCD_DEV=1 to run unprivileged for testing — hardware writes "
              "will be no-ops)", file=sys.stderr)
        return 1

    engine = ControlEngine()
    if not engine.hw.fan_control_available:
        print("[awccd] WARNING: alienware-wmi fan boost not found — "
              "fan curves/manual boost will be inert on this machine.", flush=True)

    engine.start()
    srv = server_mod.serve(engine)
    print(f"[awccd] v{__import__('awccd').__version__} listening on "
          f"{srv.server_address}", flush=True)

    def shutdown(signum, _frame):
        print(f"[awccd] signal {signum}, shutting down", flush=True)
        engine.stop()
        srv.stop_event.set()
        srv.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        srv.serve_forever()
    finally:
        try:
            srv.server_close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
