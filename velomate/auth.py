"""Strava OAuth authorization flow.

Manual paste flow — works from any machine without port forwarding:
1. Prints a URL for the user to open in their browser
2. User approves on Strava
3. Strava redirects to localhost (page won't load, that's expected)
4. User copies the 'code' parameter from the browser's URL bar
5. Pastes it back into the terminal
6. We exchange it for access + refresh tokens and store them
"""

import os
import sys
from urllib.parse import urlencode, urlparse, parse_qs

import requests

TOKEN_URL = "https://www.strava.com/oauth/token"
AUTH_URL = "https://www.strava.com/oauth/authorize"
REDIRECT_URI = "http://localhost/exchange_token"

# Scopes needed: read activities + read activity streams
SCOPE = "read,activity:read_all"


def authorize(client_id: str = None, client_secret: str = None) -> dict:
    """Run the interactive OAuth flow.

    Args:
        client_id: Strava API client ID. Falls back to STRAVA_CLIENT_ID env var.
        client_secret: Strava API client secret. Falls back to STRAVA_CLIENT_SECRET env var.

    Returns:
        dict with access_token, refresh_token, expires_at
    """
    client_id = client_id or os.environ.get("STRAVA_CLIENT_ID", "")
    client_secret = client_secret or os.environ.get("STRAVA_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print("Error: STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET must be set.")
        print("Get them from https://www.strava.com/settings/api")
        sys.exit(1)

    # Build authorization URL
    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "approval_prompt": "auto",
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print()
    print("=== Strava Authorization ===")
    print()
    print("1. Open this URL in your browser:")
    print()
    print(f"   {auth_url}")
    print()
    print("2. Approve the access on Strava.")
    print()
    print("3. You'll be redirected to a page that won't load (that's expected).")
    print("   Copy the FULL URL from your browser's address bar.")
    print("   It looks like: http://localhost/exchange_token?state=&code=XXXXX&scope=...")
    print()

    callback_url = input("4. Paste the full URL here: ").strip()

    # Extract the code from the callback URL
    try:
        parsed = urlparse(callback_url)
        qs = parse_qs(parsed.query)
        code = qs["code"][0]
    except (KeyError, IndexError):
        print(f"Error: Could not find 'code' parameter in URL: {callback_url}")
        sys.exit(1)

    # Exchange the code for tokens
    print()
    print("Exchanging code for tokens...")
    try:
        resp = requests.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        }, timeout=15)
    except requests.RequestException as e:
        print(f"Error: Could not reach Strava: {e}")
        sys.exit(1)

    if resp.status_code != 200:
        print(f"Error: Strava returned {resp.status_code}: {resp.text}")
        sys.exit(1)

    data = resp.json()
    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_at = data["expires_at"]
    athlete = data.get("athlete", {})
    name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()

    print()
    print(f"Authorized as: {name or 'Unknown'}")
    print(f"Refresh token: {refresh_token}")
    print()
    print("Add this to your .env file:")
    print(f"  STRAVA_REFRESH_TOKEN={refresh_token}")
    print()

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }


if __name__ == "__main__":
    authorize()
