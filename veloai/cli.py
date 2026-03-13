import sys
import warnings

from veloai import komoot, weather, planner

warnings.filterwarnings("ignore")

LOCATION = {"lat": 38.69, "lon": -9.32, "name": "São Domingos de Rana"}


def main():
    print("Fetching Komoot tours...", file=sys.stderr)
    tours = komoot.fetch_tours()
    print(f"  → {len(tours)} cycling tours", file=sys.stderr)

    print("Fetching weather forecast...", file=sys.stderr)
    days = weather.fetch_forecast(LOCATION["lat"], LOCATION["lon"])

    print(planner.recommend(days, tours))


if __name__ == "__main__":
    main()
