"""
conftest.py — bootstrap mocks for the entire test suite.

All heavy external dependencies (telebot, telethon, psutil, requests) are replaced
with MagicMock objects **before** any production module is imported.  Required
environment variables are also set here so config.py can be safely imported.
"""
from __future__ import annotations

import os
import sys
from types import ModuleType
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# 1. Environment variables required by config.py
# ---------------------------------------------------------------------------
os.environ.setdefault("BOT_TOKEN", "0:test_token_pytest")
os.environ.setdefault("API_ID", "12345")
os.environ.setdefault("API_HASH", "0" * 32)
os.environ.setdefault("DATA_DIR", "/tmp/pytest_bot_data")
os.environ.setdefault("IS_CLONE", "0")

os.makedirs("/tmp/pytest_bot_data", exist_ok=True)


# ---------------------------------------------------------------------------
# 2. Helper: build a fake module tree from a dotted name
# ---------------------------------------------------------------------------
def _make_module(name: str) -> MagicMock:
    mod = MagicMock(spec=ModuleType)
    mod.__name__ = name
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# 3. telebot stubs
# ---------------------------------------------------------------------------
_telebot = _make_module("telebot")
_telebot_types = _make_module("telebot.types")
_telebot_apihelper = _make_module("telebot.apihelper")
_telebot_backends = _make_module("telebot.handler_backends")

# ContinueHandling is used as a sentinel value in type-checked paths
_ContinueHandling = type("ContinueHandling", (), {})
_telebot_backends.ContinueHandling = _ContinueHandling

# Make sure `from telebot import X` works
_telebot.types = _telebot_types
_telebot.apihelper = _telebot_apihelper
_telebot.handler_backends = _telebot_backends
_telebot.TeleBot = MagicMock(return_value=MagicMock())

# telebot.types stubs used in type-annotations and isinstance checks
for _cls in (
    "User", "Chat", "Message", "CallbackQuery",
    "InlineKeyboardMarkup", "InlineKeyboardButton",
    "ChatPermissions", "ChatMember",
):
    setattr(_telebot_types, _cls, MagicMock)

# Re-export items that config.py does `from telebot.types import …`
_telebot_types.InlineKeyboardMarkup = MagicMock
_telebot_types.InlineKeyboardButton = MagicMock

# ApiTelegramException — used in except clauses, must be a real exception class
class _FakeApiException(Exception):
    description = "mock error"

_telebot_apihelper.ApiTelegramException = _FakeApiException
# also needed as `from telebot.apihelper import ApiTelegramException`
sys.modules["telebot.apihelper"].ApiTelegramException = _FakeApiException  # type: ignore[attr-defined]

# ---------------------------------------------------------------------------
# 4. telethon stubs
# ---------------------------------------------------------------------------
_telethon = _make_module("telethon")
_telethon_errors = _make_module("telethon.errors")
_telethon_tl = _make_module("telethon.tl")
_telethon_tl_types = _make_module("telethon.tl.types")

class _UsernameNotOccupied(Exception):
    pass

_telethon_errors.UsernameNotOccupiedError = _UsernameNotOccupied
_telethon.TelegramClient = MagicMock(return_value=MagicMock())
_telethon.errors = _telethon_errors
_telethon.tl = _telethon_tl
_telethon_tl.types = _telethon_tl_types

for _ent in (
    "MessageService", "PeerChannel", "PeerChat",
    "MessageEntityBold", "MessageEntityItalic", "MessageEntityUnderline",
    "MessageEntityStrike", "MessageEntityCode", "MessageEntityPre",
    "MessageEntityTextUrl", "MessageEntityUrl", "MessageEntityMention",
    "MessageEntityCustomEmoji",
):
    setattr(_telethon_tl_types, _ent, MagicMock)

# ---------------------------------------------------------------------------
# 5. psutil stub
# ---------------------------------------------------------------------------
_make_module("psutil")

# ---------------------------------------------------------------------------
# 6. requests — the real library is installed; no stub needed.
# ---------------------------------------------------------------------------
