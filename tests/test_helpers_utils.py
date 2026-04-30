"""
test_helpers_utils.py

Tests for the pure utility functions in helpers.py that do not require
a live Telegram connection:

  * get_rank_label_plain
  * get_rank_label_html
  * get_rank_label_instrumental
  * _parse_role_and_tag
  * _extract_member_tag
  * parse_closechat_duration  (English & Russian)
  * _rebuild_username_index / find_user_id_by_username_in_chat
  * setclosechatstate / getclosechatstate
  * is_group_approved / add_pending_group / deny_pending_group
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import helpers
import persistence


# ===========================================================================
# get_rank_label_plain
# ===========================================================================

class TestGetRankLabelPlain:
    def test_dev_999(self):
        assert helpers.get_rank_label_plain(999) == "Разработчик бота"

    def test_dev_above_999(self):
        assert helpers.get_rank_label_plain(1000) == "Разработчик бота"

    def test_owner_5(self):
        assert helpers.get_rank_label_plain(5) == "Владелец чата"

    def test_owner_above_5(self):
        assert helpers.get_rank_label_plain(6) == "Владелец чата"

    def test_chief_admin_4(self):
        assert helpers.get_rank_label_plain(4) == "Главный админ"

    def test_admin_3(self):
        assert helpers.get_rank_label_plain(3) == "Админ"

    def test_mod_2(self):
        assert helpers.get_rank_label_plain(2) == "Модератор"

    def test_trainee_1(self):
        assert helpers.get_rank_label_plain(1) == "Стажёр"

    def test_member_0(self):
        assert helpers.get_rank_label_plain(0) == ""

    def test_member_negative(self):
        assert helpers.get_rank_label_plain(-1) == ""


# ===========================================================================
# get_rank_label_html
# ===========================================================================

class TestGetRankLabelHtml:
    def test_dev_contains_text(self):
        result = helpers.get_rank_label_html(999)
        assert "Разработчик бота" in result
        assert "tg-emoji" in result

    def test_owner_contains_text(self):
        result = helpers.get_rank_label_html(5)
        assert "Владелец чата" in result

    def test_chief_admin(self):
        result = helpers.get_rank_label_html(4)
        assert "Главный админ" in result

    def test_admin(self):
        result = helpers.get_rank_label_html(3)
        assert "Админ" in result

    def test_mod(self):
        result = helpers.get_rank_label_html(2)
        assert "Модератор" in result

    def test_trainee(self):
        result = helpers.get_rank_label_html(1)
        assert "Стажёр" in result

    def test_member_returns_empty(self):
        assert helpers.get_rank_label_html(0) == ""


# ===========================================================================
# get_rank_label_instrumental
# ===========================================================================

class TestGetRankLabelInstrumental:
    def test_dev(self):
        assert helpers.get_rank_label_instrumental(999) == "Разработчиком бота"

    def test_owner(self):
        assert helpers.get_rank_label_instrumental(5) == "Владельцем чата"

    def test_chief_admin(self):
        assert helpers.get_rank_label_instrumental(4) == "Главным админом"

    def test_admin(self):
        assert helpers.get_rank_label_instrumental(3) == "Админом"

    def test_mod(self):
        assert helpers.get_rank_label_instrumental(2) == "Модератором"

    def test_trainee(self):
        assert helpers.get_rank_label_instrumental(1) == "Стажёром"

    def test_member_returns_empty(self):
        assert helpers.get_rank_label_instrumental(0) == ""


# ===========================================================================
# _parse_role_and_tag
# ===========================================================================

class TestParseRoleAndTag:
    def test_empty_returns_empty_tuple(self):
        role, tag = helpers._parse_role_and_tag("")
        assert role == "" and tag is None

    def test_whitespace_only(self):
        role, tag = helpers._parse_role_and_tag("   ")
        assert role == "" and tag is None

    def test_role_only_no_pipe(self):
        role, tag = helpers._parse_role_and_tag("Главный")
        assert role == "Главный" and tag is None

    def test_role_and_tag_with_pipe(self):
        role, tag = helpers._parse_role_and_tag("Главный | Тег")
        assert role == "Главный" and tag == "Тег"

    def test_empty_tag_after_pipe_is_none(self):
        role, tag = helpers._parse_role_and_tag("Главный|")
        assert role == "Главный" and tag is None

    def test_role_with_spaces_trimmed(self):
        role, tag = helpers._parse_role_and_tag("  Роль  |  Тег  ")
        assert role == "Роль" and tag == "Тег"

    def test_multiple_pipes_only_first_split(self):
        role, tag = helpers._parse_role_and_tag("A|B|C")
        assert role == "A" and tag == "B|C"


# ===========================================================================
# _extract_member_tag
# ===========================================================================

class TestExtractMemberTag:
    def test_dict_with_custom_title(self):
        assert helpers._extract_member_tag({"custom_title": "Boss"}) == "Boss"

    def test_dict_with_tag(self):
        assert helpers._extract_member_tag({"tag": "VIP"}) == "VIP"

    def test_dict_prefers_first_non_empty_key(self):
        # priority: tag > member_tag > custom_tag > custom_title
        d = {"tag": "T1", "custom_title": "T2"}
        assert helpers._extract_member_tag(d) == "T1"

    def test_dict_empty_string_skipped(self):
        d = {"tag": "", "custom_title": "Title"}
        assert helpers._extract_member_tag(d) == "Title"

    def test_dict_all_empty_returns_empty(self):
        assert helpers._extract_member_tag({}) == ""

    def test_object_with_tag_attr(self):
        class Obj:
            tag = "MemberTag"
            member_tag = ""
            custom_tag = ""
            custom_title = ""

        assert helpers._extract_member_tag(Obj()) == "MemberTag"

    def test_object_no_tag_falls_through(self):
        class Obj:
            pass

        assert helpers._extract_member_tag(Obj()) == ""

    def test_none_dict_returns_empty(self):
        assert helpers._extract_member_tag({}) == ""

    def test_whitespace_only_tag_skipped(self):
        assert helpers._extract_member_tag({"tag": "   "}) == ""


# ===========================================================================
# parse_closechat_duration  (English)
# ===========================================================================

MAX_CC = helpers.MAX_CLOSECHAT_SECONDS  # 86400


class TestParseClosechatDurationEnglish:
    def test_empty_returns_none(self):
        assert helpers.parse_closechat_duration("", False) is None

    def test_zero_returns_none(self):
        assert helpers.parse_closechat_duration("0", False) is None

    def test_minutes(self):
        assert helpers.parse_closechat_duration("30m", False) == 30 * 60

    def test_hours(self):
        assert helpers.parse_closechat_duration("2h", False) == 7200

    def test_max_exactly_one_day(self):
        assert helpers.parse_closechat_duration("1d", False) == 86400

    def test_above_max_returns_none(self):
        assert helpers.parse_closechat_duration("2d", False) is None

    def test_weeks_exceeds_max_returns_none(self):
        assert helpers.parse_closechat_duration("1w", False) is None

    def test_unknown_unit_returns_none(self):
        assert helpers.parse_closechat_duration("5x", False) is None

    def test_digit_after_unit_returns_none(self):
        assert helpers.parse_closechat_duration("1h2", False) is None

    def test_no_number_returns_none(self):
        assert helpers.parse_closechat_duration("h", False) is None

    def test_no_unit_returns_none(self):
        assert helpers.parse_closechat_duration("5", False) is None

    def test_case_insensitive(self):
        assert helpers.parse_closechat_duration("30M", False) == 30 * 60


class TestParseClosechatDurationRussian:
    def test_minutes_short(self):
        assert helpers.parse_closechat_duration("30м", True) == 1800

    def test_minutes_long(self):
        assert helpers.parse_closechat_duration("30мин", True) == 1800

    def test_hours(self):
        assert helpers.parse_closechat_duration("2ч", True) == 7200

    def test_days(self):
        assert helpers.parse_closechat_duration("1д", True) == 86400

    def test_above_max_returns_none(self):
        assert helpers.parse_closechat_duration("2д", True) is None

    def test_unknown_unit(self):
        assert helpers.parse_closechat_duration("5с", True) is None


# ===========================================================================
# _rebuild_username_index & find_user_id_by_username_in_chat
# ===========================================================================

class TestUsernameIndex:
    def setup_method(self):
        """Seed USERS with some test data and rebuild the index."""
        persistence.USERS.clear()
        persistence.USERS.update({
            "-100123": {
                "111": {"username": "alice", "first_name": "Alice",
                        "last_name": "", "full_name": "Alice", "id": 111},
                "222": {"username": "Bob", "first_name": "Bob",
                        "last_name": "", "full_name": "Bob", "id": 222},
                "333": {"username": "", "first_name": "NoUser",
                        "last_name": "", "full_name": "NoUser", "id": 333},
            }
        })
        helpers._rebuild_username_index()

    def test_find_existing_lowercase(self):
        result = helpers.find_user_id_by_username_in_chat(-100123, "alice")
        assert result == 111

    def test_find_existing_case_insensitive(self):
        result = helpers.find_user_id_by_username_in_chat(-100123, "ALICE")
        assert result == 111

    def test_find_with_at_prefix(self):
        result = helpers.find_user_id_by_username_in_chat(-100123, "@alice")
        assert result == 111

    def test_find_stored_uppercase_as_lowercase(self):
        # "Bob" is stored as "bob" in the index
        result = helpers.find_user_id_by_username_in_chat(-100123, "bob")
        assert result == 222

    def test_not_found_returns_none(self):
        result = helpers.find_user_id_by_username_in_chat(-100123, "unknown")
        assert result is None

    def test_no_username_user_not_indexed(self):
        # user 333 has no username
        result = helpers.find_user_id_by_username_in_chat(-100123, "")
        assert result is None

    def test_different_chat_not_found(self):
        result = helpers.find_user_id_by_username_in_chat(-100999, "alice")
        assert result is None

    def teardown_method(self):
        persistence.USERS.clear()
        helpers._rebuild_username_index()


# ===========================================================================
# setclosechatstate / getclosechatstate
# ===========================================================================

class TestCloseChatState:
    def setup_method(self):
        persistence.CLOSE_CHAT_STATE.clear()

    def test_set_closed_state(self):
        helpers.setclosechatstate(-100001, closed=True, until_ts=9999999999)
        state = helpers.getclosechatstate(-100001)
        assert state.get("closed") is True
        assert state.get("until") == 9999999999.0

    def test_open_chat_removes_state(self):
        helpers.setclosechatstate(-100001, closed=True, until_ts=9999999999)
        helpers.setclosechatstate(-100001, closed=False, until_ts=0)
        state = helpers.getclosechatstate(-100001)
        assert state == {}

    def test_get_unknown_chat_returns_empty(self):
        assert helpers.getclosechatstate(-999999) == {}

    def teardown_method(self):
        persistence.CLOSE_CHAT_STATE.clear()


# ===========================================================================
# is_group_approved / add_pending_group / deny_pending_group
# ===========================================================================

class _FakeUser:
    """Minimal stand-in for telebot.types.User."""
    def __init__(self, uid, username):
        self.id = uid
        self.username = username
        self.first_name = "Test"
        self.last_name = ""


class TestPendingGroups:
    def setup_method(self):
        persistence.PENDING_GROUPS.clear()

    def test_unknown_group_is_approved(self):
        assert helpers.is_group_approved(-100001) is True

    def test_pending_group_not_approved(self):
        helpers.add_pending_group(-100002, "Test Group", _FakeUser(42, "adder"))
        assert helpers.is_group_approved(-100002) is False

    def test_pending_group_has_expected_fields(self):
        helpers.add_pending_group(-100003, "My Chat", _FakeUser(99, "testuser"))
        rec = persistence.PENDING_GROUPS.get(str(-100003))
        assert rec is not None
        assert rec["title"] == "My Chat"
        assert rec["adder_id"] == 99
        assert rec["adder_username"] == "testuser"

    def test_deny_removes_from_pending(self):
        helpers.add_pending_group(-100004, "X", _FakeUser(1, "u"))
        helpers.deny_pending_group(-100004)
        assert helpers.is_group_approved(-100004) is True

    def test_approve_removes_from_pending(self):
        helpers.add_pending_group(-100005, "Y", _FakeUser(2, "v"))
        helpers.approve_pending_group(-100005)
        assert helpers.is_group_approved(-100005) is True

    def teardown_method(self):
        persistence.PENDING_GROUPS.clear()
