from datetime import datetime
from typing import Dict, List


def _top_routes(tours: List[Dict], n: int = 3) -> List[Dict]:
    """Return top N routes, deduplicated by distance/elevation bucket."""
    seen = set()
    results = []
    # Sort by most recently ridden first
    for t in sorted(tours, key=lambda x: x.get("date", ""), reverse=True):
        dist_bucket = round(t["distance"] / 1000)
        elev_bucket = round(t.get("elevation_up", 0) / 50) * 50
        key = (dist_bucket, elev_bucket)
        if key in seen:
            continue
        seen.add(key)
        results.append(t)
        if len(results) >= n:
            break
    return results


def recommend(days: List[Dict], tours: List[Dict]) -> str:
    """Return formatted WhatsApp-friendly recommendation string."""
    best_days = [d for d in days if d["score"] >= 60]
    routes = _top_routes(tours)

    lines = []
    lines.append("🚴 *Ride Planner — This Week*")
    lines.append("")

    # Weather overview
    lines.append("*📅 Weather Forecast*")
    for day in days:
        emoji = "☀️" if day["score"] >= 80 else "🌤" if day["score"] >= 60 else "🌥" if day["score"] >= 40 else "🌧"
        stars = "⭐" * (day["score"] // 20)
        line = f"  • {day['day_name'][:3]} {day['date'][5:]}: {emoji} {day['weather']}, {day['temp_min']:.0f}–{day['temp_max']:.0f}°C, wind {day['wind']:.0f} km/h"
        if day["precip"] > 0:
            line += f", rain {day['precip']:.1f}mm"
        line += f" [{stars}]"
        lines.append(line)

    lines.append("")

    # Best days
    lines.append("*🎯 Recommendation*")
    if best_days:
        day_list = ", ".join(f"*{d['day_name']}*" for d in best_days[:3])
        lines.append(f"  Best day(s) to ride: {day_list}")
    else:
        lines.append("  ⚠️ No great days this week — all have rain or heavy wind")
        sorted_days = sorted(days, key=lambda d: -d["score"])
        if sorted_days:
            lines.append(f"  Least bad option: *{sorted_days[0]['day_name']}* (score {sorted_days[0]['score']}/100)")
    lines.append("")

    # Route suggestions
    if routes:
        lines.append("*🗺 Route Suggestions*")
        for i, r in enumerate(routes, 1):
            dist = r["distance"] / 1000
            elev = r.get("elevation_up", 0)
            sport = r.get("sport", "cycling").replace("touringbicycle", "touring bike")
            date = r.get("date", "")[:10]
            url = f"https://www.komoot.com/tour/{r['id']}"
            lines.append(f"  {i}. *{r['name']}* — {dist:.1f}km, +{elev:.0f}m ({sport})")
            lines.append(f"     Last done: {date}")
            lines.append(f"     {url}")
    else:
        lines.append("  No routes found in Komoot")

    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines)
