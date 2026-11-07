"""scripts/run_pg_tests.py, run on throwaway test modules.

CI sees these tests only through this script's exit code, so what matters is that
each way of not really running them comes out red.
"""
import importlib.util
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "run_pg_tests", ROOT / "scripts" / "run_pg_tests.py")
run_pg_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_pg_tests)

URL = "postgresql://nobody@127.0.0.1:1/never_connected_test"


VARIABLE = run_pg_tests.DATABASE_VARIABLE


def _module(directory, name, body):
    path = directory / name
    path.write_text(f"# reads {VARIABLE}\n" + textwrap.dedent(body))
    return path


def _passing(directory, name="test_ok.py"):
    return _module(directory, name, """
        def test_ok():
            assert True
    """)


class TestDiscovery:
    def test_finds_modules_by_the_variable_they_read(self, tmp_path):
        wanted = _passing(tmp_path)
        (tmp_path / "test_mock_only.py").write_text("def test_x():\n    assert True\n")
        (tmp_path / "pg_helper.py").write_text(f"# {VARIABLE}\n")

        assert run_pg_tests.discover(tmp_path) == [wanted]

    def test_finds_the_modules_in_this_repository(self):
        names = {path.name for path in run_pg_tests.discover(ROOT / "tests")}

        assert "test_route_id_stability.py" in names
        assert "test_run_pg_tests.py" not in names


class TestRunModules:
    def test_passing_modules_are_a_pass(self, tmp_path):
        assert run_pg_tests.run_modules([_passing(tmp_path)], URL) == 0

    def test_a_failing_module_fails_the_run(self, tmp_path):
        module = _module(tmp_path, "test_bad.py", """
            def test_bad():
                assert False
        """)

        assert run_pg_tests.run_modules([module], URL) == 1

    def test_a_skip_fails_the_run(self, tmp_path, capsys):
        """A database was supplied, so a skip means a protection did not run."""
        module = _module(tmp_path, "test_skips.py", """
            import pytest

            def test_skipped():
                pytest.skip("no database")
        """)

        assert run_pg_tests.run_modules([module], URL) == 1
        assert "test_skips.py" in capsys.readouterr().out

    def test_a_module_with_no_tests_fails_the_run(self, tmp_path):
        module = _module(tmp_path, "test_empty.py", "VALUE = 1\n")

        assert run_pg_tests.run_modules([module], URL) == 1

    def test_a_failure_does_not_stop_later_modules(self, tmp_path):
        marker = tmp_path / "second_ran"
        first = _module(tmp_path, "test_a.py", """
            def test_bad():
                assert False
        """)
        second = _module(tmp_path, "test_b.py", f"""
            from pathlib import Path

            def test_marks():
                Path({str(marker)!r}).touch()
        """)

        assert run_pg_tests.run_modules([first, second], URL) == 1
        assert marker.exists()

    def test_each_module_gets_its_own_process(self, tmp_path):
        """What the script exists for: a mock installed in sys.modules by one
        module must not reach the next."""
        first = _module(tmp_path, "test_a.py", """
            import sys

            def test_installs_a_mock():
                sys.modules["psycopg2"] = object()
        """)
        second = _module(tmp_path, "test_b.py", """
            import sys

            def test_sees_no_mock():
                assert "psycopg2" not in sys.modules
        """)

        assert run_pg_tests.run_modules([first, second], URL) == 0

    def test_the_module_is_handed_the_database(self, tmp_path):
        module = _module(tmp_path, "test_url.py", f"""
            import os

            def test_url():
                assert os.environ[{VARIABLE!r}] == {URL!r}
        """)

        assert run_pg_tests.run_modules([module], URL) == 0


class TestMain:
    @pytest.fixture
    def tests_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(run_pg_tests, "TESTS_DIR", tmp_path)
        monkeypatch.delenv(VARIABLE, raising=False)
        return tmp_path

    def test_finding_no_modules_is_a_failure(self, tests_dir):
        """Otherwise a broken discovery reads as a clean pass."""
        assert run_pg_tests.main([]) == 1

    def test_no_database_is_a_failure(self, tests_dir):
        """With a service container promised, its absence must not read as a pass."""
        _passing(tests_dir)

        assert run_pg_tests.main([]) == 1

    def test_no_local_server_says_what_did_not_run_and_passes(
            self, tests_dir, monkeypatch, capsys):
        _passing(tests_dir, "test_needs_db.py")
        monkeypatch.setattr(run_pg_tests, "find_server_bin", lambda: None)

        assert run_pg_tests.main(["--local-server"]) == 0

        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "did NOT run" in out
        assert "test_needs_db.py" in out

    def test_an_unreachable_database_is_a_failure(self, tests_dir, monkeypatch, capsys):
        _passing(tests_dir)
        monkeypatch.setenv(VARIABLE, URL)

        def refuse(url):
            raise OSError("connection refused")

        monkeypatch.setattr(run_pg_tests, "server_version", refuse)

        assert run_pg_tests.main([]) == 1
        assert "connection refused" in capsys.readouterr().out
