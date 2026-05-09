from __future__ import annotations

import json
import html as _html
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
_OWNER_DEBUG_MAX_LEN = 400
_GUEST_QUERY_TEXT_MAX_LEN = 4000
_HTML_TAG_RE = re.compile(r"<[^>]+>")


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
                "message",
                "edited_message",
                "channel_post",
                "edited_channel_post",
                "callback_query",
            ],
            ensure_ascii=False,
        ),
    }
    if offset is not None:
        payload["offset"] = int(offset)
    data = _api_request("getUpdates", params=payload, timeout=(10.0, 70.0))
    if isinstance(data, dict) and not data.get("ok"):
        desc = str(data.get("description") or "").lower()
        if "allowed updates" in desc or "can't parse" in desc:
            fallback_payload: dict[str, str | int] = {"timeout": 50}
            if offset is not None:
                fallback_payload["offset"] = int(offset)
            data = _api_request("getUpdates", params=fallback_payload, timeout=(10.0, 70.0))
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


def _extract_chat_context(payload_obj: dict, update_obj: dict | None = None) -> tuple[int, int | None, int | None]:
    chat_id = 0
    message_thread_id: int | None = None
    reply_to_message_id: int | None = None
    chat = payload_obj.get("chat")
    if isinstance(chat, dict):
        try:
            chat_id = int(chat.get("id") or 0)
        except Exception:
            chat_id = 0
    if not chat_id:
        try:
            chat_id = int(payload_obj.get("chat_id") or 0)
        except Exception:
            chat_id = 0
    try:
        message_thread_id = int(payload_obj.get("message_thread_id"))
    except Exception:
        message_thread_id = None
    try:
        reply_to_message_id = int(payload_obj.get("message_id"))
    except Exception:
        reply_to_message_id = None

    message = payload_obj.get("message")
    if isinstance(message, dict):
        nested_chat_id, nested_thread_id, nested_reply_to = _extract_chat_context(message, None)
        if not chat_id:
            chat_id = nested_chat_id
        if message_thread_id is None:
            message_thread_id = nested_thread_id
        if reply_to_message_id is None:
            reply_to_message_id = nested_reply_to

    if not chat_id and isinstance(update_obj, dict):
        for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
            upd_payload = update_obj.get(key)
            if isinstance(upd_payload, dict):
                nested_chat_id, nested_thread_id, nested_reply_to = _extract_chat_context(upd_payload, None)
                if nested_chat_id:
                    chat_id = nested_chat_id
                    if message_thread_id is None:
                        message_thread_id = nested_thread_id
                    if reply_to_message_id is None:
                        reply_to_message_id = nested_reply_to
                    break
    return chat_id, message_thread_id, reply_to_message_id


def _send_message_response(
    chat_id: int,
    response_text: str,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> bool:
    if not chat_id or not response_text:
        return False
    payload = {
        "chat_id": int(chat_id),
        "text": response_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    if message_thread_id:
        payload["message_thread_id"] = int(message_thread_id)
    result = _api_request("sendMessage", params=payload, timeout=(10.0, 30.0))
    if isinstance(result, dict) and result.get("ok"):
        return True
    description = str(result.get("description") or "") if isinstance(result, dict) else ""
    if "ENTITY_TEXT_INVALID" in description:
        payload.pop("parse_mode", None)
        fallback_result = _api_request("sendMessage", params=payload, timeout=(10.0, 30.0))
        if isinstance(fallback_result, dict) and fallback_result.get("ok"):
            return True
    if description:
        logger.warning("[GUEST RUNTIME] sendMessage failed: %s", description)
    return False


def _get_owner_user_id(conn: sqlite3.Connection, bot_username: str) -> int:
    row = conn.execute(
        """
        SELECT owner_user_id
        FROM guest_bots
        WHERE lower(bot_username) = ?
        LIMIT 1
        """,
        (bot_username.lower(),),
    ).fetchone()
    try:
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def _send_owner_problem_report(
    conn: sqlite3.Connection,
    bot_username: str,
    cmd_key: str,
    reason: str,
    raw_text: str = "",
) -> bool:
    owner_user_id = _get_owner_user_id(conn, bot_username)
    if not owner_user_id:
        return False
    reason_safe = _html.escape((reason or "").strip()[:_OWNER_DEBUG_MAX_LEN])
    cmd_safe = _html.escape((cmd_key or "").strip()[:_CMD_MAX_NAME_LEN])
    raw_safe = _html.escape((raw_text or "").strip()[:_OWNER_DEBUG_MAX_LEN])
    text = (
        f"⚠️ <b>@{_html.escape(bot_username)} не смог обработать guest-команду</b>\n"
        f"<b>Команда:</b> <code>{cmd_safe}</code>\n"
        f"<b>Причина:</b> <code>{reason_safe or 'unknown'}</code>"
    )
    if raw_safe:
        text += f"\n<b>Вход:</b> <code>{raw_safe}</code>"
    return _send_message_response(owner_user_id, text)


def _extract_guest_query_id(payload_obj: dict, update_obj: dict | None = None) -> str:
    """Return guest query ID from legacy/new update layouts.

    Supports IDs in guest_message, guest_query, and nested guest_query payloads.
    """
    def _candidate_id(candidate: object, *, allow_plain_id: bool = False) -> str:
        if not isinstance(candidate, dict):
            return ""
        value = candidate.get("guest_query_id")
        if value is None and allow_plain_id:
            value = candidate.get("id")
        return str(value).strip() if value is not None else ""

    direct_id = _candidate_id(payload_obj)
    if direct_id:
        return direct_id

    for source in (payload_obj, update_obj):
        if not isinstance(source, dict):
            continue
        nested_guest_query = source.get("guest_query")
        nested_id = _candidate_id(nested_guest_query, allow_plain_id=True)
        if nested_id:
            return nested_id

        for nested_key in ("guest_message", "message"):
            nested_payload = source.get(nested_key)
            nested_id = _candidate_id(nested_payload)
            if nested_id:
                return nested_id
            if isinstance(nested_payload, dict):
                nested_guest_query = nested_payload.get("guest_query")
                nested_id = _candidate_id(nested_guest_query, allow_plain_id=True)
                if nested_id:
                    return nested_id
    return ""


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

    for nested_key in ("guest_message", "message", "guest_query"):
        nested_payload = payload_obj.get(nested_key)
        if isinstance(nested_payload, dict):
            nested_text = _extract_message_text(nested_payload, None, _depth + 1)
            if nested_text:
                return nested_text

    if isinstance(update_obj, dict):
        for nested_key in ("guest_query", "guest_message"):
            nested_payload = update_obj.get(nested_key)
            if isinstance(nested_payload, dict):
                nested_text = _extract_message_text(nested_payload, None, _depth + 1)
                if nested_text:
                    return nested_text
    return ""


def _answer_guest_query(guest_query_id: str, response_text: str) -> bool:
    if not guest_query_id or not response_text:
        return False

    clean_response_text = _HTML_TAG_RE.sub("", str(response_text or ""))
    clean_response_text = _html.unescape(clean_response_text).strip()
    if not clean_response_text:
        return False
    clean_response_text = clean_response_text[:_GUEST_QUERY_TEXT_MAX_LEN]

    # answerGuestQuery accepts plain text, so we avoid sendMessage-only fields.
    payloads = [
        {
            "guest_query_id": guest_query_id,
            "text": clean_response_text,
        },
        {
            "guest_query_id": guest_query_id,
            "message_text": clean_response_text,
        },
        {
            "guest_query_id": guest_query_id,
            "response_text": clean_response_text,
        },
    ]
    last_error = ""
    for payload in payloads:
        result = _api_request("answerGuestQuery", params=payload, timeout=(10.0, 30.0))
        if isinstance(result, dict) and result.get("ok"):
            return True
        description = str(result.get("description") or "") if isinstance(result, dict) else ""
        if description:
            last_error = description
    if last_error:
        logger.warning("[GUEST RUNTIME] answerGuestQuery failed: %s", last_error)
    return False


def _handle_guest_update(update_obj: dict) -> None:
    guest_payload = None
    for key in ("guest_message", "guest_query", "message", "edited_message", "channel_post", "edited_channel_post"):
        candidate = update_obj.get(key)
        if isinstance(candidate, dict):
            guest_payload = candidate
            break
    if not isinstance(guest_payload, dict):
        return

    guest_query_id = _extract_guest_query_id(guest_payload, update_obj)
    chat_id, message_thread_id, reply_to_message_id = _extract_chat_context(guest_payload, update_obj)

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
            if not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "bot disabled", text):
                logger.warning("[GUEST RUNTIME] failed to notify owner about disabled bot for cmd=%s", cmd_key)
            return
        response = _resolve_guest_response(conn, _BOT_USERNAME, cmd_key)
        if not response:
            if not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "command not found or module disabled", text):
                logger.warning("[GUEST RUNTIME] failed to notify owner about unresolved cmd=%s", cmd_key)
            return
        sent = False
        if guest_query_id:
            sent = _answer_guest_query(guest_query_id, response)
        if not sent and chat_id:
            if not guest_query_id:
                logger.debug("[GUEST RUNTIME] no guest_query_id; using sendMessage fallback")
            sent = _send_message_response(
                chat_id=chat_id,
                response_text=response,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
            )
        if not sent:
            if not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "failed to deliver response", text):
                logger.warning("[GUEST RUNTIME] failed to notify owner about delivery failure cmd=%s", cmd_key)
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
