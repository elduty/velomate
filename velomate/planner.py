from datetime import datetime
from typing import Dict, List, Optional


def _top_routes(tours: List[Dict], n: int = 3, tsb: float = None) -> List[Dict]:
    """Return top N routes, deduplicated by distance/elevation bucket.
    If TSB provided, prefer shorter/flatter when fatigued, longer/hillier when fresh.
    """
    seen = set()
    results = []
    # Sort by most recently ridden first
    sorted_tours = sorted(tours, key=lambda x: x.get("date", ""), reverse=True)

    # If we have TSB, adjust preferred route difficulty
    if tsb is not None and tsb < -10:
        # Fatigued — prefer shorter/flatter routes (sort by distance ascending)
        sorted_tours = sorted(sorted_tours, key=lambda x: x.get("distance", 0))
    elif tsb is not None and tsb > 10:
        # Fresh — prefer longer/hillier routes (sort by distance descending)
        sorted_tours = sorted(sorted_tours, key=lambda x: x.get("distance", 0), reverse=True)

    for t in sorted_tours:
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


def _form_note(fitness: Dict) -> Optional[str]:
    """Return a form note based on TSB."""
    tsb = fitness.get("tsb")
    if tsb is None:
        return None
    tsb = float(tsb)
    if tsb > 10:
        return f"🟢 You're fresh (TSB +{tsb:.0f}) — good day to push hard"
    elif tsb > -10:
        return f"🟡 Form is neutral (TSB {tsb:+.0f}) — normal ride day"
    else:
        return f"🔴 You're fatigued (TSB {tsb:.0f}) — take it easy or rest"


def recommend(days: List[Dict], tours: List[Dict], fitness: Dict = None) -> str:
    """Return formatted WhatsApp-friendly recommendation string."""
    if fitness is None:
        fitness = {}

    tsb = fitness.get("tsb")
    best_days = [d for d in days if d["score"] >= 60]
    routes = _top_routes(tours, tsb=float(tsb) if tsb is not None else None)

    lines = []
    lines.append("🚴 *Ride Planner — This Week*")
    lines.append("")

    # Fitness section
    form_note = _form_note(fitness)
    if form_note or fitness:
        lines.append("*💪 Fitness*")
        if fitness.get("ctl") is not None and fitness.get("atl") is not None:
            lines.append(f"  • Fitness (CTL): {float(fitness['ctl']):.1f}")
            lines.append(f"  • Fatigue (ATL): {float(fitness['atl']):.1f}")
            lines.append(f"  • Form (TSB): {float(fitness.get('tsb', 0)):+.1f}")
        if form_note:
            lines.append(f"  {form_note}")
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
        if tsb is not None and float(tsb) < -10:
            lines.append("  _Showing easier routes (you're fatigued)_")
        elif tsb is not None and float(tsb) > 10:
            lines.append("  _Showing challenging routes (you're fresh!)_")
        for i, r in enumerate(routes, 1):
            dist = r["distance"] / 1000
            elev = r.get("elevation_up", 0)
            sport = r.get("sport", "cycling")
            date = r.get("date", "")[:10]
            lines.append(f"  {i}. *{r['name']}* — {dist:.1f}km, +{elev:.0f}m ({sport})")
            lines.append(f"     Last done: {date}")
    else:
        lines.append("  No routes found")

    lines.append("")
    lines.append(f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    return "\n".join(lines)
