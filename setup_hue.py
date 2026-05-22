#!/usr/bin/env python3
"""Auto-discover and configure Philips Hue bridge."""
import json
import time

import requests


def discover_bridge() -> str | None:
    print("Discovering Hue bridges via nupnp...")
    try:
        r = requests.get("https://discovery.meethue.com/", timeout=5)
        bridges = r.json()
        if bridges:
            ip = bridges[0]["internalipaddress"]
            print(f"Found bridge at {ip}")
            return ip
    except Exception:
        pass
    print("No bridge found via cloud. Try: https://www.meethue.com/api/nupnp")
    return None


def register_user(bridge_ip: str) -> str | None:
    print(f"\nPress the LINK BUTTON on your Hue bridge now...")
    print("Waiting 30 seconds...")
    for i in range(30):
        time.sleep(1)
        print(f"  {30-i}s remaining...", end="\r")
        try:
            r = requests.post(
                f"http://{bridge_ip}/api",
                json={"devicetype": "jarvis#1"},
                timeout=5,
            )
            data = r.json()
            if isinstance(data, list) and "success" in data[0]:
                username = data[0]["success"]["username"]
                print(f"\nRegistered! Username: {username}")
                return username
        except Exception:
            pass
    print("\nTimeout. Please press the link button and try again.")
    return None


def save_to_env(bridge_ip: str, username: str) -> None:
    env_path = ".env"
    try:
        with open(env_path) as f:
            lines = f.readlines()
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith("HUE_BRIDGE_IP="):
                line = f"HUE_BRIDGE_IP={bridge_ip}\n"
                updated = True
            elif line.startswith("HUE_USERNAME="):
                line = f"HUE_USERNAME={username}\n"
            new_lines.append(line)
        if not updated:
            new_lines.extend([
                f"\nHUE_BRIDGE_IP={bridge_ip}\n",
                f"HUE_USERNAME={username}\n",
            ])
        with open(env_path, "w") as f:
            f.writelines(new_lines)
        print(f"Saved to {env_path}")
        print("Set HUE_ENABLED=true and restart JARVIS.")
    except Exception as e:
        print(f"Could not save: {e}")
        print(f"Add manually to .env:\nHUE_BRIDGE_IP={bridge_ip}\nHUE_USERNAME={username}")


if __name__ == "__main__":
    ip = discover_bridge()
    if ip:
        username = register_user(ip)
        if username:
            save_to_env(ip, username)
