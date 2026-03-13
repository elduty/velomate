import sys
import warnings

from veloai import komoot, strava, weather, planner

warnings.filterwarnings("ignore")

LOCATION = {"lat": 38.69, "lon": -9.32, "name": "São Domingos de Rana"}


def main():
    print("Fetching Komoot tours...", file=sys.stderr)
    tours = komoot.fetch_tours()
    print(f"  → {len(tours)} cycling tours", file=sys.stderr)

    print("Fetching weather forecast...", file=sys.stderr)
    days = weather.fetch_forecast(LOCATION["lat"], LOCATION["lon"])

    print("Fetching Strava activities...", file=sys.stderr)
    fitness = strava.get_fitness_level()
    print(f"  → Fitness level: {fitness['level']}", file=sys.stderr)

    print(planner.recommend(days, tours, fitness))


if __name__ == "__main__":
    main()
