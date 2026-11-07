"""The opt-in upload service in docker-compose.yml (parsed, never run)."""
from pathlib import Path

import yaml

COMPOSE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
SHARED = ("DATABASE_URL", "VELOMATE_MAX_HR", "VELOMATE_FTP",
          "VELOMATE_RESTING_HR", "VELOMATE_WEIGHT", "VELOMATE_RESET_RIDE_FTP")

# The ingestor's environment as it was before the shared anchor existed.
INGESTOR_ENV_BEFORE = {   'DATABASE_URL': 'postgresql://${POSTGRES_USER:-velomate}:${POSTGRES_PASSWORD}@velomate-postgres:5432/${POSTGRES_DB:-velomate}',
    'STRAVA_CLIENT_ID': '${STRAVA_CLIENT_ID:-}',
    'STRAVA_CLIENT_SECRET': '${STRAVA_CLIENT_SECRET:-}',
    'STRAVA_REFRESH_TOKEN': '${STRAVA_REFRESH_TOKEN:-}',
    'RWGPS_API_KEY': '${RWGPS_API_KEY:-}',
    'RWGPS_AUTH_TOKEN': '${RWGPS_AUTH_TOKEN:-}',
    'INTERVALS_ICU_ATHLETE_ID': '${INTERVALS_ICU_ATHLETE_ID:-}',
    'INTERVALS_ICU_API_KEY': '${INTERVALS_ICU_API_KEY:-}',
    'INTERVALS_ICU_SYNC_WINDOW_DAYS': '${INTERVALS_ICU_SYNC_WINDOW_DAYS:-14}',
    'INTERVALS_ICU_SWEEP_DAYS': '${INTERVALS_ICU_SWEEP_DAYS:-90}',
    'VELOMATE_MAX_HR': '${VELOMATE_MAX_HR:-0}',
    'VELOMATE_FTP': '${VELOMATE_FTP:-0}',
    'VELOMATE_RESTING_HR': '${VELOMATE_RESTING_HR:-0}',
    'VELOMATE_WEIGHT': '${VELOMATE_WEIGHT:-0}',
    'VELOMATE_RESET_RIDE_FTP': '${VELOMATE_RESET_RIDE_FTP:-0}',
    'VELOMATE_BACKFILL_MONTHS': '${VELOMATE_BACKFILL_MONTHS:-12}',
    'POLL_INTERVAL_MINUTES': '${POLL_INTERVAL_MINUTES:-10}',
    'VELOMATE_TOKEN_FALLBACK': '${VELOMATE_TOKEN_FALLBACK:-/app/data/.strava_refresh_token}',
    'VELOMATE_IMPORT_DIR': '${VELOMATE_IMPORT_DIR:-}',
    'PYTHONUNBUFFERED': '${PYTHONUNBUFFERED:-1}'}


def test_upload_service_shares_the_ingestor_threshold_env():
    ing = SERVICES["velomate-ingestor"]["environment"]
    up = SERVICES["velomate-upload"]["environment"]
    for key in SHARED:
        assert key in ing and key in up, key
        assert up[key] == ing[key], key


def test_upload_service_is_profile_gated_and_publishes_3022():
    up = SERVICES["velomate-upload"]
    assert up["profiles"] == ["upload"]
    assert up["ports"] == ["${UPLOAD_PORT:-3022}:8080"]
    assert up["command"][-1] == "upload"
    assert up["command"] == ["python3", "-u", "main.py", "upload"]
    assert up["restart"] == "unless-stopped"
    assert up["depends_on"]["velomate-postgres"]["condition"] == "service_healthy"
    assert "velomate-grafana" in up["depends_on"]
    assert up["environment"]["GRAFANA_URL"] == "${GRAFANA_URL:-http://velomate-grafana:3000}"
    assert up["environment"]["GRAFANA_ROOT_URL"] == \
        "${GRAFANA_ROOT_URL:-http://localhost:${GRAFANA_PORT:-3021}/}"


def test_no_other_service_is_profile_gated_or_gains_a_port():
    for name, svc in SERVICES.items():
        if name != "velomate-upload":
            assert "profiles" not in svc, name
    assert "ports" not in SERVICES["velomate-ingestor"]


def test_ingestor_service_env_unchanged_by_the_anchor():
    assert SERVICES["velomate-ingestor"]["environment"] == INGESTOR_ENV_BEFORE
