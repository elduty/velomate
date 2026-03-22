import argparse
import sys
import warnings

from velomate import weather, planner
from velomate.config import load as load_config

# Suppress noisy DeprecationWarnings from mapbox_vector_tile's protobuf dependency
warnings.filterwarnings("ignore", category=DeprecationWarning, module="mapbox_vector_tile")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.protobuf")


def cmd_recommend(args):
    """Weekly ride recommendation based on fitness + weather + past routes."""
    cfg = load_config()
    home = cfg["home"]

    fitness = {}
    tours = []

    try:
        from velomate.db import get_connection, get_latest_fitness, get_routes
        conn = get_connection()
        if conn:
            try:
                print("Connected to VeloMate DB", file=sys.stderr)
                fitness = get_latest_fitness(conn)
                tours = get_routes(conn) or []
                print(f"  → {len(tours)} routes from DB", file=sys.stderr)
                if fitness:
                    print(f"  → Fitness: CTL={fitness.get('ctl', '?')}, ATL={fitness.get('atl', '?')}, TSB={fitness.get('tsb', '?')}", file=sys.stderr)
            finally:
                conn.close()
        else:
            print("DB unavailable", file=sys.stderr)
    except Exception as e:
        print(f"DB error: {e}", file=sys.stderr)

    if not tours:
        print("No routes found in database — run the ingestor first", file=sys.stderr)
        return

    print("Fetching weather forecast...", file=sys.stderr)
    days = weather.fetch_forecast(home["lat"], home["lng"])

    if not days:
        print("Weather unavailable — skipping recommendation", file=sys.stderr)
        return

    print(planner.recommend(days, tours, fitness=fitness))


def cmd_plan(args):
    """Plan a cycling route with weather and intelligence enrichment."""
    from velomate.route_planner import plan

    cfg = load_config()
    home = cfg["home"]

    if args.start:
        parts = args.start.split(",")
        if len(parts) != 2:
            print("Error: --start must be 'lat,lng' (e.g. '38.72,-9.14')", file=sys.stderr)
            return
        try:
            home_lat, home_lng = float(parts[0]), float(parts[1])
        except ValueError:
            print("Error: --start must be 'lat,lng' with numeric values", file=sys.stderr)
            return
    else:
        home_lat, home_lng = home["lat"], home["lng"]

    result = plan(
        duration_str=args.duration,
        distance_str=args.distance,
        surface=args.surface,
        loop=args.loop,
        waypoints_str=args.waypoints,
        date_str=args.date,
        time_str=args.time,
        home_lat=home_lat,
        home_lng=home_lng,
        preference=args.preference,
        safety=args.safety,
        output_dir=args.output,
    )
    print(result)


def main():
    parser = argparse.ArgumentParser(
        prog="velomate",
        description="VeloMate — cycling data platform CLI",
    )
    subparsers = parser.add_subparsers(dest="command")

    # Plan subcommand
    plan_parser = subparsers.add_parser("plan", help="Plan a cycling route")
    target = plan_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--duration", "-d", help="Ride duration (e.g. 2h, 1h30m, 90min)")
    target.add_argument("--distance", "-k", help="Target distance in km (e.g. 30, 50km)")
    plan_parser.add_argument("--surface", "-s", default="road", choices=["road", "gravel", "mtb"], help="Surface type (default: road)")
    plan_parser.add_argument("--loop", "-l", action="store_true", default=True, help="Round-trip (default: true)")
    plan_parser.add_argument("--no-loop", action="store_false", dest="loop", help="One-way route")
    plan_parser.add_argument("--waypoints", "-w", default=None, help="Comma-separated place names to route through")
    plan_parser.add_argument("--date", default="tomorrow", help="When to ride (default: tomorrow)")
    plan_parser.add_argument("--time", "-t", default=None, help="Start time (e.g. 14:00, 2pm, 9am)")
    plan_parser.add_argument("--start", default=None, help="Start location as 'lat,lng' (default: from config)")
    plan_parser.add_argument("--preference", "-p", default="variety", choices=["variety", "comfort"], help="Route preference: variety (new roads) or comfort (familiar roads)")
    plan_parser.add_argument("--safety", default=0.5, type=float, help="Safety level 0.0-1.0: 0=fastest, 0.5=balanced, 1.0=safest (default: 0.5)")
    plan_parser.add_argument("--output", "-o", default=None, metavar="DIR", help="Save preview HTML to this directory instead of opening in browser")

    args = parser.parse_args()

    if args.command == "plan":
        cmd_plan(args)
    elif args.command is None:
        cmd_recommend(args)
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
