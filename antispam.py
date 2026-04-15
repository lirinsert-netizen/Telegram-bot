"""
antispam.py — Анти-спам модуль:
  /settings → Анти-спам (4 раздела: Телеграм-ссылки, Цитирование, Пересылка, Блок всех ссылок)
  Настройки: статус, наказание, длительность, удаление сообщений, исключения.
  Проверка входящих сообщений в реальном времени.
"""
from __future__ import annotations
import re as _re
import html as _html
import time as _time
from typing import Optional

from config import (
    types, ContinueHandling,
    InlineKeyboardMarkup, InlineKeyboardButton,
    bot,
    get_bot_me,
    EMOJI_ROLE_SETTINGS_SENT_PM_ID,
    EMOJI_ROLE_SETTINGS_CANCEL_ID,
    EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID,
    EMOJI_UNPUNISH_ID,
)
from persistence import (
    _is_duplicate_callback_query,
    tg_get_chat_member,
)
from moderation import (
    _mod_get_chat, _mod_save, _mod_duration_text,
    _parse_duration_prefix,
    _mod_new_action_id, _mod_log_append, _mod_warn_add,
    _auto_punish_for_warns,
    _apply_mute, _apply_ban,
    _mark_farewell_suppressed,
    _send_punish_message_with_button,
    _log_mod_action as _mod_log_action,
)
from helpers import (
    is_owner, is_dev, is_group_approved, get_user_rank,
    link_for_user, premium_prefix,
)
from settings_ui import (
    _pending_get, _pending_put, _pending_pop,
    _pending_msg_get, _pending_msg_set, _pending_msg_pop,
    _pending_set_raw, _pending_pop_raw,
    _delete_pending_ui, _replace_pending_ui,
    _try_delete_private_prompt,
    _show_warn_settings_ui,
    _user_can_open_settings,
    _bot_can_delete_messages,
    _get_bot_id,
    MIN_PUNISH_SECONDS, MAX_PUNISH_SECONDS,
    CLEANUP_ICON_ENABLE_ID, CLEANUP_ICON_DISABLE_ID,
)

# ─────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────

_ANTISPAM_SECTIONS: dict[str, str] = {
    "tg_links":   "Телеграм-ссылки",
    "quoting":    "Цитирование",
    "forwarding": "Пересылка",
    "all_links":  "Блок всех ссылок",
}

_ANTISPAM_SECTION_DESC: dict[str, str] = {
    "tg_links":   "Блокирует ссылки и упоминания ресурсов Telegram.",
    "quoting":    "Блокирует сообщения с цитатами из выбранных источников.",
    "forwarding": "Блокирует пересланные сообщения от выбранных источников.",
    "all_links":  "Блокирует все ссылки.",
}

# Telegram-ссылки: t.me/..., telegram.me/..., tg://...
_TG_URL_RE = _re.compile(
    r'(?:https?://)?(?:t(?:elegram)?\.me|telegram\.org|tg://)\S*',
    _re.IGNORECASE,
)

# @username (минимум 4 символа)
_TG_USERNAME_RE = _re.compile(r'@[a-zA-Z][a-zA-Z0-9_]{3,}')

# Любые HTTP(S)/www ссылки
_ALL_LINKS_RE = _re.compile(r'(?:https?://|www\.)\S+', _re.IGNORECASE)

# t.me/c/{peer_id}/... — ссылки на сообщения в приватных супергруппах
_TG_INTERNAL_MSG_RE = _re.compile(
    r'(?:https?://)?t(?:elegram)?\.me/c/(\d+)/\d+',
    _re.IGNORECASE,
)


def _is_internal_group_link(url: str, chat_id: int) -> bool:
    """Return True if *url* is a t.me/c/{peer_id}/... link pointing to a message
    inside the group identified by *chat_id*.

    For private supergroups Telegram encodes the group as a positive *peer_id*
    obtained by stripping the ``-100`` prefix from the negative chat_id:
        chat_id = -1001234567890  →  peer_id = 1234567890
    """
    m = _TG_INTERNAL_MSG_RE.search(url)
    if not m:
        return False
    peer_id_str = m.group(1)
    chat_id_str = str(chat_id)
    # Convert chat_id to peer_id by removing leading "-100" (supergroup/channel prefix)
    if chat_id_str.startswith("-100"):
        return peer_id_str == chat_id_str[4:]
    # For other negative IDs just compare the absolute value
    return peer_id_str == str(abs(chat_id))

# Punishment type labels
_PUNISH_LABELS: dict[str, str] = {
    "warn": "Предупреждение",
    "mute": "Ограничение",
    "ban":  "Блокировка",
    "kick": "Исключение",
}

MAX_EXCEPTIONS = 20  # Максимум исключений на раздел
MAX_EXCEPTION_PATTERN_LEN = 100  # Максимальная длина шаблона исключения
MAX_EXCEPTION_DISPLAY_LEN = 30  # Максимальная длина при отображении исключения в кнопке

# Exception add/delete premium emoji ids
_EXCEPTION_ADD_EMOJI_ID = "5226945370684140473"
_EXCEPTION_DEL_EMOJI_ID = "5229113891081956317"

# Regex: valid exception patterns must look like a link or @username
_VALID_EXCEPTION_RE = _re.compile(
    r'^(?:https?://|www\.|t(?:elegram)?\.me/|telegram\.org/|tg://|@[a-zA-Z])',
    _re.IGNORECASE,
)

# Valid sub-page names for the section keyboard
_SECTION_VALID_PAGES = frozenset({
    "main", "punish", "duration",
    "exceptions", "exceptions_list", "exceptions_delete",
    "flag_usernames", "flag_bots", "flag_user_usernames",
    "type_channels", "type_users", "type_bots", "type_groups",
})

# No-op callback actions (label/display buttons that do nothing)
_NOOP_ACTIONS = frozenset({"statusnoop", "delnoop", "tgflnoop", "typeflnoop", "excnoop"})

# Per-type labels for quoting / forwarding sections
_FWD_QUOTE_TYPES: dict[str, str] = {
    "channels": "Каналы",
    "users":    "Пользователи",
    "bots":     "Боты",
    "groups":   "Группы",
}


# ─────────────────────────────────────────────
# Pending helpers (переиспользуем из settings_ui)
# ─────────────────────────────────────────────

def _as_pending_put(key_prefix: str, user_id: int, chat_id: int, section: str) -> None:
    _pending_set_raw(key_prefix, user_id, f"{chat_id}:{section}")


def _as_pending_get_cid_sec(key_prefix: str, user_id: int) -> tuple[Optional[int], Optional[str]]:
    d = _pending_get(key_prefix)
    val = d.get(str(user_id))
    if not val:
        return None, None
    parts = str(val).split(":", 1)
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), parts[1]
    except Exception:
        return None, None


def _as_pending_pop_cid_sec(key_prefix: str, user_id: int) -> tuple[Optional[int], Optional[str]]:
    val = _pending_pop_raw(key_prefix, user_id)
    if not val:
        return None, None
    parts = str(val).split(":", 1)
    if len(parts) != 2:
        return None, None
    try:
        return int(parts[0]), parts[1]
    except Exception:
        return None, None


# ─────────────────────────────────────────────
# Settings access
# ─────────────────────────────────────────────

def _antispam_get_exceptions(chat_id: int) -> list:
    """Returns the global (shared) anti-spam exceptions list for the chat."""
    settings = (_mod_get_chat(chat_id).get("settings") or {})
    asp = settings.get("antispam") or {}
    # Migrate any per-section exceptions into the global list (one-time)
    global_exc = asp.get("exceptions")
    if not isinstance(global_exc, list):
        global_exc = []
    existing_lower = {e.lower() for e in global_exc if e}
    migrated = False
    for section_key in _ANTISPAM_SECTIONS:
        sec_data = asp.get(section_key) or {}
        per_sec = sec_data.get("exceptions")
        if isinstance(per_sec, list) and per_sec:
            for e in per_sec:
                if e and e.lower() not in existing_lower:
                    global_exc.append(str(e))
                    existing_lower.add(e.lower())
                    migrated = True
            sec_data.pop("exceptions", None)
            asp[section_key] = sec_data
    if migrated:
        asp["exceptions"] = global_exc
        ch = _mod_get_chat(chat_id)
        s = ch.get("settings") or {}
        s["antispam"] = asp
        ch["settings"] = s
        _mod_save()
    return [str(e) for e in global_exc if e]


def _antispam_save_exceptions(chat_id: int, exceptions: list) -> None:
    """Saves the global (shared) anti-spam exceptions list for the chat."""
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    asp = settings.get("antispam") or {}
    asp["exceptions"] = [str(e) for e in exceptions if e]
    settings["antispam"] = asp
    ch["settings"] = settings
    _mod_save()


def _antispam_get_section(chat_id: int, section: str) -> dict:
    """Returns validated settings for one anti-spam section."""
    settings = (_mod_get_chat(chat_id).get("settings") or {})
    asp = settings.get("antispam") or {}
    raw = asp.get(section) or {}
    p = raw.get("punish") or {}
    pt = str(p.get("type") or "warn").strip().lower()
    if pt not in ("warn", "mute", "ban", "kick"):
        pt = "warn"
    pd = p.get("duration")
    if pt in ("mute", "ban", "warn"):
        if pd is None:
            pd = 3600
        else:
            try:
                pd = int(pd)
            except Exception:
                pd = 3600
    else:
        pd = None
    # Use shared (global) exceptions list
    exc = asp.get("exceptions")
    if not isinstance(exc, list):
        exc = []
    result: dict = {
        "enabled": bool(raw.get("enabled", False)),
        "delete_messages": bool(raw.get("delete_messages", False)),
        "punish": {"type": pt, "duration": pd, "reason": str(p.get("reason") or "")},
        "exceptions": [str(e) for e in exc if e],
    }
    if section == "tg_links":
        result["check_usernames"] = bool(raw.get("check_usernames", False))
        result["check_user_usernames"] = bool(raw.get("check_user_usernames", False))
        result["check_bots"] = bool(raw.get("check_bots", False))
    if section in ("quoting", "forwarding"):
        raw_types = raw.get("types") or {}
        result["types"] = {
            t: bool(raw_types.get(t, False))
            for t in _FWD_QUOTE_TYPES
        }
    return result


def _antispam_save_section(chat_id: int, section: str, data: dict) -> None:
    ch = _mod_get_chat(chat_id)
    settings = ch.get("settings") or {}
    asp = settings.get("antispam") or {}
    # Never store exceptions inside per-section data — they live in asp["exceptions"]
    section_data = {k: v for k, v in data.items() if k != "exceptions"}
    asp[section] = section_data
    settings["antispam"] = asp
    ch["settings"] = settings
    _mod_save()


# ─────────────────────────────────────────────
# Rendering (text)
# ─────────────────────────────────────────────

def _render_antispam_main(chat_id: int) -> str:
    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'

    lines = [f"{emoji_settings} <b>Анти-спам</b>\n"]
    for key, label in _ANTISPAM_SECTIONS.items():
        sec = _antispam_get_section(chat_id, key)
        status_txt = "<code>включено</code>" if sec["enabled"] else "<code>выключено</code>"
        lines.append(f"<b>{label}:</b> {status_txt}")
    # Shared exceptions count displayed once
    exceptions = _antispam_get_exceptions(chat_id)
    exc_count = len(exceptions) if exceptions else 0
    exc_count_txt = str(exc_count) if exc_count else "нет"
    lines.append(f"\n<b>Исключения:</b> {exc_count_txt}")
    return "\n".join(lines)


def _render_antispam_section(chat_id: int, section: str, page: str = "main") -> str:
    label = _ANTISPAM_SECTIONS.get(section, section)
    desc = _ANTISPAM_SECTION_DESC.get(section, "")
    sec = _antispam_get_section(chat_id, section)

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'

    status_txt = "<code>включён</code>" if sec["enabled"] else "<code>выключен</code>"
    delete_txt = "<code>включено</code>" if sec["delete_messages"] else "<code>выключено</code>"
    ptype = sec["punish"]["type"]
    dur = sec["punish"]["duration"]
    punish_label = _PUNISH_LABELS.get(ptype, "Предупреждение")
    dur_label = "нет" if ptype == "kick" else _mod_duration_text(int(dur or 0))

    text = (
        f"{emoji_settings} <b>{label}</b>\n\n"
        f"{desc}\n\n"
        f"<b>Статус:</b> {status_txt}\n"
        f"<b>Удаление сообщений:</b> {delete_txt}\n"
        f"<b>Наказание:</b> <code>{_html.escape(punish_label)}</code>\n"
        f"<b>Длительность:</b> <code>{_html.escape(dur_label)}</code>"
    )

    if section == "tg_links":
        un_txt = "<code>включено</code>" if sec.get("check_usernames") else "<code>выключено</code>"
        uu_txt = "<code>включено</code>" if sec.get("check_user_usernames") else "<code>выключено</code>"
        bt_txt = "<code>включено</code>" if sec.get("check_bots") else "<code>выключено</code>"
        text += (
            f"\n<b>Юзернеймы (@группы/каналы):</b> {un_txt}"
            f"\n<b>Пользовательские юзернеймы:</b> {uu_txt}"
            f"\n<b>Боты:</b> {bt_txt}"
        )

    if section in ("quoting", "forwarding"):
        types = sec.get("types") or {}
        type_lines = []
        for t_key, t_label in _FWD_QUOTE_TYPES.items():
            t_txt = "<code>да</code>" if types.get(t_key) else "<code>нет</code>"
            type_lines.append(f"<b>{t_label}:</b> {t_txt}")
        text += "\n" + "\n".join(type_lines)

    hint = ""
    if page == "punish":
        hint = "\n\n<i>Выберите наказание за нарушение.</i>"
    elif page == "duration":
        if ptype == "kick":
            hint = "\n\nДля выбранного типа наказания длительность не используется."
        else:
            hint = "\n\n<i>Установите длительность наказания.</i>"
    elif page.startswith("flag_"):
        flag_names = {
            "flag_usernames": "Юзернеймы",
            "flag_bots": "Боты",
            "flag_user_usernames": "Пользовательские юзернеймы",
        }
        hint = f"\n\n<i>Управление фильтром «{flag_names.get(page, page)}».</i>"
    elif page.startswith("type_"):
        type_key = page[5:]
        t_label = _FWD_QUOTE_TYPES.get(type_key, type_key)
        hint = f"\n\n<i>Управление фильтром «{t_label}».</i>"

    return text + hint


# ─────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────

def _build_antispam_main_keyboard(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    # Row 1: Телеграм-ссылки (full width)
    b_tg = InlineKeyboardButton(
        "Телеграм-ссылки",
        callback_data=f"stas:sub:{chat_id}:tg_links:main",
    )
    kb.row(b_tg)

    # Row 2: Пересылка + Цитирование
    b_fwd = InlineKeyboardButton(
        "Пересылка",
        callback_data=f"stas:sub:{chat_id}:forwarding:main",
    )
    b_quot = InlineKeyboardButton(
        "Цитирование",
        callback_data=f"stas:sub:{chat_id}:quoting:main",
    )
    kb.row(b_fwd, b_quot)

    # Row 3: Блок всех ссылок (full width)
    b_all = InlineKeyboardButton(
        "Блок всех ссылок",
        callback_data=f"stas:sub:{chat_id}:all_links:main",
    )
    kb.row(b_all)

    # Row 4: Управление исключениями — expandable (highlighted when open)
    exc_title = "»Управление исключениями«" if page == "exceptions" else "Управление исключениями"
    b_exc = InlineKeyboardButton(
        exc_title,
        callback_data=f"stas:mainpage:{chat_id}:exceptions",
    )
    try:
        if page == "exceptions":
            b_exc.style = "primary"
    except Exception:
        pass
    kb.row(b_exc)

    # When exceptions section is open, add sub-buttons inline
    if page == "exceptions":
        b_list = InlineKeyboardButton(
            "Список исключений",
            callback_data=f"stas:mainpage:{chat_id}:exceptions_list",
        )
        kb.add(b_list)

        exceptions = _antispam_get_exceptions(chat_id)
        if len(exceptions) < MAX_EXCEPTIONS:
            b_add = InlineKeyboardButton(
                "Добавить исключение",
                callback_data=f"stas:exc_add:{chat_id}:all_links",
            )
            try:
                b_add.icon_custom_emoji_id = str(_EXCEPTION_ADD_EMOJI_ID)
                b_add.style = "primary"
            except Exception:
                pass
            kb.add(b_add)

        b_del_exc = InlineKeyboardButton(
            "Удалить исключение",
            callback_data=f"stas:exc_del_prompt:{chat_id}:all_links",
        )
        try:
            b_del_exc.icon_custom_emoji_id = str(_EXCEPTION_DEL_EMOJI_ID)
            b_del_exc.style = "primary"
        except Exception:
            pass
        kb.add(b_del_exc)

    b_back = InlineKeyboardButton("Назад", callback_data=f"st_main:{chat_id}:filters")
    try:
        b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back.style = "primary"
    except Exception:
        pass
    kb.add(b_back)
    return kb


def _build_antispam_main_exc_keyboard(chat_id: int, section: str, page: str = "exceptions") -> InlineKeyboardMarkup:
    """Keyboard for exceptions management on the main antispam page."""
    kb = InlineKeyboardMarkup(row_width=1)

    _exc_pages = ("exceptions", "exceptions_list", "exceptions_delete")

    if page == "exceptions":
        b_list = InlineKeyboardButton(
            "Список исключений",
            callback_data=f"stas:page:{chat_id}:{section}:exceptions_list",
        )
        kb.add(b_list)

        exceptions = _antispam_get_exceptions(chat_id)
        if len(exceptions) < MAX_EXCEPTIONS:
            b_add = InlineKeyboardButton(
                "Добавить исключение",
                callback_data=f"stas:exc_add:{chat_id}:{section}",
            )
            try:
                b_add.icon_custom_emoji_id = str(_EXCEPTION_ADD_EMOJI_ID)
                b_add.style = "primary"
            except Exception:
                pass
            kb.add(b_add)

        b_del_exc = InlineKeyboardButton(
            "Удалить исключение",
            callback_data=f"stas:exc_del_prompt:{chat_id}:{section}",
        )
        try:
            b_del_exc.icon_custom_emoji_id = str(_EXCEPTION_DEL_EMOJI_ID)
            b_del_exc.style = "primary"
        except Exception:
            pass
        kb.add(b_del_exc)

    elif page == "exceptions_delete":
        exceptions = _antispam_get_exceptions(chat_id)
        if exceptions:
            for idx, exc in enumerate(exceptions):
                b_del = InlineKeyboardButton(
                    f"🗑 {exc[:MAX_EXCEPTION_DISPLAY_LEN] + ('…' if len(exc) > MAX_EXCEPTION_DISPLAY_LEN else '')}",
                    callback_data=f"stas:exc_del:{chat_id}:{section}:{idx}",
                )
                kb.add(b_del)
        else:
            b_empty = InlineKeyboardButton("Нечего удалять", callback_data=f"stas:excnoop:{chat_id}:{section}")
            kb.add(b_empty)

    # Back button
    if page in ("exceptions_delete",):
        back_cb = f"stas:page:{chat_id}:{section}:exceptions"
    else:
        back_cb = f"stas:open:{chat_id}"
    b_back_exc = InlineKeyboardButton("Назад", callback_data=back_cb)
    try:
        b_back_exc.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back_exc.style = "primary"
    except Exception:
        pass
    kb.add(b_back_exc)
    return kb


def _build_antispam_section_keyboard(chat_id: int, section: str, page: str = "main") -> InlineKeyboardMarkup:
    sec = _antispam_get_section(chat_id, section)
    ptype = sec["punish"]["type"]
    enabled = sec["enabled"]
    delete_messages = sec["delete_messages"]

    kb = InlineKeyboardMarkup(row_width=2)
    inv = "\u2063"

    # ── Статус — single toggle button (green when on, red when off) ──
    b_status = InlineKeyboardButton(
        "Статус",
        callback_data=f"stas:statusset:{chat_id}:{section}:{0 if enabled else 1}",
    )
    try:
        b_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(b_status)

    # ── Удаление сообщений — single toggle button (green when on, red when off) ──
    b_del = InlineKeyboardButton(
        "Удаление сообщений",
        callback_data=f"stas:delset:{chat_id}:{section}:{0 if delete_messages else 1}",
    )
    try:
        b_del.style = "success" if delete_messages else "danger"
    except Exception:
        pass
    kb.add(b_del)

    # ── tg_links: [Юзернеймы][Боты] row + [Пользовательские юзернеймы] row (expandable) ──
    if section == "tg_links":
        un_title = "»Юзернеймы«" if page == "flag_usernames" else "Юзернеймы"
        bot_title = "»Боты«" if page == "flag_bots" else "Боты"
        b_un = InlineKeyboardButton(
            un_title,
            callback_data=f"stas:page:{chat_id}:{section}:flag_usernames",
        )
        b_bot = InlineKeyboardButton(
            bot_title,
            callback_data=f"stas:page:{chat_id}:{section}:flag_bots",
        )
        try:
            if page == "flag_usernames":
                b_un.style = "primary"
            if page == "flag_bots":
                b_bot.style = "primary"
        except Exception:
            pass
        kb.row(b_un, b_bot)

        if page == "flag_usernames":
            is_on = bool(sec.get("check_usernames", False))
            on_s, off_s = ("success", "danger") if is_on else ("danger", "success")
            b_on = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:usernames:1")
            b_off = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:usernames:0")
            try:
                b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
                b_on.style = on_s
                b_off.style = off_s
            except Exception:
                pass
            kb.row(b_on, b_off)
        elif page == "flag_bots":
            is_on = bool(sec.get("check_bots", False))
            on_s, off_s = ("success", "danger") if is_on else ("danger", "success")
            b_on = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:bots:1")
            b_off = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:bots:0")
            try:
                b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
                b_on.style = on_s
                b_off.style = off_s
            except Exception:
                pass
            kb.row(b_on, b_off)

        uu_title = "»Пользовательские юзернеймы«" if page == "flag_user_usernames" else "Пользовательские юзернеймы"
        b_uu = InlineKeyboardButton(
            uu_title,
            callback_data=f"stas:page:{chat_id}:{section}:flag_user_usernames",
        )
        try:
            if page == "flag_user_usernames":
                b_uu.style = "primary"
        except Exception:
            pass
        kb.add(b_uu)

        if page == "flag_user_usernames":
            is_on = bool(sec.get("check_user_usernames", False))
            on_s, off_s = ("success", "danger") if is_on else ("danger", "success")
            b_on = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:user_usernames:1")
            b_off = InlineKeyboardButton(inv, callback_data=f"stas:tgflset:{chat_id}:{section}:user_usernames:0")
            try:
                b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
                b_on.style = on_s
                b_off.style = off_s
            except Exception:
                pass
            kb.row(b_on, b_off)

    # ── quoting / forwarding: per-type flags in pairs (expandable) ──
    if section in ("quoting", "forwarding"):
        types = sec.get("types") or {}
        type_keys = list(_FWD_QUOTE_TYPES.keys())
        for i in range(0, len(type_keys), 2):
            pair = type_keys[i:i + 2]
            row_btns = []
            active_in_pair: str | None = None
            for t_key in pair:
                t_label = _FWD_QUOTE_TYPES[t_key]
                is_active = page == f"type_{t_key}"
                title = f"»{t_label}«" if is_active else t_label
                b = InlineKeyboardButton(
                    title,
                    callback_data=f"stas:page:{chat_id}:{section}:type_{t_key}",
                )
                try:
                    if is_active:
                        b.style = "primary"
                except Exception:
                    pass
                row_btns.append(b)
                if is_active:
                    active_in_pair = t_key
            kb.row(*row_btns)
            if active_in_pair:
                is_on = bool(types.get(active_in_pair, False))
                on_s, off_s = ("success", "danger") if is_on else ("danger", "success")
                b_on = InlineKeyboardButton(inv, callback_data=f"stas:typeflset:{chat_id}:{section}:{active_in_pair}:1")
                b_off = InlineKeyboardButton(inv, callback_data=f"stas:typeflset:{chat_id}:{section}:{active_in_pair}:0")
                try:
                    b_on.icon_custom_emoji_id = str(CLEANUP_ICON_ENABLE_ID)
                    b_off.icon_custom_emoji_id = str(CLEANUP_ICON_DISABLE_ID)
                    b_on.style = on_s
                    b_off.style = off_s
                except Exception:
                    pass
                kb.row(b_on, b_off)

    # ── Наказание and Длительность in same row (expandable) ──
    b_punish_title = "»Наказание«" if page == "punish" else "Наказание"
    b_dur_title = "»Длительность«" if page == "duration" else "Длительность"
    b_punish = InlineKeyboardButton(b_punish_title, callback_data=f"stas:page:{chat_id}:{section}:punish")
    b_dur = InlineKeyboardButton(b_dur_title, callback_data=f"stas:page:{chat_id}:{section}:duration")
    try:
        if page == "punish":
            b_punish.style = "primary"
        if page == "duration":
            b_dur.style = "primary"
    except Exception:
        pass
    kb.row(b_punish, b_dur)

    if page == "punish":
        btns_punish = []
        for pt_key, pt_label in [("warn", "Предупреждение"), ("mute", "Ограничение"),
                                   ("ban", "Блокировка"), ("kick", "Исключение")]:
            b = InlineKeyboardButton(pt_label, callback_data=f"stas:ptype:{chat_id}:{section}:{pt_key}")
            try:
                if ptype == pt_key:
                    b.style = "primary"
            except Exception:
                pass
            btns_punish.append(b)
        kb.row(btns_punish[0], btns_punish[1])
        kb.row(btns_punish[2], btns_punish[3])

    if page == "duration":
        b_set = InlineKeyboardButton("Установить длительность", callback_data=f"stas:dur_prompt:{chat_id}:{section}")
        try:
            b_set.style = "primary"
        except Exception:
            pass
        kb.add(b_set)

    # ── Back button ──
    back_cb = f"stas:open:{chat_id}"
    b_back = InlineKeyboardButton("Назад", callback_data=back_cb)
    try:
        b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b_back.style = "primary"
    except Exception:
        pass
    kb.add(b_back)

    return kb


# ─────────────────────────────────────────────
# Callbacks
# ─────────────────────────────────────────────

def _is_antispam_callback(data: str) -> bool:
    return bool(data) and data.startswith("stas:")


@bot.callback_query_handler(func=lambda c: _is_antispam_callback(c.data or ""))
def cb_antispam_settings(c: types.CallbackQuery) -> None:
    if _is_duplicate_callback_query(c):
        return

    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != "private":
        bot.answer_callback_query(c.id)
        return

    # stas:<action>:<chat_id>[:<section>[:<extra>]]
    parts = data.split(":", 5)
    if len(parts) < 3:
        bot.answer_callback_query(c.id)
        return

    _, action = parts[0], parts[1]
    chat_id_s = parts[2] if len(parts) > 2 else ""
    section = parts[3] if len(parts) > 3 else ""
    extra = parts[4] if len(parts) > 4 else ""

    try:
        chat_id = int(chat_id_s)
    except ValueError:
        bot.answer_callback_query(c.id)
        return

    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        bot.answer_callback_query(c.id, err or "Недостаточно прав.", show_alert=True)
        return

    # Clear pending states on most actions
    if action not in ("dur_prompt", "exc_add", "exc_del_prompt"):
        _as_pending_pop_cid_sec("pending_antispam_duration", user.id)
        _pending_msg_pop("pending_antispam_duration_msg", user.id)
        _as_pending_pop_cid_sec("pending_antispam_exception", user.id)
        _pending_msg_pop("pending_antispam_exception_msg", user.id)
        _as_pending_pop_cid_sec("pending_antispam_exception_delete", user.id)
        _pending_msg_pop("pending_antispam_exception_delete_msg", user.id)

    # ── open main antispam page ──
    if action == "open":
        text = _render_antispam_main(chat_id)
        kb = _build_antispam_main_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── main page exceptions panel (expand/collapse in main keyboard) ──
    # Callback format: stas:mainpage:<chat_id>:<subpage>
    # The parser puts <subpage> into `section`; it is not a section name here.
    if action == "mainpage":
        subpage = section  # "exceptions" | "exceptions_list"
        if subpage == "exceptions_list":
            # Delete current message and send a new one with the exceptions list
            exceptions = _antispam_get_exceptions(chat_id)
            if exceptions:
                exc_lines = "\n".join(
                    f"{i + 1}. <code>{_html.escape(e)}</code>" for i, e in enumerate(exceptions)
                )
                list_text = f"<b>Исключения анти-спама:</b>\n\n{exc_lines}"
            else:
                list_text = "<b>Исключения анти-спама:</b>\n\nСписок пуст."
            kb_list = InlineKeyboardMarkup(row_width=1)
            b_back_list = InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{chat_id}:exceptions")
            try:
                b_back_list.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
                b_back_list.style = "primary"
            except Exception:
                pass
            kb_list.add(b_back_list)
            try:
                bot.delete_message(msg_chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_message(
                msg_chat.id,
                list_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb_list,
            )
            bot.answer_callback_query(c.id)
            return

        # For "exceptions" (open panel) or any other mainpage value — show main with expanded kb
        text = _render_antispam_main(chat_id)
        kb = _build_antispam_main_keyboard(chat_id, subpage if subpage == "exceptions" else "main")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── sub-section page ──
    if action == "sub":
        if section not in _ANTISPAM_SECTIONS:
            bot.answer_callback_query(c.id)
            return
        page = extra or "main"
        text = _render_antispam_section(chat_id, section, page)
        kb = _build_antispam_section_keyboard(chat_id, section, page)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── validate section for actions below ──
    if section not in _ANTISPAM_SECTIONS:
        bot.answer_callback_query(c.id)
        return

    sec = _antispam_get_section(chat_id, section)

    # ── no-op actions (label buttons) ──
    if action in _NOOP_ACTIONS:
        bot.answer_callback_query(c.id)
        return

    # ── status set (ON/OFF) ──
    if action == "statusset":
        sec["enabled"] = (extra == "1")
        _antispam_save_section(chat_id, section, sec)

    # ── delete messages set (ON/OFF) ──
    elif action == "delset":
        sec["delete_messages"] = (extra == "1")
        _antispam_save_section(chat_id, section, sec)

    # ── legacy toggle enabled (backwards compat) ──
    elif action == "toggle":
        sec["enabled"] = not sec["enabled"]
        _antispam_save_section(chat_id, section, sec)

    # ── legacy toggle delete messages (backwards compat) ──
    elif action == "deltoggle":
        sec["delete_messages"] = not sec["delete_messages"]
        _antispam_save_section(chat_id, section, sec)

    # ── page switch ──
    elif action == "page":
        if extra not in _SECTION_VALID_PAGES:
            extra = "main"

        _exc_pages = ("exceptions", "exceptions_list", "exceptions_delete")

        # Special handling for exceptions_list: delete message and send text list
        if extra == "exceptions_list":
            exceptions = _antispam_get_exceptions(chat_id)
            if exceptions:
                exc_lines = "\n".join(
                    f"{i + 1}. <code>{_html.escape(e)}</code>" for i, e in enumerate(exceptions)
                )
                list_text = f"<b>Общие исключения анти-спама:</b>\n\n{exc_lines}"
            else:
                list_text = "<b>Общие исключения анти-спама:</b>\n\nСписок пуст."
            kb_list = InlineKeyboardMarkup(row_width=1)
            b_back_list = InlineKeyboardButton("Назад", callback_data=f"stas:page:{chat_id}:{section}:exceptions")
            try:
                b_back_list.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
                b_back_list.style = "primary"
            except Exception:
                pass
            kb_list.add(b_back_list)
            try:
                bot.delete_message(msg_chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_message(
                msg_chat.id,
                list_text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=kb_list,
            )
            bot.answer_callback_query(c.id)
            return

        # Exceptions pages: show main antispam text (exceptions are global)
        if extra in _exc_pages:
            text = _render_antispam_main(chat_id)
            kb = _build_antispam_main_exc_keyboard(chat_id, section, extra)
            if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
                bot.answer_callback_query(c.id, "Не удалось открыть страницу.", show_alert=True)
                return
            bot.answer_callback_query(c.id)
            return

        text = _render_antispam_section(chat_id, section, extra)
        kb = _build_antispam_section_keyboard(chat_id, section, extra)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть страницу.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── set punishment type ──
    elif action == "ptype":
        pt = (extra or "").strip().lower()
        if pt in ("warn", "mute", "ban", "kick"):
            sec["punish"]["type"] = pt
            if pt == "kick":
                sec["punish"]["duration"] = None
            elif sec["punish"].get("duration") is None:
                sec["punish"]["duration"] = 3600
            _antispam_save_section(chat_id, section, sec)
        text = _render_antispam_section(chat_id, section, "punish")
        kb = _build_antispam_section_keyboard(chat_id, section, "punish")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── duration prompt ──
    elif action == "dur_prompt":
        ptype = sec["punish"]["type"]
        if ptype == "kick":
            bot.answer_callback_query(c.id, "Для исключения длительность не используется.", show_alert=True)
            return

        _as_pending_put("pending_antispam_duration", user.id, chat_id, section)
        _delete_pending_ui(msg_chat.id, "pending_antispam_duration_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:page:{chat_id}:{section}:duration")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_back)

        prompt_text = (
            f"<b>Установите длительность наказания для «{_ANTISPAM_SECTIONS[section]}»</b>\n\n"
            "<b>Подсказка по интервалам:</b>\n"
            "<code>m</code> — минуты, <code>h</code> — часы, <code>d</code> — дни, <code>w</code> — недели\n"
            "<code>м</code> — минуты, <code>ч</code> — часы, <code>д</code> — дни, <code>н</code> — недели\n"
            "Можно комбинировать до <b>3</b> интервалов.\n\n"
            "<b>Примеры:</b> <code>10m</code>, <code>1h 30m</code>, <code>2д</code>, <code>навсегда</code>."
        )
        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_antispam_duration_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── add exception prompt ──
    elif action == "exc_add":
        exceptions = _antispam_get_exceptions(chat_id)
        if len(exceptions) >= MAX_EXCEPTIONS:
            bot.answer_callback_query(c.id, f"Достигнут лимит исключений ({MAX_EXCEPTIONS}).", show_alert=True)
            return

        _as_pending_put("pending_antispam_exception", user.id, chat_id, section)
        _delete_pending_ui(msg_chat.id, "pending_antispam_exception_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{chat_id}:exceptions")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_back)

        prompt_text = (
            "<b>Добавить исключение (общее для всех разделов анти-спама)</b>\n\n"
            "Введите ссылку, которая будет исключена из проверки.\n"
            "<b>Примеры:</b> <code>t.me/mygroup</code>, <code>https://example.com</code>, <code>www.site.com</code>, <code>@myfriend</code>"
        )
        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_antispam_exception_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── delete exception prompt (text input) ──
    elif action == "exc_del_prompt":
        exceptions = _antispam_get_exceptions(chat_id)
        if not exceptions:
            bot.answer_callback_query(c.id, "Список исключений пуст.", show_alert=True)
            return

        _as_pending_put("pending_antispam_exception_delete", user.id, chat_id, section)
        _delete_pending_ui(msg_chat.id, "pending_antispam_exception_delete_msg", user.id, also_msg_id=c.message.message_id)

        kb_prompt = InlineKeyboardMarkup(row_width=1)
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{chat_id}:exceptions")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_prompt.add(b_back)

        prompt_text = (
            "<b>Удалить исключение (общее для всех разделов анти-спама)</b>\n\n"
            "Введите исключение (полностью или частично) для удаления."
        )
        sent = bot.send_message(
            msg_chat.id,
            prompt_text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb_prompt,
        )
        _pending_msg_set("pending_antispam_exception_delete_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── delete exception ──
    elif action == "exc_del":
        try:
            idx = int(extra)
        except Exception:
            bot.answer_callback_query(c.id)
            return
        exceptions = list(_antispam_get_exceptions(chat_id))
        if 0 <= idx < len(exceptions):
            exceptions.pop(idx)
            _antispam_save_exceptions(chat_id, exceptions)
        text = _render_antispam_main(chat_id)
        kb = _build_antispam_main_keyboard(chat_id, "exceptions")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── tg_links flag set (ON/OFF) ──
    elif action == "tgflset":
        if section != "tg_links":
            bot.answer_callback_query(c.id)
            return
        # callback_data format: stas:tgflset:<chat_id>:<section>:<flag>:<val>
        flag = (extra or "").strip().lower()
        val_s = parts[5] if len(parts) > 5 else ""
        if flag == "usernames":
            sec["check_usernames"] = (val_s == "1")
            _antispam_save_section(chat_id, section, sec)
        elif flag == "user_usernames":
            sec["check_user_usernames"] = (val_s == "1")
            _antispam_save_section(chat_id, section, sec)
        elif flag == "bots":
            sec["check_bots"] = (val_s == "1")
            _antispam_save_section(chat_id, section, sec)
        else:
            bot.answer_callback_query(c.id)
            return
        return_page = f"flag_{flag}"
        text = _render_antispam_section(chat_id, section, return_page)
        kb = _build_antispam_section_keyboard(chat_id, section, return_page)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── legacy tgfl toggle ──
    elif action == "tgfl":
        if section != "tg_links":
            bot.answer_callback_query(c.id)
            return
        flag = (extra or "").strip().lower()
        if flag == "usernames":
            sec["check_usernames"] = not bool(sec.get("check_usernames", False))
            _antispam_save_section(chat_id, section, sec)
        elif flag == "user_usernames":
            sec["check_user_usernames"] = not bool(sec.get("check_user_usernames", False))
            _antispam_save_section(chat_id, section, sec)
        elif flag == "bots":
            sec["check_bots"] = not bool(sec.get("check_bots", False))
            _antispam_save_section(chat_id, section, sec)
        else:
            bot.answer_callback_query(c.id)
            return

    # ── per-type flag set for quoting/forwarding (ON/OFF) ──
    elif action == "typeflset":
        if section not in ("quoting", "forwarding"):
            bot.answer_callback_query(c.id)
            return
        # callback_data: stas:typeflset:<chat_id>:<section>:<type_key>:<val>
        type_key = (extra or "").strip().lower()
        val_s = parts[5] if len(parts) > 5 else ""
        if type_key in _FWD_QUOTE_TYPES:
            types = dict(sec.get("types") or {})
            types[type_key] = (val_s == "1")
            sec["types"] = types
            _antispam_save_section(chat_id, section, sec)
        else:
            bot.answer_callback_query(c.id)
            return
        return_page = f"type_{type_key}"
        text = _render_antispam_section(chat_id, section, return_page)
        kb = _build_antispam_section_keyboard(chat_id, section, return_page)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    else:
        bot.answer_callback_query(c.id)
        return

    # Refresh section page
    text = _render_antispam_section(chat_id, section, "main")
    kb = _build_antispam_section_keyboard(chat_id, section, "main")
    if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
        bot.answer_callback_query(c.id, "Не удалось обновить раздел.", show_alert=True)
        return
    bot.answer_callback_query(c.id)


# ─────────────────────────────────────────────
# Pending text input handler (duration + exceptions)
# Called from settings_ui.on_settings_private_input via lazy import.
# ─────────────────────────────────────────────

def handle_antispam_private_pending(m: types.Message) -> bool:
    """
    Handles pending text inputs for anti-spam duration and exceptions.
    Returns True if the message was handled (no further processing needed).
    """
    user_id = int(m.from_user.id)
    ct = getattr(m, "content_type", "text") or "text"

    # ── duration pending ──
    dur_cid, dur_sec = _as_pending_get_cid_sec("pending_antispam_duration", user_id)
    if dur_cid is not None and dur_sec is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:page:{dur_cid}:{dur_sec}:duration"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_duration_msg",
                user_id,
                premium_prefix("Пришлите длительность текстом: 30m, 2h, 3д, 1н или 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(dur_cid, m.from_user)
        if not allowed:
            _as_pending_pop_cid_sec("pending_antispam_duration", user_id)
            _pending_msg_pop("pending_antispam_duration_msg", user_id)
            return True

        raw = (m.text or "").strip()
        parsed_duration, consumed_tokens, invalid = _parse_duration_prefix(
            raw, allow_russian_duration=True, max_parts=3,
        )
        total_tokens = len(raw.split()) if raw else 0
        if invalid or parsed_duration is None or consumed_tokens == 0 or consumed_tokens != total_tokens:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:page:{dur_cid}:{dur_sec}:duration"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_duration_msg",
                user_id,
                premium_prefix("Неверный формат. Используйте до 3 интервалов: 30m, 1h 2m, 2д, навсегда."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        duration = int(parsed_duration)
        if duration != 0 and (duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:page:{dur_cid}:{dur_sec}:duration"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_duration_msg",
                user_id,
                premium_prefix("Длительность должна быть от 1 минуты до 365 дней, либо 'навсегда'."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        sec = _antispam_get_section(dur_cid, dur_sec)
        ptype = sec["punish"]["type"]
        if ptype == "kick":
            _as_pending_pop_cid_sec("pending_antispam_duration", user_id)
            _pending_msg_pop("pending_antispam_duration_msg", user_id)
            bot.send_message(
                m.chat.id,
                premium_prefix("Для исключения длительность не используется."),
                parse_mode="HTML",
            )
            return True

        sec["punish"]["duration"] = int(duration)
        _antispam_save_section(dur_cid, dur_sec, sec)

        _as_pending_pop_cid_sec("pending_antispam_duration", user_id)
        prompt_id = _pending_msg_pop("pending_antispam_duration_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        ok_text = premium_prefix("✅ Длительность наказания установлена.")
        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:page:{dur_cid}:{dur_sec}:duration")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    # ── exception pending ──
    exc_cid, exc_sec = _as_pending_get_cid_sec("pending_antispam_exception", user_id)
    if exc_cid is not None and exc_sec is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_msg",
                user_id,
                premium_prefix("Пришлите шаблон исключения текстом."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(exc_cid, m.from_user)
        if not allowed:
            _as_pending_pop_cid_sec("pending_antispam_exception", user_id)
            _pending_msg_pop("pending_antispam_exception_msg", user_id)
            return True

        pattern = (m.text or "").strip()
        if not pattern:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_msg",
                user_id,
                premium_prefix("Шаблон не может быть пустым."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        if len(pattern) > MAX_EXCEPTION_PATTERN_LEN:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_msg",
                user_id,
                premium_prefix(f"Шаблон не должен превышать {MAX_EXCEPTION_PATTERN_LEN} символов."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        if not _VALID_EXCEPTION_RE.match(pattern):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_msg",
                user_id,
                premium_prefix(
                    "Исключение должно быть ссылкой или упоминанием (например: "
                    "<code>https://example.com</code>, <code>www.site.com</code>, "
                    "<code>t.me/channel</code>, <code>@username</code>)."
                ),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        exceptions = list(_antispam_get_exceptions(exc_cid))
        if pattern.lower() not in [e.lower() for e in exceptions]:
            if len(exceptions) < MAX_EXCEPTIONS:
                exceptions.append(pattern)
                _antispam_save_exceptions(exc_cid, exceptions)

        _as_pending_pop_cid_sec("pending_antispam_exception", user_id)
        prompt_id = _pending_msg_pop("pending_antispam_exception_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        ok_text = premium_prefix(f"✅ Исключение <code>{_html.escape(pattern)}</code> добавлено.")
        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_cid}:exceptions")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    # ── exception delete pending ──
    exc_del_cid, exc_del_sec = _as_pending_get_cid_sec("pending_antispam_exception_delete", user_id)
    if exc_del_cid is not None and exc_del_sec is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_del_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_delete_msg",
                user_id,
                premium_prefix("Пришлите шаблон исключения для удаления текстом."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(exc_del_cid, m.from_user)
        if not allowed:
            _as_pending_pop_cid_sec("pending_antispam_exception_delete", user_id)
            _pending_msg_pop("pending_antispam_exception_delete_msg", user_id)
            return True

        pattern = (m.text or "").strip()
        if not pattern:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_del_cid}:exceptions"))
            _replace_pending_ui(
                m.chat.id,
                "pending_antispam_exception_delete_msg",
                user_id,
                premium_prefix("Шаблон не может быть пустым."),
                reply_markup=kb_err,
                parse_mode="HTML",
            )
            return True

        exceptions = list(_antispam_get_exceptions(exc_del_cid))

        # Find by exact match (case-insensitive), then partial match
        pattern_lower = pattern.lower()
        to_delete_idx = None
        for i, exc in enumerate(exceptions):
            if exc.lower() == pattern_lower:
                to_delete_idx = i
                break
        if to_delete_idx is None:
            for i, exc in enumerate(exceptions):
                if pattern_lower in exc.lower():
                    to_delete_idx = i
                    break

        _as_pending_pop_cid_sec("pending_antispam_exception_delete", user_id)
        prompt_id = _pending_msg_pop("pending_antispam_exception_delete_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        if to_delete_idx is None:
            ok_text = premium_prefix(f"❌ Исключение не найдено: <code>{_html.escape(pattern)}</code>.")
        else:
            deleted_exc = exceptions.pop(to_delete_idx)
            _antispam_save_exceptions(exc_del_cid, exceptions)
            ok_text = premium_prefix(f"✅ Исключение <code>{_html.escape(deleted_exc)}</code> удалено.")

        kb_ok = InlineKeyboardMarkup()
        b_back = InlineKeyboardButton("Назад", callback_data=f"stas:mainpage:{exc_del_cid}:exceptions")
        try:
            b_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
            b_back.style = "primary"
        except Exception:
            pass
        kb_ok.add(b_back)
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    return False


# ─────────────────────────────────────────────
# Runtime anti-spam checking
# ─────────────────────────────────────────────

def _antispam_user_allowed(chat_id: int, user: types.User) -> bool:
    """Returns False if the user should NOT be checked (is exempt)."""
    if not user:
        return False
    uid = int(getattr(user, "id", 0) or 0)
    if uid <= 0:
        return False
    if is_owner(user) or is_dev(user):
        return False
    try:
        if int(get_user_rank(chat_id, uid) or 0) > 0:
            return False
    except Exception:
        pass
    if bool(getattr(user, "is_bot", False)):
        return False
    bot_id = _get_bot_id()
    if bot_id and uid == bot_id:
        return False
    try:
        member = tg_get_chat_member(chat_id, uid)
        if getattr(member, "status", "") in ("administrator", "creator"):
            return False
    except Exception:
        pass
    return True


def _antispam_matches_exceptions(text: str, exceptions: list) -> bool:
    """Returns True if any exception pattern is found in the text (case-insensitive)."""
    tl = text.lower()
    for exc in exceptions:
        es = (exc or "").strip().lower()
        if es and es in tl:
            return True
    return False


def _antispam_apply_punishment(
    chat_id: int,
    user: types.User,
    section: str,
    sec_settings: dict,
    message_id: int,
) -> None:
    target_id = int(getattr(user, "id", 0) or 0)
    if target_id <= 0:
        return

    punish = sec_settings.get("punish") or {}
    ptype = str(punish.get("type") or "warn").lower()
    duration_raw = punish.get("duration")
    section_label = _ANTISPAM_SECTIONS.get(section, section)
    reason = f"Анти-спам: {section_label}"

    actor_id = _get_bot_id() or target_id

    # Delete message if configured
    if sec_settings.get("delete_messages") and message_id:
        if _bot_can_delete_messages(chat_id):
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass

    if ptype == "warn":
        action_id, count_after, _ = _mod_warn_add(chat_id, actor_id, target_id, reason)
        warn_limit = int((_mod_get_chat(chat_id).get("settings") or {}).get("warn_limit", 3))
        if count_after >= warn_limit:
            try:
                _auto_punish_for_warns(chat_id, get_bot_me(), target_id,
                                       source_tag="#АНТИ_СПАМ")
            except Exception:
                pass
        else:
            _send_punish_message_with_button(
                chat_id, "warn", action_id, target_id, actor_id,
                None, reason,
                warn_count=count_after, warn_limit=warn_limit,
            )
        _mod_log_action(chat_id, "warn", actor_id, target_id, reason=reason,
                        source_tag="#АНТИ_СПАМ")
        return

    if ptype == "kick":
        try:
            if hasattr(bot, "ban_chat_member"):
                bot.ban_chat_member(chat_id, target_id)
            else:
                bot.kick_chat_member(chat_id, target_id)
        except Exception:
            return
        try:
            bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        except Exception:
            pass
        _mark_farewell_suppressed(chat_id, target_id)
        kick_action_id = _mod_new_action_id()
        kick_row = {
            "id": kick_action_id,
            "target_id": target_id,
            "actor_id": actor_id,
            "created_at": _time.time(),
            "duration": 0, "until": 0,
            "reason": reason, "active": True,
            "auto": True, "source": f"antispam:{section}",
        }
        _mod_log_append(chat_id, "kick", kick_row)
        # Kicks have no undo button — send plain message
        try:
            emoji_p = f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">⚠️</tg-emoji>'
            tname = link_for_user(chat_id, target_id)
            aname = link_for_user(chat_id, actor_id)
            txt = (
                f"{emoji_p} <b>Пользователь</b> {tname} <b>наказан.</b>\n"
                f"<b>Наказание:</b> Исключение\n"
                f"<b>Причина:</b> {_html.escape(reason)}\n\n"
                f"<b>Администратор:</b> {aname}"
            )
            bot.send_message(chat_id, txt, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
        _mod_log_action(chat_id, "kick", actor_id, target_id, reason=reason,
                        source_tag="#АНТИ_СПАМ")
        return

    # mute / ban
    try:
        duration = int(duration_raw) if duration_raw is not None else 3600
    except Exception:
        duration = 3600
    if duration != 0:
        duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, duration))

    until_ts = None
    ok = False
    if ptype == "ban":
        ok, _, until_ts = _apply_ban(chat_id, target_id, duration)
    else:
        ok, _, until_ts = _apply_mute(chat_id, target_id, duration)
        ptype = "mute"
    if not ok:
        return

    action_id = _mod_new_action_id()
    row = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor_id,
        "created_at": _time.time(),
        "duration": int(duration or 0),
        "until": int(until_ts or 0),
        "reason": reason,
        "active": True,
        "auto": True,
        "source": f"antispam:{section}",
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
    _send_punish_message_with_button(
        chat_id, ptype, action_id, target_id, actor_id,
        int(duration or 0), reason,
        until_ts=int(until_ts or 0),
        created_at=row["created_at"],
    )
    _mod_log_action(chat_id, ptype, actor_id, target_id,
                    duration=int(duration or 0), reason=reason,
                    until_ts=int(until_ts or 0) if until_ts else None,
                    source_tag="#АНТИ_СПАМ")


def _extract_entity_urls(m: types.Message) -> list[str]:
    """
    Extract all URLs embedded in message entities (text_link, url types).
    These are hyperlinks hidden inside visible text like [word](https://spam.com)
    and are not part of the raw text string.
    """
    urls: list[str] = []
    msg_text = getattr(m, "text", None) or ""
    msg_caption = getattr(m, "caption", None) or ""
    # Map each entity attribute to its corresponding source text
    for attr, src in (("entities", msg_text), ("caption_entities", msg_caption)):
        for entity in (getattr(m, attr, None) or []):
            etype = getattr(entity, "type", "")
            if etype == "url":
                offset = int(getattr(entity, "offset", 0) or 0)
                length = int(getattr(entity, "length", 0) or 0)
                url = src[offset:offset + length]
                if url:
                    urls.append(url)
            elif etype == "text_link":
                url = str(getattr(entity, "url", "") or "")
                if url:
                    urls.append(url)
    return urls


def _should_block_by_type(types_config: dict, src_type: str) -> bool:
    """
    Returns True if the message should be blocked based on per-type config.
    When no types are configured (all False), defaults to blocking everything
    to preserve legacy behaviour where the section was a simple on/off switch.
    """
    any_type_enabled = any(types_config.values())
    # Legacy fallback: if no specific type is selected, block all sources
    return bool(types_config.get(src_type)) if any_type_enabled else True


def _antispam_runtime_check(m: types.Message) -> None:
    chat_id = int(m.chat.id)
    if not is_group_approved(chat_id):
        return

    # Пропускаем сообщения от привязанного канала (sender_chat — канал)
    sender_chat = getattr(m, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) == "channel":
        return

    user = getattr(m, "from_user", None)
    if not _antispam_user_allowed(chat_id, user):
        return

    text = (getattr(m, "text", None) or getattr(m, "caption", None) or "")
    msg_id = int(getattr(m, "message_id", 0) or 0)

    # Collect entity-embedded URLs (links hidden inside visible text)
    entity_urls = _extract_entity_urls(m)
    # Combined scan text: visible text + all embedded URLs
    combined_text = text + (" " + " ".join(entity_urls) if entity_urls else "")

    # Shared exceptions list (used for all sections)
    exceptions = _antispam_get_exceptions(chat_id)

    # ── tg_links ──
    sec_tg = _antispam_get_section(chat_id, "tg_links")
    if sec_tg["enabled"]:
        violation = False
        # Check t.me / telegram.me URLs in text and entity URLs
        if _TG_URL_RE.search(combined_text):
            violation = True
        # Check @usernames of groups/channels if toggle is on.
        if not violation and sec_tg.get("check_usernames") and _TG_USERNAME_RE.search(combined_text):
            violation = True
        # Check @...bot usernames if bots toggle is on
        if not violation and sec_tg.get("check_bots"):
            for match in _TG_USERNAME_RE.finditer(combined_text):
                uname = match.group(0).lower()
                if uname.endswith("bot"):
                    violation = True
                    break
        # Check regular user @usernames if user_usernames toggle is on
        if not violation and sec_tg.get("check_user_usernames") and _TG_USERNAME_RE.search(combined_text):
            violation = True
        if violation:
            # Internal-link exception: if ALL detected t.me URLs (from text and
            # entity links) are links to messages within the same group, do not punish.
            tg_urls_from_text = _TG_URL_RE.findall(combined_text)
            tg_urls_from_entities = [u for u in entity_urls if _TG_URL_RE.search(u)]
            tg_urls_found = tg_urls_from_text + tg_urls_from_entities
            if tg_urls_found and all(
                _is_internal_group_link(u, chat_id) for u in tg_urls_found
            ):
                violation = False
        if violation and not _antispam_matches_exceptions(combined_text, exceptions):
            _antispam_apply_punishment(chat_id, user, "tg_links", sec_tg, msg_id)
            return

    # ── forwarding ──
    sec_fwd = _antispam_get_section(chat_id, "forwarding")
    if sec_fwd["enabled"]:
        is_forwarded = bool(
            getattr(m, "forward_from", None) or
            getattr(m, "forward_from_chat", None) or
            getattr(m, "forward_date", None)
        )
        if is_forwarded:
            # Same-group exception: forwarded from the same group → skip
            fwd_from_chat = getattr(m, "forward_from_chat", None)
            if fwd_from_chat and getattr(fwd_from_chat, "id", None) == chat_id:
                pass
            else:
                # Determine forward source type
                fwd_from = getattr(m, "forward_from", None)
                if fwd_from_chat:
                    chat_type = getattr(fwd_from_chat, "type", "") or ""
                    if chat_type == "channel":
                        src_type = "channels"
                    elif chat_type in ("group", "supergroup"):
                        src_type = "groups"
                    else:
                        src_type = "users"
                elif fwd_from:
                    src_type = "bots" if getattr(fwd_from, "is_bot", False) else "users"
                else:
                    src_type = "users"

                fwd_types = sec_fwd.get("types") or {}
                if _should_block_by_type(fwd_types, src_type):
                    fwd_text = combined_text
                    if fwd_from:
                        fwd_text += " " + str(getattr(fwd_from, "username", "") or "")
                    if fwd_from_chat:
                        fwd_text += " " + str(getattr(fwd_from_chat, "username", "") or "")
                        fwd_text += " " + str(getattr(fwd_from_chat, "title", "") or "")
                    if not _antispam_matches_exceptions(fwd_text, exceptions):
                        _antispam_apply_punishment(chat_id, user, "forwarding", sec_fwd, msg_id)
                        return

    # ── quoting ──
    sec_qt = _antispam_get_section(chat_id, "quoting")
    if sec_qt["enabled"]:
        # Quote = any reply to a message (reply_to_message is set), including
        # explicit text-selection quotes (Bot API 7.0 m.quote feature).
        reply_msg = getattr(m, "reply_to_message", None)
        is_quote = bool(
            reply_msg is not None or
            getattr(m, "quote", None)
        )
        if is_quote:
            # Same-group exception: if the replied-to message was sent by the same
            # group (anonymous admin post) or by a regular user within this group,
            # the citation is internal and must not trigger punishment.
            if reply_msg:
                replied_sender_chat = getattr(reply_msg, "sender_chat", None)
                replied_sender_chat_id = getattr(replied_sender_chat, "id", None) if replied_sender_chat else None
                if replied_sender_chat_id is None or replied_sender_chat_id == chat_id:
                    # Regular user reply (sender_chat is None) or anonymous post
                    # from the same group → always exempt
                    pass
                else:
                    # Determine quote source type from reply_to_message
                    sc_type = getattr(replied_sender_chat, "type", "") or ""
                    if sc_type == "channel":
                        src_type = "channels"
                    elif sc_type in ("group", "supergroup"):
                        src_type = "groups"
                    else:
                        src_type = "users"

                    qt_types = sec_qt.get("types") or {}
                    if _should_block_by_type(qt_types, src_type):
                        if not _antispam_matches_exceptions(combined_text, exceptions):
                            _antispam_apply_punishment(chat_id, user, "quoting", sec_qt, msg_id)
                            return
            else:
                # m.quote is set without a reply_to_message — treat as user/same-group
                pass

    # ── all_links ──
    sec_al = _antispam_get_section(chat_id, "all_links")
    if sec_al["enabled"]:
        all_urls_found = _ALL_LINKS_RE.findall(combined_text) + (entity_urls or [])
        if all_urls_found:
            # Internal-link exception: skip if every URL is an internal group message link
            if not all(
                _is_internal_group_link(u, chat_id) for u in all_urls_found
            ):
                if not _antispam_matches_exceptions(combined_text, exceptions):
                    _antispam_apply_punishment(chat_id, user, "all_links", sec_al, msg_id)
                    return


_ANTISPAM_CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio", "animation",
    "sticker", "voice", "video_note",
]


@bot.message_handler(
    content_types=_ANTISPAM_CONTENT_TYPES,
    func=lambda m: m.chat.type in ("group", "supergroup"),
)
def antispam_runtime_handler(m: types.Message) -> None:
    try:
        _antispam_runtime_check(m)
    except Exception:
        pass
    return ContinueHandling()


__all__ = [name for name in globals() if not name.startswith("__")]
