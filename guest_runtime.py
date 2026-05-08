from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import json
import threading

import telebot


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guest_runtime")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "/data").strip()
DB_PATH = os.path.join(DATA_DIR, "bot_data.sqlite3")
BOT_THREADS = max(1, int(os.getenv("BOT_THREADS", "4")))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required for guest runtime")

try:
    bot = telebot.TeleBot(TOKEN, parse_mode="HTML", num_threads=BOT_THREADS)
except Exception as e:
    raise RuntimeError(f"Failed to initialize guest bot runtime: {e}") from e

_BOT_USERNAME = ""

_CMD_MAX_NAME_LEN = 30
_CMD_STRIP_CHARS = "`'\"«»()[]{}<>.,;:!?"


def _db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def _normalize_key(value: str) -> str:
    return (value or "").strip().lower().strip(_CMD_STRIP_CHARS).strip()


def _extract_command_key(text: str, bot_username: str) -> str | None:
    raw = (text or "").strip()
    if not raw or not bot_username:
        return None

    tokens = raw.split()
    if not tokens:
        return None

    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""
    mention = ""
    cmd = ""

    if first.startswith("@"):
        mention = first[1:].strip().lower()
        cmd = second.lstrip("/")
    elif first.startswith("/") and "@" in first:
        cmd_part, sep, mention_part = first[1:].partition("@")
        if not sep:
            return None
        mention = mention_part.strip().lower()
        cmd = cmd_part.strip()
    else:
        return None

    if mention != bot_username.lower():
        return None

    cmd_key = _normalize_key(cmd)
    if (
        not cmd_key
        or cmd_key.startswith("@")
        or cmd_key.startswith("/")
        or len(cmd_key) > _CMD_MAX_NAME_LEN
    ):
        return None
    return cmd_key


def _bot_is_enabled(conn: sqlite3.Connection, bot_username: str) -> bool:
    row = conn.execute(
        """
        SELECT enabled
        FROM guest_bots
        WHERE lower(bot_username) = ?
        LIMIT 1
        """,
        (bot_username.lower(),),
    ).fetchone()
    return bool(row and int(row[0] or 0) == 1)


def _resolve_guest_response(conn: sqlite3.Connection, bot_username: str, cmd_key: str) -> str | None:
    row = conn.execute(
        """
        SELECT gc.response_text, gb.linked_modules_json
        FROM guest_bots gb
        JOIN guest_commands gc ON gc.guest_bot_id = gb.id
        WHERE lower(gb.bot_username) = ?
          AND gb.enabled = 1
          AND gc.enabled = 1
          AND gc.name = ?
        LIMIT 1
        """,
        (bot_username.lower(), cmd_key.lower()),
    ).fetchone()
    if not row:
        return None
    try:
        modules = json.loads(row[1] or "[]")
    except Exception:
        modules = []
    if not isinstance(modules, list) or "commands" not in [str(m).lower() for m in modules]:
        return None
    text = str(row[0] or "").strip()
    return text or None


@bot.message_handler(func=lambda m: m.chat.type in ("group", "supergroup") and bool(getattr(m, "text", None)))
def on_guest_command(m: telebot.types.Message):
    if not m.from_user:
        return
    text = (m.text or "").strip()
    if not text:
        return

    cmd_key = _extract_command_key(text, _BOT_USERNAME)
    if not cmd_key:
        return

    conn = None
    try:
        conn = _db_connect()
        if not _bot_is_enabled(conn, _BOT_USERNAME):
            return
        response = _resolve_guest_response(conn, _BOT_USERNAME, cmd_key)
        if not response:
            return
        bot.send_message(m.chat.id, response, reply_to_message_id=m.message_id)
    except Exception as e:
        logger.warning("[GUEST RUNTIME] command handling failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _wait_until_disabled() -> None:
    while True:
        time.sleep(10)
        conn = None
        try:
            conn = _db_connect()
            if not _bot_is_enabled(conn, _BOT_USERNAME):
                logger.info("[GUEST RUNTIME] bot @%s disabled in DB; stopping", _BOT_USERNAME)
                bot.stop_polling()
                return
        except Exception as e:
            logger.warning("[GUEST RUNTIME] disable watcher error: %s", e)
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


if __name__ == "__main__":
    me = bot.get_me()
    _BOT_USERNAME = (getattr(me, "username", "") or "").lower()
    if not _BOT_USERNAME:
        raise RuntimeError("Guest runtime requires bot username")

    logger.info("Starting guest runtime for @%s", _BOT_USERNAME)

    threading.Thread(target=_wait_until_disabled, daemon=True, name="guest-disable-watch").start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
