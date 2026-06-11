"""Self-monitoring: a health snapshot + a watchdog that revives dead
always-on threads and alerts the owner on repeated failures.

JARVIS runs several always-on background threads (system monitor, Telegram
poll). If one dies on an unhandled exception, the feature silently stops.
The :class:`Watchdog` periodically checks them and calls their idempotent
``start()`` to revive — and, after repeated revivals of the same
subsystem, pushes a Telegram/notification alert so the owner knows
something is misbehaving (now that the notification path actually reaches
the phone).

:func:`collect_health` powers the ``/health`` endpoint: a single
"is everything OK" view across managers, their threads, and the DBs.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


def _thread_alive(mgr: Any, attr: str) -> bool | None:
    """True/False if the named thread exists, None if there's no thread
    (e.g. an on-demand poller that's simply idle — not a fault)."""
    if mgr is None:
        return None
    t = getattr(mgr, attr, None)
    if t is None:
        return None
    try:
        return bool(t.is_alive())
    except Exception:  # noqa: BLE001
        return None


def collect_health(app: Any) -> dict[str, Any]:
    """Aggregate subsystem health. A value of False = a fault (thread that
    should be running died); None = not running but that's expected;
    True = healthy."""
    st = app.state
    subs: dict[str, Any] = {}

    brain = getattr(st, "brain", None)
    subs["brain"] = brain is not None
    subs["memory"] = bool(
        brain is not None and getattr(brain, "memory", None) is not None
        and getattr(brain.memory, "long_term", None) is not None
        and brain.memory.long_term.available)

    sec = getattr(st, "security", None)
    if sec is not None:
        subs["security.system_monitor"] = _thread_alive(
            getattr(sec, "system", None), "_thread")
    comm = getattr(st, "communication", None)
    if comm is not None and getattr(comm, "telegram", None) is not None \
            and comm.telegram.configured:
        subs["telegram_poll"] = _thread_alive(comm.telegram, "_poll_thread")
    fin = getattr(st, "finance", None)
    if fin is not None and getattr(fin, "market", None) is not None:
        subs["finance.market_poll"] = _thread_alive(fin.market, "_thread")

    for name in ("security", "communication", "finance", "productivity",
                 "entertainment", "intelligence", "smarthome", "vision"):
        subs[name] = getattr(st, name, None) is not None

    healthy = all(v is not False for v in subs.values())
    return {"status": "healthy" if healthy else "degraded",
            "subsystems": subs, "ts": time.time()}


class Watchdog:
    """Revives dead always-on threads; alerts the owner on repeat failures."""

    def __init__(self, app: Any, alert: Callable[[str, str], None] | None = None,
                 interval_s: int = 60, alert_after: int = 3) -> None:
        self._app = app
        self._alert = alert
        self._interval = max(15, int(interval_s))
        self._alert_after = alert_after
        self._revivals: dict[str, int] = {}
        self._alerted: set[str] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-watchdog",
                                        daemon=True)
        self._thread.start()
        print(f"[Watchdog] active (every {self._interval}s)")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    # Subsystems with an idempotent start() we can call to revive a dead
    # always-on thread. (name, get_manager, thread_attr, restart)
    def _targets(self) -> list[tuple[str, Any, str, Callable[[], None]]]:
        st = self._app.state
        out: list[tuple[str, Any, str, Callable[[], None]]] = []
        sec = getattr(st, "security", None)
        if sec is not None and getattr(sec, "system", None) is not None:
            out.append(("system_monitor", sec.system, "_thread", sec.system.start))
        comm = getattr(st, "communication", None)
        if comm is not None and getattr(comm, "telegram", None) is not None \
                and comm.telegram.configured:
            tg = comm.telegram
            out.append(("telegram_poll", tg, "_poll_thread",
                        lambda _tg=tg, _c=comm: _tg.start_polling(
                            _c._on_telegram_message)))
        return out

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for name, mgr, attr, restart in self._targets():
                    if _thread_alive(mgr, attr) is False:  # existed but died
                        print(f"[Watchdog] {name} thread dead — restarting")
                        try:
                            restart()
                        except Exception as exc:  # noqa: BLE001
                            print(f"[Watchdog] {name} restart failed: {exc}")
                        n = self._revivals.get(name, 0) + 1
                        self._revivals[name] = n
                        if n >= self._alert_after and name not in self._alerted:
                            self._alerted.add(name)
                            self._fire_alert(name, n)
            except Exception as exc:  # noqa: BLE001
                print(f"[Watchdog] tick failed: {exc}")
            self._stop.wait(self._interval)

    def _fire_alert(self, name: str, count: int) -> None:
        msg = (f"Subsystem '{name}' ist {count}× ausgefallen und wurde neu "
               f"gestartet — bitte prüfen.")
        print(f"[Watchdog] ALERT: {msg}")
        if self._alert is not None:
            try:
                self._alert(msg, "high")
            except Exception as exc:  # noqa: BLE001
                print(f"[Watchdog] alert failed: {exc}")
