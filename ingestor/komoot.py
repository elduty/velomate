"""Komoot tour sync — imports recorded rides as individual activities."""

import os

from komPYoot import API, TourType

CYCLING_SPORTS = {
    "touringbicycle", "mtb", "racebike", "gravelbike",
    "e_touringbicycle", "e_racebike", "e_mtb", "e_mtb_easy",
    "mtb_easy", "mtb_advanced", "e_mtb_advanced", "citybike",
    "roadcycling",
}


def _get_api() -> API:
    api = API()
    if not api.login(os.environ["KOMOOT_EMAIL"], os.environ["KOMOOT_PASSWORD"]):
        raise RuntimeError("Komoot login failed")
    return api


def _parse_tour(tour: dict) -> dict | None:
    """Extract activity fields from a Komoot tour dict. Returns None if unusable."""
    date_str = tour.get("date")
    if not date_str:
        return None

    distance_m = tour.get("distance") or 0
    # Komoot may expose duration as 'duration' or 'time_in_motion' (seconds)
    duration_s = tour.get("duration") or tour.get("time_in_motion")
    elevation_m = tour.get("elevation_up") or 0
    name = tour.get("name") or "Komoot Ride"

    avg_speed = None
    if distance_m and duration_s:
        avg_speed = round(distance_m / duration_s * 3.6, 1)

    return {
        "strava_id": None,
        "komoot_tour_id": tour["id"],
        "name": name,
        "date": date_str,
        "distance_m": distance_m,
        "duration_s": duration_s,
        "elevation_m": elevation_m,
        "avg_hr": None,
        "max_hr": None,
        "avg_power": None,
        "max_power": None,
        "avg_cadence": None,
        "avg_speed_kmh": avg_speed,
        "calories": None,
        "suffer_score": None,
        "device": "komoot",
    }


def sync_activities(conn) -> int:
    """
    Fetch all recorded Komoot cycling tours and import as individual activities.

    For each tour:
      1. Skip if already linked (komoot_tour_id set on an existing activity).
      2. If a Strava activity exists at the same time ± 5 min, link it instead
         of creating a duplicate.
      3. Otherwise insert as a new Komoot-only activity.

    Returns number of newly inserted activities.
    """
    from db import find_duplicate, upsert_komoot_activity

    api = _get_api()
    all_tours = api.get_user_tours_list(tour_type=TourType.RECORDED)
    cycling = [t for t in all_tours if t.get("sport") in CYCLING_SPORTS]

    print(f"[komoot] {len(cycling)} recorded cycling tours fetched")

    imported = linked = skipped = 0

    for tour in cycling:
        tour_id = tour.get("id")

        # Already linked to an activity?
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM activities WHERE komoot_tour_id = %s", (tour_id,))
            if cur.fetchone():
                skipped += 1
                continue

        data = _parse_tour(tour)
        if not data:
            skipped += 1
            continue

        # Does a Strava activity already cover this ride?
        if data["date"] and data["duration_s"]:
            dup = find_duplicate(conn, data["date"], data["duration_s"], tolerance_seconds=300)
            if dup:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE activities SET komoot_tour_id = %s WHERE id = %s AND komoot_tour_id IS NULL",
                        (tour_id, dup[0]),
                    )
                print(f"  [komoot] Linked tour {tour_id} ({data['name'][:40]}) → Strava activity {dup[0]}")
                linked += 1
                continue

        # New Komoot-only ride — insert it
        upsert_komoot_activity(conn, data)
        print(f"  [komoot] Imported: {data['name'][:40]} ({data['date'][:10]}, {round(data['distance_m']/1000, 1)} km)")
        imported += 1

    print(f"[komoot] Done: {imported} imported, {linked} linked to Strava, {skipped} skipped")
    return imported
