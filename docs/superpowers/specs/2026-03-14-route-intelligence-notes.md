# Route Intelligence Layer — Brainstorming Notes

**Status:** Brainstorming paused. Resume next session.

**Context:** Current route generator places 4 circular waypoints mathematically and routes via Valhalla. The intelligence layer makes waypoints smarter using external data.

---

## Four Data Sources

### 1. OSM Surface Tags (Overpass API)
- Query roads within route radius for `surface=asphalt/gravel/unpaved/compacted`
- Verify Valhalla's route actually matches requested surface type
- Find best gravel/road corridors to place waypoints on
- Free, no API key, well-documented

### 2. Strava Segment Popularity
- Pull popular cycling segments near home via Strava API (already have auth)
- High-traffic cycling roads = usually safer, better surface
- Use as waypoint candidates — route through popular corridors
- Rate limited: 100 req/15min

### 3. Ride History (DB)
- Query GPS points from `activity_streams`, cluster into road segments
- "Variety" mode: avoid segments ridden in last 30 days
- "Comfort" mode: prefer most-ridden roads
- Needs spatial clustering (DBSCAN or grid bucketing)

### 4. POIs (OSM + Komoot Highlights)
- **OSM (Overpass API):** `tourism=viewpoint`, `amenity=cafe`, `amenity=drinking_water`, `natural=peak`, `amenity=bicycle_repair_station`
- **Komoot Highlights:** community-curated cycling POIs (viewpoints, scenic spots, tricky sections)
  - komPYoot doesn't expose highlights
  - Internal API: `https://api.komoot.de/v007/highlights/` with bounding box — needs reverse engineering
- Route through interesting spots by placing POIs as intermediate waypoints

---

## Proposed Phasing

### Phase 1 (immediate value, well-documented APIs)
- OSM POIs via Overpass → place as waypoints (viewpoints, cafes, water fountains)
- OSM surface verification → check Valhalla route matches requested surface
- Strava popular segments → use as waypoint candidates
- **Result:** Routes through interesting places on correct surfaces via popular roads

### Phase 2 (personalization, more complex)
- Komoot highlights API (reverse-engineer internal API)
- Ride history GPS clustering (variety/comfort modes)
- "Avoid this road" / "prefer this area" learning
- Route variety detection (avoid repeating same loop)

---

## Implementation Approach (Phase 1)

### New file: `veloai/route_intelligence.py`

```
def get_pois(lat, lng, radius_km, poi_types) -> list[dict]
    # Overpass API query for OSM POIs within radius
    # Returns list of {lat, lng, name, type}

def get_popular_segments(lat, lng, radius_km) -> list[dict]
    # Strava API: /segments/explore?bounds=...
    # Returns list of {lat_start, lng_start, lat_end, lng_end, name, athlete_count}

def verify_surface(gpx_coords, expected_surface) -> dict
    # Overpass API: query road surfaces along the route
    # Returns {match_pct, mismatched_segments}

def smart_waypoints(lat, lng, target_km, surface, preference) -> list[dict]
    # Combine POIs + popular segments + surface data
    # Place waypoints on interesting, popular, correct-surface roads
    # Returns list of {lat, lng, reason} to feed into route_generator
```

### Modified flow in `route_planner.py`
```
Current:  estimate_distance → circular waypoints → Valhalla → GPX → Komoot
New:      estimate_distance → smart_waypoints() → Valhalla → verify_surface() → GPX → Komoot
```

---

## Open Questions (for next session)
- How many POIs per route? (too many = route becomes a tour, not a ride)
- Should POIs be optional (`--pois` flag) or always included?
- Strava segment API: does `/segments/explore` return enough data for waypoint placement?
- Surface verification: what to do if mismatch found? Re-route, warn, or ignore?
- Komoot highlights: worth the reverse-engineering effort, or OSM POIs sufficient?
