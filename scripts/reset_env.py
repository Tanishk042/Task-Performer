#!/usr/bin/env python
"""Put the two environment apps back to a known state.

Evals need this before every run, otherwise one failure cascades into the next
and a duplicate check passes for the wrong reason. The databases are rebuilt from
the seed, so the same reset always produces the same world.

The apps cache open SQLite handles, so this also restarts them: a reset the app
cannot see is worse than no reset at all.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import get_settings  # noqa: E402
from common.logging_setup import get_logger, setup_logging  # noqa: E402

log = get_logger("reset")

APPS = [
    ("vendor_portal", "environments.vendor_portal.app:app", 8001),
    ("ap_system", "environments.ap_system.app:app", 8002),
]


def port_pids(port: int) -> list[int]:
    result = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True)
    return [int(line) for line in result.stdout.split() if line.strip().isdigit()]


def stop_apps() -> None:
    for name, _module, port in APPS:
        pids = port_pids(port)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                log.info("stopped %s on :%s (pid %s)", name, port, pid)
            except ProcessLookupError:
                pass
    for _name, _module, port in APPS:
        for _ in range(40):
            if not port_pids(port):
                break
            time.sleep(0.1)


def start_apps() -> list[subprocess.Popen]:
    procs = []
    for name, module, port in APPS:
        log.info("starting %s on :%s", name, port)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
             "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        ))
    return procs


def wait_healthy(timeout: float = 20.0) -> bool:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    for _name, _module, port in APPS:
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=2) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        else:
            log.error("%s did not come up on :%s", _name, port)
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", type=int, default=None, help="seed override")
    parser.add_argument("--no-restart", action="store_true",
                        help="reset the files but leave the apps alone")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    setup_logging(args.log_level)

    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.seed

    from environments.ap_system import db as ap_db
    from environments.vendor_portal import db as portal_db

    if not args.no_restart:
        stop_apps()

    portal_stats = portal_db.bootstrap(settings.vendor_db_path, seed, reset=True)
    ap_stats = ap_db.bootstrap(settings.ap_db_path, seed, reset=True)
    log.info("vendor portal: %s", portal_stats)
    log.info("ap system:      %s", ap_stats)

    if args.no_restart:
        print(f"reset {settings.vendor_db_path} and {settings.ap_db_path} "
              f"(apps not restarted — they will not see this)")
        return 0

    start_apps()
    if not wait_healthy():
        print("reset the databases, but the apps did not come back up", file=sys.stderr)
        return 1
    print(f"reset to seed {seed}; vendor portal and AP are healthy again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())