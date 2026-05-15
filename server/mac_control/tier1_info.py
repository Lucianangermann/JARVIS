"""Tier 1 — read-only system info, always permitted.

Every function in this module reads state without changing anything.
No confirmation, no unlock, no password. The dispatcher executes Tier 1
actions inline.

Registered actions
------------------
    get_time              local clock
    get_date              today's date
    get_battery           % charged, charging state, time-remaining
    get_wifi              SSID of the active Wi-Fi
    get_volume            output volume (0–100, "muted" flag)
    read_clipboard        pbpaste — first MAX_CLIPBOARD_CHARS chars
    get_weather           open-meteo current conditions for a city name

Implementation notes
--------------------
- Shell-outs use a fixed argv (no shell=True, no string interpolation).
- Weather uses ``urllib`` so no extra dependency.
- All errors are returned as plain strings so the brain can speak them
  back; raising would bubble into a generic "Sorry, something went wrong".
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import ssl
import subprocess
import urllib.parse
import urllib.request

from . import action_logger, permission_manager
from .permission_manager import Tier

MAX_CLIPBOARD_CHARS = 500
_HTTP_TIMEOUT_S = 5.0

# Python on macOS ships without the system trust store linked, so default
# urllib calls hit SSL_VERIFY_FAILED. ``certifi`` is already a transitive
# dep of ``anthropic``; if it's absent fall back to the platform default.
try:
    import certifi  # type: ignore[import-not-found]
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    _SSL_CTX = ssl.create_default_context()

_DE_WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag",
                "Freitag", "Samstag", "Sonntag")
_DE_MONTHS = ("", "Januar", "Februar", "März", "April", "Mai", "Juni",
              "Juli", "August", "September", "Oktober", "November", "Dezember")


# --- shell helper ---------------------------------------------------------- #

def _run(*argv: str, timeout: float = 5.0) -> str:
    """Run a fixed-argv subprocess and return trimmed stdout. Errors return
    a "(error: …)" string so they end up in the spoken reply rather than
    crashing the dispatcher."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return f"(error: {exc})"
    if proc.returncode != 0:
        return f"(error: {proc.stderr.strip() or proc.returncode})"
    return proc.stdout.strip()


# --- actions --------------------------------------------------------------- #

def _get_time(**_params: object) -> str:
    return _dt.datetime.now().strftime("Es ist %H:%M.")


def _get_date(**_params: object) -> str:
    n = _dt.datetime.now()
    wd = _DE_WEEKDAYS[n.weekday()]
    mo = _DE_MONTHS[n.month]
    return f"Heute ist {wd}, der {n.day}. {mo} {n.year}."


def _get_battery(**_params: object) -> str:
    out = _run("/usr/bin/pmset", "-g", "batt")
    # Typical line:
    # -InternalBattery-0 (id=…) 87%; discharging; 4:32 remaining present: true
    pct_m = re.search(r"(\d+)%", out)
    state_m = re.search(r"%;\s*(charging|discharging|charged|AC attached|finishing charge)", out)
    pct = pct_m.group(1) if pct_m else "?"
    state = state_m.group(1) if state_m else "unknown"
    # Suppress the bogus "0:00 remaining" pmset prints while charged.
    rem = ""
    if state != "charged":
        rem_m = re.search(r"(\d+:\d{2})\s+remaining", out)
        if rem_m and rem_m.group(1) != "0:00":
            rem = f", noch {rem_m.group(1)}"
    state_de = {"charging": "lädt", "discharging": "entlädt",
                "charged": "voll geladen", "AC attached": "am Netz",
                "finishing charge": "fast voll"}.get(state, state)
    return f"Akku bei {pct}%, {state_de}{rem}."


def _get_wifi(**_params: object) -> str:
    # Try built-in airport tool first (deprecated in recent macOS but
    # still present); fall back to networksetup which is always there.
    out = _run("/usr/sbin/networksetup", "-getairportnetwork", "en0")
    # Output forms:
    #   "Current Wi-Fi Network: <ssid>"
    #   "You are not associated with an AirPort network."
    if "Current Wi-Fi Network:" in out:
        return out.split(":", 1)[1].strip()
    return "Kein WLAN verbunden."


def _get_volume(**_params: object) -> str:
    vol = _run("/usr/bin/osascript", "-e",
               "output volume of (get volume settings)")
    muted = _run("/usr/bin/osascript", "-e",
                 "output muted of (get volume settings)")
    if not vol.isdigit():
        # macOS returns "missing value" when the active output device
        # doesn't expose volume via Core Audio (HDMI, some Bluetooth
        # headphones, AirPlay receivers). The user can still adjust
        # volume on the device itself.
        return ("Lautstärke nicht lesbar — das aktive Audio-Gerät meldet "
                "den Wert nicht (z. B. externer Lautsprecher).")
    flag = "stumm" if muted.strip().lower() == "true" else "an"
    return f"Lautstärke {vol} ({flag})."


def _list_allowed_apps(**_params: object) -> str:
    from . import allowlist as _al
    apps = _al.list_all()
    if not apps:
        return "Keine Apps in der Allowlist."
    return "Erlaubte Apps: " + ", ".join(apps)


def _read_clipboard(**_params: object) -> str:
    txt = _run("/usr/bin/pbpaste")
    if not txt:
        return "Zwischenablage ist leer."
    if len(txt) > MAX_CLIPBOARD_CHARS:
        return txt[:MAX_CLIPBOARD_CHARS] + " …(gekürzt)"
    return txt


# --- weather (open-meteo, no API key) -------------------------------------- #

_WMO_CODE_DE: dict[int, str] = {
    0: "klar", 1: "überwiegend klar", 2: "teils bewölkt", 3: "bedeckt",
    45: "neblig", 48: "Reifnebel",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    66: "gefrierender Regen", 67: "starker gefrierender Regen",
    71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall",
    77: "Schneegriesel",
    80: "leichte Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer",
    85: "leichte Schneeschauer", 86: "starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit Hagel", 99: "starkes Gewitter mit Hagel",
}


def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_weather(*, city: str = "Berlin", **_params: object) -> str:
    """Current conditions for a city via open-meteo (no API key)."""
    if not isinstance(city, str) or not city.strip():
        return "Bitte einen Ortsnamen angeben."
    city = city.strip()[:80]
    try:
        geo = _http_json(
            "https://geocoding-api.open-meteo.com/v1/search?"
            + urllib.parse.urlencode({"name": city, "count": 1, "language": "de"})
        )
        results = geo.get("results") or []
        if not results:
            return f"Ort {city!r} nicht gefunden."
        loc = results[0]
        lat, lon = loc["latitude"], loc["longitude"]
        name = f"{loc.get('name', city)}"

        w = _http_json(
            "https://api.open-meteo.com/v1/forecast?"
            + urllib.parse.urlencode({
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,weather_code,wind_speed_10m",
                "timezone": "auto",
            })
        )
        cur = w.get("current") or {}
        temp = cur.get("temperature_2m")
        code = int(cur.get("weather_code") or -1)
        wind = cur.get("wind_speed_10m")
        cond = _WMO_CODE_DE.get(code, f"Wettercode {code}")
        wind_str = f", Wind {wind} km/h" if wind is not None else ""
        return f"{name}: {temp}°C, {cond}{wind_str}."
    except Exception as exc:  # noqa: BLE001 — surface to user
        return f"Wetter konnte nicht abgerufen werden ({exc})."


# --- registry -------------------------------------------------------------- #

# Every Tier 1 action: (name, handler, summary).
_TIER1: tuple[tuple[str, callable, callable], ...] = (
    ("get_time",          _get_time,          lambda **_: "Uhrzeit lesen"),
    ("get_date",          _get_date,          lambda **_: "Datum lesen"),
    ("get_battery",       _get_battery,       lambda **_: "Akkustand lesen"),
    ("get_wifi",          _get_wifi,          lambda **_: "WLAN-Status lesen"),
    ("get_volume",        _get_volume,        lambda **_: "Lautstärke lesen"),
    ("read_clipboard",    _read_clipboard,    lambda **_: "Zwischenablage lesen"),
    ("get_weather",       _get_weather,       lambda **p: f"Wetter in {p.get('city', 'Berlin')} lesen"),
    ("list_allowed_apps", _list_allowed_apps, lambda **_: "Allowlist anzeigen"),
)


def register_all() -> None:
    """Idempotently register every Tier 1 action. Called from the package
    init once the brain is wired."""
    for name, handler, summary in _TIER1:
        permission_manager.register(name, Tier.INFO, handler, summary)


# Auto-register on import — Tier 1 has no side effects, so it's safe.
register_all()
