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
_MAX_NESTED_EXTRACTION_DEPTH = 4
_MAX_BUTTON_ROWS = 10
_MAX_BUTTONS_PER_ROW = 3
_MAX_TOTAL_BUTTONS = 30
_GUEST_BUTTON_CACHE_LIMIT = 512
_EMPTY_BUTTONS_JSON = '{"rows":[],"popups":[]}'
_GUEST_BUTTON_CACHE: dict[tuple[int, int], dict] = {}
_GUEST_BUTTON_CACHE_LOCK = threading.Lock()


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
    try:
        conn.execute("ALTER TABLE guest_commands ADD COLUMN media_items TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    except Exception:
        pass
    try:
        conn.execute("""ALTER TABLE guest_commands ADD COLUMN buttons_json TEXT NOT NULL DEFAULT '{"rows":[],"popups":[]}'""")
        conn.commit()
    except Exception:
        pass
    return conn


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_key(value: str) -> str:
    return (value or "").strip().lower().strip(_CMD_STRIP_CHARS).strip()


def _safe_json_loads(raw: str, fallback):
    try:
        value = json.loads(raw or "")
    except Exception:
        return fallback
    return value


def _utf16_units(text: str) -> list[int]:
    raw = (text or "").encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _utf16_len(text: str) -> int:
    return len((text or "").encode("utf-16-le")) // 2


def _remove_utf16_range(text: str, start_u: int, len_u: int) -> str:
    units = _utf16_units(text)
    start = max(int(start_u or 0), 0)
    end = max(start + int(len_u or 0), 0)
    if start >= len(units) or end <= start:
        return text
    end = min(end, len(units))
    new_units = units[:start] + units[end:]
    return b"".join(u.to_bytes(2, "little") for u in new_units).decode("utf-16-le")


def _normalize_url(raw: str) -> str:
    url = str(raw or "").strip()
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", url):
        url = "https://" + url
    return url


def _is_supported_button_url(url: str) -> bool:
    value = str(url or "").strip()
    if not value or re.search(r"\s", value):
        return False
    if re.match(r"^tg://", value, flags=re.I):
        return True
    if not re.match(r"^https?://", value, flags=re.I):
        return False
    host = re.sub(r"^https?://", "", value, flags=re.I).split("/", 1)[0].strip()
    return bool(host) and ("." in host or host.lower() == "localhost")


class ButtonSyntaxError(ValueError):
    def __init__(self, line_no: int, problem: str, details: str = ""):
        self.line_no = int(line_no or 0)
        self.problem = str(problem or "other")
        self.details = str(details or "").strip()
        super().__init__(self.details or self.problem)


def _format_button_syntax_error(err: ButtonSyntaxError) -> str:
    line_no = int(getattr(err, "line_no", 0) or 0)
    problem = str(getattr(err, "problem", "other") or "other")
    details = str(getattr(err, "details", "") or "").strip()
    base = {
        "format": "Неправильный формат",
        "url": "Неправильная ссылка",
    }.get(problem, "Другая проблема")
    prefix = f"Строка {line_no}: " if line_no > 0 else "Ошибка: "
    return f"{prefix}{base}. {details}".strip()


def _button_syntax_error(line_no: int, problem: str, details: str = "") -> ButtonSyntaxError:
    return ButtonSyntaxError(line_no=line_no, problem=problem, details=details)


def _extract_button_icon_custom_emoji_id(label: str) -> tuple[str, str | None]:
    value = str(label or "").strip()
    match = re.match(r"^\s*<emoji\s+id=['\"](\d+)['\"]>\s*.*?\s*</>\s*", value, flags=re.I | re.S)
    if not match:
        return value, None
    icon_id = match.group(1)
    rest = re.sub(r"^\s*<emoji\s+id=['\"]\d+['\"]>\s*.*?\s*</>\s*", "", value, flags=re.I | re.S).strip()
    return (rest if rest else " "), icon_id


def _find_custom_emoji_entity_at_offset(entities: list[dict], offset_u: int) -> tuple[int, str] | None:
    for entity in entities or []:
        if not isinstance(entity, dict):
            continue
        entity_type = str(entity.get("type") or "").lower()
        if entity_type != "custom_emoji":
            continue
        try:
            offset = int(entity.get("offset") or 0)
            length = int(entity.get("length") or 0)
        except Exception:
            continue
        if offset != offset_u or length <= 0:
            continue
        custom_emoji_id = str(entity.get("custom_emoji_id") or "").strip()
        if custom_emoji_id:
            return length, custom_emoji_id
    return None


def _parse_buttons_text(user_text: str, entities: list[dict] | None = None) -> tuple[list[list[dict]], list[str]]:
    original = str(user_text or "")
    if not original.strip():
        return [], []
    if len(original) > 6000:
        raise _button_syntax_error(0, "other", "Слишком длинный текст кнопок.")

    has_custom_emoji_entities = any(
        isinstance(entity, dict) and str(entity.get("type") or "").lower() == "custom_emoji"
        for entity in (entities or [])
    )
    original_u = "".join(chr(unit) for unit in _utf16_units(original)) if has_custom_emoji_entities else ""
    rows: list[list[dict]] = []
    popups: list[str] = []
    search_pos_u = 0

    def _parse_button_token(token: str, token_start_u: int, line_no: int) -> dict:
        tok = token.strip()
        style = None
        prefix_units = 0
        match_color = re.match(r"^(#r|#g|#b)(\s+)(.*)$", tok, flags=re.I | re.S)
        if match_color:
            color = str(match_color.group(1) or "").lower()
            prefix_units = _utf16_len((match_color.group(1) or "") + (match_color.group(2) or " "))
            tok = str(match_color.group(3) or "").strip()
            style = {"#r": "danger", "#g": "success", "#b": "primary"}.get(color)

        if " - " not in tok:
            raise _button_syntax_error(
                line_no,
                "format",
                "Используйте формат «Название - ссылка», «Название - popup: текст», «Название - rules», «Название - del» или «Название - cmd: имя_команды».",
            )

        name_raw, value = tok.split(" - ", 1)
        name_start_u = 0
        name_end_u = 0
        if has_custom_emoji_entities:
            name_raw_start_u = token_start_u + prefix_units
            name_raw_end_u = name_raw_start_u + _utf16_len(name_raw)
            name_lead = name_raw[: len(name_raw) - len(name_raw.lstrip())]
            name_trail = name_raw[len(name_raw.rstrip()):]
            name_start_u = name_raw_start_u + _utf16_len(name_lead)
            name_end_u = name_raw_end_u - _utf16_len(name_trail)

        name = name_raw.strip()
        value = str(value or "").strip()
        name, icon_eid = _extract_button_icon_custom_emoji_id(name)
        if not icon_eid and has_custom_emoji_entities and entities:
            found = _find_custom_emoji_entity_at_offset(entities, name_start_u)
            if found and name_end_u > name_start_u:
                icon_len_u, icon_eid = found
                stripped_name = _remove_utf16_range(name, 0, icon_len_u).strip()
                name = stripped_name if stripped_name else " "

        if not name.strip() and not icon_eid:
            raise _button_syntax_error(line_no, "format", "У кнопки отсутствует название.")
        if not name.strip():
            name = " "
        if not value:
            raise _button_syntax_error(line_no, "format", "После « - » нужно указать ссылку, popup, rules, del или cmd: имя_команды.")

        lowered = value.lower()
        if lowered == "rules":
            return {"type": "rules", "text": name, "style": style, "icon_emoji_id": icon_eid}
        if lowered == "del":
            return {"type": "del", "text": name, "style": style, "icon_emoji_id": icon_eid}
        if lowered.startswith("cmd:"):
            cmd_name = value[len("cmd:"):].strip()
            if not cmd_name:
                raise _button_syntax_error(line_no, "format", "После «cmd:» укажите имя команды.")
            return {"type": "cmd", "text": name, "style": style, "cmd_name": cmd_name, "icon_emoji_id": icon_eid}
        if lowered.startswith("popup:"):
            popup_text = value[len("popup:"):].strip()
            if not popup_text:
                raise _button_syntax_error(line_no, "format", "Для popup укажите текст после «popup:».")
            popups.append(popup_text)
            return {
                "type": "popup",
                "text": name,
                "style": style,
                "popup_index": len(popups) - 1,
                "icon_emoji_id": icon_eid,
            }

        url = _normalize_url(value)
        if not _is_supported_button_url(url):
            raise _button_syntax_error(line_no, "url", "Поддерживаются http(s) и tg:// ссылки без пробелов.")
        return {"type": "url", "text": name, "style": style, "url": url, "icon_emoji_id": icon_eid}

    for line_no, raw_line in enumerate([ln.strip() for ln in original.splitlines() if ln.strip()], start=1):
        line_start_u = None
        if has_custom_emoji_entities:
            raw_line_u = "".join(chr(unit) for unit in _utf16_units(raw_line))
            line_start_u = original_u.find(raw_line_u, search_pos_u)
            if line_start_u != -1:
                search_pos_u = line_start_u + len(raw_line_u)
            else:
                line_start_u = None
        parts = [part.strip() for part in raw_line.split("&") if part.strip()]
        if not parts:
            continue
        if len(parts) > _MAX_BUTTONS_PER_ROW:
            raise _button_syntax_error(line_no, "format", f"В одном ряду можно использовать не больше {_MAX_BUTTONS_PER_ROW} кнопок.")
        row: list[dict] = []
        line_seek_u = line_start_u
        for part in parts:
            token_start_u = 0
            if has_custom_emoji_entities and line_seek_u is not None:
                token_units = "".join(chr(unit) for unit in _utf16_units(part))
                token_start_u = original_u.find(token_units, line_seek_u)
                if token_start_u == -1:
                    token_start_u = line_seek_u
                else:
                    line_seek_u = token_start_u + len(token_units)
            row.append(_parse_button_token(part, token_start_u, line_no))
        rows.append(row)
        if len(rows) > _MAX_BUTTON_ROWS:
            raise _button_syntax_error(line_no, "other", f"Допустимо не больше {_MAX_BUTTON_ROWS} рядов кнопок.")
    if sum(len(row) for row in rows) > _MAX_TOTAL_BUTTONS:
        raise _button_syntax_error(0, "other", f"Допустимо не больше {_MAX_TOTAL_BUTTONS} кнопок в одном наборе.")
    return rows, popups


def _normalize_guest_buttons_payload(raw_buttons: object) -> dict:
    default = {"rows": [], "popups": []}
    buttons = raw_buttons
    if isinstance(raw_buttons, str):
        buttons = _safe_json_loads(raw_buttons, default)
    if not isinstance(buttons, dict):
        return default
    rows = buttons.get("rows")
    popups = buttons.get("popups")
    if not isinstance(rows, list):
        rows = []
    if not isinstance(popups, list):
        popups = []
    return {"rows": rows, "popups": [str(item or "") for item in popups]}


def _load_guest_media_payload(raw_media: object) -> list[dict]:
    items = raw_media
    if isinstance(raw_media, str):
        items = _safe_json_loads(raw_media, [])
    if not isinstance(items, list):
        return []
    normalized: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        media_type = str(item.get("type") or "").strip().lower()
        file_id = str(item.get("file_id") or "").strip()
        if media_type not in {"photo", "video", "document", "audio", "animation"} or not file_id:
            continue
        normalized.append({"type": media_type, "file_id": file_id})
    return normalized


def _extract_media_payload(msg: dict) -> dict | None:
    if not isinstance(msg, dict):
        return None
    photos = msg.get("photo") or []
    if isinstance(photos, list) and photos:
        photo = photos[-1] if isinstance(photos[-1], dict) else {}
        file_id = str(photo.get("file_id") or "").strip()
        if file_id:
            return {"type": "photo", "file_id": file_id}
    for media_type, key in (
        ("video", "video"),
        ("document", "document"),
        ("audio", "audio"),
        ("animation", "animation"),
    ):
        obj = msg.get(key) or {}
        if isinstance(obj, dict):
            file_id = str(obj.get("file_id") or "").strip()
            if file_id:
                return {"type": media_type, "file_id": file_id}
    return None


def _media_can_album(items: list[dict]) -> bool:
    return len(items) >= 2 and all(str(item.get("type") or "") in {"photo", "video"} for item in items)


def _sanitize_button_for_payload(button: object, popups: list[str]) -> dict | None:
    if not isinstance(button, dict):
        return None
    button_type = str(button.get("type") or "").strip().lower()
    if button_type not in {"url", "popup", "rules", "del", "cmd"}:
        return None
    text = str(button.get("text") or "").strip()
    icon_emoji_id = str(button.get("icon_emoji_id") or "").strip() or None
    if not text and not icon_emoji_id:
        return None
    if not text:
        text = " "
    style = button.get("style")
    if style not in {None, "danger", "success", "primary"}:
        style = None
    normalized = {
        "type": button_type,
        "text": text,
        "style": style,
        "icon_emoji_id": icon_emoji_id,
    }
    if button_type == "url":
        url = _normalize_url(str(button.get("url") or ""))
        if not _is_supported_button_url(url):
            return None
        normalized["url"] = url
        return normalized
    if button_type == "popup":
        try:
            popup_index = int(button.get("popup_index"))
        except Exception:
            return None
        if popup_index < 0 or popup_index >= len(popups) or not str(popups[popup_index] or "").strip():
            return None
        normalized["popup_index"] = popup_index
        return normalized
    if button_type == "cmd":
        cmd_name = str(button.get("cmd_name") or "").strip()
        if not cmd_name:
            return None
        normalized["cmd_name"] = cmd_name
        return normalized
    return normalized


def _build_guest_reply_markup(rows: list[list[dict]], popups: list[str], viewer_user_id: int) -> dict | None:
    if not rows:
        return None
    keyboard: list[list[dict]] = []
    for row in rows[:_MAX_BUTTON_ROWS]:
        out_row: list[dict] = []
        for button in (row or [])[:_MAX_BUTTONS_PER_ROW]:
            normalized = _sanitize_button_for_payload(button, popups)
            if not normalized:
                continue
            btn: dict = {"text": normalized.get("text") or " "}
            button_type = normalized["type"]
            if button_type == "url":
                btn["url"] = normalized.get("url") or ""
            elif button_type == "popup":
                btn["callback_data"] = f"gbtn:popup:{int(viewer_user_id)}:{int(normalized.get('popup_index') or 0)}"[:64]
            elif button_type == "del":
                btn["callback_data"] = f"gbtn:del:{int(viewer_user_id)}"[:64]
            elif button_type == "rules":
                btn["callback_data"] = f"gbtn:rules:{int(viewer_user_id)}"[:64]
            elif button_type == "cmd":
                btn["callback_data"] = f"gbtn:cmd:{int(viewer_user_id)}:{str(normalized.get('cmd_name') or '').strip()}"[:64]
            style = normalized.get("style")
            if style:
                btn["style"] = style
            icon_emoji_id = normalized.get("icon_emoji_id")
            if icon_emoji_id:
                btn["icon_custom_emoji_id"] = str(icon_emoji_id)
            out_row.append(btn)
        if out_row:
            keyboard.append(out_row)
    return {"inline_keyboard": keyboard} if keyboard else None


def _cache_guest_button_payload(chat_id: int, message_id: int | None, buttons_payload: dict, viewer_user_id: int) -> None:
    if not chat_id or not message_id:
        return
    rows = list((buttons_payload.get("rows") or [])) if isinstance(buttons_payload, dict) else []
    popups = list((buttons_payload.get("popups") or [])) if isinstance(buttons_payload, dict) else []
    cache_value = {
        "rows": rows,
        "popups": popups,
        "viewer_user_id": int(viewer_user_id or 0),
        "updated_at": time.time(),
    }
    with _GUEST_BUTTON_CACHE_LOCK:
        _GUEST_BUTTON_CACHE[(int(chat_id), int(message_id))] = cache_value
        while len(_GUEST_BUTTON_CACHE) > _GUEST_BUTTON_CACHE_LIMIT:
            try:
                oldest_key = next(iter(_GUEST_BUTTON_CACHE))
            except StopIteration:
                break
            _GUEST_BUTTON_CACHE.pop(oldest_key, None)


def _get_cached_guest_button_payload(chat_id: int, message_id: int | None) -> dict | None:
    if not chat_id or not message_id:
        return None
    with _GUEST_BUTTON_CACHE_LOCK:
        return _GUEST_BUTTON_CACHE.get((int(chat_id), int(message_id)))


def _drop_cached_guest_button_payload(chat_id: int, message_id: int | None) -> None:
    if not chat_id or not message_id:
        return
    with _GUEST_BUTTON_CACHE_LOCK:
        _GUEST_BUTTON_CACHE.pop((int(chat_id), int(message_id)), None)


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


def _resolve_guest_response(conn: sqlite3.Connection, bot_username: str, cmd_key: str, sender_id: int = 0) -> dict | None:
    row = conn.execute(
        """
        SELECT gc.response_text, gc.media_items, gc.buttons_json, gb.linked_modules_json, gc.owner_only, gb.owner_user_id
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
        modules = json.loads(row[3] or "[]")
    except Exception:
        modules = []
    if not isinstance(modules, list) or "commands" not in [str(m).lower() for m in modules]:
        return None
    owner_only = bool(int(row[4] or 0))
    owner_user_id = int(row[5] or 0)
    if owner_only and sender_id and sender_id != owner_user_id and not _is_dev_user(sender_id):
        return None
    text = str(row[0] or "").strip()
    media = _load_guest_media_payload(row[1] or "[]")
    buttons = _normalize_guest_buttons_payload(row[2] or _EMPTY_BUTTONS_JSON)
    if not text and not media:
        return None
    return {
        "text": text,
        "media": media,
        "buttons": buttons,
        "owner_only": owner_only,
    }


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


def _extract_api_message_id(result: dict | None) -> int | None:
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    payload = result.get("result")
    if isinstance(payload, dict):
        try:
            return int(payload.get("message_id") or 0) or None
        except Exception:
            return None
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            try:
                return int(first.get("message_id") or 0) or None
            except Exception:
                return None
    return None


def _send_message_response_ex(
    chat_id: int,
    response_text: str,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
    *,
    reply_markup: dict | None = None,
) -> tuple[bool, int | None]:
    if not chat_id or not response_text:
        return False, None
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
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    result = _api_request("sendMessage", params=payload, timeout=(10.0, 30.0))
    if isinstance(result, dict) and result.get("ok"):
        return True, _extract_api_message_id(result)
    description = str(result.get("description") or "") if isinstance(result, dict) else ""
    _html_error = (
        "ENTITY_TEXT_INVALID" in description
        or "can't parse entities" in description.lower()
        or "wrong html" in description.lower()
    )
    if _html_error:
        payload.pop("parse_mode", None)
        payload["text"] = _prepare_guest_query_text(response_text)
        fallback_result = _api_request("sendMessage", params=payload, timeout=(10.0, 30.0))
        if isinstance(fallback_result, dict) and fallback_result.get("ok"):
            return True, _extract_api_message_id(fallback_result)
    if description:
        logger.warning("[GUEST RUNTIME] sendMessage failed: %s", description)
    return False, None


def _send_message_response(
    chat_id: int,
    response_text: str,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> bool:
    ok, _ = _send_message_response_ex(
        chat_id,
        response_text,
        reply_to_message_id=reply_to_message_id,
        message_thread_id=message_thread_id,
    )
    return ok


def _build_media_group_item(media_item: dict, caption: str | None = None) -> dict | None:
    media_type = str(media_item.get("type") or "").strip().lower()
    file_id = str(media_item.get("file_id") or "").strip()
    if media_type not in {"photo", "video"} or not file_id:
        return None
    item: dict = {"type": media_type, "media": file_id}
    if caption:
        item["caption"] = caption
        item["parse_mode"] = "HTML"
    return item


def _send_payload_response(
    chat_id: int,
    response_text: str,
    media: list[dict] | None,
    buttons_payload: dict | None,
    viewer_user_id: int,
    *,
    reply_to_message_id: int | None = None,
    message_thread_id: int | None = None,
) -> bool:
    if not chat_id:
        return False
    html_text = str(response_text or "").strip()
    media_items = _load_guest_media_payload(media or [])
    normalized_buttons = _normalize_guest_buttons_payload(buttons_payload or {})
    reply_markup = _build_guest_reply_markup(
        normalized_buttons.get("rows") or [],
        normalized_buttons.get("popups") or [],
        viewer_user_id,
    )

    if not media_items:
        if not html_text:
            return False
        ok, message_id = _send_message_response_ex(
            chat_id,
            html_text,
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            reply_markup=reply_markup,
        )
        if ok and reply_markup and message_id:
            _cache_guest_button_payload(chat_id, message_id, normalized_buttons, viewer_user_id)
        return ok

    if _media_can_album(media_items):
        group_items: list[dict] = []
        for index, item in enumerate(media_items):
            payload_item = _build_media_group_item(item, html_text if index == 0 and html_text else None)
            if payload_item:
                group_items.append(payload_item)
        if not group_items:
            return False
        payload: dict = {
            "chat_id": int(chat_id),
            "media": json.dumps(group_items, ensure_ascii=False),
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        if message_thread_id:
            payload["message_thread_id"] = int(message_thread_id)
        result = _api_request("sendMediaGroup", params=payload, timeout=(10.0, 60.0))
        if not (isinstance(result, dict) and result.get("ok")):
            description = str(result.get("description") or "") if isinstance(result, dict) else ""
            if description:
                logger.warning("[GUEST RUNTIME] sendMediaGroup failed: %s", description)
            return False
        if reply_markup:
            ok, message_id = _send_message_response_ex(
                chat_id,
                "\u2063",
                message_thread_id=message_thread_id,
                reply_markup=reply_markup,
            )
            if ok and message_id:
                _cache_guest_button_payload(chat_id, message_id, normalized_buttons, viewer_user_id)
            return ok
        return True

    first_message_id: int | None = None
    first_message_sent = False
    for item in media_items:
        media_type = str(item.get("type") or "").strip().lower()
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            continue
        method = {
            "photo": "sendPhoto",
            "video": "sendVideo",
            "document": "sendDocument",
            "audio": "sendAudio",
            "animation": "sendAnimation",
        }.get(media_type)
        if not method:
            continue
        payload: dict = {
            "chat_id": int(chat_id),
            media_type: file_id,
        }
        if not first_message_sent and html_text:
            payload["caption"] = html_text
            payload["parse_mode"] = "HTML"
        if not first_message_sent and reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        if not first_message_sent and reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        if message_thread_id:
            payload["message_thread_id"] = int(message_thread_id)
        result = _api_request(method, params=payload, timeout=(10.0, 60.0))
        if isinstance(result, dict) and result.get("ok"):
            if not first_message_sent:
                first_message_sent = True
                first_message_id = _extract_api_message_id(result)
            continue
        description = str(result.get("description") or "") if isinstance(result, dict) else ""
        caption_html_error = (
            not first_message_sent
            and bool(payload.get("caption"))
            and (
                "ENTITY_TEXT_INVALID" in description
                or "can't parse entities" in description.lower()
                or "wrong html" in description.lower()
            )
        )
        if caption_html_error:
            payload.pop("parse_mode", None)
            payload["caption"] = _prepare_guest_query_text(str(payload.get("caption") or ""))
            result = _api_request(method, params=payload, timeout=(10.0, 60.0))
            if isinstance(result, dict) and result.get("ok"):
                if not first_message_sent:
                    first_message_sent = True
                    first_message_id = _extract_api_message_id(result)
                continue
            description = str(result.get("description") or "") if isinstance(result, dict) else description
        if description:
            logger.warning("[GUEST RUNTIME] %s failed: %s", method, description)
        return False
    if first_message_sent and reply_markup and first_message_id:
        _cache_guest_button_payload(chat_id, first_message_id, normalized_buttons, viewer_user_id)
    return first_message_sent


def _get_owner_user_id(conn: sqlite3.Connection, bot_username: str) -> int:
    uname = _normalize_bot_username(bot_username)
    if not uname:
        return 0
    row = conn.execute(
        """
        SELECT owner_user_id
        FROM guest_bots
        WHERE lower(bot_username) = ?
        LIMIT 1
        """,
        (uname,),
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


def _normalize_bot_username(value: str) -> str:
    return str(value or "").strip().lstrip("@").lower()


def _get_ai_access_mode(conn: sqlite3.Connection, bot_username: str) -> str:
    uname = _normalize_bot_username(bot_username)
    if not uname:
        return _AI_ACCESS_OWNER
    row = conn.execute(
        """
        SELECT ai_access_mode
        FROM guest_bots
        WHERE lower(bot_username) = ?
        LIMIT 1
        """,
        (uname,),
    ).fetchone()
    raw = row[0] if row else _AI_ACCESS_ALL
    return _normalize_ai_access_mode(str(raw or ""))


def _set_ai_access_mode(conn: sqlite3.Connection, bot_username: str, mode: str) -> bool:
    ts = int(time.time())
    norm_mode = _normalize_ai_access_mode(mode)
    uname = _normalize_bot_username(bot_username)
    if not uname:
        return False
    try:
        cur = conn.execute(
            """
            UPDATE guest_bots
            SET ai_access_mode = ?, updated_at = ?
            WHERE lower(bot_username) = ?
            """,
            (norm_mode, ts, uname),
        )
        if int(cur.rowcount or 0) == 0:
            return False
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
    if isinstance(result, dict) and result.get("ok"):
        return True
    description = str(result.get("description") or "") if isinstance(result, dict) else ""
    if "message is not modified" in description.lower():
        return True
    if description:
        logger.warning("[GUEST RUNTIME] editMessageText failed: %s", description)
    return False


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
            SELECT id, name, response_text, media_items, buttons_json, enabled, owner_only
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
                    "media": _load_guest_media_payload(r[3] or "[]"),
                    "buttons": _normalize_guest_buttons_payload(r[4] or _EMPTY_BUTTONS_JSON),
                    "enabled": bool(int(r[5] or 0)),
                    "owner_only": bool(int(r[6] or 0)),
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
        f"Для вызова команды напишите /имя команды или @{_html.escape(_BOT_USERNAME)} имя команды. "
        "Команды поддерживают текст, медиа и кнопки.\n\n"
        f"<b>Количество команд:</b> <code>{count}</code>\n"
        f"<b>Режим ИИ:</b> <code>{_html.escape(ai_label)}</code>\n"
        f"<b>Порог ИИ:</b> <code>{_AI_MIN_WORD_COUNT_DEFAULT}+</code> слова (обычные пользователи), "
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
    has_media = "есть" if draft.get("media") else "нет"
    has_buttons = "есть" if (draft.get("buttons") or {}).get("rows") else "нет"
    owner_only = bool(draft.get("owner_only"))
    access_label = "Для владельца" if owner_only else "Все пользователи"
    name_str = f"<code>{name}</code>" if name else "<i>не задано</i>"
    return (
        f"{_E_ADD} <b>Новая команда</b>\n\n"
        f"<b>Имя:</b> {name_str}\n"
        f"<b>Текст:</b> <code>{has_text}</code>\n"
        f"<b>Медиа:</b> <code>{has_media}</code>\n"
        f"<b>Кнопки:</b> <code>{has_buttons}</code>\n"
        f"<b>Доступ:</b> {access_label}"
    )


def _rt_build_draft_kb(draft: dict) -> dict:
    owner_only = bool(draft.get("owner_only"))
    btn_text = _rt_btn("Текст", "gcmd:draft_text", icon_custom_emoji_id="5334882760735598374")
    btn_media = _rt_btn("Медиа", "gcmd:draft_media", icon_custom_emoji_id="5431449001532594346")
    btn_buttons = _rt_btn("Кнопки", "gcmd:draft_buttons", icon_custom_emoji_id="5395463497783983254")
    if owner_only:
        btn_owner = _rt_btn("»Для владельца«", "gcmd:draft_owner", style="primary")
        btn_all = _rt_btn("Все пользователи", "gcmd:draft_all")
    else:
        btn_owner = _rt_btn("Для владельца", "gcmd:draft_owner")
        btn_all = _rt_btn("»Все пользователи«", "gcmd:draft_all", style="primary")
    btn_discard = _rt_btn("Удалить", "gcmd:draft_cancel", style="danger")
    btn_save = _rt_btn("Сохранить", "gcmd:draft_save", style="success")
    return _rt_inline_kb(
        [btn_text, btn_media],
        [btn_buttons],
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


def _rt_upsert_command(
    conn: sqlite3.Connection,
    bot_username: str,
    name: str,
    text: str,
    owner_only: bool,
    media: list[dict] | None = None,
    buttons: dict | None = None,
) -> bool:
    try:
        guest_bot_id = _rt_get_guest_bot_id(conn, bot_username)
        if not guest_bot_id:
            return False
        ts = int(time.time())
        media_json = json.dumps(_load_guest_media_payload(media or []), ensure_ascii=False)
        buttons_json = json.dumps(_normalize_guest_buttons_payload(buttons or {}), ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO guest_commands(guest_bot_id, name, response_text, media_items, buttons_json, enabled, owner_only, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(guest_bot_id, name) DO UPDATE SET
                response_text = excluded.response_text,
                media_items = excluded.media_items,
                buttons_json = excluded.buttons_json,
                enabled = 1,
                owner_only = excluded.owner_only,
                updated_at = excluded.updated_at
            """,
            (guest_bot_id, name.strip().lower(), text, media_json, buttons_json, 1 if owner_only else 0, ts, ts),
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
        if not ok:
            logger.warning("[GUEST RUNTIME] failed to switch AI mode to all for @%s", _BOT_USERNAME)
            _rt_answer_cq(query_id, "Не удалось изменить режим ИИ.", show_alert=True)
            return
        _go_main()
        _rt_answer_cq(query_id, "Режим ИИ: для всех.")
        return

    if data == "gcmd:ai_owner":
        ok = _set_ai_access_mode(conn, _BOT_USERNAME, _AI_ACCESS_OWNER)
        if not ok:
            logger.warning("[GUEST RUNTIME] failed to switch AI mode to owner for @%s", _BOT_USERNAME)
            _rt_answer_cq(query_id, "Не удалось изменить режим ИИ.", show_alert=True)
            return
        _go_main()
        _rt_answer_cq(query_id, "Режим ИИ: для владельца.")
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
        state = {
            "step": "await_name",
            "name": "",
            "text": "",
            "media": [],
            "buttons": {"rows": [], "popups": []},
            "owner_only": False,
        }
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

    if data == "gcmd:draft_media":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        draft["step"] = "await_media"
        _RT_PENDING[sender_id] = draft
        _rt_replace_pending_ui(
            chat_id,
            draft,
            f"{_E_SETTINGS} <b>Пришлите медиа для команды.</b>\n\n"
            "<b>Поддерживается:</b>\n"
            "• Фото\n• Видео\n• Файл\n• Музыка\n• GIF\n\n"
            "<i>Подпись отдельно не задаётся.</i>\n"
            "Если у команды есть текст, он будет автоматически использоваться как подпись.",
            _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_media_cancel")]),
            also_delete_msg_id=message_id,
        )
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:draft_buttons":
        draft = _RT_PENDING.get(sender_id, {})
        if not draft:
            _rt_answer_cq(query_id, "Черновик не найден.", show_alert=True)
            return
        draft["step"] = "await_buttons"
        _RT_PENDING[sender_id] = draft
        _rt_replace_pending_ui(
            chat_id,
            draft,
            f"{_E_SETTINGS} <b>Пришлите кнопки для команды.</b>\n\n"
            "<b>Формат:</b>\n"
            "<code>Название - example.com</code>\n"
            "<code>Название - popup: текст</code>\n"
            "<code>Название - rules</code>\n"
            "<code>Название - del</code>\n"
            "<code>Название - cmd: имя_команды</code>\n\n"
            "<b>Несколько в одном ряду:</b>\n"
            "<code>Кнопка1 - example.com & Кнопка2 - example.com</code>\n\n"
            "<b>Цвет:</b>\n"
            "<code>#r Название - example.com</code> (красный)\n"
            "<code>#g Название - example.com</code> (зелёный)\n"
            "<code>#b Название - example.com</code> (цвет зависит от темы пользователя)\n\n"
            "<b>Лимиты:</b>\n"
            f"• 1–{_MAX_BUTTONS_PER_ROW} кнопки в ряду\n"
            f"• до {_MAX_BUTTON_ROWS} рядов\n"
            f"• до {_MAX_TOTAL_BUTTONS} кнопок всего\n"
            "• до 1 премиум-эмодзи в кнопке (только в начале названия)",
            _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_buttons_cancel")]),
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

    if data == "gcmd:draft_media_cancel":
        draft = _RT_PENDING.get(sender_id, {})
        if draft:
            draft["step"] = "draft"
            _RT_PENDING[sender_id] = draft
            _rt_edit(chat_id, message_id, _rt_build_draft_text(draft), _rt_build_draft_kb(draft))
        _rt_answer_cq(query_id)
        return

    if data == "gcmd:draft_buttons_cancel":
        draft = _RT_PENDING.get(sender_id, {})
        if draft:
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
        media_items = _load_guest_media_payload(draft.get("media") or [])
        buttons_payload = _normalize_guest_buttons_payload(draft.get("buttons") or {})
        owner_only = bool(draft.get("owner_only"))
        if not name:
            _rt_answer_cq(query_id, "Имя команды не задано.", show_alert=True)
            return
        if not text_val and not media_items:
            _rt_answer_cq(query_id, "Нельзя сохранить пустую команду. Добавьте текст или медиа.", show_alert=True)
            return
        ok = _rt_upsert_command(conn, _BOT_USERNAME, name, text_val, owner_only, media_items, buttons_payload)
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

    if step == "await_media":
        media_payload = _extract_media_payload(msg)
        if not media_payload:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>Этот тип медиа не поддерживается.</b>\nПришлите фото/видео/файл/музыку/gif.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_media_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        state["media"] = [media_payload]
        state["step"] = "draft"
        _RT_PENDING[sender_id] = state
        _rt_clear_pending_ui(chat_id, state, also_delete_msg_id=message_id)
        _rt_send(chat_id, _rt_build_draft_text(state), _rt_build_draft_kb(state))
        return True

    if step == "await_buttons":
        if not has_text_field:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>Это не текст.</b>\nПришлите кнопки текстом.",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_buttons_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        try:
            rows, popups = _parse_buttons_text(raw_text, msg.get("entities") or [])
        except ButtonSyntaxError as err:
            _rt_replace_pending_ui(
                chat_id,
                state,
                f"{_E_OFF} <b>{_html.escape(_format_button_syntax_error(err))}</b>",
                _rt_inline_kb([_rt_btn("Отмена", "gcmd:draft_buttons_cancel")]),
                also_delete_msg_id=message_id,
            )
            return True
        state["buttons"] = {"rows": rows, "popups": popups}
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


def _send_owner_ai_problem_report(
    conn: sqlite3.Connection,
    bot_username: str,
    reason: str,
    raw_text: str = "",
) -> bool:
    owner_user_id = _get_owner_user_id(conn, bot_username)
    if not owner_user_id:
        return False
    reason_safe = _html.escape((reason or "").strip()[:_OWNER_DEBUG_MAX_LEN])
    raw_safe = _html.escape((raw_text or "").strip()[:_OWNER_DEBUG_MAX_LEN])
    text = (
        f"⚠️ <b>@{_html.escape(bot_username)} не смог выполнить AI-запрос</b>\n"
        f"<b>Причина:</b> <code>{reason_safe or 'unknown'}</code>"
    )
    if raw_safe:
        text += f"\n<b>Запрос:</b> <code>{raw_safe}</code>"
    return _send_message_response(owner_user_id, text)


def _format_ai_failure_text(user_message: str) -> str:
    detail = _html.escape((user_message or "").strip() or "Неизвестная ошибка.")
    return f"⚠️ <b>ИИ сейчас недоступен.</b>\n{detail}"


def _handle_guest_button_callback(cq: dict, sender_id: int, conn: sqlite3.Connection) -> bool:
    data = str(cq.get("data") or "")
    if not data.startswith("gbtn:"):
        return False
    query_id = str(cq.get("id") or "")
    message = cq.get("message") or {}
    chat = message.get("chat") or {}
    try:
        chat_id = int(chat.get("id") or 0)
    except Exception:
        chat_id = 0
    try:
        message_id = int(message.get("message_id") or 0)
    except Exception:
        message_id = 0
    parts = data.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    try:
        viewer_user_id = int(parts[2]) if len(parts) > 2 else 0
    except Exception:
        viewer_user_id = 0
    if viewer_user_id and sender_id != viewer_user_id:
        _rt_answer_cq(query_id, "Недоступно.", show_alert=True)
        return True

    if action == "popup":
        cached = _get_cached_guest_button_payload(chat_id, message_id)
        if not cached:
            _rt_answer_cq(query_id, "Данные кнопки устарели. Отправьте команду заново.", show_alert=True)
            return True
        try:
            popup_index = int(parts[3]) if len(parts) > 3 else -1
        except Exception:
            popup_index = -1
        popups = cached.get("popups") or []
        if popup_index < 0 or popup_index >= len(popups):
            _rt_answer_cq(query_id, "Попап недоступен.", show_alert=True)
            return True
        _rt_answer_cq(query_id, str(popups[popup_index] or "").strip()[:200], show_alert=True)
        return True

    if action == "del":
        _drop_cached_guest_button_payload(chat_id, message_id)
        _rt_delete_message(chat_id, message_id)
        _rt_answer_cq(query_id)
        return True

    if action == "rules":
        _rt_answer_cq(query_id, "Кнопка rules недоступна для guest-команд.", show_alert=True)
        return True

    if action == "cmd":
        cmd_name = parts[3] if len(parts) > 3 else ""
        payload = _resolve_guest_response(conn, _BOT_USERNAME, cmd_name, sender_id)
        if not payload:
            _rt_answer_cq(query_id, "Команда недоступна.", show_alert=True)
            return True
        _drop_cached_guest_button_payload(chat_id, message_id)
        try:
            _rt_delete_message(chat_id, message_id)
        except Exception:
            pass
        sent = _send_payload_response(
            chat_id=chat_id,
            response_text=str(payload.get("text") or ""),
            media=payload.get("media") or [],
            buttons_payload=payload.get("buttons") or {},
            viewer_user_id=sender_id,
            message_thread_id=int(message.get("message_thread_id") or 0) or None,
        )
        if not sent:
            logger.warning("[GUEST RUNTIME] failed to send payload for guest button command=%s", cmd_name)
            _rt_answer_cq(query_id, "Не удалось выполнить команду.", show_alert=True)
            return True
        _rt_answer_cq(query_id)
        return True

    _rt_answer_cq(query_id)
    return True


def _extract_guest_query_id(payload_obj: dict, update_obj: dict | None = None) -> str:
    """Return guest query ID from legacy/new update layouts.

    Supports IDs in guest_message, guest_query, and nested guest_query payloads.
    """
    def _candidate_id(candidate: object, *, allow_fallback_id: bool = False) -> str:
        if not isinstance(candidate, dict):
            return ""
        for key in ("guest_query_id", "query_id", "inline_query_id"):
            value = candidate.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        if allow_fallback_id:
            value = candidate.get("id")
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        return ""

    direct_id = _candidate_id(payload_obj, allow_fallback_id=False)
    if direct_id:
        return direct_id

    for source in (payload_obj, update_obj):
        if not isinstance(source, dict):
            continue
        for nested_key in ("guest_query", "guest_message", "message", "inline_query"):
            nested_payload = source.get(nested_key)
            nested_id = _candidate_id(nested_payload, allow_fallback_id=True)
            if nested_id:
                return nested_id
            if isinstance(nested_payload, dict):
                nested_guest_query = nested_payload.get("guest_query")
                nested_id = _candidate_id(nested_guest_query, allow_fallback_id=True)
                if nested_id:
                    return nested_id
    return ""


def _extract_sender_id(payload_obj: dict, update_obj: dict | None = None, _depth: int = 0) -> int:
    """Extract sender user ID from guest update payloads.

    Supports legacy/new payload shapes and nested objects.
    Uses bounded recursion via _depth to avoid infinite loops.
    """
    if _depth > _MAX_NESTED_EXTRACTION_DEPTH:
        logger.debug("[GUEST RUNTIME] sender extraction depth exceeded")
        return 0
    if not isinstance(payload_obj, dict):
        return 0

    def _extract_user_id(candidate: object) -> int:
        if not isinstance(candidate, dict):
            return 0
        try:
            uid = int(candidate.get("id") or 0)
        except Exception:
            uid = 0
        return uid if uid > 0 else 0

    for key in ("from", "sender", "user", "sender_user"):
        uid = _extract_user_id(payload_obj.get(key))
        if uid:
            return uid

    for key in ("from_user_id", "sender_id", "user_id", "sender_user_id"):
        value = payload_obj.get(key)
        try:
            uid = int(value or 0)
        except Exception:
            uid = 0
        if uid > 0:
            return uid

    for nested_key in ("guest_query", "guest_message", "message", "inline_query"):
        nested_payload = payload_obj.get(nested_key)
        if isinstance(nested_payload, dict):
            uid = _extract_sender_id(nested_payload, None, _depth + 1)
            if uid:
                return uid

    if isinstance(update_obj, dict):
        for nested_key in (
            "guest_query",
            "guest_message",
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "inline_query",
        ):
            nested_payload = update_obj.get(nested_key)
            if isinstance(nested_payload, dict):
                uid = _extract_sender_id(nested_payload, None, _depth + 1)
                if uid:
                    return uid
    return 0


def _extract_message_text(
    payload_obj: dict,
    update_obj: dict | None = None,
    _depth: int = 0,
) -> str:
    """Extract command text from guest payloads with bounded recursive fallback.

    Handles both guest_message and guest_query shapes and nested message objects.
    """
    if _depth > _MAX_NESTED_EXTRACTION_DEPTH:
        logger.debug("[GUEST RUNTIME] message text extraction depth exceeded")
        return ""
    if not isinstance(payload_obj, dict):
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
    # Handle callback_query from command buttons and PM management interface.
    cq = update_obj.get("callback_query")
    if isinstance(cq, dict):
        sender = cq.get("from") or {}
        sender_id = int(sender.get("id") or 0)
        if sender_id:
            conn = None
            try:
                conn = _db_connect()
                if _handle_guest_button_callback(cq, sender_id, conn):
                    return
                if _is_allowed_pm_user(sender_id, conn):
                    _handle_pm_callback(cq, sender_id, conn)
                    return
            except Exception as e:
                logger.warning("[GUEST RUNTIME] callback handling error: %s", e)
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

    # Extract sender ID for owner_only/AI access checks
    sender_id = _extract_sender_id(guest_payload, update_obj)

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
            response_text = str(response.get("text") or "")
            response_media = response.get("media") or []
            response_buttons = response.get("buttons") or {}
            has_buttons = bool((response_buttons.get("rows") or [])) if isinstance(response_buttons, dict) else False
            if guest_query_id and not response_media and not has_buttons and response_text:
                sent = _answer_guest_query(guest_query_id, response_text)
            if not sent and chat_id:
                if guest_query_id and (response_media or has_buttons):
                    logger.info("[GUEST RUNTIME] guest_query response requires chat delivery because media/buttons are present")
                elif not guest_query_id:
                    logger.debug("[GUEST RUNTIME] no guest_query_id; using chat payload delivery")
                sent = _send_payload_response(
                    chat_id=chat_id,
                    response_text=response_text,
                    media=response_media,
                    buttons_payload=response_buttons,
                    viewer_user_id=sender_id,
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
        is_owner_sender = bool(owner_user_id and sender_id == owner_user_id)
        owner_intent = _detect_owner_intent(text) if is_owner_sender else None
        ai_result = _AI_SERVICE.generate_reply_result(
            text,
            is_owner_sender=is_owner_sender,
            owner_intent=owner_intent,
        )
        ai_text = ai_result.text if ai_result.ok else _format_ai_failure_text(ai_result.user_message or _AI_FALLBACK_TEXT)
        if not ai_result.ok:
            logger.warning(
                "[GUEST RUNTIME] AI request failed for @%s sender=%s code=%s details=%s query=%r",
                _BOT_USERNAME,
                sender_id,
                ai_result.error_code or "unknown",
                ai_result.debug_message or "-",
                text[:120],
            )
        sent = False
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
            if not ai_result.ok:
                _send_owner_ai_problem_report(
                    conn,
                    _BOT_USERNAME,
                    f"{ai_result.error_code or 'ai_failure'}: {ai_result.debug_message or ai_result.user_message}",
                    raw_text=text,
                )
            return
        failure_reason = (
            f"{ai_result.error_code or 'ai_failure'}: {ai_result.debug_message or ai_result.user_message}"
            if not ai_result.ok
            else "failed to deliver ai response"
        )
        if not _send_owner_ai_problem_report(conn, _BOT_USERNAME, failure_reason, text):
            logger.warning("[GUEST RUNTIME] failed to notify owner about AI failure sender=%s", sender_id)
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
