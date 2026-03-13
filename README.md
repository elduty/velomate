# VeloAI 🚴

Smart ride planner for cycling — fetches weather forecasts, your Komoot tour history, and Strava fitness data to recommend the best days and routes for your next ride.

## Requirements

- Python 3.9+
- macOS (uses Keychain for credential storage)
- Komoot account with recorded tours
- Strava account with API credentials

## Setup

```bash
pip install -r requirements.txt
```

### Keychain Setup

Two Keychain entries are required (JSON format, stored under account `openclaw`):

- **`openclaw/komoot`** — `{"email": "...", "password": "..."}`
- **`openclaw/strava`** — `{"client_id": "...", "client_secret": "...", "refresh_token": "..."}`

Store them with:

```bash
security add-generic-password -a openclaw -s openclaw/komoot -w '{"email":"...","password":"..."}'
security add-generic-password -a openclaw -s openclaw/strava -w '{"client_id":"...","client_secret":"...","refresh_token":"..."}'
```

## Usage

```bash
python3 -m veloai.cli
```

Outputs a WhatsApp-friendly ride recommendation with:
- 7-day weather forecast with cycling scores
- Recent fitness summary from Strava
- Best days to ride
- Route suggestions from Komoot history

## Roadmap

- Weekly automated reports via cron
- Route difficulty matching improvements
- Integration with more weather data sources
