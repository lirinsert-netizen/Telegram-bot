"""
settings_ui.py — Настройки чата:
  /settings, welcome/farewell/rules/cleanup UI,
  on_welcome_new_members (new_chat_members — ПОСЛЕ helpers.on_new_members),
  left_chat_member, rules trigger,
  cleanup_delete_commands_runtime, cleanup_delete_system_runtime.
"""
from __future__ import annotations
import time
import threading
import asyncio
import logging
import re as _re
import html as _html
from functools import lru_cache

from config import (
    os, json, re, random, datetime,
    Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    bot, bot_raw, tg_client,
    TOKEN, OWNER_USERNAME, DATA_DIR, API_BASE_URL,
    IS_GUEST_BOT,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    PREMIUM_PREFIX_EMOJI_ID, EMOJI_RATE_LIMIT_ID,
    EMOJI_DEV_ID, EMOJI_MEMBER_ID, EMOJI_ADMIN_ID, EMOJI_OWNER_ID,
    EMOJI_PROFILE_ID, EMOJI_MSG_COUNT_ID, EMOJI_DESC_ID,
    EMOJI_AWARDS_BLOCK_ID, EMOJI_PREMIUM_STATUS_ID,
    EMOJI_VERIFY_ADMIN_ID, EMOJI_VERIFY_DEV_ID,
    EMOJI_ROLE_OWNER_ID, EMOJI_ROLE_CHIEF_ADMIN_ID, EMOJI_ROLE_ADMIN_ID,
    EMOJI_ROLE_MOD_ID, EMOJI_ROLE_TRAINEE_ID,
    EMOJI_USER_ROLE_TEXT_ID, EMOJI_ROLE_ACTION_ID,
    EMOJI_SCOPE_GROUP_ID, EMOJI_SCOPE_PM_ID, EMOJI_SCOPE_ALL_ID,
    EMOJI_LIST_ID, EMOJI_ADMIN_RIGHTS_ID, EMOJI_BTN_UNADMIN_ID, EMOJI_BTN_KICK_ID,
    EMOJI_PING_ID, EMOJI_LOG_ID, EMOJI_LOG_PM_ID,
    EMOJI_CHAT_CLOSED_ID, EMOJI_CHAT_OPEN_BTN_ID,
    EMOJI_SENT_OK_ID, EMOJI_NEW_MSG_OWNER_ID, EMOJI_BOT_VERSION_ID,
    EMOJI_CONTACT_DEV_ID, EMOJI_SEND_TEXT_PROMPT_ID,
    EMOJI_REPLY_BTN_ID, EMOJI_IGNORE_BTN_ID, EMOJI_REPLY_RECEIVED_ID,
    EMOJI_LEGEND_ANYWHERE_ID, EMOJI_LEGEND_DEV_ONLY_ID,
    EMOJI_LEGEND_DEV_OR_VERIFIED_ID, EMOJI_LEGEND_GROUP_ADMIN_ID,
    EMOJI_LEGEND_PM_ONLY_ID, EMOJI_LEGEND_GROUP_ONLY_ID,
    EMOJI_LEGEND_ALL_USERS_ID,
    AWARD_EMOJI_IDS,
    EMOJI_WELCOME_TEXT_ID, EMOJI_WELCOME_MEDIA_ID, EMOJI_WELCOME_BUTTONS_ID,
    EMOJI_LEFT_ID,
    get_user_id_by_username_mtproto,
    get_bot_me,
)
from persistence import (
    VERIFY_ADMINS, VERIFY_DEV,
    DEV_CONTACT_INBOX, DEV_CONTACT_META,
    PENDING_DEV_CONTACT_FROM_USER, PENDING_DEV_REPLY_FROM_OWNER,
    BROADCAST_DRAFTS, BROADCAST_PENDING_INPUT,
    CLOSE_CHAT_STATE, GROUP_STATS, GROUP_SETTINGS,
    CHAT_SETTINGS, MODERATION, PENDING_GROUPS,
    USERS, GLOBAL_USERS, PROFILES,
    CHAT_ROLES, ROLE_PERMS,
    STATS,
    save_verify_admins, save_verify_dev,
    save_dev_contact_inbox, save_dev_contact_meta,
    save_close_chat_state,
    save_group_stats, save_group_settings,
    save_chat_settings, save_moderation, save_pending_groups,
    save_users, save_global_users, save_profiles,
    save_chat_roles, save_role_perms,
    tg_get_chat, tg_get_chat_member,
    tg_invalidate_member_cache, tg_invalidate_chat_cache,
    tg_invalidate_chat_member_caches,
    load_json_file, save_json_file, throttled_save_json_file,
    get_sqlite_status, migrate_legacy_json_to_sqlite,
    _is_duplicate_callback_query,
    get_tg_cache_stats,
    GLOBAL_LAST_SEEN_UPDATE_SECONDS,
    # log-channel
    PENDING_LOG_CHANNEL_SETUP,
    get_log_channel, set_log_channel, remove_log_channel,
    set_log_channel_event, send_log_event,
    LOG_CHANNEL_ALL_EVENTS,
    get_all_bot_chat_ids,
)
from helpers import *
from helpers import _user_can_open_settings, _user_can_edit_now, _build_ranks_keyboard

# Константы наказаний (дублируем из moderation.py, чтобы не создавать цикличных импортов)
MIN_PUNISH_SECONDS = 60
MAX_PUNISH_SECONDS = 365 * 24 * 60 * 60
from moderation import (
    _mod_get_chat, _mod_save, _mod_duration_text,
    _parse_duration_prefix, _build_open_pm_markup,
    _is_farewell_suppressed,
    _mark_farewell_suppressed,
    _mod_new_action_id, _mod_log_append, _mod_warn_add,
    _auto_punish_for_warns,
    _apply_mute, _apply_ban,
)
from pin import _should_keep_pin_service_message, _try_delete_last_bot_service_pin
from cmd_basic import _broadcast_render_panel_text, _build_broadcast_panel_keyboard
from cmd_basic import _sendpm_render_panel_text, _build_sendpm_panel_keyboard, SENDPM_DRAFTS, SENDPM_PENDING_INPUT

# ==== НАСТРОЙКИ ЧАТА (/settings) + WELCOME / FAREWELL / RULES
# ============================================
logger = logging.getLogger(__name__)

def _now_ts() -> int:
    return int(time.time())


# ------------------------------------------------------------
# Pending helpers (чтобы cancel/ok работали одинаково везде)
# Хранятся в памяти (_PENDING_STATE), а не в CHAT_SETTINGS,
# чтобы не провоцировать лишние записи на диск.
# При перезапуске бота ожидающие ввода состояния сбрасываются — это нормально.
# ------------------------------------------------------------

_PENDING_STATE: dict = {}


def _pending_get(key: str) -> dict:
    return _PENDING_STATE.get(key) or {}


def _pending_put(key: str, user_id: int, chat_id: int):
    d = _PENDING_STATE.get(key) or {}
    d[str(user_id)] = str(chat_id)
    _PENDING_STATE[key] = d


def _pending_pop(key: str, user_id: int) -> Optional[str]:
    d = _PENDING_STATE.get(key) or {}
    val = d.pop(str(user_id), None)
    _PENDING_STATE[key] = d
    return val


def _pending_msg_get(key: str, user_id: int) -> Optional[int]:
    d = _PENDING_STATE.get(key) or {}
    val = d.get(str(user_id))
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def _pending_msg_set(key: str, user_id: int, msg_id: int):
    d = _PENDING_STATE.get(key) or {}
    d[str(user_id)] = int(msg_id)
    _PENDING_STATE[key] = d


def _pending_msg_pop(key: str, user_id: int) -> Optional[int]:
    d = _PENDING_STATE.get(key) or {}
    val = d.pop(str(user_id), None)
    _PENDING_STATE[key] = d
    try:
        return int(val) if val is not None else None
    except Exception:
        return None


def _pending_set_raw(key: str, user_id: int, value: str) -> None:
    """Установить произвольную строку в pending-состоянии (для внешних модулей)."""
    d = _PENDING_STATE.get(key) or {}
    d[str(user_id)] = value
    _PENDING_STATE[key] = d


def _pending_pop_raw(key: str, user_id: int) -> Optional[str]:
    """Извлечь произвольную строку из pending-состояния (для внешних модулей)."""
    d = _PENDING_STATE.get(key) or {}
    val = d.pop(str(user_id), None)
    _PENDING_STATE[key] = d
    return val


def _try_delete_private_prompt(chat_id: int, msg_id: Optional[int]):
    """Пытаемся удалить сообщение бота в ЛС. Любые ошибки проглатываем."""
    if not msg_id:
        return
    try:
        raw_delete_message(chat_id, msg_id)
        return
    except Exception:
        pass
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


def _delete_pending_ui(chat_id: int, msg_key: str, user_id: int, also_msg_id: Optional[int] = None):
    """
    Удаляет текущую UI-мессагу для pending_* (prompt/error/deleted),
    которая хранится в pending_*_msg. Если stored msg_id нет — можно
    передать also_msg_id (например c.message.message_id).
    """
    stored = _pending_msg_pop(msg_key, user_id)
    if stored:
        _try_delete_private_prompt(chat_id, stored)
    if also_msg_id and (not stored or stored != also_msg_id):
        _try_delete_private_prompt(chat_id, also_msg_id)


def _replace_pending_ui(chat_id: int, msg_key: str, user_id: int, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """
    Заменяет UI-мессагу для pending_*: удаляет предыдущую (если была),
    отправляет новую и сохраняет её message_id в msg_key.
    """
    old_id = _pending_msg_pop(msg_key, user_id)
    _try_delete_private_prompt(chat_id, old_id)
    sent = bot.send_message(chat_id, text, parse_mode=parse_mode, disable_web_page_preview=True, reply_markup=reply_markup)
    _pending_msg_set(msg_key, user_id, sent.message_id)
    return sent


def _build_cancel_btn(callback_data: str) -> "InlineKeyboardButton":
    btn = InlineKeyboardButton("Отмена", callback_data=callback_data)
    try:
        btn.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
    except Exception:
        pass
    return btn


def _build_back_to_prompt_btn(callback_data: str) -> "InlineKeyboardButton":
    btn = InlineKeyboardButton("Назад", callback_data=callback_data)
    try:
        btn.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    except Exception:
        pass
    try:
        btn.style = "primary"
    except Exception:
        pass
    return btn


def _kb_error_cancel(callback_data: str) -> "InlineKeyboardMarkup":
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(_build_cancel_btn(callback_data))
    return kb


def _kb_deleted(back_cb: str, cancel_cb: str) -> "InlineKeyboardMarkup":
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(_build_back_to_prompt_btn(back_cb), _build_cancel_btn(cancel_cb))
    return kb


def _safe_edit_message_html(chat_id: int, msg_id: int, text: str, reply_markup=None, disable_web_page_preview: bool = True) -> bool:
    """
    FIX #1:
    при edit_message_text всегда передаём parse_mode='HTML', иначе
    в интерфейсе/превью будут показываться теги (<tg-emoji>, <b>, <quote> и т.д.)
    """
    try:
        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode="HTML",
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup,
        )
        return True
    except Exception as e:
        # Частый кейс: пользователь нажал уже выбранное значение, Telegram отвечает
        # "message is not modified" — это не ошибка для UI.
        if "message is not modified" in str(e).lower():
            return True
        # fallback на твой raw-редактор, если он есть
        try:
            resp = raw_edit_message_with_keyboard(chat_id, msg_id, text, reply_markup)
            if isinstance(resp, dict):
                if resp.get("ok"):
                    return True
                desc = str(resp.get("description") or "").lower()
                if "message is not modified" in desc:
                    return True
            return False
        except Exception:
            return False


# ------------------------------------------------------------
# Section model: welcome / farewell / rules
# ------------------------------------------------------------

SECTION_KEYS = ("welcome", "farewell", "rules", "first_comment")


def _default_section(enabled: bool) -> dict:
    return {
        "enabled": enabled,          # rules может быть выключен/включен, но использоваться по кнопке
        "text_custom": "",           # канон: твой кастом
        "source": "plain",           # plain/custom/entities/hybrid
        "entities": [],              # debug
        "updated_at": 0,
        "media": [],                 # список элементов медиа (dict)
        "buttons": {                 # кнопки + попапы
            "rows": [],              # rows: [[btn,btn],[btn]]
            "popups": [],            # список текстов попапов
        },
    }


def get_chat_settings(chat_id: int) -> dict:
    cid = str(chat_id)
    st = CHAT_SETTINGS.get(cid)
    _changed = False

    # --- новый чат ---
    if st is None or not isinstance(st, dict):
        st = {
            "welcome": _default_section(False),
            "farewell": _default_section(False),
            "rules": _default_section(False),
            "first_comment": _default_section(False),
            "cleanup": _default_cleanup(),
        }
        CHAT_SETTINGS[cid] = st
        save_chat_settings()
        return st

    # --- миграция/нормализация секций ---
    for sec in SECTION_KEYS:
        cur = st.get(sec)

        if cur is None or not isinstance(cur, dict):
            cur = _default_section(False)
            _changed = True

        # --- миграция старых полей ---
        if "text_custom" not in cur:
            _changed = True
            raw = (
                cur.get("text_custom")
                or cur.get("text_raw")
                or cur.get("text_html")
                or cur.get("text")
                or ""
            )
            cur["text_custom"] = raw if isinstance(raw, str) else ""
            cur["source"] = cur.get("source") or (
                "custom" if _contains_custom_tags(cur["text_custom"]) else "plain"
            )
            cur["entities"] = cur.get("text_entities") or cur.get("entities") or []
            cur["updated_at"] = cur.get("updated_at") or 0

            if not isinstance(cur.get("media"), list):
                cur["media"] = []

            btn = cur.get("buttons")
            if isinstance(btn, list):
                cur["buttons"] = {"rows": btn, "popups": []}
            elif btn is None:
                cur["buttons"] = {"rows": [], "popups": []}
            elif isinstance(btn, dict):
                btn.setdefault("rows", [])
                btn.setdefault("popups", [])
                cur["buttons"] = btn
            else:
                cur["buttons"] = {"rows": [], "popups": []}

            for k in ("text_raw", "text_html", "text_entities", "text"):
                cur.pop(k, None)

        # --- нормализация текущего формата ---
        if "enabled" not in cur:
            cur["enabled"] = False
            _changed = True
        if "text_custom" not in cur:
            cur["text_custom"] = ""
            _changed = True
        if "source" not in cur:
            cur["source"] = "plain"
            _changed = True
        if "entities" not in cur:
            cur["entities"] = []
            _changed = True
        if "updated_at" not in cur:
            cur["updated_at"] = 0
            _changed = True

        if not isinstance(cur.get("media"), list):
            cur["media"] = []
            _changed = True

        btn = cur.get("buttons")
        if isinstance(btn, list):
            cur["buttons"] = {"rows": btn, "popups": []}
            _changed = True
        elif btn is None:
            cur["buttons"] = {"rows": [], "popups": []}
            _changed = True
        elif isinstance(btn, dict):
            if "rows" not in btn:
                btn["rows"] = []
                _changed = True
            if "popups" not in btn:
                btn["popups"] = []
                _changed = True
            cur["buttons"] = btn
        else:
            cur["buttons"] = {"rows": [], "popups": []}
            _changed = True

        if not isinstance(cur["buttons"].get("rows"), list):
            cur["buttons"]["rows"] = []
            _changed = True
        if not isinstance(cur["buttons"].get("popups"), list):
            cur["buttons"]["popups"] = []
            _changed = True

        st[sec] = cur

    # --- нормализация cleanup (В КОНЦЕ, после секций) ---
    cleanup_norm, changed = _normalize_cleanup(st.get("cleanup"))
    if changed:
        st["cleanup"] = cleanup_norm
        _changed = True

    # --- нормализация commands ---
    if not isinstance(st.get("commands"), dict):
        st["commands"] = {}
        _changed = True

    CHAT_SETTINGS[cid] = st
    if _changed:
        save_chat_settings()
    return st

# ------------------------------------------------------------
# CLEANUP: удаление сообщений (Команды / Системные сообщения)
# ------------------------------------------------------------

CLEANUP_CMD_SIGNS = ("/", ".", "!", ",", "#")

# Premium emoji ids (твои)
CLEANUP_ICON_ENABLE_ID = "5825794181183836432"   # включить
CLEANUP_ICON_DISABLE_ID = "5778527486270770928"  # выключить

# Системные типы (как content_type у pyTelegramBotAPI)
CLEANUP_SYSTEM_TYPES_ORDER = [
    "new_chat_members",
    "left_chat_member",
    "new_chat_title",
    "new_chat_photo",
    "delete_chat_photo",
    "pinned_message",
    "message_auto_delete_timer_changed",
    "video_chat_scheduled",
    "video_chat_started",
    "video_chat_ended",
    "video_chat_participants_invited",
    "boost_added",
]

CLEANUP_SYSTEM_LABELS = {
    "new_chat_members": "Вход/добавление участников",
    "left_chat_member": "Выход/удаление участников",
    "new_chat_title": "Изменение названия",
    "new_chat_photo": "Новое фото чата",
    "delete_chat_photo": "Удаление фото чата",
    "pinned_message": "Закрепление сообщения",
    "message_auto_delete_timer_changed": "Таймер автоудаления",
    "video_chat_scheduled": "Запланирован видеочат",
    "video_chat_started": "Видеочат начался",
    "video_chat_ended": "Видеочат закончился",
    "video_chat_participants_invited": "Приглашения в видеочат",
    "boost_added": "Бусты",
}

CLEANUP_SYSTEM_CONTENT_TYPES = list(CLEANUP_SYSTEM_LABELS.keys())


def _default_cleanup() -> dict:
    return {
        "commands": {s: False for s in CLEANUP_CMD_SIGNS},
        "system": {ct: False for ct in CLEANUP_SYSTEM_TYPES_ORDER},
        "updated_at": 0,
        # legacy: оставим, но UI больше не использует
        "system_messages": False,
    }


def _normalize_cleanup(cleanup_any) -> tuple[dict, bool]:
    """
    Возвращает (cleanup_norm, changed_flag).
    Миграция с legacy system_messages:
      - если system ещё не было, а system_messages=True -> включим ВСЕ system-типы.
    """
    changed = False
    if not isinstance(cleanup_any, dict):
        return _default_cleanup(), True

    cleanup = dict(cleanup_any)  # копия

    # commands
    cmds = cleanup.get("commands")
    if not isinstance(cmds, dict):
        cmds = {}
        changed = True
    for s in CLEANUP_CMD_SIGNS:
        v = cmds.get(s, False)
        if not isinstance(v, bool):
            v = bool(v)
            changed = True
        if s not in cmds:
            changed = True
        cmds[s] = v
    cleanup["commands"] = cmds

    # system
    legacy_sys = cleanup.get("system_messages")
    sysd = cleanup.get("system")
    sys_was_missing = not isinstance(sysd, dict)
    if not isinstance(sysd, dict):
        sysd = {}
        changed = True

    for ct in CLEANUP_SYSTEM_TYPES_ORDER:
        v = sysd.get(ct, False)
        if not isinstance(v, bool):
            v = bool(v)
            changed = True
        if ct not in sysd:
            changed = True
        # миграция legacy
        if sys_was_missing and isinstance(legacy_sys, bool) and legacy_sys:
            v = True
        sysd[ct] = v

    cleanup["system"] = sysd

    # updated_at
    if not isinstance(cleanup.get("updated_at"), int):
        cleanup["updated_at"] = int(cleanup.get("updated_at") or 0)
        changed = True

    # legacy key keep
    if not isinstance(cleanup.get("system_messages"), bool):
        cleanup["system_messages"] = bool(cleanup.get("system_messages"))
        changed = True

    return cleanup, changed


def _cleanup_get(chat_id: int) -> dict:
    st = get_chat_settings(chat_id)
    cleanup_norm, changed = _normalize_cleanup(st.get("cleanup"))
    if changed:
        st["cleanup"] = cleanup_norm
        CHAT_SETTINGS[str(chat_id)] = st
        save_chat_settings()
    return cleanup_norm


def _cleanup_save(chat_id: int, cleanup: dict):
    st = get_chat_settings(chat_id)
    st["cleanup"] = cleanup
    CHAT_SETTINGS[str(chat_id)] = st
    save_chat_settings()


def _bot_can_delete_messages(chat_id: int) -> bool:
    """
    Проверяем право бота на удаление сообщений.
    ВАЖНО: никаких уведомлений в чат в рантайме -> без флуда.
    """
    bot_id = _get_bot_id()  # определён ниже в файле — ок
    if not bot_id:
        return False
    try:
        member = bot.get_chat_member(chat_id, bot_id)
        if getattr(member, "status", "") == "creator":
            return True
        if getattr(member, "status", "") == "administrator" and getattr(member, "can_delete_messages", False):
            return True
    except Exception:
        pass
    return False


# ------------------------------------------------------------
# Твой кастом -> Telegram HTML
# ------------------------------------------------------------

def _contains_custom_tags(s: str) -> bool:
    """
    FIX #1 (часть 2):
    Раньше функция не считала <b>/<i>/<u>/<s>/<code>/<pre>/<a ...> за кастом,
    из-за чего source часто становился "plain" и внешний код мог отправлять text_custom
    без конвертации -> в чате показывались теги.
    """
    if not s:
        return False
    sl = s.lower()
    return (
        "<b" in sl or "<i" in sl or "<u" in sl or "<s" in sl or "<code" in sl or "<pre" in sl
        or "<sp" in sl or "<spoiler" in sl or "<quote" in sl or "<emoji" in sl
        or "<br" in sl or "<a " in sl
        # поддержим также "официальные" теги Telegram, если пользователь их вставит:
        or "<tg-emoji" in sl or "<blockquote" in sl or 'class="tg-spoiler"' in sl
    )


class _Node:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: Optional[str] = None, attrs: Optional[dict] = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: List[Any] = []

    def append(self, child: Any):
        self.children.append(child)

    def render(self) -> str:
        # FIX: нормализуем escape/unescape, чтобы не было двойного &amp;amp;
        if self.tag is None:
            return "".join(
                ch.render() if isinstance(ch, _Node) else _html.escape(_html.unescape(str(ch)))
                for ch in self.children
            )

        inner = "".join(
            ch.render() if isinstance(ch, _Node) else _html.escape(_html.unescape(str(ch)))
            for ch in self.children
        )

        tag = self.tag
        attrs = self.attrs or {}

        if tag == "tg-emoji":
            eid = _html.escape(attrs.get("emoji-id", ""), quote=True)
            return f'<tg-emoji emoji-id="{eid}">{inner}</tg-emoji>'

        if tag == "span" and attrs.get("class") == "tg-spoiler":
            return f'<span class="tg-spoiler">{inner}</span>'

        if tag == "a":
            href = _html.escape(attrs.get("href", ""), quote=True)
            return f'<a href="{href}">{inner}</a>'

        if tag == "blockquote":
            if attrs.get("expandable") == "true":
                return f'<blockquote expandable="true">{inner}</blockquote>'
            return f"<blockquote>{inner}</blockquote>"

        return f"<{tag}>{inner}</{tag}>"


def convert_custom_markup_to_telegram_html(text: str) -> str:
    """
    Вход (твой кастом):
      <b>..</> <i>..</> <u>..</> <s>..</>
      <code>..</> <pre>..</>
      <sp>..</> / <spoiler>..</>
      <a href='URL'>..</>
      <quote>..</> / <quote exp>..</>
      <emoji id='123'>😀</>
      <br> -> \n

    + ПОДДЕРЖКА "официального" Telegram HTML (если пользователь вставит):
      <tg-emoji emoji-id="...">..</tg-emoji>
      <blockquote expandable="true">..</blockquote>
      <span class="tg-spoiler">..</span>
    """
    if not text:
        return ""

    s = text
    i = 0
    n = len(s)
    root = _Node()
    stack = [root]

    def push_text(chunk: str):
        if chunk:
            stack[-1].append(chunk)

    while i < n:
        if s[i] != "<":
            nxt = s.find("<", i)
            if nxt == -1:
                push_text(s[i:])
                break
            push_text(s[i:nxt])
            i = nxt
            continue

        close = s.find(">", i + 1)
        if close == -1:
            push_text(s[i:])
            break

        rawtag = s[i + 1:close].strip()
        i = close + 1
        if not rawtag:
            continue

        raw_low = rawtag.lower()

        if raw_low in ("br", "br/"):
            push_text("\n")
            continue

        # закрывающие: </> или </b> или </tg-emoji> и т.д.
        if rawtag.startswith("/"):
            name = rawtag[1:].strip().lower()
            if not name:
                if len(stack) > 1:
                    stack.pop()
                continue
            name = name.split()[0]
            j = len(stack) - 1
            while j > 0 and stack[j].tag not in (name, "blockquote"):
                j -= 1
            if j > 0:
                while len(stack) - 1 >= j:
                    stack.pop()
            continue

        # --- Официальный tg-emoji ---
        if raw_low.startswith("tg-emoji"):
            m = re.match(r'tg-emoji\s+emoji-id=[\'"]?(\d+)[\'"]?', rawtag, flags=re.I)
            if not m:
                push_text("<" + rawtag + ">")
                continue
            eid = m.group(1)
            node = _Node("tg-emoji", {"emoji-id": eid})
            stack[-1].append(node)
            stack.append(node)
            continue

        # --- Официальный blockquote ---
        if raw_low.startswith("blockquote"):
            attrs = {}
            if re.search(r'expandable\s*=\s*[\'"]?true[\'"]?', rawtag, flags=re.I):
                attrs["expandable"] = "true"
            node = _Node("blockquote", attrs)
            stack[-1].append(node)
            stack.append(node)
            continue

        # --- Официальный spoiler span ---
        if raw_low.startswith("span"):
            if re.search(r'class\s*=\s*[\'"]tg-spoiler[\'"]', rawtag, flags=re.I):
                node = _Node("span", {"class": "tg-spoiler"})
                stack[-1].append(node)
                stack.append(node)
                continue

        # quote (твой кастом)
        if raw_low.startswith("quote"):
            attrs = {}
            if re.match(r"quote\s+exp", raw_low):
                attrs["expandable"] = "true"
            node = _Node("blockquote", attrs)
            stack[-1].append(node)
            stack.append(node)
            continue

        # emoji (твой кастом)
        if raw_low.startswith("emoji"):
            m = re.match(r"emoji\s+id=['\"]?(\d+)['\"]?", rawtag, flags=re.I)
            if not m:
                push_text("<" + rawtag + ">")
                continue
            eid = m.group(1)
            node = _Node("tg-emoji", {"emoji-id": eid})
            stack[-1].append(node)
            stack.append(node)
            continue

        # a href
        if raw_low.startswith("a"):
            m = re.match(r'a\s+href=[\'"]([^\'"]+)[\'"]', rawtag, flags=re.I)
            if not m:
                push_text("<" + rawtag + ">")
                continue
            href = m.group(1)
            node = _Node("a", {"href": href})
            stack[-1].append(node)
            stack.append(node)
            continue

        tagname = raw_low.split()[0]

        if tagname in ("sp", "spoiler"):
            node = _Node("span", {"class": "tg-spoiler"})
            stack[-1].append(node)
            stack.append(node)
            continue

        if tagname in ("b", "i", "u", "s", "code", "pre"):
            node = _Node(tagname, {})
            stack[-1].append(node)
            stack.append(node)
            continue

        push_text("<" + rawtag + ">")

    return root.render()


# ------------------------------------------------------------
# Telegram entities -> твой кастом (UTF-16 offsets)
# ------------------------------------------------------------

def _utf16_units(text: str) -> List[int]:
    b = text.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _slice_utf16(text: str, units: List[int], start_u: int, len_u: int) -> str:
    start = max(start_u, 0)
    end = max(start_u + len_u, 0)
    sub = units[start:end]
    bb = b"".join(u.to_bytes(2, "little") for u in sub)
    return bb.decode("utf-16-le")


def _remove_utf16_range(text: str, start_u: int, len_u: int) -> str:
    units = _utf16_units(text)
    start = max(start_u, 0)
    end = max(start_u + len_u, 0)
    if start >= len(units) or end <= start:
        return text
    end = min(end, len(units))
    new_units = units[:start] + units[end:]
    bb = b"".join(u.to_bytes(2, "little") for u in new_units)
    return bb.decode("utf-16-le")


def _serialize_entities(entities: list) -> list:
    out = []
    for e in (entities or []):
        out.append({
            "type": getattr(e, "type", "") or "",
            "offset": int(getattr(e, "offset", 0) or 0),
            "length": int(getattr(e, "length", 0) or 0),
            "custom_emoji_id": getattr(e, "custom_emoji_id", None),
            "url": getattr(e, "url", None),
        })
    return out


def _entity_conflicts_with_tags(text: str, entities: list) -> bool:
    if not text or not entities:
        return False
    units = _utf16_units(text)
    for e in entities:
        try:
            off = int(getattr(e, "offset", 0) or 0)
            ln = int(getattr(e, "length", 0) or 0)
        except Exception:
            continue
        if ln <= 0:
            continue
        seg = _slice_utf16(text, units, off, ln)
        if "<" in seg or ">" in seg:
            return True
    return False


def _wrap_custom(escaped_inner: str, ent) -> str:
    et = (getattr(ent, "type", "") or "").lower()

    if et == "custom_emoji":
        ce_id = getattr(ent, "custom_emoji_id", None)
        ce_safe = _html.escape(str(ce_id or ""), quote=True)
        return f"<emoji id='{ce_safe}'>{escaped_inner}</>"

    if et == "bold":
        return f"<b>{escaped_inner}</>"
    if et == "italic":
        return f"<i>{escaped_inner}</>"
    if et == "underline":
        return f"<u>{escaped_inner}</>"
    if et == "strikethrough":
        return f"<s>{escaped_inner}</>"
    if et == "spoiler":
        return f"<spoiler>{escaped_inner}</>"
    if et == "code":
        return f"<code>{escaped_inner}</>"
    if et == "pre":
        return f"<pre>{escaped_inner}</>"
    if et == "text_link":
        url = getattr(ent, "url", "") or ""
        url_safe = _html.escape(url, quote=True)
        return f"<a href='{url_safe}'>{escaped_inner}</>"
    if et == "url":
        href = _html.unescape(escaped_inner)
        href_safe = _html.escape(href, quote=True)
        return f"<a href='{href_safe}'>{escaped_inner}</>"

    return escaped_inner


def entities_to_custom(text: str, entities: list) -> str:
    if not text:
        return ""
    if not entities:
        return _html.escape(text)

    units = _utf16_units(text)
    total_u = len(units)

    norm = []
    for e in entities:
        try:
            off = int(getattr(e, "offset", 0) or 0)
            ln = int(getattr(e, "length", 0) or 0)
        except Exception:
            continue
        if ln <= 0:
            continue
        end = min(off + ln, total_u)
        if off < 0 or off >= total_u or end <= off:
            continue
        norm.append((off, end, e))

    if not norm:
        return _html.escape(text)

    bounds = {0, total_u}
    for off, end, _ in norm:
        bounds.add(off)
        bounds.add(end)
    bounds = sorted(bounds)

    def prio(ent) -> int:
        t = (getattr(ent, "type", "") or "").lower()
        order = {
            "blockquote": 0,
            "expandable_blockquote": 0,
            "text_link": 1,
            "url": 1,
            "bold": 2,
            "italic": 3,
            "underline": 4,
            "strikethrough": 5,
            "spoiler": 6,
            "code": 7,
            "pre": 7,
            "custom_emoji": 8,
        }
        return order.get(t, 50)

    out_parts: List[str] = []

    for i in range(len(bounds) - 1):
        seg_start = bounds[i]
        seg_end = bounds[i + 1]
        if seg_end <= seg_start:
            continue

        raw_seg = _slice_utf16(text, units, seg_start, seg_end - seg_start)
        esc_seg = _html.escape(raw_seg)

        active = [ent for off, end, ent in norm if off <= seg_start and end >= seg_end]
        if not active:
            out_parts.append(esc_seg)
            continue

        quote_type = None
        non_quote = []
        for ent in active:
            t = (getattr(ent, "type", "") or "").lower()
            if t == "blockquote":
                quote_type = "quote"
            elif t == "expandable_blockquote":
                quote_type = "quote exp"
            else:
                non_quote.append(ent)

        non_quote_sorted = sorted(non_quote, key=prio)

        inner = esc_seg
        for ent in reversed(non_quote_sorted):
            inner = _wrap_custom(inner, ent)

        if quote_type == "quote":
            inner = f"<quote>{inner}</>"
        elif quote_type == "quote exp":
            inner = f"<quote exp>{inner}</>"

        out_parts.append(inner)

    return "".join(out_parts)


# ------------------------------------------------------------
# Message -> canonical text_custom
# ------------------------------------------------------------

def convert_section_text_from_message(m: types.Message) -> Tuple[str, str, list]:
    # ВАЖНО: offsets entities считаются по исходному тексту.
    raw_full = (m.text or "")
    entities = m.entities or []

    if not raw_full.strip():
        return "", "plain", []

    # если есть entities — НЕ strip'аем, иначе оффсеты съедут
    raw_text = raw_full if entities else raw_full.strip()

    entities_ser = _serialize_entities(entities)
    has_custom = _contains_custom_tags(raw_text)
    has_entities = bool(entities)

    if not has_custom and not has_entities:
        return raw_text, "plain", entities_ser

    if has_custom and not has_entities:
        return raw_text, "custom", entities_ser

    if (not has_custom) and has_entities:
        return entities_to_custom(raw_text, entities), "entities", entities_ser

    if _entity_conflicts_with_tags(raw_text, entities):
        return raw_text, "custom", entities_ser

    return entities_to_custom(raw_text, entities), "hybrid", entities_ser


def build_html_from_text_custom(text_custom: str) -> str:
    tc = (text_custom or "").strip()
    if not tc:
        return ""
    try:
        return convert_custom_markup_to_telegram_html(tc)
    except Exception:
        return _html.escape(tc)


def _apply_vars(html_text: str, chat_id: int, chat_title: str, user_obj) -> str:
    viewer = user_obj
    viewer_name = (viewer.full_name or viewer.first_name or "").strip() or "Участник"
    viewer_link = link_for_user(chat_id, viewer.id)
    try:
        viewer_mention = mention_html_user(viewer)
    except Exception:
        viewer_mention = viewer_link

    return (
        (html_text or "")
        .replace("[NAME]", _html.escape(viewer_name))
        .replace("[ID]", str(viewer.id))
        .replace("[GROUP_NAME]", _html.escape(chat_title or str(chat_id)))
        .replace("[NAME_LINK]", viewer_link)
        .replace("[MENTION]", viewer_mention)
    )


# ------------------------------------------------------------
# Media: store file_id, type, (caption не храним отдельно!)
# ------------------------------------------------------------

SUPPORTED_MEDIA_TYPES = {"photo", "video", "document", "audio", "animation"}


def _extract_media_payload(m: types.Message) -> Optional[dict]:
    ct = m.content_type
    if ct not in SUPPORTED_MEDIA_TYPES:
        return None

    if ct == "photo":
        # берём самое большое
        fid = m.photo[-1].file_id if m.photo else None
    elif ct == "video":
        fid = m.video.file_id if m.video else None
    elif ct == "document":
        fid = m.document.file_id if m.document else None
    elif ct == "audio":
        fid = m.audio.file_id if m.audio else None
    elif ct == "animation":
        fid = m.animation.file_id if m.animation else None
    else:
        fid = None

    if not fid:
        return None

    return {"type": ct, "file_id": fid}


def _media_can_album(items: List[dict]) -> bool:
    if not items or len(items) < 2:
        return False
    # альбомы: фото/видео (gif/audio/doc не альбом)
    for it in items:
        if it.get("type") not in ("photo", "video"):
            return False
    return True


def _send_media_only(chat_id: int, media: List[dict]):
    # показ без текста и без кнопок
    if not media:
        return

    if _media_can_album(media):
        mg = []
        for it in media:
            t = it["type"]
            fid = it["file_id"]
            if t == "photo":
                mg.append(types.InputMediaPhoto(media=fid))
            else:
                mg.append(types.InputMediaVideo(media=fid))
        bot.send_media_group(chat_id, mg)
        return

    # single or non-album list: шлём по одному
    for it in media:
        t = it["type"]
        fid = it["file_id"]
        if t == "photo":
            bot.send_photo(chat_id, fid)
        elif t == "video":
            bot.send_video(chat_id, fid)
        elif t == "document":
            bot.send_document(chat_id, fid)
        elif t == "audio":
            bot.send_audio(chat_id, fid)
        elif t == "animation":
            bot.send_animation(chat_id, fid)


def _send_payload(chat_id: int, html_text: str, media: List[dict], reply_markup=None, disable_web_page_preview=True, reply_to_message_id: Optional[int] = None):
    """
    Главное правило: caption отдельно не задаём пользователю.
    Если media есть, то caption = html_text (если поддерживается),
    иначе text message = html_text.
    """
    html_text = (html_text or "").strip()

    if media:
        if _media_can_album(media):
            mg = []
            for idx, it in enumerate(media):
                t = it["type"]
                fid = it["file_id"]
                if t == "photo":
                    if idx == 0 and html_text:
                        mg.append(types.InputMediaPhoto(media=fid, caption=html_text, parse_mode="HTML"))
                    else:
                        mg.append(types.InputMediaPhoto(media=fid))
                else:
                    if idx == 0 and html_text:
                        mg.append(types.InputMediaVideo(media=fid, caption=html_text, parse_mode="HTML"))
                    else:
                        mg.append(types.InputMediaVideo(media=fid))
            bot.send_media_group(chat_id, mg, reply_to_message_id=reply_to_message_id)
            # кнопки нельзя к media_group, поэтому отдельным сообщением с невидимым символом
            if reply_markup:
                bot.send_message(chat_id, "\u2063", disable_web_page_preview=True, reply_markup=reply_markup)
            return

        # НЕ альбом: шлём первое медиа с caption, остальное без
        first = True
        for it in media:
            t = it["type"]
            fid = it["file_id"]
            cap = html_text if (first and html_text) else None
            cur_markup = reply_markup if first else None
            rtid = reply_to_message_id if first else None
            first = False

            if t == "photo":
                bot.send_photo(chat_id, fid, caption=cap, parse_mode="HTML" if cap else None, reply_markup=cur_markup, reply_to_message_id=rtid)
            elif t == "video":
                bot.send_video(chat_id, fid, caption=cap, parse_mode="HTML" if cap else None, reply_markup=cur_markup, reply_to_message_id=rtid)
            elif t == "document":
                bot.send_document(chat_id, fid, caption=cap, parse_mode="HTML" if cap else None, reply_markup=cur_markup, reply_to_message_id=rtid)
            elif t == "audio":
                bot.send_audio(chat_id, fid, caption=cap, parse_mode="HTML" if cap else None, reply_markup=cur_markup, reply_to_message_id=rtid)
            elif t == "animation":
                bot.send_animation(chat_id, fid, caption=cap, parse_mode="HTML" if cap else None, reply_markup=cur_markup, reply_to_message_id=rtid)
        return

    # no media
    if not html_text:
        # нельзя отправить «пусто» с кнопками — подставим невидимый символ
        if reply_markup:
            return bot.send_message(chat_id, "\u2063", disable_web_page_preview=True, reply_markup=reply_markup, reply_to_message_id=reply_to_message_id)
        return

    bot.send_message(
        chat_id,
        html_text,
        parse_mode="HTML",
        disable_web_page_preview=disable_web_page_preview,
        reply_markup=reply_markup,
        reply_to_message_id=reply_to_message_id,
    )


# ------------------------------------------------------------
# Buttons parsing
# ------------------------------------------------------------

MAX_ROWS = 10
MAX_TOTAL_BTNS = 30
MAX_PER_ROW = 3  # твоя логика


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

    if problem == "format":
        base = "Неправильный формат"
    elif problem == "url":
        base = "Неправильная ссылка"
    else:
        base = "Другая проблема"

    if line_no > 0:
        prefix = f"<b>Строка {line_no}:</b> "
    else:
        prefix = "<b>Ошибка:</b> "

    if details:
        return f"{prefix}{base}. {details}"
    return f"{prefix}{base}."


def _normalize_url(raw: str) -> str:
    u = (raw or "").strip()
    if not u:
        return u
    # если нет схемы — добавим https://
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", u):
        u = "https://" + u
    return u


def _is_supported_button_url(url: str) -> bool:
    value = (url or "").strip()
    if not value or re.search(r"\s", value):
        return False

    if re.match(r"^tg://", value, flags=re.I):
        return True

    if not re.match(r"^https?://", value, flags=re.I):
        return False

    host = re.sub(r"^https?://", "", value, flags=re.I).split("/", 1)[0].strip()
    if not host:
        return False

    return "." in host or host.lower() == "localhost"


def _button_syntax_error(line_no: int, problem: str, details: str = "") -> ButtonSyntaxError:
    return ButtonSyntaxError(line_no=line_no, problem=problem, details=details)


def _sanitize_button_for_payload(button: Any, popups: List[str]) -> Optional[dict]:
    if not isinstance(button, dict):
        return None

    btn_type = str(button.get("type") or "").strip().lower()
    if btn_type not in {"url", "popup", "rules", "del", "cmd"}:
        return None

    icon_eid = button.get("icon_emoji_id")
    text = str(button.get("text") or "").strip()
    if not text and not icon_eid:
        return None
    if not text:
        text = " "

    style = button.get("style")
    if style not in {None, "danger", "success", "primary"}:
        style = None

    normalized = {
        "type": btn_type,
        "text": text,
        "style": style,
        "icon_emoji_id": str(icon_eid) if icon_eid else None,
    }

    if btn_type == "url":
        url = _normalize_url(str(button.get("url") or ""))
        if not _is_supported_button_url(url):
            return None
        normalized["url"] = url
        return normalized

    if btn_type == "popup":
        try:
            idx = int(button.get("popup_index"))
        except Exception:
            return None

        if idx < 0 or idx >= len(popups):
            return None

        popup_text = str(popups[idx] or "").strip()
        if not popup_text:
            return None

        normalized["popup_index"] = idx
        return normalized

    if btn_type == "cmd":
        cmd_name = str(button.get("cmd_name") or "").strip()
        if not cmd_name:
            return None
        normalized["cmd_name"] = cmd_name
        return normalized

    return normalized


def _extract_button_icon_custom_emoji_id(label: str) -> Tuple[str, Optional[str]]:
    """
    Поддержка твоего кастома для премиум-эмодзи в начале:
      <emoji id='123'>😀</> Текст
    -> icon_custom_emoji_id=123, label="Текст"

    Если эмодзи не в начале — считаем обычным текстом (не пытаемся магичить).
    """
    s = (label or "").strip()
    m = re.match(r"^\s*<emoji\s+id=['\"](\d+)['\"]>\s*.*?\s*</>\s*", s, flags=re.I | re.S)
    if not m:
        return s, None
    eid = m.group(1)
    rest = re.sub(r"^\s*<emoji\s+id=['\"]\d+['\"]>\s*.*?\s*</>\s*", "", s, flags=re.I | re.S).strip()
    return (rest if rest else " "), eid


def _find_custom_emoji_entity_at_offset(entities: list, offset_u: int):
    """Ищем custom_emoji entity, который начинается ровно на offset_u."""
    if not entities:
        return None
    for e in entities:
        try:
            et = (getattr(e, "type", "") or "").lower()
            if et != "custom_emoji":
                continue
            off = int(getattr(e, "offset", 0) or 0)
            ln = int(getattr(e, "length", 0) or 0)
            if off != offset_u or ln <= 0:
                continue
            ce_id = getattr(e, "custom_emoji_id", None)
            if not ce_id:
                continue
            return ln, str(ce_id)
        except Exception:
            continue
    return None


def parse_buttons_text(user_text: str, entities: Optional[list] = None) -> Tuple[List[List[dict]], List[str]]:
    """
    Формат:
      #r Название - example.com & #g Название - popup: текст
      Название - rules
      Название - del

    Возвращает:
      rows: [[btn, btn], [btn]]
      popups: ["текст", ...]
    btn dict:
      {"type":"url|popup|rules|del", "text":"...", "style":"danger|success|primary|None", "url": "...", "popup_index": int, "icon_emoji_id": "..."}

    FIX #2:
    если в названии кнопки стоит premium/custom emoji БЕЗ нашего <emoji id='...'>,
    то Telegram присылает entity type=custom_emoji. Мы забираем custom_emoji_id и
    ставим как icon_custom_emoji_id для кнопки.
    """
    original = user_text or ""
    text = original.strip()
    if not text:
        return [], []

    if len(original) > 6000:
        raise _button_syntax_error(0, "other", "Слишком длинный текст кнопок.")

    has_custom_emoji_entities = False
    if entities:
        for e in entities:
            try:
                if (getattr(e, "type", "") or "").lower() == "custom_emoji":
                    has_custom_emoji_entities = True
                    break
            except Exception:
                continue

    # Сопоставление offset'ов entities (UTF-16) нужно только при custom_emoji.
    original_u = ""
    if has_custom_emoji_entities:
        original_u = "".join(chr(u) for u in _utf16_units(original))

    lines = [ln.strip() for ln in original.splitlines() if ln.strip()]
    rows: List[List[dict]] = []
    popups: List[str] = []

    search_pos_u = 0  # глобальный указатель по original_u

    def parse_one(token: str, token_start_u: int, line_no: int) -> dict:
        tok = token.strip()

        style = None
        prefix_units = 0

        # цвет для КАЖДОЙ кнопки отдельно (фикс бага)
        mcol = re.match(r"^(#r|#g|#b)(\s+)(.*)$", tok, flags=re.I | re.S)
        if mcol:
            col = (mcol.group(1) or "").lower()
            spaces = mcol.group(2) or " "
            rest = (mcol.group(3) or "").strip()
            prefix_units = _utf16_len(mcol.group(1) + spaces)
            tok = rest
            if col == "#r":
                style = "danger"
            elif col == "#g":
                style = "success"
            elif col == "#b":
                style = "primary"

        # name/value
        if " - " not in tok:
            raise _button_syntax_error(
                line_no,
                "format",
                "Используйте формат «Название - ссылка», «Название - popup: текст», «Название - rules», «Название - del» или «Название - cmd: имя_команды»."
            )

        name_raw, value = tok.split(" - ", 1)

        name_start_u = 0
        name_end_u = 0
        if has_custom_emoji_entities:
            # offsets для имени (в исходном сообщении)
            name_raw_start_u = token_start_u + prefix_units
            name_raw_end_u = name_raw_start_u + _utf16_len(name_raw)

            # strip для имени
            name_lead = name_raw[:len(name_raw) - len(name_raw.lstrip())]
            name_trail = name_raw[len(name_raw.rstrip()):]
            lead_u = _utf16_len(name_lead)
            trail_u = _utf16_len(name_trail)

            name_start_u = name_raw_start_u + lead_u
            name_end_u = name_raw_end_u - trail_u

        name = name_raw.strip()
        value = (value or "").strip()

        # 1) сначала наш кастом <emoji id='...'>
        name, icon_eid = _extract_button_icon_custom_emoji_id(name)

        # 2) если нет кастома — пробуем entity custom_emoji в начале названия
        if not icon_eid and has_custom_emoji_entities and entities:
            found = _find_custom_emoji_entity_at_offset(entities, name_start_u)
            if found and name_end_u > name_start_u:
                ln_u, ce_id = found
                new_name = _remove_utf16_range(name, 0, ln_u).strip()
                name = (new_name if new_name else " ")
                icon_eid = ce_id

        if not name.strip() and not icon_eid:
            raise _button_syntax_error(line_no, "format", "У кнопки отсутствует название.")

        if not name.strip():
            name = " "

        if not value:
            raise _button_syntax_error(line_no, "format", "После « - » нужно указать ссылку, popup, rules, del или cmd: имя_команды.")

        vlow = (value or "").lower()
        if vlow == "rules":
            return {"type": "rules", "text": name, "style": style, "icon_emoji_id": icon_eid}
        if vlow == "del":
            return {"type": "del", "text": name, "style": style, "icon_emoji_id": icon_eid}

        if vlow.startswith("cmd:"):
            cmd_name = value[len("cmd:"):].strip()
            if not cmd_name:
                raise _button_syntax_error(line_no, "format", "После «cmd:» укажите имя команды.")
            return {"type": "cmd", "text": name, "style": style, "cmd_name": cmd_name, "icon_emoji_id": icon_eid}

        if vlow.startswith("popup:"):
            popup_text = value[len("popup:"):].strip()
            if not popup_text:
                raise _button_syntax_error(line_no, "format", "Для popup укажите текст после «popup:».")
            popups.append(popup_text)
            idx = len(popups) - 1
            return {"type": "popup", "text": name, "style": style, "popup_index": idx, "icon_emoji_id": icon_eid}

        # url
        url = _normalize_url(value)
        if not _is_supported_button_url(url):
            raise _button_syntax_error(line_no, "url", "Поддерживаются http(s) и tg:// ссылки без пробелов.")
        return {"type": "url", "text": name, "style": style, "url": url, "icon_emoji_id": icon_eid}

    for line_no, ln in enumerate(lines, start=1):
        line_start_u = None
        if has_custom_emoji_entities:
            ln_u = "".join(chr(u) for u in _utf16_units(ln))
            line_start_u = original_u.find(ln_u, search_pos_u)
            if line_start_u == -1:
                line_start_u = None
            else:
                search_pos_u = line_start_u + len(ln_u)

        parts = [p.strip() for p in ln.split("&") if p.strip()]
        if not parts:
            continue
        if len(parts) > MAX_PER_ROW:
            raise _button_syntax_error(line_no, "format", f"В одном ряду можно использовать не больше {MAX_PER_ROW} кнопок.")

        row: List[dict] = []
        line_seek_u = line_start_u if line_start_u is not None else None

        for p in parts:
            tok = p.strip()
            token_start_u = 0
            if has_custom_emoji_entities:
                tok_u = "".join(chr(u) for u in _utf16_units(tok))

                if line_seek_u is not None:
                    token_start_u = original_u.find(tok_u, line_seek_u)
                    if token_start_u == -1:
                        token_start_u = line_seek_u
                    else:
                        line_seek_u = token_start_u + len(tok_u)

            row.append(parse_one(tok, token_start_u, line_no))

        rows.append(row)
        if len(rows) >= MAX_ROWS:
            remaining_rows = lines[line_no:]
            if any(item.strip() for item in remaining_rows):
                raise _button_syntax_error(line_no + 1, "other", f"Допустимо не больше {MAX_ROWS} рядов кнопок.")

    # total limit
    flat = sum(len(r) for r in rows)
    if flat > MAX_TOTAL_BTNS:
        raise _button_syntax_error(0, "other", f"Допустимо не больше {MAX_TOTAL_BTNS} кнопок в одном наборе.")

    return rows, popups


def build_inline_keyboard_for_payload(section_name: str, chat_id: int, rows: List[List[dict]], popups: List[str], viewer_user_id: int) -> Optional[InlineKeyboardMarkup]:
    """
    section_name: welcome/farewell/rules — чтобы callback различать
    viewer_user_id: кто имеет право нажимать rules/del/popup
    """
    if not rows:
        return None

    kb = InlineKeyboardMarkup(row_width=MAX_PER_ROW)
    for r in rows:
        btns = []
        for b in (r or [])[:MAX_PER_ROW]:
            b = _sanitize_button_for_payload(b, popups)
            if not b:
                continue

            text = b.get("text") or " "
            btn = None

            if b["type"] == "url":
                btn = InlineKeyboardButton(text, url=b.get("url") or "")
            elif b["type"] == "popup":
                idx = int(b.get("popup_index", 0))
                btn = InlineKeyboardButton(text, callback_data=f"p:{section_name}:{chat_id}:{viewer_user_id}:{idx}")
            elif b["type"] == "rules":
                btn = InlineKeyboardButton(text, callback_data=f"rules:{chat_id}:{viewer_user_id}")
            elif b["type"] == "del":
                btn = InlineKeyboardButton(text, callback_data=f"del:{chat_id}:{viewer_user_id}")
            elif b["type"] == "cmd":
                cmd_n = (b.get("cmd_name") or "").strip()
                if cmd_n:
                    btn = InlineKeyboardButton(text, callback_data=f"cn:{chat_id}:{viewer_user_id}:{cmd_n}"[:64])

            if not btn:
                continue

            # цвет
            st = b.get("style")
            if st:
                try:
                    btn.style = st
                except Exception:
                    pass

            # премиум-эмодзи как icon_custom_emoji_id (если задан)
            eid = b.get("icon_emoji_id")
            if eid:
                try:
                    btn.icon_custom_emoji_id = str(eid)
                except Exception:
                    pass

            btns.append(btn)

        if btns:
            kb.row(*btns)

    return kb


# ------------------------------------------------------------
# UI helpers: одинаковые для welcome/farewell/rules
# ------------------------------------------------------------

@lru_cache(maxsize=None)
def _section_title(sec: str) -> str:
    return {
        "welcome": "приветствия",
        "farewell": "прощания",
        "rules": "правил",
        "first_comment": "первого комментария",
    }.get(sec, sec)


def _render_section_preview(chat_id: int, sec: str) -> str:
    st = get_chat_settings(chat_id)
    sc = st.get(sec) or _default_section(False)

    enabled = bool(sc.get("enabled"))
    has_text = bool((sc.get("text_custom") or "").strip())
    has_media = bool(sc.get("media"))
    has_buttons = bool((sc.get("buttons") or {}).get("rows"))

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'

    status = "<code>включено</code>" if enabled else "<code>выключено</code>"
    text_flag = "<code>есть</code>" if has_text else "<code>нет</code>"
    media_flag = "<code>есть</code>" if has_media else "<code>нет</code>"
    buttons_flag = "<code>есть</code>" if has_buttons else "<code>нет</code>"
    src = (sc.get("source") or "plain").upper()

    section_desc = {
        "welcome": "Отправляет приветственное сообщение, когда пользователь входит в группу.",
        "farewell": "Отправляет прощальное сообщение, когда пользователь выходит из группы.",
        "rules": "Показывает правила группы по кнопке или команде.",
        "first_comment": (
            "Автоматически оставляет первый комментарий к новым постам в привязанном канале, "
            "как только пост появляется в группе."
        ),
    }.get(sec, "")

    desc_block = f"{_html.escape(section_desc)}\n\n" if section_desc else ""

    return (
        f"{emoji_settings} <b>Настройки {_section_title(sec)}</b>\n\n"
        f"{desc_block}"
        f"<b>Статус:</b> {status}\n"
        f"<b>Текст:</b> {text_flag}\n"
        f"<b>Медиа:</b> {media_flag}\n"
        f"<b>Кнопки:</b> {buttons_flag}\n"
        f"<b>Источник:</b> <code>{_html.escape(src)}</code>"
    )


def _render_cleanup_main(chat_id: int) -> str:
    cl = _cleanup_get(chat_id)
    cmds = cl.get("commands") or {}
    sysd = cl.get("system") or {}

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'

    enabled_cmds = [s for s in CLEANUP_CMD_SIGNS if cmds.get(s)]
    enabled_cmds_txt = " ".join(enabled_cmds) if enabled_cmds else "нет"

    enabled_sys = [ct for ct in CLEANUP_SYSTEM_TYPES_ORDER if sysd.get(ct)]
    enabled_sys_txt = str(len(enabled_sys)) if enabled_sys else "нет"

    return (
        f"{emoji_settings} <b>Удаление сообщений</b>\n\n"
        f"<b>Команды:</b> <code>{_html.escape(enabled_cmds_txt)}</code>\n"
        f"<b>Системные сообщения:</b> <code>{_html.escape(enabled_sys_txt)}</code>"
    )



def _build_cleanup_main_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    kb.add(InlineKeyboardButton("Команды", callback_data=f"st_cleanup_cmds:{chat_id}"))
    kb.add(InlineKeyboardButton("Системные сообщения", callback_data=f"st_cleanup_sys:{chat_id}"))

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:modules")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)
    return kb


def _render_cleanup_commands(chat_id: int) -> str:
    cl = _cleanup_get(chat_id)
    cmds = cl.get("commands") or {}
    enabled = [s for s in CLEANUP_CMD_SIGNS if cmds.get(s)]
    enabled_txt = " ".join(enabled) if enabled else "нет"

    emoji = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">🧹</tg-emoji>'
    return (
        f"{emoji} <b>Удаление команд</b>\n\n"
        "Удаляет сообщения, которые начинаются с выбранного знака.\n"
        f"\n<b>Включены:</b> <code>{_html.escape(enabled_txt)}</code>"
    )


def _btn_style_pair(is_enabled: bool) -> tuple[str, str]:
    # включено -> ON зелёная, OFF красная
    # выключено -> ON красная, OFF зелёная
    return ("success", "danger") if is_enabled else ("danger", "success")


def _build_cleanup_commands_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    cl = _cleanup_get(chat_id)
    cmds = cl.get("commands") or {}

    kb = InlineKeyboardMarkup(row_width=3)
    inv = "\u2063"  # “пустой символ”, чтобы была видна только иконка

    for sign in CLEANUP_CMD_SIGNS:
        is_on = bool(cmds.get(sign))
        on_style, off_style = _btn_style_pair(is_on)

        lbl = InlineKeyboardButton(sign, callback_data=f"st_cleanup_cmdnoop:{chat_id}:{sign}")
        try:
            lbl.style = "primary"
        except Exception:
            pass

        b_on = InlineKeyboardButton(inv, callback_data=f"st_cleanup_cmdset:{chat_id}:{sign}:1")
        b_off = InlineKeyboardButton(inv, callback_data=f"st_cleanup_cmdset:{chat_id}:{sign}:0")

        try:
            b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
            b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
        except Exception:
            pass

        try:
            b_on.style = on_style
            b_off.style = off_style
        except Exception:
            pass

        kb.row(lbl, b_on, b_off)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:cleanup")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _render_cleanup_system(chat_id: int) -> str:
    cl = _cleanup_get(chat_id)
    sysd = cl.get("system") or {}
    enabled = [ct for ct in CLEANUP_SYSTEM_TYPES_ORDER if sysd.get(ct)]

    emoji = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">🧽</tg-emoji>'
    return (
        f"{emoji} <b>Удаление системных сообщений</b>\n\n"
        "Удаляет выбранные системные сообщения.\n"
        f"\n<b>Включено:</b> <code>{len(enabled)}</code>"
    )


def _build_cleanup_system_keyboard(chat_id: int, selected_idx: Optional[int] = None) -> InlineKeyboardMarkup:
    cl = _cleanup_get(chat_id)
    sysd = cl.get("system") or {}

    kb = InlineKeyboardMarkup(row_width=2)
    inv = "\u2063"  # “пустой символ”, чтобы была видна только иконка

    for idx, ct in enumerate(CLEANUP_SYSTEM_TYPES_ORDER):
        label = CLEANUP_SYSTEM_LABELS.get(ct, ct)

        is_selected = (selected_idx == idx)
        title = f"»{label}«" if is_selected else label

                # 1) строка с названием типа
        btn_type = InlineKeyboardButton(title[:48], callback_data=f"st_cleanup_syspick:{chat_id}:{idx}")

        # ✅ без цвета по умолчанию, primary только для выбранного
        if is_selected:
            try:
                btn_type.style = "primary"
            except Exception:
                pass

        kb.row(btn_type)
        
        # 2) если выбран — добавляем строку ВКЛ/ВЫКЛ под ним
        if is_selected:
            is_on = bool(sysd.get(ct))
            on_style, off_style = _btn_style_pair(is_on)

            b_on = InlineKeyboardButton(inv, callback_data=f"st_cleanup_sysset:{chat_id}:{idx}:1")
            b_off = InlineKeyboardButton(inv, callback_data=f"st_cleanup_sysset:{chat_id}:{idx}:0")

            try:
                b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
            except Exception:
                pass

            try:
                b_on.style = on_style
                b_off.style = off_style
            except Exception:
                pass

            kb.row(b_on, b_off)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:cleanup")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _warn_type_label(ptype: str) -> str:
    return {
        "mute": "Ограничение",
        "ban": "Блокировка",
        "kick": "Исключение",
    }.get((ptype or "").lower(), "Ограничение")


def _render_warn_settings(chat_id: int, page: str = "main") -> str:
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}

    enabled = bool(settings.get("warn_enabled", True))
    warn_limit = int(settings.get("warn_limit") or 3)
    wp = settings.get("warn_punish") or {}
    ptype = (wp.get("type") or "mute").lower()
    duration = wp.get("duration")

    type_label = _warn_type_label(ptype)
    dur_label = "Не используется" if ptype == "kick" else _mod_duration_text(int(duration or 0))

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'

    status = f"{emoji_ok} Включено" if enabled else f"{emoji_x} Выключено"
    if page == "punish":
        return (
            f"{emoji_settings} <b>Наказание за предупреждения</b>\n\n"
            f"<b>Текущий тип:</b> <code>{_html.escape(type_label)}</code>\n"
            f"<b>Текущая длительность:</b> <code>{_html.escape(dur_label)}</code>\n\n"
            "Выберите тип наказания и длительность ниже:"
        )

    return (
        f"{emoji_settings} <b>Настройки предупреждений</b>\n\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Макс. предупреждений:</b> <code>{warn_limit}</code>\n"
        f"<b>Наказание за максимум:</b> <code>{_html.escape(type_label)}</code>\n"
        f"<b>Длительность наказания:</b> <code>{_html.escape(dur_label)}</code>\n\n"
        "Ниже можно быстро задать лимит от 2 до 10."
    )


def _build_warn_settings_keyboard(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}

    enabled = bool(settings.get("warn_enabled", True))
    warn_limit = int(settings.get("warn_limit") or 3)
    wp = settings.get("warn_punish") or {}
    ptype = (wp.get("type") or "mute").lower()
    duration = int(wp.get("duration") or 24 * 60 * 60)

    kb = InlineKeyboardMarkup(row_width=5)

    if page == "punish":
        btn_mute = InlineKeyboardButton("Ограничение", callback_data=f"st_warn_ptype:{chat_id}:mute")
        btn_ban = InlineKeyboardButton("Блокировка", callback_data=f"st_warn_ptype:{chat_id}:ban")
        btn_kick = InlineKeyboardButton("Исключение", callback_data=f"st_warn_ptype:{chat_id}:kick")
        for btn, key in ((btn_mute, "mute"), (btn_ban, "ban"), (btn_kick, "kick")):
            try:
                btn.style = "primary" if ptype == key else "secondary"
            except Exception:
                pass
        kb.row(btn_mute, btn_ban, btn_kick)

        if ptype in ("mute", "ban"):
            presets = [
                (60 * 60, "1ч"),
                (6 * 60 * 60, "6ч"),
                (12 * 60 * 60, "12ч"),
                (24 * 60 * 60, "1д"),
                (3 * 24 * 60 * 60, "3д"),
                (7 * 24 * 60 * 60, "7д"),
                (30 * 24 * 60 * 60, "30д"),
            ]
            row = []
            for sec, label in presets:
                b = InlineKeyboardButton(label, callback_data=f"st_warn_dur:{chat_id}:{sec}")
                try:
                    b.style = "primary" if duration == sec else "secondary"
                except Exception:
                    pass
                row.append(b)

            for i in range(0, len(row), 4):
                kb.row(*row[i:i + 4])

        btn_back_warn = InlineKeyboardButton("Назад", callback_data=f"st_warn_page:{chat_id}:main")
        try:
            btn_back_warn.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            btn_back_warn.style = "primary"
        except Exception:
            pass
        kb.add(btn_back_warn)
        return kb

    btn_status = InlineKeyboardButton("Статус", callback_data=f"st_warn_toggle:{chat_id}")
    try:
        btn_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(btn_status)

    btn_punish = InlineKeyboardButton("Наказание", callback_data=f"st_warn_page:{chat_id}:punish")
    try:
        btn_punish.style = "primary"
    except Exception:
        pass
    kb.add(btn_punish)

    number_buttons: list[InlineKeyboardButton] = []
    for n in range(2, 11):
        btn = InlineKeyboardButton(str(n), callback_data=f"st_warn_setlimit:{chat_id}:{n}")
        try:
            btn.style = "primary" if warn_limit == n else "secondary"
        except Exception:
            pass
        number_buttons.append(btn)

    for i in range(0, len(number_buttons), 5):
        kb.row(*number_buttons[i:i + 5])

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:modules")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


ANTIFLOOD_TIME_PRESETS = (3, 4, 5, 6, 7, 8, 9, 10, 15, 20)
ANTIFLOOD_MESSAGES_PRESETS = (3, 4, 5, 6, 7, 8, 9, 10, 15, 20)
ANTIFLOOD_DURATION_PRESETS = (
    (60 * 10, "10м"),
    (60 * 30, "30м"),
    (60 * 60, "1ч"),
    (6 * 60 * 60, "6ч"),
    (12 * 60 * 60, "12ч"),
    (24 * 60 * 60, "1д"),
    (3 * 24 * 60 * 60, "3д"),
    (7 * 24 * 60 * 60, "7д"),
)


def _antiflood_type_label(ptype: str) -> str:
    return {
        "mute": "Ограничение",
        "ban": "Блокировка",
        "kick": "Исключение",
        "warn": "Предупреждение",
    }.get((ptype or "").lower(), "Ограничение")


def _antiflood_get_settings(chat_id: int) -> dict:
    settings = (_mod_get_chat(chat_id).get("settings") or {})
    af = settings.get("antiflood") or {}
    return {
        "enabled": bool(af.get("enabled", False)),
        "delete_messages": bool(af.get("delete_messages", False)),
        "period": int(af.get("period") or 10),
        "messages": int(af.get("messages") or 6),
        "punish": af.get("punish") or {"type": "mute", "duration": 30 * 60, "reason": ""},
    }


def _render_antiflood_settings_local(chat_id: int, page: str = "main") -> str:
    af = _antiflood_get_settings(chat_id)
    enabled = bool(af.get("enabled"))
    delete_messages = bool(af.get("delete_messages"))
    period = int(af.get("period") or 10)
    messages = int(af.get("messages") or 6)
    punish = af.get("punish") or {}
    ptype = (punish.get("type") or "mute").lower()
    duration = punish.get("duration")

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    emoji_ok = f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'

    status_line = f"{emoji_ok} Включён" if enabled else f"{emoji_x} Выключен"
    delete_line = f"{emoji_ok} Включено" if delete_messages else f"{emoji_x} Выключено"
    ptype_line = _antiflood_type_label(ptype)
    duration_line = "нет" if ptype == "kick" else _mod_duration_text(int(duration or 0))

    hint = ""
    if page == "time":
        hint = "\n\n<i>Установите временное окно (в секундах), за которое считаются сообщения.</i>"
    elif page == "messages":
        hint = "\n\n<i>Установите лимит сообщений в выбранном временном окне.</i>"
    elif page == "punish":
        hint = "\n\n<i>Выберите наказание за превышение лимита антифлуда.</i>"
    elif page == "duration":
        if ptype == "kick":
            hint = "\n\nДля выбранного типа наказания длительность не используется."
        else:
            hint = "\n\n<i>Установите длительность наказания.</i>"

    return (
        f"{emoji_settings} <b>Настройки антифлуда</b>\n\n"
        "Автоматически наказывает пользователя, если он отправит определённое количество сообщений за заданный период.\n\n"
        f"<b>Статус:</b> {status_line}\n"
        f"<b>Удаление сообщений:</b> {delete_line}\n"
        f"<b>Время:</b> <code>{period}</code> сек\n"
        f"<b>Сообщения:</b> <code>{messages}</code>\n"
        f"<b>Наказание</b> <code>{_html.escape(ptype_line)}</code>\n"
        f"<b>Длительность:</b> <code>{_html.escape(duration_line)}</code>"
        f"{hint}"
    )


def _build_antiflood_settings_keyboard_local(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    af = _antiflood_get_settings(chat_id)
    enabled = bool(af.get("enabled"))
    delete_messages = bool(af.get("delete_messages"))
    period = int(af.get("period") or 10)
    messages = int(af.get("messages") or 6)
    punish = af.get("punish") or {}
    ptype = (punish.get("type") or "mute").lower()
    duration = int(punish.get("duration") or 30 * 60)

    kb = InlineKeyboardMarkup(row_width=3)

    b_status = InlineKeyboardButton("Статус", callback_data=f"stf:toggle:{chat_id}")
    try:
        b_status.icon_custom_emoji_id = str(EMOJI_UNPUNISH_ID if enabled else EMOJI_ROLE_SETTINGS_CANCEL_ID)
        b_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(b_status)

    b_delete = InlineKeyboardButton("Удаление сообщений", callback_data=f"stf:deltoggle:{chat_id}")
    try:
        b_delete.style = "success" if delete_messages else "danger"
    except Exception:
        pass
    kb.add(b_delete)

    b_messages_text = "»Сообщения«" if page == "messages" else "Сообщения"
    b_time_text = "»Время«" if page == "time" else "Время"
    b_punish_text = "»Наказание«" if page == "punish" else "Наказание"
    b_duration_text = "»Длительность«" if page == "duration" else "Длительность"

    b_messages = InlineKeyboardButton(b_messages_text, callback_data=f"stf:page:{chat_id}:messages")
    b_time = InlineKeyboardButton(b_time_text, callback_data=f"stf:page:{chat_id}:time")
    b_punish = InlineKeyboardButton(b_punish_text, callback_data=f"stf:page:{chat_id}:punish")
    b_duration = InlineKeyboardButton(b_duration_text, callback_data=f"stf:page:{chat_id}:duration")

    try:
        if page == "messages":
            b_messages.style = "primary"
        if page == "time":
            b_time.style = "primary"
        if page == "punish":
            b_punish.style = "primary"
        if page == "duration":
            b_duration.style = "primary"
    except Exception:
        pass

    kb.row(b_messages, b_time)

    if page == "time":
        row: list[InlineKeyboardButton] = []
        for sec in ANTIFLOOD_TIME_PRESETS:
            b = InlineKeyboardButton(str(sec), callback_data=f"stf:time:{chat_id}:{sec}")
            try:
                if period == sec:
                    b.style = "primary"
            except Exception:
                pass
            row.append(b)
        for i in range(0, len(row), 5):
            kb.row(*row[i:i + 5])

    if page == "messages":
        row = []
        for count in ANTIFLOOD_MESSAGES_PRESETS:
            b = InlineKeyboardButton(str(count), callback_data=f"stf:msgs:{chat_id}:{count}")
            try:
                if messages == count:
                    b.style = "primary"
            except Exception:
                pass
            row.append(b)
        for i in range(0, len(row), 5):
            kb.row(*row[i:i + 5])

    kb.row(b_punish, b_duration)

    if page == "punish":
        b_mute = InlineKeyboardButton("Ограничение", callback_data=f"stf:ptype:{chat_id}:mute")
        b_ban = InlineKeyboardButton("Блокировка", callback_data=f"stf:ptype:{chat_id}:ban")
        b_kick = InlineKeyboardButton("Исключение", callback_data=f"stf:ptype:{chat_id}:kick")
        b_warn = InlineKeyboardButton("Предупреждение", callback_data=f"stf:ptype:{chat_id}:warn")
        for btn, p_key in ((b_mute, "mute"), (b_ban, "ban"), (b_kick, "kick"), (b_warn, "warn")):
            try:
                if ptype == p_key:
                    btn.style = "primary"
            except Exception:
                pass
        kb.row(b_mute, b_ban)
        kb.row(b_kick, b_warn)

    if page == "duration":
        b_set = InlineKeyboardButton("Установить длительность", callback_data=f"stf:dur_prompt:{chat_id}")
        try:
            b_set.style = "primary"
        except Exception:
            pass
        kb.add(b_set)

    b_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:filters")
    try:
        b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back.style = "primary"
    except Exception:
        pass
    kb.add(b_back)

    return kb


# ============================================================
# Анти-рейд settings UI
# ============================================================

ANTIRAID_COUNT_PRESETS = (5, 10, 15, 20, 25)
ANTIRAID_TIME_PRESETS_SECONDS = (3, 5, 6, 10, 15)
ANTIRAID_MIN_COUNT = 5
ANTIRAID_MAX_COUNT = 25
ANTIRAID_MIN_PERIOD = 3
ANTIRAID_MAX_PERIOD = 60
ANTIRAID_REPUNISH_COOLDOWN = 60  # seconds before same user can be re-punished
ANTIRAID_DURATION_PRESETS = (
    (60 * 10, "10м"),
    (60 * 30, "30м"),
    (60 * 60, "1ч"),
    (6 * 60 * 60, "6ч"),
    (12 * 60 * 60, "12ч"),
    (24 * 60 * 60, "1д"),
    (3 * 24 * 60 * 60, "3д"),
    (7 * 24 * 60 * 60, "7д"),
)


def _antiraid_type_label(ptype: str) -> str:
    return {
        "mute": "Ограничение",
        "ban": "Блокировка",
        "kick": "Исключение",
    }.get((ptype or "").lower(), "Ограничение")


def _antiraid_get_settings(chat_id: int) -> dict:
    settings = (_mod_get_chat(chat_id).get("settings") or {})
    ar = settings.get("antiraid") or {}
    return {
        "enabled": bool(ar.get("enabled", False)),
        "count": int(ar.get("count") or 10),
        "period": int(ar.get("period") or 10),
        "punish": ar.get("punish") or {"type": "mute", "duration": 30 * 60, "reason": ""},
    }


def _render_antiraid_settings_local(chat_id: int, page: str = "main") -> str:
    ar = _antiraid_get_settings(chat_id)
    enabled = bool(ar.get("enabled"))
    count = int(ar.get("count") or 10)
    period = int(ar.get("period") or 10)
    punish = ar.get("punish") or {}
    ptype = (punish.get("type") or "mute").lower()
    duration = punish.get("duration")

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    emoji_ok = f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'

    status_line = f"{emoji_ok} Включён" if enabled else f"{emoji_x} Выключен"
    ptype_line = _antiraid_type_label(ptype)
    duration_line = "нет" if ptype == "kick" else _mod_duration_text(int(duration or 0))

    hint = ""
    if page == "count":
        hint = "\n\n<i>Установите количество участников, которые должны войти за период, чтобы сработал рейд.</i>"
    elif page == "time":
        hint = "\n\n<i>Установите временное окно (в секундах), за которое отслеживаются входы участников.</i>"
    elif page == "punish":
        hint = "\n\n<i>Выберите наказание для участников во время рейда.</i>"
    elif page == "duration":
        if ptype == "kick":
            hint = "\n\nДля выбранного типа наказания длительность не используется."
        else:
            hint = "\n\n<i>Установите длительность наказания.</i>"

    return (
        f"{emoji_settings} <b>Настройки анти-рейда</b>\n\n"
        "Автоматически защищает группу при массовом входе участников. "
        "При срабатывании активируется режим рейда на <b>10 минут</b>, "
        "в течение которых все новые входящие участники будут наказаны.\n\n"
        f"<b>Статус:</b> {status_line}\n"
        f"<b>Количество:</b> <code>{count}</code> участников\n"
        f"<b>Время:</b> <code>{period}</code> сек\n"
        f"<b>Наказание:</b> <code>{_html.escape(ptype_line)}</code>\n"
        f"<b>Длительность:</b> <code>{_html.escape(duration_line)}</code>"
        f"{hint}"
    )


def _build_antiraid_settings_keyboard_local(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    ar = _antiraid_get_settings(chat_id)
    enabled = bool(ar.get("enabled"))
    count = int(ar.get("count") or 10)
    period = int(ar.get("period") or 10)
    punish = ar.get("punish") or {}
    ptype = (punish.get("type") or "mute").lower()
    duration = int(punish.get("duration") or 30 * 60)

    kb = InlineKeyboardMarkup(row_width=3)

    b_status = InlineKeyboardButton("Статус", callback_data=f"star:toggle:{chat_id}")
    try:
        b_status.icon_custom_emoji_id = str(EMOJI_UNPUNISH_ID if enabled else EMOJI_ROLE_SETTINGS_CANCEL_ID)
        b_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(b_status)

    b_count_text = "»Количество«" if page == "count" else "Количество"
    b_time_text = "»Время«" if page == "time" else "Время"
    b_punish_text = "»Наказание«" if page == "punish" else "Наказание"
    b_duration_text = "»Длительность«" if page == "duration" else "Длительность"

    b_count = InlineKeyboardButton(b_count_text, callback_data=f"star:page:{chat_id}:count")
    b_time = InlineKeyboardButton(b_time_text, callback_data=f"star:page:{chat_id}:time")
    b_punish = InlineKeyboardButton(b_punish_text, callback_data=f"star:page:{chat_id}:punish")
    b_duration = InlineKeyboardButton(b_duration_text, callback_data=f"star:page:{chat_id}:duration")

    try:
        if page == "count":
            b_count.style = "primary"
        if page == "time":
            b_time.style = "primary"
        if page == "punish":
            b_punish.style = "primary"
        if page == "duration":
            b_duration.style = "primary"
    except Exception:
        pass

    kb.row(b_count, b_time)

    if page == "count":
        row: list[InlineKeyboardButton] = []
        for cnt in ANTIRAID_COUNT_PRESETS:
            b = InlineKeyboardButton(str(cnt), callback_data=f"star:count:{chat_id}:{cnt}")
            try:
                if count == cnt:
                    b.style = "primary"
            except Exception:
                pass
            row.append(b)
        for i in range(0, len(row), 5):
            kb.row(*row[i:i + 5])

    if page == "time":
        row = []
        for sec in ANTIRAID_TIME_PRESETS_SECONDS:
            b = InlineKeyboardButton(str(sec), callback_data=f"star:time:{chat_id}:{sec}")
            try:
                if period == sec:
                    b.style = "primary"
            except Exception:
                pass
            row.append(b)
        for i in range(0, len(row), 5):
            kb.row(*row[i:i + 5])

    kb.row(b_punish, b_duration)

    if page == "punish":
        b_mute = InlineKeyboardButton("Ограничение", callback_data=f"star:ptype:{chat_id}:mute")
        b_ban = InlineKeyboardButton("Блокировка", callback_data=f"star:ptype:{chat_id}:ban")
        b_kick = InlineKeyboardButton("Исключение", callback_data=f"star:ptype:{chat_id}:kick")
        for btn, p_key in ((b_mute, "mute"), (b_ban, "ban"), (b_kick, "kick")):
            try:
                if ptype == p_key:
                    btn.style = "primary"
            except Exception:
                pass
        kb.row(b_mute, b_ban, b_kick)

    if page == "duration":
        b_set = InlineKeyboardButton("Установить длительность", callback_data=f"star:dur_prompt:{chat_id}")
        try:
            b_set.style = "primary"
        except Exception:
            pass
        kb.add(b_set)

    b_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:modules")
    try:
        b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back.style = "primary"
    except Exception:
        pass
    kb.add(b_back)

    return kb


def _build_settings_main_keyboard(chat_id: int, viewer_user: types.User | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    btn_welcome = InlineKeyboardButton("Приветствие", callback_data=f"st_main:{chat_id}:welcome")
    try:
        btn_welcome.icon_custom_emoji_id = "5472055112702629499"
    except Exception:
        pass

    btn_farewell = InlineKeyboardButton("Прощание", callback_data=f"st_main:{chat_id}:farewell")
    try:
        btn_farewell.icon_custom_emoji_id = "5370867268051806190"
    except Exception:
        pass

    btn_rules = InlineKeyboardButton("Правила", callback_data=f"st_main:{chat_id}:rules")
    try:
        btn_rules.icon_custom_emoji_id = "5226512880362332956"
    except Exception:
        pass

    btn_modules = InlineKeyboardButton("Модули", callback_data=f"st_main:{chat_id}:modules")
    try:
        btn_modules.icon_custom_emoji_id = "5433653135799228968"
    except Exception:
        pass

    btn_filters = InlineKeyboardButton("Фильтры", callback_data=f"st_main:{chat_id}:filters")
    try:
        btn_filters.icon_custom_emoji_id = "5431736674147114227"
    except Exception:
        pass

    can_manage_roles = bool(viewer_user and _user_can_edit_now(viewer_user, chat_id))
    btn_roles = InlineKeyboardButton("Управление должностями", callback_data=f"st_main:{chat_id}:roles")
    try:
        btn_roles.icon_custom_emoji_id = "5429337466760864755"
    except Exception:
        pass

    kb.add(btn_welcome, btn_farewell)
    kb.add(btn_rules)
    kb.add(btn_filters, btn_modules)
    if can_manage_roles:
        kb.add(btn_roles)

    btn_logging = InlineKeyboardButton("Логирование", callback_data=f"st_main:{chat_id}:logging")
    try:
        btn_logging.icon_custom_emoji_id = str(EMOJI_LOG_ID)
    except Exception:
        pass
    kb.add(btn_logging)

    btn_close = InlineKeyboardButton("Закрыть", callback_data=f"st_close:{chat_id}")
    try:
        btn_close.icon_custom_emoji_id = str(PREMIUM_CLOSE_EMOJI_ID)
    except Exception:
        pass
    kb.add(btn_close)

    return kb


def _render_modules_text(chat_id: int) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    return (
        f"{emoji_settings} <b>Модули</b>\n\n"
        "Управляйте расширенными функциями бота: предупреждениями, удалением сообщений, "
        "первым комментарием к постам канала, защитой от рейдов и пользовательскими командами.\n\n"
        "<b>Выберите раздел для настройки:</b>"
    )


def _build_modules_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    btn_warns = InlineKeyboardButton("Предупреждения", callback_data=f"stw:open:{chat_id}")
    try:
        btn_warns.icon_custom_emoji_id = "5467928559664242360"
    except Exception:
        pass
    kb.add(btn_warns)

    btn_cleanup = InlineKeyboardButton("Удаление сообщений", callback_data=f"st_main:{chat_id}:cleanup")
    try:
        btn_cleanup.icon_custom_emoji_id = "5229113891081956317"
    except Exception:
        pass
    kb.add(btn_cleanup)

    btn_antiraid = InlineKeyboardButton("Анти-рейд", callback_data=f"star:open:{chat_id}")
    try:
        btn_antiraid.icon_custom_emoji_id = "5318757666800031348"
    except Exception:
        pass
    kb.add(btn_antiraid)

    btn_first_comment = InlineKeyboardButton("Первый комментарий", callback_data=f"st_main:{chat_id}:first_comment")
    try:
        btn_first_comment.icon_custom_emoji_id = "5472055112702629499"
    except Exception:
        pass
    kb.add(btn_first_comment)

    btn_commands = InlineKeyboardButton("Команды", callback_data=f"st_main:{chat_id}:commands")
    try:
        btn_commands.icon_custom_emoji_id = "5377844313575150051"
    except Exception:
        pass
    kb.add(btn_commands)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_back_main:{chat_id}")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _render_filters_text(chat_id: int) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    return (
        f"{emoji_settings} <b>Фильтры</b>\n\n"
        "Настройте фильтры для автоматической защиты чата от спама, флуда "
        "и запрещённых слов.\n\n"
        "<b>Выберите раздел для настройки:</b>"
    )


def _build_filters_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    btn_antispam = InlineKeyboardButton("Анти-спам", callback_data=f"stas:open:{chat_id}")
    try:
        btn_antispam.icon_custom_emoji_id = "5375129357373165375"
    except Exception:
        pass
    kb.add(btn_antispam)

    btn_antiflood = InlineKeyboardButton("Анти-флуд", callback_data=f"stf:open:{chat_id}")
    try:
        btn_antiflood.icon_custom_emoji_id = "5451732530048802485"
    except Exception:
        pass
    kb.add(btn_antiflood)

    btn_banwords = InlineKeyboardButton("Запрещённые слова", callback_data=f"stbw:open:{chat_id}")
    try:
        btn_banwords.icon_custom_emoji_id = "5370930189322688800"
    except Exception:
        pass
    kb.add(btn_banwords)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_back_main:{chat_id}")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


# ------------------------------------------------------------
# LOG CHANNEL UI
# ------------------------------------------------------------

LOG_CHANNEL_EVENT_LABELS: dict[str, str] = {
    "ban":          "Блокировка",
    "mute":         "Ограничение",
    "kick":         "Исключение",
    "warn":         "Предупреждение",
    "unban":        "Снятие блокировки",
    "unmute":       "Снятие ограничения",
    "unwarn":       "Снятие предупреждения",
    "antiflood":    "Анти-флуд",
    "antispam":     "Анти-спам",
    "antiraid":     "Анти-рейд",
    "settings":     "Изменение настроек",
    "chat_closed":  "Закрытие чата",
    "chat_opened":  "Открытие чата",
    "join":         "Вход участника",
    "leave":        "Выход участника",
    "verify":       "Верификация",
    "role":         "Должности",
    "role_change":  "Повышение/понижение",
    "manual_punish": "Ручные наказания",
}

# Event groups for the UI selection
LOG_CHANNEL_EVENT_GROUPS: list[tuple[str, list[str]]] = [
    ("Выдача наказаний",        ["ban", "mute", "kick", "warn"]),
    ("Снятие наказаний",        ["unban", "unmute", "unwarn"]),
    ("Автоматические наказания", ["antiflood", "antispam", "antiraid"]),
    ("Управление группой",      ["settings", "chat_closed", "chat_opened"]),
    ("Участники",               ["join", "leave"]),
    ("Прочее",                  ["verify", "role", "role_change", "manual_punish"]),
]


def _render_logging_text(chat_id: int) -> str:
    emoji = f'<tg-emoji emoji-id="{EMOJI_LOG_ID}">📋</tg-emoji>'
    lc = get_log_channel(chat_id)
    if not lc:
        status = "<i>Не настроен</i>"
    else:
        title = _html.escape(lc.get("channel_title") or str(lc["channel_id"]))
        events = lc.get("events") or {}
        enabled_groups = sum(
            1 for _, group_events in LOG_CHANNEL_EVENT_GROUPS
            if _log_group_state(events, [ev for ev in group_events if ev in LOG_CHANNEL_ALL_EVENTS])
        )
        total_groups = len(LOG_CHANNEL_EVENT_GROUPS)
        status = (
            f"<b>Лог-канал:</b> {title} [<code>{lc['channel_id']}</code>]\n"
            f"Включённые логи: <b>{enabled_groups}</b> из <b>{total_groups}</b>"
        )
    return (
        f"{emoji} <b>Логирование</b>\n\n"
        f"Бот отправляет логи действий в выбранный канал.\n\n"
        f"{status}"
    )


def _build_logging_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    lc = get_log_channel(chat_id)

    if not lc:
        btn_add = InlineKeyboardButton("Добавить лог-канал", callback_data=f"stlc:add:{chat_id}")
        try:
            btn_add.icon_custom_emoji_id = str(EMOJI_LOG_PM_ID)
        except Exception:
            pass
        kb.add(btn_add)
    else:
        btn_events = InlineKeyboardButton("Настроить события", callback_data=f"stlc:events:{chat_id}")
        try:
            btn_events.icon_custom_emoji_id = str(EMOJI_LOG_ID)
        except Exception:
            pass
        kb.add(btn_events)

        btn_remove = InlineKeyboardButton("Удалить лог-канал", callback_data=f"stlc:remove_confirm:{chat_id}")
        kb.add(btn_remove)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_back_main:{chat_id}")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)
    return kb


def _build_logging_remove_confirm_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    btn_yes = InlineKeyboardButton("Да", callback_data=f"stlc:remove:{chat_id}")
    btn_no = InlineKeyboardButton("Нет", callback_data=f"stlc:open:{chat_id}")
    kb.row(btn_yes, btn_no)
    return kb


def _log_group_state(events: dict, group_events: list[str]) -> bool:
    """Returns True if ALL events in the group are enabled."""
    return all(bool(events.get(ev, True)) for ev in group_events)


def _render_logging_events_text(chat_id: int) -> str:
    emoji = f'<tg-emoji emoji-id="{EMOJI_LOG_ID}">📋</tg-emoji>'
    lc = get_log_channel(chat_id)
    if not lc:
        return f"{emoji} <b>Лог-канал не настроен.</b>"
    events = lc.get("events", {})
    enabled_groups = sum(
        1 for _, group_events in LOG_CHANNEL_EVENT_GROUPS
        if _log_group_state(events, [ev for ev in group_events if ev in LOG_CHANNEL_ALL_EVENTS])
    )
    total_groups = len(LOG_CHANNEL_EVENT_GROUPS)
    return (
        f"{emoji} <b>События лог-канала</b>\n\n"
        f"<b>Включено групп:</b> <b>{enabled_groups}</b> из <b>{total_groups}</b>\n\n"
        "Нажмите на группу, чтобы включить или выключить все её события:"
    )


def _build_logging_events_keyboard(chat_id: int, selected_idx: Optional[int] = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    lc = get_log_channel(chat_id)
    events = (lc.get("events") if lc else None) or {}

    ev_valid = LOG_CHANNEL_ALL_EVENTS  # already a frozenset — O(1) membership
    inv = "\u2063"

    for g_idx, (group_name, group_events) in enumerate(LOG_CHANNEL_EVENT_GROUPS):
        valid_events = [ev for ev in group_events if ev in ev_valid]
        if not valid_events:
            continue

        is_on = _log_group_state(events, valid_events)
        is_selected = (selected_idx == g_idx)
        on_style, off_style = _btn_style_pair(is_on)

        title = f"»{group_name}«" if is_selected else group_name
        btn_group = InlineKeyboardButton(
            title,
            callback_data=f"stlc:evsel:{chat_id}:{g_idx}",
        )
        try:
            if is_selected:
                btn_group.style = "primary"
        except Exception:
            pass

        kb.row(btn_group)

        if is_selected:
            b_on = InlineKeyboardButton(inv, callback_data=f"stlc:evgroup:{chat_id}:{g_idx}:1")
            b_off = InlineKeyboardButton(inv, callback_data=f"stlc:evgroup:{chat_id}:{g_idx}:0")
            try:
                b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
            except Exception:
                pass
            try:
                b_on.style = on_style
                b_off.style = off_style
            except Exception:
                pass
            kb.row(b_on, b_off)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"stlc:open:{chat_id}")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)
    return kb


def _build_section_keyboard(chat_id: int, sec: str) -> InlineKeyboardMarkup:
    st = get_chat_settings(chat_id)
    sc = st.get(sec) or _default_section(False)
    enabled = bool(sc.get("enabled"))

    kb = InlineKeyboardMarkup(row_width=2)

    btn_status = InlineKeyboardButton("Статус", callback_data=f"st_{sec}_toggle:{chat_id}")
    try:
        btn_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(btn_status)

    btn_show = InlineKeyboardButton("Показать текущее", callback_data=f"st_{sec}_show:{chat_id}")
    try:
        btn_show.style = "primary"
    except Exception:
        pass
    kb.add(btn_show)

    btn_text = InlineKeyboardButton("Текст", callback_data=f"st_{sec}_text:{chat_id}")
    btn_text.icon_custom_emoji_id = EMOJI_WELCOME_TEXT_ID

    btn_media = InlineKeyboardButton("Медиа", callback_data=f"st_{sec}_media:{chat_id}")
    btn_media.icon_custom_emoji_id = EMOJI_WELCOME_MEDIA_ID

    kb.add(btn_text, btn_media)

    btn_buttons = InlineKeyboardButton("Кнопки", callback_data=f"st_{sec}_buttons:{chat_id}")
    btn_buttons.icon_custom_emoji_id = EMOJI_WELCOME_BUTTONS_ID
    kb.add(btn_buttons)

    back_cb = f"st_main:{chat_id}:modules" if sec == "first_comment" else f"st_back_main:{chat_id}"
    btn_back = InlineKeyboardButton("Назад", callback_data=back_cb)
    btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    try:
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _only_back_kb(chat_id: int, sec: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:{sec}")
    btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    try:
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)
    return kb



# ------------------------------------------------------------
# /settings
# ------------------------------------------------------------


def _is_channel_sender(m: types.Message) -> bool:
    """
    Возвращает True, если сообщение отправлено от имени любого канала.
    Такие сообщения должны игнорироваться командными обработчиками.
    """
    sender_chat = getattr(m, "sender_chat", None)
    if not sender_chat:
        return False
    return getattr(sender_chat, "type", None) == "channel"


def _is_anonymous_admin(m: types.Message) -> bool:
    """
    Возвращает True, если сообщение отправлено анонимным администратором
    (sender_chat совпадает с текущей группой).
    """
    sender_chat = getattr(m, "sender_chat", None)
    if not sender_chat:
        return False
    chat_id = getattr(m, "chat", None)
    if chat_id and getattr(sender_chat, "id", None) == getattr(chat_id, "id", None):
        return True
    return False


def _find_settings_groups_for_user(user: types.User) -> list[tuple[int, str]]:
    """
    Returns list of (chat_id, title) of approved groups where user has settings rights.
    Uses local role data first to avoid unnecessary Telegram API calls.
    """
    chat_ids = set(get_all_bot_chat_ids())
    # Also include any chats already in memory (in case bot restarted)
    for cid_str in (CHAT_SETTINGS or {}):
        try:
            chat_ids.add(int(cid_str))
        except ValueError:
            pass

    # Fast pre-checks: bot owner and dev users always have access
    user_is_global = is_owner(user) or _is_dev_user(user)
    user_id_str = str(user.id)

    result: list[tuple[int, str]] = []
    for chat_id in chat_ids:
        if not is_group_approved(chat_id):
            continue

        cid_str = str(chat_id)

        # Check permission using local data first to avoid API calls
        if user_is_global:
            has_access = True
        else:
            chat_roles = CHAT_ROLES.get(cid_str, {})
            rec = chat_roles.get(user_id_str, {})
            local_rank = int(rec.get("rank", 0))
            if local_rank > 0:
                # User has a local rank — check settings permission without API
                has_access = bool(get_role_perms(chat_id, local_rank).get(PERM_SETTINGS))
            else:
                # No local rank — need API call to check if user is the chat creator
                try:
                    member = tg_get_chat_member(chat_id, user.id)
                    has_access = member.status == 'creator'
                except Exception:
                    has_access = False

        if not has_access:
            continue

        # Only fetch from Telegram once we know the user has access
        try:
            chat = tg_get_chat(chat_id)
        except Exception:
            continue

        if getattr(chat, "type", None) not in ("group", "supergroup"):
            continue

        title = chat.title or str(chat_id)
        result.append((chat_id, title))

    result.sort(key=lambda x: x[1].lower())
    return result


@bot.message_handler(func=lambda m: match_command(m, 'settings') and bool(m.from_user) and not _is_channel_sender(m))
def cmd_settings(m: types.Message):
    add_stat_message(m)
    add_stat_command('settings')

    # Анонимный админ (sender_chat == группа) — не обрабатываем
    if _is_anonymous_admin(m):
        return

    wait_seconds = cooldown_hit('user', int(m.from_user.id), 'settings', 5)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='settings')

    if m.chat.type == 'private':
        user = m.from_user
        groups = _find_settings_groups_for_user(user)
        emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
        if not groups:
            return bot.reply_to(
                m,
                f"{emoji_settings} <b>Нет доступных групп</b>\n\n"
                "У вас нет права изменения настроек ни в одной из групп.",
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        text = (
            f"{emoji_settings} <b>Настройки групп</b>\n\n"
            "Выберите группу для настройки:"
        )
        kb = InlineKeyboardMarkup()
        for cid, title in groups:
            kb.add(InlineKeyboardButton(
                f"{title}",
                callback_data=f"pm_settings_open:{cid}",
            ))
        return bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)

    if m.chat.type not in ['group', 'supergroup']:
        return

    # Проверка одобрения группы
    if not check_group_approval(m):
        return

    chat_id = m.chat.id
    user = m.from_user

    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        if err:
            return bot.reply_to(m, premium_prefix(err), parse_mode='HTML', disable_web_page_preview=True)
        return

    get_chat_settings(chat_id)

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    text = (
        f"{emoji_settings} <b>Настройки чата</b>\n"
        f"Чат: {m.chat.title or chat_id}\n\n"
        "Выберите раздел для настройки:"
    )

    kb = _build_settings_main_keyboard(chat_id, viewer_user=user)

    try:
        bot.send_message(user.id, text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)
    except Exception:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось отправить интерфейс в ЛС. Напишите боту в ЛС и попробуйте снова."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    bot.reply_to(
        m,
        "<i>Настройки отправлены в ЛС.</i>",
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=_build_open_pm_markup(),
    )


# ------------------------------------------------------------
# Callbacks: settings + section UI
# ------------------------------------------------------------

def _is_warn_settings_callback_data(data: str) -> bool:
    return bool(data) and data.startswith("stw:")


def _is_antiflood_settings_callback_data(data: str) -> bool:
    return bool(data) and data.startswith("stf:")


def _is_antiraid_settings_callback_data(data: str) -> bool:
    return bool(data) and data.startswith("star:")


def _render_warn_settings_local(chat_id: int, page: str = "main") -> str:
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    enabled = bool(settings.get("warn_enabled", True))
    warn_limit = int(settings.get("warn_limit") or 3)
    wp = settings.get("warn_punish") or {}
    ptype = (wp.get("type") or "mute").lower()
    duration = wp.get("duration")

    type_label = _warn_type_label(ptype)
    dur_label = "Не используется" if ptype == "kick" else _mod_duration_text(int(duration or 0))
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    emoji_ok = f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    status_line = f"{emoji_ok} Включён" if enabled else f"{emoji_x} Выключен"

    hint = ""
    if page == "count":
        hint = "\n\n<i>Выберите максимальное количество предупреждений.</i>"
    elif page == "punish":
        hint = "\n\n<i>Выберите наказание, которое будет применяться при достижении максимального количества предупреждений.</i>"
    elif page == "duration":
        if ptype == "kick":
            hint = "\n\nДля наказания «Исключение» длительность не устанавливается."
        else:
            hint = "\n\n<i>Установите время наказания.</i>"

    return (
        f"{emoji_settings} <b>Настройки предупреждений</b>\n\n"
        "Автоматически применяет наказание, когда пользователь достигает лимита предупреждений.\n\n"
        f"<b>Статус:</b> {status_line}\n"
        f"<b>Максимальное количество:</b> <code>{warn_limit}</code>\n"
        f"<b>Наказание:</b> <code>{_html.escape(type_label)}</code>\n"
        f"<b>Длительность:</b> <code>{_html.escape(dur_label)}</code>"
        f"{hint}"
    )


def _build_warn_settings_keyboard_local(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    enabled = bool(settings.get("warn_enabled", True))
    warn_limit = int(settings.get("warn_limit") or 3)
    wp = settings.get("warn_punish") or {}
    ptype = (wp.get("type") or "mute").lower()
    duration = int(wp.get("duration") or 24 * 60 * 60)

    kb = InlineKeyboardMarkup(row_width=3)

    b_status = InlineKeyboardButton("Статус", callback_data=f"stw:toggle:{chat_id}")
    try:
        b_status.icon_custom_emoji_id = str(EMOJI_UNPUNISH_ID if enabled else EMOJI_ROLE_SETTINGS_CANCEL_ID)
        b_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(b_status)

    b_count_title = "»Количество«" if page == "count" else "Количество"
    b_punish_title = "»Наказание«" if page == "punish" else "Наказание"
    b_duration_title = "»Длительность«" if page == "duration" else "Длительность"

    b_count = InlineKeyboardButton(b_count_title, callback_data=f"stw:page:{chat_id}:count")
    b_punish = InlineKeyboardButton(b_punish_title, callback_data=f"stw:page:{chat_id}:punish")
    b_duration = InlineKeyboardButton(b_duration_title, callback_data=f"stw:page:{chat_id}:duration")

    try:
        if page == "count":
            b_count.style = "primary"
        if page == "punish":
            b_punish.style = "primary"
        if page == "duration":
            b_duration.style = "primary"
    except Exception:
        pass

    kb.row(b_count)

    if page == "count":
        nums: list[InlineKeyboardButton] = []
        for n in range(2, 11):
            b = InlineKeyboardButton(str(n), callback_data=f"stw:limit:{chat_id}:{n}")
            try:
                if warn_limit == n:
                    b.style = "primary"
            except Exception:
                pass
            nums.append(b)
        for i in range(0, len(nums), 5):
            kb.row(*nums[i:i + 5])

    kb.row(b_punish, b_duration)

    if page == "punish":
        b_mute = InlineKeyboardButton("Ограничение", callback_data=f"stw:ptype:{chat_id}:mute")
        b_ban = InlineKeyboardButton("Блокировка", callback_data=f"stw:ptype:{chat_id}:ban")
        b_kick = InlineKeyboardButton("Исключение", callback_data=f"stw:ptype:{chat_id}:kick")
        for btn, p_key in ((b_mute, "mute"), (b_ban, "ban"), (b_kick, "kick")):
            try:
                if ptype == p_key:
                    btn.style = "primary"
            except Exception:
                pass
        kb.row(b_mute, b_ban, b_kick)

    if page == "duration" and ptype in ("mute", "ban"):
        b_set = InlineKeyboardButton("Установить время", callback_data=f"stw:dur_prompt:{chat_id}")
        kb.add(b_set)

    b_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:modules")
    try:
        b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back.style = "primary"
    except Exception:
        pass
    kb.add(b_back)
    return kb


def _clone_inline_kb_plain(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    plain = InlineKeyboardMarkup(row_width=5)
    try:
        for row in (kb.keyboard or []):
            new_row: list[InlineKeyboardButton] = []
            for btn in row:
                text = getattr(btn, "text", "") or "-"
                cb = getattr(btn, "callback_data", None)
                url = getattr(btn, "url", None)
                if cb is not None:
                    new_row.append(InlineKeyboardButton(text, callback_data=cb))
                elif url is not None:
                    new_row.append(InlineKeyboardButton(text, url=url))
                else:
                    new_row.append(InlineKeyboardButton(text, callback_data="stw:noop:0"))
            if new_row:
                plain.row(*new_row)
    except Exception:
        pass
    return plain


def _strip_tg_emoji_tags(text: str) -> str:
    try:
        return _re.sub(r"</?tg-emoji[^>]*>", "", text or "")
    except Exception:
        return text or ""


def _safe_answer_cq(query_id: str, text: str | None = None, show_alert: bool = False) -> None:
    """Answer a callback query, silently ignoring errors (e.g. duplicate answers)."""
    try:
        bot.answer_callback_query(query_id, text, show_alert=show_alert)
    except Exception:
        pass


def _notify_access_denied(chat_id: int, err: str | None) -> None:
    """Send an access-denied error message to the user's chat (PM), ignoring errors."""
    if not err:
        return
    try:
        bot.send_message(chat_id, premium_prefix(err), parse_mode='HTML')
    except Exception:
        pass


def _show_warn_settings_ui(pm_chat_id: int, message_id: int, text: str, kb: InlineKeyboardMarkup) -> bool:
    try:
        resp = raw_edit_message_with_keyboard(pm_chat_id, message_id, text, kb)
        if isinstance(resp, dict):
            if resp.get("ok"):
                return True
            desc = str(resp.get("description") or "").lower()
            if "message is not modified" in desc:
                return True
    except Exception:
        pass

    if _safe_edit_message_html(pm_chat_id, message_id, text, kb):
        return True

    try:
        resp = raw_send_with_inline_keyboard(pm_chat_id, text, kb)
        if isinstance(resp, dict) and resp.get("ok"):
            return True
    except Exception:
        pass

    plain_text = _strip_tg_emoji_tags(text)
    plain_kb = _clone_inline_kb_plain(kb)

    # fallback без style/icon_custom_emoji_id и без tg-emoji в тексте
    if _safe_edit_message_html(pm_chat_id, message_id, plain_text, plain_kb):
        return True

    try:
        resp = raw_send_with_inline_keyboard(pm_chat_id, plain_text, plain_kb)
        if isinstance(resp, dict) and resp.get("ok"):
            return True
    except Exception:
        pass

    try:
        bot.send_message(
            pm_chat_id,
            plain_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=plain_kb,
        )
        return True
    except Exception:
        pass

    try:
        bot.send_message(
            pm_chat_id,
            premium_prefix("Не удалось отрисовать клавиатуру, попробуйте снова /settings."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    return False


@bot.callback_query_handler(func=lambda c: _is_warn_settings_callback_data(c.data or ""))
def cb_warn_settings_only(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != 'private':
        _safe_answer_cq(c.id)
        return

    parts = data.split(":", 3)
    if len(parts) < 3:
        _safe_answer_cq(c.id)
        return

    _, action, chat_id_s, extra = (parts + [""])[:4]
    try:
        chat_id = int(chat_id_s)
    except ValueError:
        _safe_answer_cq(c.id)
        return

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    page = "main"
    should_render = True

    if action != "dur_prompt":
        _pending_pop("pending_warn_duration", user.id)
        _pending_msg_pop("pending_warn_duration_msg", user.id)

    if action == "open":
        page = "main"
    elif action == "noop":
        _safe_answer_cq(c.id)
        return
    elif action == "toggle":
        settings["warn_enabled"] = not bool(settings.get("warn_enabled", True))
        ch["settings"] = settings
        _mod_save()
    elif action == "limit":
        try:
            value = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        value = max(2, min(10, value))
        current = int(settings.get("warn_limit") or 3)
        if value == current:
            _safe_answer_cq(c.id)
            return
        settings["warn_limit"] = value
        ch["settings"] = settings
        _mod_save()
    elif action == "ptype":
        ptype = (extra or "").strip().lower()
        if ptype in ("mute", "ban", "kick"):
            wp = settings.get("warn_punish") or {}
            wp["type"] = ptype
            if ptype == "kick":
                wp["duration"] = None
            elif wp.get("duration") is None:
                wp["duration"] = 24 * 60 * 60
            settings["warn_punish"] = wp
            ch["settings"] = settings
            _mod_save()
            page = "punish"
    elif action == "dur_prompt":
        wp = settings.get("warn_punish") or {}
        if (wp.get("type") or "mute").lower() not in ("mute", "ban"):
            _safe_answer_cq(c.id, "Для исключения длительность не используется.", show_alert=True)
            return
        _pending_put("pending_warn_duration", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_warn_duration_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_cancel = InlineKeyboardButton("Назад", callback_data=f"stw:open:{chat_id}")
        try:
            b_cancel.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_cancel.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_cancel)

        prompt_text = (
            "<b>Установите время наказания</b>\n\n"
            "<b>Подсказка по интервалам:</b>\n"
            "<code>m</code> - минуты, <code>h</code> - часы, <code>d</code> - дни, <code>w</code> - недели, <code>mou</code> - месяцы, <code>y</code> - годы\n"
            "<code>м</code> - минуты, <code>мин</code> - минуты, <code>ч</code> - часы, <code>д</code> - дни, <code>н</code> - недели, <code>мес</code> - месяцы, <code>г</code> - годы\n"
            "Можно комбинировать до <b>3</b> интервалов.\n\n"
            "<b>Примеры:</b> <code>30m</code>, <code>2h</code>, <code>3д</code>, <code>1н</code>, <code>1h 2m</code>, <code>2mou 1d</code>, <code>навсегда</code>."
        )
        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_warn_duration_msg", user.id, sent.message_id)
        _safe_answer_cq(c.id)
        return
    elif action == "page":
        if extra in ("count", "punish", "duration"):
            page = extra
        else:
            page = "main"

    if not should_render:
        _safe_answer_cq(c.id)
        return

    text = _render_warn_settings_local(chat_id, page=page)
    kb = _build_warn_settings_keyboard_local(chat_id, page=page)
    if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
        _safe_answer_cq(c.id, "Не удалось открыть раздел предупреждений.", show_alert=True)
        return

    _safe_answer_cq(c.id)


@bot.callback_query_handler(func=lambda c: _is_antiflood_settings_callback_data(c.data or ""))
def cb_antiflood_settings_only(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return

    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != 'private':
        _safe_answer_cq(c.id)
        return

    parts = data.split(":", 3)
    if len(parts) < 3:
        _safe_answer_cq(c.id)
        return

    _, action, chat_id_s, extra = (parts + [""])[:4]
    try:
        chat_id = int(chat_id_s)
    except ValueError:
        _safe_answer_cq(c.id)
        return

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    af = settings.get("antiflood") or {}
    punish = af.get("punish") or {}

    page = "main"

    if action != "dur_prompt":
        _pending_pop("pending_antiflood_duration", user.id)
        _pending_msg_pop("pending_antiflood_duration_msg", user.id)

    if action == "open":
        page = "main"
    elif action == "toggle":
        af["enabled"] = not bool(af.get("enabled", False))
        settings["antiflood"] = af
        ch["settings"] = settings
        _mod_save()
    elif action == "deltoggle":
        af["delete_messages"] = not bool(af.get("delete_messages", False))
        settings["antiflood"] = af
        ch["settings"] = settings
        _mod_save()
    elif action == "time":
        try:
            sec = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        af["period"] = max(3, min(300, sec))
        settings["antiflood"] = af
        ch["settings"] = settings
        _mod_save()
        page = "time"
    elif action == "msgs":
        try:
            count = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        af["messages"] = max(2, min(50, count))
        settings["antiflood"] = af
        ch["settings"] = settings
        _mod_save()
        page = "messages"
    elif action == "ptype":
        ptype = (extra or "").strip().lower()
        if ptype in ("mute", "ban", "kick", "warn"):
            punish["type"] = ptype
            if ptype == "kick":
                punish["duration"] = None
            elif punish.get("duration") is None:
                punish["duration"] = 30 * 60
            af["punish"] = punish
            settings["antiflood"] = af
            ch["settings"] = settings
            _mod_save()
        page = "punish"
    elif action == "dur":
        try:
            sec = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        ptype = (punish.get("type") or "mute").lower()
        if ptype != "kick":
            punish["duration"] = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, sec))
            af["punish"] = punish
            settings["antiflood"] = af
            ch["settings"] = settings
            _mod_save()
        page = "duration"
    elif action == "dur_prompt":
        ptype = (punish.get("type") or "mute").lower()
        if ptype == "kick":
            _safe_answer_cq(c.id, "Для исключения длительность не используется.", show_alert=True)
            return

        _pending_put("pending_antiflood_duration", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_antiflood_duration_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_back = InlineKeyboardButton("Назад", callback_data=f"stf:open:{chat_id}")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_back)

        prompt_text = (
            "<b>Установите длительность наказания для антифлуда</b>\n\n"
            "<b>Подсказка по интервалам:</b>\n"
            "<code>m</code> - минуты, <code>h</code> - часы, <code>d</code> - дни, <code>w</code> - недели, <code>mou</code> - месяцы, <code>y</code> - годы\n"
            "<code>м</code> - минуты, <code>мин</code> - минуты, <code>ч</code> - часы, <code>д</code> - дни, <code>н</code> - недели, <code>мес</code> - месяцы, <code>г</code> - годы\n"
            "Можно комбинировать до <b>3</b> интервалов.\n\n"
            "<b>Примеры:</b> <code>10m</code>, <code>1h 30m</code>, <code>2д</code>, <code>навсегда</code>."
        )

        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_antiflood_duration_msg", user.id, sent.message_id)
        _safe_answer_cq(c.id)
        return
    elif action == "page":
        if extra in ("time", "messages", "punish", "duration"):
            page = extra
        else:
            page = "main"
    else:
        _safe_answer_cq(c.id)
        return

    text = _render_antiflood_settings_local(chat_id, page=page)
    kb = _build_antiflood_settings_keyboard_local(chat_id, page=page)
    if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
        _safe_answer_cq(c.id, "Не удалось открыть раздел антифлуда.", show_alert=True)
        return

    _safe_answer_cq(c.id)


@bot.callback_query_handler(func=lambda c: _is_antiraid_settings_callback_data(c.data or ""))
def cb_antiraid_settings_only(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return

    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != 'private':
        _safe_answer_cq(c.id)
        return

    parts = data.split(":", 3)
    if len(parts) < 3:
        _safe_answer_cq(c.id)
        return

    _, action, chat_id_s, extra = (parts + [""])[:4]
    try:
        chat_id = int(chat_id_s)
    except ValueError:
        _safe_answer_cq(c.id)
        return

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    ar = settings.get("antiraid") or {}
    punish = ar.get("punish") or {}

    page = "main"

    if action != "dur_prompt":
        _pending_pop("pending_antiraid_duration", user.id)
        _pending_msg_pop("pending_antiraid_duration_msg", user.id)

    if action == "open":
        page = "main"
    elif action == "toggle":
        ar["enabled"] = not bool(ar.get("enabled", False))
        settings["antiraid"] = ar
        ch["settings"] = settings
        _mod_save()
    elif action == "count":
        try:
            val = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        ar["count"] = max(ANTIRAID_MIN_COUNT, min(ANTIRAID_MAX_COUNT, val))
        settings["antiraid"] = ar
        ch["settings"] = settings
        _mod_save()
        page = "count"
    elif action == "time":
        try:
            sec = int(extra)
        except Exception:
            _safe_answer_cq(c.id)
            return
        ar["period"] = max(ANTIRAID_MIN_PERIOD, min(ANTIRAID_MAX_PERIOD, sec))
        settings["antiraid"] = ar
        ch["settings"] = settings
        _mod_save()
        page = "time"
    elif action == "ptype":
        ptype = (extra or "").strip().lower()
        if ptype in ("mute", "ban", "kick"):
            punish["type"] = ptype
            if ptype == "kick":
                punish["duration"] = None
            elif punish.get("duration") is None:
                punish["duration"] = 30 * 60
            ar["punish"] = punish
            settings["antiraid"] = ar
            ch["settings"] = settings
            _mod_save()
        page = "punish"
    elif action == "dur_prompt":
        ptype = (punish.get("type") or "mute").lower()
        if ptype == "kick":
            _safe_answer_cq(c.id, "Для исключения длительность не используется.", show_alert=True)
            return

        _pending_put("pending_antiraid_duration", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_antiraid_duration_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_back = InlineKeyboardButton("Назад", callback_data=f"star:open:{chat_id}")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_back)

        prompt_text = (
            "<b>Установите длительность наказания для анти-рейда</b>\n\n"
            "<b>Подсказка по интервалам:</b>\n"
            "<code>m</code> - минуты, <code>h</code> - часы, <code>d</code> - дни, <code>w</code> - недели, <code>mou</code> - месяцы, <code>y</code> - годы\n"
            "<code>м</code> - минуты, <code>мин</code> - минуты, <code>ч</code> - часы, <code>д</code> - дни, <code>н</code> - недели, <code>мес</code> - месяцы, <code>г</code> - годы\n"
            "Можно комбинировать до <b>3</b> интервалов.\n\n"
            "<b>Примеры:</b> <code>10m</code>, <code>1h 30m</code>, <code>2д</code>, <code>навсегда</code>."
        )

        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_antiraid_duration_msg", user.id, sent.message_id)
        _safe_answer_cq(c.id)
        return
    elif action == "page":
        if extra in ("count", "time", "punish", "duration"):
            page = extra
        else:
            page = "main"
    else:
        _safe_answer_cq(c.id)
        return

    text = _render_antiraid_settings_local(chat_id, page=page)
    kb = _build_antiraid_settings_keyboard_local(chat_id, page=page)
    if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
        _safe_answer_cq(c.id, "Не удалось открыть раздел анти-рейда.", show_alert=True)
        return

    _safe_answer_cq(c.id)


@bot.callback_query_handler(func=lambda c: c.data and (
    c.data.startswith("st_close:") or
    (c.data.startswith("st_main:") and not c.data.endswith(":warn") and not c.data.endswith(":warns")) or
    c.data.startswith("st_back_main:") or
    c.data.startswith("st_welcome_") or
    c.data.startswith("st_farewell_") or
    c.data.startswith("st_rules_") or
    c.data.startswith("st_first_comment_") or
    c.data.startswith("p:") or
    c.data.startswith("rules:") or
    c.data.startswith("del:") or
    c.data.startswith("st_cleanup_")
))
def cb_settings_main(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    # popup/rules/del работают в группах и в ЛС (но доступ по uid)
    if data.startswith("p:"):
        # p:section:chat_id:uid:idx
        try:
            _, sec, chat_id_s, uid_s, idx_s = data.split(":")
            chat_id = int(chat_id_s)
            uid = int(uid_s)
            idx = int(idx_s)
        except Exception:
            _safe_answer_cq(c.id)
            return

        if user.id != uid:
            _safe_answer_cq(c.id, "Недоступно.", show_alert=True)
            return

        st = get_chat_settings(chat_id)
        popups = ((st.get(sec) or {}).get("buttons") or {}).get("popups") or []
        txt = popups[idx] if 0 <= idx < len(popups) else "..."
        _safe_answer_cq(c.id, txt, show_alert=True)
        return

    if data.startswith("rules:"):
        # rules:chat_id:uid
        try:
            _, chat_id_s, uid_s = data.split(":")
            chat_id = int(chat_id_s)
            uid = int(uid_s)
        except Exception:
            _safe_answer_cq(c.id)
            return
        if user.id != uid:
            _safe_answer_cq(c.id, "Недоступно.", show_alert=True)
            return

        st = get_chat_settings(chat_id)
        rules = st.get("rules") or _default_section(False)
        html = build_html_from_text_custom(rules.get("text_custom") or "")
        media = rules.get("media") or []
        rows = ((rules.get("buttons") or {}).get("rows")) or []
        popups = ((rules.get("buttons") or {}).get("popups")) or []
        kb = build_inline_keyboard_for_payload("rules", chat_id, rows, popups, uid)

        _safe_answer_cq(c.id)
        _send_payload(c.message.chat.id, html, media, reply_markup=kb)
        return

    if data.startswith("del:"):
        # del:chat_id:uid
        try:
            _, chat_id_s, uid_s = data.split(":")
            uid = int(uid_s)
        except Exception:
            _safe_answer_cq(c.id)
            return
        if user.id != uid:
            _safe_answer_cq(c.id, "Недоступно.", show_alert=True)
            return
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        _safe_answer_cq(c.id)
        return

    # settings UI only in private
    if msg_chat.type != 'private':
        _safe_answer_cq(c.id)
        return

    parts = data.split(":", 2)
    prefix = parts[0]
    if len(parts) < 2:
        _safe_answer_cq(c.id)
        return

    try:
        chat_id = int(parts[1])
    except ValueError:
        _safe_answer_cq(c.id)
        return

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    # close
    if prefix == "st_close":
        _try_delete_private_prompt(msg_chat.id, c.message.message_id)
        _safe_answer_cq(c.id)
        return

    # back main
    if prefix == "st_back_main":
        _safe_answer_cq(c.id)
        emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
        text = (
            f"{emoji_settings} <b>Настройки чата</b>\n"
            f"Чат ID: <code>{chat_id}</code>\n\n"
            "<b>Выберите раздел для настройки:</b>"
        )
        kb = _build_settings_main_keyboard(chat_id, viewer_user=user)
        edited = _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        if not edited:
            try:
                bot.send_message(
                    msg_chat.id,
                    text,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception:
                pass
        return

    # main section
    if prefix == "st_main":
        if len(parts) < 3:
            _safe_answer_cq(c.id)
            return
        sec = parts[2]

        get_chat_settings(chat_id)

        if sec in SECTION_KEYS:
            _safe_answer_cq(c.id)
            text = _render_section_preview(chat_id, sec)
            kb = _build_section_keyboard(chat_id, sec)
        elif sec == "modules":
            _safe_answer_cq(c.id)
            text = _render_modules_text(chat_id)
            kb = _build_modules_keyboard(chat_id)
        elif sec == "cleanup":
            _safe_answer_cq(c.id)
            text = _render_cleanup_main(chat_id)
            kb = _build_cleanup_main_keyboard(chat_id)
        elif sec in ("warns", "warn"):
            _safe_answer_cq(c.id)
            try:
                text = _render_warn_settings_local(chat_id, page="main")
                kb = _build_warn_settings_keyboard_local(chat_id, page="main")
            except Exception:
                try:
                    bot.send_message(msg_chat.id, "⚠️ Не удалось открыть раздел предупреждений.",
                                     parse_mode='HTML')
                except Exception:
                    pass
                return
        elif sec == "antiflood":
            _safe_answer_cq(c.id)
            try:
                text = _render_antiflood_settings_local(chat_id, page="main")
                kb = _build_antiflood_settings_keyboard_local(chat_id, page="main")
            except Exception:
                try:
                    bot.send_message(msg_chat.id, "⚠️ Не удалось открыть раздел антифлуда.",
                                     parse_mode='HTML')
                except Exception:
                    pass
                return
        elif sec == "filters":
            _safe_answer_cq(c.id)
            text = _render_filters_text(chat_id)
            kb = _build_filters_keyboard(chat_id)
        elif sec == "commands":
            _safe_answer_cq(c.id)
            text = _render_commands_main(chat_id)
            kb = _build_commands_main_keyboard(chat_id)
        elif sec == "roles":
            if not _user_can_edit_now(user, chat_id):
                _safe_answer_cq(c.id, "Недостаточно прав для настройки ролей.", show_alert=True)
                return
            _safe_answer_cq(c.id)

            emoji_chat = f'<tg-emoji emoji-id="5341715473882955310">📋</tg-emoji>'
            emoji_choose = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHOOSE_RANK_ID}">🔽</tg-emoji>'
            try:
                chat_obj = tg_get_chat(chat_id)
                title = chat_obj.title or str(chat_id)
            except Exception:
                title = str(chat_id)

            text = (
                f"{emoji_chat} <b>Настройка прав должностей для чата</b> "
                f"<b>{_html.escape(title)}</b> (<code>{chat_id}</code>)\n"
                f"{emoji_choose} <b>Выберите должность для настройки прав:</b>"
            )
            kb = _build_ranks_keyboard(chat_id, for_pm=True, back_callback=f"st_back_main:{chat_id}")
        elif sec == "logging":
            _safe_answer_cq(c.id)
            text = _render_logging_text(chat_id)
            kb = _build_logging_keyboard(chat_id)
        else:
            _safe_answer_cq(c.id)
            text = premium_prefix("Неизвестный раздел настроек.")
            kb = _build_settings_main_keyboard(chat_id, viewer_user=user)

        edited = _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        if not edited:
            try:
                bot.send_message(
                    msg_chat.id,
                    text,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception:
                pass
        return

    # section actions: st_<sec>_...
    msec = re.match(r"st_(welcome|farewell|rules|first_comment|cleanup|warn)_(.+)", prefix)
    if not msec:
        _safe_answer_cq(c.id)
        return

    sec = msec.group(1)
    action = msec.group(2)

    if sec == "warn":
        ch = _mod_get_chat(chat_id)
        settings = ch.get("settings") or {}
        page = "main"

        if action == "open":
            text = _render_warn_settings(chat_id, page="main")
            kb = _build_warn_settings_keyboard(chat_id, page="main")
            edited = _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            if not edited:
                try:
                    bot.send_message(
                        msg_chat.id,
                        text,
                        parse_mode='HTML',
                        disable_web_page_preview=True,
                        reply_markup=kb,
                    )
                except Exception:
                    _safe_answer_cq(c.id, "Не удалось открыть раздел предупреждений.", show_alert=True)
                    return
            _safe_answer_cq(c.id)
            return

        if action == "noop":
            _safe_answer_cq(c.id)
            return

        if action == "toggle":
            settings["warn_enabled"] = not bool(settings.get("warn_enabled", True))
            ch["settings"] = settings
            _mod_save()
        elif action == "ptype":
            try:
                _, chat_id_s, ptype = data.split(":", 2)
            except Exception:
                _safe_answer_cq(c.id)
                return
            if ptype in ("mute", "ban", "kick"):
                wp = settings.get("warn_punish") or {}
                wp["type"] = ptype
                if ptype == "kick":
                    wp["duration"] = None
                elif wp.get("duration") is None:
                    wp["duration"] = 24 * 60 * 60
                settings["warn_punish"] = wp
                ch["settings"] = settings
                _mod_save()
                page = "punish"
        elif action == "dur":
            try:
                _, chat_id_s, dur_s = data.split(":", 2)
                duration = int(dur_s)
            except Exception:
                _safe_answer_cq(c.id)
                return
            duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, duration))
            wp = settings.get("warn_punish") or {}
            if (wp.get("type") or "mute").lower() in ("mute", "ban"):
                wp["duration"] = duration
                settings["warn_punish"] = wp
                ch["settings"] = settings
                _mod_save()
                page = "punish"
        elif action == "limit":
            try:
                _, chat_id_s, delta_s = data.split(":", 2)
                delta = int(delta_s)
            except Exception:
                _safe_answer_cq(c.id)
                return
            cur = int(settings.get("warn_limit") or 3)
            settings["warn_limit"] = max(2, min(10, cur + delta))
            ch["settings"] = settings
            _mod_save()
        elif action == "setlimit":
            try:
                _, chat_id_s, value_s = data.split(":", 2)
                value = int(value_s)
            except Exception:
                _safe_answer_cq(c.id)
                return
            settings["warn_limit"] = max(2, min(10, value))
            ch["settings"] = settings
            _mod_save()
        elif action == "page":
            try:
                _, chat_id_s, page_s = data.split(":", 2)
                page = "punish" if page_s == "punish" else "main"
            except Exception:
                _safe_answer_cq(c.id)
                return

        text = _render_warn_settings(chat_id, page=page)
        kb = _build_warn_settings_keyboard(chat_id, page=page)
        edited = _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        if not edited:
            try:
                bot.send_message(
                    msg_chat.id,
                    text,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception:
                _safe_answer_cq(c.id, "Не удалось открыть раздел предупреждений.", show_alert=True)
                return
        _safe_answer_cq(c.id)
        return

    # ✅ cleanup как секция
    if sec == "cleanup":
        cl = _cleanup_get(chat_id)

        if action == "cmds":
            text = _render_cleanup_commands(chat_id)
            kb = _build_cleanup_commands_keyboard(chat_id)
            _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            _safe_answer_cq(c.id)
            return

        if action == "sys":
            text = _render_cleanup_system(chat_id)
            kb = _build_cleanup_system_keyboard(chat_id, selected_idx=None)
            _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            _safe_answer_cq(c.id)
            return

        if action == "syspick":
            try:
                _, chat_id_s, idx_s = (c.data or "").split(":", 2)
                idx = int(idx_s)
            except Exception:
                _safe_answer_cq(c.id)
                return

            if idx < 0 or idx >= len(CLEANUP_SYSTEM_TYPES_ORDER):
                idx = None

            text = _render_cleanup_system(chat_id)
            kb = _build_cleanup_system_keyboard(chat_id, selected_idx=idx)
            _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            _safe_answer_cq(c.id)
            return

        if action in ("cmdnoop", "sysnoop"):
            _safe_answer_cq(c.id)
            return

        if action == "cmdset":
            try:
                _, chat_id_s, sign, val_s = (c.data or "").split(":", 3)
                sign = sign.strip()
                val = (val_s.strip() == "1")
            except Exception:
                _safe_answer_cq(c.id)
                return

            if sign in CLEANUP_CMD_SIGNS:
                cmds = cl.get("commands") or {}
                cmds[sign] = bool(val)
                cl["commands"] = cmds
                cl["updated_at"] = _now_ts()
                _cleanup_save(chat_id, cl)

            text = _render_cleanup_commands(chat_id)
            kb = _build_cleanup_commands_keyboard(chat_id)
            _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            _safe_answer_cq(c.id)
            return

        if action == "sysset":
            try:
                _, chat_id_s, idx_s, val_s = (c.data or "").split(":", 3)
                idx = int(idx_s)
                val = (val_s.strip() == "1")
            except Exception:
                _safe_answer_cq(c.id)
                return

            if 0 <= idx < len(CLEANUP_SYSTEM_TYPES_ORDER):
                ct = CLEANUP_SYSTEM_TYPES_ORDER[idx]
                sysd = cl.get("system") or {}
                sysd[ct] = bool(val)
                cl["system"] = sysd
                cl["updated_at"] = _now_ts()
                _cleanup_save(chat_id, cl)

            text = _render_cleanup_system(chat_id)
            kb = _build_cleanup_system_keyboard(chat_id, selected_idx=idx)
            _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
            _safe_answer_cq(c.id)
            return

        _safe_answer_cq(c.id)
        return



    # ✅ иначе — welcome/farewell/rules
    st = get_chat_settings(chat_id)
    sc = st.get(sec) or _default_section(False)

    # toggle
    if action == "toggle":
        sc["enabled"] = not bool(sc.get("enabled"))
        sc["updated_at"] = _now_ts()
        st[sec] = sc
        CHAT_SETTINGS[str(chat_id)] = st
        save_chat_settings()

        text = _render_section_preview(chat_id, sec)
        kb = _build_section_keyboard(chat_id, sec)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)

        _safe_answer_cq(c.id)
        return

    # show current full (как увидит пользователь) — 2 сообщения
    if action == "show":
        html_no_subs = build_html_from_text_custom(sc.get("text_custom") or "")
        html_with_subs = _apply_vars(html_no_subs, chat_id, c.message.chat.title or "", user)

        media = sc.get("media") or []
        rows = ((sc.get("buttons") or {}).get("rows")) or []
        popups = ((sc.get("buttons") or {}).get("popups")) or []
        kb_payload = build_inline_keyboard_for_payload(sec, chat_id, rows, popups, user.id)

        _safe_answer_cq(c.id)
        bot.send_message(
            msg_chat.id,
            f"<b>{_html.escape(_section_title(sec).capitalize())} (как увидит пользователь):</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        _send_payload(msg_chat.id, html_with_subs, media, reply_markup=kb_payload)
        return

    # ---------------- TEXT UI ----------------
    if action == "text":
        _pending_put(f"pending_{sec}_text", user.id, chat_id)

        # удаляем старую UI-мессагу для этого pending (если была) + текущее сообщение
        _delete_pending_ui(msg_chat.id, f"pending_{sec}_text_msg", user.id, also_msg_id=c.message.message_id)

        emoji_text = f'<tg-emoji emoji-id="{EMOJI_WELCOME_TEXT_ID}">📝</tg-emoji>'
        body = (
            f"{emoji_text} <b>Пришлите новый текст для {_section_title(sec)}.</b>\n\n"
            "<blockquote expandable=\"true\">"
            "<b>Доступные переменные:</b>\n"
            "[NAME] — полное имя пользователя\n"
            "[ID] — ID пользователя\n"
            "[GROUP_NAME] — название группы\n"
            "[NAME_LINK] — полное имя пользователя с ссылкой на профиль\n"
            "[MENTION] — упоминание пользователя"
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
            "то Telegram-форматирование может быть проигнорировано."
        )

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("Показать текущий текст", callback_data=f"st_{sec}_text_show:{chat_id}"))
        kb.add(InlineKeyboardButton("Удалить текст", callback_data=f"st_{sec}_text_del:{chat_id}"))
        kb.add(_build_cancel_btn(f"st_{sec}_text_cancel:{chat_id}"))

        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _pending_msg_set(f"pending_{sec}_text_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "text_cancel":
        _pending_pop(f"pending_{sec}_text", user.id)
        msg_id = _pending_msg_pop(f"pending_{sec}_text_msg", user.id)
        _try_delete_private_prompt(msg_chat.id, msg_id)

        text = _render_section_preview(chat_id, sec)
        kb = _build_section_keyboard(chat_id, sec)
        bot.send_message(msg_chat.id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _safe_answer_cq(c.id)
        return

    if action == "text_del":
        sc["text_custom"] = ""
        sc["source"] = "plain"
        sc["entities"] = []
        sc["updated_at"] = _now_ts()
        st[sec] = sc
        CHAT_SETTINGS[str(chat_id)] = st
        save_chat_settings()

        # FIX #3: prompt исчезает, "удалено" приходит с Назад + Отмена
        _delete_pending_ui(msg_chat.id, f"pending_{sec}_text_msg", user.id, also_msg_id=c.message.message_id)
        _pending_put(f"pending_{sec}_text", user.id, chat_id)

        sent = bot.send_message(
            msg_chat.id,
            f"{emoji_ok} <b>Текст удалён.</b>\n\nНажмите «Назад», чтобы снова увидеть инструкцию и прислать новый текст.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_kb_deleted(
                back_cb=f"st_{sec}_text:{chat_id}",
                cancel_cb=f"st_{sec}_text_cancel:{chat_id}",
            ),
        )
        _pending_msg_set(f"pending_{sec}_text_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "text_show":
        tc = (sc.get("text_custom") or "").strip()
        _safe_answer_cq(c.id)
        bot.send_message(msg_chat.id, "<b>Текущий текст (как увидит пользователь):</b>", parse_mode="HTML", disable_web_page_preview=True)
        if not tc:
            bot_raw.send_message(msg_chat.id, "Текст не задан.", disable_web_page_preview=True)
            return
        html_no_subs = build_html_from_text_custom(tc)
        bot.send_message(msg_chat.id, html_no_subs, parse_mode="HTML", disable_web_page_preview=True)
        return

    # ---------------- MEDIA UI ----------------
    
    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    
    if action == "media":
        _pending_put(f"pending_{sec}_media", user.id, chat_id)

        _delete_pending_ui(msg_chat.id, f"pending_{sec}_media_msg", user.id, also_msg_id=c.message.message_id)

        emoji_media = f'<tg-emoji emoji-id="{EMOJI_WELCOME_MEDIA_ID}">🖼</tg-emoji>'
        body = (
            f"{emoji_media} <b>Пришлите медиа для {_section_title(sec)}.</b>\n\n"
            "<b>Поддерживается:</b>\n"
            "• Фото\n• Видео\n• Файл\n• Музыка\n• GIF\n\n"
            "<i>Подпись отдельно не задаётся.</i>\n"
            "Если у вас есть текст — он будет автоматически использоваться как описание, когда медиа есть."
        )

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("Показать текущее медиа", callback_data=f"st_{sec}_media_show:{chat_id}"))
        kb.add(InlineKeyboardButton("Удалить медиа", callback_data=f"st_{sec}_media_del:{chat_id}"))
        kb.add(_build_cancel_btn(f"st_{sec}_media_cancel:{chat_id}"))

        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _pending_msg_set(f"pending_{sec}_media_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "media_cancel":
        _pending_pop(f"pending_{sec}_media", user.id)
        msg_id = _pending_msg_pop(f"pending_{sec}_media_msg", user.id)
        _try_delete_private_prompt(msg_chat.id, msg_id)

        text = _render_section_preview(chat_id, sec)
        kb = _build_section_keyboard(chat_id, sec)
        bot.send_message(msg_chat.id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _safe_answer_cq(c.id)
        return

    if action == "media_del":
        sc["media"] = []
        sc["updated_at"] = _now_ts()
        st[sec] = sc
        CHAT_SETTINGS[str(chat_id)] = st
        save_chat_settings()

        _delete_pending_ui(msg_chat.id, f"pending_{sec}_media_msg", user.id, also_msg_id=c.message.message_id)
        _pending_put(f"pending_{sec}_media", user.id, chat_id)

        sent = bot.send_message(
            msg_chat.id,
            f"{emoji_ok} <b>Медиа удалено.</b>\n\nНажмите «Назад», чтобы снова увидеть инструкцию и прислать новое медиа.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_kb_deleted(
                back_cb=f"st_{sec}_media:{chat_id}",
                cancel_cb=f"st_{sec}_media_cancel:{chat_id}",
            ),
        )
        _pending_msg_set(f"pending_{sec}_media_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "media_show":
        _safe_answer_cq(c.id)
        bot.send_message(msg_chat.id, "<b>Текущее медиа:</b>", parse_mode="HTML", disable_web_page_preview=True)
        media = sc.get("media") or []
        if not media:
            bot_raw.send_message(msg_chat.id, "Медиа не задано.", disable_web_page_preview=True)
            return
        _send_media_only(msg_chat.id, media)  # ВАЖНО: без кнопок
        return

    # ---------------- BUTTONS UI ----------------

    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    
    if action == "buttons":
        _pending_put(f"pending_{sec}_buttons", user.id, chat_id)

        _delete_pending_ui(msg_chat.id, f"pending_{sec}_buttons_msg", user.id, also_msg_id=c.message.message_id)

        emoji_btn = f'<tg-emoji emoji-id="{EMOJI_WELCOME_BUTTONS_ID}">🔘</tg-emoji>'
        body = (
            f"{emoji_btn} <b>Пришлите кнопки для {_section_title(sec)}.</b>\n\n"
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
            "<code>#b Название - example.com</code> (цвет, зависящий от темы пользователя)\n\n"
            "<b>Лимиты:</b>\n"
            f"• 1–{MAX_PER_ROW} кнопки в ряду\n"
            f"• до {MAX_ROWS} рядов\n"
            f"• до {MAX_TOTAL_BTNS} кнопок всего\n"
            "• до 1 премиум-эмодзи в кнопке (эмодзи может быть только в начале названия)"  
        )

        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton("Показать текущие кнопки", callback_data=f"st_{sec}_buttons_show:{chat_id}"))
        kb.add(InlineKeyboardButton("Удалить кнопки", callback_data=f"st_{sec}_buttons_del:{chat_id}"))
        kb.add(_build_cancel_btn(f"st_{sec}_buttons_cancel:{chat_id}"))

        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _pending_msg_set(f"pending_{sec}_buttons_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "buttons_cancel":
        _pending_pop(f"pending_{sec}_buttons", user.id)
        msg_id = _pending_msg_pop(f"pending_{sec}_buttons_msg", user.id)
        _try_delete_private_prompt(msg_chat.id, msg_id)

        text = _render_section_preview(chat_id, sec)
        kb = _build_section_keyboard(chat_id, sec)
        bot.send_message(msg_chat.id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb)
        _safe_answer_cq(c.id)
        return

    if action == "buttons_del":
        sc["buttons"] = {"rows": [], "popups": []}
        sc["updated_at"] = _now_ts()
        st[sec] = sc
        CHAT_SETTINGS[str(chat_id)] = st
        save_chat_settings()

        _delete_pending_ui(msg_chat.id, f"pending_{sec}_buttons_msg", user.id, also_msg_id=c.message.message_id)
        _pending_put(f"pending_{sec}_buttons", user.id, chat_id)

        sent = bot.send_message(
            msg_chat.id,
             f"{emoji_ok} <b>Кнопки удалены.</b>\n\nНажмите «Назад», чтобы снова увидеть инструкцию и прислать кнопки заново.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_kb_deleted(
                back_cb=f"st_{sec}_buttons:{chat_id}",
                cancel_cb=f"st_{sec}_buttons_cancel:{chat_id}",
            ),
        )
        _pending_msg_set(f"pending_{sec}_buttons_msg", user.id, sent.message_id)

        _safe_answer_cq(c.id)
        return

    if action == "buttons_show":
        rows = ((sc.get("buttons") or {}).get("rows")) or []
        popups = ((sc.get("buttons") or {}).get("popups")) or []

        _safe_answer_cq(c.id)
        bot.send_message(msg_chat.id, "<b>Текущие кнопки:</b>", parse_mode="HTML", disable_web_page_preview=True)

        kb_show = build_inline_keyboard_for_payload(sec, chat_id, rows, popups, user.id)
        if not kb_show:
            bot_raw.send_message(msg_chat.id, "Кнопки не заданы.", disable_web_page_preview=True)
            return

        bot.send_message(msg_chat.id, "\u2063", disable_web_page_preview=True, reply_markup=kb_show)
        return

    _safe_answer_cq(c.id)
  
# ------------------------------------------------------------
# LOG CHANNEL callbacks  (stlc:...)
# ------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("stlc:"))
def cb_log_channel(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != 'private':
        bot.answer_callback_query(c.id)
        return

    # stlc:<action>:<chat_id>[:<extra>]
    parts = data.split(":", 3)
    if len(parts) < 3:
        bot.answer_callback_query(c.id)
        return
    action = parts[1]
    try:
        chat_id = int(parts[2])
    except ValueError:
        bot.answer_callback_query(c.id)
        return
    extra = parts[3] if len(parts) > 3 else ""

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    if action == "open":
        text = _render_logging_text(chat_id)
        kb = _build_logging_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action == "add":
        # Store pending state
        PENDING_LOG_CHANNEL_SETUP[user.id] = {
            "chat_id": chat_id,
            "prompt_msg_id": c.message.message_id,
        }
        emoji = f'<tg-emoji emoji-id="{EMOJI_LOG_PM_ID}">📨</tg-emoji>'
        instruction = (
            f"{emoji} <b>Добавление лог-канала</b>\n\n"
            "1. Создайте канал (или выберите существующий).\n"
            "2. Добавьте этого бота в канал как администратора с правом <b>публикации сообщений</b>.\n"
            "3. Опубликуйте <b>любой пост</b> в этом канале и <b>перешлите его сюда</b> "
            "(или отправьте сообщение <b>от имени канала</b>).\n\n"
            "Бот автоматически определит канал и настроит логирование.\n\n"
            "<i>Для отмены нажмите «Отмена».</i>"
        )
        kb_cancel = InlineKeyboardMarkup()
        btn_cancel = InlineKeyboardButton("Отмена", callback_data=f"stlc:add_cancel:{chat_id}")
        try:
            btn_cancel.style = "danger"
        except Exception:
            pass
        kb_cancel.add(btn_cancel)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, instruction, kb_cancel)
        return

    if action == "add_cancel":
        PENDING_LOG_CHANNEL_SETUP.pop(user.id, None)
        text = _render_logging_text(chat_id)
        kb = _build_logging_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action == "remove_confirm":
        text = premium_prefix(
            "Внимание: лог-канал и все настройки событий будут удалены без возможности восстановления. "
            "Вы действительно хотите удалить лог-канал?"
        )
        kb = _build_logging_remove_confirm_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action == "remove":
        remove_log_channel(chat_id)
        text = _render_logging_text(chat_id)
        kb = _build_logging_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action == "events":
        text = _render_logging_events_text(chat_id)
        kb = _build_logging_events_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action == "evsel":
        try:
            g_idx = int(extra)
        except Exception:
            return
        text = _render_logging_events_text(chat_id)
        kb = _build_logging_events_keyboard(chat_id, selected_idx=g_idx)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action in ("evgroup", "evpick"):
        # evgroup:<chat_id>:<g_idx>[:<0|1>] — toggle a whole group
        # evpick kept for backward compat (treated as noop now)
        try:
            parts = extra.split(":", 1)
            g_idx = int(parts[0])
            forced_val: int | None = None
            if len(parts) == 2:
                forced_val = int(parts[1])
        except Exception:
            return

        if 0 <= g_idx < len(LOG_CHANNEL_EVENT_GROUPS):
            _, group_events = LOG_CHANNEL_EVENT_GROUPS[g_idx]
            valid_events = [ev for ev in group_events if ev in LOG_CHANNEL_ALL_EVENTS]
            if valid_events:
                lc_now = get_log_channel(chat_id)
                cur_events = (lc_now.get("events") if lc_now else None) or {}
                if forced_val is not None:
                    new_val = bool(forced_val)
                else:
                    # Toggle: if all on → all off; otherwise → all on
                    all_on = _log_group_state(cur_events, valid_events)
                    new_val = not all_on
                for ev in valid_events:
                    set_log_channel_event(chat_id, ev, new_val)

        text = _render_logging_events_text(chat_id)
        # Keep selection after enable/disable (like cleanup_system)
        kb = _build_logging_events_keyboard(chat_id, selected_idx=g_idx if forced_val is not None else None)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return

    if action in ("evset", "evnoop"):
        # evset / evnoop kept for backward compat (stale inline buttons)
        text = _render_logging_events_text(chat_id)
        kb = _build_logging_events_keyboard(chat_id)
        _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        return


# ------------------------------------------------------------
# PRIVATE handler: принимает ТЕКСТ / МЕДИА / КНОПКИ (welcome/farewell/rules)
# ------------------------------------------------------------

@bot.message_handler(func=lambda m: m.chat.type == "private", content_types=[
    "text", "photo", "video", "document", "audio", "animation"
])
def on_settings_private_input(m: types.Message):
    user_id = m.from_user.id
    ct = getattr(m, "content_type", "text")

    # ── Log-channel setup: ожидаем пересланное сообщение от канала ──
    pending_lc = PENDING_LOG_CHANNEL_SETUP.get(user_id)
    if pending_lc:
        group_chat_id = int(pending_lc.get("chat_id") or 0)
        prompt_msg_id = int(pending_lc.get("prompt_msg_id") or 0)

        # Определяем channel_id из sender_chat (пост от имени канала) или из forwarded
        channel_id: int | None = None
        channel_title: str = ""

        sender_chat = getattr(m, "sender_chat", None)
        if sender_chat and getattr(sender_chat, "type", None) == "channel":
            channel_id = int(sender_chat.id)
            channel_title = str(sender_chat.title or "")

        if not channel_id:
            fwd_chat = getattr(m, "forward_from_chat", None)
            if fwd_chat and getattr(fwd_chat, "type", None) == "channel":
                channel_id = int(fwd_chat.id)
                channel_title = str(fwd_chat.title or "")

        if channel_id:
            PENDING_LOG_CHANNEL_SETUP.pop(user_id, None)
            # Проверяем, что бот — администратор с правом публикации
            bot_admin = False
            try:
                me = get_bot_me()
                member = bot.get_chat_member(channel_id, me.id)
                if member.status == "administrator" and getattr(member, "can_post_messages", False):
                    bot_admin = True
            except Exception:
                pass

            emoji_log = f'<tg-emoji emoji-id="{EMOJI_LOG_ID}">📋</tg-emoji>'
            if not bot_admin:
                err_text = (
                    f"{emoji_log} <b>Ошибка</b>\n\n"
                    "Бот не является администратором канала с правом публикации сообщений.\n\n"
                    "Выдайте боту права администратора канала с разрешением <b>публикации постов</b> "
                    "и попробуйте снова."
                )
                kb_retry = InlineKeyboardMarkup()
                btn_retry = InlineKeyboardButton("Попробовать снова", callback_data=f"stlc:add:{group_chat_id}")
                btn_back = InlineKeyboardButton("Отмена", callback_data=f"stlc:open:{group_chat_id}")
                kb_retry.add(btn_retry)
                kb_retry.add(btn_back)
                if prompt_msg_id:
                    _safe_edit_message_html(m.chat.id, prompt_msg_id, err_text, kb_retry)
                else:
                    bot.send_message(m.chat.id, err_text, parse_mode="HTML",
                                     disable_web_page_preview=True, reply_markup=kb_retry)
                return

            set_log_channel(group_chat_id, channel_id, channel_title)
            ok_text = (
                f"{emoji_log} <b>Лог-канал подключён!</b>\n\n"
                f"Канал: <b>{_html.escape(channel_title or str(channel_id))}</b>\n"
                f"ID: <code>{channel_id}</code>\n\n"
                "Теперь бот будет отправлять туда логи событий группы."
            )
            text = _render_logging_text(group_chat_id)
            kb = _build_logging_keyboard(group_chat_id)
            if prompt_msg_id:
                _safe_edit_message_html(m.chat.id, prompt_msg_id, ok_text, None)
            bot.send_message(m.chat.id, text, parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=kb)
            return
        # Сообщение не содержит информации о канале — напоминаем
        bot.send_message(
            m.chat.id,
            "Пожалуйста, перешлите <b>пост из канала</b> или отправьте сообщение <b>от имени канала</b>.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    pending_bc = BROADCAST_PENDING_INPUT.get(user_id)
    if pending_bc and is_owner(m.from_user):
        draft = BROADCAST_DRAFTS.get(user_id)
        if not draft or int(draft.get("id") or 0) != int(pending_bc.get("draft_id") or 0):
            BROADCAST_PENDING_INPUT.pop(user_id, None)
            return

        mode = str(pending_bc.get("mode") or "")
        prompt_id = int(pending_bc.get("prompt_message_id") or 0)

        if mode == "text":
            if ct != "text":
                bot.send_message(m.chat.id, premium_prefix("Для текста рассылки пришлите текстовое сообщение."), parse_mode='HTML', disable_web_page_preview=True)
                return
            text_custom, source, entities_ser = convert_section_text_from_message(m)
            draft["text_custom"] = text_custom
            draft["source"] = source
            draft["entities"] = entities_ser
            draft["updated_at"] = int(time.time())
        elif mode == "media":
            if ct == "text":
                bot.send_message(m.chat.id, premium_prefix("Для медиа рассылки пришлите фото/видео/файл/музыку/gif."), parse_mode='HTML', disable_web_page_preview=True)
                return
            payload = _extract_media_payload(m)
            if not payload:
                bot.send_message(m.chat.id, premium_prefix("Этот тип медиа не поддерживается для рассылки."), parse_mode='HTML', disable_web_page_preview=True)
                return
            draft["media"] = [payload]
            draft["updated_at"] = int(time.time())
        elif mode == "buttons":
            if ct != "text":
                bot.send_message(m.chat.id, premium_prefix("Кнопки для рассылки нужно отправлять текстом."), parse_mode='HTML', disable_web_page_preview=True)
                return
            try:
                rows, popups = parse_buttons_text(m.text or "", m.entities or [])
            except ButtonSyntaxError as err:
                bot.send_message(m.chat.id, premium_prefix(_format_button_syntax_error(err)), parse_mode='HTML', disable_web_page_preview=True)
                return
            draft["buttons"] = {"rows": rows, "popups": popups}
            draft["updated_at"] = int(time.time())
        else:
            BROADCAST_PENDING_INPUT.pop(user_id, None)
            return

        BROADCAST_DRAFTS[user_id] = draft
        BROADCAST_PENDING_INPUT.pop(user_id, None)

        if prompt_id > 0:
            try:
                bot.delete_message(m.chat.id, prompt_id)
            except Exception:
                pass

        panel_text = _broadcast_render_panel_text(user_id)
        kb = _build_broadcast_panel_keyboard(int(draft.get("id") or 0))
        bot.send_message(m.chat.id, panel_text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)
        return

    pending_pm = SENDPM_PENDING_INPUT.get(user_id)
    if pending_pm and is_owner(m.from_user):
        draft = SENDPM_DRAFTS.get(user_id)
        if not draft or int(draft.get("id") or 0) != int(pending_pm.get("draft_id") or 0):
            SENDPM_PENDING_INPUT.pop(user_id, None)
            return

        mode = str(pending_pm.get("mode") or "")
        prompt_id = int(pending_pm.get("prompt_message_id") or 0)

        if mode == "text":
            if ct != "text":
                bot.send_message(m.chat.id, premium_prefix("Для текста сообщения пришлите текстовое сообщение."), parse_mode='HTML', disable_web_page_preview=True)
                return
            text_custom, source, entities_ser = convert_section_text_from_message(m)
            draft["text_custom"] = text_custom
            draft["source"] = source
            draft["entities"] = entities_ser
            draft["updated_at"] = int(time.time())
        elif mode == "media":
            if ct == "text":
                bot.send_message(m.chat.id, premium_prefix("Для медиа пришлите фото/видео/файл/музыку/gif."), parse_mode='HTML', disable_web_page_preview=True)
                return
            payload = _extract_media_payload(m)
            if not payload:
                bot.send_message(m.chat.id, premium_prefix("Этот тип медиа не поддерживается."), parse_mode='HTML', disable_web_page_preview=True)
                return
            draft["media"] = [payload]
            draft["updated_at"] = int(time.time())
        elif mode == "buttons":
            if ct != "text":
                bot.send_message(m.chat.id, premium_prefix("Кнопки нужно отправлять текстом."), parse_mode='HTML', disable_web_page_preview=True)
                return
            try:
                rows, popups = parse_buttons_text(m.text or "", m.entities or [])
            except ButtonSyntaxError as err:
                bot.send_message(m.chat.id, premium_prefix(_format_button_syntax_error(err)), parse_mode='HTML', disable_web_page_preview=True)
                return
            draft["buttons"] = {"rows": rows, "popups": popups}
            draft["updated_at"] = int(time.time())
        else:
            SENDPM_PENDING_INPUT.pop(user_id, None)
            return

        SENDPM_DRAFTS[user_id] = draft
        SENDPM_PENDING_INPUT.pop(user_id, None)

        if prompt_id > 0:
            try:
                bot.delete_message(m.chat.id, prompt_id)
            except Exception:
                pass

        panel_text = _sendpm_render_panel_text(user_id)
        kb = _build_sendpm_panel_keyboard(int(draft.get("id") or 0))
        bot.send_message(m.chat.id, panel_text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)
        return


    def _check_allowed(chat_id: int) -> bool:
        allowed, _ = _user_can_open_settings(chat_id, m.from_user)
        return bool(allowed)

    # ---------------- CUSTOM ANTIFLOOD DURATION ----------------
    antiflood_pending_cid = _pending_get("pending_antiflood_duration").get(str(user_id))
    if antiflood_pending_cid:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stf:open:{antiflood_pending_cid}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiflood_duration_msg",
                user_id,
                premium_prefix("Пришлите длительность текстом: 30m, 2h, 3д, 1н или 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        try:
            chat_id = int(antiflood_pending_cid)
        except Exception:
            _pending_pop("pending_antiflood_duration", user_id)
            return

        if not _check_allowed(chat_id):
            _pending_pop("pending_antiflood_duration", user_id)
            return

        raw = (m.text or "").strip()
        parsed_duration, consumed_tokens, invalid = _parse_duration_prefix(
            raw,
            allow_russian_duration=True,
            max_parts=3,
        )
        total_tokens = len(raw.split()) if raw else 0
        if invalid or parsed_duration is None or consumed_tokens == 0 or consumed_tokens != total_tokens:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stf:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiflood_duration_msg",
                user_id,
                premium_prefix("Неверный формат. Используйте до 3 интервалов: 30m, 1h 2m, 2mou 1d, навсегда."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        duration = int(parsed_duration)
        if duration != 0 and (duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stf:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiflood_duration_msg",
                user_id,
                premium_prefix("Длительность должна быть от 1 минуты до 365 дней, либо 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        ch = _mod_get_chat(chat_id)
        settings = ch.get("settings") or {}
        af = settings.get("antiflood") or {}
        punish = af.get("punish") or {}
        ptype = (punish.get("type") or "mute").lower()
        if ptype == "kick":
            _pending_pop("pending_antiflood_duration", user_id)
            _try_delete_private_prompt(m.chat.id, _pending_msg_pop("pending_antiflood_duration_msg", user_id))
            bot.send_message(
                m.chat.id,
                premium_prefix("Для исключения длительность не используется."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return

        punish["duration"] = int(duration)
        af["punish"] = punish
        settings["antiflood"] = af
        ch["settings"] = settings
        _mod_save()

        _pending_pop("pending_antiflood_duration", user_id)
        prompt_id = _pending_msg_pop("pending_antiflood_duration_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        ok_text = premium_prefix("✅ Время наказания антифлуда установлено.")
        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"stf:open:{chat_id}")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(
            m.chat.id,
            ok_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_ok,
        )
        return

    # ---------------- CUSTOM ANTIRAID DURATION ----------------
    antiraid_pending_cid = _pending_get("pending_antiraid_duration").get(str(user_id))
    if antiraid_pending_cid:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"star:open:{antiraid_pending_cid}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiraid_duration_msg",
                user_id,
                premium_prefix("Пришлите длительность текстом: 30m, 2h, 3д, 1н или 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        try:
            chat_id = int(antiraid_pending_cid)
        except Exception:
            _pending_pop("pending_antiraid_duration", user_id)
            return

        if not _check_allowed(chat_id):
            _pending_pop("pending_antiraid_duration", user_id)
            return

        raw = (m.text or "").strip()
        parsed_duration, consumed_tokens, invalid = _parse_duration_prefix(
            raw,
            allow_russian_duration=True,
            max_parts=3,
        )
        total_tokens = len(raw.split()) if raw else 0
        if invalid or parsed_duration is None or consumed_tokens == 0 or consumed_tokens != total_tokens:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"star:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiraid_duration_msg",
                user_id,
                premium_prefix("Неверный формат. Используйте до 3 интервалов: 30m, 1h 2m, 2mou 1d, навсегда."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        duration = int(parsed_duration)
        if duration != 0 and (duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"star:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antiraid_duration_msg",
                user_id,
                premium_prefix("Длительность должна быть от 1 минуты до 365 дней, либо 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        ch = _mod_get_chat(chat_id)
        settings = ch.get("settings") or {}
        ar = settings.get("antiraid") or {}
        punish = ar.get("punish") or {}
        ptype = (punish.get("type") or "mute").lower()
        if ptype == "kick":
            _pending_pop("pending_antiraid_duration", user_id)
            _try_delete_private_prompt(m.chat.id, _pending_msg_pop("pending_antiraid_duration_msg", user_id))
            bot.send_message(
                m.chat.id,
                premium_prefix("Для исключения длительность не используется."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return

        punish["duration"] = int(duration)
        ar["punish"] = punish
        settings["antiraid"] = ar
        ch["settings"] = settings
        _mod_save()

        _pending_pop("pending_antiraid_duration", user_id)
        prompt_id = _pending_msg_pop("pending_antiraid_duration_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        ok_text = premium_prefix("✅ Время наказания анти-рейда установлено.")
        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"star:open:{chat_id}")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(
            m.chat.id,
            ok_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_ok,
        )
        return

    # ---------------- CUSTOM WARN DURATION ----------------
    warn_pending_cid = _pending_get("pending_warn_duration").get(str(user_id))
    if warn_pending_cid:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stw:open:{warn_pending_cid}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_warn_duration_msg",
                user_id,
                premium_prefix("Пришлите длительность текстом: 30m, 2h, 3д, 1н или 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        try:
            chat_id = int(warn_pending_cid)
        except Exception:
            _pending_pop("pending_warn_duration", user_id)
            return

        if not _check_allowed(chat_id):
            _pending_pop("pending_warn_duration", user_id)
            return

        raw = (m.text or "").strip()
        parsed_duration, consumed_tokens, invalid = _parse_duration_prefix(
            raw,
            allow_russian_duration=True,
            max_parts=3,
        )
        total_tokens = len(raw.split()) if raw else 0
        if invalid or parsed_duration is None or consumed_tokens == 0 or consumed_tokens != total_tokens:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stw:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_warn_duration_msg",
                user_id,
                premium_prefix("Неверный формат. Используйте до 3 интервалов: 30m, 1h 2m, 2mou 1d, навсегда."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        duration = int(parsed_duration)

        if duration != 0 and (duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stw:open:{chat_id}"))
            _replace_pending_ui(
                m.chat.id,
                "pending_warn_duration_msg",
                user_id,
                premium_prefix("Длительность должна быть от 1 минуты до 365 дней, либо 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

        ch = _mod_get_chat(chat_id)
        settings = ch.get("settings") or {}
        wp = settings.get("warn_punish") or {}
        ptype = (wp.get("type") or "mute").lower()
        if ptype not in ("mute", "ban"):
            _pending_pop("pending_warn_duration", user_id)
            _try_delete_private_prompt(m.chat.id, _pending_msg_pop("pending_warn_duration_msg", user_id))
            bot.send_message(
                m.chat.id,
                premium_prefix("Для типа наказания 'Исключение' длительность не используется."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return

        wp["duration"] = int(duration)
        settings["warn_punish"] = wp
        ch["settings"] = settings
        _mod_save()
        _pending_pop("pending_warn_duration", user_id)
        prompt_id = _pending_msg_pop("pending_warn_duration_msg", user_id)

        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        ok_text = premium_prefix("✅ Время установлено.")
        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"stw:open:{chat_id}")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(
            m.chat.id,
            ok_text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb_ok,
        )
        return

    # =========================================================
    # FIX #3:
    # - Любое сообщение "пришлите ..." / "ошибка" / "удалено" всегда заменяет предыдущее UI-сообщение
    # - Ошибки приходят с кнопкой "Отмена"
    # =========================================================

    # ---------------- MEDIA message ----------------
    emoji_x = '<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    
    if ct != "text":
        # 1) если есть pending_media — принимаем/ругаемся по медиа
        for sec in SECTION_KEYS:
            cid = _pending_get(f"pending_{sec}_media").get(str(user_id))
            if not cid:
                continue
            try:
                chat_id = int(cid)
            except Exception:
                _pending_pop(f"pending_{sec}_media", user_id)
                return

            if not _check_allowed(chat_id):
                _pending_pop(f"pending_{sec}_media", user_id)

                return

            payload = _extract_media_payload(m)
            if not payload:
                # удаляем prompt и показываем ошибку + cancel
                kb_err = _kb_error_cancel(f"st_{sec}_media_cancel:{chat_id}")
                _replace_pending_ui(
                    m.chat.id,
                    f"pending_{sec}_media_msg",
                    user_id,
                    f"{emoji_x} <b>Это медиа не поддерживается.</b>\nПришлите фото/видео/файл/музыку/gif.",
                    reply_markup=kb_err,
                    parse_mode="HTML",
                )
                return

            st = get_chat_settings(chat_id)
            sc = st.get(sec) or _default_section(False)

            # альбомы: пока упрощённо (как было у тебя)
            sc["media"] = [payload]
            sc["updated_at"] = _now_ts()

            st[sec] = sc
            CHAT_SETTINGS[str(chat_id)] = st
            save_chat_settings()

            _pending_pop(f"pending_{sec}_media", user_id)
            msg_id = _pending_msg_pop(f"pending_{sec}_media_msg", user_id)
            _try_delete_private_prompt(m.chat.id, msg_id)

            bot.reply_to(
                m,
                f"{emoji_ok} <b>Медиа {_section_title(sec)} установлено.</b>",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_only_back_kb(chat_id, sec),
            )
            return

        # ---- pending_cmd_media ----
        cid_cm = _pending_get("pending_cmd_media").get(str(user_id))
        if cid_cm:
            try:
                chat_id_cm = int(cid_cm)
            except Exception:
                _pending_pop("pending_cmd_media", user_id)
                return
            payload_cm = _extract_media_payload(m)
            if not payload_cm:
                kb_err = InlineKeyboardMarkup()
                kb_err.add(_build_cancel_btn(f"cmd_draft_media_cancel:{chat_id_cm}"))
                _replace_pending_ui(
                    m.chat.id, "pending_cmd_media_msg", user_id,
                    f"{emoji_x} <b>Этот тип медиа не поддерживается.</b>\nПришлите фото/видео/файл/музыку/gif.",
                    reply_markup=kb_err,
                )
                return
            draft_cm = _get_cmd_draft(chat_id_cm, user_id)
            draft_cm["media"] = [payload_cm]
            draft_cm["updated_at"] = _now_ts()
            _set_cmd_draft(chat_id_cm, user_id, draft_cm)
            _pending_pop("pending_cmd_media", user_id)
            msg_id_cm = _pending_msg_pop("pending_cmd_media_msg", user_id)
            _try_delete_private_prompt(m.chat.id, msg_id_cm)
            bot.reply_to(
                m,
                f"{emoji_ok} <b>Медиа команды установлено.</b>",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_build_cmd_draft_keyboard(chat_id_cm, user_id),
            )
            return

        # 2) если медиа прислали, а ожидается текст/кнопки — показываем ошибку и заменяем prompt
        for sec in SECTION_KEYS:
            cid = _pending_get(f"pending_{sec}_text").get(str(user_id))
            if cid:
                try:
                    chat_id = int(cid)
                except Exception:
                    _pending_pop(f"pending_{sec}_text", user_id)
                    return
                kb_err = _kb_error_cancel(f"st_{sec}_text_cancel:{chat_id}")
                _replace_pending_ui(
                    m.chat.id,
                    f"pending_{sec}_text_msg",
                    user_id,
                    f"{emoji_x} <b>Это не текст.</b>\nПришлите текстовое сообщение.",
                    reply_markup=kb_err,
                    parse_mode="HTML",
                )
                return

        for sec in SECTION_KEYS:
            cid = _pending_get(f"pending_{sec}_buttons").get(str(user_id))
            if cid:
                try:
                    chat_id = int(cid)
                except Exception:
                    _pending_pop(f"pending_{sec}_buttons", user_id)
                    return
                kb_err = _kb_error_cancel(f"st_{sec}_buttons_cancel:{chat_id}")
                _replace_pending_ui(
                    m.chat.id,
                    f"pending_{sec}_buttons_msg",
                    user_id,
                    f"{emoji_x} <b>Это не текст.</b>\nПришлите кнопки текстом по формату из инструкции.",
                    reply_markup=kb_err,
                    parse_mode="HTML",
                )
                return

        return

    # ---------------- TEXT message ----------------

    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'

    # ---- pending_cmd_name ----
    cid_cn = _pending_get("pending_cmd_name").get(str(user_id))
    if cid_cn:
        try:
            chat_id_cn = int(cid_cn)
        except Exception:
            _pending_pop("pending_cmd_name", user_id)
            return
        if ct != "text":
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id_cn}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_name_msg", user_id,
                                premium_prefix("Пришлите имя команды текстом."),
                                reply_markup=kb_err)
            return
        raw_name = (m.text or "").strip()
        if not raw_name or len(raw_name.split()) != 1:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id_cn}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_name_msg", user_id,
                                premium_prefix("Имя команды должно быть <b>одним словом</b> без пробелов."),
                                reply_markup=kb_err)
            return
        if len(raw_name) > _CMD_MAX_NAME_LEN:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id_cn}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_name_msg", user_id,
                                premium_prefix(f"Имя команды не должно превышать {_CMD_MAX_NAME_LEN} символов."),
                                reply_markup=kb_err)
            return
        if raw_name.lower() in _RESERVED_CMD_NAMES:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id_cn}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_name_msg", user_id,
                                premium_prefix("Это имя зарезервировано ботом. Выберите другое."),
                                reply_markup=kb_err)
            return
        cmds_existing = _get_commands_dict(chat_id_cn)
        if raw_name.lower() in cmds_existing:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id_cn}"))
            _replace_pending_ui(
                m.chat.id, "pending_cmd_name_msg", user_id,
                premium_prefix(f"Команда <code>{_html.escape(raw_name)}</code> уже существует."),
                reply_markup=kb_err,
            )
            return
        draft = _get_cmd_draft(chat_id_cn, user_id)
        draft["name"] = raw_name
        _set_cmd_draft(chat_id_cn, user_id, draft)
        _pending_pop("pending_cmd_name", user_id)
        msg_id_cn = _pending_msg_pop("pending_cmd_name_msg", user_id)
        _try_delete_private_prompt(m.chat.id, msg_id_cn)
        bot.send_message(
            m.chat.id,
            _render_cmd_draft(chat_id_cn, user_id),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_build_cmd_draft_keyboard(chat_id_cn, user_id),
        )
        return

    # ---- pending_cmd_delete ----
    cid_cd = _pending_get("pending_cmd_delete").get(str(user_id))
    if cid_cd:
        try:
            chat_id_cd = int(cid_cd)
        except Exception:
            _pending_pop("pending_cmd_delete", user_id)
            return
        if ct != "text":
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_del_cancel:{chat_id_cd}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_delete_msg", user_id,
                                premium_prefix("Пришлите имя команды текстом."),
                                reply_markup=kb_err)
            return
        raw_del = (m.text or "").strip()
        cmds_del = _get_commands_dict(chat_id_cd)
        cmd_key_del = raw_del.lower()
        if cmd_key_del not in cmds_del:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_del_cancel:{chat_id_cd}"))
            _replace_pending_ui(
                m.chat.id, "pending_cmd_delete_msg", user_id,
                premium_prefix(f"Команда <code>{_html.escape(raw_del)}</code> не найдена. Введите точное имя."),
                reply_markup=kb_err,
            )
            return
        cmd_name_display = cmds_del[cmd_key_del].get("name", raw_del)
        del cmds_del[cmd_key_del]
        _save_commands(chat_id_cd, cmds_del)
        _pending_pop("pending_cmd_delete", user_id)
        msg_id_cd = _pending_msg_pop("pending_cmd_delete_msg", user_id)
        _try_delete_private_prompt(m.chat.id, msg_id_cd)
        bot.reply_to(
            m,
            f'{emoji_ok} <b>Команда <code>{_html.escape(cmd_name_display)}</code> удалена.</b>',
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_build_commands_main_keyboard(chat_id_cd),
        )
        return

    # ---- pending_cmd_text ----
    cid_ct = _pending_get("pending_cmd_text").get(str(user_id))
    if cid_ct:
        try:
            chat_id_ct = int(cid_ct)
        except Exception:
            _pending_pop("pending_cmd_text", user_id)
            return
        if ct != "text":
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_draft_text_cancel:{chat_id_ct}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_text_msg", user_id,
                                f"{emoji_x} <b>Это не текст.</b>\nПришлите текстовое сообщение.",
                                reply_markup=kb_err)
            return
        text_custom, source, entities_ser = convert_section_text_from_message(m)
        draft = _get_cmd_draft(chat_id_ct, user_id)
        draft["text_custom"] = text_custom
        draft["source"] = source
        draft["entities"] = entities_ser
        draft["updated_at"] = _now_ts()
        _set_cmd_draft(chat_id_ct, user_id, draft)
        _pending_pop("pending_cmd_text", user_id)
        msg_id_ct = _pending_msg_pop("pending_cmd_text_msg", user_id)
        _try_delete_private_prompt(m.chat.id, msg_id_ct)
        bot.reply_to(
            m,
            f"{emoji_ok} <b>Текст команды установлен.</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_build_cmd_draft_keyboard(chat_id_ct, user_id),
        )
        return

    # ---- pending_cmd_buttons ----
    cid_cb = _pending_get("pending_cmd_buttons").get(str(user_id))
    if cid_cb:
        try:
            chat_id_cb = int(cid_cb)
        except Exception:
            _pending_pop("pending_cmd_buttons", user_id)
            return
        if ct != "text":
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_draft_buttons_cancel:{chat_id_cb}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_buttons_msg", user_id,
                                f"{emoji_x} <b>Это не текст.</b>\nПришлите кнопки текстом.",
                                reply_markup=kb_err)
            return
        try:
            rows, popups = parse_buttons_text(m.text or "", m.entities or [])
        except ButtonSyntaxError as err:
            kb_err = InlineKeyboardMarkup()
            kb_err.add(_build_cancel_btn(f"cmd_draft_buttons_cancel:{chat_id_cb}"))
            _replace_pending_ui(m.chat.id, "pending_cmd_buttons_msg", user_id,
                                premium_prefix(_format_button_syntax_error(err)),
                                reply_markup=kb_err)
            return
        draft = _get_cmd_draft(chat_id_cb, user_id)
        draft["buttons"] = {"rows": rows, "popups": popups}
        draft["updated_at"] = _now_ts()
        _set_cmd_draft(chat_id_cb, user_id, draft)
        _pending_pop("pending_cmd_buttons", user_id)
        msg_id_cb = _pending_msg_pop("pending_cmd_buttons_msg", user_id)
        _try_delete_private_prompt(m.chat.id, msg_id_cb)
        bot.reply_to(
            m,
            f"{emoji_ok} <b>Кнопки команды установлены.</b>",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_build_cmd_draft_keyboard(chat_id_cb, user_id),
        )
        return

    # 1) если есть pending_text — принимаем
    for sec in SECTION_KEYS:
        cid = _pending_get(f"pending_{sec}_text").get(str(user_id))
        if cid:
            try:
                chat_id = int(cid)
            except Exception:
                _pending_pop(f"pending_{sec}_text", user_id)
                return

            if not _check_allowed(chat_id):
                _pending_pop(f"pending_{sec}_text", user_id)
                return

            text_custom, source, entities_ser = convert_section_text_from_message(m)

            if not text_custom.strip():
                kb_err = _kb_error_cancel(f"st_{sec}_text_cancel:{chat_id}")
                _replace_pending_ui(
                    m.chat.id,
                    f"pending_{sec}_text_msg",
                    user_id,
                    f"{emoji_x} <b>Текст пустой.</b>\nПришлите непустой текст.",
                    reply_markup=kb_err,
                    parse_mode="HTML",
                )
                return

            st = get_chat_settings(chat_id)
            sc = st.get(sec) or _default_section(False)

            sc["text_custom"] = text_custom
            sc["source"] = source
            sc["entities"] = entities_ser
            sc["updated_at"] = _now_ts()

            st[sec] = sc
            CHAT_SETTINGS[str(chat_id)] = st
            save_chat_settings()

            _pending_pop(f"pending_{sec}_text", user_id)
            msg_id = _pending_msg_pop(f"pending_{sec}_text_msg", user_id)
            _try_delete_private_prompt(m.chat.id, msg_id)

            bot.reply_to(
                m,
                f"{emoji_ok} <b>Текст {_section_title(sec)} установлен.</b>",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_only_back_kb(chat_id, sec),
            )
            return

    # 2) если есть pending_buttons — принимаем
    for sec in SECTION_KEYS:
        cid = _pending_get(f"pending_{sec}_buttons").get(str(user_id))
        if cid:
            try:
                chat_id = int(cid)
            except Exception:
                _pending_pop(f"pending_{sec}_buttons", user_id)
                return

            if not _check_allowed(chat_id):
                _pending_pop(f"pending_{sec}_buttons", user_id)
                return

            # FIX #2: передаём entities, чтобы подхватить premium/custom emoji как icon
            try:
                rows, popups = parse_buttons_text(m.text or "", m.entities or [])
            except ButtonSyntaxError as err:
                kb_err = _kb_error_cancel(f"st_{sec}_buttons_cancel:{chat_id}")
                _replace_pending_ui(
                    m.chat.id,
                    f"pending_{sec}_buttons_msg",
                    user_id,
                    premium_prefix(_format_button_syntax_error(err)),
                    reply_markup=kb_err,
                    parse_mode="HTML",
                )
                return

            st = get_chat_settings(chat_id)
            sc = st.get(sec) or _default_section(False)

            sc["buttons"] = {"rows": rows, "popups": popups}
            sc["updated_at"] = _now_ts()

            st[sec] = sc
            CHAT_SETTINGS[str(chat_id)] = st
            save_chat_settings()

            _pending_pop(f"pending_{sec}_buttons", user_id)
            msg_id = _pending_msg_pop(f"pending_{sec}_buttons_msg", user_id)
            _try_delete_private_prompt(m.chat.id, msg_id)

            bot.reply_to(
                m,
                f"{emoji_ok} <b>Кнопки {_section_title(sec)} установлены.</b>",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_only_back_kb(chat_id, sec),
            )
            return

    # 3) если ожидается медиа, а пришёл текст — ошибка + cancel (и удаляем prompt)
    for sec in SECTION_KEYS:
        cid = _pending_get(f"pending_{sec}_media").get(str(user_id))
        if cid:
            try:
                chat_id = int(cid)
            except Exception:
                _pending_pop(f"pending_{sec}_media", user_id)
                return

            kb_err = _kb_error_cancel(f"st_{sec}_media_cancel:{chat_id}")
            _replace_pending_ui(
                m.chat.id,
                f"pending_{sec}_media_msg",
                user_id,
                f"{emoji_x} <b>Это не медиа.</b>\nПришлите фото/видео/файл/музыку/gif.",
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return

    # Delegate to antispam module for its pending states
    try:
        from antispam import handle_antispam_private_pending
        if handle_antispam_private_pending(m):
            return
    except ImportError:
        pass

    # Delegate to banned_words module for its pending states
    try:
        from banned_words import handle_banwords_private_pending
        if handle_banwords_private_pending(m):
            return
    except ImportError:
        pass

    return ContinueHandling()


# ------------------------------------------------------------
# ВЫЗОВ WELCOME / FAREWELL / RULES (group/supergroup)
# ------------------------------------------------------------

_BOT_USERNAME_LC: Optional[str] = None
_BOT_ID: Optional[int] = None

_RULES_ALIASES = {"rules", "правила"}


def _get_bot_username_lower() -> str:
    global _BOT_USERNAME_LC
    if _BOT_USERNAME_LC is None:
        try:
            me = get_bot_me()
            _BOT_USERNAME_LC = (getattr(me, "username", "") or "").lower()
        except Exception:
            _BOT_USERNAME_LC = ""
    return _BOT_USERNAME_LC or ""


def _get_bot_id() -> int:
    global _BOT_ID
    if _BOT_ID is None:
        try:
            me = get_bot_me()
            _BOT_ID = int(getattr(me, "id", 0) or 0)
        except Exception:
            _BOT_ID = 0
    return _BOT_ID or 0


def _is_rules_trigger(text: Optional[str]) -> bool:
    """
    Триггеры:
      /rules, rules, /правила, правила, .правила, .rules, !rules, !правила
    + поддержка /rules@MyBot (игнорируем команды для других ботов)
    """
    if not text:
        return False

    t = text.strip()
    if not t:
        return False

    tl = t.lower()

    # /rules or /rules@botusername
    if tl.startswith("/"):
        first = tl.split()[0]           # "/rules@xxx"
        cmd = first[1:]                 # "rules@xxx"
        cmd_name, sep, cmd_target = cmd.partition("@")

        if sep and cmd_target:
            my = _get_bot_username_lower()
            # если не смогли узнать username, лучше не реагировать на @команды
            if not my:
                return False
            if cmd_target.lower() != my:
                return False

        return cmd_name in _RULES_ALIASES

    # .rules / !rules (берём только первый токен)
    if tl[0] in (".", "!"):
        first = tl.split()[0]
        return first[1:] in _RULES_ALIASES

    # plain: "rules" / "правила" (только если сообщение состоит из одного слова)
    if " " in tl:
        return False

    return tl in _RULES_ALIASES


def _channel_post_has_command_name(chat_id: int, m: types.Message) -> bool:
    """Проверка: автопост канала начинается с имени встроенной/пользовательской команды."""
    raw = (getattr(m, "text", None) or getattr(m, "caption", None) or "").strip()
    if not raw:
        return False

    first = raw.split(maxsplit=1)[0].strip().lower()
    if not first:
        return False

    cmd_token = first.strip("`'\"«»()[]{}<>.,;:!?")
    for prefix in tuple(COMMAND_PREFIXES) + (".", "!"):
        if cmd_token.startswith(prefix):
            cmd_token = cmd_token[len(prefix):]
            break
    cmd_token = cmd_token.split("@", 1)[0].strip("`'\"«»()[]{}<>.,;:!?")
    if not cmd_token:
        return False

    if cmd_token in _RESERVED_CMD_NAMES:
        return True

    cmds = _get_commands_dict(chat_id)
    return cmd_token in cmds


def _send_section_payload(chat_id: int, sec: str, viewer_user, chat_title: str, viewer_uid_for_buttons: int, reply_to_message_id: Optional[int] = None) -> bool:
    """
    Унифицированная отправка секции:
      - конвертируем text_custom -> Telegram HTML
      - применяем переменные под viewer_user (если viewer_user не None)
      - добавляем медиа
      - строим inline-клаву (popup/rules/del будут доступны только viewer_uid_for_buttons)
    """
    st = get_chat_settings(chat_id)
    sc = st.get(sec) or _default_section(False)

    html_text = build_html_from_text_custom(sc.get("text_custom") or "")
    if html_text and viewer_user is not None:
        html_text = _apply_vars(html_text, chat_id, chat_title or str(chat_id), viewer_user)

    media = sc.get("media") or []
    rows = ((sc.get("buttons") or {}).get("rows")) or []
    popups = ((sc.get("buttons") or {}).get("popups")) or []
    kb = build_inline_keyboard_for_payload(sec, chat_id, rows, popups, viewer_uid_for_buttons)

    # если вообще пусто — не шлём ничего
    if not html_text and not media and not rows:
        return False

    _send_payload(chat_id, html_text, media, reply_markup=kb, reply_to_message_id=reply_to_message_id)
    return True


# ============================================================
# COMMANDS MODULE (пользовательские команды)
# ============================================================

_CMD_MAX_NAME_LEN = 30
_CMD_MAX_COUNT = 100
_CMD_USER_COOLDOWN_SECONDS = 10
_BOT_SCOPED_COMMANDS_KEY = "commands_by_bot"

_cmd_user_cooldown: dict[tuple[int, int], float] = {}
_cmd_user_cooldown_lock = threading.Lock()

# Зарезервированные имена команд (встроенные команды бота)
_RESERVED_CMD_NAMES = {
    "профиль", "наградить", "снять", "повысить", "понизить",
    "описание", "награды", "настройки", "правила", "закрыть", "открыть",
    "settings", "rules", "start", "ping", "пинг", "log", "broadcast", "sendpm",
    "adminstats", "adminstat", "админстата",
    "taglist", "settag", "removetag",
    "список", "тегов", "выдать", "тег", "снять",
    "promote", "demote", "staff", "ranks", "myrank",
    "mute", "ban", "kick", "warn", "unmute", "unban", "unwarn",
    "мут", "бан", "кик", "варн",
    "modlist", "warnlist", "banlist", "mutelist",
    "closechat", "openchat",
    "verify", "unverify", "vlist", "devverify", "devunverify", "devvlist",
    "clones", "clone_register", "clone_unlink", "newbot",
    "newguest",
    "del", "delete",
}


def _cmd_cooldown_check(chat_id: int, user_id: int) -> float:
    """Returns 0 if allowed, else remaining seconds to wait."""
    now = time.time()
    key = (int(chat_id), int(user_id))
    with _cmd_user_cooldown_lock:
        last = _cmd_user_cooldown.get(key, 0.0)
        remaining = _CMD_USER_COOLDOWN_SECONDS - (now - last)
        if remaining > 0:
            return remaining
        _cmd_user_cooldown[key] = now
        return 0.0


def _get_commands_dict(chat_id: int) -> dict:
    """Get the commands dict for a chat, scoped by current bot id."""
    st = get_chat_settings(chat_id)
    scoped = st.get(_BOT_SCOPED_COMMANDS_KEY)
    if not isinstance(scoped, dict):
        scoped = {}
        st[_BOT_SCOPED_COMMANDS_KEY] = scoped

    scope_key = str(_get_bot_id() or 0)
    cmds = scoped.get(scope_key)
    if not isinstance(cmds, dict):
        legacy_cmds = st.get("commands")
        if (
            not IS_GUEST_BOT
            and isinstance(legacy_cmds, dict)
            and legacy_cmds
            and not scoped
        ):
            cmds = dict(legacy_cmds)
        else:
            cmds = {}
        scoped[scope_key] = cmds

    if not IS_GUEST_BOT:
        st["commands"] = cmds
    CHAT_SETTINGS[str(chat_id)] = st
    return cmds


def _save_commands(chat_id: int, cmds: dict):
    st = get_chat_settings(chat_id)
    scoped = st.get(_BOT_SCOPED_COMMANDS_KEY)
    if not isinstance(scoped, dict):
        scoped = {}
        st[_BOT_SCOPED_COMMANDS_KEY] = scoped
    scoped[str(_get_bot_id() or 0)] = cmds
    if not IS_GUEST_BOT:
        st["commands"] = cmds
    CHAT_SETTINGS[str(chat_id)] = st
    save_chat_settings()


def _extract_guest_command_key(text: str, bot_username: str) -> Optional[str]:
    """Extract command key in guest mode from @username command-like formats."""
    raw = (text or "").strip()
    if not raw or not bot_username:
        return None

    my_username = bot_username.lower()
    tokens = raw.split()
    if not tokens:
        return None

    first = tokens[0]
    second = tokens[1] if len(tokens) > 1 else ""

    mention = ""
    cmd = ""
    if first.startswith("@"):
        mention = first[1:]
        cmd = second.lstrip("/")
    elif first.startswith("/") and "@" in first:
        cmd_part, _, mention_part = first[1:].partition("@")
        mention = mention_part
        cmd = cmd_part
    if not mention or mention.lower() != my_username:
        return None
    if not cmd:
        return None

    cmd = cmd.strip().lower()
    cmd = cmd.strip("`'\"«»()[]{}<>.,;:!?")
    if (
        not cmd
        or cmd.startswith("@")
        or cmd.startswith("/")
        or len(cmd) > _CMD_MAX_NAME_LEN
    ):
        return None
    return cmd


def _render_commands_main(chat_id: int) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    cmds = _get_commands_dict(chat_id)
    count = len(cmds)
    trigger_suffix = "и получить настроенное сообщение с текстом, медиа и кнопками."
    trigger_hint = (
        f"ввести <code>@username_бота имя_команды</code> {trigger_suffix}"
        if IS_GUEST_BOT
        else f"ввести команду {trigger_suffix}"
    )
    return (
        f'<tg-emoji emoji-id="5377844313575150051">📋</tg-emoji> <b>Команды</b>\n\n'
        f"Создайте пользовательские команды для этой группы. Пользователи смогут {trigger_hint}\n\n"
        f"<b>Количество команд:</b> <code>{count}</code> / <code>{_CMD_MAX_COUNT}</code>"
    )


def _build_commands_main_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    btn_list = InlineKeyboardButton("Список команд", callback_data=f"cmd_list:{chat_id}:0")
    try:
        btn_list.icon_custom_emoji_id = "5334882760735598374"
    except Exception:
        pass
    kb.add(btn_list)

    btn_add = InlineKeyboardButton("Добавить команду", callback_data=f"cmd_add:{chat_id}")
    try:
        btn_add.icon_custom_emoji_id = "5226945370684140473"
    except Exception:
        pass
    kb.add(btn_add)

    btn_del = InlineKeyboardButton("Удалить команду", callback_data=f"cmd_del_list:{chat_id}:0")
    try:
        btn_del.icon_custom_emoji_id = "5229113891081956317"
    except Exception:
        pass
    kb.add(btn_del)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:modules")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _render_commands_list_text(chat_id: int, page: int = 0) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    cmds = _get_commands_dict(chat_id)
    if not cmds:
        return f"{emoji_settings} <b>Список команд</b>\n\n<i>Нет созданных команд.</i>"

    page_size = 10
    keys = sorted(cmds.keys())
    total_pages = max(1, (len(keys) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = keys[start:start + page_size]

    header = f"{emoji_settings} <b>Список команд ({page + 1}/{total_pages})</b>\n"
    lines = [header]
    for i, k in enumerate(chunk, start=start + 1):
        c = cmds[k]
        access_label = "Только состав" if c.get("access") == "admin" else "Все пользователи"
        lines.append(
            f"{i}. <code>{_html.escape(c.get('name', k))}</code> — {_html.escape(access_label)}"
        )
    return "\n".join(lines)


def _build_commands_list_keyboard(chat_id: int, page: int = 0) -> InlineKeyboardMarkup:
    cmds = _get_commands_dict(chat_id)
    page_size = 10
    keys = sorted(cmds.keys())
    total_pages = max(1, (len(keys) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    kb = InlineKeyboardMarkup(row_width=2)
    nav_btns = []
    if page > 0:
        nav_btns.append(InlineKeyboardButton("◀", callback_data=f"cmd_list:{chat_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_btns.append(InlineKeyboardButton("▶", callback_data=f"cmd_list:{chat_id}:{page + 1}"))
    if nav_btns:
        kb.row(*nav_btns)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:commands")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)
    return kb


def _get_cmd_draft(chat_id: int, user_id: int) -> dict:
    key = f"cmd_draft_{chat_id}_{user_id}"
    d = CHAT_SETTINGS.get(key)
    if not isinstance(d, dict):
        d = {
            "name": "", "access": "all", "text_custom": "", "source": "plain",
            "entities": [], "media": [], "buttons": {"rows": [], "popups": []},
            "updated_at": 0,
        }
        CHAT_SETTINGS[key] = d
    return d


def _set_cmd_draft(chat_id: int, user_id: int, draft: dict):
    key = f"cmd_draft_{chat_id}_{user_id}"
    CHAT_SETTINGS[key] = draft
    save_chat_settings()


def _clear_cmd_draft(chat_id: int, user_id: int):
    key = f"cmd_draft_{chat_id}_{user_id}"
    CHAT_SETTINGS.pop(key, None)
    save_chat_settings()


def _render_cmd_draft(chat_id: int, user_id: int) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    draft = _get_cmd_draft(chat_id, user_id)
    name = draft.get("name") or ""
    access = draft.get("access") or "all"
    has_text = "есть" if draft.get("text_custom") else "нет"
    has_media = "есть" if draft.get("media") else "нет"
    has_btns = "есть" if (draft.get("buttons") or {}).get("rows") else "нет"
    access_label = "Только состав" if access == "admin" else "Все пользователи"
    name_str = f"<code>{_html.escape(name)}</code>" if name else "<i>не задано</i>"
    return (
        f"{emoji_settings} <b>Новая команда</b>\n\n"
        f"<b>Имя:</b> {name_str}\n"
        f"<b>Текст:</b> <code>{has_text}</code>\n"
        f"<b>Медиа:</b> <code>{has_media}</code>\n"
        f"<b>Кнопки:</b> <code>{has_btns}</code>\n"
        f"<b>Доступ:</b> {access_label}"
    )


def _build_cmd_draft_keyboard(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    draft = _get_cmd_draft(chat_id, user_id)
    access = draft.get("access") or "all"

    kb = InlineKeyboardMarkup(row_width=2)

    btn_text = InlineKeyboardButton("Текст", callback_data=f"cmd_draft_text:{chat_id}")
    try:
        btn_text.icon_custom_emoji_id = EMOJI_WELCOME_TEXT_ID
    except Exception:
        pass
    btn_media = InlineKeyboardButton("Медиа", callback_data=f"cmd_draft_media:{chat_id}")
    try:
        btn_media.icon_custom_emoji_id = EMOJI_WELCOME_MEDIA_ID
    except Exception:
        pass
    kb.add(btn_text, btn_media)

    btn_buttons = InlineKeyboardButton("Кнопки", callback_data=f"cmd_draft_buttons:{chat_id}")
    try:
        btn_buttons.icon_custom_emoji_id = EMOJI_WELCOME_BUTTONS_ID
    except Exception:
        pass
    kb.add(btn_buttons)

    if access == "all":
        btn_admin = InlineKeyboardButton("Только состав", callback_data=f"cmd_draft_access:{chat_id}:admin")
        btn_all = InlineKeyboardButton("»Все пользователи«", callback_data=f"cmd_draft_access:{chat_id}:all")
        try:
            btn_all.style = "primary"
        except Exception:
            pass
    else:
        btn_admin = InlineKeyboardButton("»Только состав«", callback_data=f"cmd_draft_access:{chat_id}:admin")
        try:
            btn_admin.style = "primary"
        except Exception:
            pass
        btn_all = InlineKeyboardButton("Все пользователи", callback_data=f"cmd_draft_access:{chat_id}:all")
    kb.add(btn_admin, btn_all)

    btn_del_draft = InlineKeyboardButton("Удалить", callback_data=f"cmd_draft_discard:{chat_id}")
    try:
        btn_del_draft.style = "danger"
    except Exception:
        pass
    btn_save = InlineKeyboardButton("Сохранить", callback_data=f"cmd_draft_save:{chat_id}")
    try:
        btn_save.style = "success"
    except Exception:
        pass
    kb.add(btn_del_draft, btn_save)

    btn_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:commands")
    try:
        btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        btn_back.style = "primary"
    except Exception:
        pass
    kb.add(btn_back)

    return kb


def _apply_cmd_vars(html_text: str, chat_id: int, chat_title: str,
                    user_obj, reply_user_obj=None) -> str:
    """Apply Commands-module-specific variables to html_text.
    [NOLINK] is detected and stripped from the text. Callers that need to know
    whether [NOLINK] was present should check the raw text before calling this function.
    """
    viewer = user_obj
    viewer_name = (
        getattr(viewer, "full_name", "") or getattr(viewer, "first_name", "") or ""
    ).strip() or "Участник"
    try:
        viewer_mention = mention_html_user(viewer)
    except Exception:
        viewer_mention = link_for_user(chat_id, viewer.id)

    result = (html_text or "")
    result = result.replace("[NOLINK]", "")
    result = result.replace("[LINK]", "")
    result = result.replace("[GROUP_NAME]", _html.escape(chat_title or str(chat_id)))
    result = result.replace("[USER_MENTION]", viewer_mention)
    result = result.replace("[USER_ID]", str(viewer.id))
    result = result.replace("[USER_NAME]", _html.escape(viewer_name))

    if reply_user_obj is not None:
        ru = reply_user_obj
        ru_name = (
            getattr(ru, "full_name", "") or getattr(ru, "first_name", "") or ""
        ).strip() or "Участник"
        try:
            ru_mention = mention_html_user(ru)
        except Exception:
            ru_mention = link_for_user(chat_id, ru.id)
        result = result.replace("[REPLY_MENTION]", ru_mention)
        result = result.replace("[REPLY_ID]", str(ru.id))
        result = result.replace("[REPLY_NAME]", _html.escape(ru_name))
    else:
        result = result.replace("[REPLY_MENTION]", "")
        result = result.replace("[REPLY_ID]", "")
        result = result.replace("[REPLY_NAME]", "")

    return result


# ---- Settings callbacks for Commands module ----

@bot.callback_query_handler(func=lambda c: c.data and (
    c.data.startswith("cmd_list:") or
    c.data.startswith("cmd_add:") or
    c.data.startswith("cmd_add_cancel:") or
    c.data.startswith("cmd_del_list:") or
    c.data.startswith("cmd_del_cancel:") or
    c.data.startswith("cmd_draft_text:") or
    c.data.startswith("cmd_draft_text_cancel:") or
    c.data.startswith("cmd_draft_media:") or
    c.data.startswith("cmd_draft_media_cancel:") or
    c.data.startswith("cmd_draft_buttons:") or
    c.data.startswith("cmd_draft_buttons_cancel:") or
    c.data.startswith("cmd_draft_access:") or
    c.data.startswith("cmd_draft_discard:") or
    c.data.startswith("cmd_draft_save:")
))
def cb_commands_settings(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != "private":
        bot.answer_callback_query(c.id)
        return

    # Extract chat_id — second colon-separated field for all these callbacks
    try:
        parts_d = data.split(":", 2)
        chat_id = int(parts_d[1])
    except Exception:
        bot.answer_callback_query(c.id)
        return

    _safe_answer_cq(c.id)
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        _notify_access_denied(msg_chat.id, err)
        return

    def _edit(text, kb):
        edited = _safe_edit_message_html(msg_chat.id, c.message.message_id, text, kb)
        if not edited:
            bot.send_message(msg_chat.id, text, parse_mode="HTML",
                             disable_web_page_preview=True, reply_markup=kb)

    # ---- cmd_list ----
    if data.startswith("cmd_list:"):
        try:
            page = int(parts_d[2]) if len(parts_d) > 2 else 0
        except Exception:
            page = 0
        _edit(_render_commands_list_text(chat_id, page), _build_commands_list_keyboard(chat_id, page))
        return

    # ---- cmd_add ----
    if data.startswith("cmd_add:"):
        cmds = _get_commands_dict(chat_id)
        if len(cmds) >= _CMD_MAX_COUNT:
            _safe_answer_cq(c.id, f"Достигнут лимит команд ({_CMD_MAX_COUNT}).", show_alert=True)
            return
        _pending_put("pending_cmd_name", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_cmd_name_msg", user.id,
                           also_msg_id=c.message.message_id)
        body = (
            f'<tg-emoji emoji-id="5226945370684140473">➕</tg-emoji> <b>Создание команды</b>\n\n'
            "Пришлите <b>имя</b> новой команды.\n"
            f"<i>Одно слово, до {_CMD_MAX_NAME_LEN} символов. "
            + (
                "Команда срабатывает, когда пользователь напишет «@username_бота имя_команды» в группе.</i>"
                if IS_GUEST_BOT
                else "Команда срабатывает, когда пользователь напишет только это слово в группе.</i>"
            )
        )
        kb_n = InlineKeyboardMarkup()
        kb_n.add(_build_cancel_btn(f"cmd_add_cancel:{chat_id}"))
        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_n)
        _pending_msg_set("pending_cmd_name_msg", user.id, sent.message_id)
        return

    # ---- cmd_add_cancel ----
    if data.startswith("cmd_add_cancel:"):
        _pending_pop("pending_cmd_name", user.id)
        _pending_msg_pop("pending_cmd_name_msg", user.id)
        _clear_cmd_draft(chat_id, user.id)
        _edit(_render_commands_main(chat_id), _build_commands_main_keyboard(chat_id))
        return

    # ---- cmd_del_list ----
    if data.startswith("cmd_del_list:"):
        cmds = _get_commands_dict(chat_id)
        if not cmds:
            _edit(_render_commands_main(chat_id), _build_commands_main_keyboard(chat_id))
            return
        _pending_put("pending_cmd_delete", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_cmd_delete_msg", user.id,
                           also_msg_id=c.message.message_id)
        emoji_s = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
        keys = sorted(cmds.keys())
        cmd_names_list = "\n".join(
            f"{i + 1}. <code>{_html.escape(cmds[k].get('name', k))}</code>"
            for i, k in enumerate(keys)
        )
        body = (
            f"{emoji_s} <b>Удалить команду</b>\n\n"
            f"Введите <b>имя команды</b> для удаления.\n\n"
            f"<b>Список команд:</b>\n{cmd_names_list}"
        )
        kb_d = InlineKeyboardMarkup()
        kb_d.add(_build_cancel_btn(f"cmd_del_cancel:{chat_id}"))
        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_d)
        _pending_msg_set("pending_cmd_delete_msg", user.id, sent.message_id)
        return

    # ---- cmd_del_cancel ----
    if data.startswith("cmd_del_cancel:"):
        _pending_pop("pending_cmd_delete", user.id)
        _pending_msg_pop("pending_cmd_delete_msg", user.id)
        _edit(_render_commands_main(chat_id), _build_commands_main_keyboard(chat_id))
        return

    # ---- cmd_draft_text ----
    if data.startswith("cmd_draft_text:"):
        _pending_put("pending_cmd_text", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_cmd_text_msg", user.id,
                           also_msg_id=c.message.message_id)
        emoji_t = f'<tg-emoji emoji-id="{EMOJI_WELCOME_TEXT_ID}">📝</tg-emoji>'
        body = (
            f"{emoji_t} <b>Пришлите текст команды.</b>\n\n"
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
            "то Telegram-форматирование может быть проигнорировано."
        )
        kb_t = InlineKeyboardMarkup()
        kb_t.add(_build_cancel_btn(f"cmd_draft_text_cancel:{chat_id}"))
        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_t)
        _pending_msg_set("pending_cmd_text_msg", user.id, sent.message_id)
        return

    # ---- cmd_draft_text_cancel ----
    if data.startswith("cmd_draft_text_cancel:"):
        _pending_pop("pending_cmd_text", user.id)
        _pending_msg_pop("pending_cmd_text_msg", user.id)
        _edit(_render_cmd_draft(chat_id, user.id), _build_cmd_draft_keyboard(chat_id, user.id))
        return

    # ---- cmd_draft_media ----
    if data.startswith("cmd_draft_media:"):
        _pending_put("pending_cmd_media", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_cmd_media_msg", user.id,
                           also_msg_id=c.message.message_id)
        emoji_m = f'<tg-emoji emoji-id="{EMOJI_WELCOME_MEDIA_ID}">🖼</tg-emoji>'
        body = (
            f"{emoji_m} <b>Пришлите медиа для команды.</b>\n\n"
            "<b>Поддерживается:</b>\n"
            "• Фото\n• Видео\n• Файл\n• Музыка\n• GIF\n\n"
            "<i>Подпись отдельно не задаётся.</i>\n"
            "Если у вас есть текст — он будет автоматически использоваться как описание, когда медиа есть."
        )
        kb_m = InlineKeyboardMarkup()
        kb_m.add(_build_cancel_btn(f"cmd_draft_media_cancel:{chat_id}"))
        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_m)
        _pending_msg_set("pending_cmd_media_msg", user.id, sent.message_id)
        return

    # ---- cmd_draft_media_cancel ----
    if data.startswith("cmd_draft_media_cancel:"):
        _pending_pop("pending_cmd_media", user.id)
        _pending_msg_pop("pending_cmd_media_msg", user.id)
        _edit(_render_cmd_draft(chat_id, user.id), _build_cmd_draft_keyboard(chat_id, user.id))
        return

    # ---- cmd_draft_buttons ----
    if data.startswith("cmd_draft_buttons:"):
        _pending_put("pending_cmd_buttons", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_cmd_buttons_msg", user.id,
                           also_msg_id=c.message.message_id)
        emoji_b = f'<tg-emoji emoji-id="{EMOJI_WELCOME_BUTTONS_ID}">🔘</tg-emoji>'
        body = (
            f"{emoji_b} <b>Пришлите кнопки для команды.</b>\n\n"
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
            "<code>#b Название - example.com</code> (цвет, зависящий от темы пользователя)\n\n"
            "<b>Лимиты:</b>\n"
            f"• 1–{MAX_PER_ROW} кнопки в ряду\n"
            f"• до {MAX_ROWS} рядов\n"
            f"• до {MAX_TOTAL_BTNS} кнопок всего\n"
            "• до 1 премиум-эмодзи в кнопке (эмодзи может быть только в начале названия)"
        )
        kb_b = InlineKeyboardMarkup()
        kb_b.add(_build_cancel_btn(f"cmd_draft_buttons_cancel:{chat_id}"))
        sent = bot.send_message(msg_chat.id, body, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_b)
        _pending_msg_set("pending_cmd_buttons_msg", user.id, sent.message_id)
        return

    # ---- cmd_draft_buttons_cancel ----
    if data.startswith("cmd_draft_buttons_cancel:"):
        _pending_pop("pending_cmd_buttons", user.id)
        _pending_msg_pop("pending_cmd_buttons_msg", user.id)
        _edit(_render_cmd_draft(chat_id, user.id), _build_cmd_draft_keyboard(chat_id, user.id))
        return

    # ---- cmd_draft_access ----
    if data.startswith("cmd_draft_access:"):
        try:
            _, _, access_val = data.split(":", 2)
        except Exception:
            return
        draft = _get_cmd_draft(chat_id, user.id)
        draft["access"] = "admin" if access_val == "admin" else "all"
        _set_cmd_draft(chat_id, user.id, draft)
        _edit(_render_cmd_draft(chat_id, user.id), _build_cmd_draft_keyboard(chat_id, user.id))
        return

    # ---- cmd_draft_discard ----
    if data.startswith("cmd_draft_discard:"):
        _clear_cmd_draft(chat_id, user.id)
        _pending_pop("pending_cmd_name", user.id)
        _pending_pop("pending_cmd_text", user.id)
        _pending_pop("pending_cmd_media", user.id)
        _pending_pop("pending_cmd_buttons", user.id)
        _edit(_render_commands_main(chat_id), _build_commands_main_keyboard(chat_id))
        return

    # ---- cmd_draft_save ----
    if data.startswith("cmd_draft_save:"):
        draft = _get_cmd_draft(chat_id, user.id)
        has_content = (
            bool(draft.get("text_custom")) or
            bool(draft.get("media"))
        )
        if not has_content:
            _safe_answer_cq(
                c.id,
                "Нельзя сохранить пустую команду. Добавьте текст или медиа.",
                show_alert=True,
            )
            return
        name = (draft.get("name") or "").strip()
        if not name:
            _safe_answer_cq(
                c.id,
                "Имя команды не задано. Сначала введите имя команды.",
                show_alert=True,
            )
            return
        cmd_key = name.lower()
        cmds = _get_commands_dict(chat_id)
        cmds[cmd_key] = {
            "name": name,
            "access": draft.get("access") or "all",
            "text_custom": draft.get("text_custom") or "",
            "source": draft.get("source") or "plain",
            "entities": draft.get("entities") or [],
            "media": draft.get("media") or [],
            "buttons": draft.get("buttons") or {"rows": [], "popups": []},
            "updated_at": int(time.time()),
        }
        _save_commands(chat_id, cmds)
        _clear_cmd_draft(chat_id, user.id)
        _pending_pop("pending_cmd_name", user.id)
        _pending_pop("pending_cmd_text", user.id)
        _pending_pop("pending_cmd_media", user.id)
        _pending_pop("pending_cmd_buttons", user.id)
        bot.send_message(
            msg_chat.id,
            f'✅ Команда <code>{_html.escape(name)}</code> сохранена.',
            parse_mode="HTML",
        )
        _edit(_render_commands_main(chat_id), _build_commands_main_keyboard(chat_id))
        return


# ---- cmd: navigation button callback (in groups) ----

def _msg_has_media(msg: types.Message) -> bool:
    """Return True if the message contains any media (photo, video, document, audio, animation, etc.)."""
    return bool(
        msg.photo or msg.video or msg.document or
        msg.audio or msg.animation or msg.voice or msg.video_note
    )


def _make_input_media(media_item: dict, caption: Optional[str]) -> Optional[types.InputMedia]:
    """Build an InputMedia object for editMessageMedia from a media dict."""
    t = media_item.get("type")
    fid = media_item.get("file_id")
    pm = "HTML" if caption else None
    if t == "photo":
        return types.InputMediaPhoto(media=fid, caption=caption, parse_mode=pm)
    if t == "video":
        return types.InputMediaVideo(media=fid, caption=caption, parse_mode=pm)
    if t == "document":
        return types.InputMediaDocument(media=fid, caption=caption, parse_mode=pm)
    if t == "audio":
        return types.InputMediaAudio(media=fid, caption=caption, parse_mode=pm)
    if t == "animation":
        return types.InputMediaAnimation(media=fid, caption=caption, parse_mode=pm)
    return None


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("cn:"))
def cb_cmd_navigate(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    # cn:{chat_id}:{owner_uid}:{cmd_name}
    data = c.data or ""
    try:
        _, chat_id_s, owner_uid_s, cmd_name = data.split(":", 3)
        chat_id = int(chat_id_s)
        owner_uid = int(owner_uid_s)
    except Exception:
        bot.answer_callback_query(c.id)
        return

    if c.from_user.id != owner_uid:
        bot.answer_callback_query(c.id, "Недоступно.", show_alert=True)
        return

    cmds = _get_commands_dict(chat_id)
    cmd_data = cmds.get(cmd_name.lower())
    if not cmd_data:
        bot.answer_callback_query(c.id, "Команда не найдена.", show_alert=True)
        return

    bot.answer_callback_query(c.id)

    try:
        chat_obj = tg_get_chat(chat_id)
        chat_title = chat_obj.title or ""
    except Exception:
        chat_title = ""

    invoker = c.from_user
    raw_text_custom = cmd_data.get("text_custom") or ""
    link_preview = "[LINK]" in raw_text_custom
    disable_preview = not link_preview
    html_text = build_html_from_text_custom(raw_text_custom)
    if html_text:
        html_text = _apply_cmd_vars(html_text, chat_id, chat_title, invoker, None)

    media = cmd_data.get("media") or []
    rows = ((cmd_data.get("buttons") or {}).get("rows")) or []
    popups = ((cmd_data.get("buttons") or {}).get("popups")) or []
    kb = build_inline_keyboard_for_payload("cmd", chat_id, rows, popups, invoker.id)

    cur_msg = c.message
    current_has_media = _msg_has_media(cur_msg)

    if not current_has_media and not media:
        # Both text-only: edit in place
        edited = _safe_edit_message_html(
            cur_msg.chat.id, cur_msg.message_id,
            html_text or "\u2063", kb,
            disable_web_page_preview=disable_preview,
        )
        if not edited:
            _send_payload(chat_id, html_text, media, reply_markup=kb,
                          disable_web_page_preview=disable_preview)
    elif current_has_media and media and len(media) == 1:
        # Current has media, new has single media: try editMessageMedia
        input_media = _make_input_media(media[0], html_text or None)
        edited = False
        if input_media is not None:
            try:
                bot.edit_message_media(
                    input_media,
                    chat_id=cur_msg.chat.id,
                    message_id=cur_msg.message_id,
                    reply_markup=kb,
                )
                edited = True
            except Exception:
                pass
        if not edited:
            try:
                bot.delete_message(cur_msg.chat.id, cur_msg.message_id)
            except Exception:
                pass
            _send_payload(chat_id, html_text, media, reply_markup=kb,
                          disable_web_page_preview=disable_preview)
    else:
        # text→media, media→text, or media→album: delete old + send new
        try:
            bot.delete_message(cur_msg.chat.id, cur_msg.message_id)
        except Exception:
            pass
        _send_payload(chat_id, html_text, media, reply_markup=kb,
                      disable_web_page_preview=disable_preview)


# ---- Group message handler for custom commands ----

@bot.message_handler(func=lambda m: (
    m.chat.type in ("group", "supergroup") and
    bool(m.text)
))
def on_custom_command_message(m: types.Message):
    # Посты от привязанного канала или анонимного администратора — пропускаем
    if not m.from_user or _is_channel_sender(m) or _is_anonymous_admin(m):
        return ContinueHandling()
    if should_ignore_text_triggers(m):
        return ContinueHandling()

    if not is_group_approved(m.chat.id):
        return ContinueHandling()

    text = (m.text or "").strip()
    if not text:
        return ContinueHandling()

    if IS_GUEST_BOT:
        bot_username = _get_bot_username_lower()
        if not bot_username:
            logger.warning("[GUEST CMD] Cannot process custom command: bot username is unknown.")
            return ContinueHandling()
        cmd_key = _extract_guest_command_key(text, bot_username)
        if not cmd_key:
            return ContinueHandling()
    else:
        parts = text.split()
        if len(parts) != 1:
            return ContinueHandling()
        cmd_key = text.lower()
        if cmd_key.startswith("/") or len(cmd_key) > _CMD_MAX_NAME_LEN:
            return ContinueHandling()

    cmds = _get_commands_dict(m.chat.id)
    if not cmds:
        return ContinueHandling()

    cmd_data = cmds.get(cmd_key)
    if not cmd_data:
        return ContinueHandling()

    # Access check
    access = cmd_data.get("access") or "all"
    if access == "admin":
        rank = get_user_rank(m.chat.id, m.from_user.id)
        if rank <= 0:
            try:
                member = tg_get_chat_member(m.chat.id, m.from_user.id)
                if getattr(member, "status", "") not in ("administrator", "creator"):
                    return ContinueHandling()
            except Exception:
                return ContinueHandling()

    # Per-user cooldown (10 seconds)
    remaining = _cmd_cooldown_check(m.chat.id, m.from_user.id)
    if remaining > 0:
        return ContinueHandling()

    # Determine reply target and reply_user for variables
    reply_user = None
    target_reply_id: Optional[int] = None
    if m.reply_to_message:
        target_reply_id = m.reply_to_message.message_id
        try:
            if m.reply_to_message.from_user:
                reply_user = m.reply_to_message.from_user
        except Exception:
            pass
    else:
        target_reply_id = m.message_id

    chat_title = m.chat.title or ""

    raw_text_custom = cmd_data.get("text_custom") or ""
    link_preview = "[LINK]" in raw_text_custom
    disable_preview = not link_preview
    html_text = build_html_from_text_custom(raw_text_custom)
    if html_text:
        html_text = _apply_cmd_vars(html_text, m.chat.id, chat_title, m.from_user, reply_user)

    media = cmd_data.get("media") or []
    rows = ((cmd_data.get("buttons") or {}).get("rows")) or []
    popups = ((cmd_data.get("buttons") or {}).get("popups")) or []
    kb = build_inline_keyboard_for_payload("cmd", m.chat.id, rows, popups, m.from_user.id)

    if not html_text and not media and not rows:
        return ContinueHandling()

    try:
        _send_payload(m.chat.id, html_text, media, reply_markup=kb,
                      disable_web_page_preview=disable_preview,
                      reply_to_message_id=target_reply_id)
    except Exception:
        try:
            _send_payload(m.chat.id, html_text, media, reply_markup=kb,
                          disable_web_page_preview=disable_preview)
        except Exception:
            pass


# ---------------- WELCOME ----------------

@bot.message_handler(content_types=["new_chat_members"])
def on_welcome_new_members(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return ContinueHandling()

    chat_id = m.chat.id
    bot_id = _get_bot_id()

    # Anti-raid check: run for every new member before welcome message
    for u in (m.new_chat_members or []):
        try:
            if bot_id and u.id == bot_id:
                continue
        except Exception:
            pass
        try:
            _antiraid_runtime_check(chat_id, u)
        except Exception:
            pass

    st = get_chat_settings(chat_id)
    sc = st.get("welcome") or _default_section(False)

    if not bool(sc.get("enabled")):
        return ContinueHandling()

    # If anti-raid is currently triggered, suppress welcome messages
    if _antiraid_is_active(chat_id):
        return ContinueHandling()

    title = m.chat.title or ""

    for u in (m.new_chat_members or []):
        try:
            if bot_id and u.id == bot_id:
                continue  # не приветствуем сами себя
        except Exception:
            pass

        _send_section_payload(
            chat_id=chat_id,
            sec="welcome",
            viewer_user=u,
            chat_title=title,
            viewer_uid_for_buttons=int(getattr(u, "id", 0) or 0),
        )

    # Log joins to log channel
    for u in (m.new_chat_members or []):
        try:
            if bot_id and u.id == bot_id:
                continue
            uid = int(getattr(u, "id", 0) or 0)
            if uid <= 0:
                continue
            uname = _html.escape(getattr(u, "first_name", None) or str(uid))
            ulink = f'<a href="tg://user?id={uid}">{uname}</a>'
            log_text = (
                f"<b>#ВХОД</b>\n"
                f"<b>Группа:</b> {_html.escape(m.chat.title or str(chat_id))}\n"
                f"<b>Пользователь:</b> {ulink} (<code>{uid}</code>)"
            )
            send_log_event(chat_id, "join", log_text)
        except Exception:
            pass

    # ВАЖНО: даём дойти до cleanup_delete_system_runtime (удаление system messages по типам)
    return ContinueHandling()

# ---------------- FAREWELL ----------------

@bot.message_handler(content_types=["left_chat_member"])
def on_farewell_left_member(m: types.Message):
    if m.chat.type not in ("group", "supergroup"):
        return ContinueHandling()

    chat_id = m.chat.id
    st = get_chat_settings(chat_id)
    sc = st.get("farewell") or _default_section(False)

    if not bool(sc.get("enabled")):
        return ContinueHandling()

    left = getattr(m, "left_chat_member", None) or getattr(m, "from_user", None)
    if not left:
        return ContinueHandling()

    left_id = int(getattr(left, "id", 0) or 0)
    if left_id and _is_farewell_suppressed(chat_id, left_id):
        return ContinueHandling()

    bot_id = _get_bot_id()
    try:
        if bot_id and left.id == bot_id:
            return ContinueHandling()  # если выгнали бота — не пытаемся слать farewell
    except Exception:
        pass

    # Log leave to log channel
    try:
        uid = int(left_id)
        if uid > 0:
            lname = _html.escape(getattr(left, "first_name", None) or str(uid))
            llink = f'<a href="tg://user?id={uid}">{lname}</a>'
            log_text = (
                f"<b>#ВЫХОД</b>\n"
                f"<b>Группа:</b> {_html.escape(m.chat.title or str(chat_id))}\n"
                f"<b>Пользователь:</b> {llink} (<code>{uid}</code>)"
            )
            send_log_event(chat_id, "leave", log_text)
    except Exception:
        pass

    _send_section_payload(
        chat_id=chat_id,
        sec="farewell",
        viewer_user=left,
        chat_title=m.chat.title or "",
        viewer_uid_for_buttons=left_id,
    )

    return ContinueHandling()

# ---------------- ПЕРВЫЙ КОММЕНТАРИЙ ----------------

# {(chat_id, message_id)} — сообщения, к которым уже оставлен первый комментарий
_FIRST_COMMENT_SENT: set[tuple[int, int]] = set()
_FIRST_COMMENT_LOCK = threading.Lock()


@bot.message_handler(
    content_types=[
        "text", "photo", "video", "document", "audio", "animation",
        "sticker", "voice", "video_note", "poll", "dice",
    ],
    func=lambda m: (
        m.chat.type in ("group", "supergroup")
        and bool(getattr(m, "is_automatic_forward", False))
    ),
)
def on_channel_post_in_group(m: types.Message):
    """Оставляет первый комментарий к посту канала, автоматически пересланному в группу."""
    chat_id = m.chat.id
    msg_id = m.message_id

    if not is_group_approved(chat_id):
        return ContinueHandling()

    st = get_chat_settings(chat_id)
    sc = st.get("first_comment") or _default_section(False)
    if not bool(sc.get("enabled")):
        return ContinueHandling()

    # Если пост похож на команду (встроенную или пользовательскую), не оставляем первый комментарий.
    if _channel_post_has_command_name(chat_id, m):
        return ContinueHandling()

    # Дедупликация: не комментировать одно и то же сообщение дважды
    key = (chat_id, msg_id)
    with _FIRST_COMMENT_LOCK:
        if key in _FIRST_COMMENT_SENT:
            return ContinueHandling()
        _FIRST_COMMENT_SENT.add(key)
        # Ограничиваем размер кеша: при превышении убираем ~10% старых записей
        if len(_FIRST_COMMENT_SENT) > 2000:
            to_remove = []
            for k in _FIRST_COMMENT_SENT:
                if k != key:
                    to_remove.append(k)
                    if len(to_remove) >= 200:
                        break
            for k in to_remove:
                _FIRST_COMMENT_SENT.discard(k)

    try:
        _send_section_payload(
            chat_id=chat_id,
            sec="first_comment",
            viewer_user=None,
            chat_title=m.chat.title or "",
            viewer_uid_for_buttons=0,
            reply_to_message_id=msg_id,
        )
    except Exception:
        pass

    return ContinueHandling()

# ---------------- RULES triggers ----------------

@bot.message_handler(
    content_types=["text"],
    func=lambda m: (
        m.chat.type in ("group", "supergroup")
        and not should_ignore_text_triggers(m)
        and _is_rules_trigger(getattr(m, "text", None))
    )
)
def on_rules_trigger(m: types.Message):
    chat_id = m.chat.id
    st = get_chat_settings(chat_id)
    rules = st.get("rules") or _default_section(False)

    # Правила по команде/словам — только если включены в настройках
    if not bool(rules.get("enabled")):
        return

    ok = _send_section_payload(
        chat_id=chat_id,
        sec="rules",
        viewer_user=m.from_user,
        chat_title=m.chat.title or "",
        viewer_uid_for_buttons=int(getattr(m.from_user, "id", 0) or 0),
    )

    if not ok:
        # Если включены, но пустые — даём понятный ответ
        bot.reply_to(m, "Правила не заданы.", disable_web_page_preview=True)
        

def _cleanup_cmd_enabled(chat_id: int, sign: str) -> bool:
    try:
        st = get_chat_settings(chat_id)
        cl = st.get("cleanup") or {}
        cmds = cl.get("commands") or {}
        return bool(cmds.get(sign))
    except Exception:
        return False


def _cleanup_sys_enabled(chat_id: int, ct: str) -> bool:
    try:
        st = get_chat_settings(chat_id)
        cl = st.get("cleanup") or {}

        # новый формат
        sysd = cl.get("system") or {}
        if isinstance(sysd, dict):
            return bool(sysd.get(ct))

        # fallback на старый формат (если вдруг остался)
        sm = cl.get("system_messages", False)
        if isinstance(sm, bool):
            return sm

        return False
    except Exception:
        return False


_ANTIFLOOD_LOCK = threading.Lock()
_ANTIFLOOD_TIMELINE: dict[tuple[int, int], list[tuple[int, int]]] = {}
_ANTIFLOOD_LAST_PUNISH: dict[tuple[int, int], int] = {}
ANTIFLOOD_TRACK_CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio", "animation",
    "sticker", "voice", "video_note",
]


def _antiflood_get_effective_settings(chat_id: int) -> dict:
    af = ((_mod_get_chat(chat_id).get("settings") or {}).get("antiflood") or {})
    punish = af.get("punish") or {}
    try:
        period = int(af.get("period") or 10)
    except Exception:
        period = 10
    try:
        messages = int(af.get("messages") or 6)
    except Exception:
        messages = 6

    ptype = str(punish.get("type") or "mute").strip().lower()
    if ptype not in ("mute", "ban", "kick", "warn"):
        ptype = "mute"

    return {
        "enabled": bool(af.get("enabled", False)),
        "delete_messages": bool(af.get("delete_messages", False)),
        "period": max(3, min(300, period)),
        "messages": max(2, min(50, messages)),
        "punish": {
            "type": ptype,
            "duration": punish.get("duration"),
            "reason": str(punish.get("reason") or "").strip(),
        },
    }


def _antiflood_target_allowed(chat_id: int, user_obj: types.User) -> bool:
    if not user_obj:
        return False

    uid = int(getattr(user_obj, "id", 0) or 0)
    if uid <= 0:
        return False

    # Разработчик бота и dev-пользователи не попадают под антифлуд.
    if is_owner(user_obj) or is_dev(user_obj):
        return False

    # Пользователи с назначенными ролями (1-5) не попадают под антифлуд.
    try:
        if int(get_user_rank(chat_id, uid) or 0) > 0:
            return False
    except Exception:
        pass

    try:
        if bool(getattr(user_obj, "is_bot", False)):
            return False
    except Exception:
        return False

    try:
        if uid == _get_bot_id():
            return False
    except Exception:
        return False

    try:
        member = bot.get_chat_member(chat_id, uid)
        if getattr(member, "status", "") in ("administrator", "creator"):
            return False
    except Exception:
        pass

    return True


def _antiflood_send_punish_message(
    chat_id: int,
    action_kind: str,
    action_id: str,
    target_id: int,
    actor_id: int,
    until_ts: int | None,
    warn_count: int | None = None,
    warn_limit: int | None = None,
) -> None:
    from moderation import _fmt_time
    punish_label = {
        "mute": "Ограничение",
        "ban": "Блокировка",
        "kick": "Исключение",
        "warn": "Предупреждение",
    }.get(action_kind, "Наказание")

    if action_kind == "warn" and warn_count is not None and warn_limit is not None:
        punish_label = f"Предупреждение [{warn_count}/{warn_limit}]"

    emoji_p = f'<tg-emoji emoji-id="{EMOJI_PUNISHMENT_ID}">⚠️</tg-emoji>'
    target_name = link_for_user(chat_id, target_id)
    actor_name = link_for_user(chat_id, actor_id)

    lines = [
        f"{emoji_p} <b>Пользователь</b> {target_name} <b>наказан.</b>",
        f"<b>Наказание:</b> {punish_label}",
    ]

    if action_kind in ("mute", "ban"):
        if until_ts and int(until_ts) > 0:
            lines.append(f"<b>Истекает:</b> {_fmt_time(int(until_ts))}")
        else:
            lines.append("<b>Истекает:</b> навсегда")

    lines.append(f"<b>Причина:</b> Флуд")
    lines.extend(["", f"<b>Администратор:</b> {actor_name}"])
    text = "\n".join(lines)

    kb = None
    if action_kind in ("mute", "ban", "warn"):
        btn_text = {
            "mute": "Снять ограничение",
            "ban": "Разблокировать",
            "warn": "Снять предупреждение",
        }[action_kind]
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(
            btn_text,
            callback_data=f"punish_un:{chat_id}:{action_kind}:{target_id}:{action_id}",
            icon_custom_emoji_id=str(EMOJI_UNPUNISH_ID),
        ))

    try:
        bot.send_message(
            chat_id,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        pass


def _antiflood_try_delete_messages(chat_id: int, message_ids: list[int]) -> int:
    if not message_ids:
        return 0
    if not _bot_can_delete_messages(chat_id):
        return 0

    deleted = 0
    uniq_ids = list(dict.fromkeys(int(mid) for mid in message_ids if int(mid) > 0))
    if len(uniq_ids) > 80:
        uniq_ids = uniq_ids[-80:]

    for mid in uniq_ids:
        try:
            bot.delete_message(chat_id, mid)
            deleted += 1
        except Exception:
            pass
    return deleted


def _antiflood_apply_punishment(
    chat_id: int,
    target_user: types.User,
    af: dict,
    message_ids: list[int] | None = None,
) -> bool:
    target_id = int(getattr(target_user, "id", 0) or 0)
    if target_id <= 0:
        return False

    punish = af.get("punish") or {}
    ptype = str(punish.get("type") or "mute").lower()
    duration_raw = punish.get("duration")
    reason_custom = str(punish.get("reason") or "").strip()
    reason = reason_custom or (
        f"Антифлуд: отправлено {int(af['messages'])}+ сообщений за {int(af['period'])} сек."
    )

    actor_id = _get_bot_id()
    if actor_id <= 0:
        try:
            actor_id = int(getattr(get_bot_me(), "id", 0) or 0)
        except Exception:
            actor_id = 0
    if actor_id <= 0:
        actor_id = target_id

    if bool(af.get("delete_messages")) and message_ids:
        _antiflood_try_delete_messages(chat_id, message_ids)

    if ptype == "warn":
        action_id, count_after, _ = _mod_warn_add(chat_id, actor_id, target_id, reason)
        warn_limit = int((_mod_get_chat(chat_id).get("settings") or {}).get("warn_limit", 3))
        if count_after >= warn_limit:
            try:
                _auto_punish_for_warns(chat_id, get_bot_me(), target_id,
                                       source_tag="#АНТИ_ФЛУД")
            except Exception:
                pass
        _antiflood_send_punish_message(
            chat_id=chat_id,
            action_kind="warn",
            action_id=action_id,
            target_id=target_id,
            actor_id=actor_id,
            until_ts=None,
            warn_count=count_after,
            warn_limit=warn_limit,
        )
        _log_antiflood_action(chat_id, "warn", target_id, actor_id, 0, None, reason)
        return True

    if ptype == "kick":
        try:
            if hasattr(bot, "ban_chat_member"):
                bot.ban_chat_member(chat_id, target_id)
            else:
                bot.kick_chat_member(chat_id, target_id)
        except Exception:
            return False
        try:
            bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except Exception:
            pass
        _mark_farewell_suppressed(chat_id, target_id)

        row = {
            "id": _mod_new_action_id(),
            "target_id": target_id,
            "actor_id": actor_id,
            "created_at": time.time(),
            "duration": 0,
            "until": 0,
            "reason": reason,
            "active": True,
            "auto": True,
            "source": "antiflood",
        }
        _mod_log_append(chat_id, "kick", row)
        _antiflood_send_punish_message(
            chat_id=chat_id,
            action_kind="kick",
            action_id=str(row["id"]),
            target_id=target_id,
            actor_id=actor_id,
            until_ts=None,
        )
        _log_antiflood_action(chat_id, "kick", target_id, actor_id, 0, None, reason)
        return True

    try:
        duration = int(duration_raw) if duration_raw is not None else 30 * 60
    except Exception:
        duration = 30 * 60
    if duration != 0:
        duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, duration))

    until_ts = None
    if ptype == "ban":
        ok, _, until_ts = _apply_ban(chat_id, target_id, duration)
    else:
        ok, _, until_ts = _apply_mute(chat_id, target_id, duration)
        ptype = "mute"
    if not ok:
        return False

    action_id = _mod_new_action_id()
    row = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor_id,
        "created_at": time.time(),
        "duration": int(duration or 0),
        "until": int(until_ts or 0),
        "reason": reason,
        "active": True,
        "auto": True,
        "source": "antiflood",
    }
    _mod_log_append(chat_id, ptype, row)

    ch = _mod_get_chat(chat_id)
    ch.setdefault("active", {}).setdefault(ptype, {})[str(target_id)] = {
        "id": action_id,
        "actor_id": actor_id,
        "created_at": row["created_at"],
        "duration": row["duration"],
        "until": row["until"],
        "reason": row["reason"],
    }
    _mod_save()

    _antiflood_send_punish_message(
        chat_id=chat_id,
        action_kind=ptype,
        action_id=action_id,
        target_id=target_id,
        actor_id=actor_id,
        until_ts=int(until_ts or 0),
    )
    _log_antiflood_action(chat_id, ptype, target_id, actor_id, int(duration or 0), int(until_ts or 0) if until_ts else None, reason)
    return True


def _log_antiflood_action(chat_id: int, ptype: str, target_id: int, actor_id: int,
                           duration: int, until_ts: int | None, reason: str) -> None:
    from moderation import _log_mod_action
    event_map = {"mute": "mute", "ban": "ban", "kick": "kick", "warn": "warn"}
    event = event_map.get(ptype, ptype)
    _log_mod_action(chat_id, event, actor_id, target_id,
                    duration=duration, reason=reason, until_ts=until_ts,
                    source_tag="#АНТИ_ФЛУД")


def _antiflood_runtime_check(m: types.Message):
    chat_id = int(m.chat.id)
    if not is_group_approved(chat_id):
        return

    # Пропускаем сообщения от привязанного канала (sender_chat — канал)
    sender_chat = getattr(m, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) == "channel":
        return

    user = getattr(m, "from_user", None)
    if not _antiflood_target_allowed(chat_id, user):
        return

    af = _antiflood_get_effective_settings(chat_id)
    if not af["enabled"]:
        return

    user_id = int(user.id)
    period = int(af["period"])
    msg_limit = int(af["messages"])
    now_ts = _now_ts()
    msg_id = int(getattr(m, "message_id", 0) or 0)
    key = (chat_id, user_id)

    should_punish = False
    punish_message_ids: list[int] = []
    with _ANTIFLOOD_LOCK:
        timeline = _ANTIFLOOD_TIMELINE.get(key) or []
        keep_from = now_ts - period
        timeline = [(ts, mid) for ts, mid in timeline if ts >= keep_from]
        timeline.append((now_ts, msg_id))
        if len(timeline) > max(200, msg_limit * 4):
            timeline = timeline[-max(200, msg_limit * 4):]
        _ANTIFLOOD_TIMELINE[key] = timeline

        last_punish = int(_ANTIFLOOD_LAST_PUNISH.get(key) or 0)
        if len(timeline) >= msg_limit and (now_ts - last_punish) >= max(3, period):
            should_punish = True
            _ANTIFLOOD_LAST_PUNISH[key] = now_ts
            punish_message_ids = [mid for _, mid in timeline if int(mid) > 0]
            _ANTIFLOOD_TIMELINE[key] = []

    if should_punish:
        _antiflood_apply_punishment(chat_id, user, af, message_ids=punish_message_ids)


@bot.message_handler(content_types=ANTIFLOOD_TRACK_CONTENT_TYPES, func=lambda m: m.chat.type in ("group", "supergroup"))
def antiflood_runtime_handler(m: types.Message):
    try:
        _antiflood_runtime_check(m)
    except Exception:
        pass
    return ContinueHandling()


@bot.message_handler(content_types=["text"], func=lambda m: m.chat.type in ("group", "supergroup"))
def cleanup_delete_commands_runtime(m: types.Message):
    try:
        chat_id = m.chat.id
        
        # Проверка одобрения группы
        if not is_group_approved(chat_id):
            return ContinueHandling()
        
        if not _bot_can_delete_messages(chat_id):
            return ContinueHandling()

        txt = (m.text or "")
        s = txt.lstrip()
        if not s:
            return ContinueHandling()

        sign = s[0]
        if sign not in CLEANUP_CMD_SIGNS:
            return ContinueHandling()

        if not _cleanup_cmd_enabled(chat_id, sign):
            return ContinueHandling()

        # не трогаем сообщения самого бота (на всякий)
        try:
            if getattr(m.from_user, "is_bot", False) and int(getattr(m.from_user, "id", 0) or 0) == _get_bot_id():
                return ContinueHandling()
        except Exception:
            pass

        try:
            bot.delete_message(chat_id, m.message_id)
        except Exception:
            pass

    except Exception:
        pass

    return ContinueHandling()    

@bot.message_handler(content_types=CLEANUP_SYSTEM_CONTENT_TYPES, func=lambda m: m.chat.type in ("group", "supergroup"))
def cleanup_delete_system_runtime(m: types.Message):
    try:
        chat_id = m.chat.id
        
        # Проверка одобрения группы
        if not is_group_approved(chat_id):
            return ContinueHandling()
        
        ct = getattr(m, "content_type", "") or ""
        if ct not in CLEANUP_SYSTEM_LABELS:
            return ContinueHandling()

        if not _cleanup_sys_enabled(chat_id, ct):
            return ContinueHandling()

        if not _bot_can_delete_messages(chat_id):
            return ContinueHandling()

        if ct == "pinned_message" and _should_keep_pin_service_message(chat_id):
            return ContinueHandling()

        # 1) пробуем Bot API
        try:
            bot.delete_message(chat_id, m.message_id)
            return ContinueHandling()
        except Exception:
            pass

        # 2) fallback для pinned_message (если у тебя Telethon уже подключён)
        if ct == "pinned_message":
            try:
                _try_delete_last_bot_service_pin(chat_id)
            except Exception:
                pass

    except Exception:
        pass

    return ContinueHandling()


# ============================================================
# Анти-рейд runtime
# ============================================================

ANTIRAID_BLOCK_DURATION = 600  # 10 minutes (in seconds)

_ANTIRAID_LOCK = threading.Lock()
# {chat_id: [(timestamp, user_id), ...]} - join timeline per chat
_ANTIRAID_JOIN_TIMELINE: dict[int, list[tuple[int, int]]] = {}
# {chat_id: int} - raid mode active until this timestamp
_ANTIRAID_ACTIVE_UNTIL: dict[int, int] = {}
# {(chat_id, user_id): int} - last punish timestamp to avoid duplicate punishments in same raid
_ANTIRAID_PUNISHED: dict[tuple[int, int], int] = {}


def _antiraid_is_active(chat_id: int) -> bool:
    """Returns True if anti-raid mode is currently triggered (raid is in progress)."""
    return _now_ts() < int(_ANTIRAID_ACTIVE_UNTIL.get(chat_id) or 0)


def _antiraid_get_effective_settings(chat_id: int) -> dict:
    ar = ((_mod_get_chat(chat_id).get("settings") or {}).get("antiraid") or {})
    punish = ar.get("punish") or {}
    try:
        count = int(ar.get("count") or 10)
    except Exception:
        count = 10
    try:
        period = int(ar.get("period") or 10)
    except Exception:
        period = 10

    ptype = str(punish.get("type") or "mute").strip().lower()
    if ptype not in ("mute", "ban", "kick"):
        ptype = "mute"

    return {
        "enabled": bool(ar.get("enabled", False)),
        "count": max(ANTIRAID_MIN_COUNT, min(ANTIRAID_MAX_COUNT, count)),
        "period": max(ANTIRAID_MIN_PERIOD, min(ANTIRAID_MAX_PERIOD, period)),
        "punish": {
            "type": ptype,
            "duration": punish.get("duration"),
            "reason": str(punish.get("reason") or "").strip(),
        },
    }


def _antiraid_target_allowed(chat_id: int, user_obj: types.User) -> bool:
    if not user_obj:
        return False

    uid = int(getattr(user_obj, "id", 0) or 0)
    if uid <= 0:
        return False

    if is_owner(user_obj) or is_dev(user_obj):
        return False

    try:
        if int(get_user_rank(chat_id, uid) or 0) > 0:
            return False
    except Exception:
        pass

    try:
        if bool(getattr(user_obj, "is_bot", False)):
            return False
    except Exception:
        return False

    try:
        if uid == _get_bot_id():
            return False
    except Exception:
        return False

    try:
        member = bot.get_chat_member(chat_id, uid)
        if getattr(member, "status", "") in ("administrator", "creator"):
            return False
    except Exception:
        pass

    return True


def _antiraid_apply_punishment(
    chat_id: int,
    target_user: types.User,
    ar: dict,
) -> bool:
    target_id = int(getattr(target_user, "id", 0) or 0)
    if target_id <= 0:
        return False

    punish = ar.get("punish") or {}
    ptype = str(punish.get("type") or "mute").lower()
    duration_raw = punish.get("duration")
    reason = str(punish.get("reason") or "").strip() or "Анти-рейд: массовый вход участников."

    actor_id = _get_bot_id()
    if actor_id <= 0:
        try:
            actor_id = int(getattr(get_bot_me(), "id", 0) or 0)
        except Exception:
            actor_id = 0
    if actor_id <= 0:
        actor_id = target_id

    if ptype == "kick":
        try:
            if hasattr(bot, "ban_chat_member"):
                bot.ban_chat_member(chat_id, target_id)
            else:
                bot.kick_chat_member(chat_id, target_id)
        except Exception:
            return False
        try:
            bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except Exception:
            pass
        _mark_farewell_suppressed(chat_id, target_id)

        action_id = _mod_new_action_id()
        row = {
            "id": action_id,
            "target_id": target_id,
            "actor_id": actor_id,
            "created_at": time.time(),
            "duration": 0,
            "until": 0,
            "reason": reason,
            "active": True,
            "auto": True,
            "source": "antiraid",
        }
        _mod_log_append(chat_id, "kick", row)
        from moderation import _log_mod_action
        _log_mod_action(chat_id, "kick", actor_id, target_id, reason=reason,
                        source_tag="#АНТИ_РЕЙД")
        return True

    try:
        duration = int(duration_raw) if duration_raw is not None else 30 * 60
    except Exception:
        duration = 30 * 60
    if duration != 0:
        duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, duration))

    if ptype == "ban":
        ok, _, until_ts = _apply_ban(chat_id, target_id, duration)
    else:
        ok, _, until_ts = _apply_mute(chat_id, target_id, duration)
        ptype = "mute"
    if not ok:
        return False

    action_id = _mod_new_action_id()
    row = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor_id,
        "created_at": time.time(),
        "duration": int(duration or 0),
        "until": int(until_ts or 0),
        "reason": reason,
        "active": True,
        "auto": True,
        "source": "antiraid",
    }
    _mod_log_append(chat_id, ptype, row)

    ch = _mod_get_chat(chat_id)
    ch.setdefault("active", {}).setdefault(ptype, {})[str(target_id)] = {
        "id": action_id,
        "actor_id": actor_id,
        "created_at": row["created_at"],
        "duration": row["duration"],
        "until": row["until"],
        "reason": row["reason"],
    }
    _mod_save()
    from moderation import _log_mod_action
    _log_mod_action(chat_id, ptype, actor_id, target_id,
                    duration=int(duration or 0), reason=reason,
                    until_ts=int(until_ts or 0) if until_ts else None,
                    source_tag="#АНТИ_РЕЙД")
    return True


def _antiraid_runtime_check(chat_id: int, user: types.User) -> None:
    if not is_group_approved(chat_id):
        return

    if not _antiraid_target_allowed(chat_id, user):
        return

    ar = _antiraid_get_effective_settings(chat_id)
    if not ar["enabled"]:
        return

    user_id = int(user.id)
    count_limit = int(ar["count"])
    period = int(ar["period"])
    now_ts = _now_ts()

    users_to_punish: list[int] = []
    raid_activated = False

    with _ANTIRAID_LOCK:
        # Clean up old punish records
        keys_to_remove = [
            k for k, v in list(_ANTIRAID_PUNISHED.items())
            if k[0] == chat_id and (now_ts - v) > ANTIRAID_BLOCK_DURATION * 2
        ]
        for k in keys_to_remove:
            _ANTIRAID_PUNISHED.pop(k, None)

        # Extend raid mode if threshold hit again while in raid mode
        active_until = int(_ANTIRAID_ACTIVE_UNTIL.get(chat_id) or 0)
        raid_active = now_ts < active_until

        if raid_active:
            # Raid mode is on: punish this new joiner immediately
            key = (chat_id, user_id)
            last_punish = int(_ANTIRAID_PUNISHED.get(key) or 0)
            if (now_ts - last_punish) > ANTIRAID_REPUNISH_COOLDOWN:  # avoid re-punishing too fast
                users_to_punish.append(user_id)
                _ANTIRAID_PUNISHED[key] = now_ts
            # Track join and potentially extend raid
            timeline = _ANTIRAID_JOIN_TIMELINE.get(chat_id) or []
            keep_from = now_ts - period
            timeline = [(ts, uid) for ts, uid in timeline if ts >= keep_from]
            timeline.append((now_ts, user_id))
            _ANTIRAID_JOIN_TIMELINE[chat_id] = timeline
            if len(timeline) >= count_limit:
                # Extend raid mode
                _ANTIRAID_ACTIVE_UNTIL[chat_id] = now_ts + ANTIRAID_BLOCK_DURATION
                _ANTIRAID_JOIN_TIMELINE[chat_id] = []
        else:
            # Raid mode is off: track joins
            timeline = _ANTIRAID_JOIN_TIMELINE.get(chat_id) or []
            keep_from = now_ts - period
            timeline = [(ts, uid) for ts, uid in timeline if ts >= keep_from]
            timeline.append((now_ts, user_id))
            _ANTIRAID_JOIN_TIMELINE[chat_id] = timeline

            if len(timeline) >= count_limit:
                # Raid triggered!
                raid_activated = True
                _ANTIRAID_ACTIVE_UNTIL[chat_id] = now_ts + ANTIRAID_BLOCK_DURATION
                _ANTIRAID_JOIN_TIMELINE[chat_id] = []
                # Punish all users in the trigger window
                for _, uid in timeline:
                    key = (chat_id, uid)
                    last_punish = int(_ANTIRAID_PUNISHED.get(key) or 0)
                    if (now_ts - last_punish) > ANTIRAID_REPUNISH_COOLDOWN:
                        users_to_punish.append(uid)
                        _ANTIRAID_PUNISHED[key] = now_ts

    if not users_to_punish:
        return

    # Punish all collected users
    # For the current user we have the user object; for others we try to get them
    for uid in users_to_punish:
        if uid == user_id:
            target = user
        else:
            try:
                member = bot.get_chat_member(chat_id, uid)
                target = getattr(member, "user", None)
                if not target:
                    continue
            except Exception:
                continue
        try:
            _antiraid_apply_punishment(chat_id, target, ar)
        except Exception:
            pass

    if raid_activated:
        # Send a notification about raid mode activation
        try:
            emoji_warn = f'<tg-emoji emoji-id="{EMOJI_PUNISHMENT_ID}">⚠️</tg-emoji>'
            bot.send_message(
                chat_id,
                f"{emoji_warn} Сработал анти-рейд. Пытаюсь справиться с наплывом пользователей.",
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        except Exception:
            pass


__all__ = [name for name in globals() if not name.startswith('__')]
