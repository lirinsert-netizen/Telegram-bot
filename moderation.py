"""
moderation.py — Команды модерации:
  /мут, /бан, /кик, /варн, /делварн, /снятьварн,
  /делбан, /снятьмут, /удалить, /мутлист, /банлист,
  /варнлист, punish_un callback, modlist callback,
  /adminstats и связанн. алиасы.
"""
from __future__ import annotations
import time
import threading
import asyncio
import re as _re
import html as _html

from config import (
    os, json, re, random, datetime,
    Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    bot, bot_raw, tg_client,
    TOKEN, OWNER_USERNAME, DATA_DIR, API_BASE_URL,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    PREMIUM_PREFIX_EMOJI_ID, EMOJI_RATE_LIMIT_ID,
    EMOJI_DEV_ID, EMOJI_MEMBER_ID, EMOJI_ADMIN_ID, EMOJI_OWNER_ID,
    EMOJI_LIST_ID, EMOJI_ADMIN_RIGHTS_ID, EMOJI_BTN_UNADMIN_ID, EMOJI_BTN_KICK_ID,
    EMOJI_ROLE_OWNER_ID, EMOJI_ROLE_CHIEF_ADMIN_ID, EMOJI_ROLE_ADMIN_ID,
    EMOJI_ROLE_MOD_ID, EMOJI_ROLE_TRAINEE_ID,
    EMOJI_USER_ROLE_TEXT_ID, EMOJI_ROLE_ACTION_ID,
    EMOJI_SCOPE_GROUP_ID, EMOJI_SCOPE_PM_ID, EMOJI_SCOPE_ALL_ID,
    EMOJI_BTN_UNADMIN_ID, EMOJI_BTN_KICK_ID,
    get_user_id_by_username_mtproto,
    get_bot_me,
)
from persistence import (
    VERIFY_ADMINS, VERIFY_DEV,
    CLOSE_CHAT_STATE, GROUP_STATS, GROUP_SETTINGS,
    CHAT_SETTINGS, MODERATION, PENDING_GROUPS,
    USERS, GLOBAL_USERS, PROFILES,
    CHAT_ROLES, ROLE_PERMS,
    STATS,
    save_verify_admins, save_verify_dev,
    save_close_chat_state,
    save_group_stats, save_group_settings,
    save_chat_settings, save_moderation, save_pending_groups,
    save_users, save_global_users, save_profiles,
    save_chat_roles, save_role_perms,
    tg_get_chat, tg_get_chat_member,
    tg_invalidate_member_cache, tg_invalidate_chat_cache,
    tg_invalidate_chat_member_caches,
    load_json_file, save_json_file, throttled_save_json_file,
    _is_duplicate_callback_query,
    GLOBAL_LAST_SEEN_UPDATE_SECONDS,
    send_log_event,
)
import helpers
from helpers import *

# ==== МОДЕРАЦИЯ: MUTE / BAN / WARN / LISTS / DEL ==== 
from cmd_basic import _kick_with_unban

MIN_PUNISH_SECONDS = 60
MAX_PUNISH_SECONDS = 365 * 24 * 60 * 60

MOD_ERR = {
    "no_perm_mute": "У вашей должности нет права выдавать ограничения.",
    "no_perm_ban": "У вашей должности нет права выдавать блокировки.",
    "no_perm_warn": "У вашей должности нет права использовать предупреждения.",
    "no_perm_del": TEXTS["no_perm_del"],
    "bot_no_del": TEXTS["bot_no_del"],
    "bot_no_mute": "У бота нет прав для ограничения. Дайте право «Блокировка пользователей».",
    "bot_no_ban": "У бота нет прав для блокировки. Дайте право «Блокировка пользователей».",
    "user_not_found": "Не удалось определить пользователя. Укажи @username/ID/ссылку или используй ответ на сообщение.",
    "bad_duration": "Время наказания должно быть от 1 минуты до 365 дней. Можно комбинировать до 3 интервалов: 1h 2m, 2mou 1d.",
    "no_rights": "Недостаточно прав для использования команды.",
    "no_rights_delete": "Недостаточно прав для удаления сообщений.",
}

# Временное подавление farewell для пользователей, удалённых модерацией (/ban, /kick).
# Ключ: (chat_id, user_id), значение: unix_ts до которого farewell игнорируется.
_FAREWELL_SUPPRESS: dict[tuple[int, int], float] = {}
FAREWELL_SUPPRESS_SECONDS = 120

_REPLY_BLOCKED_ON_LINKED_CHANNEL_CMDS = {
    "mute", "мут", "ban", "бан", "warn", "варн", "kick", "кик",
    "delmute", "delban", "delwarn", "делмут", "делбан", "делварн",
    "unmute", "размут", "анмут", "unban", "разбан", "unwarn", "снятьварн", "анварн",
    "del", "дел",
}


def _mark_farewell_suppressed(chat_id: int, user_id: int, seconds: int = FAREWELL_SUPPRESS_SECONDS):
    _FAREWELL_SUPPRESS[(int(chat_id), int(user_id))] = time.time() + max(1, int(seconds))


def _is_farewell_suppressed(chat_id: int, user_id: int) -> bool:
    key = (int(chat_id), int(user_id))
    until_ts = float(_FAREWELL_SUPPRESS.get(key, 0) or 0)
    if until_ts <= 0:
        return False
    if until_ts < time.time():
        _FAREWELL_SUPPRESS.pop(key, None)
        return False
    return True


def _log_mod_action(chat_id: int, event: str, actor_id: int, target_id: int,
                    duration: int | None = None, reason: str = "",
                    until_ts: int | None = None, msg_link: str | None = None,
                    forward_from_chat_id: int | None = None,
                    forward_message_id: int | None = None,
                    source_tag: str | None = None) -> None:
    """Формирует и отправляет сообщение о действии модерации в лог-канал.

    source_tag — необязательный дополнительный хештег для автоматических
    наказаний (например, '#АНТИ_ФЛУД', '#ЗАПРЕЩЁННЫЕ_СЛОВА' и т.д.).
    """
    event_labels = {
        "ban":    "#БЛОКИРОВКА",
        "unban":  "#СНЯТИЕ_БЛОКИРОВКИ",
        "mute":   "#ОГРАНИЧЕНИЕ",
        "unmute": "#СНЯТИЕ_ОГРАНИЧЕНИЯ",
        "kick":   "#ИСКЛЮЧЕНИЕ",
        "warn":   "#ПРЕДУПРЕЖДЕНИЕ",
        "unwarn": "#СНЯТИЕ_ПРЕДУПРЕЖДЕНИЯ",
    }
    label = event_labels.get(event, f"#{event.upper()}")
    label_line = f"<b>{label}</b>"
    if source_tag:
        label_line += f" {source_tag}"
    try:
        chat = bot.get_chat(chat_id)
        chat_link = f'<a href="https://t.me/c/{str(chat.id).lstrip("-100")}/0">{_html.escape(chat.title or str(chat_id))}</a>'
    except Exception:
        chat_link = f"<code>{chat_id}</code>"
    try:
        actor = bot.get_chat(actor_id)
        actor_name = _html.escape(actor.first_name or str(actor_id))
        actor_link = f'<a href="tg://user?id={actor_id}">{actor_name}</a> [<code>{actor_id}</code>]'
    except Exception:
        actor_link = f"<code>{actor_id}</code>"
    try:
        target = bot.get_chat(target_id)
        target_name = _html.escape(target.first_name or str(target_id))
        target_link = f'<a href="tg://user?id={target_id}">{target_name}</a> [<code>{target_id}</code>]'
    except Exception:
        target_link = f"<code>{target_id}</code>"

    lines = [
        label_line,
        f"<b>Группа:</b> {chat_link}",
        f"<b>Пользователь:</b> {target_link}",
        f"<b>Администратор:</b> {actor_link}",
    ]
    if event in ("mute", "ban"):
        if until_ts and int(until_ts) > 0:
            lines.append(f"<b>Истекает:</b> {_fmt_time(int(until_ts))}")
        elif duration is not None:
            dur_text = _mod_duration_text(duration)
            if dur_text and dur_text != "навсегда":
                lines.append(f"<b>Истекает:</b> {_html.escape(dur_text)}")
            else:
                lines.append("<b>Истекает:</b> навсегда")
    if reason:
        lines.append(f"<b>Причина:</b> {_html.escape(reason)}")
    if msg_link:
        lines.append(f'<a href="{msg_link}">Перейти к сообщению</a>')
    # Hashtag footer for filtering
    lines.append(f"#id{target_id} #id{abs(chat_id)}")

    send_log_event(chat_id, event, "\n".join(lines),
                   forward_from_chat_id=forward_from_chat_id,
                   forward_message_id=forward_message_id)


def _mod_get_chat(chat_id: int) -> dict:
    cid = str(chat_id)
    ch = MODERATION.get(cid)
    if ch is None:
        ch = {
            "settings": {
                "warn_enabled": True,
                "warn_limit": 3,
                "warn_punish": {
                    "type": "mute",
                    "duration": 24 * 60 * 60,
                    "reason": "",
                },
                "antiflood": {
                    "enabled": False,
                    "delete_messages": False,
                    "period": 10,
                    "messages": 6,
                    "punish": {
                        "type": "mute",
                        "duration": 30 * 60,
                        "reason": "",
                    },
                },
                "antiraid": {
                    "enabled": False,
                    "count": 10,
                    "period": 10,
                    "punish": {
                        "type": "mute",
                        "duration": 30 * 60,
                        "reason": "",
                    },
                },
            },
            "active": {"mute": {}, "ban": {}},
            "warns": {},
            "logs": {"mute": [], "ban": [], "warn": [], "kick": []},
        }
        MODERATION[cid] = ch

    settings = ch.get("settings") or {}
    warn_enabled = settings.get("warn_enabled")
    if not isinstance(warn_enabled, bool):
        warn_enabled = True
    settings["warn_enabled"] = warn_enabled

    warn_limit = settings.get("warn_limit")
    try:
        warn_limit = int(warn_limit)
    except Exception:
        warn_limit = 3
    settings["warn_limit"] = max(2, min(10, warn_limit))

    wp = settings.get("warn_punish") or {}
    wp_type = (wp.get("type") or "mute").lower()
    if wp_type not in ("mute", "ban", "kick"):
        wp_type = "mute"

    wp_duration = wp.get("duration")
    if wp_type in ("mute", "ban"):
        if wp_duration is None:
            wp_duration = 24 * 60 * 60
        else:
            try:
                wp_duration = int(wp_duration)
            except Exception:
                wp_duration = 24 * 60 * 60
            if wp_duration != 0:
                wp_duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, wp_duration))
    else:
        wp_duration = None

    settings["warn_punish"] = {
        "type": wp_type,
        "duration": wp_duration,
        "reason": str(wp.get("reason") or ""),
    }

    af = settings.get("antiflood") or {}
    af_enabled = af.get("enabled")
    if not isinstance(af_enabled, bool):
        af_enabled = bool(af_enabled)

    af_delete_messages = af.get("delete_messages")
    if not isinstance(af_delete_messages, bool):
        af_delete_messages = bool(af_delete_messages)

    try:
        af_period = int(af.get("period") or 10)
    except Exception:
        af_period = 10
    af_period = max(3, min(300, af_period))

    try:
        af_messages = int(af.get("messages") or 6)
    except Exception:
        af_messages = 6
    af_messages = max(2, min(50, af_messages))

    af_p = af.get("punish") or {}
    af_type = str(af_p.get("type") or "mute").strip().lower()
    if af_type not in ("mute", "ban", "kick", "warn"):
        af_type = "mute"

    af_duration = af_p.get("duration")
    if af_type != "kick":
        if af_duration is None:
            af_duration = 30 * 60
        else:
            try:
                af_duration = int(af_duration)
            except Exception:
                af_duration = 30 * 60
            if af_duration != 0:
                af_duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, af_duration))
    else:
        af_duration = None

    settings["antiflood"] = {
        "enabled": af_enabled,
        "delete_messages": af_delete_messages,
        "period": af_period,
        "messages": af_messages,
        "punish": {
            "type": af_type,
            "duration": af_duration,
            "reason": str(af_p.get("reason") or ""),
        },
    }

    # antiraid section
    ar = settings.get("antiraid") or {}
    ar_enabled = ar.get("enabled")
    if not isinstance(ar_enabled, bool):
        ar_enabled = bool(ar_enabled)

    try:
        ar_count = int(ar.get("count") or 10)
    except Exception:
        ar_count = 10
    ar_count = max(5, min(25, ar_count))

    try:
        ar_period = int(ar.get("period") or 10)
    except Exception:
        ar_period = 10
    ar_period = max(3, min(60, ar_period))

    ar_p = ar.get("punish") or {}
    ar_type = str(ar_p.get("type") or "mute").strip().lower()
    if ar_type not in ("mute", "ban", "kick"):
        ar_type = "mute"

    ar_duration = ar_p.get("duration")
    if ar_type != "kick":
        if ar_duration is None:
            ar_duration = 30 * 60
        else:
            try:
                ar_duration = int(ar_duration)
            except Exception:
                ar_duration = 30 * 60
            if ar_duration != 0:
                ar_duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, ar_duration))
    else:
        ar_duration = None

    settings["antiraid"] = {
        "enabled": ar_enabled,
        "count": ar_count,
        "period": ar_period,
        "punish": {
            "type": ar_type,
            "duration": ar_duration,
            "reason": str(ar_p.get("reason") or ""),
        },
    }

    # antispam section
    def _asp_section(key: str, extra_keys: list | None = None, dict_keys: list | None = None) -> dict:
        raw = (settings.get("antispam") or {}).get(key) or {}
        p = raw.get("punish") or {}
        pt = str(p.get("type") or "warn").strip().lower()
        if pt not in ("warn", "mute", "ban", "kick"):
            pt = "warn"
        pd = p.get("duration")
        if pt in ("mute", "ban"):
            if pd is None:
                pd = 3600
            else:
                try:
                    pd = int(pd)
                except Exception:
                    pd = 3600
                if pd != 0:
                    pd = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, pd))
        else:
            pd = None
        exc = raw.get("exceptions")
        if not isinstance(exc, list):
            exc = []
        result: dict = {
            "enabled": bool(raw.get("enabled", False)),
            "delete_messages": bool(raw.get("delete_messages", False)),
            "punish": {"type": pt, "duration": pd, "reason": str(p.get("reason") or "")},
            "exceptions": [str(e) for e in exc if e],
        }
        for ek in (extra_keys or []):
            result[ek] = bool(raw.get(ek, False))
        for dk in (dict_keys or []):
            val = raw.get(dk)
            result[dk] = val if isinstance(val, dict) else {}
        return result

    asp = settings.get("antispam") or {}
    settings["antispam"] = {
        "tg_links": _asp_section("tg_links", extra_keys=["check_usernames", "check_user_usernames", "check_bots"]),
        "quoting": _asp_section("quoting", dict_keys=["types"]),
        "forwarding": _asp_section("forwarding", dict_keys=["types"]),
        "all_links": _asp_section("all_links"),
    }

    ch["settings"] = settings

    if not isinstance(ch.get("active"), dict):
        ch["active"] = {}
    ch["active"].setdefault("mute", {})
    ch["active"].setdefault("ban", {})

    if not isinstance(ch.get("warns"), dict):
        ch["warns"] = {}

    if not isinstance(ch.get("logs"), dict):
        ch["logs"] = {}
    ch["logs"].setdefault("mute", [])
    ch["logs"].setdefault("ban", [])
    ch["logs"].setdefault("warn", [])
    ch["logs"].setdefault("kick", [])

    MODERATION[cid] = ch
    return ch


def _mod_save():
    save_moderation()


def _mod_new_action_id() -> str:
    return f"{int(time.time() * 1000)}{random.randint(100, 999)}"


def _mod_fmt_ts(ts: float | int | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _fmt_time(unix_ts: int | float | None, fmt: str = "wDT") -> str:
    """HTML-тег <tg-time> — Telegram покажет локальное время пользователя."""
    if not unix_ts:
        return "—"
    ts = int(unix_ts)
    if fmt:
        return f'<tg-time unix="{ts}" format="{fmt}">...</tg-time>'
    return f'<tg-time unix="{ts}">...</tg-time>'


def _ru_plural(n: int, forms: tuple[str, str, str]) -> str:
    n_abs = abs(int(n))
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return forms[0]
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _format_mod_duration_human(seconds: int) -> str:
    if seconds <= 0:
        return "навсегда"

    remaining = int(seconds)
    units: list[tuple[int, tuple[str, str, str]]] = [
        (365 * 24 * 60 * 60, ("год", "года", "лет")),
        (30 * 24 * 60 * 60, ("месяц", "месяца", "месяцев")),
        (7 * 24 * 60 * 60, ("неделя", "недели", "недель")),
        (24 * 60 * 60, ("день", "дня", "дней")),
        (60 * 60, ("час", "часа", "часов")),
        (60, ("минута", "минуты", "минут")),
    ]

    parts: list[str] = []
    for sec_in_unit, forms in units:
        if remaining >= sec_in_unit:
            amount = remaining // sec_in_unit
            remaining %= sec_in_unit
            parts.append(f"{amount} {_ru_plural(amount, forms)}")

    if not parts:
        return "1 минута"
    return " ".join(parts)


def _mod_duration_text(seconds: int | None) -> str:
    if not seconds or seconds <= 0:
        return "навсегда"
    return _format_mod_duration_human(int(seconds))


def _parse_punish_duration(value: str, is_russian: bool) -> int | None:
    if not value:
        return None
    value = value.strip().lower().strip('.,;:!?)(')

    if value in ("forever", "навсегда"):
        return 0

    num_part = ''
    unit_part = ''
    for ch in value:
        if ch.isdigit():
            if unit_part:
                return None
            num_part += ch
        else:
            unit_part += ch

    if not num_part or not unit_part:
        return None

    try:
        amount = int(num_part)
    except ValueError:
        return None

    if amount <= 0:
        return None

    if not is_russian:
        if unit_part == 'm':
            seconds = amount * 60
        elif unit_part == 'h':
            seconds = amount * 60 * 60
        elif unit_part == 'd':
            seconds = amount * 24 * 60 * 60
        elif unit_part == 'w':
            seconds = amount * 7 * 24 * 60 * 60
        elif unit_part == 'mou':
            seconds = amount * 30 * 24 * 60 * 60
        elif unit_part == 'y':
            seconds = amount * 365 * 24 * 60 * 60
        else:
            return None
    else:
        if unit_part in ('м', 'мин'):
            seconds = amount * 60
        elif unit_part == 'ч':
            seconds = amount * 60 * 60
        elif unit_part == 'д':
            seconds = amount * 24 * 60 * 60
        elif unit_part == 'н':
            seconds = amount * 7 * 24 * 60 * 60
        elif unit_part == 'мес':
            seconds = amount * 30 * 24 * 60 * 60
        elif unit_part == 'г':
            seconds = amount * 365 * 24 * 60 * 60
        else:
            return None

    if seconds == 0:
        return 0

    if seconds < MIN_PUNISH_SECONDS or seconds > MAX_PUNISH_SECONDS:
        return None
    return int(seconds)


def _parse_duration_token_parts(token: str, allow_russian_duration: bool) -> list[int] | None:
    token_norm = (token or "").strip().lower().strip('.,;:!?)(')
    if not token_norm:
        return None

    unit_table: list[tuple[str, int]] = [
        ("mou", 30 * 24 * 60 * 60),
        ("y", 365 * 24 * 60 * 60),
        ("w", 7 * 24 * 60 * 60),
        ("d", 24 * 60 * 60),
        ("h", 60 * 60),
        ("m", 60),
    ]
    if allow_russian_duration:
        unit_table += [
            ("мес", 30 * 24 * 60 * 60),
            ("мин", 60),
            ("г", 365 * 24 * 60 * 60),
            ("н", 7 * 24 * 60 * 60),
            ("д", 24 * 60 * 60),
            ("ч", 60 * 60),
            ("м", 60),
        ]

    out: list[int] = []
    i = 0
    n = len(token_norm)
    while i < n:
        j = i
        while j < n and token_norm[j].isdigit():
            j += 1
        if j == i:
            return None

        try:
            amount = int(token_norm[i:j])
        except Exception:
            return None
        if amount <= 0:
            return None

        matched_unit: tuple[str, int] | None = None
        for unit, mult in unit_table:
            if token_norm.startswith(unit, j):
                matched_unit = (unit, mult)
                break
        if matched_unit is None:
            return None

        out.append(amount * matched_unit[1])
        i = j + len(matched_unit[0])

    return out or None


def _parse_duration_prefix(
    text: str,
    allow_russian_duration: bool,
    max_parts: int = 3,
) -> tuple[int | None, int, bool]:
    """
    Парсит длительность в начале строки.
    Возвращает: (seconds_or_0_for_forever_or_None, consumed_tokens, is_invalid)
    - consumed_tokens == 0: длительность в начале не обнаружена
    - is_invalid == True: формат времени распознан как ошибочный
    """
    src = (text or "").strip()
    if not src:
        return None, 0, False

    tokens = src.split()
    if not tokens:
        return None, 0, False

    first_norm = tokens[0].strip().lower().strip('.,;:!?)(')
    if first_norm in ("forever", "навсегда"):
        if len(tokens) > 1 and _parse_duration_token_parts(tokens[1], allow_russian_duration):
            return None, 0, True
        return 0, 1, False

    total_seconds = 0
    consumed = 0
    parts_count = 0

    for tok in tokens[:max_parts]:
        parts = _parse_duration_token_parts(tok, allow_russian_duration)
        if not parts:
            break

        if parts_count + len(parts) > max_parts:
            return None, 0, True

        total_seconds += sum(parts)
        parts_count += len(parts)
        consumed += 1

    if consumed == 0:
        return None, 0, False

    if consumed < len(tokens):
        if _parse_duration_token_parts(tokens[consumed], allow_russian_duration):
            return None, 0, True

    if total_seconds < MIN_PUNISH_SECONDS or total_seconds > MAX_PUNISH_SECONDS:
        return None, 0, True

    return int(total_seconds), consumed, False


def _mod_is_row_active(kind: str, row: dict, now_ts: int | None = None) -> bool:
    if not row:
        return False
    if not row.get("active", True):
        return False
    if kind in ("mute", "ban"):
        now_val = int(now_ts or time.time())
        until_ts = int(row.get("until") or 0)
        if until_ts > 0 and until_ts <= now_val:
            return False
    return True


def _mod_deactivate_log(chat_id: int, kind: str, action_id: str, revoked_by: int | None = None):
    row = _mod_find_log(chat_id, kind, action_id)
    if row is None:
        return
    row["active"] = False
    row["revoked_at"] = time.time()
    if revoked_by:
        row["revoked_by"] = revoked_by


def _mod_cleanup_expired(chat_id: int):
    ch = _mod_get_chat(chat_id)
    now_ts = int(time.time())
    changed = False

    for kind in ("mute", "ban"):
        active_map = ch.get("active", {}).get(kind, {})
        for uid_s, rec in list(active_map.items()):
            until_ts = int((rec or {}).get("until") or 0)
            if until_ts > 0 and until_ts <= now_ts:
                active_map.pop(uid_s, None)
                rec_id = str((rec or {}).get("id") or "")
                if rec_id:
                    _mod_deactivate_log(chat_id, kind, rec_id)
                changed = True

        logs = ch.get("logs", {}).get(kind, [])
        for row in logs:
            if row.get("active", True) and not _mod_is_row_active(kind, row, now_ts):
                row["active"] = False
                row["revoked_at"] = float(now_ts)
                changed = True

    if changed:
        _mod_save()


def _mod_parse_target_for_un(m: types.Message, rest: str) -> int | None:
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id

    parts = (rest or "").strip().split(maxsplit=1)
    if not parts:
        return None
    target_id = parse_target_user(m, [parts[0]])
    if isinstance(target_id, int) and target_id > 0:
        return target_id
    return None


def _mod_build_list_header(kind: str, page: int, total_pages: int) -> str:
    title = _mod_list_title(kind)
    return f'<tg-emoji emoji-id="{EMOJI_LOG_ID}">📋</tg-emoji> <b>{title} ({page}/{total_pages})</b>'


MOD_LIST_PAGE_SIZE = 10
LISTS_GROUP_COOLDOWN_SECONDS = 300


def _mod_list_title(kind: str) -> str:
    if kind == "warn":
        return "Список предупреждений"
    if kind == "mute":
        return "Список ограничений"
    return "Черный список"


def _mod_collect_rows(chat_id: int, kind: str) -> list[dict]:
    ch = _mod_get_chat(chat_id)
    rows: list[dict] = []

    if kind in ("mute", "ban"):
        active_map = (ch.get("active") or {}).get(kind) or {}
        for uid_s, rec in active_map.items():
            try:
                target_id = int(uid_s)
            except Exception:
                continue
            row = {
                "id": rec.get("id"),
                "target_id": target_id,
                "actor_id": rec.get("actor_id"),
                "created_at": rec.get("created_at"),
                "duration": rec.get("duration"),
                "until": rec.get("until"),
                "reason": rec.get("reason"),
                "active": True,
            }
            if _mod_is_row_active(kind, row):
                rows.append(row)
    else:
        warns_by_user = ch.get("warns") or {}
        for uid_s, arr in warns_by_user.items():
            try:
                target_id = int(uid_s)
            except Exception:
                continue
            for w in (arr or []):
                if not w.get("active", True):
                    continue
                rows.append({
                    "id": w.get("id"),
                    "target_id": target_id,
                    "actor_id": w.get("actor_id"),
                    "created_at": w.get("created_at"),
                    "duration": None,
                    "reason": w.get("reason"),
                    "active": True,
                })

    rows.sort(key=lambda r: float(r.get("created_at") or 0), reverse=True)
    return rows


def _mod_format_list_item(chat_id: int, row: dict, kind: str) -> str:
    action_id = str(row.get("id") or "")
    target_id = int(row.get("target_id") or 0)
    actor_id = int(row.get("actor_id") or 0)
    created_ts = int(float(row.get("created_at") or 0))
    until_ts = int(float(row.get("until") or 0))
    reason = (row.get("reason") or "").strip() or "—"

    target_name = link_for_user(chat_id, target_id)
    actor_name = link_for_user(chat_id, actor_id)

    kind_title = "Предупреждение" if kind == "warn" else ("Ограничение" if kind == "mute" else "Блокировка")
    lines = [
        f"<b>{kind_title} #{_html.escape(action_id)}</b>",
        f"• <b>Пользователь:</b> {target_name}",
        f"• <b>Выдан:</b> {_fmt_time(created_ts) if created_ts > 0 else '—'}",
    ]

    if kind in ("mute", "ban"):
        if until_ts > 0:
            lines.append(f"• <b>Истекает:</b> {_fmt_time(until_ts)}")
        else:
            lines.append("• <b>Истекает:</b> никогда")

    lines.extend([
        f"• <b>Причина:</b> {_html.escape(reason)}",
        f"• <b>Администратор:</b> {actor_name}",
    ])

    return "\n".join(lines)


def _mod_list_page_text(chat_id: int, kind: str, rows: list[dict], page: int) -> str:
    total_pages = max(1, (len(rows) + MOD_LIST_PAGE_SIZE - 1) // MOD_LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * MOD_LIST_PAGE_SIZE
    end = start + MOD_LIST_PAGE_SIZE

    lines = [_mod_build_list_header(kind, page + 1, total_pages)]
    for row in rows[start:end]:
        lines.append("")
        lines.append(_mod_format_list_item(chat_id, row, kind))

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3897] + "..."
    return text


def _mod_list_keyboard(source_chat_id: int, kind: str, page: int, total_pages: int, viewer_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    nav_row: list[InlineKeyboardButton] = []

    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "Предыдущая страница",
            callback_data=f"modlist:{kind}:{source_chat_id}:{page - 1}:{viewer_id}",
            icon_custom_emoji_id=str(EMOJI_PAGINATION_PREV_ID),
        ))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(
            "Следующая страница",
            callback_data=f"modlist:{kind}:{source_chat_id}:{page + 1}:{viewer_id}",
            icon_custom_emoji_id=str(EMOJI_PAGINATION_NEXT_ID),
        ))

    if nav_row:
        kb.row(*nav_row)

    return kb


def _build_open_pm_markup() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Открыть ЛС бота", url=f"https://t.me/{get_bot_me().username}"))
    return kb


def _reply_sent_to_pm(trigger: types.Message, text: str):
    return bot.reply_to(
        trigger,
        f"<i>{_html.escape(text)}</i>",
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=_build_open_pm_markup(),
    )


def _mod_unpunish_message(text: str) -> str:
    return f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">✅</tg-emoji> {text}'


def _send_mod_list_to_pm(trigger: types.Message, kind: str, rows: list[dict]):
    source_chat_id = trigger.chat.id
    total_pages = max(1, (len(rows) + MOD_LIST_PAGE_SIZE - 1) // MOD_LIST_PAGE_SIZE)
    text = _mod_list_page_text(source_chat_id, kind, rows, 0)
    kb = _mod_list_keyboard(source_chat_id, kind, 0, total_pages, trigger.from_user.id)

    try:
        bot.send_message(
            trigger.from_user.id,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        return bot.reply_to(
            trigger,
            premium_prefix("Не удалось отправить список в ЛС. Напишите боту в ЛС и повторите команду."),
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=_build_open_pm_markup(),
        )
    return _reply_sent_to_pm(trigger, f"{_mod_list_title(kind)} отправлен в ЛС.")


def _extract_command_info(m: types.Message) -> tuple[str | None, str | None, str]:
    text = (m.text or "").strip()
    if not text:
        return None, None, ""

    parts = text.split(maxsplit=1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    prefix = first[0] if first and first[0] in COMMAND_PREFIXES else None
    cmd = first[1:] if prefix else first
    cmd = cmd.split('@', 1)[0].lower()
    return prefix, cmd, rest


def _is_mod_trigger(m: types.Message) -> bool:
    if m.chat.type not in ['group', 'supergroup']:
        return False
    # Пропускаем сообщения от каналов и анонимных администраторов
    if not m.from_user:
        return False
    if should_ignore_text_triggers(m):
        return False
    prefix, cmd, rest = _extract_command_info(m)
    if not cmd:
        return False

    prefixed_only = {
        "mute", "ban", "warn", "kick", "delmute", "delban", "delwarn",
        "warnlist", "mutelist", "banlist", "варнлист", "мутлист", "банлист", "del",
        "unmute", "unban", "unwarn",
        "делмут", "делбан", "делварн",
    }
    ru_plain_allowed = {"мут", "бан", "варн", "кик", "дел", "размут", "разбан", "снятьварн", "анмут", "анварн"}
    ru_list_aliases = {"варнлист", "мутлист", "банлист"}

    if prefix in COMMAND_PREFIXES:
        return cmd in prefixed_only or cmd in ru_plain_allowed

    if cmd not in ru_plain_allowed:
        return cmd in ru_list_aliases and not rest

    # без префикса перехватываем только когда есть reply или аргументы,
    # чтобы обычные фразы не воспринимались как команды
    return bool(m.reply_to_message or rest)


def _parse_target_duration_reason(
    m: types.Message,
    rest: str,
    allow_russian_duration: bool,
    force_reply_target: bool = False,
) -> tuple[int | None, int | None, str | None]:
    target_id: int | None = None
    reason: str | None = None
    duration: int | None = None

    if m.reply_to_message and m.reply_to_message.from_user:
        target_id = m.reply_to_message.from_user.id
        tail = (rest or "").strip()
        if tail:
            maybe_duration, consumed_tokens, invalid = _parse_duration_prefix(
                tail,
                allow_russian_duration=allow_russian_duration,
                max_parts=3,
            )
            if invalid:
                duration = -1
                reason = None
            elif consumed_tokens > 0:
                duration = None if maybe_duration == 0 else maybe_duration
                rem = tail.split()[consumed_tokens:]
                reason = " ".join(rem).strip() or None
            else:
                reason = tail
        return target_id, duration, reason

    if force_reply_target:
        return None, None, None

    if not rest:
        return None, None, None

    parts = rest.split(maxsplit=1)
    first = parts[0]
    after_target = parts[1].strip() if len(parts) > 1 else ""

    target_id = parse_target_user(m, [first])
    if not isinstance(target_id, int) or target_id < 1:
        return None, None, None

    if after_target:
        maybe_duration, consumed_tokens, invalid = _parse_duration_prefix(
            after_target,
            allow_russian_duration=allow_russian_duration,
            max_parts=3,
        )
        if invalid:
            duration = -1
            reason = None
        elif consumed_tokens > 0:
            duration = None if maybe_duration == 0 else maybe_duration
            rem = after_target.split()[consumed_tokens:]
            reason = " ".join(rem).strip() or None
        else:
            reason = after_target

    return target_id, duration, reason


def _parse_target_reason(
    m: types.Message,
    rest: str,
    force_reply_target: bool = False,
) -> tuple[int | None, str | None]:
    """
    Парсер цели и причины без поддержки длительности.
    Используется для команд, где временной формат недопустим.
    """
    if m.reply_to_message and m.reply_to_message.from_user:
        reason = (rest or "").strip() or None
        return m.reply_to_message.from_user.id, reason

    if force_reply_target:
        return None, None

    rest = (rest or "").strip()
    if not rest:
        return None, None

    parts = rest.split(maxsplit=1)
    target_id = parse_target_user(m, [parts[0]])
    if not isinstance(target_id, int) or target_id < 1:
        return None, None

    reason = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return target_id, reason


def _can_punish_target(chatid: int, actor: types.User, target_id: int) -> tuple[bool, str | None]:
    if target_id == actor.id:
        return False, "Нельзя применить наказание к самому себе."

    try:
        dummy = types.User(id=target_id, is_bot=False, first_name=".", last_name=None, username=None)
        if helpers._is_special_actor(chatid, dummy):
            return False, "Нельзя наказать пользователя с особым статусом."
    except Exception:
        pass

    if target_id in VERIFY_DEV:
        return False, "Нельзя наказать dev-пользователя."

    try:
        member = tg_get_chat_member(chatid, target_id)
        if member.status in ("administrator", "creator"):
            return False, "Нельзя наказать пользователя с префиксом."
    except Exception:
        pass

    if not can_act_on(chatid, actor.id, target_id):
        return False, "Недостаточно иерархии для этого действия."

    return True, None


def _bot_can_restrict(chat_id: int) -> bool:
    try:
        me = get_bot_me()
        member = bot.get_chat_member(chat_id, me.id)
        if member.status == "creator":
            return True
        if member.status == "administrator" and getattr(member, "can_restrict_members", False):
            return True
    except Exception:
        pass
    return False


def _mod_restrict_closed_permissions() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_messages=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def _mod_restrict_open_permissions() -> types.ChatPermissions:
    return types.ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_messages=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


def _mod_log_append(chat_id: int, kind: str, row: dict):
    ch = _mod_get_chat(chat_id)
    logs = ch["logs"].setdefault(kind, [])
    logs.append(row)
    if len(logs) > 300:
        del logs[:-300]
    _mod_save()


def _mod_find_log(chat_id: int, kind: str, action_id: str) -> dict | None:
    ch = _mod_get_chat(chat_id)
    for row in reversed(ch["logs"].get(kind, [])):
        if str(row.get("id")) == str(action_id):
            return row
    return None


def _mod_warn_count(chat_id: int, user_id: int) -> int:
    ch = _mod_get_chat(chat_id)
    arr = ch["warns"].get(str(user_id)) or []
    return sum(1 for w in arr if w.get("active", True))


def _mod_warn_add(chat_id: int, actor_id: int, target_id: int, reason: str | None) -> tuple[str, int, float]:
    ch = _mod_get_chat(chat_id)
    uid = str(target_id)
    arr = ch["warns"].get(uid)
    if arr is None:
        arr = []
        ch["warns"][uid] = arr

    action_id = _mod_new_action_id()
    entry = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor_id,
        "created_at": time.time(),
        "reason": (reason or "").strip(),
        "active": True,
    }
    arr.append(entry)

    count_after = sum(1 for w in arr if w.get("active", True))
    row = dict(entry)
    row["count_after"] = count_after
    _mod_log_append(chat_id, "warn", row)
    _mod_save()
    return action_id, count_after, float(entry["created_at"])


def _mod_warn_remove(chat_id: int, action_id: str, actor_id: int | None = None) -> tuple[bool, int | None]:
    ch = _mod_get_chat(chat_id)
    warns_by_user = ch.get("warns") or {}

    for uid, arr in warns_by_user.items():
        for w in arr:
            if str(w.get("id")) != str(action_id):
                continue
            if not w.get("active", True):
                return False, int(uid)
            w["active"] = False
            w["revoked_at"] = time.time()
            if actor_id:
                w["revoked_by"] = actor_id

            row = _mod_find_log(chat_id, "warn", action_id)
            if row is not None:
                row["active"] = False
                row["revoked_at"] = w["revoked_at"]
                if actor_id:
                    row["revoked_by"] = actor_id

            _mod_save()
            return True, int(uid)

    return False, None


def _mod_clear_all_warns_for_user(chat_id: int, user_id: int):
    ch = _mod_get_chat(chat_id)
    arr = ch.get("warns", {}).get(str(user_id)) or []
    now_ts = time.time()
    for w in arr:
        if w.get("active", True):
            w["active"] = False
            w["revoked_at"] = now_ts
    _mod_save()


def _mod_try_delete(chat_id: int, message_id: int | None):
    if not message_id:
        return
    try:
        bot.delete_message(chat_id, message_id)
    except Exception:
        pass


def _apply_mute(chat_id: int, target_id: int, duration: int | None) -> tuple[bool, str | None, int | None]:
    if not _bot_can_restrict(chat_id):
        return False, MOD_ERR["bot_no_mute"], None

    until_ts = int(time.time() + duration) if duration else None
    perms = _mod_restrict_closed_permissions()
    try:
        if until_ts:
            bot.restrict_chat_member(chat_id, target_id, permissions=perms, until_date=until_ts)
        else:
            bot.restrict_chat_member(chat_id, target_id, permissions=perms)
        tg_invalidate_chat_member_caches(chat_id, target_id)
        return True, None, until_ts
    except Exception as e:
        return False, f"Не удалось выдать ограничение: {e}", None


def _apply_ban(chat_id: int, target_id: int, duration: int | None) -> tuple[bool, str | None, int | None]:
    if not _bot_can_restrict(chat_id):
        return False, MOD_ERR["bot_no_ban"], None

    until_ts = int(time.time() + duration) if duration else None
    try:
        if until_ts:
            bot.ban_chat_member(chat_id, target_id, until_date=until_ts)
        else:
            bot.ban_chat_member(chat_id, target_id)
        _mark_farewell_suppressed(chat_id, target_id)
        tg_invalidate_chat_member_caches(chat_id, target_id)
        return True, None, until_ts
    except AttributeError:
        try:
            if until_ts:
                bot.kick_chat_member(chat_id, target_id, until_date=until_ts)
            else:
                bot.kick_chat_member(chat_id, target_id)
            tg_invalidate_chat_member_caches(chat_id, target_id)
            return True, None, until_ts
        except Exception as e:
            return False, f"Не удалось выдать блокировку: {e}", None
    except Exception as e:
        return False, f"Не удалось выдать блокировку: {e}", None


def _mod_unmute(chat_id: int, target_id: int) -> tuple[bool, str | None]:
    if not _bot_can_restrict(chat_id):
        return False, "У бота нет прав для снятия ограничения."
    try:
        bot.restrict_chat_member(chat_id, target_id, permissions=_mod_restrict_open_permissions())
        tg_invalidate_chat_member_caches(chat_id, target_id)
        return True, None
    except Exception as e:
        return False, f"Не удалось снять ограничение: {e}"


def _mod_unban(chat_id: int, target_id: int) -> tuple[bool, str | None]:
    if not _bot_can_restrict(chat_id):
        return False, "У бота нет прав для снятия блокировки."
    try:
        bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
        tg_invalidate_chat_member_caches(chat_id, target_id)
        return True, None
    except Exception as e:
        return False, f"Не удалось снять блокировку: {e}"


def _send_punish_message_with_button(
    chat_id: int,
    action_kind: str,
    action_id: str,
    target_id: int,
    actor_id: int,
    duration: int | None,
    reason: str | None,
    *,
    until_ts: int | None = None,
    created_at: float | None = None,
    warn_count: int | None = None,
    warn_limit: int | None = None,
):
    if action_kind == "mute":
        label = "Ограничение"
        btn_text = "Снять ограничение"
    elif action_kind == "ban":
        label = "Блокировка"
        btn_text = "Разблокировать"
    else:
        if warn_count is not None and warn_limit is not None:
            label = f"Предупреждение [{warn_count}/{warn_limit}]"
        else:
            label = "Предупреждение"
        btn_text = "Снять предупреждение"

    emoji_p = f'<tg-emoji emoji-id="{EMOJI_PUNISHMENT_ID}">⚠️</tg-emoji>'
    target_name = link_for_user(chat_id, target_id)
    actor_name = link_for_user(chat_id, actor_id)

    lines = [
        f"{emoji_p} <b>Пользователь</b> {target_name} <b>наказан.</b>",
        f"<b>Наказание:</b> {label}",
    ]

    if action_kind in ("mute", "ban"):
        if until_ts and until_ts > 0:
            lines.append(f"<b>Истекает:</b> {_fmt_time(until_ts)}")
        else:
            lines.append("<b>Истекает:</b> навсегда")

    if reason:
        lines.append(f"<b>Причина:</b> {_html.escape(reason.strip())}")

    lines.extend(["", f"<b>Администратор:</b> {actor_name}"])
    text = "\n".join(lines)

    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(
        btn_text,
        callback_data=f"punish_un:{chat_id}:{action_kind}:{target_id}:{action_id}",
        icon_custom_emoji_id=str(EMOJI_UNPUNISH_ID),
    ))

    try:
        sent_msg = bot.send_message(
            chat_id,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )

        # Сохраняем исходный HTML сообщения, чтобы при редактировании не терялись tg-time теги.
        row = _mod_find_log(chat_id, action_kind, action_id)
        if row is not None:
            row["message_html"] = text
            row["message_id"] = int(getattr(sent_msg, "message_id", 0) or 0)
            _mod_save()
    except Exception as e:
        print(f"[PUNISH] send_message failed: {e}")



def _auto_punish_for_warns(chat_id: int, actor: types.User, target_id: int,
                           source_tag: str | None = None):
    ch = _mod_get_chat(chat_id)
    warn_limit = int((ch.get("settings") or {}).get("warn_limit", 3))
    wp = ch["settings"].get("warn_punish") or {}
    ptype = (wp.get("type") or "mute").lower()
    reason_suffix = (wp.get("reason") or "").strip()
    auto_reason = f"За достижение максимального количества предупреждений ({warn_limit})."
    if reason_suffix:
        auto_reason += f" {reason_suffix}"

    if ptype == "kick":
        err = _kick_with_unban(chat_id, actor, target_id, auto_reason)
        if not err:
            _mod_clear_all_warns_for_user(chat_id, target_id)
            _log_mod_action(chat_id, "kick", actor.id, target_id,
                            reason=auto_reason, source_tag=source_tag)
        return

    duration = wp.get("duration")
    if duration is not None:
        try:
            duration = int(duration)
        except Exception:
            duration = None

    action_id = _mod_new_action_id()
    until_ts = None
    if ptype == "mute":
        ok, _, until_ts = _apply_mute(chat_id, target_id, duration)
        if not ok:
            return
    elif ptype == "ban":
        ok, _, until_ts = _apply_ban(chat_id, target_id, duration)
        if not ok:
            return
    else:
        return

    row = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor.id,
        "created_at": time.time(),
        "duration": int(duration or 0),
        "until": int(until_ts or 0),
        "reason": auto_reason,
        "active": True,
        "auto": True,
    }
    _mod_log_append(chat_id, ptype, row)

    ch = _mod_get_chat(chat_id)
    ch["active"][ptype][str(target_id)] = {
        "id": action_id,
        "actor_id": actor.id,
        "created_at": row["created_at"],
        "duration": row["duration"],
        "until": row["until"],
        "reason": row["reason"],
    }
    _mod_save()

    _send_punish_message_with_button(
        chat_id=chat_id,
        action_kind=ptype,
        action_id=action_id,
        target_id=target_id,
        actor_id=actor.id,
        duration=duration,
        reason=auto_reason,
        until_ts=int(until_ts or 0),
        created_at=row["created_at"],
    )
    _log_mod_action(chat_id, ptype, actor.id, target_id,
                    duration=int(duration or 0), reason=auto_reason,
                    until_ts=int(until_ts or 0) if until_ts else None,
                    source_tag=source_tag)
    _mod_clear_all_warns_for_user(chat_id, target_id)


def _make_msg_link(chat_id: int, msg_id: int) -> str:
    """Build a Telegram message link."""
    stripped = str(chat_id).lstrip("-")
    if stripped.startswith("100"):
        stripped = stripped[3:]
    return f"https://t.me/c/{stripped}/{msg_id}"


def _process_moderation_action(
    m: types.Message,
    action_kind: str,
    rest: str,
    allow_russian_duration: bool,
    delete_target_message: bool = False,
    force_reply_target: bool = False,
) -> str | None:
    chat_id = m.chat.id
    actor = m.from_user
    _mod_cleanup_expired(chat_id)

    perm_map = {"mute": PERM_MUTE, "ban": PERM_BAN, "warn": PERM_WARN, "kick": PERM_KICK}
    no_perm_map = {
        "mute": MOD_ERR["no_perm_mute"],
        "ban": MOD_ERR["no_perm_ban"],
        "warn": MOD_ERR["no_perm_warn"],
        "kick": "У вашей должности нет права использовать исключение.",
    }
    perm = perm_map[action_kind]

    status, allowed = check_role_permission(chat_id, actor.id, perm)
    if not allowed:
        if status == 'no_rank':
            return None
        if status == 'no_perm':
            return no_perm_map[action_kind]
        return MOD_ERR["no_rights"]

    if action_kind == "warn":
        warn_enabled = bool((_mod_get_chat(chat_id).get("settings") or {}).get("warn_enabled", True))
        if not warn_enabled:
            return "Система предупреждений выключена в /settings."

    if delete_target_message:
        st_del, ok_del = check_role_permission(chat_id, actor.id, PERM_DEL_MSG)
        if not ok_del:
            if st_del == 'no_rank':
                return None
            if st_del == 'no_perm':
                return MOD_ERR["no_perm_del"]
            return MOD_ERR["no_rights_delete"]
        from settings_ui import _bot_can_delete_messages
        if not _bot_can_delete_messages(chat_id):
            return MOD_ERR["bot_no_del"]

    if action_kind == "kick":
        target_id, reason = _parse_target_reason(
            m,
            rest,
            force_reply_target=force_reply_target,
        )
        duration = None
    else:
        target_id, duration, reason = _parse_target_duration_reason(
            m,
            rest,
            allow_russian_duration=allow_russian_duration,
            force_reply_target=force_reply_target,
        )
    if not target_id:
        return MOD_ERR["user_not_found"]

    ok_target, target_err = _can_punish_target(chat_id, actor, target_id)
    if not ok_target:
        return target_err or "Нельзя применить наказание к этому пользователю."

    if action_kind in ("mute", "ban") and duration is not None:
        if duration == -1:
            return MOD_ERR["bad_duration"]
        if duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS:
            return MOD_ERR["bad_duration"]

    if action_kind == "mute":
        ok, err, until_ts = _apply_mute(chat_id, target_id, duration)
        if not ok:
            return err or "Не удалось выдать ограничение."

        ch = _mod_get_chat(chat_id)
        prev = (ch.get("active", {}).get("mute", {}) or {}).get(str(target_id)) or {}
        prev_id = str(prev.get("id") or "")
        if prev_id:
            _mod_deactivate_log(chat_id, "mute", prev_id, revoked_by=actor.id)

        action_id = _mod_new_action_id()
        row = {
            "id": action_id,
            "target_id": target_id,
            "actor_id": actor.id,
            "created_at": time.time(),
            "duration": int(duration or 0),
            "until": int(until_ts or 0),
            "reason": (reason or "").strip(),
            "active": True,
        }
        _mod_log_append(chat_id, "mute", row)
        ch = _mod_get_chat(chat_id)
        ch["active"]["mute"][str(target_id)] = {
            "id": action_id,
            "actor_id": actor.id,
            "created_at": row["created_at"],
            "duration": row["duration"],
            "until": row["until"],
            "reason": row["reason"],
        }
        _mod_save()
        _send_punish_message_with_button(chat_id, "mute", action_id, target_id, actor.id, duration, reason,
                         until_ts=until_ts, created_at=row["created_at"])
        msg_link = _make_msg_link(chat_id, m.reply_to_message.message_id) if m and m.reply_to_message else None
        _log_mod_action(chat_id, "mute", actor.id, target_id, duration, reason or "",
                        until_ts=int(until_ts or 0) if until_ts else None, msg_link=msg_link,
                        forward_from_chat_id=chat_id if msg_link else None,
                        forward_message_id=m.reply_to_message.message_id if msg_link else None)

    elif action_kind == "ban":
        ok, err, until_ts = _apply_ban(chat_id, target_id, duration)
        if not ok:
            return err or "Не удалось выдать блокировку."

        ch = _mod_get_chat(chat_id)
        prev = (ch.get("active", {}).get("ban", {}) or {}).get(str(target_id)) or {}
        prev_id = str(prev.get("id") or "")
        if prev_id:
            _mod_deactivate_log(chat_id, "ban", prev_id, revoked_by=actor.id)

        action_id = _mod_new_action_id()
        row = {
            "id": action_id,
            "target_id": target_id,
            "actor_id": actor.id,
            "created_at": time.time(),
            "duration": int(duration or 0),
            "until": int(until_ts or 0),
            "reason": (reason or "").strip(),
            "active": True,
        }
        _mod_log_append(chat_id, "ban", row)
        ch = _mod_get_chat(chat_id)
        ch["active"]["ban"][str(target_id)] = {
            "id": action_id,
            "actor_id": actor.id,
            "created_at": row["created_at"],
            "duration": row["duration"],
            "until": row["until"],
            "reason": row["reason"],
        }
        _mod_save()
        _send_punish_message_with_button(chat_id, "ban", action_id, target_id, actor.id, duration, reason,
                         until_ts=until_ts, created_at=row["created_at"])
        msg_link = _make_msg_link(chat_id, m.reply_to_message.message_id) if m and m.reply_to_message else None
        _log_mod_action(chat_id, "ban", actor.id, target_id, duration, reason or "",
                        until_ts=int(until_ts or 0) if until_ts else None, msg_link=msg_link,
                        forward_from_chat_id=chat_id if msg_link else None,
                        forward_message_id=m.reply_to_message.message_id if msg_link else None)

    elif action_kind == "warn":
        action_id, count_after, warn_created_at = _mod_warn_add(chat_id, actor.id, target_id, reason)
        warn_limit = int((_mod_get_chat(chat_id).get("settings") or {}).get("warn_limit", 3))
        if count_after >= warn_limit:
            _auto_punish_for_warns(chat_id, actor, target_id)
        else:
            _send_punish_message_with_button(chat_id, "warn", action_id, target_id, actor.id, None, reason,
                     created_at=warn_created_at, warn_count=count_after, warn_limit=warn_limit)
        msg_link = _make_msg_link(chat_id, m.reply_to_message.message_id) if m and m.reply_to_message else None
        _log_mod_action(chat_id, "warn", actor.id, target_id, None, reason or "",
                        msg_link=msg_link,
                        forward_from_chat_id=chat_id if msg_link else None,
                        forward_message_id=m.reply_to_message.message_id if msg_link else None)

    else:
        err = _kick_with_unban(chat_id, actor, target_id, reason)
        if err:
            return err
        row = {
            "id": _mod_new_action_id(),
            "target_id": target_id,
            "actor_id": actor.id,
            "created_at": time.time(),
            "duration": 0,
            "until": 0,
            "reason": (reason or "").strip(),
            "active": True,
        }
        _mod_log_append(chat_id, "kick", row)
        msg_link = _make_msg_link(chat_id, m.reply_to_message.message_id) if m and m.reply_to_message else None
        _log_mod_action(chat_id, "kick", actor.id, target_id, None, reason or "",
                        msg_link=msg_link,
                        forward_from_chat_id=chat_id if msg_link else None,
                        forward_message_id=m.reply_to_message.message_id if msg_link else None)

    if delete_target_message and m.reply_to_message:
        _mod_try_delete(chat_id, m.reply_to_message.message_id)
    if delete_target_message:
        _mod_try_delete(chat_id, m.message_id)

    return None


@bot.message_handler(func=lambda m: _is_mod_trigger(m))
def cmd_moderation_main(m: types.Message):
    add_stat_message(m)

    prefix, cmd, rest = _extract_command_info(m)
    if not cmd:
        return
    if cmd in _REPLY_BLOCKED_ON_LINKED_CHANNEL_CMDS and is_reply_to_linked_channel_post(m):
        return

    if cmd in ("del", "дел") and prefix in ('/', '.'):
        add_stat_command('del')
        if not m.reply_to_message:
            return

        status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_DEL_MSG)
        if not allowed:
            if status == 'no_perm':
                return reply_error(m, "no_perm_del")
            return

        from settings_ui import _bot_can_delete_messages
        if not _bot_can_delete_messages(m.chat.id):
            return reply_error(m, "bot_no_del")

        target_user = m.reply_to_message.from_user
        _mod_try_delete(m.chat.id, m.reply_to_message.message_id)
        _mod_try_delete(m.chat.id, m.message_id)

        if rest:
            emoji_del = f'<tg-emoji emoji-id="{EMOJI_DELETED_REASON_ID}">🗑️</tg-emoji>'
            mention = mention_html_by_id(target_user.id, target_user.full_name or target_user.first_name or "Пользователь")
            txt = f"{emoji_del} {mention} <b>Ваше сообщение было удалено по причине:</b> {_html.escape(rest.strip())}."
            try:
                bot.send_message(m.chat.id, txt, parse_mode='HTML', disable_web_page_preview=True)
            except Exception:
                pass
        return

    list_aliases = {
        "warnlist": "warn",
        "mutelist": "mute",
        "banlist": "ban",
        "варнлист": "warn",
        "мутлист": "mute",
        "банлист": "ban",
    }
    if cmd in list_aliases:
        # По требованию: у list-команд не должно быть аргументов.
        if rest:
            return
        add_stat_command(cmd)

        if not is_owner(m.from_user):
            wait_seconds = cooldown_hit('chat', int(m.chat.id), f"list_{cmd}", LISTS_GROUP_COOLDOWN_SECONDS)
            if wait_seconds > 0:
                return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action=f"list_{cmd}")

        _mod_cleanup_expired(m.chat.id)
        st, ok = check_role_permission(m.chat.id, m.from_user.id, PERM_VIEW_LISTS)
        if not ok and not helpers._is_special_actor(m.chat.id, m.from_user):
            if st == 'no_perm':
                return bot.reply_to(
                    m,
                    premium_prefix("У вашей должности нет права смотреть списки."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            return

        kind = list_aliases[cmd]
        rows = _mod_collect_rows(m.chat.id, kind)

        if not rows:
            return bot.reply_to(m, premium_prefix("Список пуст."), parse_mode='HTML', disable_web_page_preview=True)

        return _send_mod_list_to_pm(m, kind, rows)

    if cmd in ("unmute", "размут", "анмут"):
        add_stat_command(cmd)
        _mod_cleanup_expired(m.chat.id)
        st, ok = check_role_permission(m.chat.id, m.from_user.id, PERM_UNMUTE)
        if not ok:
            if st == 'no_perm':
                return bot.reply_to(m, premium_prefix("У вашей должности нет права снимать ограничения."), parse_mode='HTML', disable_web_page_preview=True)
            return

        target_id = _mod_parse_target_for_un(m, rest)
        if not target_id:
            return bot.reply_to(m, premium_prefix(MOD_ERR["user_not_found"]), parse_mode='HTML', disable_web_page_preview=True)

        ok_target, target_err = _can_punish_target(m.chat.id, m.from_user, target_id)
        if not ok_target:
            return bot.reply_to(m, premium_prefix(target_err or "Нельзя снять ограничение этому пользователю."), parse_mode='HTML', disable_web_page_preview=True)

        ok2, err = _mod_unmute(m.chat.id, target_id)
        if not ok2:
            return bot.reply_to(m, premium_prefix(err or "Не удалось снять ограничение."), parse_mode='HTML', disable_web_page_preview=True)

        ch = _mod_get_chat(m.chat.id)
        prev = (ch.get("active", {}).get("mute", {}) or {}).pop(str(target_id), None)
        if prev and prev.get("id"):
            _mod_deactivate_log(m.chat.id, "mute", str(prev.get("id")), revoked_by=m.from_user.id)
            _mod_save()

        _log_mod_action(m.chat.id, "unmute", m.from_user.id, target_id)
        return bot.reply_to(m, _mod_unpunish_message("Ограничение снято."), parse_mode='HTML', disable_web_page_preview=True)

    if cmd in ("unban", "разбан"):
        add_stat_command(cmd)
        _mod_cleanup_expired(m.chat.id)
        st, ok = check_role_permission(m.chat.id, m.from_user.id, PERM_UNBAN)
        if not ok:
            if st == 'no_perm':
                return bot.reply_to(m, premium_prefix("У вашей должности нет права снимать блокировки."), parse_mode='HTML', disable_web_page_preview=True)
            return

        target_id = _mod_parse_target_for_un(m, rest)
        if not target_id:
            return bot.reply_to(m, premium_prefix(MOD_ERR["user_not_found"]), parse_mode='HTML', disable_web_page_preview=True)

        ok_target, target_err = _can_punish_target(m.chat.id, m.from_user, target_id)
        if not ok_target:
            return bot.reply_to(m, premium_prefix(target_err or "Нельзя снять блокировку этому пользователю."), parse_mode='HTML', disable_web_page_preview=True)

        ok2, err = _mod_unban(m.chat.id, target_id)
        if not ok2:
            return bot.reply_to(m, premium_prefix(err or "Не удалось снять блокировку."), parse_mode='HTML', disable_web_page_preview=True)

        ch = _mod_get_chat(m.chat.id)
        prev = (ch.get("active", {}).get("ban", {}) or {}).pop(str(target_id), None)
        if prev and prev.get("id"):
            _mod_deactivate_log(m.chat.id, "ban", str(prev.get("id")), revoked_by=m.from_user.id)
            _mod_save()

        _log_mod_action(m.chat.id, "unban", m.from_user.id, target_id)
        return bot.reply_to(m, _mod_unpunish_message("Пользователь разблокирован."), parse_mode='HTML', disable_web_page_preview=True)

    if cmd in ("unwarn", "снятьварн", "анварн"):
        add_stat_command(cmd)
        st, ok = check_role_permission(m.chat.id, m.from_user.id, PERM_UNWARN)
        if not ok:
            if st == 'no_perm':
                return bot.reply_to(m, premium_prefix("У вашей должности нет права снимать предупреждения."), parse_mode='HTML', disable_web_page_preview=True)
            return

        target_id = _mod_parse_target_for_un(m, rest)
        if not target_id:
            return bot.reply_to(m, premium_prefix(MOD_ERR["user_not_found"]), parse_mode='HTML', disable_web_page_preview=True)

        ch = _mod_get_chat(m.chat.id)
        warns = (ch.get("warns") or {}).get(str(target_id)) or []
        active_warns = [w for w in warns if w.get("active", True)]
        if not active_warns:
            return bot.reply_to(m, premium_prefix("У пользователя нет активных предупреждений."), parse_mode='HTML', disable_web_page_preview=True)

        latest = sorted(active_warns, key=lambda x: float(x.get("created_at") or 0), reverse=True)[0]
        ok2, _ = _mod_warn_remove(m.chat.id, str(latest.get("id")), actor_id=m.from_user.id)
        if not ok2:
            return bot.reply_to(m, premium_prefix("Не удалось снять предупреждение."), parse_mode='HTML', disable_web_page_preview=True)

        ch_after = _mod_get_chat(m.chat.id)
        warns_after = (ch_after.get("warns") or {}).get(str(target_id)) or []
        count_after = len([w for w in warns_after if w.get("active", True)])
        warn_limit = int((ch_after.get("settings") or {}).get("warn_limit", 3))
        _log_mod_action(m.chat.id, "unwarn", m.from_user.id, target_id)
        return bot.reply_to(m, _mod_unpunish_message(f"Предупреждение снято [{count_after}/{warn_limit}]."), parse_mode='HTML', disable_web_page_preview=True)

    action_map = {
        "mute": "mute",
        "мут": "mute",
        "ban": "ban",
        "бан": "ban",
        "warn": "warn",
        "варн": "warn",
        "kick": "kick",
        "кик": "kick",
        "delmute": "mute",
        "delban": "ban",
        "delwarn": "warn",
        "делмут": "mute",
        "делбан": "ban",
        "делварн": "warn",
    }
    if cmd not in action_map:
        return

    add_stat_command(cmd)
    action_kind = action_map[cmd]

    allow_ru = True

    need_reply = cmd in ("delmute", "delban", "delwarn", "делмут", "делбан", "делварн")
    if need_reply and not m.reply_to_message:
        return

    err = _process_moderation_action(
        m,
        action_kind=action_kind,
        rest=rest,
        allow_russian_duration=allow_ru,
        delete_target_message=need_reply,
        force_reply_target=need_reply,
    )
    if err:
        return bot.reply_to(m, premium_prefix(err), parse_mode='HTML', disable_web_page_preview=True)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("punish_un:"))
def cb_punish_un(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    try:
        _, chat_s, kind, target_s, action_id = c.data.split(":", 4)
        chat_id = int(chat_s)
        target_id = int(target_s)
    except Exception:
        return bot.answer_callback_query(c.id)

    user = c.from_user
    perm_map = {
        "mute": (PERM_UNMUTE, "У вашей должности нет права снимать ограничения."),
        "ban": (PERM_UNBAN, "У вашей должности нет права снимать блокировки."),
        "warn": (PERM_UNWARN, "У вашей должности нет права снимать предупреждения."),
    }
    if kind not in perm_map:
        return bot.answer_callback_query(c.id)

    perm_name, no_perm_text = perm_map[kind]
    st, ok = check_role_permission(chat_id, user.id, perm_name)
    if not ok:
        if st == 'no_perm':
            return bot.answer_callback_query(c.id, no_perm_text, show_alert=True)
        return bot.answer_callback_query(c.id)

    row_snapshot = _mod_find_log(chat_id, kind, action_id)

    _mod_cleanup_expired(chat_id)

    if kind == "mute":
        ok2, err = _mod_unmute(chat_id, target_id)
        if not ok2:
            return bot.answer_callback_query(c.id, err or "Не удалось снять ограничение.", show_alert=True)
        ch = _mod_get_chat(chat_id)
        ch["active"]["mute"].pop(str(target_id), None)
        row = _mod_find_log(chat_id, "mute", action_id)
        if row is not None:
            row["active"] = False
            row["revoked_at"] = time.time()
            row["revoked_by"] = user.id
        result_line = "<b>Ограничение снято.</b>"
    elif kind == "ban":
        ok2, err = _mod_unban(chat_id, target_id)
        if not ok2:
            return bot.answer_callback_query(c.id, err or "Не удалось снять блокировку.", show_alert=True)
        ch = _mod_get_chat(chat_id)
        ch["active"]["ban"].pop(str(target_id), None)
        row = _mod_find_log(chat_id, "ban", action_id)
        if row is not None:
            row["active"] = False
            row["revoked_at"] = time.time()
            row["revoked_by"] = user.id
        result_line = "<b>Пользователь разблокирован.</b>"
    else:
        ok2, _ = _mod_warn_remove(chat_id, action_id, actor_id=user.id)
        if not ok2:
            return bot.answer_callback_query(c.id, "Предупреждение уже снято.", show_alert=True)
        ch_after = _mod_get_chat(chat_id)
        warns_after = (ch_after.get("warns") or {}).get(str(target_id)) or []
        count_after = len([w for w in warns_after if w.get("active", True)])
        warn_limit_val = int((ch_after.get("settings") or {}).get("warn_limit", 3))
        result_line = f"<b>Предупреждение снято [{count_after}/{warn_limit_val}].</b>"

    _mod_save()

    old_html = ""
    if isinstance(row_snapshot, dict):
        old_html = str(row_snapshot.get("message_html") or "").strip()
    if not old_html:
        old_html = c.message.html_text or c.message.text or ""
    append_block = _mod_unpunish_message(result_line)

    try:
        bot.edit_message_text(
            f"{old_html}\n\n{append_block}",
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=None,
        )
    except Exception:
        try:
            bot.edit_message_reply_markup(
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

    return bot.answer_callback_query(c.id, "Готово.")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("modlist:"))
def cb_modlist(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    parts = c.data.split(":")
    if len(parts) not in (4, 5):
        return bot.answer_callback_query(c.id)

    if len(parts) == 5:
        _, kind_or_close, source_chat_s, page_s, viewer_s = parts
    else:
        # Легаси-формат: modlist:{kind}:{page}:{viewer}
        _, kind_or_close, page_s, viewer_s = parts
        source_chat_s = str(c.message.chat.id)

    try:
        viewer_id = int(viewer_s)
        source_chat_id = int(source_chat_s)
    except Exception:
        return bot.answer_callback_query(c.id)

    if c.from_user.id != viewer_id:
        return bot.answer_callback_query(c.id, "Эти кнопки доступны только вызвавшему список.", show_alert=True)

    if kind_or_close == "close":
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        return bot.answer_callback_query(c.id)

    if not helpers._is_special_actor(source_chat_id, c.from_user):
        st, ok = check_role_permission(source_chat_id, c.from_user.id, PERM_VIEW_LISTS)
        if not ok:
            if st == 'no_perm':
                return bot.answer_callback_query(c.id, "У вашей должности нет права смотреть списки.", show_alert=True)
            return bot.answer_callback_query(c.id)

    kind = kind_or_close
    if kind not in ("warn", "mute", "ban"):
        return bot.answer_callback_query(c.id)

    try:
        page = int(page_s)
    except Exception:
        page = 0

    _mod_cleanup_expired(source_chat_id)
    rows = _mod_collect_rows(source_chat_id, kind)
    if not rows:
        try:
            bot.edit_message_text(
                premium_prefix("Список пуст."),
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=None,
            )
        except Exception:
            pass
        return bot.answer_callback_query(c.id)

    total_pages = max(1, (len(rows) + MOD_LIST_PAGE_SIZE - 1) // MOD_LIST_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    text = _mod_list_page_text(source_chat_id, kind, rows, page)
    kb = _mod_list_keyboard(source_chat_id, kind, page, total_pages, viewer_id)

    try:
        bot.edit_message_text(
            text,
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        pass

    return bot.answer_callback_query(c.id)


ADMIN_STATS_PAGE_SIZE = 10


def _can_view_adminstats_for_chat(chat_id: int, user: types.User) -> bool:
    if helpers._is_special_actor(chat_id, user):
        return True
    _, allowed = check_role_permission(chat_id, user.id, PERM_VIEW_LISTS)
    return bool(allowed)


def _find_adminstats_groups_for_user(user: types.User) -> list[tuple[int, str]]:
    chat_ids: set[int] = set()
    for cid_str in (USERS or {}):
        try:
            chat_ids.add(int(cid_str))
        except ValueError:
            pass
    for cid_str in (MODERATION or {}):
        try:
            chat_ids.add(int(cid_str))
        except ValueError:
            pass
    for cid_str in (CHAT_ROLES or {}):
        try:
            chat_ids.add(int(cid_str))
        except ValueError:
            pass

    result: list[tuple[int, str]] = []
    for chat_id in chat_ids:
        if not is_group_approved(chat_id):
            continue
        if not _can_view_adminstats_for_chat(chat_id, user):
            continue
        try:
            chat = bot.get_chat(chat_id)
            title = chat.title or str(chat_id)
        except Exception:
            title = str(chat_id)
        result.append((chat_id, title))

    result.sort(key=lambda x: x[1].lower())
    return result


def _adminstats_is_current_admin(chat_id: int, user_id: int) -> bool:
    if get_user_rank(chat_id, user_id) > 0:
        return True

    try:
        member = bot.get_chat_member(chat_id, user_id)
        if member.status in ("administrator", "creator"):
            return True
    except Exception:
        pass

    if user_id in VERIFY_DEV:
        return True

    try:
        u = tg_get_chat(user_id)
        if is_owner(u):
            return True
    except Exception:
        pass

    return False


def _adminstats_role_label(chat_id: int, user_id: int) -> str:
    rank = get_user_rank(chat_id, user_id)
    role = get_rank_label_plain(rank)
    if role:
        return role

    try:
        member = bot.get_chat_member(chat_id, user_id)
        if member.status == "creator":
            return "Владелец чата"
        if member.status == "administrator":
            return "Администратор Telegram"
    except Exception:
        pass

    if user_id in VERIFY_DEV:
        return "Dev-пользователь"

    try:
        u = tg_get_chat(user_id)
        if is_owner(u):
            return "Разработчик бота"
    except Exception:
        pass

    return "Без должности"


def _adminstats_collect(chat_id: int) -> tuple[list[dict], list[dict]]:
    ch = _mod_get_chat(chat_id)
    logs = ch.get("logs") or {}
    by_actor: dict[int, dict] = {}

    for kind in ("mute", "ban", "warn", "kick"):
        for row in (logs.get(kind) or []):
            if not isinstance(row, dict):
                continue

            try:
                actor_id = int(row.get("actor_id") or 0)
            except Exception:
                actor_id = 0
            if actor_id <= 0:
                continue

            rec = by_actor.get(actor_id)
            if rec is None:
                rec = {
                    "user_id": actor_id,
                    "mute_count": 0,
                    "ban_count": 0,
                    "kick_count": 0,
                    "warn_count": 0,
                    "total": 0,
                    "last_ts": 0,
                }
                by_actor[actor_id] = rec

            if kind == "mute":
                rec["mute_count"] += 1
            elif kind == "ban":
                rec["ban_count"] += 1
            elif kind == "kick":
                rec["kick_count"] += 1
            elif kind == "warn":
                rec["warn_count"] += 1

            rec["total"] += 1

            try:
                ts = int(float(row.get("created_at") or 0))
            except Exception:
                ts = 0
            if ts > rec["last_ts"]:
                rec["last_ts"] = ts

    all_stats = list(by_actor.values())
    all_stats.sort(key=lambda x: (-int(x.get("total") or 0), -int(x.get("last_ts") or 0), int(x.get("user_id") or 0)))

    current: list[dict] = []
    past: list[dict] = []
    for rec in all_stats:
        uid = int(rec.get("user_id") or 0)
        rec["role"] = _adminstats_role_label(chat_id, uid)
        if _adminstats_is_current_admin(chat_id, uid):
            current.append(rec)
        else:
            past.append(rec)

    return current, past


def _adminstats_format_entry(chat_id: int, row: dict) -> str:
    uid = int(row.get("user_id") or 0)
    role_plain = str(row.get("role") or "Без должности")
    role_lower = role_plain.lower()
    last_ts = int(row.get("last_ts") or 0)
    last_ts_text = _fmt_time(last_ts) if last_ts > 0 else "—"
    name = link_for_user(chat_id, uid)

    rank = get_user_rank(chat_id, uid)
    if "разработчик" in role_lower or "dev" in role_lower:
        role_html = f'<tg-emoji emoji-id="{EMOJI_DEV_ID}">👨‍💻</tg-emoji> Разработчик бота'
    elif rank in (1, 2, 3, 4, 5):
        role_html = get_rank_label_html(rank)
    elif "владелец" in role_lower:
        role_html = f'<tg-emoji emoji-id="{EMOJI_ROLE_OWNER_ID}">👑</tg-emoji> Владелец чата'
    elif "администратор" in role_lower:
        role_html = f'<tg-emoji emoji-id="{EMOJI_ADMIN_ID}">⭐</tg-emoji> Администратор Telegram'
    else:
        role_html = f'<tg-emoji emoji-id="{EMOJI_MEMBER_ID}">👤</tg-emoji> {_html.escape(role_plain)}'

    lines = [
        f"<b>Пользователь:</b> {name} [<code>{uid}</code>] [{role_html}]",
        f"• <b>Ограничений:</b> <code>{int(row.get('mute_count') or 0)}</code>",
        f"• <b>Блокировок:</b> <code>{int(row.get('ban_count') or 0)}</code>",
        f"• <b>Предупреждений:</b> <code>{int(row.get('warn_count') or 0)}</code>",
        f"• <b>Исключений:</b> <code>{int(row.get('kick_count') or 0)}</code>",
        f"• <b>Всего наказаний:</b> <code>{int(row.get('total') or 0)}</code>",
        f"• <b>Последнее выданное наказание:</b> {last_ts_text}",
    ]
    return "\n".join(lines)


def _adminstats_render_section(chat_id: int, title: str, rows: list[dict], page: int) -> tuple[str, int, int]:
    total_pages = max(1, (len(rows) + ADMIN_STATS_PAGE_SIZE - 1) // ADMIN_STATS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * ADMIN_STATS_PAGE_SIZE
    end = start + ADMIN_STATS_PAGE_SIZE

    lines = [f"<b>{title} ({page + 1}/{total_pages})</b>"]
    sliced = rows[start:end]
    if not sliced:
        lines.append("<i>Нет данных.</i>")
    else:
        for row in sliced:
            lines.append("")
            lines.append(_adminstats_format_entry(chat_id, row))

    return "\n".join(lines), page, total_pages


def _adminstats_text(chat_id: int, current_rows: list[dict], past_rows: list[dict], view: str, page: int) -> tuple[str, int, int]:
    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    block, page, total = _adminstats_render_section(chat_id, "Администраторы", current_rows, page)

    emoji_stats = f'<tg-emoji emoji-id="{PREMIUM_STATS_EMOJI_ID}">📊</tg-emoji>'
    text = (
        f"{emoji_stats} <b>Статистика администраторов</b>\n"
        f"<b>Чат:</b> {_html.escape(title)} [<code>{chat_id}</code>]\n\n"
        f"{block}"
    )
    if len(text) > 3900:
        text = text[:3897] + "..."

    return text, page, total


def _adminstats_keyboard(chat_id: int, view: str, page: int, total: int, viewer_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(
            "Предыдущая страница",
            callback_data=f"astnav:{chat_id}:{viewer_id}:{view}:{page}:prev",
            icon_custom_emoji_id=str(EMOJI_PAGINATION_PREV_ID),
        ))
    if page < total - 1:
        nav_row.append(InlineKeyboardButton(
            "Следующая страница",
            callback_data=f"astnav:{chat_id}:{viewer_id}:{view}:{page}:next",
            icon_custom_emoji_id=str(EMOJI_PAGINATION_NEXT_ID),
        ))
    if nav_row:
        kb.row(*nav_row)

    return kb


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and match_command_aliases(m, ['adminstats', 'adminstat', 'админстата']))
def cmd_adminstats(m: types.Message):
    add_stat_message(m)
    _, cmd, _ = _extract_command_info(m)
    add_stat_command(cmd or 'adminstats')

    if not check_group_approval(m):
        return

    if not is_owner(m.from_user):
        wait_seconds = cooldown_hit('chat', int(m.chat.id), 'adminstats', LISTS_GROUP_COOLDOWN_SECONDS)
        if wait_seconds > 0:
            return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='adminstats')

    if not _can_view_adminstats_for_chat(m.chat.id, m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("У вашей должности нет права смотреть статистику администраторов."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    current_rows, past_rows = _adminstats_collect(m.chat.id)
    text, page, total = _adminstats_text(m.chat.id, current_rows, past_rows, "current", 0)
    kb = _adminstats_keyboard(m.chat.id, "current", page, total, m.from_user.id)

    try:
        bot.send_message(
            m.from_user.id,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось отправить статистику в ЛС. Напишите боту в ЛС и повторите команду."),
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=_build_open_pm_markup(),
        )

    return _reply_sent_to_pm(m, "Статистика администраторов отправлена в ЛС.")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("astchat:"))
def cb_adminstats_chat(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    try:
        _, chat_s, viewer_s = c.data.split(":", 2)
        chat_id = int(chat_s)
        viewer_id = int(viewer_s)
    except Exception:
        return bot.answer_callback_query(c.id)

    if c.from_user.id != viewer_id:
        return bot.answer_callback_query(c.id, "Эти кнопки доступны только вызвавшему команду.", show_alert=True)
    if c.message.chat.type != 'private':
        return bot.answer_callback_query(c.id)
    if not _can_view_adminstats_for_chat(chat_id, c.from_user):
        return bot.answer_callback_query(c.id, "Нет прав для просмотра статистики этого чата.", show_alert=True)

    current_rows, past_rows = _adminstats_collect(chat_id)
    text, page, total = _adminstats_text(chat_id, current_rows, past_rows, "current", 0)
    kb = _adminstats_keyboard(chat_id, "current", page, total, viewer_id)

    try:
        bot.edit_message_text(
            text,
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        pass

    return bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("astnav:"))
def cb_adminstats_nav(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    try:
        _, chat_s, viewer_s, view, page_s, action = c.data.split(":", 5)
        chat_id = int(chat_s)
        viewer_id = int(viewer_s)
        page = int(page_s)
    except Exception:
        return bot.answer_callback_query(c.id)

    if c.from_user.id != viewer_id:
        return bot.answer_callback_query(c.id, "Эти кнопки доступны только вызвавшему команду.", show_alert=True)
    if c.message.chat.type != 'private':
        return bot.answer_callback_query(c.id)
    if not _can_view_adminstats_for_chat(chat_id, c.from_user):
        return bot.answer_callback_query(c.id, "Нет прав для просмотра статистики этого чата.", show_alert=True)

    current_rows, past_rows = _adminstats_collect(chat_id)
    if view not in ("current", "past"):
        view = "current"

    rows = current_rows
    view = "current"
    total = max(1, (len(rows) + ADMIN_STATS_PAGE_SIZE - 1) // ADMIN_STATS_PAGE_SIZE)
    page = max(0, min(page, total - 1))

    if action == "prev":
        page = max(0, page - 1)
    elif action == "next":
        page = min(total - 1, page + 1)

    text, page, total = _adminstats_text(chat_id, current_rows, past_rows, view, page)
    kb = _adminstats_keyboard(chat_id, view, page, total, viewer_id)

    try:
        bot.edit_message_text(
            text,
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        pass

    return bot.answer_callback_query(c.id)
