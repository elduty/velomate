from typing import Dict, List

from veloai import keychain

CYCLING_SPORTS = {
    "touringbicycle", "mtb", "racebike", "roadcycling",
    "gravelbike", "e_touringbicycle",
}


def fetch_tours() -> List[Dict]:
    """Return deduplicated list of cycling tours: {name, distance_km, elevation_m, url}"""
    creds = keychain.get("openclaw/komoot")

    from komPYoot import API
    api = API()
    if not api.login(creds["email"], creds["password"]):
        print("\u26a0\ufe0f  Komoot login failed")
        return []

    all_tours = api.get_user_tours_list()

    # Filter to cycling only
    cycling = [t for t in all_tours if t.get("sport") in CYCLING_SPORTS]

    # Deduplicate by date + distance bucket
    seen = set()
    unique = []
    for t in cycling:
        date = t["date"][:10]
        dist_bucket = round(t["distance"] / 1000)
        key = (date, dist_bucket)
        if key not in seen:
            seen.add(key)
            unique.append(t)

    return unique
