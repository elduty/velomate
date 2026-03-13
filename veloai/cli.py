import sys
import warnings

from veloai import komoot, weather, planner

warnings.filterwarnings("ignore")

LOCATION = {"lat": 38.69, "lon": -9.32, "name": "São Domingos de Rana"}


def main():
    fitness = {}
    tours = None

    # Try DB first
    try:
        from veloai.db import get_connection, get_latest_fitness, get_routes
        conn = get_connection()
        if conn:
            try:
                print("Connected to VeloAI DB", file=sys.stderr)
                fitness = get_latest_fitness(conn)
                db_routes = get_routes(conn)
                if db_routes:
                    tours = db_routes
                    print(f"  → {len(tours)} routes from DB", file=sys.stderr)
                if fitness:
                    print(f"  → Fitness: CTL={fitness.get('ctl', '?')}, ATL={fitness.get('atl', '?')}, TSB={fitness.get('tsb', '?')}", file=sys.stderr)
            finally:
                conn.close()
        else:
            print("DB unavailable, falling back to Komoot API", file=sys.stderr)
    except Exception as e:
        print(f"DB error ({e}), falling back to Komoot API", file=sys.stderr)

    # Fall back to Komoot API if no DB routes
    if tours is None:
        print("Fetching Komoot tours...", file=sys.stderr)
        tours = komoot.fetch_tours()
        print(f"  → {len(tours)} cycling tours", file=sys.stderr)

    print("Fetching weather forecast...", file=sys.stderr)
    days = weather.fetch_forecast(LOCATION["lat"], LOCATION["lon"])

    if not days:
        print("Weather unavailable — skipping recommendation", file=sys.stderr)
        return

    print(planner.recommend(days, tours, fitness=fitness))


if __name__ == "__main__":
    main()
