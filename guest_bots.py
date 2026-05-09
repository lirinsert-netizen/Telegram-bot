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
    delete_guest_command,
    get_guest_bot_by_id,
    list_guest_bots,
    list_guest_commands,
    set_guest_bot_enabled,
    set_guest_bot_runtime_pid,
    set_guest_command_enabled,
    update_guest_bot_modules,
    upsert_guest_command,
)


_TOKEN_RE = _re.compile(r"\b(\d{8,10}:[A-Za-z0-9_-]{35,})\b")
_GUEST_RUNTIME_SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "guest_runtime.py")
_MAX_COMMAND_RESPONSE_LEN = 3500
_CMD_MAX_NAME_LEN = 30
_MAX_COMMANDS_PER_PAGE = 20

_PENDING_TOKEN_USERS: set[int] = set()
# Step-by-step command creation draft: user_id -> draft dict
_GUEST_CMD_DRAFTS: dict[int, dict] = {}

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
            f"{i}. {status_icon} <b>@{uname}</b>\n"
            f"   <b>ID:</b> <code>{guest_id}</code> · <i>{status_text}</i>"
        )
    return "\n".join(lines)


def _guest_bots_menu_kb(user: types.User) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item in list_guest_bots(owner_user_id=int(user.id)):
        guest_id = int(item.get("id") or 0)
        uname = (item.get("bot_username") or str(guest_id)).strip().lstrip("@")
        kb.add(_btn(f"@{uname}", callback_data=f"guestbot:manage:{guest_id}"))
    kb.add(_btn("Подключить гостевого бота", callback_data="guestbot:create", icon_id=_EMOJI_CONNECT))
    kb.add(_btn("Назад", callback_data="start:home", icon_id=_EMOJI_BACK))
    return kb


def _manage_bot_text(entry: dict, cmds: list) -> str:
    uname = _html.escape((entry.get("bot_username") or "").strip().lstrip("@"))
    enabled = bool(entry.get("enabled"))
    status_icon = _pe(_EMOJI_OK, "✅") if enabled else _pe(_EMOJI_CANCEL, "❌")
    hdr = _pe(_EMOJI_LIST, "📋")
    lines = [
        f"{hdr} <b>Команды @{uname}</b>\n",
        f"<b>Статус:</b> {status_icon} {'активен' if enabled else 'отключён'}\n",
    ]
    if not cmds:
        lines.append("<i>Команд пока нет.</i>\n\nНажмите «Добавить команду» для создания.")
    else:
        lines.append(f"<b>Команд:</b> <code>{len(cmds)}</code>\n")
        for i, cmd in enumerate(cmds, 1):
            mark = _pe(_EMOJI_OK, "✅") if cmd.get("enabled") else _pe(_EMOJI_CANCEL, "❌")
            access = " <i>(владелец)</i>" if cmd.get("owner_only") else ""
            lines.append(f"{i}. {mark} <code>{_html.escape(cmd['name'])}</code>{access}")
    return "\n".join(lines)


def _manage_bot_kb(entry: dict, cmds: list) -> types.InlineKeyboardMarkup:
    guest_id = int(entry.get("id") or 0)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(_btn("Добавить команду", callback_data=f"guestbot:cmd_add:{guest_id}", icon_id=_EMOJI_CONNECT))
    for cmd in cmds[:_MAX_COMMANDS_PER_PAGE]:
        name = cmd.get("name", "")
        mark = "✅" if cmd.get("enabled") else "❌"
        kb.add(
            _btn(f"{mark} {name}", callback_data=f"guestbot:cmd_tog:{guest_id}:{name}"),
            _btn("🗑", callback_data=f"guestbot:cmd_del:{guest_id}:{name}"),
        )
    kb.add(_btn("Отвязать бота", callback_data=f"guestbot:unbind_ask:{guest_id}", icon_id=_EMOJI_CANCEL))
    kb.add(_btn("Назад", callback_data="guestbot:list", icon_id=_EMOJI_BACK))
    return kb


# --------------- Draft-based command creation helpers ---------------

def _draft_get(uid: int) -> dict:
    return _GUEST_CMD_DRAFTS.get(uid, {})


def _draft_set(uid: int, draft: dict) -> None:
    _GUEST_CMD_DRAFTS[uid] = draft


def _draft_clear(uid: int) -> None:
    _GUEST_CMD_DRAFTS.pop(uid, None)


def _render_cmd_draft_text(draft: dict) -> str:
    emoji = _pe(_EMOJI_CONNECT, "➕")
    name = _html.escape(draft.get("name") or "")
    text_raw = draft.get("text") or ""
    has_text = "есть" if text_raw else "нет"
    owner_only = bool(draft.get("owner_only"))
    access_label = "Для владельца" if owner_only else "Все пользователи"
    name_str = f"<code>{name}</code>" if name else "<i>не задано</i>"
    return (
        f"{emoji} <b>Новая команда</b>\n\n"
        f"<b>Имя:</b> {name_str}\n"
        f"<b>Текст:</b> <code>{has_text}</code>\n"
        f"<b>Доступ:</b> {access_label}"
    )


def _build_cmd_draft_kb(draft: dict) -> types.InlineKeyboardMarkup:
    guest_id = int(draft.get("guest_bot_id") or 0)
    owner_only = bool(draft.get("owner_only"))
    kb = types.InlineKeyboardMarkup(row_width=2)

    btn_text = _btn("Текст", callback_data=f"guestbot:draft_text:{guest_id}")
    try:
        btn_text.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_SENT_PM_ID)
    except Exception:
        pass

    if owner_only:
        btn_owner = _btn("»Для владельца«", callback_data=f"guestbot:draft_access:{guest_id}:owner")
        try:
            btn_owner.style = "primary"
        except Exception:
            pass
        btn_all = _btn("Все пользователи", callback_data=f"guestbot:draft_access:{guest_id}:all")
    else:
        btn_owner = _btn("Для владельца", callback_data=f"guestbot:draft_access:{guest_id}:owner")
        btn_all = _btn("»Все пользователи«", callback_data=f"guestbot:draft_access:{guest_id}:all")
        try:
            btn_all.style = "primary"
        except Exception:
            pass

    kb.add(btn_text)
    kb.add(btn_owner, btn_all)

    btn_cancel = _btn("Отмена", callback_data=f"guestbot:draft_cancel:{guest_id}")
    try:
        btn_cancel.style = "danger"
    except Exception:
        pass
    btn_save = _btn("Сохранить", callback_data=f"guestbot:draft_save:{guest_id}")
    try:
        btn_save.style = "success"
    except Exception:
        pass
    kb.add(btn_cancel, btn_save)
    return kb



def _show_guest_bots_menu(chat_id: int, user: types.User) -> None:
    bot.send_message(
        chat_id,
        _guest_bots_menu_text(user),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=_guest_bots_menu_kb(user),
    )


def _begin_guest_creation(chat_id: int, user: types.User) -> None:
    _PENDING_TOKEN_USERS.add(int(user.id))
    kb = types.InlineKeyboardMarkup()
    kb.add(_btn("Открыть BotFather", url="https://t.me/BotFather", icon_id=_EMOJI_PM))
    bot.send_message(
        chat_id,
        f'{_pe(_EMOJI_PM, "⚙️")} <b>Подключение гостевого бота</b>\n\n'
        "1) Создайте отдельного бота в @BotFather\n"
        "2) Отправьте токен следующим сообщением\n"
        "3) После регистрации guest-бот запустится как отдельный процесс\n\n"
        "Для отмены: <code>/cancel</code> или <code>отмена</code>",
        parse_mode="HTML",
        reply_markup=kb,
    )


@bot.message_handler(commands=["guestbots"])
def cmd_guestbots(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return
    _show_guest_bots_menu(m.chat.id, m.from_user)


@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("guestbot:"))
def guest_bots_callback(call: types.CallbackQuery):
    if not _is_owner(call.from_user):
        bot.answer_callback_query(call.id, "Недостаточно прав.", show_alert=False)
        return

    data = call.data or ""
    chat_id = call.message.chat.id
    msg_id = call.message.message_id
    uid = int(call.from_user.id)

    def _edit_menu():
        try:
            bot.edit_message_text(
                _guest_bots_menu_text(call.from_user),
                chat_id,
                msg_id,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_guest_bots_menu_kb(call.from_user),
            )
        except Exception:
            _show_guest_bots_menu(chat_id, call.from_user)

    # ---- create ----
    if data == "guestbot:create":
        _begin_guest_creation(chat_id, call.from_user)
        bot.answer_callback_query(call.id, "Ожидаю токен гостевого бота.", show_alert=False)
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
    if data.startswith("guestbot:manage:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        cmds = list_guest_commands(guest_id)
        try:
            bot.edit_message_text(
                _manage_bot_text(entry, cmds),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        except Exception:
            bot.send_message(
                chat_id,
                _manage_bot_text(entry, cmds),
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        bot.answer_callback_query(call.id)
        return

    # ---- cmd_add (start command creation flow for a bot) ----
    if data.startswith("guestbot:cmd_add:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        _start_cmd_add_for_bot(chat_id, uid, guest_id)
        bot.answer_callback_query(call.id)
        return

    # ---- cmd_del (delete a command) ----
    if data.startswith("guestbot:cmd_del:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        cmd_name = parts[3].strip()
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        if not cmd_name:
            bot.answer_callback_query(call.id, "Некорректное имя команды.", show_alert=True)
            return
        ok = delete_guest_command(guest_id, cmd_name)
        if ok:
            bot.answer_callback_query(call.id, f"Команда «{cmd_name}» удалена.", show_alert=False)
        else:
            bot.answer_callback_query(call.id, "Не удалось удалить команду.", show_alert=True)
            return
        cmds = list_guest_commands(guest_id)
        try:
            bot.edit_message_text(
                _manage_bot_text(entry, cmds),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        except Exception:
            pass
        return

    # ---- cmd_tog (toggle command enabled/disabled) ----
    if data.startswith("guestbot:cmd_tog:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        cmd_name = parts[3].strip()
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        if not cmd_name:
            bot.answer_callback_query(call.id, "Некорректное имя команды.", show_alert=True)
            return
        cmds = list_guest_commands(guest_id)
        cmd = next((c for c in cmds if c["name"] == cmd_name), None)
        if not cmd:
            bot.answer_callback_query(call.id, "Команда не найдена.", show_alert=True)
            return
        set_guest_command_enabled(guest_id, cmd_name, not bool(cmd.get("enabled")))
        bot.answer_callback_query(call.id)
        cmds = list_guest_commands(guest_id)
        try:
            bot.edit_message_text(
                _manage_bot_text(entry, cmds),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        except Exception:
            pass
        return

    # ---- draft: text input trigger ----
    if data.startswith("guestbot:draft_text:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        draft = _draft_get(uid)
        if not draft or int(draft.get("guest_bot_id") or 0) != guest_id:
            bot.answer_callback_query(call.id, "Черновик не найден.", show_alert=False)
            return
        draft["step"] = "await_text"
        _draft_set(uid, draft)
        kb = types.InlineKeyboardMarkup()
        kb.add(_btn("Отмена", callback_data=f"guestbot:draft_cancel:{guest_id}", icon_id=_EMOJI_CANCEL))
        bot.send_message(
            chat_id,
            f'{_pe(_EMOJI_PM, "📝")} <b>Пришлите текст команды.</b>\n\n'
            "Поддерживается HTML-форматирование Telegram:\n"
            "<code>&lt;b&gt;жирный&lt;/b&gt;</code>, "
            "<code>&lt;i&gt;курсив&lt;/i&gt;</code>, "
            "<code>&lt;code&gt;код&lt;/code&gt;</code>, "
            "<code>&lt;a href='...'&gt;ссылка&lt;/a&gt;</code> и другие теги.",
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        bot.answer_callback_query(call.id, "Ожидаю текст.", show_alert=False)
        return

    # ---- draft: access toggle ----
    if data.startswith("guestbot:draft_access:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        access_val = parts[3]
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        draft = _draft_get(uid)
        if not draft or int(draft.get("guest_bot_id") or 0) != guest_id:
            bot.answer_callback_query(call.id, "Черновик не найден.", show_alert=False)
            return
        draft["owner_only"] = (access_val == "owner")
        _draft_set(uid, draft)
        try:
            bot.edit_message_text(
                _render_cmd_draft_text(draft),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_build_cmd_draft_kb(draft),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return

    # ---- draft: cancel ----
    if data.startswith("guestbot:draft_cancel:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        _draft_clear(uid)
        bot.answer_callback_query(call.id, "Создание команды отменено.", show_alert=False)
        entry = get_guest_bot_by_id(guest_id)
        if entry and int(entry.get("owner_user_id") or 0) == uid:
            cmds = list_guest_commands(guest_id)
            try:
                bot.edit_message_text(
                    _manage_bot_text(entry, cmds),
                    chat_id,
                    msg_id,
                    parse_mode="HTML",
                    reply_markup=_manage_bot_kb(entry, cmds),
                )
            except Exception:
                bot.send_message(
                    chat_id,
                    _manage_bot_text(entry, cmds),
                    parse_mode="HTML",
                    reply_markup=_manage_bot_kb(entry, cmds),
                )
        else:
            _edit_menu()
        return

    # ---- draft: save ----
    if data.startswith("guestbot:draft_save:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != uid:
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        draft = _draft_get(uid)
        if not draft or int(draft.get("guest_bot_id") or 0) != guest_id:
            bot.answer_callback_query(call.id, "Черновик не найден.", show_alert=False)
            return
        cmd_name = _normalize_command_name(draft.get("name") or "")
        response_text = (draft.get("text") or "").strip()
        owner_only = bool(draft.get("owner_only"))
        if not _is_command_name_valid(cmd_name):
            bot.answer_callback_query(call.id, "Некорректное имя команды.", show_alert=True)
            return
        if not response_text:
            bot.answer_callback_query(call.id, "Текст ответа не задан.", show_alert=True)
            return
        ok = upsert_guest_command(guest_id, cmd_name, response_text, enabled=True, owner_only=owner_only)
        _draft_clear(uid)
        if ok:
            bot.answer_callback_query(call.id, f"Команда «{cmd_name}» сохранена.", show_alert=False)
        else:
            bot.answer_callback_query(call.id, "Не удалось сохранить команду.", show_alert=True)
        cmds = list_guest_commands(guest_id)
        try:
            bot.edit_message_text(
                _manage_bot_text(entry, cmds),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        except Exception:
            bot.send_message(
                chat_id,
                _manage_bot_text(entry, cmds),
                parse_mode="HTML",
                reply_markup=_manage_bot_kb(entry, cmds),
            )
        return

    bot.answer_callback_query(call.id)


def _is_waiting_guest_input(m: types.Message) -> bool:
    if should_ignore_text_triggers(m):
        return False
    if m.chat.type != "private" or not _is_owner(m.from_user) or not m.text:
        return False
    uid = int(m.from_user.id)
    if uid in _PENDING_TOKEN_USERS:
        return True
    draft = _draft_get(uid)
    if not draft:
        return False
    return draft.get("step") in ("await_name", "await_text")


@bot.message_handler(func=_is_waiting_guest_input)
def on_guest_pending_input(m: types.Message):
    text = (m.text or "").strip()
    uid = int(m.from_user.id)
    lower_text = text.lower()

    if lower_text in {"/cancel", "отмена", "cancel"}:
        _PENDING_TOKEN_USERS.discard(uid)
        _draft_clear(uid)
        bot.reply_to(m, "Операция отменена.")
        return

    # ---- token input ----
    if uid in _PENDING_TOKEN_USERS:
        token_match = _TOKEN_RE.search(text)
        if not token_match:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "⚠️")} Не удалось распознать токен. Пришлите токен целиком.',
                parse_mode="HTML",
            )
            return

        token = token_match.group(1)
        try:
            test_bot = _tb.TeleBot(token)
            me = test_bot.get_me()
        except Exception as e:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Невалидный токен</b>\n\n'
                f"<code>{_html.escape(str(e))}</code>",
                parse_mode="HTML",
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
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Не удалось подключить бота</b>\n\n'
                f"<code>{err_text}</code>",
                parse_mode="HTML",
            )
            return

        _PENDING_TOKEN_USERS.discard(uid)
        _launch_guest_runtime(entry or {})
        final = entry or {}
        uname = _html.escape(final.get("bot_username") or "")
        bot.reply_to(
            m,
            f'{_pe(_EMOJI_OK, "✅")} <b>Гостевой бот @{uname} подключён</b>\n\n'
            f"Управляйте командами прямо в боте — отправьте ему /start.\n"
            f"В группах вызов: <code>@{uname} имя_команды</code>.",
            parse_mode="HTML",
        )
        _show_guest_bots_menu(m.chat.id, m.from_user)
        return

    # ---- draft: await name ----
    draft = _draft_get(uid)
    if not draft:
        return

    guest_id = int(draft.get("guest_bot_id") or 0)
    entry = get_guest_bot_by_id(guest_id)
    if not entry or int(entry.get("owner_user_id") or 0) != uid:
        _draft_clear(uid)
        bot.reply_to(m, "Guest-бот не найден.")
        return

    step = draft.get("step")

    if step == "await_name":
        if not text or " " in text:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Некорректное имя</b>\n\n'
                "Имя команды — одно слово без пробелов, не более 30 символов.",
                parse_mode="HTML",
            )
            return
        if len(text) > _CMD_MAX_NAME_LEN:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Слишком длинное имя</b>\n\n'
                f"Максимум {_CMD_MAX_NAME_LEN} символов.",
                parse_mode="HTML",
            )
            return
        draft["name"] = _normalize_command_name(text)
        draft["step"] = "draft"
        _draft_set(uid, draft)
        bot.send_message(
            m.chat.id,
            _render_cmd_draft_text(draft),
            parse_mode="HTML",
            reply_markup=_build_cmd_draft_kb(draft),
        )
        return

    if step == "await_text":
        response_text = text
        if not response_text:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} Текст ответа не должен быть пустым.',
                parse_mode="HTML",
            )
            return
        if len(response_text) > _MAX_COMMAND_RESPONSE_LEN:
            bot.reply_to(
                m,
                f'{_pe(PREMIUM_PREFIX_EMOJI_ID, "❌")} <b>Текст слишком длинный</b>\n\n'
                f"Максимум {_MAX_COMMAND_RESPONSE_LEN} символов.",
                parse_mode="HTML",
            )
            return
        draft["text"] = response_text
        draft["step"] = "draft"
        _draft_set(uid, draft)
        bot.send_message(
            m.chat.id,
            _render_cmd_draft_text(draft),
            parse_mode="HTML",
            reply_markup=_build_cmd_draft_kb(draft),
        )
        return

    _draft_clear(uid)


# ---- Trigger for "add command" flow from any context ----
def _start_cmd_add_for_bot(chat_id: int, uid: int, guest_id: int) -> None:
    """Initiate the step-by-step command creation flow."""
    _draft_set(uid, {
        "guest_bot_id": guest_id,
        "step": "await_name",
        "name": "",
        "text": "",
        "owner_only": False,
    })
    kb = types.InlineKeyboardMarkup()
    kb.add(_btn("Отмена", callback_data=f"guestbot:draft_cancel:{guest_id}", icon_id=_EMOJI_CANCEL))
    bot.send_message(
        chat_id,
        f'{_pe(_EMOJI_CONNECT, "➕")} <b>Создание команды</b>\n\n'
        f"Пришлите <b>имя</b> новой команды.\n"
        f"<i>Одно слово, до {_CMD_MAX_NAME_LEN} символов. Допустимы любые символы без пробелов.</i>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )
