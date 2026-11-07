#!/usr/bin/env python3
"""Import every ingestor module, with the network blocked and no database.

On the image's Python with only ingestor/requirements.txt installed, this fails
where the container would: on syntax that Python rejects, or on a dependency that
file leaves out. It imports every module rather than only main because several are
imported inside a function, so a broken one would otherwise surface mid-sync.
"""
import importlib
import os
import socket
import sys
from pathlib import Path

INGESTOR = Path(__file__).resolve().parent.parent / "ingestor"


def _refuse(*args, **kwargs):
    raise RuntimeError("importing the ingestor must not touch the network")


def main(directory):
    modules = sorted(path.stem for path in directory.glob("*.py"))
    if not modules:
        print(f"No modules in {directory}")
        return 1

    os.environ.pop("DATABASE_URL", None)
    socket.socket.connect = _refuse
    socket.getaddrinfo = _refuse
    sys.path.insert(0, str(directory))

    print(f"Python {sys.version.split()[0]}")
    for name in modules:
        importlib.import_module(name)
        print(f"imported {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else INGESTOR))
