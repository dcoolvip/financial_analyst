import os
import subprocess
import sys
import time

from finance import reloader
from finance.reloader import Supervisor


def child(code="import time; time.sleep(60)"):
    return subprocess.Popen([sys.executable, "-c", code])


def bump(p):
    t = time.time() + 5
    os.utime(p, (t, t))


def test_restarts_on_change(tmp_path):
    f = tmp_path / "gate.py"
    f.write_text("x")
    spawned = []
    s = Supervisor([f], lambda: spawned.append(child()) or spawned[-1])
    s.start()
    first = spawned[0]
    assert s.tick() is None
    bump(f)
    assert s.tick() == "reloaded"
    assert first.poll() is not None           # old process stopped
    assert len(spawned) == 2 and spawned[1].poll() is None
    s._stop()


def test_crash_retries_then_recovers_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr(reloader, "RETRY_AFTER_CRASH_S", 0)
    f = tmp_path / "gate.py"
    f.write_text("x")
    spawned = []
    s = Supervisor([f], lambda: spawned.append(child("raise SystemExit(1)")) or spawned[-1])
    s.start()
    spawned[0].wait()
    assert s.tick() == "crashed"              # reported once, window stays alive
    assert s.tick() == "retried"
    spawned[-1].wait()
    bump(f)
    assert s.tick() == "reloaded"             # a fix is picked up straight away


def test_requirements_change_installs_first(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("aiohttp")
    calls = []
    s = Supervisor([req], lambda: calls.append("spawn") or child(), install=lambda: calls.append("install"))
    s.start()
    bump(req)
    s.tick()
    assert calls == ["spawn", "install", "spawn"]
    s._stop()


def test_port_taken_by_another_copy_waits_quietly(tmp_path, capsys):
    """Regression: a second launcher's gate failed on 'Address already in use' and the reloader printed a traceback
    every 5 seconds forever. Now: one message, then quiet retries every minute."""
    from finance import reloader

    class Busy:
        returncode = reloader.PORT_BUSY
        def poll(self):
            return self.returncode
    t = [0.0]
    sup = reloader.Supervisor([], lambda: Busy(), clock=lambda: t[0])
    sup.start()
    assert sup.tick() == "busy"
    t[0] += 30
    assert sup.tick() is None                                  # not every 5 seconds
    t[0] += 31
    assert sup.tick() == "retried"
    assert sup.tick() == "busy"
    assert capsys.readouterr().out.count("port in use") == 1  # said once
