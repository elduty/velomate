"""Komoot route sync to DB."""

import os
from collections import defaultdict

from komPYoot import API

CYCLING_SPORTS = {
    "touringbicycle", "mtb", "racebike", "roadcycling",
    "gravelbike", "e_touringbicycle",
}


def _get_api() -> API:
    """Login to Komoot and return API instance."""
    api = API()
    email = os.environ["KOMOOT_EMAIL"]
    password = os.environ["KOMOOT_PASSWORD"]
    if not api.login(email, password):
        raise RuntimeError("Komoot login failed")
    return api


def sync_routes(conn):
    """Fetch all Komoot cycling tours, aggregate into routes, upsert to DB."""
    from db import upsert_route

    api = _get_api()
    all_tours = api.get_user_tours_list()
    cycling = [t for t in all_tours if t.get("sport") in CYCLING_SPORTS]

    print(f"[komoot] Fetched {len(cycling)} cycling tours from Komoot")

    # Group by name+distance bucket to identify unique routes
    route_map = defaultdict(list)
    for t in cycling:
        dist_bucket = round(t["distance"] / 1000)
        key = (t.get("name", "Unnamed"), dist_bucket)
        route_map[key].append(t)

    count = 0
    for (name, _), tours in route_map.items():
        # Use most recent tour's data
        latest = max(tours, key=lambda t: t.get("date", ""))
        dates = [t.get("date", "")[:10] for t in tours if t.get("date")]
        last_ridden = max(dates) if dates else None

        upsert_route(conn, {
            "komoot_id": latest["id"],
            "name": name,
            "distance_m": latest.get("distance", 0),
            "elevation_m": latest.get("elevation_up", 0),
            "sport": latest.get("sport", "cycling"),
            "last_ridden_at": last_ridden,
            "ride_count": len(tours),
        })
        count += 1

    print(f"[komoot] Upserted {count} unique routes")
    return count
