import json
import subprocess


def get(service: str) -> dict:
    """Retrieve JSON credentials from macOS Keychain.
    Raises RuntimeError if entry not found or not valid JSON.
    """
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-a", "openclaw", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(f"Keychain entry not found: account=openclaw service={service}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Keychain entry {service} is not valid JSON")
