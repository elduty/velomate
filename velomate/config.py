"""Configuration loader — YAML file + env vars + defaults."""

import os
import subprocess
import sys
import yaml

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/velomate/config.yaml")

DEFAULTS = {
    "home": {"lat": None, "lng": None, "name": ""},
    "db": {"host": "localhost", "port": 5432, "name": "velomate", "user": "velomate", "password": ""},
    "strava": {"client_id": "", "client_secret": "", "refresh_token": ""},
    "defaults": {"surface": "gravel", "loop": True},
    "fitness": {"max_hr": 0, "ftp": 0},
}

ENV_MAP = {
    ("home", "lat"): "VELOMATE_HOME_LAT",
    ("home", "lng"): "VELOMATE_HOME_LNG",
    ("db", "host"): "VELOMATE_DB_HOST",
    ("db", "port"): "VELOMATE_DB_PORT",
    ("db", "name"): "VELOMATE_DB_NAME",
    ("db", "user"): "VELOMATE_DB_USER",
    ("db", "password"): "VELOMATE_DB_PASS",
    ("strava", "client_id"): "STRAVA_CLIENT_ID",
    ("strava", "client_secret"): "STRAVA_CLIENT_SECRET",
    ("strava", "refresh_token"): "STRAVA_REFRESH_TOKEN",
}

_config = None
_config_path_used = None


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
        import shlex
        try:
            return subprocess.check_output(shlex.split(cmd), stderr=subprocess.DEVNULL).decode().strip()
        except subprocess.CalledProcessError as e:
            print(f"[config] Warning: {key}_cmd failed: {e}", file=sys.stderr)
        except (ValueError, FileNotFoundError, TypeError) as e:
            print(f"[config] Warning: invalid {key}_cmd '{cmd}': {e}", file=sys.stderr)
    return ""


def load(config_path: str = None) -> dict:
    """Load config from YAML file + env vars. Caches result.
    If config_path differs from the previously cached path, the cache is invalidated
    so callers with different paths always get the correct config.
    """
    global _config, _config_path_used
    path = config_path or os.environ.get("VELOMATE_CONFIG", DEFAULT_CONFIG_PATH)
    if _config is not None and _config_path_used == path:
        return _config
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
    db_file = cfg.get("db", {}) or {}
    if not result["db"].get("password"):
        result["db"]["password"] = _resolve_secret(db_file, "password")

    # Resolve strava secrets via _cmd/_env patterns
    strava_file = cfg.get("strava", {}) or {}
    for key in ("client_id", "client_secret", "refresh_token"):
        if not result["strava"].get(key):
            result["strava"][key] = _resolve_secret(strava_file, key)

    # Load avoid zones (list, not key-value)
    result["avoid"] = cfg.get("avoid", []) or []

    _config = result
    _config_path_used = path
    return result


def get(section: str, key: str, default=None):
    """Get a single config value."""
    cfg = load()
    return cfg.get(section, {}).get(key, default)
