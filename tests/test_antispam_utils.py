"""
test_antispam_utils.py

Tests for the pure utility functions in antispam.py:

  * _is_internal_group_link   (regex-based, no I/O)
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import antispam


# ===========================================================================
# _is_internal_group_link
# ===========================================================================

class TestIsInternalGroupLink:
    """
    Private supergroups have negative chat_ids like -1001234567890.
    The t.me/c/ link encodes the peer_id as the positive part after -100:
        chat_id = -1001234567890  →  peer_id = 1234567890
    """
    CHAT_ID = -1001234567890
    PEER_ID = "1234567890"

    # ------------------------------------------------------------------
    # Matching links (should return True)
    # ------------------------------------------------------------------

    def test_https_link_matches(self):
        url = f"https://t.me/c/{self.PEER_ID}/42"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is True

    def test_http_link_matches(self):
        url = f"http://t.me/c/{self.PEER_ID}/5"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is True

    def test_no_scheme_link_matches(self):
        url = f"t.me/c/{self.PEER_ID}/100"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is True

    def test_telegram_me_domain_matches(self):
        url = f"https://telegram.me/c/{self.PEER_ID}/1"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is True

    # ------------------------------------------------------------------
    # Non-matching links (should return False)
    # ------------------------------------------------------------------

    def test_different_peer_id_does_not_match(self):
        url = "https://t.me/c/9999999999/1"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is False

    def test_public_username_link_not_internal(self):
        url = "https://t.me/somechannel/42"
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is False

    def test_regular_url_not_internal(self):
        assert antispam._is_internal_group_link("https://example.com", self.CHAT_ID) is False

    def test_empty_string_not_internal(self):
        assert antispam._is_internal_group_link("", self.CHAT_ID) is False

    def test_plain_chat_link_no_message_id(self):
        # Needs both peer_id AND message_id in the URL
        url = f"https://t.me/c/{self.PEER_ID}/"
        # The regex requires \d+ for message_id; trailing slash with no digits won't match
        assert antispam._is_internal_group_link(url, self.CHAT_ID) is False

    def test_different_chat_id_same_url_does_not_match(self):
        url = f"https://t.me/c/{self.PEER_ID}/1"
        other_chat_id = -1009999999999
        assert antispam._is_internal_group_link(url, other_chat_id) is False

    def test_non_minus100_negative_id(self):
        # For IDs that don't start with -100, compare abs value
        chat_id = -9876
        url = "https://t.me/c/9876/1"
        assert antispam._is_internal_group_link(url, chat_id) is True

    def test_non_minus100_negative_id_mismatch(self):
        chat_id = -9876
        url = "https://t.me/c/1111/1"
        assert antispam._is_internal_group_link(url, chat_id) is False


# ===========================================================================
# Regex constants: _TG_URL_RE
# ===========================================================================

class TestTgUrlRegex:
    def test_https_tme_matched(self):
        assert antispam._TG_URL_RE.search("https://t.me/username") is not None

    def test_http_tme_matched(self):
        assert antispam._TG_URL_RE.search("http://t.me/username") is not None

    def test_tme_no_scheme_matched(self):
        assert antispam._TG_URL_RE.search("t.me/username") is not None

    def test_telegram_me_matched(self):
        assert antispam._TG_URL_RE.search("telegram.me/username") is not None

    def test_telegram_org_matched(self):
        assert antispam._TG_URL_RE.search("telegram.org/something") is not None

    def test_tg_scheme_matched(self):
        assert antispam._TG_URL_RE.search("tg://resolve?domain=test") is not None

    def test_regular_url_not_matched(self):
        assert antispam._TG_URL_RE.search("https://example.com") is None

    def test_google_not_matched(self):
        assert antispam._TG_URL_RE.search("https://google.com") is None


# ===========================================================================
# Regex constants: _TG_USERNAME_RE
# ===========================================================================

class TestTgUsernameRegex:
    def test_valid_username(self):
        assert antispam._TG_USERNAME_RE.search("@user_name123") is not None

    def test_minimum_length(self):
        # @[a-zA-Z][a-zA-Z0-9_]{3,}  →  min 5 chars total: @x + 3 chars
        assert antispam._TG_USERNAME_RE.search("@abcd") is not None

    def test_too_short_not_matched(self):
        # @abc → only 3 chars after @, need 3+ after first letter = 4 total chars
        assert antispam._TG_USERNAME_RE.search("@abc") is None

    def test_starts_with_digit_not_matched(self):
        assert antispam._TG_USERNAME_RE.search("@1abc") is None

    def test_no_at_not_matched(self):
        assert antispam._TG_USERNAME_RE.search("username") is None


# ===========================================================================
# Regex constants: _ALL_LINKS_RE
# ===========================================================================

class TestAllLinksRegex:
    def test_https_matched(self):
        assert antispam._ALL_LINKS_RE.search("https://example.com") is not None

    def test_http_matched(self):
        assert antispam._ALL_LINKS_RE.search("http://example.com") is not None

    def test_www_matched(self):
        assert antispam._ALL_LINKS_RE.search("www.example.com") is not None

    def test_plain_domain_not_matched(self):
        assert antispam._ALL_LINKS_RE.search("example.com") is None

    def test_empty_not_matched(self):
        assert antispam._ALL_LINKS_RE.search("") is None
