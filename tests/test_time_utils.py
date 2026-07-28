"""Datetime formatting: the low-level _fmt_dt shifts to a given offset (default
Baku UTC+4); fmt_dt_for_user follows the USER's stored timezone."""
from handlers.utils import _fmt_dt, fmt_dt_for_user


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


def test_explicit_offset_uses_matching_suffix():
    # 18:00 UTC in UTC+0 stays 18:00 with a UTC+0 suffix (not a hardcoded +4).
    assert _fmt_dt("2026-06-01T18:00:00Z", 0) == "01.06 18:00 (UTC+0)"


def test_fmt_dt_for_user_follows_stored_timezone(temp_db):
    temp_db.db_ensure(940001, "u", "en")
    temp_db.db_set(940001, "tz_offset", 2)   # UTC+2
    # 18:00 UTC → 20:00 at UTC+2, with a matching suffix.
    assert fmt_dt_for_user("2026-06-01T18:00:00Z", 940001) == "01.06 20:00 (UTC+2)"


def test_fmt_dt_for_user_falls_back_to_baku_without_tz(temp_db):
    temp_db.db_ensure(940002, "u", "en")   # no tz stored → default Baku
    assert fmt_dt_for_user("2026-06-01T18:00:00Z", 940002) == "01.06 22:00 (UTC+4)"
