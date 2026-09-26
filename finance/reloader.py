"""Keep a server process running and on the latest code - the same idea as Flask's reloader.

    python -m finance.reloader --watch finance/gate.py requirements.txt -- python -m finance.gate ...

* A watched file changes      -> restart the process (within ~1s of saving).
* requirements.txt changes    -> install them first, then restart.
* The process exits/crashes   -> retry after a short pause, or as soon as a file changes
                                 (so a half-saved edit never leaves you with a dead window).

Deliberately imports nothing from the app: a broken edit to the watched code can't break the watcher.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

RETRY_AFTER_CRASH_S = 5


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Supervisor:
    def __init__(self, watch: list[Path], spawn: Callable[[], subprocess.Popen],
                 install: Callable[[], None] | None = None, clock: Callable[[], float] = time.monotonic):
        self.watch, self.spawn, self.install, self.clock = watch, spawn, install, clock
        self.seen = self._snapshot()
        self.child: subprocess.Popen | None = None
        self.crashed_at: float | None = None

    def _snapshot(self) -> dict[Path, float]:
        return {p: p.stat().st_mtime for p in self.watch if p.exists()}

    def _stop(self) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()

    def start(self) -> None:
        self.child, self.crashed_at = self.spawn(), None

    def tick(self) -> str | None:
        """One check. Returns what happened ('reloaded', 'crashed', 'retried') or None."""
        now = self._snapshot()
        if now != self.seen:
            changed = sorted(p.name for p in set(now) | set(self.seen) if now.get(p) != self.seen.get(p))
            self.seen = now
            if self.install and "requirements.txt" in changed:
                _log("requirements.txt changed - installing packages…")
                self.install()
            _log(f"{', '.join(changed)} changed - reloading")
            self._stop()
            self.start()
            return "reloaded"
        if self.child and self.child.poll() is not None:
            if self.crashed_at is None:
                self.crashed_at = self.clock()
                _log(f"stopped (exit code {self.child.returncode}) - retrying in {RETRY_AFTER_CRASH_S}s "
                     "or as soon as a file changes")
                return "crashed"
            if self.clock() - self.crashed_at >= RETRY_AFTER_CRASH_S:
                self.start()
                return "retried"
        return None

    def run(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(1)
                self.tick()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Restart a command when files change")
    ap.add_argument("--watch", nargs="+", required=True, type=Path)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("missing command after --")
    root = Path(__file__).resolve().parent.parent

    def install() -> None:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(root / "requirements.txt")])
        if r.returncode:
            _log("package install failed - see above; will keep running the current version")

    Supervisor(a.watch, lambda: subprocess.Popen(cmd), install).run()


if __name__ == "__main__":
    main()
