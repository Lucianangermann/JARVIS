"""Windows (or any-OS) text client for JARVIS.

Usage
-----
    set JARVIS_URL=http://192.168.1.50:8000
    set JARVIS_AUTH_TOKEN=<same token the server has>
    python clients/windows/client.py            # text loop
    python clients/windows/client.py --ws       # WebSocket loop (recommended)

The client is intentionally tiny — REST + `input()`. It validates the
server's TLS only if you point JARVIS_URL at an https URL; over LAN
plain http to the MacBook server is fine.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import quote, urlparse

import requests

DEFAULT_URL = os.environ.get("JARVIS_URL", "http://127.0.0.1:8000")
TOKEN = os.environ.get("JARVIS_AUTH_TOKEN", "")


def _auth_header() -> dict[str, str]:
    if not TOKEN:
        print(
            "ERROR: set JARVIS_AUTH_TOKEN to the same value as the server's .env",
            file=sys.stderr,
        )
        sys.exit(2)
    return {"Authorization": f"Bearer {TOKEN}"}


# ---------- REST loop ----------------------------------------------------- #

def rest_loop(url: str) -> None:
    print(f"[CLIENT: Windows] connected to {url} (REST)")
    while True:
        try:
            text = input("[YOU] ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text.lower() in {"exit", "quit", ":q"}:
            return

        try:
            r = requests.post(
                f"{url}/chat",
                headers=_auth_header(),
                json={"text": text},
                timeout=60,
            )
        except requests.RequestException as exc:
            print(f"[NET ERROR] {exc}")
            continue

        if r.status_code != 200:
            print(f"[HTTP {r.status_code}] {r.text}")
            continue
        print(f"[JARVIS] {r.json()['reply']}")


# ---------- WebSocket loop ----------------------------------------------- #

def ws_loop(url: str) -> None:
    """Stream messages over a single long-lived socket."""
    try:
        from websockets.sync.client import connect  # websockets >= 12
    except ImportError as exc:
        print(f"WebSocket support needs `pip install websockets`: {exc}", file=sys.stderr)
        sys.exit(2)

    parsed = urlparse(url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    # URL-encode the token: it may legitimately contain '#', '&', '=', etc.,
    # which the `websockets` URL parser otherwise misreads (e.g. '#' becomes
    # a fragment, which is illegal in a WS URL).
    ws_url = f"{ws_scheme}://{parsed.netloc}/ws?token={quote(TOKEN, safe='')}"

    print(f"[CLIENT: Windows] connecting to {ws_url} (WebSocket)")
    with connect(ws_url, additional_headers=_auth_header()) as sock:
        print("[CLIENT: Windows] connected. Type 'exit' to quit.")
        while True:
            try:
                text = input("[YOU] ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not text:
                continue
            if text.lower() in {"exit", "quit", ":q"}:
                return

            sock.send(json.dumps({"text": text}))
            raw = sock.recv()
            msg = json.loads(raw)
            if "error" in msg:
                print(f"[ERROR] {msg['error']}")
            else:
                print(f"[JARVIS] {msg['reply']}")


# ---------- CLI ----------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(description="JARVIS client")
    p.add_argument("--url", default=DEFAULT_URL, help="Server base URL")
    p.add_argument("--ws", action="store_true", help="Use the WebSocket endpoint")
    args = p.parse_args()

    _auth_header()  # fail fast if token missing

    if args.ws:
        ws_loop(args.url)
    else:
        rest_loop(args.url)


if __name__ == "__main__":
    main()
