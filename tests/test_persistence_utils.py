"""
test_persistence_utils.py

Tests for the SQLite/JSON persistence layer and helper utilities in
persistence.py that can be exercised without a live Telegram connection:

  * load_json_file / save_json_file   (SQLite round-trip)
  * set_log_channel / get_log_channel / remove_log_channel
  * set_log_channel_event
  * assign_bot_to_chat / get_chat_assignment / unassign_bot_from_chat
  * _is_duplicate_callback_query (deduplication logic)
  * buffer_msg_event / get_msg_stats_period
"""
from __future__ import annotations

import os
import sys
import time
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import persistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tmp_path(suffix: str) -> str:
    """Return a unique path inside a temp directory."""
    return os.path.join(tempfile.mkdtemp(prefix="pytest_bot_"), suffix)


# ===========================================================================
# save_json_file / load_json_file (SQLite round-trip)
# ===========================================================================

class TestJsonRoundTrip:
    def test_save_and_load_dict(self):
        path = _tmp_path("data.json")
        payload = {"key": "value", "number": 42}
        assert persistence.save_json_file(path, payload) is True
        loaded = persistence.load_json_file(path, {})
        assert loaded == payload

    def test_save_and_load_list(self):
        path = _tmp_path("list.json")
        payload = [1, 2, 3, "four"]
        persistence.save_json_file(path, payload)
        assert persistence.load_json_file(path, []) == payload

    def test_save_overwrites_previous(self):
        path = _tmp_path("overwrite.json")
        persistence.save_json_file(path, {"v": 1})
        persistence.save_json_file(path, {"v": 2})
        loaded = persistence.load_json_file(path, {})
        assert loaded == {"v": 2}

    def test_load_missing_key_returns_default(self):
        path = _tmp_path("missing.json")
        default = {"default": True}
        result = persistence.load_json_file(path, default)
        assert result == default

    def test_empty_dict_round_trip(self):
        path = _tmp_path("empty.json")
        persistence.save_json_file(path, {})
        assert persistence.load_json_file(path, None) == {}

    def test_unicode_content(self):
        path = _tmp_path("unicode.json")
        payload = {"msg": "Привет мир 🌍"}
        persistence.save_json_file(path, payload)
        assert persistence.load_json_file(path, {}) == payload

    def test_nested_structure(self):
        path = _tmp_path("nested.json")
        payload = {"a": {"b": {"c": [1, 2, {"d": True}]}}}
        persistence.save_json_file(path, payload)
        assert persistence.load_json_file(path, {}) == payload


# ===========================================================================
# log_channels helpers
# ===========================================================================

class TestLogChannels:
    CHAT_ID = -1001111111111
    CHANNEL_ID = -1002222222222

    def setup_method(self):
        persistence.remove_log_channel(self.CHAT_ID)

    def test_set_and_get_log_channel(self):
        ok = persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID, "Test Log")
        assert ok is True
        lc = persistence.get_log_channel(self.CHAT_ID)
        assert lc is not None
        assert lc["channel_id"] == self.CHANNEL_ID
        assert lc["channel_title"] == "Test Log"

    def test_default_events_all_enabled(self):
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID)
        lc = persistence.get_log_channel(self.CHAT_ID)
        for event in persistence.LOG_CHANNEL_ALL_EVENTS:
            assert lc["events"].get(event) is True, f"Event {event!r} should be True by default"

    def test_set_log_channel_event_disable(self):
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID)
        persistence.set_log_channel_event(self.CHAT_ID, "ban", False)
        lc = persistence.get_log_channel(self.CHAT_ID)
        assert lc["events"]["ban"] is False

    def test_set_log_channel_event_re_enable(self):
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID)
        persistence.set_log_channel_event(self.CHAT_ID, "ban", False)
        persistence.set_log_channel_event(self.CHAT_ID, "ban", True)
        lc = persistence.get_log_channel(self.CHAT_ID)
        assert lc["events"]["ban"] is True

    def test_remove_log_channel(self):
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID)
        persistence.remove_log_channel(self.CHAT_ID)
        assert persistence.get_log_channel(self.CHAT_ID) is None

    def test_get_nonexistent_returns_none(self):
        assert persistence.get_log_channel(-1009999999999) is None

    def test_set_channel_event_on_nonexistent_returns_false(self):
        ok = persistence.set_log_channel_event(-1009999999999, "ban", False)
        assert ok is False

    def test_overwrite_preserves_custom_events(self):
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID)
        persistence.set_log_channel_event(self.CHAT_ID, "ban", False)
        # Overwrite channel_id
        persistence.set_log_channel(self.CHAT_ID, self.CHANNEL_ID + 1)
        lc = persistence.get_log_channel(self.CHAT_ID)
        # Events should be preserved across overwrite
        assert lc["events"]["ban"] is False

    def teardown_method(self):
        persistence.remove_log_channel(self.CHAT_ID)


# ===========================================================================
# assign_bot_to_chat / get_chat_assignment / unassign_bot_from_chat
# ===========================================================================

class TestChatBotAssignment:
    CHAT_ID = -1003333333333
    BOT_ID = 100500
    BOT_USERNAME = "test_bot"

    def setup_method(self):
        persistence.unassign_bot_from_chat(self.CHAT_ID)

    def test_assign_returns_true(self):
        ok = persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        assert ok is True

    def test_get_assignment_returns_correct_data(self):
        persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        rec = persistence.get_chat_assignment(self.CHAT_ID)
        assert rec is not None
        assert rec["bot_id"] == self.BOT_ID
        assert rec["bot_username"] == self.BOT_USERNAME

    def test_same_bot_second_assign_returns_true(self):
        persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        ok = persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        assert ok is True

    def test_different_bot_assign_returns_false(self):
        persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        ok = persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID + 1, "other_bot")
        assert ok is False

    def test_unassign_removes_record(self):
        persistence.assign_bot_to_chat(self.CHAT_ID, self.BOT_ID, self.BOT_USERNAME)
        persistence.unassign_bot_from_chat(self.CHAT_ID)
        assert persistence.get_chat_assignment(self.CHAT_ID) is None

    def test_get_unassigned_returns_none(self):
        assert persistence.get_chat_assignment(-1009999999999) is None

    def teardown_method(self):
        persistence.unassign_bot_from_chat(self.CHAT_ID)


# ===========================================================================
# _is_duplicate_callback_query
# ===========================================================================

class _FakeCallbackQuery:
    """Minimal stand-in for telebot.types.CallbackQuery."""
    def __init__(self, user_id: int, data: str, call_id: str = "0"):
        self.id = call_id
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()


class TestDuplicateCallbackQuery:
    def setup_method(self):
        # Clear the deduplication set before each test
        persistence._CALLBACK_DEDUPE.clear()

    def test_first_call_not_duplicate(self):
        call = _FakeCallbackQuery(1, "some_action")
        assert persistence._is_duplicate_callback_query(call) is False

    def test_second_call_same_user_same_data_is_duplicate(self):
        call = _FakeCallbackQuery(1, "some_action")
        persistence._is_duplicate_callback_query(call)
        assert persistence._is_duplicate_callback_query(call) is True

    def test_different_user_not_duplicate(self):
        call1 = _FakeCallbackQuery(1, "action")
        call2 = _FakeCallbackQuery(2, "action")
        persistence._is_duplicate_callback_query(call1)
        assert persistence._is_duplicate_callback_query(call2) is False

    def test_different_data_not_duplicate(self):
        persistence._is_duplicate_callback_query(_FakeCallbackQuery(1, "action_a"))
        assert persistence._is_duplicate_callback_query(_FakeCallbackQuery(1, "action_b")) is False

    def test_empty_data_not_duplicate(self):
        call = _FakeCallbackQuery(1, "")
        assert persistence._is_duplicate_callback_query(call) is False

    def teardown_method(self):
        persistence._CALLBACK_DEDUPE.clear()


# ===========================================================================
# buffer_msg_event / get_msg_stats_period
# ===========================================================================

class TestMsgStatsBuffer:
    CHAT_ID = -1004444444444
    USER_ID = 77777

    def setup_method(self):
        # Flush any leftover buffered events from previous tests
        persistence._flush_msg_events()

    def test_buffer_and_flush_increments_count(self):
        ts = int(time.time())
        persistence.buffer_msg_event(self.CHAT_ID, self.USER_ID, ts, 1)
        persistence._flush_msg_events()
        # After flushing the event appears in the DB; count since ts-1 should be >= 1
        result = persistence.get_user_msg_count_for_period(self.CHAT_ID, self.USER_ID, ts - 1)
        assert result >= 1

    def test_multiple_events_counted(self):
        ts = int(time.time())
        uid = self.USER_ID + 1
        for i in range(5):
            persistence.buffer_msg_event(self.CHAT_ID, uid, ts + i, 100 + i)
        persistence._flush_msg_events()
        count = persistence.get_user_msg_count_for_period(self.CHAT_ID, uid, ts - 1)
        assert count == 5

    def test_count_outside_window_is_zero(self):
        ts = int(time.time())
        uid = self.USER_ID + 2
        # Event happened 1000 seconds ago
        persistence.buffer_msg_event(self.CHAT_ID, uid, ts - 1000, 200)
        persistence._flush_msg_events()
        # Query only the last second — should find nothing
        count = persistence.get_user_msg_count_for_period(self.CHAT_ID, uid, ts)
        assert count == 0

    def test_different_user_not_counted(self):
        ts = int(time.time())
        uid_writer = self.USER_ID + 3
        uid_query = self.USER_ID + 4
        persistence.buffer_msg_event(self.CHAT_ID, uid_writer, ts, 300)
        persistence._flush_msg_events()
        count = persistence.get_user_msg_count_for_period(self.CHAT_ID, uid_query, ts - 1)
        assert count == 0
