"""Nominatim geocoder — place name to lat/lng."""

import requests


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


def geocode(place: str, near_lat: float = 38.69, near_lng: float = -9.32) -> dict | None:
    """Geocode a place name to lat/lng coordinates.
    Biases results toward near_lat/near_lng (default: São Domingos de Rana, Portugal).
    Returns {"lat": float, "lng": float, "display_name": str} or None if not found.
    """
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": place,
                "format": "json",
                "limit": 1,
                "viewbox": f"{near_lng - 1},{near_lat + 1},{near_lng + 1},{near_lat - 1}",
                "bounded": 0,
            },
            headers={"User-Agent": "VeloAI/1.0"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            r = results[0]
            return {
                "lat": float(r["lat"]),
                "lng": float(r["lon"]),
                "display_name": r.get("display_name", place),
            }
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"[geocode] Failed to geocode '{place}': {e}")
    return None


def geocode_many(places: list, near_lat: float = 38.69, near_lng: float = -9.32) -> list:
    """Geocode a list of place names. Returns list of successfully geocoded results."""
    results = []
    for place in places:
        result = geocode(place.strip(), near_lat, near_lng)
        if result:
            results.append(result)
    return results
