"""
helpers.py — Raw API helpers, бизнес-логика (роли, права, пользователи, профили,
одобрение групп, verify/dbg команды, утилиты), обработчики: dbg_users,
role-settings callbacks, verify команды, new_chat_members.

Импортирует из config и persistence. Не имеет циклических зависимостей.
"""
from __future__ import annotations

import time
import threading
import asyncio
import io

from config import (
    os, json, re, _re, _html, random, psutil,
    datetime, Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    # bot instances
    bot, bot_raw, tg_client,
    # constants
    TOKEN, OWNER_USERNAME, DATA_DIR, API_BASE_URL,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    # file paths (нужны для save-функций через persistence)
    CLOSE_CHAT_FILE, CHAT_ROLES_FILE, ROLE_PERMS_FILE,
    PROFILES_FILE, USERS_FILE, GLOBAL_USERS_FILE,
    GROUP_STATS_FILE, GROUP_SETTINGS_FILE,
    CHAT_SETTINGS_FILE, MODERATION_FILE, PENDING_GROUPS_FILE,
    SQLITE_JSON_FALLBACK_WRITE,
    # emoji IDs
    PREMIUM_PREFIX_EMOJI_ID, EMOJI_RATE_LIMIT_ID,
    EMOJI_CHAT_OPEN_BTN_ID, EMOJI_ROLE_SETTINGS_CHAT_ID,
    EMOJI_ROLE_SETTINGS_CANCEL_ID, EMOJI_ROLE_SETTINGS_SAVE_ID,
    EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID, EMOJI_ROLE_SETTINGS_CHOOSE_RANK_ID,
    EMOJI_ROLE_SETTINGS_OPEN_AGAIN_ID, EMOJI_ROLE_SETTINGS_SENT_PM_ID,
    EMOJI_ROLE_OWNER_ID, EMOJI_ROLE_CHIEF_ADMIN_ID, EMOJI_ROLE_ADMIN_ID,
    EMOJI_ROLE_MOD_ID, EMOJI_ROLE_TRAINEE_ID, EMOJI_ROLE_ACTION_ID,
    EMOJI_DEV_ID, EMOJI_MEMBER_ID, EMOJI_ADMIN_ID, EMOJI_OWNER_ID,
    EMOJI_VERIFY_ADMIN_ID, EMOJI_VERIFY_DEV_ID, EMOJI_NEW_MSG_OWNER_ID,
    EMOJI_SENT_OK_ID, EMOJI_LOG_ID, EMOJI_LIST_ID,
    EMOJI_BOT_VERSION_ID, EMOJI_PING_ID, EMOJI_LEFT_ID,
    EMOJI_LEGEND_ANYWHERE_ID, EMOJI_LEGEND_DEV_ONLY_ID,
    EMOJI_LEGEND_DEV_OR_VERIFIED_ID, EMOJI_LEGEND_GROUP_ADMIN_ID,
    EMOJI_LEGEND_PM_ONLY_ID, EMOJI_LEGEND_GROUP_ONLY_ID,
    EMOJI_LEGEND_ALL_USERS_ID,
    # emoji IDs — дополнительные (нужны в модулях через from helpers import *)
    EMOJI_PROFILE_ID, EMOJI_MSG_COUNT_ID, EMOJI_DESC_ID, EMOJI_AWARDS_BLOCK_ID,
    EMOJI_PREMIUM_STATUS_ID, EMOJI_ROLE_ALL_ID, EMOJI_ROLE_DEV_ID,
    EMOJI_USER_ROLE_TEXT_ID,
    EMOJI_SCOPE_GROUP_ID, EMOJI_SCOPE_PM_ID, EMOJI_SCOPE_ALL_ID,
    EMOJI_ADMIN_RIGHTS_ID, EMOJI_BTN_UNADMIN_ID, EMOJI_BTN_KICK_ID,
    EMOJI_REASON_ID, EMOJI_LOG_PM_ID, EMOJI_CHAT_CLOSED_ID,
    EMOJI_PIN_NOTIFY_ID, EMOJI_PIN_SILENT_ID, EMOJI_PIN_REPIN_ID,
    EMOJI_DELETED_REASON_ID,
    EMOJI_WELCOME_TEXT_ID, EMOJI_WELCOME_MEDIA_ID, EMOJI_WELCOME_BUTTONS_ID,
    EMOJI_PUNISHMENT_ID, EMOJI_UNPUNISH_ID,
    EMOJI_PAGINATION_NEXT_ID, EMOJI_PAGINATION_PREV_ID,
    EMOJI_CONTACT_DEV_ID, EMOJI_SEND_TEXT_PROMPT_ID,
    EMOJI_REPLY_BTN_ID, EMOJI_IGNORE_BTN_ID, EMOJI_REPLY_RECEIVED_ID,
    # PREMIUM emoji IDs
    PREMIUM_STATS_EMOJI_ID, PREMIUM_USER_EMOJI_ID, PREMIUM_CLOSE_EMOJI_ID,
    # Другие константы
    AWARD_EMOJI_IDS,
    _HTTP_SESSION,
)
from persistence import (
    # state dicts (изменяются in-place — разделяем ссылки на те же объекты)
    VERIFY_ADMINS, VERIFY_DEV,
    DEV_CONTACT_INBOX, DEV_CONTACT_META,
    CLOSE_CHAT_STATE, GROUP_STATS, GROUP_SETTINGS,
    CHAT_SETTINGS, MODERATION, PENDING_GROUPS,
    USERS, GLOBAL_USERS, PROFILES,
    CHAT_ROLES, ROLE_PERMS,
    # volatile state
    PENDING_DEV_CONTACT_FROM_USER, PENDING_DEV_REPLY_FROM_OWNER,
    BROADCAST_DRAFTS, BROADCAST_PENDING_INPUT,
    _OPERATION_QUEUE, _OPERATION_QUEUE_LOCK, _OPERATION_QUEUE_ACTIVE,
    STATS,
    # save functions
    save_verify_admins, save_verify_dev,
    save_dev_contact_inbox, save_dev_contact_meta,
    save_close_chat_state,
    save_group_stats, save_group_settings,
    save_chat_settings, save_moderation, save_pending_groups,
    save_users, save_global_users, save_profiles,
    save_chat_roles, save_role_perms,
    # persistence helpers
    tg_get_chat, tg_get_chat_member,
    tg_invalidate_member_cache, tg_invalidate_chat_cache,
    tg_invalidate_chat_member_caches,
    load_json_file, save_json_file, throttled_save_json_file,
    get_sqlite_status, migrate_legacy_json_to_sqlite,
    _is_duplicate_callback_query,
    get_tg_cache_stats,
    GLOBAL_LAST_SEEN_UPDATE_SECONDS,
    tg_get_user_by_id_cached,
    # message-event stats
    buffer_msg_event,
    # log channel
    send_log_event,
)
from config import get_user_id_by_username_mtproto, _tg_client_status, call_mtproto_sync, get_bot_me

# ── Обратный индекс username → user_id (O(1) lookup по @username) ────────────
# Структура: {chat_id: {lowercase_username: str(user_id)}}
# Строится при старте из USERS, обновляется в update_user_in_chat.
_USERNAME_INDEX: dict[int, dict[str, str]] = {}
_USERNAME_INDEX_LOCK = threading.Lock()


def _rebuild_username_index() -> None:
    """Построить обратный индекс username→user_id из текущего словаря USERS."""
    new_index: dict[int, dict[str, str]] = {}
    for cid_s, chat_users in (USERS or {}).items():
        try:
            cid = int(cid_s)
        except (ValueError, TypeError):
            continue
        idx: dict[str, str] = {}
        for uid_s, data in (chat_users or {}).items():
            uname = (data.get("username") or "").lower()
            if uname:
                idx[uname] = uid_s
        if idx:
            new_index[cid] = idx
    with _USERNAME_INDEX_LOCK:
        _USERNAME_INDEX.clear()
        _USERNAME_INDEX.update(new_index)


# ==== RAW-ХЕЛПЕРЫ ====

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from telebot.apihelper import ApiTelegramException


def raw_request(method: str, payload: dict):
    url = f"{API_BASE_URL}/{method}"
    try:
        r = _HTTP_SESSION.post(url, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"[RAW] Ошибка запроса {method}: {e}")
        return None


def raw_set_chat_member_tag(chat_id: int, user_id: int, tag: str | None) -> tuple[bool, str | None]:
    clean_tag = (tag or "").strip()
    is_admin = False
    try:
        member = tg_get_chat_member(chat_id, user_id)
        is_admin = getattr(member, "status", "") in ("administrator", "creator")
    except Exception:
        is_admin = False

    try:
        if is_admin:
            set_admin_title = getattr(bot, "set_chat_administrator_custom_title", None)
            if callable(set_admin_title):
                set_admin_title(chat_id, user_id, clean_tag)
            else:
                apihelper._make_request(
                    TOKEN,
                    "setChatAdministratorCustomTitle",
                    method="post",
                    params={"chat_id": chat_id, "user_id": user_id, "custom_title": clean_tag},
                )
        else:
            payload = {"chat_id": chat_id, "user_id": user_id, "tag": clean_tag}
            set_tag_method = getattr(bot, "set_chat_member_tag", None)
            if callable(set_tag_method):
                set_tag_method(chat_id, user_id, clean_tag)
            else:
                apihelper._make_request(TOKEN, "setChatMemberTag", method="post", params=payload)
        tg_invalidate_chat_member_caches(chat_id, user_id)
        return True, None
    except ApiTelegramException as e:
        return False, str(e.description or str(e) or "Неизвестная ошибка Telegram API")
    except Exception as e:
        return False, str(e)


def raw_get_chat_member(chat_id: int, user_id: int) -> dict | None:
    try:
        result = apihelper._make_request(
            TOKEN,
            "getChatMember",
            method="post",
            params={"chat_id": chat_id, "user_id": user_id},
        )
        if isinstance(result, dict):
            return result
    except Exception:
        return None
    return None


def _extract_member_tag(member_obj: Any) -> str:
    if isinstance(member_obj, dict):
        for key in ("tag", "member_tag", "custom_tag", "custom_title"):
            value = member_obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    for attr in ("tag", "member_tag", "custom_tag", "custom_title"):
        value = getattr(member_obj, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _kb_to_dict(keyboard):
    """
    Универсальная конвертация:
    - если dict или None — возвращаем как есть (для старого кода);
    - если InlineKeyboardMarkup — собираем {inline_keyboard: [[...]]},
      прокидывая style / icon_custom_emoji_id, если они заданы на кнопках.
    """
    if keyboard is None or isinstance(keyboard, dict):
        return keyboard

    if isinstance(keyboard, InlineKeyboardMarkup):
        rows = []
        for row in keyboard.keyboard:
            row_buttons = []
            for btn in row:
                btn_dict = {"text": btn.text}
                if btn.callback_data is not None:
                    btn_dict["callback_data"] = btn.callback_data
                if btn.url is not None:
                    btn_dict["url"] = btn.url

                style = getattr(btn, "style", None)
                if style is not None:
                    btn_dict["style"] = style

                icon_id = getattr(btn, "icon_custom_emoji_id", None)
                if icon_id is not None:
                    btn_dict["icon_custom_emoji_id"] = icon_id

                row_buttons.append(btn_dict)
            rows.append(row_buttons)
        return {"inline_keyboard": rows}

    return keyboard


def raw_send_with_inline_keyboard(
    chat_id: int, text: str, keyboard, reply_to_message_id: int | None = None
):
    kb_dict = _kb_to_dict(keyboard)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if kb_dict is not None:
        payload["reply_markup"] = json.dumps(kb_dict, ensure_ascii=False)
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    return raw_request("sendMessage", payload)


def raw_edit_inline_keyboard(chat_id: int, message_id: int, keyboard):
    kb_dict = _kb_to_dict(keyboard)
    payload = {"chat_id": chat_id, "message_id": message_id}
    if kb_dict is not None:
        payload["reply_markup"] = json.dumps(kb_dict, ensure_ascii=False)
    return raw_request("editMessageReplyMarkup", payload)


def raw_edit_message_with_keyboard(chat_id: int, message_id: int, text: str, keyboard):
    kb_dict = _kb_to_dict(keyboard)
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if kb_dict is not None:
        payload["reply_markup"] = json.dumps(kb_dict, ensure_ascii=False)
    return raw_request("editMessageText", payload)


def raw_delete_message(chat_id: int, message_id: int):
    payload = {"chat_id": chat_id, "message_id": message_id}
    return raw_request("deleteMessage", payload)


# ==== СОСТОЯНИЕ ЗАКРЫТИЯ ЧАТОВ (ПРОСТО ФЛАГ) ====



def setclosechatstate(chatid: int, closed: bool, until_ts: float | int):
    cid = str(chatid)
    if not closed:
        CLOSE_CHAT_STATE.pop(cid, None)
    else:
        CLOSE_CHAT_STATE[cid] = {
            "closed": True,
            "until": float(until_ts) if until_ts else 0.0,
        }
    save_close_chat_state()


def getclosechatstate(chatid: int) -> dict:
    return CLOSE_CHAT_STATE.get(str(chatid)) or {}


def getcurrentdefaultpermissions(chatid: int) -> types.ChatPermissions | None:
    """
    Текущие default-permissions чата.
    """
    try:
        chat = bot.get_chat(chatid)
        perms = getattr(chat, "permissions", None)
        if isinstance(perms, types.ChatPermissions):
            return perms
    except Exception:
        pass
    return None


def build_closed_permissions() -> types.ChatPermissions:
    """
    Чат закрыт: всем нельзя ничего отправлять.
    Админские права (изменение профиля, закрепление) не трогаем, TeleBot их тут не задаёт.
    """
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


def build_open_permissions() -> types.ChatPermissions:
    """
    Чат открыт: всем можно отправлять всё.
    Админские права не трогаем (они отдельно задаются в ролях/админах).
    """
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


def setchatdefaultpermissions(chatid: int, perms: types.ChatPermissions) -> bool:
    try:
        bot.set_chat_permissions(chatid, perms)
        return True
    except ApiTelegramException:
        return False
    except Exception:
        return False


# ==== АВТО-ОТКРЫТИЕ ЧАТА ПО ТАЙМЕРУ ==== 

def schedulereopenchat(chatid: int, delayseconds: int):
    """
    Планирует авто-открытие чата через delayseconds.
    Просто включает все права отправки.
    """
    if delayseconds <= 0:
        return

    def worker():
        time.sleep(delayseconds)
        try:
            state = getclosechatstate(chatid)
            if not state or not state.get("closed"):
                return
            until_ts = state.get("until") or 0
            if until_ts and time.time() < until_ts:
                return

            new_perms = build_open_permissions()
            ok = setchatdefaultpermissions(chatid, new_perms)
            if not ok:
                return

            setclosechatstate(chatid, closed=False, until_ts=0)

            emoji_open = f'<tg-emoji emoji-id="{EMOJI_CHAT_OPEN_BTN_ID}">🔓</tg-emoji>'
            text = f"{emoji_open} <b>Чат снова открыт.</b>"

            try:
                bot.send_message(chatid, text, parse_mode='HTML', disable_web_page_preview=True)
            except Exception:
                pass

            # Лог открытия (автоматическое)
            try:
                me = get_bot_me()
                try:
                    chat = bot.get_chat(chatid)
                    chat_title = _html.escape(chat.title or str(chatid))
                    chat_link = f'<a href="https://t.me/c/{str(chat.id).lstrip("-100")}/0">{chat_title}</a> [<code>{chatid}</code>]'
                except Exception:
                    chat_link = f"<code>{chatid}</code>"
                actor_name = _html.escape(me.first_name or me.username or "Бот")
                actor_link = f'<a href="tg://user?id={me.id}">{actor_name}</a> [<code>{me.id}</code>]'
                send_log_event(
                    chatid, "chat_opened",
                    "\n".join([
                        "<b>#ОТКРЫТИЕ_ЧАТА</b>",
                        f"<b>Группа:</b> {chat_link}",
                        f"<b>Администратор:</b> {actor_link}",
                        "<b>Источник:</b> автоматически по таймеру",
                        f"#id{me.id} #id{abs(chatid)}",
                    ])
                )
            except Exception:
                pass
        except Exception:
            pass

    t = threading.Thread(target=worker, daemon=True)
    t.start()

# ==== ГРУППОВАЯ СТАТИСТИКА ====



def get_group_settings(chat_id: int):
    cid = str(chat_id)
    st = GROUP_SETTINGS.get(cid)
    if st is None:
        st = {"auto_stats": True}
        GROUP_SETTINGS[cid] = st
        save_group_settings()
    return st

def update_group_stats(message: types.Message):
    chat = message.chat
    if chat.type not in ['group', 'supergroup']:
        return
    if not is_group_approved(chat.id):
        return
    user = message.from_user
    if not user:
        return

    chat_id = str(chat.id)
    user_id = str(user.id)

    chat_stats = GROUP_STATS.get(chat_id)
    if chat_stats is None:
        chat_stats = {}
        GROUP_STATS[chat_id] = chat_stats

    user_stats = chat_stats.get(user_id)
    if user_stats is None:
        user_stats = {"count": 0, "last_msg_id": None}
        chat_stats[user_id] = user_stats

    user_stats["count"] += 1
    user_stats["last_msg_id"] = message.message_id

    save_group_stats()

    # Buffer event for time-period statistics (batch-flushed every few seconds)
    buffer_msg_event(chat.id, user.id, int(message.date), message.message_id)


# ==== УПРАВЛЕНИЕ НЕПОДТВЕРЖДЕННЫМИ ГРУППАМИ ====

def is_group_approved(chat_id: int) -> bool:
    cid = str(chat_id)
    return cid not in PENDING_GROUPS


def check_group_approval(m: types.Message) -> bool:
    """Возвращает True если группа подтверждена, False иначе."""
    if m.chat.type not in ['group', 'supergroup']:
        return True  # Приватные чаты всегда OK
    
    if not is_group_approved(m.chat.id):
        emoji_wait = f'<tg-emoji emoji-id="{EMOJI_RATE_LIMIT_ID}">⏳</tg-emoji>'
        bot.reply_to(
            m,
            f"{emoji_wait} Бот находится на модерации. Ожидание подтверждения от разработчика.",
            parse_mode='HTML'
        )
        return False
    return True


def add_pending_group(chat_id: int, chat_title: str, adder_user: types.User) -> None:
    """Добавляет группу в список неподтвержденных."""
    cid = str(chat_id)
    adder_id = adder_user.id if adder_user else 0
    adder_username = (adder_user.username or "unknown") if adder_user else "unknown"
    
    PENDING_GROUPS[cid] = {
        "title": chat_title or "Unknown Group",
        "adder_id": adder_id,
        "adder_username": adder_username,
        "added_at": int(time.time()),
        "message_id": None,
    }
    save_pending_groups()


def approve_pending_group(chat_id: int) -> None:
    """Одобрит группу владельцем (удаляет из список pending)."""
    cid = str(chat_id)
    PENDING_GROUPS.pop(cid, None)
    save_pending_groups()
    get_group_settings(chat_id)  # инициализируем настройки группы
    save_group_settings()


def deny_pending_group(chat_id: int) -> None:
    """Отказывает группе в доступе (удаляет из список pending)."""
    cid = str(chat_id)
    PENDING_GROUPS.pop(cid, None)
    save_pending_groups()


def notify_dev_about_new_group(chat_id: int, chat_title: str, adder_user: types.User) -> None:
    """Отправляет сообщение разработчику при добавлении бота в новую группу."""
    owner_id = _resolve_owner_user_id()
    if not owner_id:
        return
    
    adder_name = f"@{adder_user.username}" if adder_user and adder_user.username else \
                 (f"{adder_user.first_name} {adder_user.last_name}".strip() if adder_user else "Unknown")
    
    emoji_new = f'<tg-emoji emoji-id="{EMOJI_NEW_MSG_OWNER_ID}">📨</tg-emoji>'
    
    text = (
        f"<b>{emoji_new} Новая группа для одобрения</b>\n\n"
        f"<b>Группа:</b> {_html.escape(chat_title or 'Без названия')}\n"
        f"<b>ID:</b> <code>{chat_id}</code>\n"
        f"<b>Добавил:</b> {adder_name} (ID: {adder_user.id if adder_user else 'Unknown'})\n"
        f"<b>Время:</b> {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
        f"<i>Выберите действие ниже.</i>"
    )
    
    keyboard = types.InlineKeyboardMarkup()
    btn_approve = types.InlineKeyboardButton("Разрешить", callback_data=f"approve_group:{chat_id}")
    btn_approve.icon_custom_emoji_id = str(EMOJI_SENT_OK_ID)
    
    btn_deny = types.InlineKeyboardButton("Запретить", callback_data=f"deny_group:{chat_id}")
    btn_deny.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
    
    keyboard.add(btn_approve, btn_deny)
    
    try:
        bot.send_message(
            owner_id,
            text,
            parse_mode='HTML',
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение разработчику: {e}")

# ==== ПАРСИНГ ВРЕМЕНИ ДЛЯ /closechat ==== 

MAX_CLOSECHAT_SECONDS = 24 * 60 * 60  # 1 день, лимит только для closechat


def parse_closechat_duration(value: str, is_russian: bool) -> int | None:
    """
    is_russian = False -> суффиксы: m, h, d, w, mou, y
    is_russian = True  -> суффиксы: м/мин, ч, д, н, мес, г

    Возвращает секунды или None, если формат неверный или > MAX_CLOSECHAT_SECONDS.
    Если указано "0" или пусто — None (будет трактоваться как 'навсегда' в хендлере).
    """
    if not value:
        return None

    value = value.strip().lower()

    # английские: m, h, d, w, mou, y
    if not is_russian:
        num_part = ''
        unit_part = ''
        for ch in value:
            if ch.isdigit():
                if unit_part:
                    # цифра после юнита -> фигня
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

        unit = unit_part
        if unit == 'm':      # minute
            seconds = amount * 60
        elif unit == 'h':    # hour
            seconds = amount * 60 * 60
        elif unit == 'd':    # day
            seconds = amount * 24 * 60 * 60
        elif unit == 'w':    # week
            seconds = amount * 7 * 24 * 60 * 60
        elif unit == 'mou':  # month (условно 30 дней)
            seconds = amount * 30 * 24 * 60 * 60
        elif unit == 'y':    # year (условно 365 дней)
            seconds = amount * 365 * 24 * 60 * 60
        else:
            return None

    # русские: м/мин, ч, д, н, мес, г
    else:
        # возможные варианты "10м", "10 мин", "10мес" и т.п. не допускаем,
        # нам нужен формат точно "10м", "10ч", "10д", "10н", "10мес", "10г"
        num_part = ''
        unit_part = ''
        for ch in value:
            if ch.isdigit():
                if unit_part:
                    # цифра после юнита -> не принимаем
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

        unit = unit_part

        if unit in ('м', 'мин'):
            seconds = amount * 60
        elif unit == 'ч':
            seconds = amount * 60 * 60
        elif unit == 'д':
            seconds = amount * 24 * 60 * 60
        elif unit == 'н':
            seconds = amount * 7 * 24 * 60 * 60
        elif unit == 'мес':
            seconds = amount * 30 * 24 * 60 * 60
        elif unit == 'г':
            seconds = amount * 365 * 24 * 60 * 60
        else:
            return None

    # лимит только для closechat
    if seconds > MAX_CLOSECHAT_SECONDS:
        return None

    return seconds


# ==== БД ПОЛЬЗОВАТЕЛЕЙ ПО ЧАТАМ ====


@bot.message_handler(commands=['dbg_users'])
def cmd_dbg_users(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    chat_id_s = str(m.chat.id)
    chat_users = USERS.get(chat_id_s) or {}
    lines = [f"USERS для чата {m.chat.id}:"]
    if not chat_users:
        lines.append("пусто")
    else:
        for uid, data in chat_users.items():
            lines.append(f"{uid}: @{data.get('username')} {data.get('first_name')}")
    bot.reply_to(m, "\n".join(lines)[:4000])

def update_user_in_chat(chat: types.Chat, user: types.User):
    if chat.type not in ['group', 'supergroup']:
        return
    if not user:
        return

    cid = str(chat.id)
    uid = str(user.id)

    chat_users = USERS.get(cid)
    if chat_users is None:
        chat_users = {}
        USERS[cid] = chat_users

    new_data = {
        "id": user.id,
        "username": (user.username or "").lower(),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "full_name": user.full_name or user.first_name or ""
    }
    if chat_users.get(uid) == new_data:
        return

    # Обновляем обратный индекс username перед записью новых данных
    old_uname = ((chat_users.get(uid) or {}).get("username") or "").lower()
    new_uname = new_data["username"]  # уже в нижнем регистре
    with _USERNAME_INDEX_LOCK:
        cid_int = chat.id
        if cid_int not in _USERNAME_INDEX:
            _USERNAME_INDEX[cid_int] = {}
        idx = _USERNAME_INDEX[cid_int]
        if old_uname and idx.get(old_uname) == uid:
            del idx[old_uname]
        if new_uname:
            idx[new_uname] = uid

    chat_users[uid] = new_data
    save_users()

def find_user_id_by_username_in_chat(chat_id: int, username: str) -> int | None:
    uname = username.lstrip('@').lower()
    if not uname:
        return None
    with _USERNAME_INDEX_LOCK:
        uid_s = _USERNAME_INDEX.get(chat_id, {}).get(uname)
    if uid_s is not None:
        try:
            return int(uid_s)
        except (ValueError, TypeError):
            pass
    return None


# ==== ГЛОБАЛЬНАЯ БД ПОЛЬЗОВАТЕЛЕЙ ====


def update_global_user_from_telebot(user: types.User):
    """Обновить/создать запись в глобальной БД по объекту TeleBot User."""
    if not user:
        return
    uid = str(user.id)
    now_ts = time.time()
    prev = GLOBAL_USERS.get(uid) or {}

    next_rec = {
        "id": user.id,
        "username": (user.username or "").lower(),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "full_name": user.full_name or user.first_name or "",
        "last_seen": prev.get("last_seen") or 0,
    }

    has_profile_changes = any([
        prev.get("username") != next_rec["username"],
        prev.get("first_name") != next_rec["first_name"],
        prev.get("last_name") != next_rec["last_name"],
        prev.get("full_name") != next_rec["full_name"],
    ])

    prev_seen = float(prev.get("last_seen") or 0)
    should_touch_last_seen = (now_ts - prev_seen) >= GLOBAL_LAST_SEEN_UPDATE_SECONDS
    if should_touch_last_seen:
        next_rec["last_seen"] = now_ts
    else:
        next_rec["last_seen"] = prev_seen

    if not has_profile_changes and not should_touch_last_seen:
        return

    GLOBAL_USERS[uid] = next_rec
    save_global_users()


def update_global_user_basic(user_id: int, username: str | None = None):
    """Обновление глобалки, когда есть только ID и username (например, из MTProto)."""
    uid = str(user_id)
    rec = GLOBAL_USERS.get(uid) or {}
    prev_username = rec.get("username") or ""
    prev_seen = float(rec.get("last_seen") or 0)
    rec["id"] = user_id
    if username:
        rec["username"] = username.lower()
    rec.setdefault("first_name", "")
    rec.setdefault("last_name", "")
    rec.setdefault("full_name", "")

    now_ts = time.time()
    if (now_ts - prev_seen) >= GLOBAL_LAST_SEEN_UPDATE_SECONDS:
        rec["last_seen"] = now_ts
    else:
        rec["last_seen"] = prev_seen

    username_changed = bool(username and prev_username != rec.get("username"))
    if not username_changed and (now_ts - prev_seen) < GLOBAL_LAST_SEEN_UPDATE_SECONDS:
        return

    GLOBAL_USERS[uid] = rec
    save_global_users()


# ==== ПРОФИЛИ ПО ЧАТАМ ====


def get_profile(chat_id: int, user_id: int) -> dict:
    cid = str(chat_id)
    uid = str(user_id)
    chat_profiles = PROFILES.get(cid)
    if chat_profiles is None:
        chat_profiles = {}
        PROFILES[cid] = chat_profiles
    pr = chat_profiles.get(uid)
    if pr is None:
        pr = {"description": "", "awards": []}
        chat_profiles[uid] = pr
        save_profiles()
    return pr

# { chat_id: { user_id: {"rank": int, "role_text": str} } }


def get_user_rank(chat_id: int, user_id: int) -> int:
    """
    5 – владелец чата
    4 – главный админ
    3 – админ
    2 – модератор
    1 – стажёр
    0 – обычный участник
    Владелец бота — 999 (бог-режим, вне иерархии).
    """
    # Проверяем владельца бота без обращения к API:
    # если его числовой ID уже закеширован в DEV_CONTACT_META — сравниваем напрямую;
    # иначе запрашиваем username через per-task кеш (bot.get_chat вызывается максимум
    # один раз за весь обработчик благодаря tg_get_user_by_id_cached).
    owner_id = int(DEV_CONTACT_META.get("owner_user_id") or 0)
    if owner_id > 0:
        if user_id == owner_id:
            return 999
    else:
        try:
            u = tg_get_user_by_id_cached(user_id)
        except Exception:
            u = types.User(user_id, False, first_name="", last_name=None, username=None)
        if is_owner(u):
            return 999

    # дальше — обычная логика чата
    try:
        member = tg_get_chat_member(chat_id, user_id)
        if member.status == 'creator':
            return 5
    except Exception:
        pass

    cid = str(chat_id)
    uid = str(user_id)
    chat_roles = CHAT_ROLES.get(cid, {})
    rec = chat_roles.get(uid, {})
    return int(rec.get("rank", 0))


def set_user_rank(chat_id: int, user_id: int, rank: int):
    fake_user = types.User(user_id, False, first_name="", last_name=None, username=None)
    if is_owner(fake_user):
        return

    cid = str(chat_id)
    uid = str(user_id)
    chat_roles = CHAT_ROLES.get(cid)
    if chat_roles is None:
        chat_roles = {}
        CHAT_ROLES[cid] = chat_roles

    if rank <= 0:
        if uid in chat_roles:
            del chat_roles[uid]
    else:
        rec = chat_roles.get(uid) or {}
        rec["rank"] = rank
        chat_roles[uid] = rec

    save_chat_roles()
    tg_invalidate_chat_member_caches(chat_id, user_id)


def get_user_role_text(chat_id: int, user_id: int) -> str:
    return ""


def get_user_custom_tag(chat_id: int, user_id: int) -> str:
    try:
        member = tg_get_chat_member(chat_id, user_id)
        tag = _extract_member_tag(member)
        if tag:
            return tag
    except Exception:
        pass

    lib_member = raw_get_chat_member(chat_id, user_id)
    if lib_member:
        return _extract_member_tag(lib_member)
    return ""


def set_user_role_text(chat_id: int, user_id: int, text: str | None):
    return


def set_user_custom_tag(chat_id: int, user_id: int, tag: str | None):
    return


def _parse_role_and_tag(raw_text: str) -> tuple[str, str | None]:
    text = (raw_text or "").strip()
    if not text:
        return "", None

    if "|" not in text:
        return text, None

    left, right = text.split("|", 1)
    role_text = left.strip()
    tag_text = right.strip()
    return role_text, (tag_text or None)

def get_rank_label_html(rank: int) -> str:
    """
    Возвращает HTML-строку с эмодзи и названием должности по рангу.
    999 – разработчик бота (владелец бота).
    5 – владелец чата.
    4 – главный админ.
    3 – админ.
    2 – модератор.
    1 – стажёр.
    0 – без должности.
    """
    # 999 и выше — разработчик бота (владелец бота)
    if rank >= 999:
        return f'<tg-emoji emoji-id="{EMOJI_DEV_ID}">👨‍💻</tg-emoji> Разработчик бота'

    # 5 — владелец чата
    if rank >= 5:
        return f'<tg-emoji emoji-id="{EMOJI_ROLE_OWNER_ID}">👑</tg-emoji> Владелец чата'
    elif rank == 4:
        return f'<tg-emoji emoji-id="{EMOJI_ROLE_CHIEF_ADMIN_ID}">⭐</tg-emoji> Главный админ'
    elif rank == 3:
        return f'<tg-emoji emoji-id="{EMOJI_ROLE_ADMIN_ID}">🛡️</tg-emoji> Админ'
    elif rank == 2:
        return f'<tg-emoji emoji-id="{EMOJI_ROLE_MOD_ID}">🔧</tg-emoji> Модератор'
    elif rank == 1:
        return f'<tg-emoji emoji-id="{EMOJI_ROLE_TRAINEE_ID}">🎓</tg-emoji> Стажёр'
    else:
        return ""


def get_rank_label_plain(rank: int) -> str:
    """
    Возвращает название должности БЕЗ эмодзи по рангу.
    999 – разработчик бота (владелец бота).
    5 – владелец чата.
    4 – главный админ.
    3 – админ.
    2 – модератор.
    1 – стажёр.
    0 – без должности.
    """
    if rank >= 999:
        return "Разработчик бота"
    if rank >= 5:
        return "Владелец чата"
    elif rank == 4:
        return "Главный админ"
    elif rank == 3:
        return "Админ"
    elif rank == 2:
        return "Модератор"
    elif rank == 1:
        return "Стажёр"
    else:
        return ""


def get_rank_label_instrumental(rank: int) -> str:
    if rank >= 999:
        return "Разработчиком бота"
    if rank >= 5:
        return "Владельцем чата"
    elif rank == 4:
        return "Главным админом"
    elif rank == 3:
        return "Админом"
    elif rank == 2:
        return "Модератором"
    elif rank == 1:
        return "Стажёром"
    return ""


# ==== ПРАВА ДОЛЖНОСТЕЙ (ПО ЧАТАМ) ==== 



PERM_MUTE = "mute"
PERM_UNMUTE = "unmute"
PERM_BAN = "ban"
PERM_UNBAN = "unban"
PERM_WARN = "warn"
PERM_UNWARN = "unwarn"
PERM_KICK = "kick"
PERM_DEL_MSG = "del_msg"           # /del
PERM_VIEW_LISTS = "view_lists"     # warnlist/banlist/mutelist/vlist/adminstats/taglist
PERM_CLOSE_CHAT = "close_chat"
PERM_OPEN_CHAT = "open_chat"

# новые права закрепа
PERM_PIN = "pin"                   # закрепить сообщение
PERM_UNPIN = "unpin"               # открепить сообщение

# verify: единое право
PERM_MANAGE_VERIFY = "manage_verify"

# роли/описания
PERM_SET_ROLE_TEXT = "set_role_text"      # /addrole /removerole
PERM_SET_DESC_OTHERS = "set_desc_others"  # менять описание другим
PERM_PROMOTE = "promote"
PERM_DEMOTE = "demote"

# награды: единое право
PERM_MANAGE_AWARDS = "manage_awards"

# настройки чата (/settings)
PERM_SETTINGS = "settings"


ROLE_PERMS_KEYS = [
    (PERM_MUTE, "Ограничить"),
    (PERM_UNMUTE, "Снять ограничение"),
    (PERM_BAN, "Заблокировать"),
    (PERM_UNBAN, "Разблокировать"),
    (PERM_WARN, "Предупреждение"),
    (PERM_UNWARN, "Снять предупреждение"),
    (PERM_KICK, "Исключить"),
    (PERM_DEL_MSG, "Удаление сообщений"),
    (PERM_VIEW_LISTS, "Списки"),
    (PERM_PIN, "Закрепить"),
    (PERM_UNPIN, "Открепить"),
    (PERM_CLOSE_CHAT, "Закрыть чат"),
    (PERM_OPEN_CHAT, "Открыть чат"),

    (PERM_MANAGE_VERIFY, "Управление верификацией"),

    (PERM_SET_ROLE_TEXT, "Управление тегами"),
    (PERM_SET_DESC_OTHERS, "Управление описаниями других"),
    (PERM_PROMOTE, "Повысить"),
    (PERM_DEMOTE, "Понизить"),

    (PERM_MANAGE_AWARDS, "Управление наградами"),
    (PERM_SETTINGS, "Изменение настроек"),
]


def get_role_perms(chat_id: int, rank: int) -> dict:
    """
    Вернуть словарь прав для данной должности (ранга) в чате.
    Если записи нет — создать.
    Для ранга 5 при первом обращении включаем все права.
    """
    cid = str(chat_id)
    perms_by_rank = ROLE_PERMS.get(cid) or {}
    key = str(rank)
    perms = perms_by_rank.get(key)

    if perms is None:
        if rank == 5:
            perms = {k: True for k, _ in ROLE_PERMS_KEYS}
        else:
            perms = {}
        perms_by_rank[key] = perms
        ROLE_PERMS[cid] = perms_by_rank
        save_role_perms()
    else:
        # Новые ключи в ROLE_PERMS_KEYS: для совместимости со старыми JSON — у ранга 5 включаем, у остальных выключаем.
        changed = False
        for k, _ in ROLE_PERMS_KEYS:
            if k not in perms:
                perms[k] = rank == 5
                changed = True
        if changed:
            save_role_perms()

    return perms


def has_role_perm(chat_id: int, user_id: int, perm_name: str) -> bool:
    """
    Проверить, есть ли у пользователя право perm_name через его должность.
    Владелец бота и настоящий владелец чата всегда могут всё.
    """
    u = tg_get_user_by_id_cached(user_id)

    if is_owner(u):
        return True

    try:
        member = tg_get_chat_member(chat_id, user_id)
        if member.status == 'creator':
            return True
    except Exception:
        pass

    rank = get_user_rank(chat_id, user_id)
    if rank <= 0:
        return False

    perms = get_role_perms(chat_id, rank)
    return bool(perms.get(perm_name))


def check_role_permission(chat_id: int, user_id: int, perm_name: str):
    """
    Универсальная проверка:
    - Разработчик бота, dev и creator чата всегда имеют право.
    - Если у пользователя нет должности (rank 0) и он не спец-актер — ('no_rank', False).
    - Если должность есть (rank 1-5), но у этой должности нет perm_name — ('no_perm', False).
    - Если всё ок — ('ok', True).
    """
    user = tg_get_user_by_id_cached(user_id)

    if _is_special_actor(chat_id, user):
        return 'ok', True

    rank = get_user_rank(chat_id, user_id)
    if rank <= 0:
        return 'no_rank', False

    perms = get_role_perms(chat_id, rank)
    if not perms.get(perm_name):
        return 'no_perm', False

    return 'ok', True


def can_act_on(chat_id: int, actor_id: int, target_id: int) -> bool:
    """
    Нельзя трогать разработчика и фактического владельца.
    Владелец бота может трогать всех остальных.
    Остальные — только если actor_rank > target_rank.
    """
    target_user = tg_get_user_by_id_cached(target_id)

    if is_owner(target_user):
        return False

    actor_user = tg_get_user_by_id_cached(actor_id)

    if is_owner(actor_user):
        return True

    try:
        member_t = tg_get_chat_member(chat_id, target_id)
        if member_t.status == 'creator':
            return False
    except Exception:
        pass

    try:
        member_a = tg_get_chat_member(chat_id, actor_id)
        if member_a.status == 'creator':
            return True
    except Exception:
        pass

    actor_rank = get_user_rank(chat_id, actor_id)
    target_rank = get_user_rank(chat_id, target_id)
    return actor_rank > target_rank


def _is_dev_user(user: types.User) -> bool:
    return is_dev(user)


def _user_has_any_rank(chat_id: int, user_id: int) -> bool:
    return get_user_rank(chat_id, user_id) > 0


def _user_can_edit_now(user: types.User, chat_id: int) -> bool:
    """
    Для callback'ов rs_*: разработчик бота, dev-юзер или фактический владелец чата.
    """
    if is_owner(user):
        return True
    if _is_dev_user(user):
        return True
    try:
        member = tg_get_chat_member(chat_id, user.id)
        if member.status == 'creator':
            return True
    except Exception:
        pass
    return False


def _is_special_actor(chat_id: int, user: types.User) -> bool:
    """
    Универсальная проверка для команд:
    разработчик бота, dev-юзер, фактический владелец чата.
    """
    if not user:
        return False
    if is_owner(user) or _is_dev_user(user):
        return True
    try:
        member = tg_get_chat_member(chat_id, user.id)
        return member.status == 'creator'
    except Exception:
        return False


def _user_can_open_settings(chat_id: int, user: types.User) -> tuple[bool, str | None]:
    """
    Проверка доступа к /settings.

    Логика:
      - разработчик бота, dev‑юзер, фактический creator чата — всегда могут;
      - ранг 0 — молчит;
      - ранг 1–5 без PERM_SETTINGS — текст ошибки;
      - ранг 1–5 с PERM_SETTINGS — можно.
    """
    if _is_special_actor(chat_id, user):
        return True, None

    status, allowed = check_role_permission(chat_id, user.id, PERM_SETTINGS)
    if allowed:
        return True, None

    if status == 'no_rank':
        return False, None

    if status == 'no_perm':
        return False, "У вашей должности нет права открывать настройки чата."

    return False, "Вы не можете открывать настройки чата."


# ==== Интерфейс настроек прав должностей ==== 


def _build_chats_keyboard_for_owner(user: types.User) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    rows = []
    for chat_id_s, users in (USERS or {}).items():
        try:
            chat_id = int(chat_id_s)
        except ValueError:
            continue

        try:
            member = tg_get_chat_member(chat_id, user.id)
        except Exception:
            continue

        if member.status not in ('administrator', 'creator'):
            continue

        try:
            chat = tg_get_chat(chat_id)
            title = chat.title or str(chat_id)
        except Exception:
            title = str(chat_id)

        btn = InlineKeyboardButton(title[:32], callback_data=f"rs_chat:{chat_id}")
        rows.append([btn])

    if not rows:
        rows.append([InlineKeyboardButton("Нет доступных чатов", callback_data="rs_none")])

    for row in rows:
        kb.row(*row)

    # Отмена только здесь (ЛС), одна кнопка
    kb.row(InlineKeyboardButton(
        "Отмена",
        callback_data="rs_cancel",
        icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
    ))
    return kb


def _build_ranks_keyboard(chat_id: int, for_pm: bool, back_callback: str | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton(
            "Владелец чата",
            callback_data=f"rs_rank:{chat_id}:5",
            icon_custom_emoji_id=EMOJI_ROLE_OWNER_ID
        ),
        InlineKeyboardButton(
            "Главный админ",
            callback_data=f"rs_rank:{chat_id}:4",
            icon_custom_emoji_id=EMOJI_ROLE_CHIEF_ADMIN_ID
        ),
        InlineKeyboardButton(
            "Админ",
            callback_data=f"rs_rank:{chat_id}:3",
            icon_custom_emoji_id=EMOJI_ROLE_ADMIN_ID
        ),
        InlineKeyboardButton(
            "Модератор",
            callback_data=f"rs_rank:{chat_id}:2",
            icon_custom_emoji_id=EMOJI_ROLE_MOD_ID
        ),
        InlineKeyboardButton(
            "Стажёр",
            callback_data=f"rs_rank:{chat_id}:1",
            icon_custom_emoji_id=EMOJI_ROLE_TRAINEE_ID
        ),
    ]
    kb.add(*buttons)

    if for_pm:
        kb.row(InlineKeyboardButton(
            "Назад",
            callback_data=(back_callback or "rs_back_chats"),
            icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        ))
    return kb


def _build_perms_keyboard_colored(chat_id: int, rank: int, in_pm: bool) -> dict:
    """
    Цветная клавиатура прав через raw JSON.
    Рядовка:
    Ограничить - Снять ограничение
    Заблокировать - Разблокировать
    Предупреждение - Снять предупреждение
    Исключение - Удаление сообщений
    Списки
    Закрепить - Открепить
    Закрыть чат - Открыть чат
    Управление верификацией
    Повысить - Понизить
    Управление наградами
    Управление тегами
    Управление описаниями других
    Изменение настроек
    Назад - Сохранить
    """
    perms = get_role_perms(chat_id, rank)
    labels = {k: lbl for k, lbl in ROLE_PERMS_KEYS}

    ordered_keys = [
        (PERM_MUTE, PERM_UNMUTE),
        (PERM_BAN, PERM_UNBAN),
        (PERM_WARN, PERM_UNWARN),
        (PERM_KICK, PERM_DEL_MSG),
        (PERM_VIEW_LISTS, None),
        (PERM_PIN, PERM_UNPIN),
        (PERM_CLOSE_CHAT, PERM_OPEN_CHAT),
        (PERM_MANAGE_VERIFY, None),
        (PERM_PROMOTE, PERM_DEMOTE),
        (PERM_MANAGE_AWARDS, None),
        (PERM_SET_ROLE_TEXT, None),
        (PERM_SET_DESC_OTHERS, None),
        (PERM_SETTINGS, None),
    ]

    rows = []

    for left_key, right_key in ordered_keys:
        row = []

        if left_key is not None:
            enabled = perms.get(left_key, False)
            style = "success" if enabled else "danger"
            row.append({
                "text": labels.get(left_key, left_key),
                "callback_data": f"rs_perm:{chat_id}:{rank}:{left_key}",
                "style": style,
            })

        if right_key is not None:
            enabled = perms.get(right_key, False)
            style = "success" if enabled else "danger"
            row.append({
                "text": labels.get(right_key, right_key),
                "callback_data": f"rs_perm:{chat_id}:{rank}:{right_key}",
                "style": style,
            })

        if row:
            rows.append(row)

    bottom = []
    if in_pm:
        bottom.append({
            "text": "Назад",
            "callback_data": f"rs_back:{chat_id}",
            "icon_custom_emoji_id": str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID),
        })
    bottom.append({
        "text": "Сохранить",
        "callback_data": f"rs_save:{chat_id}:{rank}",
        "style": "primary",
        "icon_custom_emoji_id": str(EMOJI_ROLE_SETTINGS_SAVE_ID),
    })
    rows.append(bottom)

    return {"inline_keyboard": rows}


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rs_chat:"))
def cb_rolesettings_chat(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    try:
        _, chat_id_s = c.data.split(":", 1)
        chat_id = int(chat_id_s)
    except Exception:
        return

    if not _user_can_edit_now(c.from_user, chat_id):
        return

    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    emoji_choose = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHOOSE_RANK_ID}">🔽</tg-emoji>'

    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    kb = _build_ranks_keyboard(chat_id, for_pm=True)
    text = (
        f"{emoji_chat} <b>Настройка прав должностей для чата</b> "
        f"<b>{title}</b> (<code>{chat_id}</code>)\n"
        f"{emoji_choose} <b>Выберите должность для настройки прав:</b>"
    )
    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        kb
    )


@bot.callback_query_handler(func=lambda c: c.data == "rs_back_chats")
def cb_rolesettings_back_chats(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    if not is_owner(c.from_user):
        return
    kb = _build_chats_keyboard_for_owner(c.from_user)
    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    text = f"{emoji_chat} <b>Выбери чат для настройки прав должностей:</b>"
    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        kb
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rs_rank:"))
def cb_rolesettings_rank(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    try:
        _, chat_id_s, rank_s = c.data.split(":", 2)
        chat_id = int(chat_id_s)
        rank = int(rank_s)
    except Exception:
        return

    if not _user_can_edit_now(c.from_user, chat_id):
        return

    in_pm = (c.message.chat.type == 'private')
    kb_dict = _build_perms_keyboard_colored(chat_id, rank, in_pm=in_pm)

    rank_html = get_rank_label_html(rank) or f"Ранг {rank}"
    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    text = (
        f"{emoji_chat} <b>Чат:</b> <b>{title}</b> (<code>{chat_id}</code>)\n"
        f"<b>Должность:</b> {rank_html}\n"
        f"Зелёный цвет — включено. Красный цвет — выключено."
    )
    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        kb_dict
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rs_perm:"))
def cb_rolesettings_perm(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    try:
        _, chat_id_s, rank_s, perm_key = c.data.split(":", 3)
        chat_id = int(chat_id_s)
        rank = int(rank_s)
    except Exception:
        return

    if not _user_can_edit_now(c.from_user, chat_id):
        return

    perms = get_role_perms(chat_id, rank)
    perms[perm_key] = not perms.get(perm_key, False)
    save_role_perms()

    in_pm = (c.message.chat.type == 'private')
    kb_dict = _build_perms_keyboard_colored(chat_id, rank, in_pm=in_pm)

    rank_html = get_rank_label_html(rank) or f"Ранг {rank}"
    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    text = (
        f"{emoji_chat} <b>Чат:</b> <b>{title}</b> (<code>{chat_id}</code>)\n"
        f"<b>Должность:</b> {rank_html}\n"
        f"Зелёный цвет — включено. Красный цвет — выключено."
    )
    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        kb_dict
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rs_back:"))
def cb_rolesettings_back_chat(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    try:
        _, chat_id_s = c.data.split(":", 1)
        chat_id = int(chat_id_s)
    except Exception:
        return

    if not _user_can_edit_now(c.from_user, chat_id):
        return

    in_pm = (c.message.chat.type == 'private')
    kb = _build_ranks_keyboard(chat_id, for_pm=in_pm, back_callback=f"st_back_main:{chat_id}" if in_pm else None)

    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    emoji_choose = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHOOSE_RANK_ID}">🔽</tg-emoji>'

    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    text = (
        f"{emoji_chat} <b>Настройка прав должностей для чата</b> "
        f"<b>{title}</b> (<code>{chat_id}</code>)\n"
        f"{emoji_choose} <b>Выберите должность для настройки прав:</b>"
    )
    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        kb
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("rs_save:"))
def cb_rolesettings_save(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id, "Сохранено")
    try:
        _, chat_id_s, rank_s = c.data.split(":", 2)
        chat_id = int(chat_id_s)
        rank = int(rank_s)
    except Exception:
        return

    if not _user_can_edit_now(c.from_user, chat_id):
        return

    perms = get_role_perms(chat_id, rank)
    enabled_pairs = [(key, label) for key, label in ROLE_PERMS_KEYS if perms.get(key)]
    enabled_labels = [label for _, label in enabled_pairs]

    rank_html = get_rank_label_html(rank) or f"Ранг {rank}"

    try:
        chat = bot.get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    emoji_saved = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    emoji_chat = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CHAT_ID}">📋</tg-emoji>'
    emoji_enabled = '<tg-emoji emoji-id="5472308992514464048">✔️</tg-emoji>'

    if enabled_labels:
        perms_text = ", ".join(enabled_labels)
    else:
        perms_text = "нет включённых прав"

    text = (
        f"{emoji_saved} <b>Права для должности {rank_html} сохранены.</b>\n"
        f"{emoji_chat} <b>Чат:</b> <b>{title}</b> (<code>{chat_id}</code>)\n"
        f"{emoji_enabled} <b>Включено прав:</b> {perms_text}"
    )

    open_again_kb = {
        "inline_keyboard": [[{
            "text": "Открыть настройки снова",
            "callback_data": f"rs_rank:{chat_id}:{rank}",
            "icon_custom_emoji_id": str(EMOJI_ROLE_SETTINGS_OPEN_AGAIN_ID),
            "style": "primary"
        }]]
    }

    raw_edit_message_with_keyboard(
        c.message.chat.id,
        c.message.message_id,
        text,
        open_again_kb
    )


@bot.callback_query_handler(func=lambda c: c.data == "rs_cancel")
def cb_rolesettings_cancel(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    bot.answer_callback_query(c.id)
    emoji_cancel = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    try:
        raw_edit_message_with_keyboard(
            c.message.chat.id,
            c.message.message_id,
            f"{emoji_cancel} Настройка прав отменена.",
            None
        )
    except Exception:
        pass

# ==== ВСПОМОГАТЕЛЬНЫЕ ====

def premium_prefix(text: str) -> str:
    return f'<tg-emoji emoji-id="{PREMIUM_PREFIX_EMOJI_ID}">⚠️</tg-emoji> ' + text


COMMAND_COOLDOWNS = {
    "user": {},
    "chat": {},
}

COMMAND_COOLDOWN_NOTICES = {
    "user": {},
    "chat": {},
}


def _cooldown_cache_key(scope: str, bucket: int, action: str) -> str:
    return f"{scope}:{bucket}:{action}"


def _cooldown_notice_once(scope: str, bucket: int, action: str) -> bool:
    store = COMMAND_COOLDOWNS.get(scope)
    notice_store = COMMAND_COOLDOWN_NOTICES.get(scope)
    if not isinstance(store, dict) or not isinstance(notice_store, dict):
        return True

    now_ts = time.time()
    key = _cooldown_cache_key(scope, bucket, action)
    cooldown_until_ts = float(store.get(key, 0) or 0)
    if cooldown_until_ts <= now_ts:
        notice_store.pop(key, None)
        return True

    notified_until_ts = float(notice_store.get(key, 0) or 0)
    if notified_until_ts >= cooldown_until_ts:
        return False

    notice_store[key] = cooldown_until_ts
    return True


def cooldown_hit(scope: str, bucket: int, action: str, seconds: int) -> int:
    store = COMMAND_COOLDOWNS.get(scope)
    if not isinstance(store, dict):
        return 0

    now_ts = time.time()
    key = _cooldown_cache_key(scope, bucket, action)
    until_ts = float(store.get(key, 0) or 0)

    if until_ts > now_ts:
        return max(1, int(until_ts - now_ts))

    store[key] = now_ts + int(seconds)
    return 0


def reply_cooldown_message(
    m: types.Message,
    wait_seconds: int,
    *,
    scope: Optional[str] = None,
    bucket: Optional[int] = None,
    action: Optional[str] = None,
):
    if scope and bucket is not None and action:
        if not _cooldown_notice_once(scope, bucket, action):
            return None

    text = (
        f'<tg-emoji emoji-id="{EMOJI_RATE_LIMIT_ID}">⏳</tg-emoji> '
        f"Подожди <b>{wait_seconds}</b> сек. и повтори команду."
    )
    return bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


TEXTS = {
    "group_only": "Команда доступна только в группах.",
    "user_not_found": "Не удалось определить пользователя.",
    "no_perm_verify": "У вашей должности нет права управлять верификацией.",
    "no_perm_view_verify": "У вашей должности нет права смотреть список верифицированных пользователей.",
    "verify_already": "Этот пользователь уже имеет статус верифицированного.",
    "verify_missing": "У этого пользователя нет статуса верификации.",
    "verify_list_empty": "В этом чате нет верифицированных пользователей.",
    "no_perm_del": "У вашей должности нет права удалять сообщения.",
    "bot_no_del": "У бота нет права удалять сообщения.",
}


def _t(key: str, default: str = "") -> str:
    return TEXTS.get(key, default or key)


def reply_error(m: types.Message, key_or_text: str):
    text = _t(key_or_text, key_or_text)
    return bot.reply_to(m, premium_prefix(text), parse_mode='HTML', disable_web_page_preview=True)

def mention_html_by_id(user_id: int, fallback_name: str = "Пользователь") -> str:
    return f'<a href="tg://user?id={user_id}">{fallback_name}</a>'

def mention_html(user: telebot.types.User) -> str:
    name = user.full_name or user.first_name or user.username or "Пользователь"
    return mention_html_by_id(user.id, name)


def mention_html_user(user: telebot.types.User) -> str:
    return mention_html(user)

def stats_link_for_user(chat_id: int, user_id: int) -> str:
    chat_id_s = str(chat_id)
    user_id_s = str(user_id)

    chat_users = USERS.get(chat_id_s) or {}
    data = chat_users.get(user_id_s) or {}

    username = data.get("username") or ""
    full_name = data.get("full_name") or data.get("first_name") or "Пользователь"

    if username:
        # t.me/username
        return f'[{full_name}](https://t.me/{username})'
    else:
        # fallback для Markdown-статистики, если понадобится
        return f'[{full_name}](tg://openmessage?user_id={user_id})'

def is_owner(user: types.User | None) -> bool:
    """
    Владелец бота — это пользователь с username = OWNER_USERNAME.
    """
    if not user:
        return False
    uname = (user.username or "").lower()
    return uname == (OWNER_USERNAME or "").lower()

def is_dev(user) -> bool:
    return is_owner(user) or (user.id in VERIFY_DEV)

def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = tg_get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except Exception:
        return False

def deny_access(chat_id):
    bot.send_message(chat_id, premium_prefix("Бот недоступен для вашего аккаунта."))

def is_channel_post_message(m: types.Message | None) -> bool:
    if not m:
        return False
    sender_chat = getattr(m, "sender_chat", None)
    return bool(sender_chat and getattr(sender_chat, "type", None) == "channel")


def should_ignore_text_triggers(m: types.Message | None) -> bool:
    return is_channel_post_message(m)


_LINKED_CHANNEL_ID_CACHE: dict[int, tuple[float, int | None]] = {}
_LINKED_CHANNEL_ID_CACHE_LOCK = threading.Lock()
_LINKED_CHANNEL_ID_CACHE_TTL = 300.0


def get_linked_channel_id(chat_id: int) -> int | None:
    now = time.monotonic()
    cid = int(chat_id)
    with _LINKED_CHANNEL_ID_CACHE_LOCK:
        cached = _LINKED_CHANNEL_ID_CACHE.get(cid)
        if cached and (now - cached[0]) < _LINKED_CHANNEL_ID_CACHE_TTL:
            return cached[1]

    linked_id: int | None = None
    try:
        chat_obj = tg_get_chat(cid)
        raw_linked_id = getattr(chat_obj, "linked_chat_id", None)
        if raw_linked_id is not None:
            linked_id = int(raw_linked_id)
    except Exception:
        linked_id = None

    with _LINKED_CHANNEL_ID_CACHE_LOCK:
        _LINKED_CHANNEL_ID_CACHE[cid] = (now, linked_id)
    return linked_id


def is_reply_to_linked_channel_post(m: types.Message | None) -> bool:
    if not m or not getattr(m, "reply_to_message", None):
        return False
    reply = m.reply_to_message
    sender_chat = getattr(reply, "sender_chat", None)
    if not sender_chat or getattr(sender_chat, "type", None) != "channel":
        return False
    linked_id = get_linked_channel_id(int(getattr(m.chat, "id", 0) or 0))
    if not linked_id:
        return False
    try:
        return int(getattr(sender_chat, "id", 0) or 0) == int(linked_id)
    except Exception:
        return False


def _coerce_trigger_text(value: str | types.Message | None) -> tuple[str, types.Message | None]:
    if isinstance(value, types.Message):
        return str(getattr(value, "text", "") or ""), value
    return str(value or ""), None


def is_exact_stat(text: str | types.Message) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower() == 'статистика'


def is_exact_stat_day(text: str | types.Message) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower() == 'статистика день'


def is_exact_stat_week(text: str | types.Message) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower() == 'статистика неделя'


def is_exact_stat_month(text: str | types.Message) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower() == 'статистика месяц'


def is_exact_stat_all(text: str | types.Message) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower() == 'статистика вся'


def text_starts_with_ci(text: str | types.Message, prefix: str) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    return bool(txt) and txt.strip().lower().startswith(prefix.lower())

def format_bytes_mb(num_bytes: int) -> str:
    return f"{num_bytes / (1024 * 1024):.1f} MB"

def match_command(text: str | types.Message, name: str) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    if not txt:
        return False
    t = txt.strip()
    first = t.split(maxsplit=1)[0].lower()
    for prefix in COMMAND_PREFIXES:
        if first.startswith(prefix):
            body = first[len(prefix):]
            body = body.split('@', 1)[0]
            return body == name.lower()
    return False


def match_command_aliases(text: str | types.Message, names: list[str]) -> bool:
    txt, msg = _coerce_trigger_text(text)
    if msg and should_ignore_text_triggers(msg):
        return False
    if not txt:
        return False

    first = txt.strip().split(maxsplit=1)[0].lower()
    normalized = {str(n).lower() for n in (names or []) if str(n).strip()}
    if not normalized:
        return False

    for prefix in COMMAND_PREFIXES:
        if first.startswith(prefix):
            body = first[len(prefix):]
            body = body.split('@', 1)[0]
            return body in normalized

    return first in normalized

def parse_target_user(m: types.Message, args: list[str]) -> int | None:
    # 1) если есть reply — всегда берем из reply
    if m.reply_to_message:
        update_user_in_chat(m.chat, m.reply_to_message.from_user)
        return m.reply_to_message.from_user.id

    if not args:
        return None

    text = args[0].strip()

    # 2) ID
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None

    # 3) t.me/ссылка (любой формат t.me/...)
    if text.startswith("https://t.me/") or text.startswith("http://t.me/") or text.startswith("t.me/"):
        link = text.split("t.me/")[1]
        if "/" in link:
            uname = link.split("/")[0]
        else:
            uname = link
        uname = uname.lstrip("@")
        uname_low = uname.lower()

        # O(1) lookup через обратный индекс
        with _USERNAME_INDEX_LOCK:
            uid_s = _USERNAME_INDEX.get(m.chat.id, {}).get(uname_low)
        if uid_s is not None:
            try:
                return int(uid_s)
            except (ValueError, TypeError):
                pass

        # потом пробуем Bot API
        try:
            ch = bot.get_chat(f"@{uname}")
            if isinstance(ch, types.User):
                return ch.id
            return ch.id
        except Exception:
            # если Bot API не смог — MTProto
            try:
                mt_id = call_mtproto_sync(get_user_id_by_username_mtproto(uname))
            except Exception:
                mt_id = None
            return mt_id
    if text.startswith("@"):
        uname = text.lstrip("@")
        uname_low = uname.lower()

        # O(1) lookup через обратный индекс
        with _USERNAME_INDEX_LOCK:
            uid_s = _USERNAME_INDEX.get(m.chat.id, {}).get(uname_low)
        if uid_s is not None:
            try:
                return int(uid_s)
            except (ValueError, TypeError):
                pass

        # потом Bot API
        try:
            ch = bot.get_chat(f"@{uname}")
            if isinstance(ch, types.User):
                return ch.id
            return ch.id
        except Exception:
            # и снова MTProto
            try:
                mt_id = call_mtproto_sync(get_user_id_by_username_mtproto(uname))
            except Exception:
                mt_id = None
            return mt_id

    # если ничего не подошло
    return None

_BOT_INFO_CACHE: dict = {}


def _get_bot_info_cached() -> dict:
    global _BOT_INFO_CACHE
    if not _BOT_INFO_CACHE:
        try:
            me = get_bot_me()
            name_parts = [getattr(me, "first_name", "") or "", getattr(me, "last_name", "") or ""]
            full_name = " ".join(p for p in name_parts if p).strip() or getattr(me, "username", "") or "Бот"
            _BOT_INFO_CACHE = {
                "id": int(getattr(me, "id", 0)),
                "username": (getattr(me, "username", "") or "").lower(),
                "full_name": full_name,
            }
        except Exception:
            _BOT_INFO_CACHE = {}
    return _BOT_INFO_CACHE


def link_for_user(chat_id: int, user_id: int) -> str:
    chat_id_s = str(chat_id)
    user_id_s = str(user_id)
    chat_users = USERS.get(chat_id_s) or {}
    data = chat_users.get(user_id_s) or {}

    # Fallback to global users dict
    if not data:
        data = (GLOBAL_USERS or {}).get(user_id_s) or {}

    # Fallback to bot's own info if this is the bot
    if not data:
        bot_info = _get_bot_info_cached()
        if bot_info and bot_info.get("id") == user_id:
            data = bot_info

    username = (data.get("username") or "").lower()
    full_name = data.get("full_name") or data.get("first_name") or "Без имени"

    if username:
        url = f"https://t.me/{username}"
    else:
        url = f"tg://openmessage?user_id={user_id}"

    return f'<a href="{url}">{full_name}</a>'


def resolve_target_for_dev(m: types.Message) -> int | None:
    """
    Резолв цели для /devverify и /devunverify в ЛС.
    Поддерживает: reply, ID, t.me/username, @username.
    """
    # 1) reply
    if m.reply_to_message and m.reply_to_message.from_user:
        return m.reply_to_message.from_user.id

    # 2) аргументы
    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    if not raw_after:
        return None
    text = raw_after.strip()

    # 3) числовой ID
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None

    # 4) t.me/username или @username
    if text.startswith("https://t.me/") or text.startswith("http://t.me/") or text.startswith("t.me/"):
        link = text.split("t.me/")[1]
        if "/" in link:
            username = link.split("/")[0]
        else:
            username = link
        uname = username.lstrip("@").strip().lower()
    else:
        uname = text.lstrip("@").strip().lower()

    if not uname:
        return None

    # 5) поиск в GLOBAL_USERS по username
    for uid, data in (GLOBAL_USERS or {}).items():
        un = (data.get("username") or "").lower()
        if un and un == uname:
            try:
                return int(uid)
            except ValueError:
                continue

    # 6) MTProto, если в глобалке нет
    try:
        mtid = call_mtproto_sync(get_user_id_by_username_mtproto(uname))
    except Exception:
        mtid = None

    if mtid:
        # обновим глобалку минимальным набором
        update_global_user_basic(mtid, uname)
        return mtid

    return None


def _remember_owner_user_id(user: types.User | None):
    if not user or not is_owner(user):
        return
    prev = int(DEV_CONTACT_META.get("owner_user_id") or 0)
    if prev != user.id:
        DEV_CONTACT_META["owner_user_id"] = user.id
        save_dev_contact_meta()


def _resolve_owner_user_id() -> int | None:
    owner_id = int(DEV_CONTACT_META.get("owner_user_id") or 0)
    if owner_id > 0:
        return owner_id

    owner_username = (OWNER_USERNAME or "").strip().lstrip("@").lower()
    if not owner_username:
        return None

    try:
        owner_chat = bot.get_chat(f"@{owner_username}")
        owner_id = int(getattr(owner_chat, "id", 0) or 0)
        if owner_id > 0:
            DEV_CONTACT_META["owner_user_id"] = owner_id
            save_dev_contact_meta()
            return owner_id
    except Exception:
        pass

    for rec in (GLOBAL_USERS or {}).values():
        if not isinstance(rec, dict):
            continue
        username = str(rec.get("username") or "").strip().lstrip("@").lower()
        if username != owner_username:
            continue
        uid = int(rec.get("id") or 0)
        if uid > 0:
            DEV_CONTACT_META["owner_user_id"] = uid
            save_dev_contact_meta()
            return uid

    return None


def _dev_contact_new_id() -> int:
    last_id = int(DEV_CONTACT_INBOX.get("last_id") or 0) + 1
    DEV_CONTACT_INBOX["last_id"] = last_id
    return last_id


def _dev_contact_find_item(message_id: int) -> dict | None:
    for item in (DEV_CONTACT_INBOX.get("items") or []):
        if int(item.get("id") or 0) == int(message_id):
            return item
    return None


def resolve_target_like_profile(m: types.Message) -> int | None:
    """
    reply / id / @username / t.me → target_id.
    Для verify/unverify без указателя/ответа возвращает None.
    """
    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message or args:
        return parse_target_user(m, args)
    return None


def is_verified_admin(chat: types.Chat, user_id: int) -> bool:
    chat_id_s = str(chat.id)
    return user_id in (VERIFY_ADMINS.get(chat_id_s) or [])


@bot.message_handler(commands=['verify'])
def cmd_verify(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('verify')

    if m.chat.type not in ['group', 'supergroup']:
        return reply_error(m, "group_only")

    target_id = resolve_target_like_profile(m)

    if isinstance(target_id, int) and target_id < 1000:
        return reply_error(m, "user_not_found")

    if target_id is None:
        return reply_error(m, "user_not_found")

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_MANAGE_VERIFY)
    if not allowed:
        if status == 'no_perm':
            return reply_error(m, "no_perm_verify")
        # rank 0 — молчание
        return

    chat_id_s = str(m.chat.id)
    users = VERIFY_ADMINS.get(chat_id_s) or []

    if target_id in users:
        return reply_error(m, "verify_already")

    users.append(target_id)
    VERIFY_ADMINS[chat_id_s] = users
    save_verify_admins()

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_VERIFY_ADMIN_ID}">✔️</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>получил статус верифицированного пользователя.</b>"
    )
    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(commands=['unverify'])
def cmd_unverify(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('unverify')

    if m.chat.type not in ['group', 'supergroup']:
        return reply_error(m, "group_only")

    target_id = resolve_target_like_profile(m)

    if isinstance(target_id, int) and target_id < 1000:
        return reply_error(m, "user_not_found")

    if target_id is None:
        return reply_error(m, "user_not_found")

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_MANAGE_VERIFY)
    if not allowed:
        if status == 'no_perm':
            return reply_error(m, "no_perm_verify")
        return

    chat_id_s = str(m.chat.id)
    users = VERIFY_ADMINS.get(chat_id_s) or []

    if target_id not in users:
        return reply_error(m, "verify_missing")

    users.remove(target_id)
    VERIFY_ADMINS[chat_id_s] = users
    save_verify_admins()

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_VERIFY_ADMIN_ID}">✔️</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>лишён статуса верифицированного пользователя.</b>"
    )
    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(commands=['vlist'])
def cmd_vlist(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('vlist')

    wait_seconds = cooldown_hit('chat', int(m.chat.id), 'vlist', 15)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='vlist')

    if m.chat.type not in ['group', 'supergroup']:
        return reply_error(m, "group_only")

    # спец-актеры всегда могут, остальные — через PERM_VIEW_LISTS
    if not _is_special_actor(m.chat.id, m.from_user):
        status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_VIEW_LISTS)
        if not allowed:
            if status == 'no_perm':
                return reply_error(m, "no_perm_view_verify")
            # rank 0 — молчание
            return

    chat_id_s = str(m.chat.id)
    users = VERIFY_ADMINS.get(chat_id_s) or []

    if not users:
        return reply_error(m, "verify_list_empty")

    lines = []
    for uid in users:
        name = link_for_user(m.chat.id, uid)
        lines.append(f"•  {name} [<code>{uid}</code>]")

    text = (
        f'<tg-emoji emoji-id="{EMOJI_VERIFY_ADMIN_ID}">✔️</tg-emoji> '
        f"<b>Верифицированные пользователи:</b>\n" +
        "\n".join(lines)
    )
    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(commands=['devverify'])
def cmd_devverify(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('devverify')

    # Только владелец и только ЛС, для остальных — тихо выходим
    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    target_id = resolve_target_for_dev(m)
    if not isinstance(target_id, int) or target_id < 1:
        return bot.reply_to(
            m,
            premium_prefix("Пользователь не найден. Укажи ID, @username или ответь на его сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if target_id in VERIFY_DEV:
        return bot.reply_to(
            m,
            premium_prefix("У пользователя уже есть статус глобального разработчика."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    VERIFY_DEV.add(target_id)
    save_verify_dev()

    # Попробуем получить инфу о юзере и обновить глобалку
    name_html = f"ID <code>{target_id}</code>"
    u = tg_get_user_by_id_cached(target_id)
    if isinstance(u, types.User):
        try:
            update_global_user_from_telebot(u)
            name_html = mention_html_user(u)
        except Exception:
            pass

    text = (
        f'<tg-emoji emoji-id="{EMOJI_VERIFY_DEV_ID}">✔️</tg-emoji> '
        f"{name_html} получил статус глобального разработчика."
    )
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)

@bot.message_handler(commands=['devunverify'])
def cmd_devunverify(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('devunverify')

    # Только владелец и только ЛС
    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    target_id = resolve_target_for_dev(m)
    if not isinstance(target_id, int) or target_id < 1:
        return bot.reply_to(
            m,
            premium_prefix("Пользователь не найден. Укажи ID, @username или ответь на его сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if target_id not in VERIFY_DEV:
        return bot.reply_to(
            m,
            premium_prefix("У пользователя нет статуса глобального разработчика."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    VERIFY_DEV.remove(target_id)
    save_verify_dev()

    name_html = f"ID <code>{target_id}</code>"
    u = tg_get_user_by_id_cached(target_id)
    if isinstance(u, types.User):
        try:
            update_global_user_from_telebot(u)
            name_html = mention_html_user(u)
        except Exception:
            pass

    text = (
        f'<tg-emoji emoji-id="{EMOJI_VERIFY_DEV_ID}">✔️</tg-emoji> '
        f"С пользователя {name_html} снят статус глобального разработчика."
    )
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(commands=['devvlist'])
def cmd_devvlist(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('devvlist')

    # Только ЛС
    if m.chat.type != 'private':
        return

    # Только владелец и dev-юзеры
    if not is_dev(m.from_user):
        return

    if not VERIFY_DEV:
        return bot.reply_to(
            m,
            premium_prefix("Нет пользователей с глобальной верификацией разработчика."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    lines = []
    for uid in sorted(VERIFY_DEV):
        name_html = f"ID <code>{uid}</code>"

        # сначала пробуем GLOBAL_USERS
        gu = GLOBAL_USERS.get(str(uid)) if 'GLOBAL_USERS' in globals() else None
        if gu:
            username = gu.get("username") or ""
            full_name = gu.get("full_name") or gu.get("first_name") or ""
            if username:
                if full_name:
                    name_html = f"@{username} ({full_name}) [<code>{uid}</code>]"
                else:
                    name_html = f"@{username} [<code>{uid}</code>]"
            elif full_name:
                name_html = f"{full_name} [<code>{uid}</code>]"

        # если в глобалке нет — пробуем Bot API и сразу обновляем GLOBAL_USERS
        if not gu:
            u = tg_get_user_by_id_cached(uid)
            if isinstance(u, types.User):
                try:
                    update_global_user_from_telebot(u)
                    name_html = f"{mention_html_user(u)} [<code>{uid}</code>]"
                except Exception:
                    pass

        lines.append(
            f'<tg-emoji emoji-id="{EMOJI_VERIFY_DEV_ID}">✔️</tg-emoji> {name_html}'
        )

    text = (
        "<b>Глобально верифицированные разработчики:</b>\n" +
        "\n".join(lines)
    )

    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(commands=['dbg_global_users'])
def cmd_dbg_global_users(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('dbg_global_users')

    if not is_owner(m.from_user):
        return  # тихо игнорируем

    total = len(GLOBAL_USERS or {})
    lines = [f"GLOBAL_USERS: всего {total} пользователей.", ""]

    # покажем до 50 строк для примера
    max_show = 1000
    count = 0
    def _uid_sort_key(item):
        key = item[0]
        try:
            return int(key)
        except Exception:
            return 0

    for uid, data in sorted((GLOBAL_USERS or {}).items(), key=_uid_sort_key):
        username = (data.get("username") or "").strip()
        full_name = (data.get("full_name") or (data.get("first_name") or "")).strip()
        last_seen = int(float(data.get("last_seen") or 0))
        uname = f"@{username}" if username else "-"
        lines.append(f"{uid}\t{uname}\t{full_name}\tlast_seen={last_seen}")
        count += 1
        if count >= max_show:
            break

    if total > max_show:
        lines.append("")
        lines.append(f"... и ещё {total - max_show} записей")

    payload = "\n".join(lines).encode("utf-8")
    doc = io.BytesIO(payload)
    doc.name = f"dbg_global_users_{int(time.time())}.txt"
    bot.send_document(
        m.chat.id,
        doc,
        caption=f"Экспорт GLOBAL_USERS: {total} записей.",
        reply_to_message_id=m.message_id,
    )

@bot.message_handler(commands=['migrate_users_to_global'])
def cmd_migrate_users_to_global(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('migrate_users_to_global')

    # Только владелец и только ЛС
    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    migrated = 0
    seen = set()

    # USERS: { chat_id: { user_id: {...} } }
    for chat_id_s, chat_users in (USERS or {}).items():
        for uid, data in (chat_users or {}).items():
            try:
                user_id = int(uid)
            except ValueError:
                continue
            if user_id in seen:
                continue
            seen.add(user_id)

            username = (data.get("username") or "").lower() or None
            full_name = data.get("full_name") or ""
            first_name = data.get("first_name") or ""
            last_name = data.get("last_name") or ""

            # базовая запись
            rec = GLOBAL_USERS.get(uid) or {}
            rec["id"] = user_id
            if username:
                rec["username"] = username
            if full_name:
                rec["full_name"] = full_name
            if first_name:
                rec["first_name"] = first_name
            if last_name:
                rec["last_name"] = last_name
            rec["last_seen"] = time.time()
            GLOBAL_USERS[uid] = rec
            migrated += 1

    save_global_users()

    text = premium_prefix(f"Миграция завершена. Добавлено/обновлено {migrated} пользователей в GLOBAL_USERS.")
    bot.reply_to(m, text)


@bot.message_handler(commands=['dbmigrate', 'sqlite_migrate'])
def cmd_dbmigrate(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('dbmigrate')

    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    stats = migrate_legacy_json_to_sqlite()
    text = (
        "<b>Миграция JSON → SQLite завершена</b>\n"
        f"Перенесено: <code>{stats['migrated']}</code>\n"
        f"Пропущено (файл отсутствует): <code>{stats['skipped']}</code>\n"
        f"Ошибок: <code>{stats['failed']}</code>\n"
        f"Всего путей проверено: <code>{stats['total']}</code>"
    )
    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['dbstatus', 'sqlite_status'])
def cmd_dbstatus(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('dbstatus')

    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    st = get_sqlite_status()
    latest = int(st.get('latest_updated_at') or 0)
    latest_h = datetime.fromtimestamp(latest).strftime('%d.%m.%Y %H:%M:%S') if latest > 0 else "-"
    keys = st.get('keys') or []
    keys_preview = "\n".join(f"• <code>{_html.escape(str(k))}</code>" for k in keys) if keys else "• -"

    text = (
        "<b>SQLite статус</b>\n"
        f"Файл: <code>{_html.escape(str(st.get('db_path') or '-'))}</code>\n"
        f"Существует: <code>{'да' if st.get('exists') else 'нет'}</code>\n"
        f"Размер: <code>{int(st.get('size_bytes') or 0)}</code> bytes\n"
        f"Записей kv_store: <code>{int(st.get('rows') or 0)}</code>\n"
        f"Последнее обновление: <code>{latest_h}</code>\n"
        f"Fallback JSON при ошибке SQLite: <code>{'on' if SQLITE_JSON_FALLBACK_WRITE else 'off'}</code>\n"
        "\n"
        "<b>Последние ключи:</b>\n"
        f"{keys_preview}"
    )
    if st.get('error'):
        text += f"\n\n<b>Ошибка:</b> <code>{_html.escape(str(st['error']))}</code>"

    bot.reply_to(m, text, parse_mode='HTML')


@bot.message_handler(commands=['botstatus'])
def cmd_botstatus(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    add_stat_message(m)
    add_stat_command('botstatus')

    if m.chat.type != 'private':
        return
    if not is_owner(m.from_user):
        return

    sqlite_status = get_sqlite_status()
    cache_stats = get_tg_cache_stats()
    queue_stats = get_operation_queue_stats()
    latest = int(sqlite_status.get('latest_updated_at') or 0)
    latest_h = datetime.fromtimestamp(latest).strftime('%d.%m.%Y %H:%M:%S') if latest > 0 else '-'

    try:
        proc = psutil.Process(os.getpid())
        process_ram = format_bytes_mb(proc.memory_info().rss)
    except Exception:
        process_ram = 'n/a'

    # Telethon/MTProto status
    try:
        from config import tg_client as _tg_client
        mt_connected = _tg_client.is_connected()
    except Exception:
        mt_connected = _tg_client_status.get("connected", False)
    mt_icon = "✅" if mt_connected else "❌"
    mt_last_use = int(_tg_client_status.get("last_use") or 0)
    mt_last_use_h = datetime.fromtimestamp(mt_last_use).strftime('%d.%m.%Y %H:%M:%S') if mt_last_use > 0 else '-'
    mt_last_err = _tg_client_status.get("last_error") or '-'

    text = (
        '<b>Состояние бота</b>\n'
        f"<b>Uptime:</b> <code>{get_uptime_text()}</code>\n"
        f"<b>Обработано сообщений:</b> <code>{int(STATS.get('messages') or 0)}</code>\n"
        f"<b>Уникальных пользователей:</b> <code>{len(STATS.get('users') or set())}</code>\n"
        f"<b>Чатов runtime:</b> <code>{len(STATS.get('chats') or set())}</code>\n"
        f"<b>RAM процесса:</b> <code>{process_ram}</code>\n"
        '\n'
        '<b>Telethon / MTProto</b>\n'
        f"{mt_icon} <b>Соединение:</b> <code>{'подключён' if mt_connected else 'отключён'}</code>\n"
        f"<b>Последнее использование:</b> <code>{mt_last_use_h}</code>\n"
        f"<b>Последняя ошибка:</b> <code>{_html.escape(mt_last_err)}</code>\n"
        '\n'
        '<b>TG кэш</b>\n'
        f"<b>Member cache:</b> <code>{cache_stats['member_size']}</code>\n"
        f"<b>Chat cache:</b> <code>{cache_stats['chat_size']}</code>\n"
        f"<b>Всего записей кэша:</b> <code>{cache_stats['total_size']}</code>\n"
        f"<b>Cache misses:</b> <code>{cache_stats['total_misses']}</code>"
        f" (member: <code>{cache_stats['member_misses']}</code>, chat: <code>{cache_stats['chat_misses']}</code>)\n"
        f"<b>get_chat(user_id) в апдейте:</b> hits <code>{cache_stats['user_fetch_hits']}</code>, "
        f"misses <code>{cache_stats['user_fetch_misses']}</code>\n"
        '\n'
        '<b>SQLite</b>\n'
        f"<b>Файл:</b> <code>{_html.escape(str(sqlite_status.get('db_path') or '-'))}</code>\n"
        f"<b>Записей kv_store:</b> <code>{int(sqlite_status.get('rows') or 0)}</code>\n"
        f"<b>Размер файла:</b> <code>{int(sqlite_status.get('size_bytes') or 0)}</code> bytes\n"
        f"<b>Последнее обновление:</b> <code>{latest_h}</code>\n"
        f"<b>Ошибок SQLite:</b> <code>{int(STATS.get('sqlite_errors') or 0)}</code>\n"
        '\n'
        '<b>Очередь операций</b>\n'
        f"<b>В работе:</b> <code>{queue_stats['active']}</code>\n"
        f"<b>В очереди:</b> <code>{queue_stats['queued']}</code>\n"
        f"<b>Всего задач:</b> <code>{queue_stats['total']}</code>"
    )

    if sqlite_status.get('error'):
        text += f"\n\n<b>Ошибка SQLite:</b> <code>{_html.escape(str(sqlite_status['error']))}</code>"

    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


def get_operation_queue_stats() -> dict[str, int]:
    with _OPERATION_QUEUE_LOCK:
        active_count = len(_OPERATION_QUEUE_ACTIVE)
    queued_count = _OPERATION_QUEUE.qsize()
    return {
        'queued': queued_count,
        'active': active_count,
        'total': queued_count + active_count,
    }

# ==== СТАТИСТИКА БОТА ====


def add_stat_message(message: types.Message):
    STATS['messages'] += 1
    STATS['users'].add(message.from_user.id)
    STATS['chats'].add(message.chat.id)
    _remember_owner_user_id(message.from_user)
    update_group_stats(message)
    # обновляем БД пользователей по чатам
    update_user_in_chat(message.chat, message.from_user)
    # обновляем глобальную БД пользователей
    update_global_user_from_telebot(message.from_user)

def add_stat_command(cmd: str):
    STATS['commands_used'][cmd] = STATS['commands_used'].get(cmd, 0) + 1

def get_uptime_text() -> str:
    sec = int(time.time() - STATS['start_time'])
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, sec = divmod(sec, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    parts.append(f"{sec}с")
    return " ".join(parts)

def get_top_commands(limit=5):
    if not STATS['commands_used']:
        return "нет данных"
    sorted_cmds = sorted(STATS['commands_used'].items(), key=lambda x: x[1], reverse=True)
    return "\n".join(f"/{cmd} — <code>{cnt}</code>" for cmd, cnt in sorted_cmds[:limit])


# ==== NEW CHAT OR MEMBERS ====

@bot.message_handler(content_types=['new_chat_members'])
def on_new_members(message: types.Message):
    add_stat_message(message)

    me = get_bot_me()
    bot_added = False

    for member in (message.new_chat_members or []):
        if is_group_approved(message.chat.id):
            update_user_in_chat(message.chat, member)

        if member.id == me.id:
            bot_added = True

    if bot_added:
        adder = message.from_user
        chat_title = message.chat.title or "Группа"

        add_pending_group(message.chat.id, chat_title, adder)

        emoji_wait = f'<tg-emoji emoji-id="{EMOJI_RATE_LIMIT_ID}">⏳</tg-emoji>'
        text = (
              f"{emoji_wait} <b>Бот добавлен в чат!</b>\n\n"
              f"Чат <b>{_html.escape(chat_title)}</b> ожидает подтверждения от разработчика.\n"
              f"До подтверждения бот не будет реагировать на команды.\n\n"
              f"⏳ <i>Ожидайте проверки...</i>"
        )
        try:
            bot.send_message(
                message.chat.id,
                text,
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        except Exception:
            pass

        notify_dev_about_new_group(message.chat.id, chat_title, adder)

    return ContinueHandling()


# Построить обратный индекс username при загрузке модуля
# (USERS уже заполнен persistence.py к этому моменту)
_rebuild_username_index()

__all__ = [name for name in globals() if not name.startswith('__')]
