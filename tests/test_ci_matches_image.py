"""CI has to test what ships: the image's Python, and the image's dependencies."""
import re
import subprocess
import sys
from importlib.machinery import PathFinder
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SMOKE = ROOT / "scripts" / "smoke_import_ingestor.py"


def test_ci_runs_the_python_the_image_runs():
    dockerfile = (ROOT / "ingestor" / "Dockerfile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text()
    image = re.search(r"^FROM python:(\d+\.\d+)", dockerfile, re.MULTILINE).group(1)

    assert re.findall(r'python-version: "([\d.]+)"', workflow) == [image]
    assert re.findall(r"python(\d+\.\d+) -m venv", workflow) == [image]


def test_the_suite_runs_on_the_real_psycopg2():
    """Every test module sets a mock as a default, so without the import in conftest
    the first module to run would decide this for the whole session."""
    if PathFinder.find_spec("psycopg2") is None:
        pytest.skip("psycopg2 is not installed here, so the mock is the only option")

    assert hasattr(sys.modules["psycopg2"], "__file__")
    assert hasattr(sys.modules["psycopg2.extras"], "__file__")


@pytest.mark.parametrize("first, second", [
    ("test_fitness.py", "test_fitness_recalc.py"),
    ("test_fitness_recalc.py", "test_audit_fixes.py"),
])
def test_modules_that_once_depended_on_their_order(first, second):
    """Alphabetical order hid both, so they cannot be seen from inside the suite."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / first),
         str(ROOT / "tests" / second), "-q", "--tb=line", "-p", "no:cacheprovider"],
        capture_output=True, text=True)

    assert result.returncode == 0, result.stdout[-1500:]


def _smoke(directory):
    return subprocess.run([sys.executable, str(SMOKE), str(directory)],
                          capture_output=True, text=True)


class TestSmokeImport:
    def test_imports_every_module_not_only_the_entry_point(self, tmp_path):
        """A module the entry point only imports inside a function is the one a
        plain `import main` would never reach."""
        (tmp_path / "main.py").write_text("def run():\n    import lazy\n")
        (tmp_path / "lazy.py").write_text("import a_package_that_is_not_installed\n")

        result = _smoke(tmp_path)

        assert result.returncode != 0
        assert "a_package_that_is_not_installed" in result.stderr

    def test_passes_when_every_module_imports(self, tmp_path):
        (tmp_path / "main.py").write_text("VALUE = 1\n")
        (tmp_path / "other.py").write_text("VALUE = 2\n")

        result = _smoke(tmp_path)

        assert result.returncode == 0
        assert "imported main" in result.stdout
        assert "imported other" in result.stdout

    def test_does_not_run_the_entry_point(self, tmp_path):
        (tmp_path / "main.py").write_text(
            'if __name__ == "__main__":\n    raise SystemExit("started")\n')

        assert _smoke(tmp_path).returncode == 0

    def test_an_import_that_connects_fails(self, tmp_path):
        (tmp_path / "main.py").write_text(
            "import socket\n"
            "socket.socket().connect(('127.0.0.1', 9))\n")

        result = _smoke(tmp_path)

        assert result.returncode != 0
        assert "must not touch the network" in result.stderr

    def test_an_import_that_resolves_a_name_fails(self, tmp_path):
        (tmp_path / "main.py").write_text(
            "import socket\nsocket.getaddrinfo('example.invalid', 80)\n")

        result = _smoke(tmp_path)

        assert result.returncode != 0
        assert "must not touch the network" in result.stderr

    def test_finding_no_modules_is_a_failure(self, tmp_path):
        """Otherwise a wrong path reads as a clean pass."""
        assert _smoke(tmp_path).returncode != 0
