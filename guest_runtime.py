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
from html.parser import HTMLParser

from guest_ai_service import GuestAIService


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
# answerGuestQuery returns a message-like text payload. We keep a 96-char reserve
# below Telegram's 4096-char ceiling to stay safe after cleanup and future tweaks.
_GUEST_QUERY_TEXT_MAX_LEN = 4000
# InlineQueryResultArticle.title shown in the inline picker; keep it brief.
_INLINE_ARTICLE_TITLE_MAX_LEN = 64
_AI_MIN_WORD_COUNT_DEFAULT = 4
_AI_MIN_WORD_COUNT_OWNER_DEV = 2
_AI_FALLBACK_TEXT = "⚠️ <b>ИИ временно недоступен.</b>\nПопробуйте чуть позже."
_AI_ACCESS_ALL = "all"
_AI_ACCESS_OWNER = "owner"
_GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant").strip() or "llama-3.1-8b-instant"
_AI_SERVICE = GuestAIService(api_key=_GROQ_API_KEY, model=_GROQ_MODEL)


class _GuestQueryHTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self._chunks.append(data)

    def get_text(self) -> str:
        return "".join(self._chunks)


def _prepare_guest_query_text(response_text: str) -> str:
    raw_text = str(response_text or "").strip()
    if not raw_text:
        return ""
    parser = _GuestQueryHTMLStripper()
    try:
        parser.feed(raw_text)
        parser.close()
        clean_text = parser.get_text().strip()
    except Exception as e:
        logger.warning("[GUEST RUNTIME] failed to strip HTML from guest response: %s", e)
        clean_text = ""
    if not clean_text:
        clean_text = _html.unescape(raw_text).strip()
    return clean_text[:_GUEST_QUERY_TEXT_MAX_LEN]


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
    mention = ""
    cmd = ""

    if first.startswith("@"):
        mention = first[1:].strip().lower()
        cmd = " ".join(tokens[1:]).removeprefix("/").strip()
    elif first.startswith("/") and "@" in first:
        cmd_part, sep, mention_part = first[1:].partition("@")
        if not sep:
            return None
        mention = mention_part.strip().lower()
        tail = " ".join(tokens[1:]).strip()
        cmd = cmd_part.strip()
        if tail:
            cmd = f"{cmd} {tail}".strip()
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
        tail = " ".join(raw.split()[1:]).strip()
        cmd_full = cmd.strip()
        if tail:
            cmd_full = f"{cmd_full} {tail}".strip()
        key = _normalize_key(cmd_full)
        if not key or len(key) > _CMD_MAX_NAME_LEN:
            return None
        return key

    key = _normalize_key(raw)
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


def _resolve_guest_response(conn: sqlite3.Connection, bot_username: str, cmd_key: str, sender_id: int = 0) -> str | None:
    row = conn.execute(
        """
        SELECT gc.response_text, gb.linked_modules_json, gc.owner_only, gb.owner_user_id
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
    owner_only = bool(int(row[2] or 0))
    owner_user_id = int(row[3] or 0)
    if owner_only and sender_id and sender_id != owner_user_id and not _is_dev_user(sender_id):
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
    _html_error = (
        "ENTITY_TEXT_INVALID" in description
        or "can't parse entities" in description.lower()
        or "wrong html" in description.lower()
    )
    if _html_error:
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


_VERIFY_DEV_FILE = os.path.join(DATA_DIR, "verify_dev.json")
_VERIFY_DEV_CACHE: set[int] | None = None
_VERIFY_DEV_CACHE_TIME: float = 0.0
_VERIFY_DEV_TTL = 60.0  # seconds


def _is_dev_user(user_id: int) -> bool:
    """Check if user_id is in the dev list (reads from JSON file with caching)."""
    global _VERIFY_DEV_CACHE, _VERIFY_DEV_CACHE_TIME
    now = time.time()
    if _VERIFY_DEV_CACHE is None or now - _VERIFY_DEV_CACHE_TIME > _VERIFY_DEV_TTL:
        try:
            with open(_VERIFY_DEV_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                _VERIFY_DEV_CACHE = {int(v) for v in data if v is not None}
            else:
                _VERIFY_DEV_CACHE = set()
        except Exception:
            _VERIFY_DEV_CACHE = set()
        _VERIFY_DEV_CACHE_TIME = now
    return user_id in (_VERIFY_DEV_CACHE or set())


def _is_allowed_pm_user(user_id: int, conn: sqlite3.Connection) -> bool:
    """Return True if user_id is the owner of this bot or a dev user."""
    owner_id = _get_owner_user_id(conn, _BOT_USERNAME)
    if owner_id and user_id == owner_id:
        return True
    return _is_dev_user(user_id)


def _normalize_ai_access_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    return _AI_ACCESS_OWNER if mode == _AI_ACCESS_OWNER else _AI_ACCESS_ALL


def _get_ai_access_mode(conn: sqlite3.Connection, bot_username: str) -> str:
    row = conn.execute(
        """
        SELECT ai_access_mode
        FROM guest_bots
        WHERE lower(bot_username) = ?
        LIMIT 1
        """,
        (bot_username.lower(),),
    ).fetchone()
    raw = row[0] if row else _AI_ACCESS_ALL
    return _normalize_ai_access_mode(str(raw or ""))


def _set_ai_access_mode(conn: sqlite3.Connection, bot_username: str, mode: str) -> bool:
    ts = int(time.time())
    norm_mode = _normalize_ai_access_mode(mode)
    try:
        conn.execute(
            """
            UPDATE guest_bots
            SET ai_access_mode = ?, updated_at = ?
            WHERE lower(bot_username) = ?
            """,
            (norm_mode, ts, bot_username.lower()),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("[GUEST RUNTIME] set ai_access_mode error: %s", e)
        return False


def _is_owner_or_dev_sender(sender_id: int, owner_user_id: int) -> bool:
    if not sender_id:
        return False
    if owner_user_id and sender_id == owner_user_id:
        return True
    return _is_dev_user(sender_id)


def _is_sender_allowed_for_ai(sender_id: int, owner_user_id: int, ai_access_mode: str) -> bool:
    mode = _normalize_ai_access_mode(ai_access_mode)
    if mode == _AI_ACCESS_ALL:
        return True
    return _is_owner_or_dev_sender(sender_id, owner_user_id)


def _detect_owner_intent(text: str) -> str:
    normalized = _normalize_space(text)
    if not normalized:
        return "question"
    lowered = normalized.lower()
    if "?" in normalized:
        return "question"
    question_starts = (
        "кто", "что", "где", "когда", "почему", "зачем", "как", "какой", "какая", "какие",
        "чей", "чья", "чьи", "можно", "нужно ли", "правда ли", "ли ",
    )
    if lowered.startswith(question_starts):
        return "question"
    return "command"


# -------- PM command management state machine --------

_RT_PENDING: dict[int, dict] = {}  # user_id -> state dict
_CMD_MAX_NAME_LEN_RT = 30
_MAX_RESPONSE_LEN_RT = 3500


def _rt_api(method: str, params: dict) -> dict | None:
    return _api_request(method, params, timeout=(10.0, 30.0))


def _rt_send(chat_id: int, text: str, reply_markup: dict | None = None) -> int | None:
    """Send a message; returns message_id or None."""
    payload: dict = {
        "chat_id": int(chat_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    result = _rt_api("sendMessage", payload)
    if isinstance(result, dict) and result.get("ok"):
        msg = result.get("result") or {}
        if isinstance(msg, dict):
            return int(msg.get("message_id") or 0) or None
    return None


def _rt_delete_message(chat_id: int, message_id: int | None) -> bool:
    if not chat_id or not message_id:
        return False
    result = _rt_api("deleteMessage", {"chat_id": int(chat_id), "message_id": int(message_id)})
    return isinstance(result, dict) and bool(result.get("ok"))


def _rt_replace_pending_ui(chat_id: int, state: dict, text: str, reply_markup: dict | None = None, also_delete_msg_id: int | None = None) -> int | None:
    if not isinstance(state, dict):
        state = {}
    old_ui_id = state.get("_ui_msg_id")
    try:
        old_ui_id_int = int(old_ui_id) if old_ui_id else 0
    except Exception:
        old_ui_id_int = 0
    if old_ui_id_int:
        _rt_delete_message(chat_id, old_ui_id_int)
    if also_delete_msg_id:
        _rt_delete_message(chat_id, also_delete_msg_id)
    sent_id = _rt_send(chat_id, text, reply_markup)
    if sent_id:
        state["_ui_msg_id"] = int(sent_id)
    return sent_id


def _rt_clear_pending_ui(chat_id: int, state: dict | None = None, also_delete_msg_id: int | None = None) -> None:
    if isinstance(state, dict):
        old_ui_id = state.pop("_ui_msg_id", None)
        try:
            old_ui_id_int = int(old_ui_id) if old_ui_id else 0
        except Exception:
            old_ui_id_int = 0
        if old_ui_id_int:
            _rt_delete_message(chat_id, old_ui_id_int)
    if also_delete_msg_id:
        _rt_delete_message(chat_id, also_delete_msg_id)


def _rt_edit(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> bool:
    """Edit a message text. Returns True on success."""
    payload: dict = {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    result = _rt_api("editMessageText", payload)
    return isinstance(result, dict) and bool(result.get("ok"))


def _rt_answer_cq(query_id: str, text: str = "", show_alert: bool = False) -> None:
    payload: dict = {"callback_query_id": query_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    _rt_api("answerCallbackQuery", payload)


def _rt_inline_kb(*rows: list[dict]) -> dict:
    return {"inline_keyboard": list(rows)}


def _rt_btn(text: str, callback_data: str, style: str | None = None, icon_custom_emoji_id: str | None = None) -> dict:
    """Build an inline keyboard button dict.

    The 'style' field is a non-standard Telegram Bot API extension supported
    by this server for button colouring. Valid values: 'primary', 'danger', 'success'.
    """
    btn: dict = {"text": text, "callback_data": callback_data}
    if icon_custom_emoji_id:
        btn["icon_custom_emoji_id"] = str(icon_custom_emoji_id)
    elif text == "Назад":
        btn["icon_custom_emoji_id"] = "5963223853231509569"
    elif text == "Отмена":
        btn["icon_custom_emoji_id"] = "5465665476971471368"
    if style:
        btn["style"] = style
    return btn


# ---- Emoji constants for UI ----
_E_LIST     = '<tg-emoji emoji-id="5334882760735598374">📋</tg-emoji>'
_E_ADD      = '<tg-emoji emoji-id="5226945370684140473">➕</tg-emoji>'
_E_OK       = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
_E_OFF      = '<tg-emoji emoji-id="5465665476971471368">❌</tg-emoji>'
# Note: _E_TEXT uses the same custom emoji ID as _E_LIST; this mirrors the main bot config
# where EMOJI_LIST_ID == EMOJI_WELCOME_TEXT_ID == "5334882760735598374".
_E_TEXT     = _E_LIST
_E_SETTINGS = '<tg-emoji emoji-id="5341715473882955310">⚙️</tg-emoji>'


def _rt_list_commands_for_bot(conn: sqlite3.Connection, bot_username: str) -> list[dict]:
    """List all commands for this bot from DB."""
    try:
        row = conn.execute(
            "SELECT id FROM guest_bots WHERE lower(bot_username) = ? LIMIT 1",
            (bot_username.lower(),),
        ).fetchone()
        if not row:
            return []
        guest_bot_id = int(row[0])
        rows = conn.execute(
            """
            SELECT id, name, response_text, enabled, owner_only
            FROM guest_commands
            WHERE guest_bot_id = ?
            ORDER BY name ASC
            """,
            (guest_bot_id,),
        ).fetchall()
        return [
            {
                "id": int(r[0]),
                "name": str(r[1] or ""),
                "response_text": str(r[2] or ""),
                "enabled": bool(int(r[3] or 0)),
                "owner_only": bool(int(r[4] or 0)),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[GUEST RUNTIME] list commands error: %s", e)
        return []


def _rt_get_guest_bot_id(conn: sqlite3.Connection, bot_username: str) -> int:
    try:
        row = conn.execute(
            "SELECT id FROM guest_bots WHERE lower(bot_username) = ? LIMIT 1",
            (bot_username.lower(),),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


# ---- Main commands page (shown on /start) ----

def _rt_build_main_text(cmds: list[dict], ai_access_mode: str) -> str:
    count = len(cmds)
    ai_label = "ИИ для владельца" if ai_access_mode == _AI_ACCESS_OWNER else "ИИ для всех"
    return (
        f"{_E_LIST} <b>Команды</b>\n\n"
        f"Создайте пользовательские команды для бота @{_html.escape(_BOT_USERNAME)}. "
        f"Для вызова команды напишите /имя команды или @{_html.escape(_BOT_USERNAME)} имя команды.\n\n"
        f"<b>Количество команд:</b> <code>{count}</code>\n"
        f"<b>Режим ИИ:</b> <code>{_html.escape(ai_label)}</code>\n"
        f"<b>Порог ИИ:</b> <code>{_AI_MIN_WORD_COUNT_DEFAULT}+</code> слов (обычные пользователи), "
        f"<code>{_AI_MIN_WORD_COUNT_OWNER_DEV}+</code> слова (владелец/разработчик)"
    )


def _rt_build_main_kb(ai_access_mode: str) -> dict:
    mode = _normalize_ai_access_mode(ai_access_mode)
    if mode == _AI_ACCESS_OWNER:
        btn_ai_all = _rt_btn("ИИ для всех", "gcmd:ai_all")
        btn_ai_owner = _rt_btn("»ИИ для владельца«", "gcmd:ai_owner", style="primary")
    else:
        btn_ai_all = _rt_btn("»ИИ для всех«", "gcmd:ai_all", style="primary")
        btn_ai_owner = _rt_btn("ИИ для владельца", "gcmd:ai_owner")
    return _rt_inline_kb(
        [_rt_btn("Список команд", "gcmd:list:0", icon_custom_emoji_id="5334882760735598374")],
        [_rt_btn("Добавить команду", "gcmd:add", icon_custom_emoji_id="5226945370684140473")],
        [_rt_btn("Удалить команду", "gcmd:del_list", icon_custom_emoji_id="5229113891081956317")],
        [btn_ai_all, btn_ai_owner],
    )


# ---- List page (paginated) ----

def _rt_build_list_text(cmds: list[dict], page: int = 0) -> str:
    if not cmds:
        return f"{_E_SETTINGS} <b>Список команд</b>\n\n<i>Нет созданных команд.</i>"
    page_size = 10
    total_pages = max(1, (len(cmds) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = cmds[start:start + page_size]
    header = f"{_E_SETTINGS} <b>Список команд ({page + 1}/{total_pages})</b>\n"
    lines = [header]
    for i, cmd in enumerate(chunk, start=start + 1):
        access_label = "Для владельца" if cmd["owner_only"] else "Все пользователи"
        lines.append(f"{i}. <code>{_html.escape(cmd['name'])}</code> — {_html.escape(access_label)}")
    return "\n".join(lines)


def _rt_build_list_kb(cmds: list[dict], page: int = 0) -> dict:
    page_size = 10
    total_pages = max(1, (len(cmds) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    rows: list[list[dict]] = []
    nav: list[dict] = []
    if page > 0:
        nav.append(_rt_btn("◀", f"gcmd:list:{page - 1}"))
    if page < total_pages - 1:
        nav.append(_rt_btn("▶", f"gcmd:list:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([_rt_btn("Назад", "gcmd:main", style="primary")])
    return _rt_inline_kb(*rows)


# ---- Draft (command creation) page ----

def _rt_build_draft_text(draft: dict) -> str:
    name = _html.escape(draft.get("name") or "")
    has_text = "есть" if draft.get("text") else "нет"
    owner_only = bool(draft.get("owner_only"))
    access_label = "Для владельца" if owner_only else "Все пользователи"
    name_str = f"<code>{name}</code>" if name else "<i>не задано</i>"
    return (
        f"{_E_ADD} <b>Новая команда</b>\n\n"
        f"<b>Имя:</b> {name_str}\n"
        f"<b>Текст:</b> <code>{has_text}</code>\n"
        f"<b>Доступ:</b> {access_label}"
    )


def _rt_build_draft_kb(draft: dict) -> dict:
    owner_only = bool(draft.get("owner_only"))
    btn_text = _rt_btn("Текст", "gcmd:draft_text", icon_custom_emoji_id="5334882760735598374")
    if owner_only:
        btn_owner = _rt_btn("»Для владельца«", "gcmd:draft_owner", style="primary")
        btn_all = _rt_btn("Все пользователи", "gcmd:draft_all")
    else:
        btn_owner = _rt_btn("Для владельца", "gcmd:draft_owner")
        btn_all = _rt_btn("»Все пользователи«", "gcmd:draft_all", style="primary")
    btn_discard = _rt_btn("Удалить", "gcmd:draft_cancel", style="danger")
    btn_save = _rt_btn("Сохранить", "gcmd:draft_save", style="success")
    return _rt_inline_kb(
        [btn_text],
        [btn_owner, btn_all],
        [btn_discard, btn_save],
        [_rt_btn("Назад", "gcmd:main", style="primary")],
    )


def _rt_build_delete_prompt(cmds: list[dict]) -> str:
    cmd_names_list = "\n".join(
        f"{i + 1}. <code>{_html.escape(cmd.get('name') or '')}</code>"
        for i, cmd in enumerate(cmds)
    )
    return (
        f"{_E_SETTINGS} <b>Удалить команду</b>\n\n"
        "Введите <b>имя команды</b> для удаления.\n\n"
        f"<b>Список команд:</b>\n{cmd_names_list}"
    )


def _rt_show_main(chat_id: int, conn: sqlite3.Connection) -> None:
    cmds = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
    ai_access_mode = _get_ai_access_mode(conn, _BOT_USERNAME)
    _rt_send(chat_id, _rt_build_main_text(cmds, ai_access_mode), _rt_build_main_kb(ai_access_mode))


def _rt_upsert_command(conn: sqlite3.Connection, bot_username: str, name: str, text: str, owner_only: bool) -> bool:
    try:
        guest_bot_id = _rt_get_guest_bot_id(conn, bot_username)
        if not guest_bot_id:
            return False
        ts = int(time.time())
        conn.execute(
            """
            INSERT INTO guest_commands(guest_bot_id, name, response_text, enabled, owner_only, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(guest_bot_id, name) DO UPDATE SET
                response_text = excluded.response_text,
                enabled = 1,
                owner_only = excluded.owner_only,
                updated_at = excluded.updated_at
            """,
            (guest_bot_id, name.strip().lower(), text, 1 if owner_only else 0, ts, ts),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("[GUEST RUNTIME] upsert command error: %s", e)
        return False


def _rt_delete_command(conn: sqlite3.Connection, bot_username: str, name: str) -> bool:
    try:
        guest_bot_id = _rt_get_guest_bot_id(conn, bot_username)
        if not guest_bot_id:
            return False
        conn.execute(
            "DELETE FROM guest_commands WHERE guest_bot_id = ? AND name = ?",
            (guest_bot_id, name.strip().lower()),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("[GUEST RUNTIME] delete command error: %s", e)
        return False


def _rt_toggle_command(conn: sqlite3.Connection, bot_username: str, name: str) -> bool:
    try:
        guest_bot_id = _rt_get_guest_bot_id(conn, bot_username)
        if not guest_bot_id:
            return False
        row = conn.execute(
            "SELECT enabled FROM guest_commands WHERE guest_bot_id = ? AND name = ?",
            (guest_bot_id, name.strip().lower()),
        ).fetchone()
        if not row:
            return False
        new_enabled = 0 if int(row[0] or 0) else 1
        ts = int(time.time())
        conn.execute(
            "UPDATE guest_commands SET enabled = ?, updated_at = ? WHERE guest_bot_id = ? AND name = ?",
            (new_enabled, ts, guest_bot_id, name.strip().lower()),
        )
        conn.commit()
        return True
    except Exception as e:
        logger.warning("[GUEST RUNTIME] toggle command error: %s", e)
        return False


def _handle_pm_callback(cq: dict, sender_id: int, conn: sqlite3.Connection) -> None:
    """Handle inline keyboard callback from owner/dev in PM."""
    query_id = str(cq.get("id") or "")
    data = str(cq.get("data") or "")
    msg = cq.get("message") or {}
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    message_id = int(msg.get("message_id") or 0)

    def _go_main() -> None:
        cmds = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
        ai_access_mode = _get_ai_access_mode(conn, _BOT_USERNAME)
        _rt_edit(chat_id, message_id, _rt_build_main_text(cmds, ai_access_mode), _rt_build_main_kb(ai_access_mode))

    def _go_list(page: int = 0) -> None:
        cmds = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
        _rt_edit(chat_id, message_id, _rt_build_list_text(cmds, page), _rt_build_list_kb(cmds, page))

    # ---- main page ----
    if data == "gcmd:main":
        _go_main()
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:ai_all":
        ok = _set_ai_access_mode(conn, _BOT_USERNAME, _AI_ACCESS_ALL)
        _go_main()
        _rt_answer_cq(query_id, "Режим ИИ: для всех." if ok else "Не удалось изменить режим ИИ.", show_alert=not ok)
        return

    if data == "gcmd:ai_owner":
        ok = _set_ai_access_mode(conn, _BOT_USERNAME, _AI_ACCESS_OWNER)
        _go_main()
        _rt_answer_cq(query_id, "Режим ИИ: для владельца." if ok else "Не удалось изменить режим ИИ.", show_alert=not ok)
        return

    # ---- list page ----
    if data.startswith("gcmd:list:"):
        try:
            page = int(data.split(":", 2)[2])
        except Exception:
            page = 0
        _go_list(page)
        _rt_answer_cq(query_id)
        return

    # ---- add command ----
    if data == "gcmd:add":
        state = {"step": "await_name", "name": "", "text": "", "owner_only": False}
        _RT_PENDING[sender_id] = state
        _rt_replace_pending_ui(
            chat_id,
            state,
            f"{_E_ADD} <b>Создание команды</b>\n\n"
            f"Пришлите <b>имя</b> новой команды.\n"
            f"<i>До {_CMD_MAX_NAME_LEN_RT} символов. Можно использовать несколько слов.</i>",
            _rt_inline_kb([_rt_btn("Отмена", "gcmd:add_cancel")]),
            also_delete_msg_id=message_id,
        )
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:del_list":
        cmds = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
        if not cmds:
            _go_main()
            _rt_answer_cq(query_id)
            return
        state = {"step": "await_delete"}
        _RT_PENDING[sender_id] = state
        _rt_replace_pending_ui(
            chat_id,
            state,
            _rt_build_delete_prompt(cmds),
            _rt_inline_kb([_rt_btn("Отмена", "gcmd:del_cancel")]),
            also_delete_msg_id=message_id,
        )
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:del_cancel":
        state = _RT_PENDING.pop(sender_id, None)
        if isinstance(state, dict):
            state.pop("_ui_msg_id", None)
        _rt_answer_cq(query_id, "Отменено.")
        _go_main()
        return

    if data == "gcmd:add_cancel":
        state = _RT_PENDING.pop(sender_id, None)
        if isinstance(state, dict):
            state.pop("_ui_msg_id", None)
        _rt_answer_cq(query_id, "Отменено.")
        _go_main()
        return

    if data.startswith("gcmd:del:") or data.startswith("gcmd:tog:"):
        _rt_answer_cq(query_id, "Откройте обновлённый список команд.", show_alert=True)
        _go_main()
        return

    # ---- draft: request text ----
    if data == "gcmd:draft_text":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        draft["step"] = "await_text"
        _RT_PENDING[sender_id] = draft
        _rt_replace_pending_ui(
            chat_id,
            draft,
            f"{_E_TEXT} <b>Пришлите текст команды.</b>\n\n"
            "<blockquote expandable=\"true\">"
            "<b>Доступные переменные:</b>\n"
            "[GROUP_NAME] — название группы\n"
            "[USER_MENTION] — упоминание вызвавшего команду\n"
            "[USER_ID] — ID вызвавшего команду\n"
            "[USER_NAME] — имя вызвавшего команду\n"
            "[REPLY_MENTION] — упоминание того, на кого ответили\n"
            "[REPLY_ID] — ID того, на кого ответили\n"
            "[REPLY_NAME] — имя того, на кого ответили\n"
            "[NOLINK] — убрать предпросмотр ссылки\n"
            "[LINK] — включить предпросмотр ссылки"
            "</blockquote>\n\n"
            "<b>Поддерживается:</b>\n"
            "• обычное форматирование Telegram\n"
            "• и/или наш кастомный HTML\n\n"
            "<blockquote expandable=\"true\">"
            "<b>Кастомный HTML:</b>\n"
            "<code>&lt;b&gt;жирный&lt;/&gt;</code>\n"
            "<code>&lt;i&gt;курсив&lt;/&gt;</code>\n"
            "<code>&lt;u&gt;подчёркнутый&lt;/&gt;</code>\n"
            "<code>&lt;s&gt;зачёркнутый&lt;/&gt;</code>\n"
            "<code>&lt;code&gt;моноширинный&lt;/&gt;</code>\n"
            "<code>&lt;pre&gt;код&lt;/&gt;</code>\n"
            "<code>&lt;sp&gt;спойлер&lt;/&gt;</code>\n"
            "<code>&lt;quote&gt;цитата&lt;/&gt;</code>\n"
            "<code>&lt;quote exp&gt;свёрнутая цитата&lt;/&gt;</code>\n"
            "<code>&lt;emoji id='123'&gt;😀&lt;/&gt;</code>\n"
            "<code>&lt;a href='https://example.com'&gt;ссылка&lt;/&gt;</code>\n"
            "<code>&lt;br&gt;</code> — перенос строки"
            "</blockquote>\n\n"
            "<i>Важно:</i> если Telegram-выделение захватит символы &lt; или &gt;, "
            "то Telegram-форматирование может быть проигнорировано.",
            _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_cancel")]),
            also_delete_msg_id=message_id,
        )
        _rt_answer_cq(query_id)
        return

    # ---- draft: access toggle ----
    if data == "gcmd:draft_owner":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        draft["owner_only"] = True
        draft["step"] = "draft"
        _RT_PENDING[sender_id] = draft
        _rt_edit(chat_id, message_id, _rt_build_draft_text(draft), _rt_build_draft_kb(draft))
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:draft_all":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        draft["owner_only"] = False
        draft["step"] = "draft"
        _RT_PENDING[sender_id] = draft
        _rt_edit(chat_id, message_id, _rt_build_draft_text(draft), _rt_build_draft_kb(draft))
        _rt_answer_cq(query_id)
        return

    # ---- draft: cancel ----
    if data == "gcmd:draft_cancel":
        state = _RT_PENDING.pop(sender_id, None)
        if isinstance(state, dict):
            state.pop("_ui_msg_id", None)
        _rt_answer_cq(query_id, "Создание команды отменено.")
        _go_main()
        return

    # ---- draft: save ----
    if data == "gcmd:draft_save":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        name = (draft.get("name") or "").strip()
        text_val = (draft.get("text") or "").strip()
        owner_only = bool(draft.get("owner_only"))
        if not name:
            _rt_answer_cq(query_id, "Имя команды не задано.", show_alert=True)
            return
        if not text_val:
            _rt_answer_cq(query_id, "Текст команды не задан.", show_alert=True)
            return
        ok = _rt_upsert_command(conn, _BOT_USERNAME, name, text_val, owner_only)
        state = _RT_PENDING.pop(sender_id, None)
        if isinstance(state, dict):
            state.pop("_ui_msg_id", None)
        if ok:
            _rt_answer_cq(query_id, f"Команда «{name}» сохранена.")
        else:
            _rt_answer_cq(query_id, "Не удалось сохранить.", show_alert=True)
        _go_main()
        return

    _rt_answer_cq(query_id)


def _rt_entities_to_html(text: str, entities: list[dict]) -> str:
    """Convert a Telegram message text with formatting entities to HTML.

    Uses UTF-16 code-unit offsets as specified by the Telegram Bot API.
    Falls back to HTML-escaped plain text if conversion fails.
    """
    if not entities or not text:
        return _html.escape(text)

    try:
        encoded = text.encode("utf-16-le")
        unit_count = len(encoded) // 2

        def _utf16_slice(start_u: int, end_u: int) -> str:
            return encoded[start_u * 2:end_u * 2].decode("utf-16-le", errors="replace")

        valid: list[tuple[int, int, dict]] = []
        for e in entities:
            off = int(e.get("offset") or 0)
            ln = int(e.get("length") or 0)
            if ln <= 0:
                continue
            end = min(off + ln, unit_count)
            if off < 0 or off >= unit_count or end <= off:
                continue
            valid.append((off, end, e))

        if not valid:
            return _html.escape(text)

        bounds = sorted({0, unit_count} | {off for off, end, _ in valid} | {end for _, end, _ in valid})

        _ENTITY_PRIO = {
            "blockquote": 0, "expandable_blockquote": 0,
            "text_link": 1, "url": 1,
            "bold": 2, "italic": 3, "underline": 4,
            "strikethrough": 5, "spoiler": 6,
            "code": 7, "pre": 7,
        }

        def _wrap(inner: str, entity: dict) -> str:
            t = str(entity.get("type") or "")
            if t == "bold":
                return f"<b>{inner}</b>"
            if t == "italic":
                return f"<i>{inner}</i>"
            if t == "underline":
                return f"<u>{inner}</u>"
            if t == "strikethrough":
                return f"<s>{inner}</s>"
            if t == "spoiler":
                return f"<tg-spoiler>{inner}</tg-spoiler>"
            if t == "code":
                return f"<code>{inner}</code>"
            if t == "pre":
                lang = str(entity.get("language") or "")
                if lang:
                    return f'<pre><code class="language-{_html.escape(lang)}">{inner}</code></pre>'
                return f"<pre>{inner}</pre>"
            if t == "text_link":
                url = _html.escape(str(entity.get("url") or ""), quote=True)
                return f'<a href="{url}">{inner}</a>'
            if t == "text_mention":
                uid = int((entity.get("user") or {}).get("id") or 0)
                return f'<a href="tg://user?id={uid}">{inner}</a>'
            if t in ("blockquote", "expandable_blockquote"):
                if t == "expandable_blockquote":
                    return f'<blockquote expandable="true">{inner}</blockquote>'
                return f"<blockquote>{inner}</blockquote>"
            if t == "custom_emoji":
                eid = str(entity.get("custom_emoji_id") or "")
                return f'<tg-emoji emoji-id="{eid}">{inner}</tg-emoji>'
            return inner

        out: list[str] = []
        for i in range(len(bounds) - 1):
            seg_s = bounds[i]
            seg_e = bounds[i + 1]
            raw_seg = _utf16_slice(seg_s, seg_e)
            esc_seg = _html.escape(raw_seg)
            active = [e for off, end, e in valid if off <= seg_s and end >= seg_e]
            if not active:
                out.append(esc_seg)
                continue
            inner = esc_seg
            for e in sorted(active, key=lambda x: _ENTITY_PRIO.get(str(x.get("type") or ""), 50), reverse=True):
                inner = _wrap(inner, e)
            out.append(inner)

        return "".join(out)
    except Exception as ex:
        logger.warning("[GUEST RUNTIME] entities_to_html failed: %s", ex)
        return _html.escape(text)


def _handle_pm_message(msg: dict, sender_id: int, conn: sqlite3.Connection) -> bool:
    """Handle a private message from an allowed user. Returns True if handled."""
    raw_text = str(msg.get("text") or "")
    text = raw_text.strip()
    has_text_field = isinstance(msg.get("text"), str)
    chat_id = int((msg.get("chat") or {}).get("id") or 0)
    message_id = int(msg.get("message_id") or 0)

    if not chat_id:
        return False

    # /start or /help → show main commands page
    if text in ("/start", "/help", f"/start@{_BOT_USERNAME}", f"/help@{_BOT_USERNAME}"):
        state = _RT_PENDING.pop(sender_id, None)
        _rt_clear_pending_ui(chat_id, state, also_delete_msg_id=message_id)
        _rt_show_main(chat_id, conn)
        return True

    # Cancel
    if text.lower() in {"отмена", "cancel", "/cancel"}:
        if sender_id in _RT_PENDING:
            state = _RT_PENDING.pop(sender_id, None)
            _rt_clear_pending_ui(chat_id, state, also_delete_msg_id=message_id)
            _rt_send(chat_id, f"{_E_OK} Операция отменена.")
            return True

    # Draft state machine
    state = _RT_PENDING.get(sender_id)
    if not state:
        return False

    step = state.get("step")

    if step == "await_name":
        if not has_text_field:
            _rt_replace_pending_ui(
                chat_id,
                state,
                "Пришлите имя команды текстом.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:add_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        if not text:
            _rt_replace_pending_ui(
                chat_id,
                state,
                "Имя команды не может быть пустым.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:add_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        if len(text) > _CMD_MAX_NAME_LEN_RT:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"Имя команды не должно превышать {_CMD_MAX_NAME_LEN_RT} символов.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:add_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        cmds_existing = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
        if text.lower() in {str(cmd.get("name") or "").lower() for cmd in cmds_existing}:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"Команда <code>{_html.escape(text)}</code> уже существует.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:add_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        state["name"] = text.strip().lower()
        state["step"] = "draft"
        _RT_PENDING[sender_id] = state
        _rt_clear_pending_ui(chat_id, state, also_delete_msg_id=message_id)
        _rt_send(chat_id, _rt_build_draft_text(state), _rt_build_draft_kb(state))
        return True

    if step == "await_delete":
        if not has_text_field:
            _rt_replace_pending_ui(
                chat_id,
                state,
                "Пришлите имя команды текстом.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:del_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        raw_del = text
        cmds_del = _rt_list_commands_for_bot(conn, _BOT_USERNAME)
        cmd_by_key = {str(cmd.get("name") or "").lower(): cmd for cmd in cmds_del}
        cmd_key_del = raw_del.lower()
        if cmd_key_del not in cmd_by_key:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"Команда <code>{_html.escape(raw_del)}</code> не найдена. Введите точное имя.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:del_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        cmd_name_display = str(cmd_by_key[cmd_key_del].get("name") or raw_del)
        ok = _rt_delete_command(conn, _BOT_USERNAME, raw_del)
        old_state = _RT_PENDING.pop(sender_id, None)
        _rt_clear_pending_ui(chat_id, old_state, also_delete_msg_id=message_id)
        if ok:
            _rt_send(
                chat_id,
                f"{_E_OK} <b>Команда <code>{_html.escape(cmd_name_display)}</code> удалена.</b>",
                _rt_build_main_kb(_get_ai_access_mode(conn, _BOT_USERNAME)),
            )
        else:
            _rt_replace_pending_ui(
                chat_id,
                state,
                "Не удалось удалить команду.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:del_cancel")]),
                also_delete_msg_id=message_id,
            )
        return True

    if step == "await_text":
        if not has_text_field:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>Это не текст.</b>\nПришлите текстовое сообщение.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        if not text:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>Это не текст.</b>\nПришлите текстовое сообщение.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        if len(text) > _MAX_RESPONSE_LEN_RT:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>Текст слишком длинный</b>\n\nМаксимум {_MAX_RESPONSE_LEN_RT} символов.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        # Convert Telegram entities to HTML if present; otherwise keep as-is (allows literal HTML input)
        entities = msg.get("entities") or []
        if entities:
            stored_text = _rt_entities_to_html(raw_text, entities).strip()
        else:
            stored_text = text
        state["text"] = stored_text
        state["step"] = "draft"
        _RT_PENDING[sender_id] = state
        _rt_clear_pending_ui(chat_id, state, also_delete_msg_id=message_id)
        _rt_send(chat_id, _rt_build_draft_text(state), _rt_build_draft_kb(state))
        return True

    return False


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
    def _candidate_id(candidate: object, *, allow_fallback_id: bool = False) -> str:
        if not isinstance(candidate, dict):
            return ""
        value = candidate.get("guest_query_id")
        if value is None and allow_fallback_id:
            value = candidate.get("id")
        return str(value).strip() if value is not None else ""

    direct_id = _candidate_id(payload_obj)
    if direct_id:
        return direct_id

    for source in (payload_obj, update_obj):
        if not isinstance(source, dict):
            continue
        nested_guest_query = source.get("guest_query")
        nested_id = _candidate_id(nested_guest_query, allow_fallback_id=True)
        if nested_id:
            return nested_id

        for nested_key in ("guest_message", "message"):
            nested_payload = source.get(nested_key)
            nested_id = _candidate_id(nested_payload)
            if nested_id:
                return nested_id
            if isinstance(nested_payload, dict):
                nested_guest_query = nested_payload.get("guest_query")
                nested_id = _candidate_id(nested_guest_query, allow_fallback_id=True)
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


def _count_words(text: str) -> int:
    return sum(1 for part in re.split(r"\s+", (text or "").strip()) if part)


def _should_use_ai_fallback(text: str, min_words: int) -> bool:
    return _count_words(text) >= max(1, int(min_words or 1))


def _build_inline_article_result(text: str, parse_mode: str | None = None) -> str:
    """Return a JSON-serialised InlineQueryResultArticle for answerGuestQuery.

    answerGuestQuery requires *result* to be an InlineQueryResult object
    (not a plain text field).  We use the article type so the text is sent
    as a chat message via InputTextMessageContent.
    """
    input_content: dict = {"message_text": text}
    if parse_mode:
        input_content["parse_mode"] = parse_mode
    result_obj = {
        "type": "article",
        "id": f"{abs(hash(text)) % 0xFFFFFF:06x}",
        "title": (text[:_INLINE_ARTICLE_TITLE_MAX_LEN].strip() or "Response"),
        "input_message_content": input_content,
    }
    return json.dumps(result_obj, ensure_ascii=False)


def _answer_guest_query(guest_query_id: str, response_text: str) -> bool:
    if not guest_query_id or not response_text:
        return False

    clean_response_text = _prepare_guest_query_text(response_text)
    if not clean_response_text:
        logger.warning("[GUEST RUNTIME] answerGuestQuery skipped: empty text after cleanup")
        return False

    # answerGuestQuery requires `result` to be an InlineQueryResult object.
    # We try HTML formatting first, then fall back to plain (stripped) text.
    attempts: list[tuple[str, str | None]] = [
        (str(response_text or "").strip()[:_GUEST_QUERY_TEXT_MAX_LEN], "HTML"),
        (clean_response_text, None),
    ]
    last_error = ""
    for text, parse_mode in attempts:
        if not text:
            continue
        payload = {
            "guest_query_id": guest_query_id,
            "result": _build_inline_article_result(text, parse_mode),
        }
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
    # Handle callback_query from allowed users in PM (management interface)
    cq = update_obj.get("callback_query")
    if isinstance(cq, dict):
        sender = cq.get("from") or {}
        sender_id = int(sender.get("id") or 0)
        if sender_id:
            conn = None
            try:
                conn = _db_connect()
                if _is_allowed_pm_user(sender_id, conn):
                    _handle_pm_callback(cq, sender_id, conn)
                    return
            except Exception as e:
                logger.warning("[GUEST RUNTIME] PM callback error: %s", e)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
        return

    # Check if it's a private message from an allowed user — handle as management
    msg = update_obj.get("message")
    if isinstance(msg, dict):
        chat = msg.get("chat") or {}
        if chat.get("type") == "private":
            sender = msg.get("from") or {}
            sender_id = int(sender.get("id") or 0)
            if sender_id:
                conn = None
                try:
                    conn = _db_connect()
                    if _is_allowed_pm_user(sender_id, conn):
                        if _handle_pm_message(msg, sender_id, conn):
                            return
                except Exception as e:
                    logger.warning("[GUEST RUNTIME] PM message error: %s", e)
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:
                            pass

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

    # Extract sender ID for owner_only check
    sender_id = 0
    try:
        from_user = guest_payload.get("from") or {}
        sender_id = int(from_user.get("id") or 0)
    except Exception:
        pass

    text = _extract_message_text(guest_payload, update_obj)
    if not text:
        return

    cmd_key = _extract_guest_command_key(text, _BOT_USERNAME)

    conn = None
    try:
        conn = _db_connect()
        if not _bot_is_enabled(conn, _BOT_USERNAME):
            if cmd_key and not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "bot disabled", text):
                logger.warning("[GUEST RUNTIME] failed to notify owner about disabled bot for cmd=%s", cmd_key)
            return
        response = None
        if cmd_key:
            response = _resolve_guest_response(conn, _BOT_USERNAME, cmd_key, sender_id)
        sent = False
        if response:
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
            if not sent and cmd_key:
                if not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "failed to deliver response", text):
                    logger.warning("[GUEST RUNTIME] failed to notify owner about delivery failure cmd=%s", cmd_key)
            return
        owner_user_id = _get_owner_user_id(conn, _BOT_USERNAME)
        ai_access_mode = _get_ai_access_mode(conn, _BOT_USERNAME)
        if not _is_sender_allowed_for_ai(sender_id, owner_user_id, ai_access_mode):
            return
        min_words = (
            _AI_MIN_WORD_COUNT_OWNER_DEV
            if _is_owner_or_dev_sender(sender_id, owner_user_id)
            else _AI_MIN_WORD_COUNT_DEFAULT
        )
        if not _should_use_ai_fallback(text, min_words):
            return
        owner_intent = _detect_owner_intent(text) if owner_user_id and sender_id == owner_user_id else None
        ai_response = _AI_SERVICE.generate_reply(
            text,
            is_owner_sender=bool(owner_user_id and sender_id == owner_user_id),
            owner_intent=owner_intent,
        )
        ai_text = ai_response or _AI_FALLBACK_TEXT
        if guest_query_id:
            sent = _answer_guest_query(guest_query_id, ai_text)
        if not sent and chat_id:
            sent = _send_message_response(
                chat_id=chat_id,
                response_text=ai_text,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
            )
        if sent:
            return
        if cmd_key:
            if not _send_owner_problem_report(conn, _BOT_USERNAME, cmd_key, "command not found or module disabled", text):
                logger.warning("[GUEST RUNTIME] failed to notify owner about unresolved cmd=%s", cmd_key)
            return
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
