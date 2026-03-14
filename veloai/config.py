"""Configuration loader — YAML file + env vars + defaults."""

import os
import subprocess
import yaml

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/veloai/config.yaml")

DEFAULTS = {
    "home": {"lat": None, "lng": None, "name": ""},
    "db": {"host": "localhost", "port": 5432, "name": "veloai", "user": "veloai", "password": ""},
    "komoot": {"email": "", "password": ""},
    "strava": {"client_id": "", "client_secret": "", "refresh_token": ""},
    "defaults": {"surface": "gravel", "loop": True},
    "fitness": {"max_hr": 0, "ftp": 0},
}

ENV_MAP = {
    ("home", "lat"): "VELOAI_HOME_LAT",
    ("home", "lng"): "VELOAI_HOME_LNG",
    ("db", "host"): "VELOAI_DB_HOST",
    ("db", "port"): "VELOAI_DB_PORT",
    ("db", "name"): "VELOAI_DB_NAME",
    ("db", "user"): "VELOAI_DB_USER",
    ("db", "password"): "VELOAI_DB_PASS",
    ("komoot", "email"): "KOMOOT_EMAIL",
    ("komoot", "password"): "KOMOOT_PASSWORD",
    ("strava", "client_id"): "STRAVA_CLIENT_ID",
    ("strava", "client_secret"): "STRAVA_CLIENT_SECRET",
    ("strava", "refresh_token"): "STRAVA_REFRESH_TOKEN",
}

_config = None


def _resolve_secret(section: dict, key: str) -> str:
    """Resolve a secret value: direct value > env var > command."""
    val = section.get(key, "")
    if val:
        return val
    env_key = section.get(f"{key}_env", "")
    if env_key:
        val = os.environ.get(env_key, "")
        if val:
            return val
    cmd = section.get(f"{key}_cmd", "")
    if cmd:
        try:
            return subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError:
            pass
    return ""


def load(config_path: str = None) -> dict:
    """Load config from YAML file + env vars. Caches result."""
    global _config
    if _config is not None:
        return _config

    path = config_path or os.environ.get("VELOAI_CONFIG", DEFAULT_CONFIG_PATH)
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}

    # Merge with defaults
    result = {}
    for section, defaults in DEFAULTS.items():
        result[section] = {}
        file_section = cfg.get(section, {}) or {}
        for key, default in defaults.items():
            # Check env var first
            env_key = ENV_MAP.get((section, key))
            env_val = os.environ.get(env_key, "") if env_key else ""
            if env_val:
                # Cast to correct type
                if isinstance(default, (int, float)) and default is not None:
                    try:
                        result[section][key] = type(default)(env_val)
                    except (ValueError, TypeError):
                        print(f"[config] Warning: invalid value for {section}.{key}: {env_val}")
                        result[section][key] = default
                elif default is None:
                    # For None defaults (like home.lat/lng), try float
                    try:
                        result[section][key] = float(env_val)
                    except (ValueError, TypeError):
                        result[section][key] = env_val
                else:
                    result[section][key] = env_val
            elif key in file_section:
                result[section][key] = file_section[key]
            else:
                result[section][key] = default

    # Resolve secrets via _cmd/_env patterns
    for section in ("db", "komoot"):
        file_section = cfg.get(section, {}) or {}
        if not result[section].get("password"):
            result[section]["password"] = _resolve_secret(file_section, "password")
        if section == "komoot" and not result[section].get("email"):
            result[section]["email"] = _resolve_secret(file_section, "email")

    # Resolve strava secrets via _cmd/_env patterns
    strava_file = cfg.get("strava", {}) or {}
    for key in ("client_id", "client_secret", "refresh_token"):
        if not result["strava"].get(key):
            result["strava"][key] = _resolve_secret(strava_file, key)

    _config = result
    return result


def get(section: str, key: str, default=None):
    """Get a single config value."""
    cfg = load()
    return cfg.get(section, {}).get(key, default)
