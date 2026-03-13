import json
import subprocess


def get(service: str) -> dict:
    """Retrieve JSON credentials from macOS Keychain."""
    raw = subprocess.check_output(
        ["security", "find-generic-password", "-a", "openclaw", "-s", service, "-w"],
        stderr=subprocess.DEVNULL,
    ).decode().strip()
    return json.loads(raw)
