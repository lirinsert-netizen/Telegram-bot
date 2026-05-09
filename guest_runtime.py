from __future__ import annotations

import json
import logging
import os
import re
import requests
import sqlite3
import threading
import time


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("guest_runtime")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATA_DIR = os.getenv("DATA_DIR", "/data").strip()
DB_PATH = os.path.join(DATA_DIR, "bot_data.sqlite3")
BOT_THREADS = max(1, int(os.getenv("BOT_THREADS", "4")))
API_BASE_URL = f"https://api.telegram.org/bot{TOKEN}"
_HTTP_SESSION = requests.Session()
_STOP_EVENT = threading.Event()

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is required for guest runtime")

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


def _extract_guest_command_key(text: str, bot_username: str) -> str | None:
    direct_key = _extract_command_key(text, bot_username)
    if direct_key:
        return direct_key

    raw = (text or "").strip()
    if not raw:
        return None
    first = raw.split()[0].strip()
    if not first:
        return None

    if first.startswith("/"):
        cmd = first[1:]
        if "@" in cmd:
            cmd_part, sep, mention_part = cmd.partition("@")
            if not sep or mention_part.strip().lower() != bot_username.lower():
                return None
            cmd = cmd_part
        key = _normalize_key(cmd)
        if not key or len(key) > _CMD_MAX_NAME_LEN:
            return None
        return key

    key = _normalize_key(first)
    if not key or len(key) > _CMD_MAX_NAME_LEN:
        return None
    return key


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


def _api_request(method: str, params: dict | None = None, timeout: tuple[float, float] = (10.0, 70.0)) -> dict | None:
    try:
        response = _HTTP_SESSION.post(
            f"{API_BASE_URL}/{method}",
            data=params or {},
            timeout=timeout,
        )
        data = response.json()
        if isinstance(data, dict):
            return data
    except Exception as e:
        logger.warning("[GUEST RUNTIME] API request failed %s: %s", method, e)
    return None


def _get_bot_username() -> str:
    data = _api_request("getMe", timeout=(10.0, 20.0))
    if not isinstance(data, dict) or not data.get("ok"):
        return ""
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return ""
    return str(result.get("username") or "").strip().lower()


def _poll_guest_updates(offset: int | None) -> list[dict]:
    payload: dict[str, str | int] = {
        "timeout": 50,
        "allowed_updates": json.dumps(
            [
                "guest_message",
                "edited_guest_message",
                "deleted_guest_messages",
                "guest_query",
            ],
            ensure_ascii=False,
        ),
    }
    if offset is not None:
        payload["offset"] = int(offset)
    data = _api_request("getUpdates", params=payload, timeout=(10.0, 70.0))
    if not isinstance(data, dict):
        return []
    if not data.get("ok"):
        logger.warning("[GUEST RUNTIME] getUpdates failed: %s", data.get("description"))
        time.sleep(2)
        return []
    result = data.get("result")
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, dict)]


def _extract_guest_query_id(payload_obj: dict, update_obj: dict | None = None) -> str:
    """Return guest query ID from legacy/new update layouts.

    Supports IDs in guest_message, guest_query, and nested guest_query payloads.
    """
    value = payload_obj.get("guest_query_id")
    if value is None and isinstance(update_obj, dict):
        guest_query = update_obj.get("guest_query")
        if isinstance(guest_query, dict):
            value = guest_query.get("guest_query_id")
            if value is None:
                value = guest_query.get("id")
    if value is None:
        nested_guest_query = payload_obj.get("guest_query")
        if isinstance(nested_guest_query, dict):
            value = nested_guest_query.get("guest_query_id")
            if value is None:
                value = nested_guest_query.get("id")
    if value is None:
        return ""
    return str(value).strip()


def _extract_message_text(
    payload_obj: dict,
    update_obj: dict | None = None,
    _depth: int = 0,
) -> str:
    """Extract command text from guest payloads with bounded recursive fallback.

    Handles both guest_message and guest_query shapes and nested message objects.
    """
    if _depth > 4:
        return ""
    text = payload_obj.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    caption = payload_obj.get("caption")
    if isinstance(caption, str) and caption.strip():
        return caption.strip()

    for key in ("message_text", "query", "data", "command"):
        value = payload_obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    message = payload_obj.get("message")
    if isinstance(message, dict):
        nested_text = _extract_message_text(message, None, _depth + 1)
        if nested_text:
            return nested_text

    if isinstance(update_obj, dict):
        guest_query = update_obj.get("guest_query")
        if isinstance(guest_query, dict):
            nested_text = _extract_message_text(guest_query, None, _depth + 1)
            if nested_text:
                return nested_text
    return ""


def _answer_guest_query(guest_query_id: str, response_text: str) -> bool:
    if not guest_query_id or not response_text:
        return False

    # Bot API 10.0 supports answerGuestQuery, but some wrappers still expect
    # alternative text field names, so we retry with compatible payload keys.
    payloads = [
        {
            "guest_query_id": guest_query_id,
            "text": response_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        {
            "guest_query_id": guest_query_id,
            "message_text": response_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        {
            "guest_query_id": guest_query_id,
            "response_text": response_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    ]
    last_error = ""
    for payload in payloads:
        for candidate in (payload, {k: v for k, v in payload.items() if k != "parse_mode"}):
            result = _api_request("answerGuestQuery", params=candidate, timeout=(10.0, 30.0))
            if isinstance(result, dict) and result.get("ok"):
                return True
            if isinstance(result, dict):
                last_error = str(result.get("description") or "")
    if last_error:
        logger.warning("[GUEST RUNTIME] answerGuestQuery failed: %s", last_error)
    return False


def _handle_guest_update(update_obj: dict) -> None:
    guest_payload = update_obj.get("guest_message")
    if not isinstance(guest_payload, dict):
        guest_payload = update_obj.get("guest_query")
    if not isinstance(guest_payload, dict):
        return

    guest_query_id = _extract_guest_query_id(guest_payload, update_obj)
    if not guest_query_id:
        return

    text = _extract_message_text(guest_payload, update_obj)
    if not text:
        return

    cmd_key = _extract_guest_command_key(text, _BOT_USERNAME)
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
        _answer_guest_query(guest_query_id, response)
    except Exception as e:
        logger.warning("[GUEST RUNTIME] command handling failed: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _run_guest_polling_loop() -> None:
    offset: int | None = None
    while not _STOP_EVENT.is_set():
        updates = _poll_guest_updates(offset)
        if not updates:
            continue
        for update_obj in updates:
            update_id = update_obj.get("update_id")
            if isinstance(update_id, int):
                offset = update_id + 1
            else:
                try:
                    offset = int(update_id) + 1
                except Exception:
                    pass
            _handle_guest_update(update_obj)


def _wait_until_disabled() -> None:
    while not _STOP_EVENT.is_set():
        time.sleep(10)
        conn = None
        try:
            conn = _db_connect()
            if not _bot_is_enabled(conn, _BOT_USERNAME):
                logger.info("[GUEST RUNTIME] bot @%s disabled in DB; stopping", _BOT_USERNAME)
                _STOP_EVENT.set()
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
    _BOT_USERNAME = _get_bot_username()
    if not _BOT_USERNAME:
        raise RuntimeError("Guest runtime requires bot username")

    logger.info("Starting guest runtime for @%s", _BOT_USERNAME)

    threading.Thread(target=_wait_until_disabled, daemon=True, name="guest-disable-watch").start()
    _run_guest_polling_loop()
