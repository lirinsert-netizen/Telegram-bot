"""
handlers.py — Завершающие обработчики:
  group stats UI, gstats/pm_settings_open callbacks,
  главный callback_handler (маршрутизатор),
  approve_group / deny_group,
  my_chat_member_handler,
  all_other — ДОЛЖЕН БЫТЬ ЗАРЕГИСТРИРОВАН ПОСЛЕДНИМ.
"""
from __future__ import annotations
import time
import threading
import asyncio
import queue as _queue
import io as _io
import re as _re
import html as _html
import math as _math
import unicodedata as _unicodedata

from config import (
    os, json, re, random, datetime,
    Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    bot, bot_raw, tg_client,
    TOKEN, OWNER_USERNAME, DATA_DIR, API_BASE_URL,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    PREMIUM_STATS_EMOJI_ID, PREMIUM_USER_EMOJI_ID,
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
    # message-event stats
    get_stats_for_period,
    get_stats_by_day,
    # clone assignment
    assign_bot_to_chat,
    unassign_bot_from_chat,
)
from helpers import *
from helpers import _dev_contact_find_item, _remember_owner_user_id, _user_can_open_settings
from cmd_basic import *
from cmd_basic import (
    START_MENU_STATE,
    _broadcast_collect_targets,
    _broadcast_new_draft,
    _broadcast_render_panel_text,
    _build_broadcast_panel_keyboard,
    _build_start_about_text,
    _build_start_back_keyboard,
    _build_start_commands_keyboard,
    _build_start_commands_text,
    _build_start_home_keyboard,
    _build_start_home_text,
    _build_start_usage_text,
    _dev_contact_intro_kb,
    _dev_contact_intro_text,
    _dev_contact_prompt_kb,
    _dev_contact_prompt_text,
    _send_start_menu,
    _show_dev_contact_new_messages,
    _sendpm_new_draft,
    _sendpm_render_panel_text,
    _build_sendpm_panel_keyboard,
    _sendpm_reply_keyboard,
    SENDPM_DRAFTS,
    SENDPM_PENDING_INPUT,
)
from settings_ui import *
from settings_ui import _build_settings_main_keyboard, _send_payload
from pin import *

# ==== СТАТИСТИКА ПО ГРУППЕ ====

STATS_PAGES = {}
GROUP_STATS_PAGE_SIZE = 30

# Метки периодов статистики
PERIOD_LABELS: dict[str, str] = {
    "all": "Всё время",
    "1d":  "1 день",
    "7d":  "7 дней",
    "30d": "30 дней",
}

# ==== ПРОФИЛЬ-КЭШ ИМЁН ====
# Кешируем отображаемые имена пользователей на 5 минут чтобы не бить TG API
# каждый раз при построении страницы статистики.

_PROFILE_NAME_CACHE: dict[tuple[int, int], tuple[float, str]] = {}
_PROFILE_NAME_CACHE_LOCK = threading.Lock()
_PROFILE_NAME_CACHE_TTL: int = 300  # секунды


def _get_cached_display_name(chat_id: int, user_id: int) -> str | None:
    """Return cached display name if still fresh, else None."""
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    with _PROFILE_NAME_CACHE_LOCK:
        cached = _PROFILE_NAME_CACHE.get(key)
        if cached and (now - cached[0]) < _PROFILE_NAME_CACHE_TTL:
            return cached[1]
    return None


def _set_cached_display_name(chat_id: int, user_id: int, name: str) -> None:
    key = (int(chat_id), int(user_id))
    with _PROFILE_NAME_CACHE_LOCK:
        _PROFILE_NAME_CACHE[key] = (time.monotonic(), name)

def _get_period_since(period: str) -> int:
    """Return unix timestamp marking the start of the requested period (0 = all time)."""
    offsets = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    if period not in offsets:
        return 0
    return int(time.time()) - offsets[period]


def build_message_link(chat: types.Chat, msg_id: int) -> str:
    if not msg_id:
        return ""
    if chat.username:
        return f"https://t.me/{chat.username}/{msg_id}"
    cid = str(chat.id)
    if cid.startswith("-100"):
        internal = cid[4:]
    elif cid.startswith("-"):
        internal = cid[1:]
    else:
        internal = cid
    return f"https://t.me/c/{internal}/{msg_id}"

def stats_user_link_html(chat: types.Chat, user_id: int, display_name: str) -> str:
    chat_id_s = str(chat.id)
    user_id_s = str(user_id)

    chat_users = USERS.get(chat_id_s) or {}
    data = chat_users.get(user_id_s) or {}

    username = data.get("username") or ""
    if username:
        url = f"https://t.me/{username}"
    else:
        url = f"tg://openmessage?user_id={user_id}"

    return f'<a href="{url}"><b>{display_name}</b></a>'

def build_group_stats_pages(chat: types.Chat, period: str = "all", max_items: int | None = None) -> list[str]:
    """Build paginated stats text for the given period.

    period='all'  → uses in-memory GROUP_STATS (historical all-time data).
    period='1d'|'7d'|'30d' → queries msg_events SQLite table.
    max_items — if set, truncate the full list to this many entries (one page only).
    """
    period_label = PERIOD_LABELS.get(period, period)

    if period == "all":
        chat_id = str(chat.id)
        chat_stats = GROUP_STATS.get(chat_id, {})

        if not chat_stats:
            return [premium_prefix("В этой группе пока нет данных для статистики.")]

        sorted_items = sorted(
            chat_stats.items(),
            key=lambda item: item[1].get("count", 0),
            reverse=True,
        )
        rows_data = [
            (int(uid), data.get("count", 0), data.get("last_msg_id"))
            for uid, data in sorted_items
        ]
    else:
        since_ts = _get_period_since(period)
        rows_data = get_stats_for_period(int(chat.id), since_ts)
        if not rows_data:
            return [premium_prefix(
                f"В этой группе пока нет данных за {period_label}.\n"
                f"<i>Данные накапливаются с момента последнего обновления бота.</i>"
            )]

    if max_items is not None:
        rows_data = rows_data[:max_items]

    chat_title_esc = _html.escape(getattr(chat, "title", None) or str(chat.id))

    items: list[str] = []
    for user_id, count, last_msg_id in rows_data:
        # Use profile name cache to avoid repeated TG API calls
        display_name = _get_cached_display_name(chat.id, user_id)
        if display_name is None:
            try:
                u = tg_get_chat_member(chat.id, int(user_id)).user
                display_name = u.full_name or u.first_name or u.username or "Пользователь"
            except Exception:
                display_name = "Пользователь"
            _set_cached_display_name(chat.id, user_id, display_name)

        name_html = stats_user_link_html(chat, int(user_id), display_name)

        base = (
            f'<tg-emoji emoji-id="{PREMIUM_USER_EMOJI_ID}">👤</tg-emoji> '
            f'{name_html} [<code>{user_id}</code>] — <b>{count}</b>'
        )
        items.append(base)

    page_size = GROUP_STATS_PAGE_SIZE
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    pages: list[str] = []
    for page_idx in range(total_pages):
        start = page_idx * page_size
        end = start + page_size
        chunk = items[start:end]

        header = (
            f'<tg-emoji emoji-id="{PREMIUM_STATS_EMOJI_ID}">📊</tg-emoji>'
            f' <b>Статистика за {_html.escape(period_label)} для чата "{chat_title_esc}"</b>\n\n'
        )
        body = "\n".join(chunk)
        page_text = (header + body).strip()
        if len(page_text) > MAX_MSG_LEN:
            page_text = page_text[:MAX_MSG_LEN - 3] + "..."
        pages.append(page_text)

    return pages


def _build_group_stats_keyboard(
    page: int, total_pages: int, period: str = "all"
) -> dict:
    """Build inline keyboard with period tabs, optional nav row and image button."""
    rows: list[list[dict]] = []

    # Row 1: period selector tabs
    tab_row: list[dict] = []
    for p, label in PERIOD_LABELS.items():
        btn_label = f"✅ {label}" if p == period else label
        tab_row.append({"text": btn_label, "callback_data": f"gstats_period:{p}"})
    rows.append(tab_row)

    # Row 2: pagination (only if there are multiple pages)
    if total_pages > 1:
        nav_row: list[dict] = []
        if page > 0:
            nav_row.append({"text": "⬅ Предыдущая", "callback_data": f"gstats_prev_{page}"})
        if page < total_pages - 1:
            nav_row.append({"text": "Следующая ➡", "callback_data": f"gstats_next_{page}"})
        if nav_row:
            rows.append(nav_row)

    # Row 3: image button
    rows.append([{"text": "📸 Картинка", "callback_data": "gstats_img"}])

    return {"inline_keyboard": rows}


def _send_stats_message(
    chat: types.Chat,
    period: str = "all",
    reply_to_message_id: int | None = None,
) -> None:
    """Generate stats pages and send (or edit) the stats message with period keyboard."""
    pages = build_group_stats_pages(chat, period)
    keyboard = _build_group_stats_keyboard(0, len(pages), period)
    resp = raw_send_with_inline_keyboard(
        chat.id, pages[0], keyboard,
        reply_to_message_id=reply_to_message_id,
    )
    if not resp or not resp.get("ok"):
        print(f"[STATS] Ошибка отправки: {resp}")
        return
    msg_id = resp["result"]["message_id"]
    STATS_PAGES[(chat.id, msg_id)] = {"pages": pages, "current": 0, "period": period}


def send_group_stats(chat: types.Chat, manual: bool = False):
    _IMG_TASK_QUEUE.put({
        "chat_id": chat.id,
        "period": "all",
        "chart_type": "users",
        "reply_msg_id": None,
    })


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and is_exact_stat(m))
def cmd_group_stats_manual(m: types.Message):
    add_stat_message(m)
    add_stat_command('group_stat')

    # Проверка одобрения группы
    if not is_group_approved(m.chat.id):
        return bot.reply_to(
            m,
            "⏳ Бот находится на модерации. Ожидание подтверждения от разработчика.",
            parse_mode='HTML'
        )

    # не реагируем, если команда введена ответом на сообщение
    if m.reply_to_message:
        return

    # Антиспам: лимит per-user (60 с) и per-chat (30 с)
    if m.from_user:
        uid_int = int(m.from_user.id)
        wait_u = cooldown_hit('user', uid_int, 'group_stat', 60)
        if wait_u > 0:
            return reply_cooldown_message(m, wait_u, scope='user', bucket=uid_int, action='group_stat')

    wait_seconds = cooldown_hit('chat', int(m.chat.id), 'group_stat', 30)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='group_stat')

    _IMG_TASK_QUEUE.put({
        "chat_id": m.chat.id,
        "period": "all",
        "chart_type": "users",
        "reply_msg_id": m.message_id,
    })


# ---- Новые команды: статистика день / неделя / месяц / вся ----

def _send_stats_limited(
    chat: types.Chat,
    period: str,
    chart_type: str,
    reply_to_message_id: int | None = None,
) -> None:
    """Send a single-page stats message (top 20) with just the image button."""
    pages = build_group_stats_pages(chat, period, max_items=20)
    keyboard = {"inline_keyboard": [[{"text": "📸 Картинка", "callback_data": "gstats_img"}]]}
    resp = raw_send_with_inline_keyboard(
        chat.id, pages[0], keyboard,
        reply_to_message_id=reply_to_message_id,
    )
    if not resp or not resp.get("ok"):
        return
    msg_id = resp["result"]["message_id"]
    STATS_PAGES[(chat.id, msg_id)] = {
        "pages": pages,
        "current": 0,
        "period": period,
        "chart_type": chart_type,
    }


def _stats_limited_guard(m: types.Message, period: str, chart_type: str) -> None:
    """Common guard + dispatch for the 4 limited stats commands."""
    add_stat_message(m)
    add_stat_command('group_stat')

    if not is_group_approved(m.chat.id):
        return bot.reply_to(
            m,
            "⏳ Бот находится на модерации. Ожидание подтверждения от разработчика.",
            parse_mode='HTML',
        )

    if m.reply_to_message:
        return

    # Антиспам: лимит per-user (60 с) и per-chat (30 с)
    if m.from_user:
        uid_int = int(m.from_user.id)
        wait_u = cooldown_hit('user', uid_int, 'group_stat', 60)
        if wait_u > 0:
            return reply_cooldown_message(m, wait_u, scope='user', bucket=uid_int, action='group_stat')

    wait_seconds = cooldown_hit('chat', int(m.chat.id), 'group_stat', 30)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope='chat', bucket=int(m.chat.id), action='group_stat')

    _IMG_TASK_QUEUE.put({
        "chat_id": m.chat.id,
        "period": period,
        "chart_type": chart_type,
        "reply_msg_id": m.message_id,
    })


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and is_exact_stat_day(m))
def cmd_stats_day(m: types.Message):
    _stats_limited_guard(m, period='1d', chart_type='users')


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and is_exact_stat_week(m))
def cmd_stats_week(m: types.Message):
    _stats_limited_guard(m, period='7d', chart_type='users')


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and is_exact_stat_month(m))
def cmd_stats_month(m: types.Message):
    _stats_limited_guard(m, period='30d', chart_type='users')


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and is_exact_stat_all(m))
def cmd_stats_all(m: types.Message):
    _stats_limited_guard(m, period='all', chart_type='users')


@bot.callback_query_handler(func=lambda call: bool(call.data) and call.data.startswith("gstats_"))
def cb_group_stats_pagination(call: types.CallbackQuery):
    if _is_duplicate_callback_query(call):
        return
    data = call.data or ""
    m = call.message
    key = (m.chat.id, m.message_id)
    state = STATS_PAGES.get(key)

    # ── Period tab selected ──────────────────────────────────────────────────
    if data.startswith("gstats_period:"):
        new_period = data.split(":", 1)[1]
        if new_period not in PERIOD_LABELS:
            return bot.answer_callback_query(call.id)
        if not state:
            return bot.answer_callback_query(
                call.id, "Эта статистика устарела, откройте новую.", show_alert=True
            )
        # Отвечаем немедленно — крутилка убирается, не ожидая тяжёлой работы.
        bot.answer_callback_query(call.id)
        # Тяжёлая работа (SQL + TG API) — в фоновом треде.
        chat_ref = m.chat
        chat_id_ref = m.chat.id
        msg_id_ref = m.message_id

        def _do_period_update():
            new_pages = build_group_stats_pages(chat_ref, new_period)
            new_keyboard = _build_group_stats_keyboard(0, len(new_pages), new_period)
            try:
                raw_edit_message_with_keyboard(
                    chat_id_ref, msg_id_ref, new_pages[0], new_keyboard
                )
                STATS_PAGES[key] = {"pages": new_pages, "current": 0, "period": new_period}
            except Exception as _e:
                print(f"[GSTATS] Ошибка обновления периода {new_period} для чата {chat_id_ref}: {_e}")

        threading.Thread(target=_do_period_update, daemon=True).start()
        return

    # ── Image button ─────────────────────────────────────────────────────────
    if data == "gstats_img":
        if not state:
            return bot.answer_callback_query(
                call.id, "Эта статистика устарела, откройте новую.", show_alert=True
            )
        period = state.get("period", "all")
        chart_type = state.get("chart_type", "users")
        _IMG_TASK_QUEUE.put({
            "chat_id": m.chat.id,
            "period": period,
            "chart_type": chart_type,
            "reply_msg_id": m.message_id,
        })
        return bot.answer_callback_query(call.id, "⏳ Генерирую картинку…")

    # ── Page navigation ──────────────────────────────────────────────────────
    if not state:
        return bot.answer_callback_query(
            call.id, "Эта статистика устарела, откройте новую.", show_alert=True
        )

    pages = state.get("pages") or []
    if not pages:
        STATS_PAGES.pop(key, None)
        return bot.answer_callback_query(call.id)

    match = re.match(r"^gstats_(next|prev)_(\d+)$", data)
    if not match:
        return bot.answer_callback_query(call.id)

    action = match.group(1)
    current = int(match.group(2))
    period = state.get("period", "all")
    total_pages = len(pages)

    if action == "next":
        target = min(total_pages - 1, current + 1)
    else:
        target = max(0, current - 1)

    keyboard = _build_group_stats_keyboard(target, total_pages, period)

    # Отвечаем до сетевого вызова editMessageText
    bot.answer_callback_query(call.id)
    try:
        raw_edit_message_with_keyboard(
            m.chat.id,
            m.message_id,
            pages[target],
            keyboard,
        )
        state["current"] = target
        STATS_PAGES[key] = state
    except Exception:
        pass


# ==== ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ СТАТИСТИКИ ====

_IMG_TASK_QUEUE: _queue.Queue = _queue.Queue()

# Максимальная длина отображаемого имени (в картинке и в подписи)
_STATS_NAME_MAXLEN = 20

# ─── TrueType-шрифты с фолбэком ──────────────────────────────────────────────
_FONT_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/app/fonts/DejaVuSans-Bold.ttf",
]
_FONT_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/app/fonts/DejaVuSans.ttf",
]


_FONT_CACHE: dict = {}


def _load_font(paths: list, size: int):
    """Load TrueType font from the first available path; fall back to default.

    Results are cached by (paths, size) to avoid repeated filesystem lookups
    on every render call.
    """
    import logging
    from PIL import ImageFont
    key = (tuple(paths), size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for fp in paths:
        try:
            font = ImageFont.truetype(fp, size)
            logging.debug("[stats_image] loaded font: %s @ %dpx", fp, size)
            _FONT_CACHE[key] = font
            return font
        except Exception:
            pass
    logging.warning("[stats_image] no TrueType font found (size=%d), using load_default()", size)
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _normalize_unicode_name(text: str) -> str:
    """Normalize fancy Unicode letters (e.g. 𝑴𝒂𝒏𝒌𝒊) to ASCII for chart rendering.

    NFKC compatibility decomposition maps mathematical/bold/italic Unicode
    characters to their plain ASCII equivalents so they render correctly with
    standard TrueType fonts.  The subsequent printability filter removes any
    control characters or combining marks that NFKC may produce; standard
    Latin/Cyrillic combining characters are inherently printable so they pass
    through, which is acceptable for chart label truncation purposes.
    """
    normalized = _unicodedata.normalize('NFKC', text)
    # Replace any remaining non-printable / unsupported chars with '?'
    result = ''.join(ch if ch.isprintable() else '?' for ch in normalized)
    return result


def _nice_axis_max(max_val: int) -> tuple[int, int]:
    """Return (axis_max, step) for a clean x-axis.

    axis_max is always strictly greater than max_val and an exact multiple of
    step.  step is chosen as the smallest value from a standard list that keeps
    the tick count at most 8, starting from 5 so the axis always shows round,
    easy-to-read numbers (e.g. max_val=6 → (10, 5); max_val=329 → (350, 50)).
    """
    if max_val <= 0:
        return 10, 5
    nice_steps = [5, 10, 20, 25, 50, 100, 200, 250, 500,
                  1000, 2000, 5000, 10000, 50000, 100000]
    for step in nice_steps:
        if max_val / step <= 8:
            nice = _math.ceil(max_val / step) * step
            if nice == max_val:
                nice += step
            return nice, step
    # Fallback for very large values
    step = nice_steps[-1]
    nice = (_math.ceil(max_val / step) + 1) * step
    return nice, step



def _lookup_user_display_name(chat_id: int, user_id: int) -> str:
    """Return a display name for the user. Tries profile name cache, then local data, then TG API."""
    # Fast path: in-memory TTL cache
    cached = _get_cached_display_name(chat_id, user_id)
    if cached is not None:
        return cached

    chat_id_s = str(chat_id)
    user_id_s = str(user_id)

    # Chat-specific USERS cache
    data = (USERS.get(chat_id_s) or {}).get(user_id_s) or {}
    name = (
        data.get("full_name")
        or (
            (data.get("first_name") or "")
            + (" " + data.get("last_name", "") if data.get("last_name") else "")
        ).strip()
    )
    if name:
        result = name[:30]
        _set_cached_display_name(chat_id, user_id, result)
        return result

    # Global users cache
    gdata = GLOBAL_USERS.get(user_id_s) or {}
    gname = (
        gdata.get("full_name")
        or (
            (gdata.get("first_name") or "")
            + (" " + gdata.get("last_name", "") if gdata.get("last_name") else "")
        ).strip()
    )
    if gname:
        result = gname[:30]
        _set_cached_display_name(chat_id, user_id, result)
        return result

    # Fall back to TG API (with cache)
    try:
        member = tg_get_chat_member(chat_id, user_id)
        u = member.user
        n = (u.full_name or u.first_name or u.username or "").strip()
        result = (n or f"ID {user_id}")[:30]
    except Exception:
        result = f"ID {user_id}"
    _set_cached_display_name(chat_id, user_id, result)
    return result


def _pick_template_slots(n_users: int) -> int:
    """Return the smallest template slot count (5/10/15/20) that fits n_users."""
    for slots in (5, 10, 15, 20):
        if n_users <= slots:
            return slots
    return 20


def _render_stats_image(
    rows: list[tuple[int, int]],
    chat_title: str,
    period_label: str,
    users_map: dict[int, str],
    max_users: int = 20,
) -> bytes:
    """Render an Excel-style horizontal stacked-bar chart PNG.

    Each row has two stacked segments:
    - Value bar  (#27C2F1, bright cyan): proportional to the message count.
    - Background (#B7E8FA, light cyan): fills the remainder to MAX_X,
      giving every row the same total bar width (progress-track effect).

    Template slots are chosen as the smallest of 5/10/15/20 that covers
    the actual number of users.  Empty slots (beyond real users) receive
    only the background track — their label column is left blank.

    Output is 1280 × dynamic-height px (height depends on slot count),
    rendered at 4× then downscaled with LANCZOS for crisp text and smooth
    edges.  Light-blue colour scheme matches the Excel templates.
    """
    from PIL import Image, ImageDraw

    top_n = rows[:min(max_users, 20)]
    if not top_n:
        raise ValueError("No rows to render")

    n_real    = len(top_n)
    slots     = _pick_template_slots(n_real)
    max_count = max(count for _, count in top_n)
    MAX_X, scale_step = _nice_axis_max(max_count)

    # ── Layout constants (output-px) ─────────────────────────────────────────
    WIDTH      = 1280
    SCALE      = 4        # supersampling factor (4× for HD quality)
    HEADER_H   = 95       # title + subtitle
    LEGEND_H   = 0        # legend strip removed
    PAD_ROWS   = 16       # padding above / below the rows block
    ROW_H      = 46       # height of one bar row
    ROW_GAP    = 10       # gap between consecutive rows
    AXIS_H     = 50       # x-axis labels area at the bottom
    PAD_L      = 22
    PAD_R      = 22
    LABEL_W    = 188      # label column width
    CHART_GAP  = 8        # gap between label column and bar area
    CHART_L    = PAD_L + LABEL_W + CHART_GAP   # bar area left edge
    CHART_R    = WIDTH - PAD_R                  # bar area right edge
    VALUE_W    = 68       # right zone reserved for count label outside bar
    BAR_MAX_W  = CHART_R - CHART_L - VALUE_W   # maximum bar pixel width

    total_rows_h = slots * (ROW_H + ROW_GAP) - ROW_GAP
    HEIGHT = HEADER_H + LEGEND_H + PAD_ROWS + total_rows_h + PAD_ROWS + AXIS_H

    # ── Palette ───────────────────────────────────────────────────────────────
    C_BG          = (0xDC, 0xEE, 0xFF)  # #DCEEFF – sheet background
    C_TITLE       = (0x0E, 0x1E, 0x38)  # dark navy
    C_SUBTITLE    = (0x3A, 0x55, 0x7A)
    C_BAR_VAL     = (0x27, 0xC2, 0xF1)  # #27C2F1 – value bar
    C_BAR_BG      = (0xB7, 0xE8, 0xFA)  # #B7E8FA – background / фон bar
    C_GRID        = (0x9B, 0xB3, 0xC6)  # #9BB3C6 – gridlines
    C_LABEL_STRIP = (0xCA, 0xE3, 0xF6)  # label column stripe
    C_LABEL_FG    = (0x0E, 0x1E, 0x38)
    C_VALUE       = (0x0E, 0x1E, 0x38)  # count text (dark, used both inside and outside bar)
    C_AXIS        = (0x4A, 0x62, 0x80)

    def sc(v: float) -> int:
        return int(v * SCALE)

    W, H = WIDTH * SCALE, HEIGHT * SCALE

    img  = Image.new('RGB', (W, H), C_BG)
    draw = ImageDraw.Draw(img)

    # ── Fonts ─────────────────────────────────────────────────────────────────
    fnt_title  = _load_font(_FONT_BOLD_PATHS, sc(17))
    fnt_sub    = _load_font(_FONT_REG_PATHS,  sc(10))
    fnt_label  = _load_font(_FONT_BOLD_PATHS, sc(10))
    fnt_value  = _load_font(_FONT_BOLD_PATHS, sc(11))
    fnt_axis   = _load_font(_FONT_REG_PATHS,  sc(9))

    # ── Header: title + chat subtitle ─────────────────────────────────────────
    title_txt = f"Статистика сообщений за {period_label}"
    tb = draw.textbbox((0, 0), title_txt, font=fnt_title)
    draw.text(
        (W // 2 - (tb[2] - tb[0]) // 2, sc(14)),
        title_txt, fill=C_TITLE, font=fnt_title,
    )
    sub_txt = f'Чат: "{chat_title[:52]}"'
    sb = draw.textbbox((0, 0), sub_txt, font=fnt_sub)
    draw.text(
        (W // 2 - (sb[2] - sb[0]) // 2, sc(46)),
        sub_txt, fill=C_SUBTITLE, font=fnt_sub,
    )

    # ── Chart geometry ────────────────────────────────────────────────────────
    chart_top    = HEADER_H + LEGEND_H + PAD_ROWS
    chart_bottom = chart_top + total_rows_h

    # ── X-axis scale steps (scale_step and MAX_X come from _nice_axis_max) ────
    scale_vals = list(range(scale_step, MAX_X + 1, scale_step))

    # Origin (y-axis) line
    draw.rectangle(
        [sc(CHART_L), sc(chart_top),
         sc(CHART_L) + max(1, SCALE - 1), sc(chart_bottom)],
        fill=C_GRID,
    )
    # "0" label at origin
    zero_lb = draw.textbbox((0, 0), "0", font=fnt_axis)
    draw.text(
        (sc(CHART_L) - (zero_lb[2] - zero_lb[0]) // 2,
         sc(chart_bottom + 8)),
        "0", fill=C_AXIS, font=fnt_axis,
    )

    # Gridlines + x-axis labels at each scale value
    for sv in scale_vals:
        xf = CHART_L + (sv / MAX_X) * BAR_MAX_W
        if xf > CHART_L + BAR_MAX_W:
            continue
        draw.rectangle(
            [sc(xf), sc(chart_top),
             sc(xf) + max(1, SCALE - 1), sc(chart_bottom)],
            fill=C_GRID,
        )
        lbl = str(sv)
        lb  = draw.textbbox((0, 0), lbl, font=fnt_axis)
        lw  = lb[2] - lb[0]
        lx_lbl = max(sc(CHART_L), sc(xf) - lw // 2)
        draw.text(
            (lx_lbl, sc(chart_bottom + 8)),
            lbl, fill=C_AXIS, font=fnt_axis,
        )

    # ── Rows ──────────────────────────────────────────────────────────────────
    for i in range(slots):
        is_empty = i >= n_real
        user_id  = top_n[i][0] if not is_empty else 0
        count    = top_n[i][1] if not is_empty else 0

        y     = chart_top + i * (ROW_H + ROW_GAP)
        y_mid = y + ROW_H // 2
        bar_y0 = sc(y + 4)
        bar_y1 = sc(y + ROW_H - 4)

        # ── Label column stripe ───────────────────────────────────────────────
        draw.rectangle(
            [sc(PAD_L), sc(y), sc(PAD_L + LABEL_W), sc(y + ROW_H)],
            fill=C_LABEL_STRIP,
        )

        # Label text (only for real users)
        if not is_empty:
            name_raw = users_map.get(user_id, "")
            # Normalize fancy Unicode (e.g. mathematical italic) to ASCII for rendering
            name_norm = _normalize_unicode_name(name_raw.strip()) if name_raw else ""
            label_text = name_norm[:_STATS_NAME_MAXLEN] if name_norm else "Unknown"
            # Truncate to fit column width
            max_lbl_px = sc(LABEL_W - 8)
            candidate  = label_text
            while len(candidate) > 1:
                lb2 = draw.textbbox((0, 0), candidate, font=fnt_label)
                if lb2[2] - lb2[0] <= max_lbl_px:
                    break
                candidate = candidate[:-1].rstrip()
            lb2 = draw.textbbox((0, 0), candidate, font=fnt_label)
            ltw = lb2[2] - lb2[0]
            lth = lb2[3] - lb2[1]
            # Right-align text in label column
            draw.text(
                (sc(PAD_L + LABEL_W - 4) - ltw, sc(y_mid) - lth // 2),
                candidate, fill=C_LABEL_FG, font=fnt_label,
            )

        # ── Stacked bars ──────────────────────────────────────────────────────
        bar_val_px = int(count / MAX_X * BAR_MAX_W) if MAX_X > 0 else 0
        bar_val_px = max(0, min(bar_val_px, BAR_MAX_W))
        bar_bg_px  = BAR_MAX_W - bar_val_px

        bx0 = sc(CHART_L)

        # Background (фон) segment — always drawn to fill the track
        if bar_bg_px > 0:
            draw.rectangle(
                [bx0 + sc(bar_val_px), bar_y0,
                 bx0 + sc(BAR_MAX_W),  bar_y1],
                fill=C_BAR_BG,
            )

        # Value segment
        if bar_val_px > 0:
            draw.rectangle(
                [bx0, bar_y0, bx0 + sc(bar_val_px), bar_y1],
                fill=C_BAR_VAL,
            )

            # Count label: inside bar if it fits, otherwise right of bar
            vt  = str(count)
            vb2 = draw.textbbox((0, 0), vt, font=fnt_value)
            vw  = vb2[2] - vb2[0]
            vh  = vb2[3] - vb2[1]
            if sc(bar_val_px) >= vw + sc(10):
                # Centred inside the value bar – dark text
                cx = bx0 + sc(bar_val_px) // 2 - vw // 2
                draw.text((cx, sc(y_mid) - vh // 2), vt, fill=C_VALUE, font=fnt_value)
            else:
                # Right of the value bar – dark text
                cx = bx0 + sc(bar_val_px) + sc(4)
                draw.text((cx, sc(y_mid) - vh // 2), vt, fill=C_VALUE, font=fnt_value)

    # ── Downscale to output size ───────────────────────────────────────────────
    img_out = img.resize((WIDTH, HEIGHT), Image.LANCZOS)
    buf = _io.BytesIO()
    img_out.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
def _render_daily_chart(
    days_data: list[tuple[int, int]],
    chat_title: str,
    period_label: str,
) -> bytes:
    """Render a vertical bar chart grouped by day.

    Output dimensions are calculated dynamically based on the number of bars.
    Draws at 3× then downscales with LANCZOS.
    days_data: list of (day_ts_utc, count) ordered ASC (oldest → newest).
    """
    from PIL import Image, ImageDraw

    if not days_data:
        raise ValueError("No data")

    # ── Palette ──────────────────────────────────────────────────────────────
    C_BG      = (0x16, 0x1d, 0x2e)
    C_CARD    = (0x1e, 0x26, 0x38)
    C_SEP     = (0x2c, 0x39, 0x52)
    C_GRID    = (0x2c, 0x38, 0x52, 90)
    C_BAR     = (0x34, 0xa8, 0xd0, 255)
    C_BAR_SUN = (0x5c, 0xc8, 0xe8, 255)   # Sunday: brighter accent
    C_BAR_HI  = (0x80, 0xdc, 0xf4, 160)   # top-edge highlight
    C_TITLE   = (0xec, 0xf2, 0xfc)
    C_SUB     = (0x7a, 0x90, 0xb2)
    C_MUTED   = (0x5a, 0x72, 0x90)
    C_LABEL   = (0x8a, 0xa2, 0xc0)

    # ── Canvas — dynamic size based on number of bars ─────────────────────────
    n_bars = len(days_data)
    W_OUT = max(640, min(1920, n_bars * 40 + 200))
    H_OUT = max(400, W_OUT * 5 // 8)
    S = 3
    W, H = W_OUT * S, H_OUT * S

    def sc(v: float) -> int:
        return int(v * S)

    # ── Layout ────────────────────────────────────────────────────────────────
    HDR_H    = 72
    FTR_H    = 40
    PAD_LR   = 36
    SCALE_W  = 38   # left margin reserved for scale labels

    chart_x0 = PAD_LR + SCALE_W
    chart_y0 = HDR_H
    chart_x1 = W_OUT - PAD_LR
    chart_y1 = H_OUT - FTR_H
    chart_w  = chart_x1 - chart_x0
    chart_h  = chart_y1 - chart_y0

    n     = len(days_data)
    gap   = max(1, min(4, chart_w // max(1, n * 4)))
    bar_w = max(2, (chart_w - gap * max(0, n - 1)) // n)

    img  = Image.new('RGBA', (W, H), C_BG + (255,))
    draw = ImageDraw.Draw(img, 'RGBA')

    fnt_title = _load_font(_FONT_BOLD_PATHS, sc(17))
    fnt_sub   = _load_font(_FONT_REG_PATHS,  sc(10))
    fnt_label = _load_font(_FONT_REG_PATHS,  sc(8))
    fnt_scale = _load_font(_FONT_REG_PATHS,  sc(8))

    # ── Header ────────────────────────────────────────────────────────────────
    draw.rectangle([0, 0, W, sc(HDR_H)], fill=C_CARD + (255,))
    draw.rectangle([0, sc(HDR_H) - S, W, sc(HDR_H)], fill=C_SEP + (255,))

    for txt, yy, font, fill in [
        (f"📊  Статистика за {period_label}", 10, fnt_title, C_TITLE),
        (f'Чат: "{chat_title[:52]}"', 38, fnt_sub, C_SUB),
    ]:
        bbox = draw.textbbox((0, 0), txt, font=font)
        tw   = bbox[2] - bbox[0]
        tx   = max(sc(PAD_LR), sc(W_OUT // 2) - tw // 2)
        draw.text((tx, sc(yy)), txt, fill=fill + (255,), font=font)

    # ── Horizontal grid lines (inside plot only) + scale labels ───────────────
    max_count = max(c for _, c in days_data) or 1
    for frac in (0.25, 0.50, 0.75, 1.00):
        gy = chart_y1 - int(chart_h * frac)
        if gy < chart_y0:
            continue
        draw.rectangle(
            [sc(chart_x0), sc(gy), sc(chart_x1), sc(gy) + max(1, S - 1)],
            fill=C_GRID,
        )
        lv  = int(max_count * frac)
        lbl = str(lv)
        lb  = draw.textbbox((0, 0), lbl, font=fnt_scale)
        lw  = lb[2] - lb[0]
        lh  = lb[3] - lb[1]
        lx  = max(0, sc(chart_x0) - lw - sc(4))
        draw.text((lx, sc(gy) - lh // 2), lbl, fill=C_MUTED + (255,), font=fnt_scale)

    # ── Bars ──────────────────────────────────────────────────────────────────
    label_step = 1 if n <= 14 else 2 if n <= 28 else 7 if n <= 60 else 14

    for i, (day_ts, count) in enumerate(days_data):
        bx0 = chart_x0 + i * (bar_w + gap)
        bx1 = bx0 + bar_w
        bh  = max(1, int(chart_h * count / max_count))
        by0 = chart_y1 - bh
        by1 = chart_y1

        # Clamp strictly within chart area
        bx0 = max(chart_x0, bx0)
        bx1 = min(chart_x1, bx1)
        by0 = max(chart_y0, by0)

        dow   = (day_ts // 86400) % 7
        color = C_BAR_SUN if dow == 0 else C_BAR

        # Solid bar body
        draw.rectangle([sc(bx0), sc(by0), sc(bx1), sc(by1)], fill=color)
        # Rounded top cap
        r = min(sc(4), sc(bar_w) // 2)
        if bh >= 8 and r > 0:
            draw.rounded_rectangle(
                [sc(bx0), sc(by0), sc(bx1), sc(by0) + r * 2],
                radius=r, fill=color,
            )
            # Top highlight strip
            draw.rounded_rectangle(
                [sc(bx0), sc(by0), sc(bx1), sc(by0) + max(S, r)],
                radius=r, fill=C_BAR_HI,
            )

        # Date label at bottom (within footer zone, clamped)
        if i % label_step == 0:
            try:
                day_str = datetime.utcfromtimestamp(day_ts).strftime("%d.%m")
            except Exception:
                day_str = ""
            lb = draw.textbbox((0, 0), day_str, font=fnt_label)
            lw = lb[2] - lb[0]
            lx = sc(bx0) + (sc(bar_w) - lw) // 2
            lx = max(sc(chart_x0), min(lx, sc(chart_x1) - lw))
            draw.text((lx, sc(chart_y1 + 5)), day_str, fill=C_LABEL + (255,), font=fnt_label)

    # ── Footer divider ────────────────────────────────────────────────────────
    draw.rectangle([0, sc(chart_y1), W, sc(chart_y1) + S], fill=C_SEP + (255,))

    # ── Downscale ─────────────────────────────────────────────────────────────
    img_out = img.convert('RGB').resize((W_OUT, H_OUT), Image.LANCZOS)
    buf = _io.BytesIO()
    img_out.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


_TG_CAPTION_LIMIT = 1024   # Telegram caption character limit


def _build_stats_caption(
    chat: types.Chat | None,
    chat_title: str,
    period_label: str,
    rows: list[tuple[int, int]],
    users_map: dict[int, str],
    max_entries: int = 20,
) -> str:
    """Build an HTML photo caption: title + top-N user list.

    Stays within Telegram's 1024-character caption limit by truncating the
    list if necessary (the chart image itself always shows the full top-N).
    """
    chat_title_esc = _html.escape(chat_title)
    period_label_esc = _html.escape(period_label)
    header = (
        f'<tg-emoji emoji-id="{PREMIUM_STATS_EMOJI_ID}">📊</tg-emoji>'
        f' <b>Статистика за {period_label_esc} для чата</b> "{chat_title_esc}"\n\n'
    )
    budget = _TG_CAPTION_LIMIT - len(header)
    lines: list[str] = []
    for i, (user_id, count) in enumerate(rows[:max_entries]):
        name = users_map.get(user_id, f"ID {user_id}")
        name_short = name[:_STATS_NAME_MAXLEN]
        if chat is not None:
            name_html = stats_user_link_html(chat, user_id, name_short)
        else:
            name_html = f"<b>{_html.escape(name_short)}</b>"
        line = (
            f'{i + 1}. '
            f'<tg-emoji emoji-id="{PREMIUM_USER_EMOJI_ID}">👤</tg-emoji> '
            f'{name_html} [<code>{user_id}</code>] — <b>{count}</b>\n'
        )
        if budget - len(line) < 0:
            break
        lines.append(line)
        budget -= len(line)
    return header + "".join(lines)


def _process_image_task(task: dict) -> None:
    """Generate the appropriate chart, build caption, and send a single photo."""
    chat_id: int = task["chat_id"]
    period: str  = task.get("period", "all")
    chart_type: str = task.get("chart_type", "users")
    reply_msg_id: int | None = task.get("reply_msg_id")
    period_label = PERIOD_LABELS.get(period, period)

    try:
        chat_obj  = tg_get_chat(chat_id)
        chat_title = getattr(chat_obj, "title", None) or str(chat_id)
    except Exception:
        chat_obj = None
        chat_title = str(chat_id)

    def _send_error(text: str) -> None:
        try:
            bot.send_message(chat_id, text, reply_to_message_id=reply_msg_id)
        except Exception:
            pass

    # ── Daily chart (weekly 7d or all-time 100d) ─────────────────────────────
    if chart_type in ("daily_7", "daily_100"):
        max_days  = 7 if chart_type == "daily_7" else 100
        since_ts  = _get_period_since("7d") if chart_type == "daily_7" else 0
        if since_ts == 0:
            since_ts = int(time.time()) - 100 * 86400
        days_data = get_stats_by_day(chat_id, since_ts, max_days=max_days)

        if not days_data:
            _send_error("📊 Нет данных для отображения за выбранный период.")
            return

        try:
            img_bytes = _render_daily_chart(days_data, chat_title, period_label)
        except ImportError:
            _send_error("❌ Генерация изображений недоступна (Pillow не установлен).")
            return
        except Exception as exc:
            print(f"[IMAGE RENDER daily] {exc}")
            _send_error("❌ Ошибка при генерации изображения статистики.")
            return

        # Build top-users caption for the same period
        if period == "all":
            chat_stats = GROUP_STATS.get(str(chat_id), {})
            top_rows: list[tuple[int, int]] = sorted(
                [(int(uid), d.get("count", 0)) for uid, d in chat_stats.items()],
                key=lambda x: x[1],
                reverse=True,
            )[:20]
        else:
            raw = get_stats_for_period(chat_id, since_ts)
            top_rows = [(r[0], r[1]) for r in raw][:20]
        users_map = {uid: _lookup_user_display_name(chat_id, uid) for uid, _ in top_rows}
        caption = _build_stats_caption(chat_obj, chat_title, period_label, top_rows, users_map)

        try:
            bot.send_photo(
                chat_id,
                _io.BytesIO(img_bytes),
                caption=caption,
                parse_mode='HTML',
                reply_to_message_id=reply_msg_id,
            )
        except Exception as exc:
            print(f"[IMAGE SEND daily] {exc}")
        return

    # ── User bar chart ────────────────────────────────────────────────────────
    if period == "all":
        chat_stats = GROUP_STATS.get(str(chat_id), {})
        rows: list[tuple[int, int]] = sorted(
            [(int(uid), d.get("count", 0)) for uid, d in chat_stats.items()],
            key=lambda x: x[1],
            reverse=True,
        )[:20]
    else:
        since_ts = _get_period_since(period)
        raw = get_stats_for_period(chat_id, since_ts)
        rows = [(r[0], r[1]) for r in raw][:20]

    if not rows:
        _send_error("📊 Нет данных для отображения за выбранный период.")
        return

    users_map = {uid: _lookup_user_display_name(chat_id, uid) for uid, _ in rows}

    try:
        img_bytes = _render_stats_image(rows, chat_title, period_label, users_map)
    except ImportError:
        _send_error("❌ Генерация изображений недоступна (Pillow не установлен).")
        return
    except Exception as exc:
        print(f"[IMAGE RENDER] {exc}")
        _send_error("❌ Ошибка при генерации изображения статистики.")
        return

    caption = _build_stats_caption(chat_obj, chat_title, period_label, rows, users_map)

    try:
        bot.send_photo(
            chat_id,
            _io.BytesIO(img_bytes),
            caption=caption,
            parse_mode='HTML',
            reply_to_message_id=reply_msg_id,
        )
    except Exception as exc:
        print(f"[IMAGE SEND] {exc}")


def _image_worker() -> None:
    """Daemon thread: consumes image-generation tasks from the queue."""
    while True:
        task = _IMG_TASK_QUEUE.get()
        try:
            _process_image_task(task)
        except Exception as e:
            print(f"[IMAGE WORKER] Unexpected error: {e}")


_IMAGE_THREAD = threading.Thread(target=_image_worker, daemon=True)
_IMAGE_THREAD.start()


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("pm_settings_open:"))
def cb_pm_settings_open(call: types.CallbackQuery):
    """Открытие настроек конкретной группы из ЛС (по кнопке из /settings в ЛС)."""
    if _is_duplicate_callback_query(call):
        return
    bot.answer_callback_query(call.id)
    try:
        chat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return

    if call.message.chat.type != 'private':
        return

    user = call.from_user
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        if err:
            try:
                bot.send_message(call.message.chat.id, premium_prefix(err), parse_mode='HTML')
            except Exception:
                pass
        return

    try:
        chat = tg_get_chat(chat_id)
        title = chat.title or str(chat_id)
    except Exception:
        title = str(chat_id)

    get_chat_settings(chat_id)

    emoji_settings = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'
    text = (
        f"{emoji_settings} <b>Настройки чата</b>\n"
        f"<b>Чат:</b> {_html.escape(title)}\n\n"
        "Выберите раздел для настройки:"
    )
    kb = _build_settings_main_keyboard(chat_id, viewer_user=user)

    raw_edit_message_with_keyboard(
        call.message.chat.id,
        call.message.message_id,
        text,
        kb
    )


@bot.callback_query_handler(func=lambda call: bool(call.data) and not call.data.startswith((
    "gstats_",
    "punish_un:",
    "modlist:",
    "astchat:",
    "astnav:",
    "astclose:",
    "st_",
    "stw:",
    "stf:",
    "star:",
    "stlc:",
    "p:",
    "rules:",
    "del:",
    "approve_group:",
    "deny_group:",
    "pm_settings_open:",
    "pm_settings_back",
    "start:newguest",
    "clone_disable:",
    "clone_enable:",
    "guestbot:",
)))
def callback_handler(call: types.CallbackQuery):
    if _is_duplicate_callback_query(call):
        return
    add_stat_message(call.message)
    _remember_owner_user_id(call.from_user)

    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    if data.startswith("devcontact:"):
        action = data.split(":", 1)[1]

        if action == "back":
            try:
                raw_delete_message(chat_id, msg_id)
            except Exception:
                pass
            _send_start_menu(chat_id, call.from_user)
            return bot.answer_callback_query(call.id)

        if action == "send":
            try:
                raw_delete_message(chat_id, msg_id)
            except Exception:
                pass

            prompt = bot.send_message(
                chat_id,
                _dev_contact_prompt_text(),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=_dev_contact_prompt_kb(),
            )
            PENDING_DEV_CONTACT_FROM_USER[call.from_user.id] = {
                "prompt_message_id": prompt.message_id,
                "created_at": int(time.time()),
            }
            return bot.answer_callback_query(call.id)

        if action == "cancel":
            PENDING_DEV_CONTACT_FROM_USER.pop(call.from_user.id, None)
            PENDING_DEV_REPLY_FROM_OWNER.pop(call.from_user.id, None)
            try:
                raw_delete_message(chat_id, msg_id)
            except Exception:
                pass
            emoji_cancel = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
            bot.send_message(chat_id, f"<i>{emoji_cancel} Отправка отменена.</i>", parse_mode='HTML')
            return bot.answer_callback_query(call.id)

        if action == "reply":
            # User clicks "Ответить" on an owner-sent message; keep the message intact
            prompt = bot.send_message(
                chat_id,
                _dev_contact_prompt_text(),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=_dev_contact_prompt_kb(),
            )
            PENDING_DEV_CONTACT_FROM_USER[call.from_user.id] = {
                "prompt_message_id": prompt.message_id,
                "created_at": int(time.time()),
            }
            return bot.answer_callback_query(call.id)

        return bot.answer_callback_query(call.id)

    if data.startswith("devmsg:"):
        if not is_owner(call.from_user):
            return bot.answer_callback_query(call.id, "Недоступно.", show_alert=True)

        parts = data.split(":")
        if len(parts) != 3:
            return bot.answer_callback_query(call.id)

        action = parts[1]
        try:
            item_id = int(parts[2])
        except Exception:
            return bot.answer_callback_query(call.id)

        item = _dev_contact_find_item(item_id)
        if item is None:
            return bot.answer_callback_query(call.id, "Сообщение не найдено.", show_alert=False)

        if action == "ignore":
            item["status"] = "ignored"
            save_dev_contact_inbox()
            try:
                raw_delete_message(chat_id, msg_id)
            except Exception:
                pass
            return bot.answer_callback_query(call.id, "Сообщение проигнорировано.", show_alert=False)

        if action == "reply":
            status = item.get("status") or "new"
            if status != "new":
                return bot.answer_callback_query(call.id, "Это сообщение уже обработано.", show_alert=False)

            try:
                raw_delete_message(chat_id, msg_id)
            except Exception:
                pass

            prompt = bot.send_message(
                chat_id,
                _dev_contact_prompt_text(),
                parse_mode='HTML',
                disable_web_page_preview=True,
                reply_markup=_dev_contact_prompt_kb(),
            )

            PENDING_DEV_REPLY_FROM_OWNER[call.from_user.id] = {
                "item_id": item_id,
                "target_user_id": int(item.get("user_id") or 0),
                "prompt_message_id": prompt.message_id,
                "created_at": int(time.time()),
            }
            return bot.answer_callback_query(call.id)

        return bot.answer_callback_query(call.id)

    if data.startswith("bc2:"):
        if not is_owner(call.from_user):
            return bot.answer_callback_query(call.id, "Недоступно.", show_alert=True)
        if call.message.chat.type != 'private':
            return bot.answer_callback_query(call.id)

        parts = data.split(":")
        if len(parts) != 3:
            return bot.answer_callback_query(call.id)

        action = parts[1]
        try:
            draft_id = int(parts[2])
        except Exception:
            return bot.answer_callback_query(call.id)

        draft = BROADCAST_DRAFTS.get(call.from_user.id)
        if not draft or int(draft.get("id") or 0) != draft_id:
            return bot.answer_callback_query(call.id, "Черновик не найден или устарел.", show_alert=False)

        if action in ("text", "media", "buttons"):
            old_pending = BROADCAST_PENDING_INPUT.pop(call.from_user.id, None)
            if old_pending:
                try:
                    old_prompt = int(old_pending.get("prompt_message_id") or 0)
                    if old_prompt > 0:
                        bot.delete_message(chat_id, old_prompt)
                except Exception:
                    pass

            if action == "text":
                prompt_text = (
                    "<b>Отправьте текст рассылки.</b>\n\n"
                    "Поддерживается обычное форматирование Telegram и кастомные теги бота."
                )
            elif action == "media":
                prompt_text = "<b>Отправьте медиа для рассылки.</b>\n\nПоддерживается фото/видео/файл/музыка/gif."
            else:
                prompt_text = (
                    "<b>Отправьте кнопки для рассылки.</b>\n\n"
                    "Формат: <code>Текст - ссылка</code> или <code>Текст - popup: сообщение</code>."
                )

            prompt = bot.send_message(chat_id, prompt_text, parse_mode='HTML', disable_web_page_preview=True)
            BROADCAST_PENDING_INPUT[call.from_user.id] = {
                "mode": action,
                "draft_id": draft_id,
                "prompt_message_id": prompt.message_id,
                "created_at": int(time.time()),
            }
            return bot.answer_callback_query(call.id, "Ожидаю сообщение в чате.", show_alert=False)

        if action == "preview":
            html_text = build_html_from_text_custom(draft.get("text_custom") or "")
            media = draft.get("media") or []
            buttons = draft.get("buttons") or {"rows": [], "popups": []}

            rows = buttons.get("rows") or []
            popups = buttons.get("popups") or []
            kb_preview = build_inline_keyboard_for_payload("broadcast", call.message.chat.id, rows, popups, call.from_user.id)

            if not html_text and not media and not rows:
                return bot.answer_callback_query(call.id, "Черновик пустой.", show_alert=True)

            bot.send_message(chat_id, "<b>Предпросмотр текущего сообщения:</b>", parse_mode='HTML', disable_web_page_preview=True)
            _send_payload(chat_id, html_text, media, reply_markup=kb_preview)
            return bot.answer_callback_query(call.id)

        if action == "reset":
            BROADCAST_DRAFTS[call.from_user.id] = _broadcast_new_draft()
            draft = BROADCAST_DRAFTS[call.from_user.id]

            text = _broadcast_render_panel_text(call.from_user.id)
            kb = _build_broadcast_panel_keyboard(int(draft.get("id") or 0))
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)
            except Exception:
                pass
            return bot.answer_callback_query(call.id, "Черновик очищен.", show_alert=False)

        if action == "cancel":
            BROADCAST_DRAFTS.pop(call.from_user.id, None)
            pending = BROADCAST_PENDING_INPUT.pop(call.from_user.id, None)
            if pending:
                try:
                    prompt_id = int(pending.get("prompt_message_id") or 0)
                    if prompt_id > 0:
                        bot.delete_message(chat_id, prompt_id)
                except Exception:
                    pass
            try:
                bot.edit_message_text(
                    "<i>Рассылка отменена.</i>",
                    chat_id=chat_id,
                    message_id=msg_id,
                    parse_mode='HTML',
                )
            except Exception:
                pass
            return bot.answer_callback_query(call.id, "Отменено", show_alert=False)

        if action == "send":
            html_text = build_html_from_text_custom(draft.get("text_custom") or "")
            media = draft.get("media") or []
            buttons = draft.get("buttons") or {"rows": [], "popups": []}
            rows = buttons.get("rows") or []

            if not html_text and not media and not rows:
                return bot.answer_callback_query(call.id, "Черновик пустой.", show_alert=True)

            targets = _broadcast_collect_targets()

            BROADCAST_DRAFTS.pop(call.from_user.id, None)
            BROADCAST_PENDING_INPUT.pop(call.from_user.id, None)

            operation_id, queue_size = enqueue_operation(
                "broadcast_send",
                {
                    "panel_chat_id": chat_id,
                    "panel_message_id": msg_id,
                    "owner_user_id": call.from_user.id,
                    "html_text": html_text,
                    "media": media,
                    "buttons": buttons,
                    "targets": targets,
                },
            )

            summary = (
                f'<tg-emoji emoji-id="{EMOJI_LOG_PM_ID}">📢</tg-emoji> <b>Рассылка поставлена в очередь.</b>\n\n'
                f"<b>Операция:</b> <code>#{operation_id}</code>\n"
                f"<b>Получателей:</b> <code>{len(targets)}</code>\n"
                f"<b>Позиция в очереди:</b> <code>{queue_size}</code>"
            )

            try:
                bot.edit_message_text(
                    summary,
                    chat_id=chat_id,
                    message_id=msg_id,
                    parse_mode='HTML',
                    disable_web_page_preview=True,
                )
            except Exception:
                bot.send_message(chat_id, summary, parse_mode='HTML', disable_web_page_preview=True)

            return bot.answer_callback_query(call.id, "Рассылка поставлена в очередь.", show_alert=False)

        return bot.answer_callback_query(call.id)

    if data.startswith("bc:"):
        return bot.answer_callback_query(call.id, "Используйте новый интерфейс /broadcast", show_alert=False)

    if data.startswith("pm2:"):
        if not is_owner(call.from_user):
            return bot.answer_callback_query(call.id, "Недоступно.", show_alert=True)
        if call.message.chat.type != 'private':
            return bot.answer_callback_query(call.id)

        parts = data.split(":")
        if len(parts) != 3:
            return bot.answer_callback_query(call.id)

        action = parts[1]
        try:
            draft_id = int(parts[2])
        except Exception:
            return bot.answer_callback_query(call.id)

        draft = SENDPM_DRAFTS.get(call.from_user.id)
        if not draft or int(draft.get("id") or 0) != draft_id:
            return bot.answer_callback_query(call.id, "Черновик не найден или устарел.", show_alert=False)

        if action in ("text", "media", "buttons"):
            old_pending = SENDPM_PENDING_INPUT.pop(call.from_user.id, None)
            if old_pending:
                try:
                    old_prompt = int(old_pending.get("prompt_message_id") or 0)
                    if old_prompt > 0:
                        bot.delete_message(chat_id, old_prompt)
                except Exception:
                    pass

            if action == "text":
                prompt_text = (
                    "<b>Отправьте текст сообщения.</b>\n\n"
                    "Поддерживается обычное форматирование Telegram."
                )
            elif action == "media":
                prompt_text = "<b>Отправьте медиа для сообщения.</b>\n\nПоддерживается фото/видео/файл/музыка/gif."
            else:
                prompt_text = (
                    "<b>Отправьте кнопки для сообщения.</b>\n\n"
                    "Формат: <code>Текст - ссылка</code> или <code>Текст - popup: сообщение</code>."
                )

            prompt = bot.send_message(chat_id, prompt_text, parse_mode='HTML', disable_web_page_preview=True)
            SENDPM_PENDING_INPUT[call.from_user.id] = {
                "mode": action,
                "draft_id": draft_id,
                "prompt_message_id": prompt.message_id,
                "created_at": int(time.time()),
            }
            return bot.answer_callback_query(call.id, "Ожидаю сообщение в чате.", show_alert=False)

        if action == "preview":
            html_text = build_html_from_text_custom(draft.get("text_custom") or "")
            media = draft.get("media") or []
            buttons = draft.get("buttons") or {"rows": [], "popups": []}
            rows = buttons.get("rows") or []
            popups = buttons.get("popups") or []
            # Preview is shown in owner's chat; viewer is the owner
            kb_preview = build_inline_keyboard_for_payload("sendpm", call.message.chat.id, rows, popups, call.from_user.id)

            if not html_text and not media and not rows:
                return bot.answer_callback_query(call.id, "Черновик пустой.", show_alert=True)

            bot.send_message(chat_id, "<b>Предпросмотр текущего сообщения:</b>", parse_mode='HTML', disable_web_page_preview=True)
            _send_payload(chat_id, html_text, media, reply_markup=kb_preview)
            return bot.answer_callback_query(call.id)

        if action == "reset":
            target_user_id = int(draft.get("target_user_id") or 0)
            target_name = draft.get("target_name") or ""
            target_username = draft.get("target_username") or ""
            SENDPM_DRAFTS[call.from_user.id] = _sendpm_new_draft(target_user_id, target_name, target_username)
            draft = SENDPM_DRAFTS[call.from_user.id]

            text = _sendpm_render_panel_text(call.from_user.id)
            kb = _build_sendpm_panel_keyboard(int(draft.get("id") or 0))
            try:
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', disable_web_page_preview=True, reply_markup=kb)
            except Exception:
                pass
            return bot.answer_callback_query(call.id, "Черновик очищен.", show_alert=False)

        if action == "cancel":
            SENDPM_DRAFTS.pop(call.from_user.id, None)
            pending = SENDPM_PENDING_INPUT.pop(call.from_user.id, None)
            if pending:
                try:
                    prompt_id = int(pending.get("prompt_message_id") or 0)
                    if prompt_id > 0:
                        bot.delete_message(chat_id, prompt_id)
                except Exception:
                    pass
            try:
                bot.edit_message_text(
                    "<i>Отправка отменена.</i>",
                    chat_id=chat_id,
                    message_id=msg_id,
                    parse_mode='HTML',
                )
            except Exception:
                pass
            return bot.answer_callback_query(call.id, "Отменено", show_alert=False)

        if action == "send":
            html_text = build_html_from_text_custom(draft.get("text_custom") or "")
            media = draft.get("media") or []
            buttons = draft.get("buttons") or {"rows": [], "popups": []}
            rows = (buttons.get("rows") or [])

            if not html_text and not media and not rows:
                return bot.answer_callback_query(call.id, "Черновик пустой.", show_alert=True)

            target_user_id = int(draft.get("target_user_id") or 0)
            if target_user_id <= 0:
                return bot.answer_callback_query(call.id, "Получатель не задан.", show_alert=True)

            SENDPM_DRAFTS.pop(call.from_user.id, None)
            SENDPM_PENDING_INPUT.pop(call.from_user.id, None)

            popups = (buttons.get("popups") or [])
            # chat_id = target_user_id (message appears in their private chat)
            # viewer_user_id = target_user_id (they are the ones interacting with the buttons)
            kb_msg = build_inline_keyboard_for_payload("sendpm", target_user_id, rows, popups, target_user_id)
            # Always attach "Ответить" button so the user can reply
            if kb_msg:
                kb_msg.row(types.InlineKeyboardButton(
                    "Ответить",
                    callback_data="devcontact:reply",
                    icon_custom_emoji_id=str(EMOJI_REPLY_BTN_ID),
                ))
            else:
                kb_msg = _sendpm_reply_keyboard()

            try:
                _send_payload(target_user_id, html_text, media, reply_markup=kb_msg)
                sent_ok = True
            except Exception:
                try:
                    bot.send_message(
                        target_user_id,
                        html_text or "Сообщение от разработчика",
                        parse_mode='HTML',
                        disable_web_page_preview=True,
                        reply_markup=_sendpm_reply_keyboard(),
                    )
                    sent_ok = True
                except Exception:
                    sent_ok = False

            emoji_pm = f'<tg-emoji emoji-id="{EMOJI_SCOPE_PM_ID}">💬</tg-emoji>'
            if sent_ok:
                summary = f"{emoji_pm} <b>Сообщение отправлено пользователю.</b>"
            else:
                summary = premium_prefix("Не удалось доставить сообщение пользователю.")

            try:
                bot.edit_message_text(summary, chat_id=chat_id, message_id=msg_id, parse_mode='HTML', disable_web_page_preview=True)
            except Exception:
                bot.send_message(chat_id, summary, parse_mode='HTML', disable_web_page_preview=True)

            return bot.answer_callback_query(call.id)

        return bot.answer_callback_query(call.id)

    if data == 'start:close':
        START_MENU_STATE.pop((chat_id, msg_id), None)
        try:
            raw_delete_message(chat_id, msg_id)
        finally:
            return bot.answer_callback_query(call.id)

    if data in ('start:home', 'start:commands', 'start:about', 'start:usage'):
        state = START_MENU_STATE.get((chat_id, msg_id))
        if not state:
            return bot.answer_callback_query(call.id, "Меню устарело, открой /start заново.", show_alert=False)

        owner_id = int(state.get('user_id') or 0)
        if call.from_user.id != owner_id:
            return bot.answer_callback_query(call.id, "Это меню не для вас.", show_alert=False)

        show_owner_button = bool(state.get('show_owner_button'))

        if data == 'start:home':
            text = _build_start_home_text(call.from_user)
            kb = _build_start_home_keyboard(show_owner_button=show_owner_button)
        elif data == 'start:commands':
            text = _build_start_commands_text(call.from_user)
            kb = _build_start_commands_keyboard()
        elif data == 'start:about':
            text = _build_start_about_text()
            kb = _build_start_back_keyboard('start:home')
        else:
            text = _build_start_usage_text()
            kb = _build_start_back_keyboard('start:commands')

        bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        return bot.answer_callback_query(call.id)

    if data == 'start:contact':
        state = START_MENU_STATE.get((chat_id, msg_id))
        if not state:
            return bot.answer_callback_query(call.id, "Меню устарело, открой /start заново.", show_alert=False)

        owner_id = int(state.get('user_id') or 0)
        if call.from_user.id != owner_id:
            return bot.answer_callback_query(call.id, "Это меню не для вас.", show_alert=False)

        START_MENU_STATE.pop((chat_id, msg_id), None)
        try:
            raw_delete_message(chat_id, msg_id)
        except Exception:
            pass

        bot.send_message(
            chat_id,
            _dev_contact_intro_text(),
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=_dev_contact_intro_kb(),
        )
        return bot.answer_callback_query(call.id)

    if data == 'start:newmsgs':
        state = START_MENU_STATE.get((chat_id, msg_id))
        if not state:
            return bot.answer_callback_query(call.id, "Меню устарело, открой /start заново.", show_alert=False)

        owner_id = int(state.get('user_id') or 0)
        if call.from_user.id != owner_id:
            return bot.answer_callback_query(call.id, "Это меню не для вас.", show_alert=False)

        if not is_owner(call.from_user):
            return bot.answer_callback_query(call.id, "Кнопка доступна только разработчику.", show_alert=False)

        _show_dev_contact_new_messages(call.from_user.id)
        return bot.answer_callback_query(call.id, "Отправляю новые сообщения…", show_alert=False)

    bot.answer_callback_query(call.id)


# ==== ОДОБРЕНИЕ / ОТКАЗ ГРУПП ====

@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("approve_group:"))
def cb_approve_group(call: types.CallbackQuery):
    """Разработчик одобрил группу."""
    if _is_duplicate_callback_query(call):
        return
    if not is_owner(call.from_user):
        return bot.answer_callback_query(call.id, "Только разработчик может одобрять группы.", show_alert=True)
    
    try:
        chat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Ошибка обработки данных.", show_alert=True)
    
    pending = PENDING_GROUPS.get(str(chat_id))
    if not pending:
        return bot.answer_callback_query(call.id, "Группа уже обработана.", show_alert=True)
    
    # Одобряем группу
    approve_pending_group(chat_id)

    # Отвечаем немедленно до API-вызовов
    bot.answer_callback_query(call.id, "Группа одобрена!", show_alert=False)

    emoji_ok = f'<tg-emoji emoji-id="{EMOJI_SENT_OK_ID}">✅</tg-emoji>'

    # Обновляем сообщение в ЛС разработчика
    try:
        bot.edit_message_text(
            f"<b>{emoji_ok} Группа одобрена</b>\n\n"
            f"<b>Группа:</b> {_html.escape(pending.get('title', 'Unknown'))}\n"
            f"<b>ID:</b> <code>{chat_id}</code>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
    except Exception:
        pass
    
    # Отправляем сообщение в группу
    try:
        chat_obj = bot.get_chat(chat_id)
        if getattr(chat_obj, "type", None) == "channel":
            return
    except Exception:
        pass
    try:
        bot.send_message(
            chat_id,
            f"{emoji_ok} <b>Группа одобрена!</b>\n\n"
            f"Бот готов к работе. Для <b>стабильной работы всех функций</b> выдайте боту права администратора:\n\n"
            f"• <b>Удалять сообщения</b>\n"
            f"• <b>Закреплять сообщения</b>\n"
            f"• <b>Ограничивать участников</b>\n"
            f"• <b>Приглашать пользователей</b>\n"
            f"• <b>Управлять ссылками-приглашениями</b>\n"
            f"• <b>Назначать администраторов</b>\n\n"
            f"<i>Для этого откройте настройки группы → Администраторы → выберите бота и выдайте права.</i>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение в группу {chat_id}: {e}")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("deny_group:"))
def cb_deny_group(call: types.CallbackQuery):
    """Разработчик запретил группе доступ."""
    if _is_duplicate_callback_query(call):
        return
    if not is_owner(call.from_user):
        return bot.answer_callback_query(call.id, "Только разработчик может отказывать группам.", show_alert=True)
    
    try:
        chat_id = int(call.data.split(":")[1])
    except (ValueError, IndexError):
        return bot.answer_callback_query(call.id, "Ошибка обработки данных.", show_alert=True)
    
    pending = PENDING_GROUPS.get(str(chat_id))
    if not pending:
        return bot.answer_callback_query(call.id, "Группа уже обработана.", show_alert=True)
    
    # Запрещаем доступ
    deny_pending_group(chat_id)

    # Отвечаем немедленно до API-вызовов
    bot.answer_callback_query(call.id, "Группе запрещен доступ. Бот пытается выйти.", show_alert=False)

    emoji_cancel = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_CANCEL_ID}">❌</tg-emoji>'
    
    # Обновляем сообщение в ЛС разработчика
    try:
        bot.edit_message_text(
            f"<b>{emoji_cancel} Группе запрещен доступ</b>\n\n"
            f"<b>Группа:</b> {_html.escape(pending.get('title', 'Unknown'))}\n"
            f"<b>ID:</b> <code>{chat_id}</code>",
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            parse_mode='HTML'
        )
    except Exception:
        pass
    
    # Пытаемся выйти из группы
    try:
        bot.leave_chat(chat_id)
    except Exception as e:
        print(f"[ERROR] Не удалось выйти из группы {chat_id}: {e}")
        # Отправляем сообщение в группу, чтобы не работать
        try:
            bot.send_message(
                chat_id,
                f"<b>{emoji_cancel} Доступ запрещен</b>\n\nБот не имеет разрешения на работу в этой группе.",
                parse_mode='HTML'
            )
        except Exception:
            pass


@bot.my_chat_member_handler()
def handle_my_chat_member(update: types.ChatMemberUpdated):
    """Обработчик событий когда бот добавляется/удаляется из групп."""
    chat_id = update.chat.id
    new_status = update.new_chat_member.status

    if new_status in ("member", "administrator"):
        # Skip channels — only process groups/supergroups
        chat_type = getattr(update.chat, "type", None)
        if chat_type == "channel":
            return

        # Если бот уже был в группе (смена прав администратора) — ничего не делаем
        old_status = update.old_chat_member.status
        if old_status in ("member", "administrator"):
            return

        # Бот добавлен в группу — проверяем, нет ли уже другого бота
        try:
            me = get_bot_me()
            my_id = me.id
            my_username = me.username or ""
        except Exception:
            my_id = 0
            my_username = ""

        ok = assign_bot_to_chat(chat_id, my_id, my_username)
        if not ok:
            # Группа уже занята другим клоном — покидаем
            try:
                bot.leave_chat(chat_id)
                print(
                    f"[CLONE] Покинул группу {chat_id}: уже обслуживается другим ботом."
                )
            except Exception as e:
                print(f"[CLONE] Не удалось покинуть группу {chat_id}: {e}")
            return

        # Группа добавлена или уже наша — обычная логика одобрения
        adder = update.from_user
        if adder:
            add_pending_group(chat_id, update.chat.title or "", adder)
            notify_dev_about_new_group(chat_id, update.chat.title or "", adder)

    elif new_status == "left":
        # Бот удалён из группы — освобождаем привязку
        unassign_bot_from_chat(chat_id)
        deny_pending_group(chat_id)
        print(f"[INFO] Бот удалён из группы {chat_id}")

# ==== ФОЛЛБЭК ====

@bot.message_handler(
    func=lambda m: True,
    content_types=['text', 'photo', 'video', 'document', 'audio', 'animation', 'voice', 'video_note', 'sticker'],
)
def all_other(m: types.Message):
    add_stat_message(m)
    # всё остальное учитываем в статистике и БД пользователей


__all__ = [name for name in globals() if not name.startswith('__')]
