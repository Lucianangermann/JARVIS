#!/usr/bin/env python3
"""Check and configure HomeBridge for HomeKit access."""
import subprocess
import sys

import requests


def check_homebridge(url: str, token: str) -> bool:
    try:
        r = requests.get(
            f"{url}/api/accessories",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        accessories = r.json()
        print(f"✅ HomeBridge connected — {len(accessories)} accessories found")
        return True
    except Exception as e:
        print(f"❌ HomeBridge not reachable: {e}")
        return False


def print_install_guide() -> None:
    print("""
HomeBridge Installation Guide:
────────────────────────────────
1. Install Node.js: https://nodejs.org/
2. Install HomeBridge:
   npm install -g homebridge homebridge-ui-x

3. Start HomeBridge:
   homebridge

4. Open UI: http://localhost:8581
5. Install 'homebridge-homekit-controller' plugin for device pairing

6. Get the API token from:
   HomeBridge UI → Settings → Auth Settings → Generate Token

7. Add to .env:
   HOMEKIT_ENABLED=true
   HOMEBRIDGE_URL=http://localhost:8581
   HOMEBRIDGE_TOKEN=<your-token>
""")


if __name__ == "__main__":
    url = input("HomeBridge URL [http://localhost:8581]: ").strip() or "http://localhost:8581"
    token = input("HomeBridge API token: ").strip()

    if not token:
        print_install_guide()
        sys.exit(0)

    if check_homebridge(url, token):
        print(f"\nAdd to .env:\nHOMEKIT_ENABLED=true\nHOMEBRIDGE_URL={url}\nHOMEBRIDGE_TOKEN={token}")
    else:
        print_install_guide()
