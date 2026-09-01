"""Display-unit conversion — single source of truth for metric→imperial.

Storage stays SI everywhere; this module only formats values for display
(CLI output) and provides the Grafana unit-id mapping used by the dashboard
generator (scripts/gen_imperial_dashboards.py).
"""

KM_TO_MI = 0.621371
M_TO_FT = 3.28084
KMH_TO_MPH = 0.621371


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# Grafana unit id (metric) -> (imperial unit id, multiply factor on the value)
GRAFANA_UNIT_MAP = {
    "lengthkm": ("lengthmi", KM_TO_MI),
    "lengthm": ("lengthft", M_TO_FT),
    "velocitykmh": ("velocitymph", KMH_TO_MPH),
}


def normalize_system(system) -> str:
    """Return 'imperial' or 'metric'; anything else -> 'metric'."""
    return "imperial" if str(system).strip().lower() == "imperial" else "metric"


def format_distance(km: float, system: str) -> str:
    if normalize_system(system) == "imperial":
        return f"{km * KM_TO_MI:.1f} mi"
    return f"{km:.1f} km"


def format_elevation(m: float, system: str) -> str:
    if normalize_system(system) == "imperial":
        return f"{round(m * M_TO_FT)} ft"
    return f"{round(m)} m"


def format_speed(kmh: float, system: str) -> str:
    if normalize_system(system) == "imperial":
        return f"{round(kmh * KMH_TO_MPH)} mph"
    return f"{round(kmh)} km/h"


def format_temp(c: float, system: str) -> str:
    if normalize_system(system) == "imperial":
        return f"{round(c_to_f(c))}°F"
    return f"{round(c)}°C"


MM_TO_IN = 0.0393701


def format_precip(mm: float, system: str) -> str:
    if normalize_system(system) == "imperial":
        return f"{mm * MM_TO_IN:.1f} in"
    return f"{mm:.1f} mm"
