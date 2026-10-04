#!/usr/bin/env python
"""Start the whole demo: both mock apps, then the orchestrator + UI.

Everything is torn down and re-seeded first so the world is deterministic. The
apps cache open SQLite handles, which is why the reset restarts them rather than
just rewriting the files.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.config import get_settings  # noqa: E402
from common.logging_setup import setup_logging  # noqa: E402

APPS = [
    ("vendor_portal", "environments.vendor_portal.app:app", 8001),
    ("ap_system", "environments.ap_system.app:app", 8002),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true",
                        help="re-seed both databases first")
    parser.add_argument("--visible", action="store_true",
                        help="run the agent's browser with a window")
    parser.add_argument("--port", type=int, default=get_settings().orchestrator_port)
    args = parser.parse_args()
    setup_logging()

    if args.reset:
        # --no-restart: reset_env would otherwise start the apps itself, and we
        # are about to start them, which is a port collision and a confusing log.
        subprocess.run([sys.executable, str(ROOT / "scripts" / "reset_env.py"),
                        "--no-restart"], check=True, cwd=ROOT)

    children: list[subprocess.Popen] = []
    for name, target, port in APPS:
        children.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", target,
             "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
            cwd=ROOT))

    children.append(subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "orchestrator.app:app",
         "--host", "127.0.0.1", "--port", str(args.port), "--log-level", "warning"],
        cwd=ROOT))

    env_note = ("real Anthropic client" if os.environ.get("ANTHROPIC_API_KEY")
                else "ScriptedClient (no ANTHROPIC_API_KEY set)")
    print(f"\n  AI Worker  http://127.0.0.1:{args.port}")
    print(f"  model      {env_note}")
    print(f"  browser    {'headed' if args.visible else 'headless'}")

    def shutdown(*_: object) -> None:
        for child in children:
            child.send_signal(signal.SIGTERM)
        time.sleep(0.6)
        for child in children:
            if child.poll() is None:
                child.kill()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    try:
        return children[0].wait()
    except KeyboardInterrupt:
        shutdown()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())