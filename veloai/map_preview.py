"""Route map preview — generates an HTML map and opens in browser."""

import os
import tempfile
import webbrowser


def preview(coords: list, name: str, waypoints: list | None = None) -> str:
    """Generate an HTML map preview of a route and open in browser.

    coords: list of (lat, lng) tuples from the GPX
    name: route name
    waypoints: optional list of {lat, lng, name} dicts

    Returns path to the HTML file.
    """
    if not coords:
        return ""

    # Calculate bounds for auto-fit
    lats = [c[0] for c in coords]
    lngs = [c[1] for c in coords]
    center_lat = (min(lats) + max(lats)) / 2
    center_lng = (min(lngs) + max(lngs)) / 2

    # Build coordinate array for Leaflet
    coord_js = ",".join(f"[{lat},{lng}]" for lat, lng in coords[::5])  # sample every 5th point

    # Build waypoint markers
    markers_js = ""
    if waypoints:
        for wp in waypoints:
            markers_js += f"""
            L.marker([{wp['lat']}, {wp['lng']}])
                .addTo(map)
                .bindPopup('<b>{wp["name"]}</b><br>{wp.get("reason", "")}');
            """

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>{name} — Route Preview</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"></script>
    <style>
        body {{ margin: 0; font-family: system-ui; background: #1a1a2e; color: #eee; }}
        #map {{ height: 70vh; }}
        .info {{ padding: 16px 24px; }}
        .info h2 {{ margin: 0 0 8px; color: #6ed0ff; }}
        .info p {{ margin: 4px 0; color: #aaa; }}
        .btn {{ display: inline-block; margin-top: 12px; padding: 10px 24px; background: #73bf69; color: #fff; border: none; border-radius: 6px; font-size: 14px; cursor: pointer; text-decoration: none; }}
        .btn:hover {{ background: #5a9e52; }}
    </style>
</head>
<body>
    <div class="info">
        <h2>{name}</h2>
        <p>Review the route below. If it looks good, the CLI will upload it to Komoot.</p>
    </div>
    <div id="map"></div>
    <div class="info">
        <p>Close this tab when done reviewing.</p>
    </div>
    <script>
        var coords = [{coord_js}];
        var map = L.map('map');
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap'
        }}).addTo(map);

        var route = L.polyline(coords, {{color: '#6ed0ff', weight: 4, opacity: 0.8}}).addTo(map);
        map.fitBounds(route.getBounds().pad(0.1));

        // Start/end marker
        if (coords.length > 0) {{
            L.circleMarker(coords[0], {{radius: 8, color: '#73bf69', fillColor: '#73bf69', fillOpacity: 1}})
                .addTo(map).bindPopup('<b>Start / End</b>');
        }}

        {markers_js}
    </script>
</body>
</html>"""

    # Write to temp file and open
    fd, path = tempfile.mkstemp(suffix=".html", prefix="veloai_preview_")
    with os.fdopen(fd, "w") as f:
        f.write(html)

    webbrowser.open(f"file://{path}")
    return path
