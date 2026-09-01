import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingestor"))

# Import the real `requests` first, when it is installed.
#
# Several test modules stand in a MagicMock for it so the suite can run without
# it. pytest imports every test module into one process, so whichever module is
# imported first wins for the whole session — and since nothing has imported
# `requests` at that point, even `setdefault` installs the mock. The mock then
# leaks into unrelated modules, where `except requests.RequestException` is no
# longer a real exception class and comparisons against MagicMock raise
# TypeError. Loading the genuine module here makes every later `setdefault` a
# no-op, while an environment genuinely missing it still falls back to the mock.
#
# Only `requests`: psycopg2 and schedule are deliberately left mocked, because
# tests assert against the mock's call records rather than any real behaviour.
try:
    import requests  # noqa: F401
except ImportError:
    pass
