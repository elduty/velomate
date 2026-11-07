#!/usr/bin/env python3
"""Run every test module that needs a real PostgreSQL, each in its own process.

Test modules change process-wide state such as sys.modules, so a process each keeps
a result about the database from depending on what ran before it. They are found by
the variable they read, so a new one cannot be left out by forgetting to list it,
and a skip fails the run: a database was supplied, so a skip means a protection did
not run.

    VELOMATE_TEST_DATABASE_URL=postgresql://... python3 scripts/run_pg_tests.py

The modules wipe that database, so "test" has to start a word in its name, as in
velomate_test.

--local-server is for a CI runner that cannot start a service container. It starts
a throwaway server from the PostgreSQL installed on the runner, and when there is
none it warns and exits 0: the rest of the suite still reports, and the tests start
running as soon as a server is installed.
"""
import argparse
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

DATABASE_VARIABLE = "VELOMATE_TEST_DATABASE_URL"
TESTS_DIR = Path(__file__).resolve().parent.parent / "tests"

# postgresql@15 is keg-only on Homebrew, so installing it does not put it on PATH.
KEG_DIRS = (Path("/opt/homebrew/opt/postgresql@15/bin"),
            Path("/usr/local/opt/postgresql@15/bin"))
LOCAL_USER = "velomate"
LOCAL_DATABASE = "velomate_test"


def discover(tests_dir):
    """The test modules that read the database variable."""
    return sorted(path for path in tests_dir.glob("test_*.py")
                  if DATABASE_VARIABLE in path.read_text(encoding="utf-8"))


def _run_module(module, url):
    """Run one module in its own process. Returns what went wrong, or None."""
    print(f"\n=== {module.name}", flush=True)
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "report.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(module), "-q", "-rs", "--tb=short",
             "-p", "no:cacheprovider", f"--junitxml={report}"],
            env={**os.environ, DATABASE_VARIABLE: url})
        if result.returncode != 0:
            return f"pytest exited {result.returncode}"
        skipped = int(ET.parse(report).getroot().find("testsuite").get("skipped"))
    return f"{skipped} test(s) skipped" if skipped else None


def run_modules(modules, url):
    """Run every module, even after a failure. Returns the exit code."""
    problems = {module: _run_module(module, url) for module in modules}

    print("\n=== real-PostgreSQL modules")
    for module, problem in problems.items():
        print(f"  {'FAILED' if problem else 'ok':<6} {module.name}"
              + (f": {problem}" if problem else ""))
    return 1 if any(problems.values()) else 0


def server_version(url):
    """The version of the server at url. Raises if it cannot be reached."""
    import psycopg2

    connection = psycopg2.connect(url)
    try:
        with connection.cursor() as cur:
            cur.execute("SHOW server_version")
            return cur.fetchone()[0]
    finally:
        connection.close()


def _run_against(modules, url):
    try:
        version = server_version(url)
    except Exception as e:
        print(f"Could not reach the database in {DATABASE_VARIABLE}: {e}")
        return 1
    print(f"PostgreSQL server: {version}")
    return run_modules(modules, url)


def find_server_bin():
    """The directory holding pg_ctl on this machine, or None."""
    for directory in KEG_DIRS:
        if (directory / "pg_ctl").exists():
            return directory
    found = shutil.which("pg_ctl")
    return Path(found).parent if found else None


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def local_server(bin_dir):
    """A throwaway server on a free port, gone when the block ends. Yields its URL."""
    root = Path(tempfile.mkdtemp(prefix="velomate-pg-"))
    data, log = root / "data", root / "server.log"
    port = _free_port()
    options = (f"-p {port} -c listen_addresses=127.0.0.1 "
               "-c unix_socket_directories= -c fsync=off")
    started = False
    try:
        try:
            subprocess.run([bin_dir / "initdb", "-D", data, "-U", LOCAL_USER,
                            "--auth=trust", "-E", "UTF8", "--locale=C"],
                           check=True, stdout=subprocess.DEVNULL)
            subprocess.run([bin_dir / "pg_ctl", "-D", data, "-l", log, "-w",
                            "-o", options, "start"],
                           check=True, stdout=subprocess.DEVNULL)
            started = True
            subprocess.run([bin_dir / "createdb", "-h", "127.0.0.1", "-p", str(port),
                            "-U", LOCAL_USER, LOCAL_DATABASE], check=True)
        except subprocess.CalledProcessError:
            if log.exists():
                print(log.read_text(encoding="utf-8", errors="replace")[-2000:])
            raise
        yield f"postgresql://{LOCAL_USER}@127.0.0.1:{port}/{LOCAL_DATABASE}"
    finally:
        if started:
            subprocess.run([bin_dir / "pg_ctl", "-D", data, "-m", "immediate", "-w",
                            "stop"], check=False, stdout=subprocess.DEVNULL)
        shutil.rmtree(root, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--local-server", action="store_true",
                        help="start a throwaway server from the local PostgreSQL")
    args = parser.parse_args(argv)

    modules = discover(TESTS_DIR)
    if not modules:
        print(f"No module in {TESTS_DIR} reads {DATABASE_VARIABLE}: discovery is broken.")
        return 1
    names = ", ".join(module.name for module in modules)
    print(f"Real-PostgreSQL test modules: {names}")

    if args.local_server:
        bin_dir = find_server_bin()
        if bin_dir is None:
            print("::warning::No PostgreSQL server on this runner, so these "
                  f"real-PostgreSQL test modules did NOT run: {names}")
            return 0
        print(f"Starting a throwaway server from {bin_dir}", flush=True)
        with local_server(bin_dir) as url:
            return _run_against(modules, url)

    url = os.environ.get(DATABASE_VARIABLE)
    if not url:
        print(f"{DATABASE_VARIABLE} is not set, so nothing can run.")
        return 1
    return _run_against(modules, url)


if __name__ == "__main__":
    sys.exit(main())
