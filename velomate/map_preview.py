"""Route map preview — generates an HTML map with route info and opens in browser."""

import html
import os
import tempfile
import webbrowser


def _read_gpx(gpx_path: str) -> str:
    """Read GPX file content for embedding in HTML."""
    try:
        with open(gpx_path, "r") as f:
            return f.read()
    except Exception:
        return ""


def preview(coords: list, name: str, waypoints: list | None = None,
            route_info: dict | None = None, output_dir: str | None = None) -> str:
    """Generate an HTML map preview of a route and open in browser.

    coords: list of (lat, lng) tuples from the GPX
    name: route name
    waypoints: optional list of {lat, lng, name, reason} dicts
    route_info: optional dict with enrichment data:
        distance_km, elevation, scenic, surface, safety, weather, fitness,
        air_quality, sun, trails, best_time

    Returns path to the HTML file.
    """
    if not coords:
        return ""

    info = route_info or {}
    name = html.escape(name)  # prevent XSS via route/waypoint names
    gpx_content = _read_gpx(info.get("gpx_path", ""))
    # Escape for embedding in JS template literal
    gpx_js = gpx_content.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${").replace("</script>", "<\\/script>") if gpx_content else ""
    gpx_filename = os.path.basename(info.get("gpx_path", "route.gpx"))

    # Build coordinate array for Leaflet
    coord_js = ",".join(f"[{lat},{lng}]" for lat, lng in coords[::5])

    # Build waypoint markers
    markers_js = ""
    if waypoints:
        for wp in waypoints:
            wp_name = html.escape(wp.get("name", "")).replace("'", "\\'")
            wp_reason = html.escape(wp.get("reason", "")).replace("'", "\\'")
            markers_js += f"""
            L.marker([{wp['lat']}, {wp['lng']}])
                .addTo(map)
                .bindPopup('<b>{wp_name}</b><br>{wp_reason}');
            """

    # Build info cards HTML
    cards_html = ""

    # Distance + duration + elevation cards
    dist = info.get("distance_km", 0)
    dur = info.get("duration_min", 0)
    elev = info.get("elevation", {})
    climb = elev.get("total_climb", 0)
    descent = elev.get("total_descent", 0)
    gradient = elev.get("max_gradient", 0)
    if dist:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">📏</div>
            <div class="card-body">
                <div class="card-value">{dist:.0f} km</div>
                <div class="card-label">Distance</div>
            </div>
        </div>"""
    if dur:
        hours = dur // 60
        mins = dur % 60
        dur_str = f"{hours}h {mins:02d}m" if hours else f"{mins}m"
        cards_html += f"""
        <div class="card">
            <div class="card-icon">⏱</div>
            <div class="card-body">
                <div class="card-value">{dur_str}</div>
                <div class="card-label">Est. duration</div>
            </div>
        </div>"""
        if climb:
            cards_html += f"""
        <div class="card">
            <div class="card-icon">⛰</div>
            <div class="card-body">
                <div class="card-value">+{climb}m / -{descent}m</div>
                <div class="card-label">Climb · max {gradient}%</div>
            </div>
        </div>"""

    # Surface card
    surface = info.get("surface", {})
    if surface.get("surfaces"):
        breakdown = ", ".join(f"{s} {p}%" for s, p in list(surface["surfaces"].items())[:3])
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🛤</div>
            <div class="card-body">
                <div class="card-value">{breakdown}</div>
                <div class="card-label">Surface</div>
            </div>
        </div>"""

    # Scenic card
    scenic = info.get("scenic", {})
    if scenic.get("features"):
        features = ", ".join(scenic["features"][:3])
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🌿</div>
            <div class="card-body">
                <div class="card-value">{scenic['scenic_score']}/100</div>
                <div class="card-label">Scenic · {features}</div>
            </div>
        </div>"""

    # Safety card
    safety = info.get("safety", {})
    if safety.get("details"):
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🛡</div>
            <div class="card-body">
                <div class="card-value">{safety['safety_score']}/100</div>
                <div class="card-label">Safety · {safety['details']}</div>
            </div>
        </div>"""

    # Weather card
    weather = info.get("weather")
    if weather:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🌤</div>
            <div class="card-body">
                <div class="card-value">{weather.get('temp_min', 0):.0f}–{weather.get('temp_max', 0):.0f}°C</div>
                <div class="card-label">{weather.get('weather', '')} · wind {weather.get('wind', 0):.0f} km/h</div>
            </div>
        </div>"""

    # Best time card
    best_time = info.get("best_time")
    if best_time:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🕐</div>
            <div class="card-body">
                <div class="card-value">{best_time['hour']}</div>
                <div class="card-label">{best_time['temp']:.0f}°C · wind {best_time['wind']:.0f} km/h · UV {best_time['uv']:.0f}</div>
            </div>
        </div>"""

    # Sunrise/sunset card
    sun = info.get("sun")
    if sun:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🌅</div>
            <div class="card-body">
                <div class="card-value">{sun['sunrise']} – {sun['sunset']}</div>
                <div class="card-label">Sunrise / Sunset</div>
            </div>
        </div>"""

    # Fitness card
    fitness = info.get("fitness")
    if fitness:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">💪</div>
            <div class="card-body">
                <div class="card-value">{fitness}</div>
                <div class="card-label">Fitness</div>
            </div>
        </div>"""

    # Trails card
    trails = info.get("trails", [])
    if trails:
        cards_html += f"""
        <div class="card">
            <div class="card-icon">🚲</div>
            <div class="card-body">
                <div class="card-value">{', '.join(trails)}</div>
                <div class="card-label">Cycling trails</div>
            </div>
        </div>"""

    # Waypoints list
    wp_html = ""
    if waypoints:
        wp_items = "".join(
            f'<li><b>{html.escape(wp.get("name", ""))}</b> <span class="wp-reason">{html.escape(wp.get("reason", ""))}</span></li>'
            for wp in waypoints
        )
        wp_html = f"""
        <div class="waypoints">
            <h3>Waypoints</h3>
            <ol>{wp_items}</ol>
        </div>"""

    page = f"""<!DOCTYPE html>
<html>
<head>
    <title>{name} — Route Preview</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css">
    <script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"></script>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #1a1a2e; color: #e0e0e0; }}
        #map {{ height: 55vh; min-height: 300px; }}
        .header {{ padding: 16px 24px; border-bottom: 1px solid #2a2d35; }}
        .header h1 {{ margin: 0; font-size: 20px; color: #6ed0ff; }}
        .header .gpx {{ margin-top: 4px; font-size: 12px; color: #666; }}
        .cards {{ display: flex; flex-wrap: wrap; gap: 12px; padding: 16px 24px; }}
        .card {{ display: flex; align-items: center; gap: 10px; background: #1e2228; border-radius: 8px; padding: 12px 16px; min-width: 200px; flex: 1 1 200px; }}
        .card-icon {{ font-size: 24px; }}
        .card-body {{ flex: 1; }}
        .card-value {{ font-size: 16px; font-weight: 600; color: #fff; }}
        .card-label {{ font-size: 11px; color: #888; margin-top: 2px; }}
        .waypoints {{ padding: 0 24px 16px; }}
        .waypoints h3 {{ margin: 0 0 8px; font-size: 14px; color: #aaa; }}
        .waypoints ol {{ margin: 0; padding-left: 20px; }}
        .waypoints li {{ margin: 4px 0; font-size: 13px; }}
        .wp-reason {{ color: #666; font-size: 11px; }}
        .actions {{ display: flex; gap: 12px; padding: 16px 24px; flex-wrap: wrap; }}
        .btn {{ display: inline-flex; align-items: center; gap: 8px; padding: 12px 24px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; cursor: pointer; text-decoration: none; }}
        .btn-primary {{ background: #73bf69; color: #fff; }}
        .btn-primary:hover {{ background: #5a9e52; }}
        .btn-share {{ background: #6ed0ff; color: #1a1a2e; }}
        .btn-share:hover {{ background: #4db8e8; }}
        .btn-share.hidden {{ display: none; }}
        .footer {{ padding: 12px 24px; border-top: 1px solid #2a2d35; font-size: 12px; color: #555; }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{name}</h1>
        <div class="gpx">GPX: {info.get('gpx_path', '')}</div>
    </div>
    <div id="map"></div>
    <div class="cards">{cards_html}</div>
    {wp_html}
    <div class="actions">
        <button class="btn btn-primary" onclick="downloadGPX()">📥 Download GPX</button>
        <button class="btn btn-share hidden" id="shareBtn" onclick="shareGPX()">📤 Share to App</button>
    </div>
    <div class="footer">Generated by VeloMate</div>
    <script>
        var coords = [{coord_js}];
        var map = L.map('map');
        L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
            attribution: '&copy; OpenStreetMap'
        }}).addTo(map);
        var route = L.polyline(coords, {{color: '#6ed0ff', weight: 4, opacity: 0.8}}).addTo(map);
        map.fitBounds(route.getBounds().pad(0.1));
        if (coords.length > 0) {{
            L.circleMarker(coords[0], {{radius: 8, color: '#73bf69', fillColor: '#73bf69', fillOpacity: 1}})
                .addTo(map).bindPopup('<b>Start / End</b>');
        }}
        {markers_js}

        // GPX content embedded for download/share
        var gpxContent = `{gpx_js}`;
        var gpxFilename = '{gpx_filename}';

        function downloadGPX() {{
            var blob = new Blob([gpxContent], {{type: 'application/gpx+xml'}});
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = gpxFilename;
            a.click();
            URL.revokeObjectURL(url);
        }}

        async function shareGPX() {{
            try {{
                var file = new File([gpxContent], gpxFilename, {{type: 'application/gpx+xml'}});
                await navigator.share({{
                    title: '{name}',
                    files: [file],
                }});
            }} catch (err) {{
                if (err.name !== 'AbortError') {{
                    // Real error — fall back to download
                    downloadGPX();
                }}
                // AbortError = user cancelled share sheet — do nothing
            }}
        }}

        // Show share button on iOS/Android (Web Share API with file support)
        if (navigator.canShare && navigator.canShare({{files: [new File([''], 'test.gpx')]}})) {{
            document.getElementById('shareBtn').classList.remove('hidden');
        }}
    </script>
</body>
</html>"""

    if output_dir:
        import re
        import unicodedata
        normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
        # Strip leading "VeloMate" prefix to avoid double "velomate-velomate-"
        clean = re.sub(r"(?i)^velomate[\s\-_]*", "", normalized).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")
        slug = slug[:80]
        filename = f"velomate-{slug}.html"
        path = os.path.join(output_dir, filename)
        os.makedirs(output_dir, exist_ok=True)
        with open(path, "w") as f:
            f.write(page)
    else:
        fd, path = tempfile.mkstemp(suffix=".html", prefix="velomate_preview_")
        with os.fdopen(fd, "w") as f:
            f.write(page)
        webbrowser.open(f"file://{path}")

    return path
