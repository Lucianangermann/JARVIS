#!/usr/bin/env python3
"""Configure Home Assistant connection."""
import sys

import requests


def test_ha(url: str, token: str) -> bool:
    try:
        r = requests.get(
            f"{url}/api/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        print(f"✅ Connected to Home Assistant: {data.get('message', 'OK')}")

        # Count entities
        states = requests.get(
            f"{url}/api/states",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        ).json()
        print(f"   {len(states)} entities found")
        return True
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False


def print_token_guide(url: str) -> None:
    print(f"""
Get a Long-Lived Access Token:
──────────────────────────────
1. Open {url}/profile
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Name it "JARVIS"
5. Copy the token
""")


if __name__ == "__main__":
    url = input("Home Assistant URL [http://homeassistant.local:8123]: ").strip()
    url = url or "http://homeassistant.local:8123"
    url = url.rstrip("/")

    print_token_guide(url)
    token = input("Paste your token: ").strip()

    if not token:
        print("No token provided.")
        sys.exit(1)

    if test_ha(url, token):
        print(f"\nAdd to .env:\nHA_ENABLED=true\nHA_URL={url}\nHA_TOKEN={token}")
    else:
        print("Check that Home Assistant is running and the URL is correct.")
