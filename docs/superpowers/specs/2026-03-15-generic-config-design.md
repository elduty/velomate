# Generic Configuration — Design Spec

**Goal:** Remove all hardcoded personal configuration and keychain dependency so the project can be open-sourced. All config comes from a YAML file, environment variables, or CLI flags.

**Approach:** New `veloai/config.py` module that loads config from `~/.config/veloai/config.yaml` (overridable via `VELOAI_CONFIG` env var), with env var overrides and sensible defaults for everything.

---

## Design Decisions

1. **YAML config file** at `~/.config/veloai/config.yaml` — human-readable, comments supported
2. **Env vars override config file** — for Docker and CI use
3. **No keychain dependency** — credentials come from config file or env vars. Optional `password_cmd` for users who want platform-specific secret managers.
4. **Sensible defaults for everything** — project works out of the box with just home coordinates + DB connection
5. **Config example committed** — `config.example.yaml` in repo root

---

## Config File Structure

```yaml
# VeloAI Configuration
# Copy to ~/.config/veloai/config.yaml

home:
  lat: 38.69
  lng: -9.32
  name: São Domingos de Rana

db:
  host: 10.7.40.15
  port: 5423
  name: veloai
  user: veloai
  password: ""                    # or use password_env / password_cmd
  password_env: VELOAI_DB_PASS    # read password from this env var
  password_cmd: ""                # shell command that prints password

komoot:
  email: ""
  password: ""
  email_env: KOMOOT_EMAIL
  password_env: KOMOOT_PASSWORD

strava:
  client_id: ""
  client_secret: ""
  refresh_token: ""

defaults:
  surface: gravel
  loop: true

fitness:
  threshold_hr: 170     # auto-estimated from data if 0
  ftp: 150              # auto-estimated from data if 0
```

## Config Resolution Order

For each value: **CLI flag > env var > config file > built-in default**

| Config key | Env var | Default |
|-----------|---------|---------|
| `home.lat` | `VELOAI_HOME_LAT` | required |
| `home.lng` | `VELOAI_HOME_LNG` | required |
| `db.host` | `VELOAI_DB_HOST` | `localhost` |
| `db.port` | `VELOAI_DB_PORT` | `5432` |
| `db.name` | `VELOAI_DB_NAME` | `veloai` |
| `db.user` | `VELOAI_DB_USER` | `veloai` |
| `db.password` | `VELOAI_DB_PASS` | `""` |
| `komoot.email` | `KOMOOT_EMAIL` | `""` |
| `komoot.password` | `KOMOOT_PASSWORD` | `""` |
| `defaults.surface` | — | `gravel` |
| `defaults.loop` | — | `true` |
| `fitness.threshold_hr` | — | `170` (auto if 0) |
| `fitness.ftp` | — | `150` (auto if 0) |

## `password_cmd` Pattern

For users who want keychain/vault integration without hardcoding a specific secret manager:

```yaml
db:
  password_cmd: "security find-generic-password -a myaccount -s myservice -w"
```

The config module runs this command and uses stdout as the password. Works with macOS Keychain, 1Password CLI, Bitwarden CLI, etc.

---

## Files Changed

| File | Action |
|------|--------|
| `veloai/config.py` | **Create** — config loader |
| `config.example.yaml` | **Create** — template committed to git |
| `veloai/cli.py` | **Modify** — remove hardcoded LOCATION, load from config |
| `veloai/db.py` | **Modify** — remove hardcoded DB_HOST etc, load from config |
| `veloai/route_planner.py` | **Modify** — remove hardcoded DEFAULT_SPEEDS/FTP, use config; remove keychain import in _upload_to_komoot |
| `veloai/komoot.py` | **Modify** — remove keychain import, use config for credentials |
| `veloai/keychain.py` | **Delete** — replaced by config credential chain |
| `ingestor/fitness.py` | **Modify** — read default threshold_hr/ftp from config or keep auto-estimate |
| `.gitignore` | **Modify** — add config.yaml |
| `CLAUDE.md` | **Modify** — update credential docs |
| `README.md` | **Modify** — update setup instructions |
| `tests/test_classify_and_weather.py` | **Modify** — remove keychain mock if no longer imported |

---

## `veloai/config.py` Implementation

```python
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
    "fitness": {"threshold_hr": 170, "ftp": 150},
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
                    result[section][key] = type(default)(env_val)
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

    _config = result
    return result


def get(section: str, key: str, default=None):
    """Get a single config value."""
    cfg = load()
    return cfg.get(section, {}).get(key, default)
```

---

## .gitignore Additions

```
config.yaml
.env
```

---

## Migration for Marcin

After implementation, create `~/.config/veloai/config.yaml` with personal values:

```bash
mkdir -p ~/.config/veloai
cp config.example.yaml ~/.config/veloai/config.yaml
# Edit with personal coordinates, DB host, credentials
```
