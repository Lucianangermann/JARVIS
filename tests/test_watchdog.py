"""Tests for self-monitoring: collect_health + Watchdog revival/alerting.
No real managers — a tiny fake app with controllable threads."""
from __future__ import annotations

import threading
import time

from server.common.watchdog import Watchdog, _thread_alive, collect_health


class _FakeState:
    pass


class _FakeApp:
    def __init__(self) -> None:
        self.state = _FakeState()


class _Monitor:
    """A manager with a restartable always-on thread."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.starts = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.starts += 1
        self._stop.clear()
        self._thread = threading.Thread(
            target=lambda: self._stop.wait(60), daemon=True)
        self._thread.start()

    def kill(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)


def test_thread_alive_states() -> None:
    m = _Monitor()
    assert _thread_alive(m, "_thread") is None      # no thread yet
    m.start()
    assert _thread_alive(m, "_thread") is True
    m.kill()
    assert _thread_alive(m, "_thread") is False


class _Brain:
    class memory:
        class long_term:
            available = True


def test_collect_health_flags_degraded() -> None:
    app = _FakeApp()

    class _Sec:
        pass
    sec = _Sec()
    sec.system = _Monitor()
    sec.system.start()
    app.state.security = sec
    app.state.brain = _Brain()

    # The monitor thread flag tracks liveness; a dead always-on thread makes
    # the overall status degraded. (Absent managers also count as not-present,
    # so we assert the specific flag transition here.)
    assert collect_health(app)["subsystems"]["security.system_monitor"] is True

    sec.system.kill()
    h2 = collect_health(app)
    assert h2["subsystems"]["security.system_monitor"] is False
    assert h2["status"] == "degraded"


def test_watchdog_revives_and_alerts() -> None:
    app = _FakeApp()

    class _Sec:
        pass
    sec = _Sec()
    sec.system = _Monitor()
    sec.system.start()
    app.state.security = sec

    alerts = []
    wd = Watchdog(app, alert=lambda msg, prio: alerts.append((prio, msg)),
                  interval_s=15, alert_after=2)

    # Drive the revival logic directly (no 60s wait): kill + run one pass.
    def one_pass():
        for name, mgr, attr, restart in wd._targets():
            if _thread_alive(mgr, attr) is False:
                restart()
                wd._revivals[name] = wd._revivals.get(name, 0) + 1
                if wd._revivals[name] >= wd._alert_after and name not in wd._alerted:
                    wd._alerted.add(name)
                    wd._fire_alert(name, wd._revivals[name])

    sec.system.kill()
    one_pass()
    assert sec.system._thread.is_alive()            # revived
    assert sec.system.starts == 2                    # initial + 1 revive
    assert alerts == []                              # below alert_after

    sec.system.kill()
    one_pass()                                       # 2nd failure → alert
    assert len(alerts) == 1 and alerts[0][0] == "high"
