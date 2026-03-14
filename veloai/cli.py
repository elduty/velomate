import argparse
import sys
import warnings

from veloai import komoot, weather, planner
from veloai.config import load as load_config

warnings.filterwarnings("ignore")


def cmd_recommend(args):
    """Weekly ride recommendation (existing behavior)."""
    cfg = load_config()
    home = cfg["home"]

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
    days = weather.fetch_forecast(home["lat"], home["lng"])

    if not days:
        print("Weather unavailable — skipping recommendation", file=sys.stderr)
        return

    print(planner.recommend(days, tours, fitness=fitness))


def cmd_plan(args):
    """Plan a route and open in Komoot."""
    from veloai.route_planner import plan

    cfg = load_config()
    home = cfg["home"]

    if args.start:
        parts = args.start.split(",")
        home_lat, home_lng = float(parts[0]), float(parts[1])
    else:
        home_lat, home_lng = home["lat"], home["lng"]

    result = plan(
        duration_str=args.duration,
        surface=args.surface,
        loop=args.loop,
        waypoints_str=args.waypoints,
        date_str=args.date,
        time_str=args.time,
        home_lat=home_lat,
        home_lng=home_lng,
    )
    print(result)


def main():
    parser = argparse.ArgumentParser(
        prog="veloai",
        description="VeloAI — cycling data platform CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Plan subcommand
    plan_parser = subparsers.add_parser("plan", help="Plan a route on Komoot")
    plan_parser.add_argument("--duration", "-d", required=True, help="Ride duration (e.g. 2h, 1h30m, 90min)")
    plan_parser.add_argument("--surface", "-s", default="gravel", choices=["road", "gravel", "mtb"], help="Surface type (default: gravel)")
    plan_parser.add_argument("--loop", "-l", action="store_true", default=True, help="Round-trip (default: true)")
    plan_parser.add_argument("--no-loop", action="store_false", dest="loop", help="One-way route")
    plan_parser.add_argument("--waypoints", "-w", default=None, help="Comma-separated place names to route through")
    plan_parser.add_argument("--date", default="tomorrow", help="When to ride (default: tomorrow)")
    plan_parser.add_argument("--time", "-t", default=None, help="Start time (e.g. 14:00, 2pm, 9am)")
    plan_parser.add_argument("--start", default=None, help="Start location as 'lat,lng' (default: from config)")

    args = parser.parse_args()

    if args.command == "plan":
        cmd_plan(args)
    elif args.command is None:
        # No subcommand → run existing recommendation (backward compatible)
        cmd_recommend(args)
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
