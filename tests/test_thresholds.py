"""Reading a configured FTP or heart rate, in the one place startup and recalculation
share. The real startup is in test_thresholds_sql.py."""
from unittest.mock import MagicMock, patch

import pytest

from fitness import configured_threshold

NAME = "VELOMATE_FTP"
KEY = "configured_ftp"


def _read(monkeypatch, raw, stored=None):
    if raw is None:
        monkeypatch.delenv(NAME, raising=False)
    else:
        monkeypatch.setenv(NAME, raw)
    with patch("db.get_sync_state", return_value=stored) as get_sync_state:
        return configured_threshold(MagicMock(), NAME, KEY), get_sync_state


@pytest.mark.parametrize("raw, expected", [
    ("262", 262),
    ("262.0", 262),
    ("262.4", 262),
    ("262.5", 263),
    ("262.6", 263),
    (" 262 ", 262),
    ("1e2", 100),
])
def test_a_number_is_rounded_to_whole_units(monkeypatch, raw, expected):
    assert _read(monkeypatch, raw)[0] == expected


@pytest.mark.parametrize("raw", [None, "", "0", "0.0", "0.4", "-5", "-262.5"])
def test_nothing_positive_means_not_configured(monkeypatch, raw):
    """The compose file passes 0 for a variable the deployment leaves unset."""
    value, get_sync_state = _read(monkeypatch, raw, stored="250")

    assert value == 0
    assert not get_sync_state.called, "a real answer must not touch the database"


def test_every_whole_number_reads_as_it_always_has(monkeypatch):
    """The value a deployment's stored threshold compares against: if this moved,
    the first start after an upgrade would reset every ride's derived metrics."""
    for number in range(-50, 2001):
        expected = number if number > 0 else 0

        assert _read(monkeypatch, str(number))[0] == expected


@pytest.mark.parametrize("raw", ["262W", "262,5", "abc", "nan", "inf", "-inf", "   "])
class TestABadValue:
    def test_keeps_the_last_good_value(self, monkeypatch, raw):
        """Rather than 0, which would read as a change and reset every ride."""
        value, get_sync_state = _read(monkeypatch, raw, stored="250")

        assert value == 250
        assert get_sync_state.call_args.args[1] == KEY

    def test_with_nothing_stored_means_not_configured(self, monkeypatch, raw):
        assert _read(monkeypatch, raw, stored=None)[0] == 0

    def test_says_what_is_wrong(self, monkeypatch, capsys, raw):
        _read(monkeypatch, raw, stored="250")

        out = capsys.readouterr().out
        assert NAME in out
        assert repr(raw) in out
        assert "250" in out
