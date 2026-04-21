"""
cmd_basic.py — Основные команды бота:
  /start, pm-обработчики, /ping, /log, /broadcast,
  /профиль, /наградить, /снять награду, /settag, /removetag, /taglist,
  /promote, /demote, /staff, /myrank, /closechat, /openchat
  и их русскоязычные алиасы.

Импортирует из config и helpers (которая включает persistence).
"""
from __future__ import annotations
import time
import threading
import asyncio
import tempfile
import shutil
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
import yt_dlp

from config import (
    os, json, re, _re, _html, random, psutil,
    datetime, Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    bot, bot_raw, tg_client,
    TOKEN, OWNER_USERNAME, DATA_DIR, API_BASE_URL,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    # emoji IDs
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
    EMOJI_PUNISHMENT_ID,
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
    SENDPM_DRAFTS, SENDPM_PENDING_INPUT,
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
    tg_get_user_by_id_cached,
    send_log_event,
)
from helpers import *  # все helper-функции и константы

# ==== БАЗОВЫЕ КОМАНДЫ ====
logger = logging.getLogger(__name__)

START_MENU_STATE: dict[tuple[int, int], dict] = {}

# ── Кеш photo file_id (TTL 5 мин) ────────────────────────────────────────────
_PHOTO_FILE_ID_CACHE: dict[int, tuple[float, str | None]] = {}
_PHOTO_CACHE_LOCK = threading.Lock()
_PHOTO_CACHE_TTL: int = 300  # секунд

# ── Кеш результатов /профиль (TTL 30 сек) ───────────────────────────────────
# Ключ: (chat_id, user_id) → (timestamp, caption_text, photo_file_id_or_None)
_PROFILE_RESULT_CACHE: dict[tuple[int, int], tuple[float, str, str | None]] = {}
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_TTL: int = 30  # секунд


def _invalidate_profile_cache(chat_id: int, user_id: int) -> None:
    """Сбросить кешированный результат профиля (вызывать после любого изменения профиля)."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_RESULT_CACHE.pop((int(chat_id), int(user_id)), None)


# Пул потоков для параллельного построения профиля
_PROFILE_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="profile")


def _build_start_home_keyboard(show_owner_button: bool = False) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("Команды", callback_data="start:commands"),
        types.InlineKeyboardButton("О боте", callback_data="start:about"),
    )
    kb.row(types.InlineKeyboardButton("Связь с разработчиком", callback_data="start:contact"))
    if show_owner_button:
        btn_new = types.InlineKeyboardButton("Новые сообщения", callback_data="start:newmsgs")
        btn_new.icon_custom_emoji_id = EMOJI_NEW_MSG_OWNER_ID
        kb.row(btn_new)
    return kb


def _build_start_back_keyboard(back_to: str = "start:home") -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    btn_back = types.InlineKeyboardButton("Назад", callback_data=back_to)
    btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    kb.row(btn_back)
    return kb


def _build_start_commands_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("Инструкции применения команд", callback_data="start:usage"))
    btn_back = types.InlineKeyboardButton("Назад", callback_data="start:home")
    btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    kb.row(btn_back)
    return kb


def _user_has_admin_section(user_id: int) -> bool:
    if user_id in VERIFY_DEV:
        return True

    for chat_id_s, chat_users in (USERS or {}).items():
        if str(user_id) not in (chat_users or {}):
            continue
        try:
            chat_id = int(chat_id_s)
        except Exception:
            continue
        if get_user_rank(chat_id, user_id) > 0:
            return True

    for users in (VERIFY_ADMINS or {}).values():
        if user_id in (users or []):
            return True

    return False


def _dev_contact_intro_text() -> str:
    emoji_contact = f'<tg-emoji emoji-id="{EMOJI_CONTACT_DEV_ID}">💬</tg-emoji>'
    return (
        f"{emoji_contact} <b>Связь с разработчиком</b>\n\n"
        "Здесь можно отправить вопрос, идею или жалобу разработчику бота.\n"
        "Пожалуйста, пишите по делу — пустые и мусорные сообщения игнорируются."
    )


def _dev_contact_prompt_text() -> str:
    emoji_send = f'<tg-emoji emoji-id="{EMOJI_SEND_TEXT_PROMPT_ID}">✉️</tg-emoji>'
    return (
        f"{emoji_send} <b>Отправьте текстовое сообщение...</b>\n\n"
        "<i>Если вы отправите несколько сообщений, будет обработано только первое.</i>"
    )


def _dev_contact_prompt_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    btn_cancel = types.InlineKeyboardButton("Отменить", callback_data="devcontact:cancel")
    btn_cancel.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
    kb.row(btn_cancel)
    return kb


def _dev_contact_owner_new_text(item: dict) -> str:
    emoji_new = f'<tg-emoji emoji-id="{EMOJI_NEW_MSG_OWNER_ID}">📨</tg-emoji>'
    user_id = int(item.get("user_id") or 0)
    name = _html.escape(item.get("user_full_name") or "Пользователь")
    username = _html.escape(item.get("user_username") or "")
    username_part = f" (@{username})" if username else ""
    body = _html.escape(item.get("text") or "")
    mention = mention_html_by_id(user_id, name)
    return (
        f"{emoji_new} <b>Получено новое сообщение от пользователя!</b>\n\n"
        f"<b>Пользователь:</b> {mention}{username_part}\n\n"
        "<b>Текст сообщения:</b>\n"
        f"<blockquote>{body}</blockquote>"
    )


def _dev_contact_owner_item_kb(item_id: int) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()

    btn_reply = types.InlineKeyboardButton("Ответить", callback_data=f"devmsg:reply:{item_id}")
    btn_reply.icon_custom_emoji_id = EMOJI_REPLY_BTN_ID

    btn_ignore = types.InlineKeyboardButton("Проигнорировать", callback_data=f"devmsg:ignore:{item_id}")
    btn_ignore.icon_custom_emoji_id = EMOJI_IGNORE_BTN_ID

    kb.row(btn_reply, btn_ignore)
    return kb


def _dev_contact_intro_kb() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("Отправить сообщение", callback_data="devcontact:send"))

    btn_back = types.InlineKeyboardButton("Назад", callback_data="devcontact:back")
    btn_back.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
    kb.row(btn_back)
    return kb


def _append_dev_contact_item(user: types.User, text: str) -> dict:
    item = {
        "id": _dev_contact_new_id(),
        "status": "new",
        "created_at": int(time.time()),
        "user_id": int(user.id),
        "user_full_name": user.full_name or user.first_name or "Пользователь",
        "user_username": (user.username or ""),
        "text": text,
        "owner_notified_at": 0,
        "replied_at": 0,
        "reply_text": "",
    }
    DEV_CONTACT_INBOX.setdefault("items", []).append(item)
    save_dev_contact_inbox()
    return item


def _send_dev_contact_item_to_owner(item: dict) -> bool:
    owner_id = _resolve_owner_user_id()
    if not owner_id:
        return False

    try:
        bot.send_message(
            owner_id,
            _dev_contact_owner_new_text(item),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=_dev_contact_owner_item_kb(int(item.get("id") or 0)),
        )
        item["owner_notified_at"] = int(time.time())
        save_dev_contact_inbox()
        return True
    except Exception:
        return False


def _dev_contact_new_items() -> list[dict]:
    items = DEV_CONTACT_INBOX.get("items") or []
    result: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if (item.get("status") or "").lower() != "new":
            continue
        result.append(item)
    return result


def _show_dev_contact_new_messages(owner_id: int):
    items = _dev_contact_new_items()
    if not items:
        emoji_ok = f'<tg-emoji emoji-id="{EMOJI_SENT_OK_ID}">✅</tg-emoji>'
        bot.send_message(owner_id, f"{emoji_ok} <i>Новых сообщений нет.</i>", parse_mode="HTML")
        return

    changed = False
    for item in items:
        try:
            bot.send_message(
                owner_id,
                _dev_contact_owner_new_text(item),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_dev_contact_owner_item_kb(int(item.get("id") or 0)),
            )
            item["owner_notified_at"] = int(time.time())
            changed = True
            time.sleep(0.15)
        except Exception:
            continue

    if changed:
        save_dev_contact_inbox()


def _bot_link_title_html() -> str:
    try:
        me = get_bot_me()
        uname = (getattr(me, "username", "") or "").strip()
        title = (getattr(me, "first_name", "") or uname or "бот").strip()
        if uname:
            return f'<a href="https://t.me/{uname}">{_html.escape(title)}</a>'
        return _html.escape(title)
    except Exception:
        return "бот"


def _build_start_home_text(user: telebot.types.User) -> str:
    mention = mention_html(user)
    bot_title = _bot_link_title_html()

    lines = [
        f"<b>Привет, </b>{mention}<b>!</b>",
        "",
        f"Я {bot_title} бот для помощи в модерации группы. На данный момент меня может добавлять только мой разработчик.",
        "",
        "Если хотите сообщить о баге, предложить идею для бота или добавить бота в свою группу — свяжитесь с разработчиком, написав команду <code>Связь</code>.",
    ]

    if is_dev(user):
        emoji_verified = f'<tg-emoji emoji-id="{EMOJI_VERIFY_DEV_ID}">✅</tg-emoji>'
        emoji_version = f'<tg-emoji emoji-id="{EMOJI_BOT_VERSION_ID}">🏷️</tg-emoji>'
        lines.extend([
            "",
            f"{emoji_verified} Вы верифицированный разработчиком.",
            f"<i>{emoji_version} Версия бота <b>0.91.2</b></i>",
        ])

    return "\n".join(lines)


def _build_start_commands_text(user: telebot.types.User) -> str:
    is_owner_user = is_owner(user)
    is_dev_user = user.id in VERIFY_DEV

    legend_anywhere = f'<tg-emoji emoji-id="{EMOJI_LEGEND_ANYWHERE_ID}">🌐</tg-emoji>'
    legend_dev_only = f'<tg-emoji emoji-id="{EMOJI_LEGEND_DEV_ONLY_ID}">💻</tg-emoji>'
    legend_dev_or_verified = f'<tg-emoji emoji-id="{EMOJI_LEGEND_DEV_OR_VERIFIED_ID}">✅</tg-emoji>'
    legend_group_admin = f'<tg-emoji emoji-id="{EMOJI_LEGEND_GROUP_ADMIN_ID}">🛡️</tg-emoji>'
    legend_pm_only = f'<tg-emoji emoji-id="{EMOJI_LEGEND_PM_ONLY_ID}">💬</tg-emoji>'
    legend_group_only = f'<tg-emoji emoji-id="{EMOJI_LEGEND_GROUP_ONLY_ID}">👥</tg-emoji>'
    legend_all_users = f'<tg-emoji emoji-id="{EMOJI_LEGEND_ALL_USERS_ID}">👤</tg-emoji>'
    parts = [
        "<b>Пользовательские команды</b>",
        f"• /start ({legend_all_users} | {legend_pm_only})",
        f"• /ping, /пинг, ping, пинг ({legend_all_users} | {legend_anywhere})",
        f"• Профиль ({legend_all_users} | {legend_group_only})",
        f"• Награды ({legend_all_users} | {legend_group_only})",
        f"• Описание ({legend_all_users} | {legend_group_only})",
        f"• Статистика ({legend_all_users} | {legend_group_only})",
        f"• Связь / Связь с разработчиком ({legend_all_users} | {legend_pm_only})",
        "",
        "<b>Команды модерации</b>",
        f"• /settings ({legend_group_admin} | {legend_anywhere})",
        f"• /verify, /unverify, /vlist ({legend_group_admin} | {legend_group_only})",
        f"• /taglist, /теглист, теглист ({legend_group_admin} | {legend_group_only})",
        f"• /settag, /removetag, /taglist ({legend_group_admin} | {legend_group_only})",
        f"• Список тегов, Выдать тег, Снять тег ({legend_group_admin} | {legend_group_only})",
        f"• /staff, /ranks, /myrank, /promote, /demote, /inactive ({legend_group_admin} | {legend_group_only})",
        f"• Повысить, Понизить, Админы ({legend_group_admin} | {legend_group_only})",
        f"• Наградить, Снять награду, Снять все награды ({legend_group_admin} | {legend_group_only})",
        f"• +Описание, -Описание ({legend_group_admin} | {legend_group_only})",
        f"• /closechat, /openchat, Закрыть чат, Открыть чат ({legend_group_admin} | {legend_group_only})",
        f"• /kick, Кик, Исключить ({legend_group_admin} | {legend_group_only})",
        f"• /pin, /spin, /npin, /unpin ({legend_group_admin} | {legend_group_only})",
        f"• Пин/Закреп/Закрепить, Анпин/Откреп/Открепить ({legend_group_admin} | {legend_group_only})",
        f"• /mute, /ban, /warn, /del ({legend_group_admin} | {legend_group_only})",
        f"• /delmute, /delban, /delwarn ({legend_group_admin} | {legend_group_only})",
        f"• /warnlist, /mutelist, /banlist ({legend_group_admin} | {legend_group_only})",
        f"• /варнлист, /мутлист, /банлист, варнлист, мутлист, банлист ({legend_group_admin} | {legend_group_only})",
        f"• /adminstats, /adminstat, /админстата ({legend_group_admin} | {legend_group_only})",
        f"• /unmute, /unban, /unwarn ({legend_group_admin} | {legend_group_only})",
        f"• Предупреждение, Ограничить, Заблокировать, Дел, Снять ограничение, Разблокировать, Снять предупреждение ({legend_group_admin} | {legend_group_only})",
    ]

    if is_dev_user:
        parts.extend([
            "",
            "<b>Команды dev-пользователей</b>",
            f"• /devverify, /devunverify, /devvlist ({legend_dev_or_verified} | {legend_pm_only})",
        ])

    if is_owner_user:
        parts.extend([
            "",
            "<b>Команды разработчика</b>",
            f"• /log, /broadcast, /sendpm ({legend_dev_only} | {legend_pm_only})",
            f"• Новые сообщения ({legend_dev_only} | {legend_pm_only})",
            f"• /botstatus ({legend_dev_only} | {legend_pm_only})",
            f"• /dbstatus, /sqlite_status, /dbmigrate, /sqlite_migrate ({legend_dev_only} | {legend_pm_only})",
            f"• /testuser, /dbg_users, /dbg_global_users, /migrate_users_to_global ({legend_dev_only} | {legend_pm_only})",
        ])

    parts.extend([
        "",
        "<b>Легенда эмодзи</b>",
        f"• {legend_anywhere} — работает везде (в группе и в ЛС)",
        f"• {legend_dev_only} — только для разработчика",
        f"• {legend_dev_or_verified} — только для dev-пользователя или разработчика",
        f"• {legend_group_admin} — только для администратора группы",
        f"• {legend_pm_only} — работает только в ЛС",
        f"• {legend_group_only} — работает только в группе",
        f"• {legend_all_users} — работает для всех пользователей",
        "",
        "<i>Если у команды несколько ограничений, они показываются так: (эмодзи | эмодзи).</i>",
    ])

    return "\n".join(parts)


def _build_start_usage_text() -> str:
    return (
        "<b>Как пользоваться командами</b>\n\n"
        "1. Пишите команду с префиксом <code>/</code>, <code>.</code>, <code>,</code> или <code>!</code>.\n"
        "2. Для действий над участником используйте reply, @username, ссылку <code>t.me/...</code> или ID.\n"
        "3. Примеры наказаний: <code>/mute @username 1h причина</code>, <code>/mute @username причина</code>, <code>/ban @username 3д флуд</code>, <code>/ban @username</code>, <code>/warn @username причина</code>, <code>/kick @username причина</code>.\n"
        "4. Временные интервалы для наказаний:\n"
        "<blockquote expandable=\"true\"><b>Единицы времени:</b>\n"
        "RU: <code>м</code>/<code>мин</code>, <code>ч</code>, <code>д</code>, <code>н</code>, <code>мес</code>, <code>г</code>\n"
        "EN: <code>m</code>, <code>h</code>, <code>d</code>, <code>w</code>, <code>mou</code>, <code>y</code></blockquote>\n"
        "5. Команды модерации работают только в группе и требуют прав у вашей должности.\n"
        "6. Приветствия, правила, очистка, лимит предупреждений и права ролей настраиваются через <code>/settings</code>.\n"
        "7. Повышение можно выдать на конкретный ранг: <code>/promote @username 5</code> или <code>Повысить @username 5</code>.\n"
        "8. Если команда не сработала, проверьте права должности и права бота в чате."
    )


def _build_start_about_text() -> str:
    return (
        "<b>О боте</b>\n\n"
        "Бот создан для помощи администраторам в группах.\n\n"
        "<b>Что умеет:</b>\n"
        "• Профили участников, описания, награды и теги.\n"
        "• Гибкие роли с правами по действиям.\n"
        "• Ограничения, блокировки, предупреждения, снятие наказаний и списки по каждому типу.\n"
        "• Закрытие/открытие чата и работа с закрепами.\n"
        "• Настраиваемые приветствия/сообщения о выходах/правила.\n"
        "• Очистка системных сообщений и команд.\n"
        "• Статистика сообщений по группам.\n\n"
        "<i>Нужна доработка под ваш формат сообщества: используйте раздел связи с разработчиком.</i>"
    )


def _broadcast_new_draft() -> dict:
    return {
        "id": random.randint(100000, 999999),
        "text_custom": "",
        "source": "plain",
        "entities": [],
        "media": [],
        "buttons": {"rows": [], "popups": []},
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def _broadcast_get_or_create_draft(user_id: int) -> dict:
    draft = BROADCAST_DRAFTS.get(user_id)
    if not isinstance(draft, dict):
        draft = _broadcast_new_draft()
        BROADCAST_DRAFTS[user_id] = draft
    return draft


def _broadcast_render_panel_text(user_id: int) -> str:
    draft = _broadcast_get_or_create_draft(user_id)
    has_text = bool((draft.get("text_custom") or "").strip())
    has_media = bool(draft.get("media") or [])
    has_buttons = bool(((draft.get("buttons") or {}).get("rows") or []))

    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    emoji_broadcast = f'<tg-emoji emoji-id="{EMOJI_LOG_PM_ID}">📢</tg-emoji>'

    text_flag = emoji_ok if has_text else emoji_x
    media_flag = emoji_ok if has_media else emoji_x
    btn_flag = emoji_ok if has_buttons else emoji_x

    return (
        f"{emoji_broadcast} <b>Рассылка</b>\n\n"
        "Настройте черновик и отправьте его всем пользователям из базы.\n"
        "<blockquote expandable=\"true\">"
        "Поддерживается: текст, медиа, кнопки (URL/Popup/Rules/Del).\n"
        "Формат кнопок: <code>Текст - ссылка</code> или <code>Текст - popup: сообщение</code>."
        "</blockquote>\n"
        f"<b>Текст:</b> {text_flag}\n"
        f"<b>Медиа:</b> {media_flag}\n"
        f"<b>Кнопки:</b> {btn_flag}"
    )


def _build_broadcast_panel_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)

    kb.row(
        InlineKeyboardButton("Текст", callback_data=f"bc2:text:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_TEXT_ID)),
        InlineKeyboardButton("Медиа", callback_data=f"bc2:media:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_MEDIA_ID)),
    )
    kb.row(InlineKeyboardButton("Кнопки", callback_data=f"bc2:buttons:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_BUTTONS_ID)))
    kb.row(
        InlineKeyboardButton("Текущее сообщение", callback_data=f"bc2:preview:{draft_id}"),
        InlineKeyboardButton("Сбросить", callback_data=f"bc2:reset:{draft_id}", icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_CANCEL_ID)),
    )
    kb.row(
        InlineKeyboardButton("Подтвердить отправку", callback_data=f"bc2:send:{draft_id}", icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_SAVE_ID)),
        InlineKeyboardButton("Закрыть", callback_data=f"bc2:cancel:{draft_id}", icon_custom_emoji_id=str(PREMIUM_CLOSE_EMOJI_ID)),
    )
    return kb


def _broadcast_collect_targets() -> list[int]:
    targets: set[int] = set()

    for uid in STATS.get('users') or set():
        try:
            value = int(uid)
            if value > 0:
                targets.add(value)
        except Exception:
            continue

    for uid in (GLOBAL_USERS or {}).keys():
        try:
            value = int(uid)
            if value > 0:
                targets.add(value)
        except Exception:
            continue

    return sorted(targets)


def _broadcast_send_payload_once(user_id: int, html_text: str, media: list, buttons: dict) -> None:
    try:
        from settings_ui import build_inline_keyboard_for_payload, _send_payload
        rows = (buttons or {}).get("rows") or []
        popups = (buttons or {}).get("popups") or []
        kb = build_inline_keyboard_for_payload("broadcast", user_id, rows, popups, user_id)
        _send_payload(user_id, html_text, media or [], reply_markup=kb)
    except Exception as send_err:
        plain = _html.unescape(_re.sub(r"<[^>]+>", "", html_text or ""))
        if plain.strip():
            bot.send_message(user_id, plain, disable_web_page_preview=True)
            return
        bot.send_message(user_id, "Рассылка", disable_web_page_preview=True)


def _broadcast_send_payload(user_id: int, html_text: str, media: list, buttons: dict) -> tuple[bool, str | None]:
    try:
        _broadcast_send_payload_once(user_id, html_text, media, buttons)
        return True, None
    except Exception as send_err:
        return False, str(send_err)


# ==== ОТПРАВКА В ЛС (sendpm) ====

def _sendpm_new_draft(target_user_id: int, target_name: str, target_username: str) -> dict:
    return {
        "id": random.randint(100000, 999999),
        "target_user_id": target_user_id,
        "target_name": target_name,
        "target_username": target_username,
        "text_custom": "",
        "source": "plain",
        "entities": [],
        "media": [],
        "buttons": {"rows": [], "popups": []},
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }


def _sendpm_render_panel_text(owner_id: int) -> str:
    draft = SENDPM_DRAFTS.get(owner_id)
    if not isinstance(draft, dict):
        return premium_prefix("Черновик не найден.")

    has_text = bool((draft.get("text_custom") or "").strip())
    has_media = bool(draft.get("media") or [])
    has_buttons = bool(((draft.get("buttons") or {}).get("rows") or []))

    emoji_ok = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    emoji_x = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    emoji_pm = f'<tg-emoji emoji-id="{EMOJI_SCOPE_PM_ID}">💬</tg-emoji>'

    text_flag = emoji_ok if has_text else emoji_x
    media_flag = emoji_ok if has_media else emoji_x
    btn_flag = emoji_ok if has_buttons else emoji_x

    target_name = _html.escape(draft.get("target_name") or "Пользователь")
    target_username = draft.get("target_username") or ""
    target_uid = int(draft.get("target_user_id") or 0)
    mention = mention_html_by_id(target_uid, target_name) if target_uid > 0 else target_name
    username_part = f" (@{_html.escape(target_username)})" if target_username else ""

    return (
        f"{emoji_pm} <b>Личное сообщение</b>\n\n"
        f"<b>Получатель:</b> {mention}{username_part}\n\n"
        "Настройте сообщение и отправьте его выбранному пользователю.\n"
        "<blockquote expandable=\"true\">"
        "Поддерживается: текст, медиа, кнопки (URL/Popup).\n"
        "Формат кнопок: <code>Текст - ссылка</code> или <code>Текст - popup: сообщение</code>."
        "</blockquote>\n"
        f"<b>Текст:</b> {text_flag}\n"
        f"<b>Медиа:</b> {media_flag}\n"
        f"<b>Кнопки:</b> {btn_flag}"
    )


def _build_sendpm_panel_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("Текст", callback_data=f"pm2:text:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_TEXT_ID)),
        InlineKeyboardButton("Медиа", callback_data=f"pm2:media:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_MEDIA_ID)),
    )
    kb.row(InlineKeyboardButton("Кнопки", callback_data=f"pm2:buttons:{draft_id}", icon_custom_emoji_id=str(EMOJI_WELCOME_BUTTONS_ID)))
    kb.row(
        InlineKeyboardButton("Предпросмотр", callback_data=f"pm2:preview:{draft_id}"),
        InlineKeyboardButton("Сбросить текст", callback_data=f"pm2:reset:{draft_id}", icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_CANCEL_ID)),
    )
    kb.row(
        InlineKeyboardButton("Отправить", callback_data=f"pm2:send:{draft_id}", icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_SAVE_ID)),
        InlineKeyboardButton("Закрыть", callback_data=f"pm2:cancel:{draft_id}", icon_custom_emoji_id=str(PREMIUM_CLOSE_EMOJI_ID)),
    )
    return kb


def _sendpm_reply_keyboard() -> types.InlineKeyboardMarkup:
    """Inline keyboard attached to a message sent to a user via sendpm / owner reply."""
    kb = types.InlineKeyboardMarkup()
    btn_reply = types.InlineKeyboardButton("Ответить", callback_data="devcontact:reply")
    btn_reply.icon_custom_emoji_id = str(EMOJI_REPLY_BTN_ID)
    kb.row(btn_reply)
    return kb


def _send_start_menu(chat_id: int, user: telebot.types.User):
    show_owner_button = is_owner(user)

    sent = bot.send_message(
        chat_id,
        _build_start_home_text(user),
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=_build_start_home_keyboard(show_owner_button=show_owner_button),
    )

    START_MENU_STATE[(chat_id, sent.message_id)] = {
        "user_id": user.id,
        "show_owner_button": show_owner_button,
    }

@bot.message_handler(func=lambda m: match_command(m.text, 'start'))
def cmd_start(m: types.Message):
    add_stat_message(m)
    add_stat_command('start')
    _remember_owner_user_id(m.from_user)

    if m.chat.type != 'private':
        return

    _send_start_menu(m.chat.id, m.from_user)


@bot.message_handler(func=lambda m: m.chat.type == 'private' and (
    text_starts_with_ci(m.text, 'связь с разработчиком') or text_starts_with_ci(m.text, 'связь')
))
def cmd_contact_developer(m: types.Message):
    add_stat_message(m)
    if is_owner(m.from_user):
        _remember_owner_user_id(m.from_user)

    bot.send_message(
        m.chat.id,
        _dev_contact_intro_text(),
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=_dev_contact_intro_kb(),
    )


@bot.message_handler(func=lambda m: m.chat.type == 'private' and text_starts_with_ci(m.text, 'новые сообщения'))
def cmd_dev_new_messages(m: types.Message):
    add_stat_message(m)

    if not is_owner(m.from_user):
        return deny_access(m.chat.id)

    _remember_owner_user_id(m.from_user)
    _show_dev_contact_new_messages(m.chat.id)


@bot.message_handler(func=lambda m: m.chat.type == 'private', content_types=['text'])
def handle_dev_contact_text_input(m: types.Message):
    if is_owner(m.from_user):
        _remember_owner_user_id(m.from_user)

    user_id = m.from_user.id
    text = (m.text or '').strip()

    pending_user = PENDING_DEV_CONTACT_FROM_USER.get(user_id)
    if pending_user is not None:
        if not text:
            bot.send_message(
                m.chat.id,
                premium_prefix("Сообщение не должно быть пустым. Отправьте текст."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return

        PENDING_DEV_CONTACT_FROM_USER.pop(user_id, None)

        prompt_msg_id = int(pending_user.get('prompt_message_id') or 0)
        if prompt_msg_id > 0:
            try:
                bot.delete_message(m.chat.id, prompt_msg_id)
            except Exception:
                pass

        item = _append_dev_contact_item(m.from_user, text)
        _send_dev_contact_item_to_owner(item)

        emoji_ok = f'<tg-emoji emoji-id="{EMOJI_SENT_OK_ID}">✅</tg-emoji>'
        bot.send_message(
            m.chat.id,
            f"<i>{emoji_ok} Сообщение отправлено!</i>",
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        return

    pending_owner = PENDING_DEV_REPLY_FROM_OWNER.get(user_id)
    if pending_owner is not None and is_owner(m.from_user):
        if not text:
            bot.send_message(
                m.chat.id,
                premium_prefix("Ответ не должен быть пустым. Отправьте текст."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
            return

        PENDING_DEV_REPLY_FROM_OWNER.pop(user_id, None)

        prompt_msg_id = int(pending_owner.get('prompt_message_id') or 0)
        if prompt_msg_id > 0:
            try:
                bot.delete_message(m.chat.id, prompt_msg_id)
            except Exception:
                pass

        item_id = int(pending_owner.get('item_id') or 0)
        target_user_id = int(pending_owner.get('target_user_id') or 0)
        item = _dev_contact_find_item(item_id)

        emoji_reply = f'<tg-emoji emoji-id="{EMOJI_REPLY_RECEIVED_ID}">💬</tg-emoji>'
        payload = (
            f"{emoji_reply} <b>Получен ответ от разработчика...</b>\n\n"
            "<b>Текст сообщения:</b>\n"
            f"<blockquote>{_html.escape(text)}</blockquote>"
        )

        sent_ok = False
        if target_user_id > 0:
            try:
                bot.send_message(
                    target_user_id,
                    payload,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                    reply_markup=_sendpm_reply_keyboard(),
                )
                sent_ok = True
            except Exception:
                sent_ok = False

        if item is not None:
            item['status'] = 'replied' if sent_ok else 'new'
            item['reply_text'] = text if sent_ok else ''
            item['replied_at'] = int(time.time()) if sent_ok else 0
            save_dev_contact_inbox()

        if sent_ok:
            emoji_ok = f'<tg-emoji emoji-id="{EMOJI_SENT_OK_ID}">✅</tg-emoji>'
            bot.send_message(m.chat.id, f"<i>{emoji_ok} Ответ отправлен.</i>", parse_mode='HTML')
        else:
            bot.send_message(m.chat.id, premium_prefix("Не удалось отправить ответ пользователю."), parse_mode='HTML')
        return

    return ContinueHandling()


@bot.message_handler(func=lambda m: m.chat.type == 'private', content_types=['photo', 'video', 'document', 'audio', 'animation', 'voice', 'video_note', 'sticker'])
def handle_dev_contact_non_text_input(m: types.Message):
    if is_owner(m.from_user):
        _remember_owner_user_id(m.from_user)

    user_id = m.from_user.id

    if user_id in PENDING_DEV_CONTACT_FROM_USER:
        bot.send_message(
            m.chat.id,
            premium_prefix("Нужно отправить именно текстовое сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        return

    if user_id in PENDING_DEV_REPLY_FROM_OWNER and is_owner(m.from_user):
        bot.send_message(
            m.chat.id,
            premium_prefix("Нужно отправить именно текстовое сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        return

    return ContinueHandling()


@bot.message_handler(func=lambda m: match_command_aliases(m.text, ['ping', 'пинг']))
def cmd_ping(m: types.Message):
    add_stat_message(m)
    add_stat_command('ping')

    # Проверка одобрения группы
    if m.chat.type in ['group', 'supergroup']:
        if not is_group_approved(m.chat.id):
            return bot.reply_to(
                m,
                "Бот находится на модерации. Ожидание подтверждения от разработчика.",
                parse_mode='HTML'
            )

    # Кулдаун: 30 сек для всех, кроме разработчика
    if not is_dev(m.from_user):
        wait_seconds = cooldown_hit('user', int(m.from_user.id), 'ping', 30)
        if wait_seconds > 0:
            return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='ping')

    t_start = time.perf_counter()
    sent = bot.send_message(
        m.chat.id,
        f'<tg-emoji emoji-id="{EMOJI_PING_ID}">🏓</tg-emoji> Проверка пинга…',
        parse_mode='HTML'
    )
    t_send_end = time.perf_counter()
    send_latency_ms = int((t_send_end - t_start) * 1000)

    t_edit_start = time.perf_counter()
    try:
        # Используем текст, отличный от исходного, чтобы Telegram не отклонил
        # правку как «сообщение не изменено» и замер задержки был корректным.
        bot.edit_message_text(
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            text=f'<tg-emoji emoji-id="{EMOJI_PING_ID}">🏓</tg-emoji> Измерение…',
            parse_mode='HTML'
        )
    except Exception:
        pass
    t_edit_end = time.perf_counter()
    edit_latency_ms = int((t_edit_end - t_edit_start) * 1000)

    total_ms = send_latency_ms + edit_latency_ms
    emoji_ping = f'<tg-emoji emoji-id="{EMOJI_PING_ID}">🏓</tg-emoji>'

    if not is_dev(m.from_user):
        text = f"{emoji_ping} <b>Пинг:</b> <code>{total_ms} мс</code>"
    else:
        uptime_text = get_uptime_text()
        lines = [
            f"{emoji_ping} <b>Пинг</b>",
            f"<b>Отправка сообщения:</b> <code>{send_latency_ms} мс</code>",
            f"<b>Изменение сообщения:</b> <code>{edit_latency_ms} мс</code>",
            f"<b>Итого:</b> <code>{total_ms} мс</code>",
            f"<b>Uptime:</b> <code>{uptime_text}</code>",
        ]

        if is_owner(m.from_user):
            try:
                proc = psutil.Process(os.getpid())
                mem_text = format_bytes_mb(proc.memory_info().rss)
                vm = psutil.virtual_memory()
                ram_system_text = f"{int(vm.used / (1024 * 1024))}/{int(vm.total / (1024 * 1024))} MB"
            except Exception:
                mem_text = "не удалось узнать"
                ram_system_text = "не удалось узнать"

            lines.extend([
                f"<b>RAM процесса:</b> <code>{mem_text}</code>",
                f"<b>RAM системы:</b> <code>{ram_system_text}</code>",
            ])

        text = "\n".join(lines)

    bot.edit_message_text(
        chat_id=sent.chat.id,
        message_id=sent.message_id,
        text=text,
        parse_mode='HTML'
    )


@bot.message_handler(func=lambda m: match_command(m.text, 'log'))
def cmd_log(m: types.Message):
    add_stat_message(m)
    add_stat_command('log')

    if not is_owner(m.from_user):
        if m.chat.type == 'private':
            return deny_access(m.chat.id)
        return

    try:
        proc = psutil.Process(os.getpid())
        proc_ram = format_bytes_mb(proc.memory_info().rss)
        proc_cpu = proc.cpu_percent(interval=0.1)
        proc_threads = proc.num_threads()
        system_cpu = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        system_ram = f"{int(vm.used / (1024 * 1024))}/{int(vm.total / (1024 * 1024))} MB"
    except Exception:
        proc_ram = "n/a"
        proc_cpu = -1
        proc_threads = -1
        system_cpu = -1
        system_ram = "n/a"

    sorted_cmds = sorted((STATS.get('commands_used') or {}).items(), key=lambda x: x[1], reverse=True)
    top_lines = [f"/{cmd}: <code>{cnt}</code>" for cmd, cnt in sorted_cmds[:20]]
    if not top_lines:
        top_lines = ["Нет данных."]

    now_dt = datetime.now()
    started_dt = datetime.fromtimestamp(float(STATS.get('start_time') or time.time()))

    # Считаем только группы/супергруппы (id < 0) из известных БД бота.
    known_group_ids: set[int] = set()
    for store in (CHAT_SETTINGS, GROUP_SETTINGS, GROUP_STATS, MODERATION, PENDING_GROUPS):
        for cid_raw in (store or {}).keys():
            try:
                cid = int(str(cid_raw))
            except Exception:
                continue
            if cid < 0:
                known_group_ids.add(cid)

    users_in_db = len(GLOBAL_USERS or {})

    text = (
        f'<tg-emoji emoji-id="{EMOJI_LOG_ID}">📋</tg-emoji> <b>Лог бота</b>\n'
        f"<i>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</i>\n\n"
        "<b>Основное:</b>\n"
        f"• Время работы: <code>{get_uptime_text()}</code>\n"
        f"• Нынешнее время: <code>{now_dt.strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
        f"• Время запуска: <code>{started_dt.strftime('%d.%m.%Y %H:%M:%S')}</code>\n"
        f"• Пользователей в БД: <code>{users_in_db}</code>\n"
        f"• Групп где есть бот: <code>{len(known_group_ids)}</code>\n"
        f"• Уникальных пользователей (runtime): <code>{len(STATS.get('users') or set())}</code>\n"
        f"• Чатов (runtime): <code>{len(STATS.get('chats') or set())}</code>\n"
        f"• Сообщений: <code>{STATS.get('messages', 0)}</code>\n\n"
        "<b>VPS:</b>\n"
        f"• CPU процесса: <code>{proc_cpu:.1f}%</code>\n"
        f"• CPU системы: <code>{system_cpu:.1f}%</code>\n"
        f"• RAM процесса: <code>{proc_ram}</code>\n"
        f"• RAM системы: <code>{system_ram}</code>\n"
        f"• Потоков процесса: <code>{proc_threads}</code>\n\n"
        "<b>Топ команд:</b>\n"
        + "\n".join(top_lines)
    )

    try:
        bot.send_message(m.from_user.id, text, parse_mode='HTML', disable_web_page_preview=True)
        if m.chat.id != m.from_user.id:
            bot.reply_to(m, "Лог отправлен тебе в личку.")
    except Exception as e:
        bot.reply_to(m, premium_prefix(f"Не удалось отправить лог в ЛС: <code>{e}</code>"))


@bot.message_handler(func=lambda m: match_command(m.text, 'broadcast'))
def cmd_broadcast(m: types.Message):
    add_stat_message(m)
    add_stat_command('broadcast')

    if not is_owner(m.from_user):
        if m.chat.type == 'private':
            return deny_access(m.chat.id)
        return
    if m.chat.type != 'private':
        return

    pending = BROADCAST_PENDING_INPUT.pop(m.from_user.id, None)
    if pending:
        try:
            old_prompt_id = int(pending.get("prompt_message_id") or 0)
            if old_prompt_id > 0:
                bot.delete_message(m.chat.id, old_prompt_id)
        except Exception:
            pass

    draft = _broadcast_get_or_create_draft(m.from_user.id)

    parts = (m.text or "").split(' ', 1)
    if len(parts) > 1 and parts[1].strip():
        from settings_ui import convert_section_text_from_message
        text_custom, source, entities_ser = convert_section_text_from_message(m)
        draft["text_custom"] = text_custom
        draft["source"] = source
        draft["entities"] = entities_ser
        draft["updated_at"] = int(time.time())
        BROADCAST_DRAFTS[m.from_user.id] = draft

    panel_text = _broadcast_render_panel_text(m.from_user.id)
    kb = _build_broadcast_panel_keyboard(int(draft.get("id") or 0))
    bot.send_message(
        m.chat.id,
        panel_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=kb,
    )


@bot.message_handler(func=lambda m: match_command(m.text, 'sendpm'))
def cmd_sendpm(m: types.Message):
    add_stat_message(m)
    add_stat_command('sendpm')

    if not is_owner(m.from_user):
        if m.chat.type == 'private':
            return deny_access(m.chat.id)
        return
    if m.chat.type != 'private':
        return

    _remember_owner_user_id(m.from_user)

    parts = (m.text or "").split(None, 1)
    raw_target = parts[1].strip() if len(parts) > 1 else ""

    if not raw_target:
        bot.send_message(
            m.chat.id,
            premium_prefix(
                "Укажите пользователя: <code>/sendpm @username</code> или <code>/sendpm user_id</code>"
            ),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        return

    target_user_id: int | None = None
    target_name = ""
    target_username = ""

    # Resolve by ID or @username
    if raw_target.lstrip('-').isdigit():
        target_user_id = int(raw_target)
        try:
            chat_obj = bot.get_chat(target_user_id)
            target_name = (getattr(chat_obj, "first_name", "") or "").strip()
            ln = (getattr(chat_obj, "last_name", "") or "").strip()
            if ln:
                target_name = f"{target_name} {ln}".strip()
            target_username = (getattr(chat_obj, "username", "") or "").strip()
        except Exception:
            target_name = str(target_user_id)
    else:
        uname = raw_target.lstrip("@")
        try:
            chat_obj = bot.get_chat(f"@{uname}")
            target_user_id = int(getattr(chat_obj, "id", 0) or 0)
            target_name = (getattr(chat_obj, "first_name", "") or "").strip()
            ln = (getattr(chat_obj, "last_name", "") or "").strip()
            if ln:
                target_name = f"{target_name} {ln}".strip()
            target_username = (getattr(chat_obj, "username", "") or "").strip()
        except Exception:
            # Try mtproto as fallback
            try:
                loop = asyncio.new_event_loop()
                target_user_id = loop.run_until_complete(get_user_id_by_username_mtproto(uname))
                loop.close()
            except Exception:
                target_user_id = None
            target_name = uname
            target_username = uname

    if not target_user_id or target_user_id <= 0:
        bot.send_message(
            m.chat.id,
            premium_prefix("Пользователь не найден. Проверьте правильность username или ID."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
        return

    if not target_name:
        target_name = str(target_user_id)

    # Cancel any pending input and create fresh draft
    SENDPM_PENDING_INPUT.pop(m.from_user.id, None)
    SENDPM_DRAFTS[m.from_user.id] = _sendpm_new_draft(target_user_id, target_name, target_username)
    draft = SENDPM_DRAFTS[m.from_user.id]

    panel_text = _sendpm_render_panel_text(m.from_user.id)
    kb = _build_sendpm_panel_keyboard(int(draft.get("id") or 0))
    bot.send_message(
        m.chat.id,
        panel_text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=kb,
    )



def get_user_msg_count_in_chat(chat: types.Chat, user_id: int) -> int:
    chat_id = str(chat.id)
    chat_stats = GROUP_STATS.get(chat_id, {})
    u = chat_stats.get(str(user_id), {})
    return int(u.get("count", 0))


def get_user_photo_file_id(user_id: int) -> str | None:
    # Проверяем кеш
    now = time.monotonic()
    with _PHOTO_CACHE_LOCK:
        cached = _PHOTO_FILE_ID_CACHE.get(user_id)
        if cached and (now - cached[0]) < _PHOTO_CACHE_TTL:
            return cached[1]

    file_id: str | None = None
    try:
        # Основной способ: getUserProfilePhotos возвращает PhotoSize.file_id,
        # который можно использовать в sendPhoto.
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            sizes = photos.photos[0]
            largest = max(sizes, key=lambda s: s.file_size or 0)
            file_id = largest.file_id
    except Exception as e:
        print(f"Не удалось получить фото профиля {user_id}: {e}")

    if file_id is None:
        try:
            # Запасной вариант: big_file_id из ChatPhoto (getChat).
            # Работает только пока фото не сменилось.
            chat_obj = tg_get_user_by_id_cached(user_id)
            chat_photo = getattr(chat_obj, "photo", None)
            big_file_id = getattr(chat_photo, "big_file_id", None) if chat_photo else None
            if big_file_id:
                file_id = big_file_id
        except Exception:
            pass

    # Сохраняем в кеш (даже None — чтобы не повторять запросы)
    with _PHOTO_CACHE_LOCK:
        _PHOTO_FILE_ID_CACHE[user_id] = (time.monotonic(), file_id)
    return file_id


def _mod_has_active_punishment(chat_id: int, user_id: int, kind: str) -> bool:
    from moderation import _mod_cleanup_expired, _mod_get_chat
    _mod_cleanup_expired(chat_id)
    ch = _mod_get_chat(chat_id)
    active_map = (ch.get("active") or {}).get(kind) or {}
    return str(user_id) in active_map


def build_profile_text(chat: types.Chat, target: telebot.types.User) -> str:
    uid = target.id
    profile = get_profile(chat.id, uid)
    msg_count = get_user_msg_count_in_chat(chat, uid)
    today_ts = int(time.time()) - 86400
    from persistence import get_user_msg_count_for_period
    today_count = get_user_msg_count_for_period(chat.id, uid, today_ts)

    name = link_for_user(chat.id, uid)

    text = (
        f'<tg-emoji emoji-id="{EMOJI_PROFILE_ID}">👤</tg-emoji> '
        f"<b>Профиль пользователя</b> {name} [<code>{uid}</code>]\n\n"
    )

    # === Дата вступления ===
    joined_date_str = ""
    try:
        member = tg_get_chat_member(chat.id, uid)
        jd = getattr(member, 'joined_date', None) or getattr(member, 'date', None)
        if jd and int(jd) > 0:
            import datetime as _dt_mod
            joined_date_str = _dt_mod.datetime.utcfromtimestamp(int(jd)).strftime("%d.%m.%Y")
    except Exception:
        pass

    if joined_date_str:
        text += (
            f'<tg-emoji emoji-id="5431897022456145283">📅</tg-emoji> '
            f"<b>Дата вступления:</b> <code>{joined_date_str}</code>\n\n"
        )

    text += (
        f'<tg-emoji emoji-id="{EMOJI_MSG_COUNT_ID}">💬</tg-emoji> '
        f"<b>Количество сообщений:</b>\n"
        f"За всё время: <code>{msg_count}</code>\n"
        f"За день: <code>{today_count}</code>\n\n"
    )

    # === Статус пользователя ===
    status_lines: list[str] = []
    punish_emoji = f'<tg-emoji emoji-id="{EMOJI_PUNISHMENT_ID}">⚠️</tg-emoji>'
    member_status = None

    if is_owner(target):
        status_lines.append(
            f'<tg-emoji emoji-id="{EMOJI_DEV_ID}">💻</tg-emoji> Разработчик бота'
        )

    if chat.type in ['group', 'supergroup']:
        try:
            member = tg_get_chat_member(chat.id, uid)
            member_status = member.status
            if member_status in ("administrator", "creator"):
                status_lines.append(
                    f'<tg-emoji emoji-id="{EMOJI_ADMIN_ID}">⭐</tg-emoji> Администратор чата'
                )
            elif member_status in ("member", "restricted"):
                status_lines.append(
                    f'<tg-emoji emoji-id="{EMOJI_MEMBER_ID}">⭐</tg-emoji> Участник чата'
                )
        except Exception:
            pass

        # Один вызов cleanup вместо двух (E: оптимизация)
        from moderation import _mod_cleanup_expired, _mod_get_chat as _mgc
        _mod_cleanup_expired(chat.id)
        _ch_active = (_mgc(chat.id).get("active") or {})
        if str(uid) in (_ch_active.get("mute") or {}):
            status_lines.append(f"{punish_emoji} Ограничен")
        if str(uid) in (_ch_active.get("ban") or {}):
            status_lines.append(f"{punish_emoji} Заблокирован")
        elif member_status in ("left", "kicked"):
            status_lines.append(f"{punish_emoji} Не участник группы")

    if not status_lines:
        status_lines.append("Неизвестен")

    # Премиум
    is_premium = getattr(target, "is_premium", False)
    if is_premium:
        status_lines.append(
            f'<tg-emoji emoji-id="{EMOJI_PREMIUM_STATUS_ID}">⭐</tg-emoji> Премиум пользователь'
        )

    # Верификация разработчиком (глобальная)
    if uid in VERIFY_DEV:
        status_lines.append(
            f'<tg-emoji emoji-id="{EMOJI_VERIFY_DEV_ID}">✅</tg-emoji> Верифицирован разработчиком'
        )

    # Верификация админом (локальная для этого чата)
    chat_id_s = str(chat.id)
    if uid in (VERIFY_ADMINS.get(chat_id_s) or []):
        status_lines.append(
            f'<tg-emoji emoji-id="{EMOJI_VERIFY_ADMIN_ID}">✅</tg-emoji> Верифицирован администратором'
        )

    text += "<b>Статус пользователя:</b>\n" + "\n".join(status_lines) + "\n\n"

    rank = get_user_rank(chat.id, uid)
    rank_html = get_rank_label_html(rank)
    if rank_html:
        text += f"<b>Должность:</b>\n{rank_html}\n\n"

    user_tag = get_user_custom_tag(chat.id, uid)
    if user_tag:
        is_admin_tag = False
        try:
            m_obj = tg_get_chat_member(chat.id, uid)
            is_admin_tag = getattr(m_obj, 'status', '') in ('administrator', 'creator')
        except Exception:
            pass
        tag_type_label = "Администраторский тег" if is_admin_tag else "Пользовательский тег"
        text += (
            f"<b>Тег:</b>\n"
            f"<code>{_html.escape(user_tag)}</code>\n"
            f"<i>{tag_type_label}</i>\n\n"
        )

    return text


def build_profile_awards_text(chat_id: int, target_id: int) -> str:
    profile = get_profile(chat_id, target_id)
    awards = profile["awards"]
    name = link_for_user(chat_id, target_id)

    lines = [
        f'<tg-emoji emoji-id="{EMOJI_AWARDS_BLOCK_ID}">🏅</tg-emoji> '
        f"<b>Награды пользователя</b> {name} [<code>{target_id}</code>]",
    ]

    if not awards:
        lines.extend([
            "",
            '<blockquote expandable="true">Наград нет.</blockquote>',
        ])
    else:
        quote_lines: list[str] = []
        for idx, a in enumerate(awards[:10], start=1):
            quote_lines.append(f"<b>[{idx}] Награда</b>")
            quote_lines.append(f"{a}")
            if idx < len(awards[:10]):
                quote_lines.append("")
        quote_body = "\n".join(quote_lines)
        lines.extend([
            "",
            f'<blockquote expandable="true">{quote_body}</blockquote>',
        ])

    return "\n".join(lines)


def build_profile_description_text(chat_id: int, target_id: int) -> str:
    profile = get_profile(chat_id, target_id)
    name = link_for_user(chat_id, target_id)
    desc = profile.get("description") or "Описание не задано."

    return (
        f'<tg-emoji emoji-id="{EMOJI_DESC_ID}">📝</tg-emoji> '
        f"<b>Описание пользователя</b> {name} [<code>{target_id}</code>]\n\n"
        f'<blockquote expandable="true">{_html.escape(desc)}</blockquote>'
    )


def build_profile_keyboard(chat_id: int, target_id: int, viewer_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "Награды",
            callback_data=f"profile:awards:{chat_id}:{target_id}:{viewer_id}",
            icon_custom_emoji_id=str(EMOJI_AWARDS_BLOCK_ID),
        ),
        InlineKeyboardButton(
            "Описание",
            callback_data=f"profile:description:{chat_id}:{target_id}:{viewer_id}",
            icon_custom_emoji_id=str(EMOJI_DESC_ID),
        ),
    )
    kb.row(
        InlineKeyboardButton(
            "Закрыть",
            callback_data=f"profile:close:{chat_id}:{target_id}:{viewer_id}",
            icon_custom_emoji_id=str(PREMIUM_CLOSE_EMOJI_ID),
        )
    )
    return kb

@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "профиль"))
def cmd_profile(m: types.Message):
    add_stat_message(m)
    add_stat_command('profile')

    wait_seconds = cooldown_hit('user', int(m.from_user.id), 'profile', 5)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='profile')

    if m.chat.type == 'private':
        return bot.reply_to(m, premium_prefix("Эта команда работает только в группах."))

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    # если есть reply + что-то дописано рядом с командой — игнорируем
    if m.reply_to_message and args:
        return

    # если есть reply → берём пользователя из reply
    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        # нет аргументов → профиль самого отправителя
        if not args:
            target_id = m.from_user.id
        else:
            first = args[0]
            # аргумент должен быть id / @ / t.me
            if first.isdigit() or first.startswith("@") or first.startswith("t.me/") or "t.me/" in first:
                target_id = parse_target_user(m, args)
                if target_id is None:
                    return bot.reply_to(
                        m,
                        premium_prefix(
                            "Не удалось определить пользователя для профиля.\n"
                            "Если используется @username, убедись, что пользователь уже писал в этот чат или боту в ЛС."
                        )
                    )
            else:
                # после команды текст, но не указатель — молчим
                return

    try:
        member = tg_get_chat_member(m.chat.id, target_id)
        target_user = member.user
    except Exception:
        target_user = tg_get_user_by_id_cached(target_id)

    # Проверяем кеш профиля (A)
    _cache_key = (m.chat.id, target_user.id)
    _now_ts = time.monotonic()
    caption: str | None = None
    photo_id: str | None = None
    with _PROFILE_CACHE_LOCK:
        _cached = _PROFILE_RESULT_CACHE.get(_cache_key)
        if _cached and (_now_ts - _cached[0]) < _PROFILE_CACHE_TTL:
            caption, photo_id = _cached[1], _cached[2]

    if caption is None:
        # Кеш-промах: параллельно строим текст и получаем фото (B)
        _ft = _PROFILE_EXECUTOR.submit(build_profile_text, m.chat, target_user)
        _fp = _PROFILE_EXECUTOR.submit(get_user_photo_file_id, target_user.id)
        try:
            caption = _ft.result(timeout=15)
        except Exception as _e:
            print(f"[PROFILE] Ошибка построения текста профиля uid={target_user.id} chat={m.chat.id}: {_e}")
            caption = ""
        try:
            photo_id = _fp.result(timeout=10)
        except Exception as _e:
            print(f"[PROFILE] Ошибка получения фото профиля uid={target_user.id}: {_e}")
            photo_id = None
        # Сохраняем результат в кеш (только если caption не пустой)
        if caption:
            with _PROFILE_CACHE_LOCK:
                _PROFILE_RESULT_CACHE[_cache_key] = (time.monotonic(), caption, photo_id)

    kb = build_profile_keyboard(m.chat.id, target_user.id, m.from_user.id)

    if photo_id:
        try:
            bot.send_photo(
                m.chat.id,
                photo_id,
                caption=caption,
                parse_mode='HTML',
                reply_to_message_id=m.message_id,
                reply_markup=kb,
            )
            return
        except Exception:
            # Сброс кешей при протухшем file_id (G)
            with _PHOTO_CACHE_LOCK:
                _PHOTO_FILE_ID_CACHE.pop(target_user.id, None)
            _invalidate_profile_cache(m.chat.id, target_user.id)

    bot.send_message(
        m.chat.id,
        caption,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_to_message_id=m.message_id,
        reply_markup=kb,
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("profile:"))
def cb_profile_view(c: types.CallbackQuery):
    def _safe_ack(*args, **kwargs):
        try:
            bot.answer_callback_query(*args, **kwargs)
        except Exception:
            pass

    if _is_duplicate_callback_query(c):
        return
    try:
        _, action, chat_s, target_s, viewer_s = c.data.split(":", 4)
        chat_id = int(chat_s)
        target_id = int(target_s)
        viewer_id = int(viewer_s)
    except Exception:
        _safe_ack(c.id)
        return

    if c.from_user.id != viewer_id:
        _safe_ack(c.id, "Эти кнопки доступны только тому, кто вызвал профиль.", show_alert=True)
        return

    if action == "close":
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        _safe_ack(c.id)
        return

    if action == "awards":
        text = build_profile_awards_text(chat_id, target_id)
    elif action == "description":
        text = build_profile_description_text(chat_id, target_id)
    else:
        _safe_ack(c.id)
        return

    try:
        kb = build_profile_keyboard(chat_id, target_id, viewer_id)
        if (c.message.content_type or "") == "photo":
            bot.edit_message_caption(
                caption=text,
                chat_id=c.message.chat.id,
                message_id=c.message.message_id,
                parse_mode='HTML',
                reply_markup=kb,
            )
        else:
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

    _safe_ack(c.id)
    return

def ensure_user_in_users(m: types.Message):
    chat_id = str(m.chat.id)
    u = m.from_user
    if chat_id not in USERS:
        USERS[chat_id] = {}
    chat_users = USERS[chat_id]
    uid = str(u.id)
    chat_users[uid] = {
        "id": u.id,
        "username": u.username,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "is_bot": u.is_bot,
    }
    save_users()

# ==== НАГРАДЫ ==== 

def _to_utf16_units(s: str):
    b = s.encode('utf-16-le')
    return [int.from_bytes(b[i:i+2], 'little') for i in range(0, len(b), 2)]


def _from_utf16_units(units):
    b = b''.join((u.to_bytes(2, 'little') for u in units))
    return b.decode('utf-16-le')


def build_award_html_from_message(message: types.Message, award_text_plain: str) -> str:
    text_full = message.text or ""
    entities = message.entities or []

    try:
        start_py = text_full.index(award_text_plain)
    except ValueError:
        return award_text_plain

    end_py = start_py + len(award_text_plain)

    full_units = _to_utf16_units(text_full)
    prefix_units = _to_utf16_units(text_full[:start_py])
    award_units = _to_utf16_units(award_text_plain)
    start_u16 = len(prefix_units)
    end_u16 = start_u16 + len(award_units)

    ce_entities = []
    for e in entities:
        if getattr(e, "type", None) != "custom_emoji":
            continue
        off = e.offset
        length = e.length
        seg_start = off
        seg_end = off + length
        if seg_end <= start_u16 or seg_start >= end_u16:
            continue
        ce_entities.append({
            "offset": seg_start,
            "length": length,
            "custom_emoji_id": getattr(e, "custom_emoji_id", None),
        })

    result_units = []
    cur = start_u16
    ce_entities.sort(key=lambda x: x["offset"])

    for ent in ce_entities:
        e_off = ent["offset"]
        e_len = ent["length"]
        e_id = ent["custom_emoji_id"]

        if e_off > cur:
            result_units.extend(full_units[cur:e_off])

        emoji_units = full_units[e_off:e_off + e_len]
        emoji_char = _from_utf16_units(emoji_units)

        emoji_html = f'<tg-emoji emoji-id="{e_id}">{emoji_char}</tg-emoji>'
        result_units.extend(_to_utf16_units(emoji_html))

        cur = e_off + e_len

    if cur < end_u16:
        result_units.extend(full_units[cur:end_u16])

    award_html = _from_utf16_units(result_units).strip()
    return award_html or award_text_plain


def _is_target_owner(target_id: int) -> bool:
    try:
        user = tg_get_user_by_id_cached(target_id)
        return (user.username or "").lower() == OWNER_USERNAME.lower()
    except Exception:
        return False


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "наградить"))
def cmd_award(m: types.Message):
    add_stat_message(m)
    add_stat_command('award')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_MANAGE_AWARDS)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права управлять наградами."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
        award_text = raw_after.strip()
    else:
        if not args:
            return bot.reply_to(
                m,
                premium_prefix("Укажи пользователя и название награды."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        first = args[0]
        if (first.startswith('@') or first.startswith("t.me/") or "t.me/" in first
                or (first.isdigit() and len(first) >= 4)):
            target_id = parse_target_user(m, [first])
            if target_id is None or not isinstance(target_id, int) or target_id < 1:
                return bot.reply_to(
                    m,
                    premium_prefix("Не удалось определить пользователя."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            award_text = " ".join(args[1:]).strip()
        else:
            return bot.reply_to(
                m,
                premium_prefix("Укажи пользователя корректно."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    if _is_target_owner(target_id) and not is_owner(m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("Профиль разработчика нельзя изменять."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    award_text = award_text.replace("\n", " ").strip()
    if not award_text:
        return bot.reply_to(
            m,
            premium_prefix("Укажи название награды."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if len(award_text) > 50:
        return bot.reply_to(
            m,
            premium_prefix("Текст награды не должен превышать 50 символов."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile = get_profile(m.chat.id, target_id)
    awards = profile["awards"]
    if len(awards) >= 10:
        return bot.reply_to(
            m,
            premium_prefix("У пользователя уже максимум наград."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    award_html = build_award_html_from_message(m, award_text)
    awards.append(award_html)
    save_profiles()
    _invalidate_profile_cache(m.chat.id, target_id)

    award_idx = len(awards)

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_AWARDS_BLOCK_ID}">🏅</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>получил новую награду [{award_idx}].</b>"
    )

    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "снять награду"))
def cmd_remove_award(m: types.Message):
    add_stat_message(m)
    add_stat_command('remove_award')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_MANAGE_AWARDS)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права управлять наградами."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    text_full = m.text or ""
    raw_after = text_full[len("Снять награду"):].strip()
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
        after_target = raw_after.strip()
    else:
        if not args:
            return bot.reply_to(
                m,
                premium_prefix("Укажи пользователя и номер награды."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        first = args[0]
        if first.isdigit() and len(first) < 4:
            return bot.reply_to(
                m,
                premium_prefix("Не удалось определить пользователя."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        if first.startswith("@") or first.startswith("t.me/") or "t.me/" in first or (first.isdigit() and len(first) >= 4):
            target_id = parse_target_user(m, [first])
            if target_id is None or not isinstance(target_id, int) or target_id < 1:
                return bot.reply_to(
                    m,
                    premium_prefix("Не удалось определить пользователя."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            after_target = " ".join(args[1:]).strip()
        else:
            return bot.reply_to(
                m,
                premium_prefix("Укажи пользователя корректно."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    if not after_target:
        return bot.reply_to(
            m,
            premium_prefix("Укажи номер награды."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    try:
        idx = int(after_target)
    except ValueError:
        return bot.reply_to(
            m,
            premium_prefix("Номер награды должен быть числом."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile = get_profile(m.chat.id, target_id)
    awards = profile.get("awards") or []

    if idx < 1 or idx > len(awards):
        return bot.reply_to(
            m,
            premium_prefix("Награда с таким номером не найдена."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    awards.pop(idx - 1)
    save_profiles()
    _invalidate_profile_cache(m.chat.id, target_id)

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_AWARDS_BLOCK_ID}">🏅</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>снята награда [{idx}].</b>"
    )

    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "снять все награды"))
def cmd_remove_all_awards(m: types.Message):
    add_stat_message(m)
    add_stat_command('remove_all_awards')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_MANAGE_AWARDS)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права управлять наградами."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    text_full = m.text or ""
    raw_after = text_full[len("Снять все награды"):].strip()
    args = raw_after.split() if raw_after else []

    if m.reply_to_message or args:
        target_id = parse_target_user(m, args)
    else:
        return bot.reply_to(
            m,
            premium_prefix("Укажи пользователя, у которого нужно снять все награды."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if target_id is None or not isinstance(target_id, int) or target_id < 1:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось определить пользователя."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if _is_target_owner(target_id) and not is_owner(m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("Профиль разработчика нельзя изменять."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile = get_profile(m.chat.id, target_id)
    if not profile["awards"]:
        return bot.reply_to(
            m,
            premium_prefix("У пользователя нет наград."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile["awards"].clear()
    save_profiles()
    _invalidate_profile_cache(m.chat.id, target_id)

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_AWARDS_BLOCK_ID}">🏅</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>все награды были сняты.</b>"
    )

    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )

# ==== ТЕГИ TELEGRAM ==== 

MAX_USER_TAG_LEN = 32


def _resolve_tag_target_and_text(m: types.Message, require_text: bool) -> tuple[int | None, str]:
    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
        tag_text = raw_after.strip()
        return target_id, tag_text

    if not args:
        return None, ""

    first = args[0]
    target_id = parse_target_user(m, [first])
    if not isinstance(target_id, int) or target_id < 1:
        return None, ""

    tag_text = " ".join(args[1:]).strip() if len(args) > 1 else ""
    if require_text:
        return target_id, tag_text
    return target_id, ""


def _normalize_tag_text(tag_text: str) -> str:
    text = (tag_text or "").strip()
    if "|" in text:
        left, right = text.split("|", 1)
        text = (right.strip() or left.strip())
    return text


@bot.message_handler(commands=['settag'])
def cmd_settag(m: types.Message):
    add_stat_message(m)
    add_stat_command('settag')

    if m.chat.type not in ['group', 'supergroup']:
        return reply_error(m, "group_only")

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_SET_ROLE_TEXT)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(m, premium_prefix("У вашей должности нет права управлять тегами."), parse_mode='HTML', disable_web_page_preview=True)
        return

    target_id, raw_tag = _resolve_tag_target_and_text(m, require_text=True)
    if not target_id:
        return bot.reply_to(m, premium_prefix("Укажи пользователя и текст тега."), parse_mode='HTML', disable_web_page_preview=True)

    tag_text = _normalize_tag_text(raw_tag)
    if not tag_text:
        return bot.reply_to(m, premium_prefix("Укажи текст тега."), parse_mode='HTML', disable_web_page_preview=True)
    if len(tag_text) > MAX_USER_TAG_LEN:
        return bot.reply_to(m, premium_prefix(f"Тег не должен превышать {MAX_USER_TAG_LEN} символов."), parse_mode='HTML', disable_web_page_preview=True)

    ok_tag, err_tag = raw_set_chat_member_tag(m.chat.id, target_id, tag_text)

    name = link_for_user(m.chat.id, target_id)
    warn = ""
    if not ok_tag:
        warn = f"\n<i>Telegram-тег не применён через API: {_html.escape(err_tag or 'неизвестная ошибка')}.</i>"
    text = (
        f'<tg-emoji emoji-id="{EMOJI_USER_ROLE_TEXT_ID}">🏷</tg-emoji> '
        f"{name} [<code>{target_id}</code>] <b>получил тег:</b> <code>{_html.escape(tag_text)}</code>{warn}"
    )
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(commands=['removetag'])
def cmd_removetag(m: types.Message):
    add_stat_message(m)
    add_stat_command('removetag')

    if m.chat.type not in ['group', 'supergroup']:
        return reply_error(m, "group_only")

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_SET_ROLE_TEXT)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(m, premium_prefix("У вашей должности нет права управлять тегами."), parse_mode='HTML', disable_web_page_preview=True)
        return

    target_id, _ = _resolve_tag_target_and_text(m, require_text=False)
    if not target_id:
        return bot.reply_to(m, premium_prefix("Укажи пользователя, у которого нужно снять тег."), parse_mode='HTML', disable_web_page_preview=True)

    old_tag = get_user_custom_tag(m.chat.id, target_id)
    if not old_tag:
        return bot.reply_to(m, premium_prefix("У пользователя нет тега."), parse_mode='HTML', disable_web_page_preview=True)

    ok_tag, err_tag = raw_set_chat_member_tag(m.chat.id, target_id, "")

    name = link_for_user(m.chat.id, target_id)
    warn = ""
    if not ok_tag:
        warn = f"\n<i>Telegram-тег не снят через API: {_html.escape(err_tag or 'неизвестная ошибка')}.</i>"
    text = (
        f'<tg-emoji emoji-id="{EMOJI_USER_ROLE_TEXT_ID}">🏷</tg-emoji> '
        f"{name} [<code>{target_id}</code>] <b>тег снят.</b>{warn}"
    )
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(commands=['taglist'])
def cmd_taglist(m: types.Message):
    add_stat_message(m)
    add_stat_command('taglist')

    wait_seconds = cooldown_hit('chat', int(m.chat.id), 'taglist', 15)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='taglist')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_VIEW_LISTS)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права смотреть списки."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    candidate_ids: set[int] = set()
    for uid_s in (USERS.get(str(m.chat.id)) or {}).keys():
        try:
            candidate_ids.add(int(uid_s))
        except Exception:
            continue

    try:
        for adm in bot.get_chat_administrators(m.chat.id):
            if adm and getattr(adm, "user", None):
                candidate_ids.add(adm.user.id)
    except Exception:
        pass

    items: list[tuple[int, str]] = []
    for uid in sorted(candidate_ids):
        tag = get_user_custom_tag(m.chat.id, uid)
        if tag:
            items.append((uid, tag))

    if not items:
        return bot.reply_to(
            m,
            premium_prefix("В этом чате нет пользователей с тегами Telegram."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    lines = []
    for uid_int, user_tag in items[:50]:
        name = link_for_user(m.chat.id, uid_int)
        lines.append(f"• {name} [<code>{uid_int}</code>] — <code>{_html.escape(user_tag)}</code>")

    text = (
        f'<tg-emoji emoji-id="{EMOJI_USER_ROLE_TEXT_ID}">🏷</tg-emoji> '
        f"<b>Telegram-теги пользователей в этом чате:</b>\n" +
        "\n".join(lines)
    )

    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


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


def _is_ru_taglist_alias(m: types.Message) -> bool:
    prefix, cmd, rest = _extract_command_info(m)
    if cmd != "теглист":
        return False
    # По требованию: если к команде что-то дописано/передано аргументом — игнорируем.
    if rest:
        return False
    if prefix in COMMAND_PREFIXES:
        return True
    return (m.text or "").strip().lower() == "теглист"


@bot.message_handler(func=lambda m: _is_ru_taglist_alias(m))
def cmd_taglist_ru_alias(m: types.Message):
    fake = m
    fake.text = "/taglist"
    cmd_taglist(fake)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "список тегов"))
def cmd_taglist_text(m: types.Message):
    fake = m
    fake.text = "/taglist"
    cmd_taglist(fake)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "выдать тег"))
def cmd_settag_text(m: types.Message):
    parts = m.text.split(maxsplit=2)
    rest = parts[2] if len(parts) >= 3 else ""
    fake = m
    fake.text = f"/settag {rest}".strip()
    cmd_settag(fake)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "снять тег"))
def cmd_removetag_text(m: types.Message):
    parts = m.text.split(maxsplit=2)
    rest = parts[2] if len(parts) >= 3 else ""
    fake = m
    fake.text = f"/removetag {rest}".strip()
    cmd_removetag(fake)


# ==== ДОЛЖНОСТИ: ПОВЫСИТЬ / ПОНИЗИТЬ ====

def _change_rank(
    chat_id: int,
    actor: types.User,
    target_id: int,
    delta: int | None = None,
    set_rank: int | None = None,
) -> str:
    """
    Внутренний хелпер: изменить ранг target_id на +1 или -1
    с учётом ограничений.
    Возвращает текст ошибки (str) или пустую строку, если всё ок.
    """
    fake_target = types.User(target_id, False, first_name="", last_name=None, username=None)
    actor_rank = get_user_rank(chat_id, actor.id)
    target_rank = get_user_rank(chat_id, target_id)

    is_actor_owner_bot = is_owner(actor)
    is_target_owner_bot = is_owner(fake_target)

    # 1) разработчика бота трогать нельзя
    if is_target_owner_bot:
        return "Нельзя изменять должность разработчика бота."

    # 2) нельзя выдавать должность разработчику
    if is_target_owner_bot:
        return "Нельзя выдавать должность разработчику бота."

    # 3) определяем настоящего владельца чата
    is_real_owner_chat_actor = False
    is_real_owner_chat_target = False
    try:
        m_actor = bot.get_chat_member(chat_id, actor.id)
        if m_actor.status == 'creator':
            is_real_owner_chat_actor = True
    except Exception:
        pass
    try:
        m_target = bot.get_chat_member(chat_id, target_id)
        if m_target.status == 'creator':
            is_real_owner_chat_target = True
    except Exception:
        pass

    if is_real_owner_chat_target:
        return "Вы не можете менять должность настоящего владельца чата."

    # 4) иерархия: нельзя трогать равных и выше себя (кроме владельца бота)
    if not is_actor_owner_bot:
        if actor_rank <= target_rank:
            return "Вы не можете изменить должность этого пользователя (его должность не ниже вашей)."

    if not ((delta is None) ^ (set_rank is None)):
        return "Внутренняя ошибка изменения должности."

    # 5) считаем новый ранг
    if set_rank is not None:
        try:
            new_rank = int(set_rank)
        except Exception:
            return "Указан некорректный ранг."
        if new_rank < 1 or new_rank > 5:
            return "Ранг должен быть числом от 1 до 5."
        if new_rank == target_rank:
            return "У пользователя уже эта должность."
    else:
        if delta > 0:
            # повышаем
            if target_rank >= 5:
                return "У этого пользователя уже максимальная должность."
        else:
            # понижаем
            if target_rank <= 0:
                return "У этого пользователя нет должности, которую можно понизить."

        new_rank = target_rank + delta

    if is_real_owner_chat_actor:
        # настоящий владелец чата: может довести до 5, ниже 0 не опускаем
        if new_rank > 5:
            new_rank = 5
        if new_rank < 0:
            new_rank = 0
    else:
        # НЕ настоящий владелец чата: максимум 4
        if new_rank > 4:
            return "Вы не можете назначить должность выше ранга Главного админа."
        if new_rank < 0:
            new_rank = 0
        # нельзя назначать ранг выше собственного, кроме владельца бота
        if (not is_actor_owner_bot) and new_rank > actor_rank:
            return "Вы не можете назначить должность выше своей собственной."

    set_user_rank(chat_id, target_id, new_rank)
    return ""


def _resolve_target_for_rank(m: types.Message) -> int | None:
    """
    Общий парсер цели для повышения/понижения: reply / @username / ID / t.me.
    """
    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    # reply приоритетнее
    if m.reply_to_message:
        return m.reply_to_message.from_user.id

    if not args:
        return None

    first = args[0]
    if (first.startswith("@")
            or first.startswith("t.me/")
            or "t.me/" in first
            or (first.isdigit() and len(first) >= 4)):
        return parse_target_user(m, [first])

    return None


def _resolve_target_and_promote_rank(m: types.Message) -> tuple[int | None, int | None, bool]:
    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    target_id = _resolve_target_for_rank(m)
    if not isinstance(target_id, int):
        return None, None, False

    rank_token: str | None = None
    if m.reply_to_message:
        if args:
            rank_token = args[0]
    else:
        if len(args) >= 2:
            rank_token = args[1]

    if rank_token is None:
        return target_id, None, False

    if not rank_token.isdigit():
        return target_id, None, True

    rank_value = int(rank_token)
    if rank_value < 1 or rank_value > 5:
        return target_id, None, True
    return target_id, rank_value, False


@bot.message_handler(commands=['promote'])
def cmd_promote(m: types.Message):
    add_stat_message(m)
    add_stat_command('promote')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    # ПРАВА: должность должна иметь PERM_PROMOTE
    if not has_role_perm(m.chat.id, m.from_user.id, PERM_PROMOTE):
        return bot.reply_to(
            m,
            premium_prefix("У вашей должности нет права повышать пользователей."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    target_id, requested_rank, bad_rank = _resolve_target_and_promote_rank(m)
    if not isinstance(target_id, int):
        return bot.reply_to(
            m,
            premium_prefix("Укажи пользователя (ответом, @username, ID или ссылкой)."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )
    if bad_rank:
        return bot.reply_to(
            m,
            premium_prefix("Укажи целевой ранг числом от 1 до 5. Пример: /promote @username 5"),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    current_rank = get_user_rank(m.chat.id, target_id)
    if requested_rank is not None and requested_rank <= current_rank:
        return bot.reply_to(
            m,
            premium_prefix("Для повышения укажи ранг выше текущего."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    if requested_rank is None:
        err = _change_rank(m.chat.id, m.from_user, target_id, delta=+1)
    else:
        err = _change_rank(m.chat.id, m.from_user, target_id, set_rank=requested_rank)
    if err:
        return bot.reply_to(
            m,
            premium_prefix(err),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    new_rank = get_user_rank(m.chat.id, target_id)
    rank_html = get_rank_label_html(new_rank) or "без должности"
    rank_instr = get_rank_label_instrumental(new_rank)
    name = link_for_user(m.chat.id, target_id)
    if rank_instr:
        text = (
            "<b>Повышение пользователя</b>\n"
            f"<b>Пользователь:</b> {name} [<code>{target_id}</code>]\n"
            f"<b>Новая должность:</b> {rank_html}\n"
            f"<b>Статус:</b> назначен {rank_instr}."
        )
    else:
        text = (
            "<b>Повышение пользователя</b>\n"
            f"<b>Пользователь:</b> {name} [<code>{target_id}</code>]\n"
            f"<b>Новая должность:</b> {rank_html}"
        )
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(commands=['demote'])
def cmd_demote(m: types.Message):
    add_stat_message(m)
    add_stat_command('demote')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    # ПРАВА: должность должна иметь PERM_DEMOTE
    if not has_role_perm(m.chat.id, m.from_user.id, PERM_DEMOTE):
        return bot.reply_to(
            m,
            premium_prefix("У вашей должности нет права понижать пользователей."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    target_id = _resolve_target_for_rank(m)
    if not isinstance(target_id, int):
        return bot.reply_to(
            m,
            premium_prefix("Укажи пользователя (ответом, @username, ID или ссылкой)."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    err = _change_rank(m.chat.id, m.from_user, target_id, -1)
    if err:
        return bot.reply_to(
            m,
            premium_prefix(err),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    new_rank = get_user_rank(m.chat.id, target_id)
    name = link_for_user(m.chat.id, target_id)

    if new_rank <= 0:
        text = (
            f"{name} [<code>{target_id}</code>] больше не имеет никакой должности."
        )
    else:
        rank_html = get_rank_label_html(new_rank) or "без должности"
        rank_instr = get_rank_label_instrumental(new_rank)
        if rank_instr:
            text = (
                f"{name} [<code>{target_id}</code>] <b>назначен</b> {rank_instr}.\n"
                f"{rank_html}"
            )
        else:
            text = (
                f"{name} [<code>{target_id}</code>] теперь имеет должность:\n"
                f"{rank_html}"
            )

    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "повысить"))
def cmd_promote_text(m: types.Message):
    # "Повысить @user" или reply + "Повысить"
    parts = m.text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    fake = m
    fake.text = f"/promote {rest}".strip()
    cmd_promote(fake)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "понизить"))
def cmd_demote_text(m: types.Message):
    parts = m.text.split(maxsplit=1)
    rest = parts[1] if len(parts) > 1 else ""
    fake = m
    fake.text = f"/demote {rest}".strip()
    cmd_demote(fake)

@bot.message_handler(commands=['staff', 'ranks'])
def cmd_staff(m: types.Message):
    add_stat_message(m)
    add_stat_command('staff')

    wait_seconds = cooldown_hit('chat', int(m.chat.id), 'staff', 10)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='staff')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    cid = str(m.chat.id)
    chat_users = USERS.get(cid) or {}

    # {rank: [user_id, ...]}
    by_rank: dict[int, list[int]] = {}

    for uid_str in chat_users.keys():
        try:
            uid_int = int(uid_str)
        except ValueError:
            continue

        rank = get_user_rank(m.chat.id, uid_int)

        # ранг 999 — разработчик бота
        if rank >= 999:
            by_rank.setdefault(999, []).append(uid_int)

            # если разработчик одновременно настоящий владелец чата — добавим его и как 5
            try:
                member = bot.get_chat_member(m.chat.id, uid_int)
                if member.status == 'creator':
                    by_rank.setdefault(5, []).append(uid_int)
            except Exception:
                pass

            continue  # основной ранг уже обработали

        # обычные должности
        if rank <= 0:
            continue

        by_rank.setdefault(rank, []).append(uid_int)

    if not by_rank:
        return bot.reply_to(
            m,
            premium_prefix("В этом чате нет пользователей с должностями."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    priority_order = [999, 5, 4, 3, 2, 1]
    ranks_sorted = [r for r in priority_order if r in by_rank]

    lines = ["<b>Должности в этом чате:</b>"]
    first_block = True

    for rank in ranks_sorted:
        users = sorted(set(by_rank[rank]))
        if not users:
            continue

        header = get_rank_label_html(rank)
        if not header:
            continue

        if not first_block:
            lines.append("")
        first_block = False

        lines.append(header)

        for uid_int in users:
            name = link_for_user(m.chat.id, uid_int)
            lines.append(f"• {name} [<code>{uid_int}</code>]")

    text = "\n".join(lines)
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


@bot.message_handler(func=lambda m: (m.text or "").strip().lower() == "админы")
def cmd_staff_text(m: types.Message):
    # просто прокинуть в /staff
    fake = m
    fake.text = "/staff"
    cmd_staff(fake)


INACTIVE_USERS_COOLDOWN_SECONDS = 30 * 60
INACTIVE_USERS_DEFAULT_WINDOW_SECONDS = 30 * 24 * 60 * 60
INACTIVE_USERS_DEFAULT_LIMIT = 25
INACTIVE_USERS_MAX_LIMIT = 100


def _parse_inactive_window(value: str) -> int | None:
    token = (value or "").strip().lower()
    if not token:
        return None

    if token.isdigit():
        days = int(token)
        if days <= 0:
            return None
        return days * 24 * 60 * 60

    match = _re.match(r"^(\d+)\s*(m|h|d|w|mou|y|мин|м|ч|д|н|мес|г)$", token)
    if not match:
        return None

    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        return None

    multipliers = {
        "m": 60, "мин": 60, "м": 60,
        "h": 60 * 60, "ч": 60 * 60,
        "d": 24 * 60 * 60, "д": 24 * 60 * 60,
        "w": 7 * 24 * 60 * 60, "н": 7 * 24 * 60 * 60,
        "mou": 30 * 24 * 60 * 60, "мес": 30 * 24 * 60 * 60,
        "y": 365 * 24 * 60 * 60, "г": 365 * 24 * 60 * 60,
    }
    sec = multipliers.get(unit)
    if not sec:
        return None
    return amount * sec


def _format_inactive_age(last_seen: float, now_ts: float) -> str:
    if last_seen <= 0:
        return "нет данных"
    if last_seen > now_ts:
        return "дата активности некорректна"

    ago = int(max(0, now_ts - last_seen))
    if ago < 60:
        return "менее минуты назад"
    if ago < 60 * 60:
        return f"{ago // 60}м назад"
    if ago < 24 * 60 * 60:
        return f"{ago // (60 * 60)}ч назад"
    if ago < 30 * 24 * 60 * 60:
        return f"{ago // (24 * 60 * 60)}д назад"
    if ago < 365 * 24 * 60 * 60:
        return f"{ago // (30 * 24 * 60 * 60)}мес назад"
    return f"{ago // (365 * 24 * 60 * 60)}г назад"


def _format_inactive_window(seconds: int) -> str:
    if seconds < 60 * 60:
        return f"{max(1, seconds // 60)}м"
    if seconds < 24 * 60 * 60:
        return f"{seconds // (60 * 60)}ч"
    if seconds < 30 * 24 * 60 * 60:
        return f"{seconds // (24 * 60 * 60)}д"
    if seconds < 365 * 24 * 60 * 60:
        return f"{seconds // (30 * 24 * 60 * 60)}мес"
    return f"{seconds // (365 * 24 * 60 * 60)}г"


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and match_command_aliases(m.text, ['inactive', 'inactive_users', 'неактив', 'неактивные']))
def cmd_inactive_users(m: types.Message):
    add_stat_message(m)
    _, cmd, _ = _extract_command_info(m)
    add_stat_command(cmd or 'inactive')

    if not (_is_special_actor(m.chat.id, m.from_user) or user_is_real_admin(m.chat.id, m.from_user.id)):
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только администраторам чата."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    if not is_owner(m.from_user):
        wait_seconds = cooldown_hit('chat', int(m.chat.id), 'inactive_users', INACTIVE_USERS_COOLDOWN_SECONDS)
        if wait_seconds > 0:
            return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='inactive_users')

    parts = (m.text or "").split()
    args = parts[1:] if len(parts) > 1 else []
    if len(args) > 2:
        return bot.reply_to(
            m,
            premium_prefix("Использование: /inactive [порог_неактивности] [лимит]. Пример: /inactive 30d 50"),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    window_seconds = INACTIVE_USERS_DEFAULT_WINDOW_SECONDS
    limit = INACTIVE_USERS_DEFAULT_LIMIT

    if len(args) >= 1:
        parsed = _parse_inactive_window(args[0])
        if parsed is None:
            return bot.reply_to(
                m,
                premium_prefix("Неверный порог неактивности. Примеры: 30, 14d, 12h, 45д, 2н."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        window_seconds = parsed

    if len(args) >= 2:
        if not args[1].isdigit():
            return bot.reply_to(
                m,
                premium_prefix("Лимит должен быть числом от 1 до 100."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        limit = max(1, min(INACTIVE_USERS_MAX_LIMIT, int(args[1])))

    chat_users = USERS.get(str(m.chat.id)) or {}
    if not chat_users:
        return bot.reply_to(
            m,
            premium_prefix("В этом чате пока нет данных по пользователям."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    now_ts = time.time()
    threshold_ts = now_ts - window_seconds
    rows: list[tuple[float, int, str, str]] = []

    for uid_s, data in chat_users.items():
        try:
            uid = int(uid_s)
        except Exception:
            continue

        g = GLOBAL_USERS.get(uid_s) or {}
        last_seen = float(g.get("last_seen") or 0)
        if last_seen > threshold_ts:
            continue

        username = (g.get("username") or data.get("username") or "").strip()
        full_name = (g.get("full_name") or data.get("full_name") or data.get("first_name") or "").strip()
        rows.append((last_seen if last_seen > 0 else 0.0, uid, username, full_name))

    if not rows:
        return bot.reply_to(
            m,
            premium_prefix("Неактивных пользователей за выбранный период не найдено."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    def _sort_inactive_rows_by_last_seen(row: tuple[float, int, str, str]) -> tuple[int, float, int]:
        # last_seen=0 (нет данных) выводим первыми
        if row[0] <= 0:
            return (0, 0.0, row[1])
        return (1, row[0], row[1])

    rows.sort(key=_sort_inactive_rows_by_last_seen)
    shown = rows[:limit]

    lines = [
        "<b>Неактивные пользователи</b>",
        f"<b>Чат:</b> {_html.escape(m.chat.title or str(m.chat.id))} [<code>{m.chat.id}</code>]",
        f"<b>Порог неактивности:</b> {_format_inactive_window(window_seconds)}",
        f"<b>Найдено:</b> <code>{len(rows)}</code> (показано: <code>{len(shown)}</code>)",
        "",
    ]

    for idx, (last_seen, uid, username, full_name) in enumerate(shown, 1):
        display = _html.escape(full_name or f"id{uid}")
        if username:
            display = f"@{_html.escape(username)} ({display})"
        lines.append(
            f"{idx}. {display} [<code>{uid}</code>] — <i>{_format_inactive_age(last_seen, now_ts)}</i>"
        )

    return bot.reply_to(
        m,
        "\n".join(lines),
        parse_mode='HTML',
        disable_web_page_preview=True,
    )


@bot.message_handler(commands=['myrank'])
def cmd_myrank(m: types.Message):
    wait_seconds = cooldown_hit('user', int(m.from_user.id), 'myrank', 10)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='myrank')

    r = get_user_rank(m.chat.id, m.from_user.id)
    bot.reply_to(
        m,
        f"Ваш ID: {m.from_user.id}\nusername: @{m.from_user.username}\nrank: {r}",
        parse_mode='HTML'
    )

# ==============================
# DESCRIPTION – описание профиля
# ==============================

@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "+описание"))
def cmd_set_description(m: types.Message):
    add_stat_message(m)
    add_stat_command("setdesc")

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    # определяем target и текст
    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
        desc_text = raw_after.strip()
    else:
        if not args:
            return bot.reply_to(
                m,
                premium_prefix("Укажи пользователя и текст описания."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

        first = args[0]
        if (first.startswith("@") or first.startswith("t.me/") or "t.me/" in first
                or (first.isdigit() and len(first) >= 4)):
            target_id = parse_target_user(m, [first])
            if not isinstance(target_id, int) or target_id < 1:
                return bot.reply_to(
                    m,
                    premium_prefix("Не удалось определить пользователя."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            desc_text = " ".join(args[1:]).strip()
        else:
            target_id = m.from_user.id
            desc_text = raw_after.strip()

    if target_id != m.from_user.id and _is_target_owner(target_id) and not is_owner(m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("Профиль разработчика нельзя изменять."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if target_id != m.from_user.id:
        status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_SET_DESC_OTHERS)
        if not allowed:
            if status == 'no_perm':
                return bot.reply_to(
                    m,
                    premium_prefix("У вашей должности нет права менять описания другим пользователям."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            return bot.reply_to(
                m,
                premium_prefix("Вы не можете менять описания другим пользователям."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    if not desc_text:
        return bot.reply_to(
            m,
            premium_prefix("Укажи текст описания."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if len(desc_text) > 200:
        return bot.reply_to(
            m,
            premium_prefix("Описание не должно превышать 200 символов."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile = get_profile(m.chat.id, target_id)
    profile["description"] = desc_text
    save_profiles()
    _invalidate_profile_cache(m.chat.id, target_id)

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_DESC_ID}">📝</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>описание добавлено:</b>\n"
        f"{desc_text}"
    )
    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "-описание"))
def cmd_clear_description(m: types.Message):
    add_stat_message(m)
    add_stat_command("cleardesc")

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        if args:
            first = args[0]
            if (first.startswith("@") or first.startswith("t.me/") or "t.me/" in first
                    or (first.isdigit() and len(first) >= 4)):
                target_id = parse_target_user(m, [first])
                if not isinstance(target_id, int) or target_id < 1:
                    return bot.reply_to(
                        m,
                        premium_prefix("Не удалось определить пользователя."),
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
            else:
                target_id = m.from_user.id
        else:
            target_id = m.from_user.id

    if target_id != m.from_user.id and _is_target_owner(target_id) and not is_owner(m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("Профиль разработчика нельзя изменять."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if target_id != m.from_user.id:
        status, allowed = check_role_permission(m.chat.id, m.from_user.id, PERM_SET_DESC_OTHERS)
        if not allowed:
            if status == 'no_perm':
                return bot.reply_to(
                    m,
                    premium_prefix("У вашей должности нет права менять описания другим пользователям."),
                    parse_mode='HTML',
                    disable_web_page_preview=True
                )
            return bot.reply_to(
                m,
                premium_prefix("Вы не можете менять описания другим пользователям."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    profile = get_profile(m.chat.id, target_id)
    if not profile.get("description"):
        return bot.reply_to(
            m,
            premium_prefix("У этого пользователя нет описания."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    profile["description"] = ""
    save_profiles()
    _invalidate_profile_cache(m.chat.id, target_id)

    name = link_for_user(m.chat.id, target_id)
    text = (
        f'<tg-emoji emoji-id="{EMOJI_DESC_ID}">📝</tg-emoji> '
        f"{name} [<code>{target_id}</code>] "
        f"<b>описание очищено.</b>"
    )
    bot.reply_to(
        m,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "награды"))
def cmd_show_awards(m: types.Message):
    add_stat_message(m)
    add_stat_command('awards')

    wait_seconds = cooldown_hit('user', int(m.from_user.id), 'awards', 5)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='awards')

    if m.chat.type == 'private':
        return bot.reply_to(
            m,
            premium_prefix("Эта команда работает только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        if not args:
            target_id = m.from_user.id
        else:
            first = args[0]
            if first.isdigit() or first.startswith("@") or first.startswith("t.me/") or "t.me/" in first:
                target_id = parse_target_user(m, args)
                if target_id is None:
                    return bot.reply_to(
                        m,
                        premium_prefix(
                            "Не удалось определить пользователя для просмотра наград.\n"
                            "Если используется @username, убедись, что пользователь уже писал в этот чат или боту в ЛС."
                        ),
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
            else:
                return  # текст, но не указатель — молчим

    text = build_profile_awards_text(m.chat.id, target_id)

    bot.reply_to(
        m,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: m.text and text_starts_with_ci(m.text, "описание") and not text_starts_with_ci(m.text, "+описание") and not text_starts_with_ci(m.text, "-описание"))
def cmd_show_description(m: types.Message):
    add_stat_message(m)
    add_stat_command('showdesc')

    wait_seconds = cooldown_hit('user', int(m.from_user.id), 'showdesc', 5)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='showdesc')

    if m.chat.type == 'private':
        return bot.reply_to(
            m,
            premium_prefix("Эта команда работает только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1] if len(parts) > 1 else ""
    args = raw_after.split() if raw_after else []

    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        if not args:
            target_id = m.from_user.id
        else:
            first = args[0]
            if first.isdigit() or first.startswith("@") or first.startswith("t.me/") or "t.me/" in first:
                target_id = parse_target_user(m, args)
                if target_id is None:
                    return bot.reply_to(
                        m,
                        premium_prefix(
                            "Не удалось определить пользователя для просмотра описания.\n"
                            "Если используется @username, убедись, что пользователь уже писал в этот чат или боту в ЛС."
                        ),
                        parse_mode='HTML',
                        disable_web_page_preview=True
                    )
            else:
                return

    text = build_profile_description_text(m.chat.id, target_id)

    bot.reply_to(
        m,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True
    )


# ==== КОМАНДЫ ЗАКРЫТИЯ/ОТКРЫТИЯ ЧАТА (ПРОСТЫЕ) ====

# Закрытие: кроме PERM_CLOSE_CHAT требуется реальный админ Telegram (см. user_is_real_admin).
# Открытие (/openchat): только PERM_OPEN_CHAT — намеренно, чтобы при ошибочном закрытии
# чат могли открыть и без статуса администратора (кнопка «Открыть чат» и команда).


def user_is_real_admin(chatid: int, userid: int) -> bool:
    try:
        member = bot.get_chat_member(chatid, userid)
        return member.status in ('administrator', 'creator')
    except Exception:
        return False


def _send_chat_close_log(chatid: int, actor_id: int, durationseconds: int | None = None) -> None:
    """Отправить лог закрытия чата в лог-канал (если настроен)."""
    try:
        import html as _html_mod
        try:
            chat = bot.get_chat(chatid)
            chat_title = _html_mod.escape(chat.title or str(chatid))
            chat_link = f'<a href="https://t.me/c/{str(chat.id).lstrip("-100")}/0">{chat_title}</a> [<code>{chatid}</code>]'
        except Exception:
            chat_link = f"<code>{chatid}</code>"
        try:
            actor = bot.get_chat(actor_id)
            actor_name = _html_mod.escape(actor.first_name or str(actor_id))
            actor_link = f'<a href="tg://user?id={actor_id}">{actor_name}</a> [<code>{actor_id}</code>]'
        except Exception:
            actor_link = f"<code>{actor_id}</code>"
        lines = [
            "🔒 <b>#ЗАКРЫТИЕ_ЧАТА</b>",
            f"<b>Группа:</b> {chat_link}",
            f"<b>Администратор:</b> {actor_link}",
        ]
        if durationseconds and durationseconds > 0:
            lines.append(f"<b>Срок:</b> {format_closechat_duration_text(durationseconds)}")
        from datetime import datetime as _dt
        import time as _time
        lines.append(f"<b>Время:</b> {_dt.utcfromtimestamp(_time.time()).strftime('%Y-%m-%d %H:%M')} UTC")
        lines.append(f"#id{actor_id} #id{abs(chatid)}")
        send_log_event(chatid, "chat_closed", "\n".join(lines))
    except Exception as e:
        print(f"[LOG CHAT CLOSE] {e}")


def _send_chat_open_log(chatid: int, actor_id: int) -> None:
    """Отправить лог открытия чата в лог-канал (если настроен)."""
    try:
        import html as _html_mod
        try:
            chat = bot.get_chat(chatid)
            chat_title = _html_mod.escape(chat.title or str(chatid))
            chat_link = f'<a href="https://t.me/c/{str(chat.id).lstrip("-100")}/0">{chat_title}</a> [<code>{chatid}</code>]'
        except Exception:
            chat_link = f"<code>{chatid}</code>"
        try:
            actor = bot.get_chat(actor_id)
            actor_name = _html_mod.escape(actor.first_name or str(actor_id))
            actor_link = f'<a href="tg://user?id={actor_id}">{actor_name}</a> [<code>{actor_id}</code>]'
        except Exception:
            actor_link = f"<code>{actor_id}</code>"
        from datetime import datetime as _dt
        import time as _time
        lines = [
            "🔓 <b>#ОТКРЫТИЕ_ЧАТА</b>",
            f"<b>Группа:</b> {chat_link}",
            f"<b>Администратор:</b> {actor_link}",
            f"<b>Время:</b> {_dt.utcfromtimestamp(_time.time()).strftime('%Y-%m-%d %H:%M')} UTC",
            f"#id{actor_id} #id{abs(chatid)}",
        ]
        send_log_event(chatid, "chat_opened", "\n".join(lines))
    except Exception as e:
        print(f"[LOG CHAT OPEN] {e}")


def format_closechat_duration_text(seconds: int) -> str:
    if seconds <= 0:
        return ""

    units = [
        ('г',   365 * 24 * 60 * 60),
        ('мес',  30 * 24 * 60 * 60),
        ('н',    7 * 24 * 60 * 60),
        ('д',    24 * 60 * 60),
        ('ч',    60 * 60),
        ('м',    60),
    ]
    for suffix, sec_in_unit in units:
        if seconds % sec_in_unit == 0 and seconds >= sec_in_unit:
            amount = seconds // sec_in_unit
            return f"{amount}{suffix}"

    minutes = max(1, seconds // 60)
    return f"{minutes}м"


def send_closechat_message(chatid: int, actorid: int, durationseconds: int | None, withbutton: bool) -> int | None:
    rank = get_user_rank(chatid, actorid)
    role_plain = get_rank_label_plain(rank)
    name_html = link_for_user(chatid, actorid)
    emoji_closed = f'<tg-emoji emoji-id="{EMOJI_CHAT_CLOSED_ID}">🔒</tg-emoji>'
    from moderation import _fmt_time

    if durationseconds and durationseconds > 0:
        until_ts = int(time.time() + int(durationseconds))
        text = (
            f"{emoji_closed} <b>{role_plain}</b> {name_html} <b>закрыл чат.</b>\n"
            f"<b>Истекает:</b> {_fmt_time(until_ts)}"
        )
    else:
        text = f"{emoji_closed} <b>{role_plain}</b> {name_html} <b>закрыл чат.</b>"

    kb = None
    if withbutton:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton(
            "Открыть чат",
            callback_data=f"openchatbtn:{chatid}",
            icon_custom_emoji_id=EMOJI_CHAT_OPEN_BTN_ID,
        ))

    try:
        msg = bot.send_message(
            chatid,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        return msg.message_id
    except Exception as e:
        print(f"[CLOSECHAT] send_message failed: {e}")
        return None


@bot.message_handler(commands=['closechat'])
def cmd_closechat(m: types.Message):
    add_stat_message(m)
    add_stat_command('closechat')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    parts = m.text.split(maxsplit=1)
    raw_after = parts[1].strip() if len(parts) > 1 else ""
    durationseconds = None
    if raw_after:
        durationseconds = parse_closechat_duration(raw_after, is_russian=False)
        if durationseconds is None:
            return bot.reply_to(
                m,
                premium_prefix("Неверный формат времени для /closechat или превышен лимит (не более 1 дня)."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )

    actor = m.from_user
    chatid = m.chat.id

    if m.reply_to_message:
        return

    status, allowed = check_role_permission(chatid, actor.id, PERM_CLOSE_CHAT)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права закрывать чат."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    if not user_is_real_admin(chatid, actor.id):
        return bot.reply_to(
            m,
            premium_prefix("Без префикса нельзя закрыть чат."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    closed_perms = build_closed_permissions()
    ok = setchatdefaultpermissions(chatid, closed_perms)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось изменить права чата."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    untilts = 0
    if durationseconds:
        untilts = time.time() + durationseconds

    setclosechatstate(chatid, closed=True, until_ts=untilts)

    send_closechat_message(chatid, actor.id, durationseconds, withbutton=True)
    _send_chat_close_log(chatid, actor.id, durationseconds)

    if durationseconds:
        schedulereopenchat(chatid, durationseconds)


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "закрыть чат"))
def cmd_closechat_text(m: types.Message):
    add_stat_message(m)
    add_stat_command('closechat_text')

    if m.chat.type not in ['group', 'supergroup']:
        return

    if m.reply_to_message:
        return

    text_full = m.text.strip()
    lower = text_full.lower()
    prefix = "закрыть чат"
    if not lower.startswith(prefix):
        return

    rest = text_full[len(prefix):].strip()
    durationseconds = None

    if rest:
        parts = rest.split()
        if len(parts) != 1:
            return
        token = parts[0]
        durationseconds = parse_closechat_duration(token, is_russian=True)
        if durationseconds is None:
            return

    actor = m.from_user
    chatid = m.chat.id

    status, allowed = check_role_permission(chatid, actor.id, PERM_CLOSE_CHAT)
    if not allowed:
        if status == 'no_perm':
            bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права закрывать чат."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    if not user_is_real_admin(chatid, actor.id):
        return bot.reply_to(
            m,
            premium_prefix("Без префикса нельзя закрыть чат."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    closed_perms = build_closed_permissions()
    ok = setchatdefaultpermissions(chatid, closed_perms)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось изменить права чата."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    untilts = 0
    if durationseconds:
        untilts = time.time() + durationseconds

    setclosechatstate(chatid, closed=True, until_ts=untilts)

    send_closechat_message(chatid, actor.id, durationseconds, withbutton=True)
    _send_chat_close_log(chatid, actor.id, durationseconds)

    if durationseconds:
        schedulereopenchat(chatid, durationseconds)


@bot.message_handler(commands=['openchat'])
def cmd_openchat(m: types.Message):
    add_stat_message(m)
    add_stat_command('openchat')

    if m.chat.type not in ['group', 'supergroup']:
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только в группах."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    if m.reply_to_message:
        return

    actor = m.from_user
    chatid = m.chat.id

    status, allowed = check_role_permission(chatid, actor.id, PERM_OPEN_CHAT)
    if not allowed:
        if status == 'no_perm':
            return bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права открывать чат."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    state = getclosechatstate(chatid)
    if not state or not state.get("closed"):
        return bot.reply_to(
            m,
            premium_prefix("Чат уже открыт."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    open_perms = build_open_permissions()
    ok = setchatdefaultpermissions(chatid, open_perms)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось изменить права чата."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    setclosechatstate(chatid, closed=False, until_ts=0)
    _send_chat_open_log(chatid, actor.id)

    emoji_open = f'<tg-emoji emoji-id="{EMOJI_CHAT_OPEN_BTN_ID}">🔓</tg-emoji>'
    rank = get_user_rank(chatid, actor.id)
    role_plain = get_rank_label_plain(rank)
    name = link_for_user(chatid, actor.id)
    text = f"{emoji_open} <b>{role_plain}</b> {name} <b>открыл чат.</b>"

    bot.send_message(
        chatid,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.message_handler(func=lambda m: text_starts_with_ci(m.text, "открыть чат"))
def cmd_openchat_text(m: types.Message):
    add_stat_message(m)
    add_stat_command('openchat_text')

    if m.chat.type not in ['group', 'supergroup']:
        return

    if m.reply_to_message:
        return

    actor = m.from_user
    chatid = m.chat.id

    status, allowed = check_role_permission(chatid, actor.id, PERM_OPEN_CHAT)
    if not allowed:
        if status == 'no_perm':
            bot.reply_to(
                m,
                premium_prefix("У вашей должности нет права открывать чат."),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    state = getclosechatstate(chatid)
    if not state or not state.get("closed"):
        return

    open_perms = build_open_permissions()
    ok = setchatdefaultpermissions(chatid, open_perms)
    if not ok:
        return

    setclosechatstate(chatid, closed=False, until_ts=0)
    _send_chat_open_log(chatid, actor.id)

    emoji_open = f'<tg-emoji emoji-id="{EMOJI_CHAT_OPEN_BTN_ID}">🔓</tg-emoji>'
    rank = get_user_rank(chatid, actor.id)
    role_plain = get_rank_label_plain(rank)
    name = link_for_user(chatid, actor.id)
    text = f"{emoji_open} <b>{role_plain}</b> {name} <b>открыл чат.</b>"

    bot.send_message(
        chatid,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("openchatbtn:"))
def cb_openchat_button(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    try:
        _, chatid_s = c.data.split(":", 1)
        chatid = int(chatid_s)
    except Exception:
        return bot.answer_callback_query(c.id)

    user = c.from_user

    rank = get_user_rank(chatid, user.id)
    if rank <= 0 and not _is_special_actor(chatid, user):
        return bot.answer_callback_query(c.id)

    status, allowed = check_role_permission(chatid, user.id, PERM_OPEN_CHAT)
    if not allowed:
        if status == 'no_perm':
            return bot.answer_callback_query(
                c.id,
                text="У вашей должности недостаточно прав, чтобы открыть чат.",
                show_alert=True
            )
        return bot.answer_callback_query(c.id)

    state = getclosechatstate(chatid)
    if not state or not state.get("closed"):
        return bot.answer_callback_query(
            c.id,
            text="Чат уже открыт.",
            show_alert=True
        )

    open_perms = build_open_permissions()
    ok = setchatdefaultpermissions(chatid, open_perms)
    if not ok:
        return bot.answer_callback_query(
            c.id,
            text="Не удалось изменить права чата.",
            show_alert=True
        )

    setclosechatstate(chatid, closed=False, until_ts=0)
    _send_chat_open_log(chatid, user.id)

    try:
        bot.delete_message(chatid, c.message.message_id)
    except Exception:
        pass

    emoji_open = f'<tg-emoji emoji-id="{EMOJI_CHAT_OPEN_BTN_ID}">🔓</tg-emoji>'
    role_plain = get_rank_label_plain(rank)
    name = link_for_user(chatid, user.id)
    text = f"{emoji_open} <b>{role_plain}</b> {name} <b>открыл чат.</b>"

    try:
        bot.send_message(chatid, text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception:
        pass

    bot.answer_callback_query(c.id, text="Чат открыт.", show_alert=False)


# ==== МУЗЫКА: ПОИСК + ВЫГРУЗКА MP3 ====

MUSIC_SEARCH_LIMIT = 10
MUSIC_QUERY_MAX_LEN = 120
MUSIC_RESULT_TTL_SECONDS = 15 * 60
MUSIC_MAX_DURATION_SECONDS = 30 * 60
MUSIC_MAX_FILE_SIZE_BYTES = 49 * 1024 * 1024
MUSIC_SEARCH_COOLDOWN_SECONDS = 8
MUSIC_DOWNLOAD_COOLDOWN_SECONDS = 15

_MUSIC_SEARCH_SOURCES: list[tuple[str, int, str]] = [
    ("ytsearch", 6, "YouTube"),
    ("scsearch", 4, "SoundCloud"),
]

_MUSIC_RESULTS_CACHE: dict[str, dict[str, Any]] = {}
_MUSIC_RESULTS_CACHE_LOCK = threading.Lock()


def _music_cleanup_cache() -> None:
    now_ts = int(time.time())
    with _MUSIC_RESULTS_CACHE_LOCK:
        stale_tokens = [
            token
            for token, payload in _MUSIC_RESULTS_CACHE.items()
            if int(payload.get("expires_at", 0) or 0) <= now_ts
        ]
        for token in stale_tokens:
            _MUSIC_RESULTS_CACHE.pop(token, None)


def _music_new_token() -> str:
    return secrets.token_hex(6)


def _music_parse_duration(value: Any) -> int | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return seconds


def _music_format_duration(seconds: int | None) -> str:
    if not seconds:
        return "?"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _music_collect_results(query: str) -> list[dict[str, Any]]:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "cachedir": False,
        "socket_timeout": 15,
        "extract_flat": True,
    }
    seen_urls: set[str] = set()
    results: list[dict[str, Any]] = []

    for provider, count, source_title in _MUSIC_SEARCH_SOURCES:
        if len(results) >= MUSIC_SEARCH_LIMIT:
            break
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                data = ydl.extract_info(f"{provider}{count}:{query}", download=False)
        except Exception as e:
            logger.warning("[music] search source failed (%s): %s", provider, e)
            continue

        entries = (data or {}).get("entries") or []
        for item in entries:
            if not isinstance(item, dict):
                continue
            title = (item.get("title") or "").strip()
            url = (item.get("webpage_url") or item.get("url") or "").strip()
            if not title or not url:
                continue
            if not url.startswith("http"):
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)
            results.append(
                {
                    "title": title,
                    "url": url,
                    "duration": _music_parse_duration(item.get("duration")),
                    "uploader": (item.get("uploader") or item.get("channel") or "").strip(),
                    "source": source_title,
                }
            )
            if len(results) >= MUSIC_SEARCH_LIMIT:
                break

    return results


def _music_build_results_keyboard(token: str, results: list[dict[str, Any]]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup()
    for idx, item in enumerate(results):
        src = _html.escape(str(item.get("source") or "Источник"))
        dur = _music_format_duration(_music_parse_duration(item.get("duration")))
        title = str(item.get("title") or "").strip()
        short_title = title if len(title) <= 40 else (title[:37] + "…")
        label = f"{idx + 1}. [{src}] {short_title} ({dur})"
        kb.row(types.InlineKeyboardButton(label, callback_data=f"music:pick:{token}:{idx}"))
    kb.row(types.InlineKeyboardButton("Закрыть", callback_data=f"music:cancel:{token}"))
    return kb


def _music_store_results(chat_id: int, user_id: int, query: str, results: list[dict[str, Any]]) -> str:
    _music_cleanup_cache()
    token = _music_new_token()
    with _MUSIC_RESULTS_CACHE_LOCK:
        _MUSIC_RESULTS_CACHE[token] = {
            "chat_id": int(chat_id),
            "user_id": int(user_id),
            "query": query,
            "results": results,
            "expires_at": int(time.time()) + MUSIC_RESULT_TTL_SECONDS,
        }
    return token


def _music_get_results(token: str) -> dict[str, Any] | None:
    _music_cleanup_cache()
    with _MUSIC_RESULTS_CACHE_LOCK:
        payload = _MUSIC_RESULTS_CACHE.get(token)
        if not payload:
            return None
        if int(payload.get("expires_at", 0) or 0) <= int(time.time()):
            _MUSIC_RESULTS_CACHE.pop(token, None)
            return None
        return payload


def _music_download_mp3(url: str) -> tuple[str, dict[str, Any], str]:
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("На сервере не найден ffmpeg, конвертация в MP3 недоступна.")

    temp_dir = tempfile.mkdtemp(prefix="tgmusic_")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(temp_dir, "%(title)s [%(id)s].%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "cachedir": False,
        "socket_timeout": 20,
        "prefer_ffmpeg": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        mp3_path = None
        requested = (info or {}).get("requested_downloads") or []
        for entry in requested:
            if not isinstance(entry, dict):
                continue
            candidate = (entry.get("filepath") or "").strip()
            if candidate and candidate.lower().endswith(".mp3") and os.path.exists(candidate):
                mp3_path = candidate
                break
        if not mp3_path:
            for file_name in os.listdir(temp_dir):
                if file_name.lower().endswith(".mp3"):
                    candidate = os.path.join(temp_dir, file_name)
                    if os.path.isfile(candidate):
                        mp3_path = candidate
                        break
        if not mp3_path:
            raise RuntimeError("Не удалось подготовить MP3 файл.")
        return mp3_path, (info or {}), temp_dir


def _music_extract_query(m: types.Message) -> tuple[str | None, str]:
    _, cmd, rest = _extract_command_info(m)
    return cmd, (rest or "").strip()


@bot.message_handler(func=lambda m: match_command_aliases(m.text, ['music', 'музыка']))
def cmd_music_search(m: types.Message):
    add_stat_message(m)
    cmd, query = _music_extract_query(m)
    add_stat_command(cmd or 'music')

    if not query:
        return bot.reply_to(
            m,
            premium_prefix("Укажи название трека. Пример: <code>/music Linkin Park Numb</code>"),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    query = " ".join(query.split())
    if len(query) < 2:
        return bot.reply_to(
            m,
            premium_prefix("Слишком короткий запрос. Укажи хотя бы 2 символа."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    if len(query) > MUSIC_QUERY_MAX_LEN:
        return bot.reply_to(
            m,
            premium_prefix(f"Слишком длинный запрос. Максимум {MUSIC_QUERY_MAX_LEN} символов."),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )

    if not is_owner(m.from_user):
        wait_seconds = cooldown_hit('user', int(m.from_user.id), 'music_search', MUSIC_SEARCH_COOLDOWN_SECONDS)
        if wait_seconds > 0:
            return reply_cooldown_message(
                m, wait_seconds, scope='user', bucket=int(m.from_user.id), action='music_search'
            )

    status_msg = bot.reply_to(
        m,
        "🔎 Ищу музыку по вашему запросу…",
        disable_web_page_preview=True,
    )
    try:
        results = _music_collect_results(query)
    except Exception as e:
        logger.warning("[music] search failed for query=%r: %s", query, e)
        results = []

    if not results:
        try:
            bot.edit_message_text(
                premium_prefix("Ничего не найдено. Попробуйте другой запрос."),
                chat_id=status_msg.chat.id,
                message_id=status_msg.message_id,
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        except Exception:
            bot.reply_to(
                m,
                premium_prefix("Ничего не найдено. Попробуйте другой запрос."),
                parse_mode='HTML',
                disable_web_page_preview=True,
            )
        return

    token = _music_store_results(m.chat.id, m.from_user.id, query, results)
    kb = _music_build_results_keyboard(token, results)
    text = (
        f"🎵 <b>Результаты поиска</b> по запросу: <code>{_html.escape(query)}</code>\n"
        "Выберите трек кнопкой ниже — бот подготовит и отправит MP3."
    )
    try:
        bot.edit_message_text(
            text,
            chat_id=status_msg.chat.id,
            message_id=status_msg.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
    except Exception:
        bot.send_message(
            m.chat.id,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("music:"))
def cb_music_actions(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return

    data = (c.data or "")
    parts = data.split(":")
    if len(parts) < 3:
        return bot.answer_callback_query(c.id)

    action = parts[1]
    token = parts[2]
    payload = _music_get_results(token)
    if not payload:
        return bot.answer_callback_query(c.id, text="Список устарел. Выполните поиск заново.", show_alert=True)

    if int(payload.get("user_id") or 0) != int(c.from_user.id):
        return bot.answer_callback_query(c.id, text="Только автор запроса может выбрать трек.", show_alert=True)

    if action == "cancel":
        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
        return bot.answer_callback_query(c.id)

    if action != "pick" or len(parts) < 4:
        return bot.answer_callback_query(c.id)

    try:
        index = int(parts[3])
    except (TypeError, ValueError):
        return bot.answer_callback_query(c.id)

    results = payload.get("results") or []
    if index < 0 or index >= len(results):
        return bot.answer_callback_query(c.id, text="Элемент не найден.", show_alert=True)

    if not is_owner(c.from_user):
        wait_seconds = cooldown_hit('user', int(c.from_user.id), 'music_download', MUSIC_DOWNLOAD_COOLDOWN_SECONDS)
        if wait_seconds > 0:
            return bot.answer_callback_query(
                c.id,
                text=f"Подожди {wait_seconds} сек. и попробуй снова.",
                show_alert=True,
            )

    item = results[index]
    title = str(item.get("title") or "Без названия")
    url = str(item.get("url") or "").strip()
    source = str(item.get("source") or "Источник")
    if not url.startswith("http"):
        return bot.answer_callback_query(c.id, text="Некорректная ссылка результата.", show_alert=True)

    try:
        bot.answer_callback_query(c.id, text="Подготавливаю MP3…", show_alert=False)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            (
                "⏬ Подготавливаю MP3, подождите…\n"
                f"<b>Трек:</b> {_html.escape(title)}"
            ),
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    temp_dir = None
    try:
        bot.send_chat_action(c.message.chat.id, "upload_audio")
        mp3_path, info, temp_dir = _music_download_mp3(url)

        duration = _music_parse_duration(info.get("duration"))
        if duration and duration > MUSIC_MAX_DURATION_SECONDS:
            raise RuntimeError("Трек слишком длинный. Максимум 30 минут.")

        file_size = os.path.getsize(mp3_path)
        if file_size > MUSIC_MAX_FILE_SIZE_BYTES:
            raise RuntimeError("Файл слишком большой для отправки ботом.")

        performer = (info.get("artist") or info.get("uploader") or item.get("uploader") or "").strip()
        send_title = (info.get("track") or info.get("title") or title or "").strip()

        with open(mp3_path, "rb") as audio_file:
            bot.send_audio(
                c.message.chat.id,
                audio_file,
                title=send_title[:128] if send_title else None,
                performer=performer[:128] if performer else None,
                duration=duration,
                caption=f"Источник: {source}",
            )

        try:
            bot.delete_message(c.message.chat.id, c.message.message_id)
        except Exception:
            pass
    except Exception as e:
        err_text = str(e) or "Не удалось выгрузить MP3."
        bot.send_message(
            c.message.chat.id,
            premium_prefix(_html.escape(err_text)),
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


# ==== ПРОВЕРКА, МОЖНО ЛИ ИСКЛЮЧИТЬ ЦЕЛЬ ====

def _can_kick_target(chatid: int, actor: types.User, target_id: int) -> tuple[bool, str | None]:
    """
    Проверяем, можно ли исключить target_id:
    - нельзя трогать спец-актеров (owner/dev/глобальная верификация и т.п.);
    - нельзя трогать админов/владельца (статус администратора/создателя);
    - нельзя трогать глобальных dev / локально верифицированных;
    - нельзя трогать тех, у кого ранг >= ранга исключающего;
    - нельзя трогать себя.
    """
    if target_id == actor.id:
        return False, "Нельзя исключить самого себя."

    # спец-актеры (твоя логика is_special_actor)
    try:
        dummy_user = types.User(id=target_id, is_bot=False, first_name=".", last_name=None, username=None)
        if _is_special_actor(chatid, dummy_user):
            return False, "Нельзя исключить пользователя с особым статусом."
    except Exception:
        pass

    # админ/владелец чата
    try:
        member = bot.get_chat_member(chatid, target_id)
        if member.status in ("administrator", "creator"):
            return False, "Нельзя исключить пользователя с префиксом."
    except Exception:
        pass

    # глобальные разработчики
    if target_id in VERIFY_DEV:
        return False, "Нельзя исключить dev-пользователя."
    
    # по рангам: нельзя исключить такой же или более высокий ранг
    actor_rank = get_user_rank(chatid, actor.id)
    target_rank = get_user_rank(chatid, target_id)
    if target_rank >= actor_rank > 0:
        return False, "Нельзя исключить пользователя с должностью."

    return True, None


# ==== ЛОГИКА ИСКЛЮЧЕНИЯ + РАЗБАН ====

def _kick_with_unban(chatid: int, actor: types.User, target_id: int, reason: str | None) -> str | None:
    """
    Исключение + моментальный разбан.
    Возвращает текст ошибки (для premium_prefix) или None, если всё ок.
    Для ранга 0 возвращает понятную ошибку доступа.
    """
    from moderation import _mark_farewell_suppressed
    # проверка прав по должности
    status, allowed = check_role_permission(chatid, actor.id, PERM_KICK)
    if not allowed:
        if status == 'no_rank':
            return "Для использования исключения назначьте себе должность с этим правом в /settings."
        if status == 'no_perm':
            # есть должность (1–5), но нет права
            return "У вашей должности нет права использовать исключение."
        # прочие случаи (теоретически)
        return "Вы не можете использовать исключение."

    # нельзя исключить недопустимую цель
    ok, err = _can_kick_target(chatid, actor, target_id)
    if not ok:
        return err

    # пробуем исключить (бан + разбан)
    try:
        if hasattr(bot, "ban_chat_member"):
            bot.ban_chat_member(chatid, target_id)
        else:
            bot.kick_chat_member(chatid, target_id)
    except ApiTelegramException as e:
        msg = str(e)
        if ("not enough rights" in msg or
                "not sufficient rights" in msg or
                "can_restrict_members" in msg):
            return "У бота нет прав для исключения. Дайте ему право «Блокировка пользователей»."
        return f"Не удалось исключить пользователя: {e}"
    except Exception as e:
        return f"Не удалось исключить пользователя: {e}"

    # моментальный разбан
    try:
        bot.unban_chat_member(chatid, target_id, only_if_banned=True)
    except Exception:
        pass

    tg_invalidate_chat_member_caches(chatid, target_id)
    _mark_farewell_suppressed(chatid, target_id)

    # уведомление
    actor_name = link_for_user(chatid, actor.id)
    target_name = link_for_user(chatid, target_id)

    emoji_kick = f'<tg-emoji emoji-id="{EMOJI_BTN_KICK_ID}">👢</tg-emoji>'
    emoji_reason = f'<tg-emoji emoji-id="{EMOJI_REASON_ID}">📝</tg-emoji>'

    lines: list[str] = []
    lines.append(f"{emoji_kick} <b>Пользователь</b> {target_name} <b>наказан.</b>")
    lines.append("<b>Наказание:</b> Исключение")
    if reason:
        reason = reason.strip()
        if reason:
            lines.append(f"{emoji_reason} <b>Причина:</b> {_html.escape(reason)}")
    lines.append(f"<b>Администратор:</b> {actor_name}")

    text = "\n".join(lines)

    try:
        bot.send_message(chatid, text, parse_mode='HTML', disable_web_page_preview=True)
    except Exception:
        pass

    return None


__all__ = [name for name in globals() if not name.startswith('__')]
