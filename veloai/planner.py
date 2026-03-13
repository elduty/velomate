from datetime import datetime
from typing import Dict, List


def _recommend_routes(tours: List[Dict], fitness: Dict) -> List[Dict]:
    """Pick routes that match fitness and conditions."""
    if not tours:
        return []

    max_dist = fitness["max_recommended_distance"]
    max_elev = fitness["max_recommended_elevation"]

    scored = []
    for t in tours:
        dist_km = t["distance"] / 1000
        elev = t.get("elevation_up", 0)

        # Skip if too hard
        if dist_km > max_dist * 1.3 or elev > max_elev * 1.3:
            continue

        # Prefer routes within comfort zone
        dist_fit = 1 - min(abs(dist_km - max_dist * 0.8) / max_dist, 1)
        elev_fit = 1 - min(abs(elev - max_elev * 0.6) / max_elev, 1) if max_elev > 0 else 0.5

        score = dist_fit * 0.6 + elev_fit * 0.4
        scored.append((score, t))

    scored.sort(key=lambda x: -x[0])

    # Return top 3 distinct routes
    seen = set()
    results = []
    for score, t in scored:
        dist_bucket = round(t["distance"] / 1000)
        elev_bucket = round(t.get("elevation_up", 0) / 50) * 50
        key = (dist_bucket, elev_bucket)
        if key in seen:
            continue
        seen.add(key)
        results.append(t)
        if len(results) >= 3:
            break

    return results


def recommend(days: List[Dict], tours: List[Dict], fitness: Dict) -> str:
    """Return formatted WhatsApp-friendly recommendation string."""
    best_days = [d for d in days if d["score"] >= 60]
    routes = _recommend_routes(tours, fitness)

    lines = []
    lines.append("\U0001f6b4 *Ride Planner \u2014 This Week*")
    lines.append("")

    # Weather overview
    lines.append("*\U0001f4c5 Weather Forecast*")
    for day in days:
        emoji = "\u2600\ufe0f" if day["score"] >= 80 else "\U0001f324" if day["score"] >= 60 else "\U0001f325" if day["score"] >= 40 else "\U0001f327"
        stars = "\u2b50" * (day["score"] // 20)
        line = f"  \u2022 {day['day_name'][:3]} {day['date'][5:]}: {emoji} {day['weather']}, {day['temp_min']:.0f}\u2013{day['temp_max']:.0f}\u00b0C, wind {day['wind']:.0f} km/h"
        if day["precip"] > 0:
            line += f", rain {day['precip']:.1f}mm"
        line += f" [{stars}]"
        lines.append(line)

    lines.append("")

    # Fitness summary
    lines.append("*\U0001f3cb\ufe0f Recent Fitness (4 weeks)*")
    lines.append(f"  \u2022 Level: {fitness['level']}")
    lines.append(f"  \u2022 Rides: {fitness['real_rides']} outdoor + {fitness['virtual_rides']} indoor")
    lines.append(f"  \u2022 Total: {fitness['total_distance_km']} km, {fitness['total_elevation_m']}m elevation")
    if fitness.get("last_ride", "N/A") != "N/A":
        lines.append(f"  \u2022 Last ride: {fitness['last_ride']}")
    lines.append(f"  \u2022 Suggested max: ~{fitness['max_recommended_distance']}km / ~{fitness['max_recommended_elevation']}m gain")
    lines.append("")

    # Best days
    lines.append("*\U0001f3af Recommendation*")
    if best_days:
        day_list = ", ".join(f"*{d['day_name']}*" for d in best_days[:3])
        lines.append(f"  Best day(s) to ride: {day_list}")
    else:
        lines.append("  \u26a0\ufe0f No great days this week \u2014 all have rain or heavy wind")
        sorted_days = sorted(days, key=lambda d: -d["score"])
        if sorted_days:
            lines.append(f"  Least bad option: *{sorted_days[0]['day_name']}* (score {sorted_days[0]['score']}/100)")
    lines.append("")

    # Route suggestions
    if routes:
        lines.append("*\U0001f5fa Route Suggestions*")
        for i, r in enumerate(routes, 1):
            dist = r["distance"] / 1000
            elev = r.get("elevation_up", 0)
            sport = r.get("sport", "cycling").replace("touringbicycle", "touring bike")
            date = r.get("date", "")[:10]
            url = f"https://www.komoot.com/tour/{r['id']}"
            lines.append(f"  {i}. *{r['name']}* \u2014 {dist:.1f}km, +{elev:.0f}m ({sport})")
            lines.append(f"     Last done: {date}")
            lines.append(f"     {url}")
    else:
        lines.append("  No matching routes found in Komoot history")

    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines)
