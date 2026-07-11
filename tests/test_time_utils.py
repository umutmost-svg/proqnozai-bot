"""Datetime formatting: every source format must land in Baku time (UTC+4)."""
from handlers.utils import _fmt_dt


def test_iso_z_is_treated_as_utc():
    assert _fmt_dt("2026-06-01T18:00:00Z") == "01.06 22:00 (UTC+4)"


def test_iso_with_offset_is_normalised_to_utc_first():
    # 21:00 at UTC+3 == 18:00 UTC == 22:00 Baku.
    assert _fmt_dt("2026-06-01T21:00:00+03:00") == "01.06 22:00 (UTC+4)"


def test_mostbet_format_is_treated_as_source_tz():
    # Mostbet "DD.MM.YYYY HH:MM" is UTC+3 → +1h to Baku.
    assert _fmt_dt("01.06.2026 19:00:00") == "01.06 20:00 (UTC+4)"


def test_space_separated_iso_is_treated_as_utc():
    assert _fmt_dt("2026-06-01 18:00") == "01.06 22:00 (UTC+4)"


def test_day_rollover():
    # 21:30 UTC + 4h crosses midnight.
    assert _fmt_dt("2026-06-01T21:30:00Z") == "02.06 01:30 (UTC+4)"


def test_short_or_empty_input_returns_empty():
    assert _fmt_dt("") == ""
    assert _fmt_dt("2026-06-01") == ""
