from __future__ import annotations

import html as _html
import os as _os
import re as _re
import subprocess as _subprocess
import sys as _sys
import threading as _threading
from typing import Optional

import telebot as _tb

from config import (
    DATA_DIR,
    bot,
    types,
    EMOJI_LIST_ID,
    EMOJI_ADMIN_RIGHTS_ID,
    EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID,
    EMOJI_ROLE_SETTINGS_SAVE_ID,
    EMOJI_ROLE_SETTINGS_CANCEL_ID,
    EMOJI_ROLE_SETTINGS_SENT_PM_ID,
    EMOJI_SENT_OK_ID,
    PREMIUM_PREFIX_EMOJI_ID,
)
from helpers import should_ignore_text_triggers, is_owner as _global_is_owner
from persistence import (
    create_guest_bot,
    delete_guest_bot,
    get_guest_bot_by_id,
    list_guest_bots,
    set_guest_bot_enabled,
    set_guest_bot_runtime_pid,
    update_guest_bot_modules,
)


_TOKEN_RE = _re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{35,})\b")
_GUEST_RUNTIME_SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "guest_runtime.py")
_MAX_COMMAND_RESPONSE_LEN = 3500
_CMD_MAX_NAME_LEN = 30

_PENDING_TOKEN_USERS: set[int] = set()
_PENDING_TOKEN_PROMPTS: dict[int, tuple[int, int]] = {}

_GUEST_PROCESSES: dict[int, _subprocess.Popen] = {}
_GUEST_PROCESSES_LOCK = _threading.Lock()

# Emoji IDs
_EMOJI_CONNECT = "5226945370684140473"
_EMOJI_OK = str(EMOJI_SENT_OK_ID)
_EMOJI_CANCEL = str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
_EMOJI_BACK = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
_EMOJI_LIST = str(EMOJI_LIST_ID)
_EMOJI_SAVE = str(EMOJI_ROLE_SETTINGS_SAVE_ID)
_EMOJI_PM = str(EMOJI_ROLE_SETTINGS_SENT_PM_ID)


def _pe(emoji_id: object, fallback: str = "•") -> str:
    """Return premium emoji HTML tag."""
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def _btn(
    text: str,
    *,
    callback_data: str | None = None,
    url: str | None = None,
    icon_id: object | None = None,
) -> types.InlineKeyboardButton:
    btn = types.InlineKeyboardButton(text, callback_data=callback_data, url=url)
    if icon_id is not None:
        try:
            btn.icon_custom_emoji_id = str(icon_id)
        except Exception:
            pass
    return btn


def _is_owner(user: types.User | None) -> bool:
    return bool(_global_is_owner(user))


def _normalize_command_name(name: str) -> str:
    return str(name or "").strip().lower()


def _is_command_name_valid(name: str) -> bool:
    """Accept all characters; require non-empty, no spaces, max length."""
    stripped = _normalize_command_name(name)
    return 0 < len(stripped) <= _CMD_MAX_NAME_LEN and " " not in stripped


def _guest_bot_username(entry: dict) -> str:
    return _html.escape((entry.get("bot_username") or "").strip().lstrip("@"))


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _is_runtime_alive(guest_bot_id: int) -> bool:
    with _GUEST_PROCESSES_LOCK:
        proc = _GUEST_PROCESSES.get(int(guest_bot_id))
    return proc is not None and proc.poll() is None


def _stop_guest_runtime(guest_bot_id: int) -> None:
    with _GUEST_PROCESSES_LOCK:
        proc = _GUEST_PROCESSES.get(int(guest_bot_id))
    if not proc:
        set_guest_bot_runtime_pid(int(guest_bot_id), 0)
        return
    try:
        proc.terminate()
    except Exception:
        pass
    with _GUEST_PROCESSES_LOCK:
        _GUEST_PROCESSES.pop(int(guest_bot_id), None)
    set_guest_bot_runtime_pid(int(guest_bot_id), 0)


def _launch_guest_runtime(entry: dict) -> bool:
    guest_bot_id = int(entry.get("id") or 0)
    if not guest_bot_id:
        return False
    if _is_runtime_alive(guest_bot_id):
        return True

    token = str(entry.get("bot_token") or "").strip()
    if not token:
        return False

    env = _os.environ.copy()
    env["BOT_TOKEN"] = token
    env["DATA_DIR"] = DATA_DIR
    env["BOT_THREADS"] = "4"

    log_path = _os.path.join(DATA_DIR, f"guest_bot_{guest_bot_id}.log")
    log_file = None
    try:
        log_file = open(log_path, "a")
    except OSError:
        pass
    stdout_target = log_file if log_file is not None else _subprocess.DEVNULL

    try:
        proc = _subprocess.Popen(
            [_sys.executable, _GUEST_RUNTIME_SCRIPT],
            env=env,
            stdout=stdout_target,
            stderr=stdout_target,
        )
    except Exception:
        if log_file is not None:
            log_file.close()
        return False

    if log_file is not None:
        log_file.close()

    with _GUEST_PROCESSES_LOCK:
        _GUEST_PROCESSES[guest_bot_id] = proc
    set_guest_bot_runtime_pid(guest_bot_id, int(proc.pid))
    return True


def autostart_guest_bots() -> None:
    for entry in list_guest_bots():
        if not bool(entry.get("enabled")):
            continue
        _launch_guest_runtime(entry)


def _guest_bots_menu_text(user: types.User) -> str:
    bots = list_guest_bots(owner_user_id=int(user.id))
    hdr = _pe(_EMOJI_LIST, "📋")
    if not bots:
        return (
            f"{hdr} <b>Guest-боты</b>\n\n"
            "Подключите отдельного бота как гостевого — он будет отвечать на команды в ваших группах.\n\n"
            "<i>Нет подключённых ботов.</i>\n"
            "Нажмите «Подключить гостевого бота» и отправьте токен BotFather."
        )

    lines = [
        f"{hdr} <b>Guest-боты</b>\n",
        "Подключите отдельного бота как гостевого — он будет отвечать на команды в ваших группах.\n",
        f"<b>Подключённых ботов:</b> <code>{len(bots)}</code>\n",
    ]
    for i, item in enumerate(bots, 1):
        guest_id = int(item.get("id") or 0)
        uname = _html.escape(item.get("bot_username") or "unknown")
        enabled = bool(item.get("enabled"))
        status_icon = _pe(_EMOJI_OK, "✅") if enabled else _pe(_EMOJI_CANCEL, "❌")
        status_text = "активен" if enabled else "отключён"
        lines.append(
            f"{i}. {status_icon} <b>@{uname}</b> [<code>{guest_id}</code>] · <i>{status_text}</i>"
        )
    return "\n".join(lines)


def _guest_bots_menu_kb(user: types.User, selected_guest_id: int = 0) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item in list_guest_bots(owner_user_id=int(user.id)):
        guest_id = int(item.get("id") or 0)
        uname = (item.get("bot_username") or str(guest_id)).strip().lstrip("@")
        kb.add(_btn(f"@{uname}", callback_data=f"guestbot:select:{guest_id}"))
        if selected_guest_id and guest_id == int(selected_guest_id):
            btn_unbind = _btn("Отвязать", callback_data=f"guestbot:unbind_ask:{guest_id}", icon_id=_EMOJI_CANCEL)
            try:
                btn_unbind.style = "danger"
            except Exception:
                pass
            kb.add(btn_unbind)
    kb.add(_btn("Подключить гостевого бота", callback_data="guestbot:create", icon_id=_EMOJI_CONNECT))
    kb.add(_btn("Назад", callback_data="start:home", icon_id=_EMOJI_BACK))
    return kb


def _manage_bot_text(entry: dict) -> str:
    uname = _html.escape((entry.get("bot_username") or "").strip().lstrip("@"))
    enabled = bool(entry.get("enabled"))
    status_icon = _pe(_EMOJI_OK, "✅") if enabled else _pe(_EMOJI_CANCEL, "❌")
    hdr = _pe(_EMOJI_LIST, "📋")
    return "\n".join([
        f"{hdr} <b>Команды @{uname}</b>\n",
        f"<b>Статус:</b> {status_icon} {'активен' if enabled else 'отключён'}\n",
        f"\nУправление командами перенесено в гостевого бота @{uname}.",
        f"\n<i>Откройте @{uname} и нажмите /start.</i>",
    ])


def _manage_bot_kb(entry: dict) -> types.InlineKeyboardMarkup:
    guest_id = int(entry.get("id") or 0)
    uname = (entry.get("bot_username") or str(guest_id)).strip().lstrip("@")
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(_btn(f"Открыть @{uname}", url=f"https://t.me/{uname}", icon_id=_EMOJI_PM))
    kb.add(_btn("Отвязать бота", callback_data=f"guestbot:unbind_ask:{guest_id}", icon_id=_EMOJI_CANCEL))
    kb.add(_btn("Назад", callback_data="guestbot:list", icon_id=_EMOJI_BACK))
    return kb




def _show_guest_bots_menu(chat_id: int, user: types.User) -> None:
    bot.send_message(
        chat_id,
        _guest_bots_menu_text(user),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_guest_bots_menu_kb(user, selected_guest_id=0),
    )


def _try_delete_message(chat_id: int, msg_id: int | None) -> None:
    if not msg_id:
        return
    try:
        bot.delete_message(chat_id, int(msg_id))
    except Exception:
        pass


def _clear_guest_creation_state(user_id: int, chat_id: int | None = None) -> None:
    uid = int(user_id)
    _PENDING_TOKEN_USERS.discard(uid)
    prompt = _PENDING_TOKEN_PROMPTS.pop(uid, None)
    if prompt and chat_id is not None:
        try:
            prompt_chat_id, prompt_msg_id = int(prompt[0]), int(prompt[1])
            if prompt_chat_id == int(chat_id):
                _try_delete_message(prompt_chat_id, prompt_msg_id)
        except Exception:
            pass


def _send_guest_creation_prompt(chat_id: int, user_id: int, body: str) -> None:
    uid = int(user_id)
    prev = _PENDING_TOKEN_PROMPTS.get(uid)
    if prev:
        try:
            prev_chat_id, prev_msg_id = int(prev[0]), int(prev[1])
        except Exception:
            prev_chat_id, prev_msg_id = 0, 0
        if prev_chat_id == int(chat_id):
            _try_delete_message(prev_chat_id, prev_msg_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(_btn("Открыть BotFather", url="https://t.me/BotFather", icon_id=_EMOJI_PM))
    kb.add(_btn("Отмена", callback_data="guestbot:create_cancel", icon_id=_EMOJI_CANCEL))
    sent = bot.send_message(
        chat_id,
        body,
        parse_mode="HTML",
        reply_markup=kb,
    )
    _PENDING_TOKEN_PROMPTS[uid] = (int(chat_id), int(sent.message_id))


def _begin_guest_creation(chat_id: int, user: types.User, source_msg_id: int | None = None) -> None:
    uid = int(user.id)
    _PENDING_TOKEN_USERS.add(uid)
    _try_delete_message(chat_id, source_msg_id)
    body = (
        f'{_pe(_EMOJI_PM, "⚙️")} <b>Подключение гостевого бота</b>\n\n'
        "1) Создайте отдельного бота в @BotFather\n"
        "2) Отправьте токен следующим сообщением\n"
        "3) После регистрации guest-бот запустится как отдельный процесс\n\n"
        "Нажмите «Отмена» для завершения."
    )
    _send_guest_creation_prompt(chat_id, uid, body)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("guestbot:"))
def guest_bots_callback(call: types.CallbackQuery):
    if not _is_owner(call.from_user):
        bot.answer_callback_query(call.id, "Недостаточно прав.", show_alert=False)
        return

    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    uid = int(call.from_user.id)

    def _edit_menu(selected_guest_id: int = 0):
        try:
            bot.edit_message_text(
                _guest_bots_menu_text(call.from_user),
                chat_id,
                msg_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_guest_bots_menu_kb(call.from_user, selected_guest_id=selected_guest_id),
            )
        except Exception:
            _show_guest_bots_menu(chat_id, call.from_user)

    # ---- create ----
    if data == "guestbot:create":
        _begin_guest_creation(chat_id, call.from_user, source_msg_id=msg_id)
        bot.answer_callback_query(call.id, "Ожидаю токен гостевого бота.", show_alert=False)
        return

    if data == "guestbot:create_cancel":
        _clear_guest_creation_state(uid, chat_id=chat_id)
        _try_delete_message(chat_id, msg_id)
        bot.answer_callback_query(call.id, "Подключение отменено.", show_alert=False)
        _show_guest_bots_menu(chat_id, call.from_user)
        return

    # ---- list ----
    if data == "guestbot:list":
        _edit_menu()
        bot.answer_callback_query(call.id)
        return

    # ---- unbind ask ----
    if data.startswith("guestbot:unbind_ask:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        uname = _html.escape(entry.get("bot_username") or str(guest_id))
        kb = types.InlineKeyboardMarkup(row_width=2)
        btn_yes = _btn("Да, отвязать", callback_data=f"guestbot:unbind_do:{guest_id}")
        try:
            btn_yes.style = "danger"
        except Exception:
            pass
        btn_no = _btn("Нет, отмена", callback_data="guestbot:list")
        kb.add(btn_yes, btn_no)
        try:
            bot.edit_message_text(
                f'{_pe(_EMOJI_CANCEL, "❌")} <b>Отвязать гостевого бота?</b>\n\n'
                f"Бот <b>@{uname}</b> будет удалён из системы.\n"
                "Вы сможете подключить его заново через токен BotFather.",
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ---- unbind do ----
    if data.startswith("guestbot:unbind_do:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        _stop_guest_runtime(guest_id)
        uname = _html.escape(entry.get("bot_username") or str(guest_id))
        delete_guest_bot(guest_id)
        bot.answer_callback_query(call.id, f"@{uname} отвязан.", show_alert=False)
        _edit_menu()
        return

    # ---- manage (per-bot command management page) ----
    if data.startswith("guestbot:select:") or data.startswith("guestbot:manage:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        _edit_menu(selected_guest_id=guest_id)
        bot.answer_callback_query(call.id)
        return

    # ---- cmd_add (start command creation flow for a bot) ----
    # ---- cmd_add (removed — command creation is only via /start in the guest bot) ----
    if data.startswith("guestbot:cmd_add:"):
        bot.answer_callback_query(call.id, "Для добавления команд откройте гостевого бота и напишите /start.", show_alert=True)
        return

    if data.startswith("guestbot:cmd_del:") or data.startswith("guestbot:cmd_tog:"):
        bot.answer_callback_query(
            call.id,
            "Управляйте командами внутри гостевого бота через /start.",
            show_alert=True,
        )
        return

    bot.answer_callback_query(call.id)


def _is_waiting_guest_input(m: types.Message) -> bool:
    if should_ignore_text_triggers(m):
        return False
    if m.chat.type != "private" or not _is_owner(m.from_user) or not m.text:
        return False
    uid = int(m.from_user.id)
    return uid in _PENDING_TOKEN_USERS


@bot.message_handler(func=_is_waiting_guest_input)
def on_guest_pending_input(m: types.Message):
    text = (m.text or "").strip()
    uid = int(m.from_user.id)
    lower_text = text.lower()

    if lower_text in {"/cancel", "отмена", "cancel"}:
        _try_delete_message(m.chat.id, m.message_id)
        _clear_guest_creation_state(uid, chat_id=m.chat.id)
        bot.send_message(
            m.chat.id,
            f'{_pe(_EMOJI_OK, "✅")} Операция отменена.',
            parse_mode="HTML",
        )
        _show_guest_bots_menu(m.chat.id, m.from_user)
        return

    # ---- token input ----
    if uid in _PENDING_TOKEN_USERS:
        _try_delete_message(m.chat.id, m.message_id)
        token_match = _TOKEN_RE.search(text)
        if not token_match:
            _send_guest_creation_prompt(
                m.chat.id,
                uid,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "⚠️")} Не удалось распознать токен. Пришлите токен целиком.',
            )
            return

        token = token_match.group(1)
        try:
            test_bot = _tb.TeleBot(token)
            me = test_bot.get_me()
        except Exception as e:
            _send_guest_creation_prompt(
                m.chat.id,
                uid,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Невалидный токен</b>\n\n'
                f"<code>{_html.escape(str(e))}</code>",
            )
            return

        created, err, entry = create_guest_bot(
            owner_user_id=uid,
            bot_id=int(getattr(me, "id", 0) or 0),
            bot_username=str(getattr(me, "username", "") or "").lower(),
            bot_token=token,
            display_name=str(getattr(me, "first_name", "") or getattr(me, "username", "") or ""),
            linked_modules=["commands"],
        )
        if not created:
            err_text = _html.escape(str(err or "Ошибка"))
            _send_guest_creation_prompt(
                m.chat.id,
                uid,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Не удалось подключить бота</b>\n\n'
                f"<code>{err_text}</code>",
            )
            return

        _clear_guest_creation_state(uid, chat_id=m.chat.id)
        _launch_guest_runtime(entry or {})
        final = entry or {}
        uname = _html.escape(final.get("bot_username") or "")
        bot.send_message(
            m.chat.id,
            f'{_pe(_EMOJI_OK, "✅")} <b>Гостевой бот @{uname} подключён</b>\n\n'
            f"Управляйте командами прямо в боте — отправьте ему /start.\n"
            f"В группах вызов: <code>@{uname} имя команды</code>.",
            parse_mode="HTML",
        )
        _show_guest_bots_menu(m.chat.id, m.from_user)
