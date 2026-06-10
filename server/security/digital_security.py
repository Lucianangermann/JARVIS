"""Digital-perimeter monitoring: network, API usage, ports, certs, auth.

All probes are best-effort and time-bounded — a hung ``tailscale`` binary
or an unreachable HIBP endpoint must never stall the caller. Heavy or
blocking work (subprocess, socket, HTTP) runs under a short timeout and
degrades to a "konnte nicht prüfen" result rather than raising.

Network inventory is shared with ``system_monitor`` (the ARP read), but
the *judgement* — which devices are unknown, when to alert — lives here,
backed by the ``known_devices`` table.
"""
from __future__ import annotations

import socket
import ssl
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Any, Callable

from ..config import settings

try:
    import requests  # type: ignore[import-not-found]
    _REQUESTS_OK = True
except Exception:  # noqa: BLE001
    requests = None  # type: ignore[assignment]
    _REQUESTS_OK = False

AlertHandler = Callable[[str, str], None]

# Ports we expect to be listening on a dev MacBook + JARVIS server. Anything
# else listening is surfaced (not necessarily malicious — informational).
_EXPECTED_PORTS = {22, 80, 443, 5000, 8000, 8080, 3000, 53, 631, 5353}

# Auth-failure thresholds (spec §4).
_FAIL_ALERT = 3      # failures in 10 min → alert
_FAIL_BLOCK = 5      # failures → auto-block IP


class DigitalSecurityMonitor:
    """Network / API / port / cert / auth-log monitoring."""

    def __init__(
        self,
        db: Any = None,
        system_monitor: Any = None,
        alert_handler: AlertHandler | None = None,
    ) -> None:
        self._db = db
        self._sysmon = system_monitor
        self._alert = alert_handler
        # API-call timestamps for spike detection (filled by the brain via
        # record_api_call()).
        self._api_calls: deque[float] = deque(maxlen=4096)
        self._blocked_ips: set[str] = set()

    # ── network ────────────────────────────────────────────────────────── #

    async def check_network(self) -> dict[str, Any]:
        """List real LAN devices, flag untrusted ones. "Known" means
        *trusted* in known_devices — merely having been seen before does
        not make a device safe. We alert only on the FIRST sighting of an
        untrusted device, then record it, so repeat scans don't re-nag."""
        devices = [d for d in self._arp_devices() if self._is_real_host(d)]
        untrusted: list[dict[str, Any]] = []
        new_unknowns: list[dict[str, Any]] = []
        for d in devices:
            mac = d.get("mac")
            if not mac:
                continue
            seen_before = (
                self._db.is_known_device(mac) if self._db is not None else False
            )
            trusted = self._is_trusted(mac)
            if not trusted:
                untrusted.append(d)
            if self._db is not None:
                self._db.upsert_device(
                    mac_address=mac, hostname=d.get("hostname"),
                    ip_address=d.get("ip"), device_type=None, trusted=False,
                )
            # Record each first-seen untrusted device individually (audit),
            # but collect them so we speak ONE summary alert, not one per
            # device — a first scan of a real home would otherwise read out
            # a dozen MAC addresses aloud.
            if not trusted and not seen_before:
                new_unknowns.append(d)
                if self._db is not None:
                    self._db.log_event(
                        "unknown_device", "MEDIUM", "digital_security",
                        f"Unbekanntes Gerät im WLAN: {d.get('ip')} (MAC: {mac})",
                    )
        if new_unknowns:
            n = len(new_unknowns)
            self._fire(
                f"{n} {'neues unbekanntes Gerät' if n == 1 else 'neue unbekannte Geräte'} "
                f"im WLAN erkannt.", "MEDIUM",
            )
        return {
            "total": len(devices),
            "unknown": untrusted,
            "unknown_count": len(untrusted),
            "new_unknown_count": len(new_unknowns),
        }

    @staticmethod
    def _is_real_host(d: dict[str, Any]) -> bool:
        """Drop multicast / broadcast pseudo-entries from the ARP table."""
        mac = (d.get("mac") or "").lower()
        ip = d.get("ip") or ""
        if mac in ("ff:ff:ff:ff:ff:ff", ""):
            return False
        # IPv4 multicast MAC (01:00:5e…) / IPv6 multicast MAC (33:33…).
        if mac.startswith("01:00:5e") or mac.startswith("1:0:5e") \
                or mac.startswith("33:33"):
            return False
        # 224.0.0.0/4 multicast + 255.* broadcast.
        first = ip.split(".")[0] if "." in ip else ""
        if first.isdigit() and (224 <= int(first) <= 239 or first == "255"):
            return False
        return True

    def _is_trusted(self, mac: str) -> bool:
        if self._db is None:
            return False
        rows = self._db.query(
            "SELECT trusted FROM known_devices WHERE mac_address = ?", (mac,)
        )
        return bool(rows and rows[0]["trusted"])

    def _arp_devices(self) -> list[dict[str, Any]]:
        if self._sysmon is not None:
            try:
                return self._sysmon.get_network_devices()
            except Exception:  # noqa: BLE001
                pass
        # Local fallback.
        try:
            out = subprocess.run(["arp", "-an"], capture_output=True,
                                 text=True, timeout=5, check=False)
            devices = []
            for line in (out.stdout or "").splitlines():
                if " at " not in line or "(" not in line:
                    continue
                try:
                    ip = line.split("(")[1].split(")")[0]
                    mac = line.split(" at ")[1].split(" on ")[0].strip()
                    if mac in ("(incomplete)", ""):
                        continue
                    devices.append({"ip": ip, "mac": mac, "hostname": None})
                except (IndexError, ValueError):
                    continue
            return devices
        except Exception:  # noqa: BLE001
            return []

    # ── API usage ──────────────────────────────────────────────────────── #

    def record_api_call(self) -> None:
        """Called by the brain on each Anthropic API call."""
        self._api_calls.append(time.time())

    async def monitor_api_usage(self) -> dict[str, Any]:
        now = time.time()
        last_hour = sum(1 for t in self._api_calls if now - t <= 3600)
        spike = last_hour > settings.API_USAGE_ALERT_THRESHOLD
        if spike:
            msg = f"Ungewöhnlich hohe API Nutzung: {last_hour} Anfragen in 1 Stunde."
            if self._db is not None:
                self._db.log_event("api_spike", "HIGH", "digital_security", msg)
            self._fire(msg, "HIGH")
        return {"calls_last_hour": last_hour, "spike": spike,
                "threshold": settings.API_USAGE_ALERT_THRESHOLD}

    # ── data breaches (HaveIBeenPwned) ─────────────────────────────────── #

    async def check_data_breaches(self, email: str) -> dict[str, Any]:
        if not settings.HAVEIBEENPWNED_CHECK:
            return {"checked": False, "reason": "HAVEIBEENPWNED_CHECK disabled"}
        if not _REQUESTS_OK:
            return {"checked": False, "reason": "requests unavailable"}
        api_key = settings.__dict__.get("HIBP_API_KEY") or ""
        # The breached-account endpoint requires a paid key. Without one we
        # can't query by email — report that honestly rather than faking it.
        if not api_key:
            return {"checked": False,
                    "reason": "HIBP API key required for email lookup"}
        try:
            resp = requests.get(
                f"https://haveibeenpwned.com/api/v3/breachedaccount/{email}",
                headers={"hibp-api-key": api_key,
                         "user-agent": "JARVIS-Security"},
                params={"truncateResponse": "true"},
                timeout=8,
            )
            if resp.status_code == 404:
                return {"checked": True, "breaches": []}
            if resp.status_code != 200:
                return {"checked": False, "reason": f"HTTP {resp.status_code}"}
            breaches = [b.get("Name") for b in resp.json()]
            if breaches and self._db is not None:
                self._db.log_event(
                    "data_breach", "HIGH", "digital_security",
                    f"{email} in {len(breaches)} breaches: {', '.join(breaches)}",
                )
            return {"checked": True, "breaches": breaches}
        except Exception as exc:  # noqa: BLE001
            return {"checked": False, "reason": str(exc)}

    # ── tailscale ──────────────────────────────────────────────────────── #

    async def check_tailscale_status(self) -> dict[str, Any]:
        try:
            out = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=6, check=False,
            )
            if out.returncode != 0 or not out.stdout:
                return {"connected": False, "reason": "tailscale not running"}
            import json
            data = json.loads(out.stdout)
            backend = data.get("BackendState", "")
            peers = data.get("Peer", {}) or {}
            nodes = [
                {"host": p.get("HostName"), "online": p.get("Online")}
                for p in peers.values()
            ]
            return {
                "connected": backend == "Running",
                "backend_state": backend,
                "node_count": len(nodes),
                "nodes": nodes,
            }
        except FileNotFoundError:
            return {"connected": False, "reason": "tailscale not installed"}
        except Exception as exc:  # noqa: BLE001
            return {"connected": False, "reason": str(exc)}

    # ── open ports ─────────────────────────────────────────────────────── #

    async def scan_for_open_ports(self) -> dict[str, Any]:
        listening = self._listening_ports()
        unexpected = sorted(p for p in listening if p not in _EXPECTED_PORTS)
        # JARVIS server port exposed beyond loopback?
        exposed = self._jarvis_port_exposed()
        if exposed:
            # Bound to all interfaces (0.0.0.0) — reachable on the LAN /
            # Tailscale, which is the intended PWA setup, so this is an
            # informational MEDIUM, not a breach. A real internet exposure
            # depends on the router/firewall, which we can't see from here.
            msg = (f"JARVIS-Port {settings.PORT} ist an alle Netzwerk-"
                   f"Interfaces gebunden (nicht nur localhost).")
            if self._db is not None:
                self._db.log_event("port_exposed", "MEDIUM",
                                   "digital_security", msg)
            self._fire(msg, "MEDIUM")
        return {
            "listening": sorted(listening),
            "unexpected": unexpected,
            "jarvis_port_exposed": exposed,
        }

    def _listening_ports(self) -> set[int]:
        ports: set[int] = set()
        # psutil first (clean), fall back to lsof.
        try:
            import psutil  # type: ignore[import-not-found]
            for c in psutil.net_connections(kind="inet"):
                if c.status == "LISTEN" and c.laddr:
                    ports.add(c.laddr.port)
            if ports:
                return ports
        except Exception:  # noqa: BLE001 — AccessDenied on macOS w/o root
            pass
        try:
            out = subprocess.run(
                ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=6, check=False,
            )
            for line in (out.stdout or "").splitlines():
                if ":" in line:
                    try:
                        port = int(line.rsplit(":", 1)[1].split()[0])
                        ports.add(port)
                    except (ValueError, IndexError):
                        continue
        except Exception:  # noqa: BLE001
            pass
        return ports

    def _jarvis_port_exposed(self) -> bool:
        try:
            import psutil  # type: ignore[import-not-found]
            for c in psutil.net_connections(kind="inet"):
                if (c.status == "LISTEN" and c.laddr
                        and c.laddr.port == settings.PORT):
                    # Bound to 0.0.0.0 / :: = reachable off-box.
                    return c.laddr.ip in ("0.0.0.0", "::", "")
        except Exception:  # noqa: BLE001
            pass
        # Fall back to the configured HOST.
        return settings.HOST not in ("127.0.0.1", "localhost", "::1")

    # ── SSL certificates ───────────────────────────────────────────────── #

    async def check_ssl_certificates(self, domains: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for domain in domains:
            results.append(self._check_one_cert(domain))
        for r in results:
            if r.get("days_left") is not None and r["days_left"] <= 30:
                msg = (f"SSL-Zertifikat für {r['domain']} läuft in "
                       f"{r['days_left']} Tagen ab.")
                if self._db is not None:
                    self._db.log_event("ssl_expiry", "MEDIUM",
                                       "digital_security", msg)
                self._fire(msg, "MEDIUM")
        return results

    def _check_one_cert(self, domain: str) -> dict[str, Any]:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((domain, 443), timeout=6) as sock:
                with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                    cert = ssock.getpeercert()
            not_after = cert.get("notAfter")
            expiry = ssl.cert_time_to_seconds(not_after)
            days_left = int((expiry - time.time()) / 86400)
            return {"domain": domain, "days_left": days_left,
                    "expires": not_after}
        except Exception as exc:  # noqa: BLE001
            return {"domain": domain, "days_left": None, "error": str(exc)}

    # ── auth-log analysis ──────────────────────────────────────────────── #

    async def monitor_jarvis_auth_log(self) -> dict[str, Any]:
        """Scan recent failed access_log entries per IP; alert ≥3/10 min,
        auto-block ≥5."""
        if self._db is None:
            return {"failures": 0}
        since = time.time() - 600
        rows = self._db.query(
            "SELECT ip_address FROM access_log "
            "WHERE timestamp >= ? AND allowed = 0",
            (since,),
        )
        per_ip: dict[str, int] = defaultdict(int)
        for r in rows:
            per_ip[r["ip_address"] or "unknown"] += 1

        alerted: list[str] = []
        for ip, count in per_ip.items():
            if count >= _FAIL_BLOCK:
                self._blocked_ips.add(ip)
                msg = f"{count} fehlgeschlagene Login-Versuche von {ip} — IP blockiert."
                self._db.log_event("auth_block", "HIGH", "digital_security", msg)
                self._fire(msg, "HIGH")
                alerted.append(ip)
            elif count >= _FAIL_ALERT:
                msg = f"{count} fehlgeschlagene Login-Versuche von {ip} erkannt."
                self._db.log_event("auth_fail", "MEDIUM", "digital_security", msg)
                self._fire(msg, "MEDIUM")
                alerted.append(ip)
        return {
            "failures": len(rows),
            "by_ip": dict(per_ip),
            "alerted": alerted,
            "blocked_ips": sorted(self._blocked_ips),
        }

    def is_blocked(self, ip: str) -> bool:
        return ip in self._blocked_ips

    # ── daily report ───────────────────────────────────────────────────── #

    async def daily_security_report(self) -> str:
        net = await self.check_network()
        api = await self.monitor_api_usage()
        auth = await self.monitor_jarvis_auth_log()
        ts = await self.check_tailscale_status()

        issues: list[str] = []
        if net["unknown_count"]:
            issues.append(f"{net['unknown_count']} unbekannte Geräte im Netzwerk")
        if api["spike"]:
            issues.append("ungewöhnliche API-Nutzung")
        if auth["alerted"]:
            issues.append(f"fehlgeschlagene Logins von {len(auth['alerted'])} IPs")
        if not ts["connected"]:
            issues.append("Tailscale getrennt")

        head = (f"Netzwerk: {net['total']} Geräte, {net['unknown_count']} unbekannt. "
                f"API: {api['calls_last_hour']} Anfragen letzte Stunde. "
                f"Auth: {auth['failures']} fehlgeschlagene Versuche. "
                f"Tailscale: {'verbunden' if ts['connected'] else 'getrennt'}.")
        if not issues:
            return head + " Kein Sicherheitsproblem erkannt."
        return head + " Achtung: " + "; ".join(issues) + "."

    # ── helpers ────────────────────────────────────────────────────────── #

    def _fire(self, message: str, severity: str) -> None:
        print(f"[DigitalSecurity] {severity}: {message}")
        if self._alert is not None:
            try:
                self._alert(message, severity)
            except Exception as exc:  # noqa: BLE001
                print(f"[DigitalSecurity] alert failed: {exc}")
