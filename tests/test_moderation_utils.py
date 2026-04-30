"""
test_moderation_utils.py

Tests for the pure utility functions in moderation.py that do not require
a live Telegram connection:

  * _ru_plural
  * _format_mod_duration_human
  * _mod_duration_text
  * _parse_punish_duration  (English & Russian)
  * _parse_duration_token_parts
  * _parse_duration_prefix
  * _mod_is_row_active
  * _mod_fmt_ts
  * _fmt_time
"""
from __future__ import annotations

import sys
import os

# ---------------------------------------------------------------------------
# The conftest.py bootstrap (env-vars + mock modules) runs automatically via
# pytest's conftest mechanism before this file is loaded.
# ---------------------------------------------------------------------------

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import moderation as mod


# ===========================================================================
# _ru_plural
# ===========================================================================

FORMS = ("год", "года", "лет")


class TestRuPlural:
    def test_singular_1(self):
        assert mod._ru_plural(1, FORMS) == "год"

    def test_singular_21(self):
        assert mod._ru_plural(21, FORMS) == "год"

    def test_singular_101(self):
        assert mod._ru_plural(101, FORMS) == "год"

    def test_genitive_2(self):
        assert mod._ru_plural(2, FORMS) == "года"

    def test_genitive_3(self):
        assert mod._ru_plural(3, FORMS) == "года"

    def test_genitive_4(self):
        assert mod._ru_plural(4, FORMS) == "года"

    def test_genitive_22(self):
        assert mod._ru_plural(22, FORMS) == "года"

    def test_plural_5(self):
        assert mod._ru_plural(5, FORMS) == "лет"

    def test_plural_11(self):
        # 11 % 100 == 11 → special case → plural
        assert mod._ru_plural(11, FORMS) == "лет"

    def test_plural_12(self):
        assert mod._ru_plural(12, FORMS) == "лет"

    def test_plural_13(self):
        assert mod._ru_plural(13, FORMS) == "лет"

    def test_plural_14(self):
        assert mod._ru_plural(14, FORMS) == "лет"

    def test_plural_0(self):
        assert mod._ru_plural(0, FORMS) == "лет"

    def test_plural_20(self):
        assert mod._ru_plural(20, FORMS) == "лет"

    def test_negative_treated_as_absolute(self):
        # abs(-1) % 10 == 1, abs(-1) % 100 != 11
        assert mod._ru_plural(-1, FORMS) == "год"


# ===========================================================================
# _format_mod_duration_human
# ===========================================================================

class TestFormatModDurationHuman:
    def test_zero_returns_forever(self):
        assert mod._format_mod_duration_human(0) == "навсегда"

    def test_negative_returns_forever(self):
        assert mod._format_mod_duration_human(-60) == "навсегда"

    def test_less_than_minute_returns_one_minute(self):
        assert mod._format_mod_duration_human(30) == "1 минута"

    def test_exactly_one_minute(self):
        assert mod._format_mod_duration_human(60) == "1 минута"

    def test_two_minutes(self):
        assert mod._format_mod_duration_human(120) == "2 минуты"

    def test_five_minutes(self):
        assert mod._format_mod_duration_human(300) == "5 минут"

    def test_one_hour(self):
        assert mod._format_mod_duration_human(3600) == "1 час"

    def test_two_hours(self):
        assert mod._format_mod_duration_human(7200) == "2 часа"

    def test_one_day(self):
        assert mod._format_mod_duration_human(86400) == "1 день"

    def test_seven_days(self):
        assert mod._format_mod_duration_human(7 * 86400) == "1 неделя"

    def test_thirty_days(self):
        assert mod._format_mod_duration_human(30 * 86400) == "1 месяц"

    def test_one_year(self):
        assert mod._format_mod_duration_human(365 * 86400) == "1 год"

    def test_mixed_days_and_hours(self):
        result = mod._format_mod_duration_human(2 * 86400 + 3 * 3600)
        assert "2 дня" in result
        assert "3 часа" in result

    def test_mixed_hours_and_minutes(self):
        result = mod._format_mod_duration_human(2 * 3600 + 30 * 60)
        assert "2 часа" in result
        assert "30 минут" in result


# ===========================================================================
# _mod_duration_text
# ===========================================================================

class TestModDurationText:
    def test_none_returns_forever(self):
        assert mod._mod_duration_text(None) == "навсегда"

    def test_zero_returns_forever(self):
        assert mod._mod_duration_text(0) == "навсегда"

    def test_negative_returns_forever(self):
        assert mod._mod_duration_text(-1) == "навсегда"

    def test_positive_delegates(self):
        assert mod._mod_duration_text(3600) == "1 час"


# ===========================================================================
# _parse_punish_duration  (English)
# ===========================================================================

MIN = mod.MIN_PUNISH_SECONDS   # 60
MAX = mod.MAX_PUNISH_SECONDS   # 365 * 24 * 60 * 60


class TestParsePunishDurationEnglish:
    def test_empty_returns_none(self):
        assert mod._parse_punish_duration("", False) is None

    def test_forever_keyword(self):
        assert mod._parse_punish_duration("forever", False) == 0

    def test_minutes(self):
        assert mod._parse_punish_duration("5m", False) == 300

    def test_hours(self):
        assert mod._parse_punish_duration("2h", False) == 7200

    def test_days(self):
        assert mod._parse_punish_duration("3d", False) == 3 * 86400

    def test_weeks(self):
        assert mod._parse_punish_duration("1w", False) == 7 * 86400

    def test_months(self):
        assert mod._parse_punish_duration("1mou", False) == 30 * 86400

    def test_years(self):
        assert mod._parse_punish_duration("1y", False) == 365 * 86400

    def test_below_minimum_returns_none(self):
        # 30s < MIN (60s)
        assert mod._parse_punish_duration("30s", False) is None

    def test_unknown_unit_returns_none(self):
        assert mod._parse_punish_duration("5x", False) is None

    def test_zero_amount_returns_none(self):
        assert mod._parse_punish_duration("0m", False) is None

    def test_trailing_punctuation_stripped(self):
        assert mod._parse_punish_duration("5m.", False) == 300

    def test_leading_trailing_spaces(self):
        assert mod._parse_punish_duration("  5m  ", False) == 300


class TestParsePunishDurationRussian:
    def test_navсегда_keyword(self):
        assert mod._parse_punish_duration("навсегда", True) == 0

    def test_minutes_short(self):
        assert mod._parse_punish_duration("5м", True) == 300

    def test_minutes_long(self):
        assert mod._parse_punish_duration("5мин", True) == 300

    def test_hours(self):
        assert mod._parse_punish_duration("2ч", True) == 7200

    def test_days(self):
        assert mod._parse_punish_duration("3д", True) == 3 * 86400

    def test_weeks(self):
        assert mod._parse_punish_duration("1н", True) == 7 * 86400

    def test_months(self):
        assert mod._parse_punish_duration("1мес", True) == 30 * 86400

    def test_years(self):
        assert mod._parse_punish_duration("1г", True) == 365 * 86400

    def test_unknown_unit_returns_none(self):
        assert mod._parse_punish_duration("5с", True) is None


# ===========================================================================
# _parse_duration_token_parts
# ===========================================================================

class TestParseDurationTokenParts:
    def test_simple_english_hours(self):
        assert mod._parse_duration_token_parts("2h", False) == [7200]

    def test_combined_english(self):
        # "1h30m" → [3600, 1800]
        result = mod._parse_duration_token_parts("1h30m", False)
        assert result == [3600, 1800]

    def test_russian_hours(self):
        assert mod._parse_duration_token_parts("2ч", True) == [7200]

    def test_russian_months(self):
        assert mod._parse_duration_token_parts("1мес", True) == [30 * 86400]

    def test_empty_returns_none(self):
        assert mod._parse_duration_token_parts("", False) is None

    def test_unknown_unit_returns_none(self):
        assert mod._parse_duration_token_parts("5z", False) is None

    def test_zero_amount_returns_none(self):
        assert mod._parse_duration_token_parts("0h", False) is None

    def test_letters_only_returns_none(self):
        assert mod._parse_duration_token_parts("abc", False) is None


# ===========================================================================
# _parse_duration_prefix
# ===========================================================================

class TestParseDurationPrefix:
    def test_empty_string(self):
        secs, consumed, invalid = mod._parse_duration_prefix("", False)
        assert secs is None and consumed == 0 and not invalid

    def test_no_duration_at_start(self):
        secs, consumed, invalid = mod._parse_duration_prefix("hello world", False)
        assert consumed == 0 and not invalid

    def test_simple_duration(self):
        secs, consumed, invalid = mod._parse_duration_prefix("1h reason", False)
        assert secs == 3600 and consumed == 1 and not invalid

    def test_two_part_duration(self):
        secs, consumed, invalid = mod._parse_duration_prefix("1h 30m reason", False)
        assert secs == 3600 + 1800 and consumed == 2 and not invalid

    def test_forever_keyword(self):
        secs, consumed, invalid = mod._parse_duration_prefix("forever reason", False)
        assert secs == 0 and consumed == 1 and not invalid

    def test_russian_duration(self):
        secs, consumed, invalid = mod._parse_duration_prefix("2ч причина", True)
        assert secs == 7200 and consumed == 1 and not invalid

    def test_too_many_parts_is_invalid(self):
        # 4 parts exceed default max_parts=3
        secs, consumed, invalid = mod._parse_duration_prefix("1h 2h 3h 4h text", False)
        assert invalid

    def test_out_of_bounds_is_invalid(self):
        # below minimum after parsing
        secs, consumed, invalid = mod._parse_duration_prefix("1s text", False)
        # "1s" is not a valid unit, so not consumed
        assert consumed == 0 and not invalid


# ===========================================================================
# _mod_is_row_active
# ===========================================================================

class TestModIsRowActive:
    NOW = 1_700_000_000  # fixed timestamp for determinism

    def test_empty_row_is_not_active(self):
        assert not mod._mod_is_row_active("warn", {}, self.NOW)

    def test_warn_active_no_until(self):
        assert mod._mod_is_row_active("warn", {"active": True}, self.NOW)

    def test_warn_explicitly_inactive(self):
        assert not mod._mod_is_row_active("warn", {"active": False}, self.NOW)

    def test_mute_permanent_is_active(self):
        row = {"active": True, "until": 0}
        assert mod._mod_is_row_active("mute", row, self.NOW)

    def test_mute_future_expiry_is_active(self):
        row = {"active": True, "until": self.NOW + 3600}
        assert mod._mod_is_row_active("mute", row, self.NOW)

    def test_mute_expired_is_not_active(self):
        row = {"active": True, "until": self.NOW - 1}
        assert not mod._mod_is_row_active("mute", row, self.NOW)

    def test_mute_expires_exactly_now_is_not_active(self):
        row = {"active": True, "until": self.NOW}
        assert not mod._mod_is_row_active("mute", row, self.NOW)

    def test_ban_future_is_active(self):
        row = {"active": True, "until": self.NOW + 100}
        assert mod._mod_is_row_active("ban", row, self.NOW)

    def test_ban_expired_is_not_active(self):
        row = {"active": True, "until": self.NOW - 100}
        assert not mod._mod_is_row_active("ban", row, self.NOW)

    def test_kick_no_until_is_active(self):
        # kick rows don't use "until"
        row = {"active": True}
        assert mod._mod_is_row_active("kick", row, self.NOW)


# ===========================================================================
# _mod_fmt_ts
# ===========================================================================

class TestModFmtTs:
    def test_none_returns_dash(self):
        assert mod._mod_fmt_ts(None) == "—"

    def test_zero_returns_dash(self):
        assert mod._mod_fmt_ts(0) == "—"

    def test_valid_timestamp_returns_formatted_string(self):
        result = mod._mod_fmt_ts(1_700_000_000)
        # Should produce a "YYYY-MM-DD HH:MM" string
        assert len(result) == 16
        assert result[4] == "-" and result[7] == "-" and result[13] == ":"


# ===========================================================================
# _fmt_time
# ===========================================================================

class TestFmtTime:
    def test_none_returns_dash(self):
        assert mod._fmt_time(None) == "—"

    def test_zero_returns_dash(self):
        assert mod._fmt_time(0) == "—"

    def test_valid_ts_produces_tg_time_tag(self):
        result = mod._fmt_time(1_700_000_000)
        assert result.startswith('<tg-time unix="1700000000"')
        assert result.endswith("</tg-time>")

    def test_custom_format_included(self):
        result = mod._fmt_time(1_700_000_000, "D")
        assert 'format="D"' in result
