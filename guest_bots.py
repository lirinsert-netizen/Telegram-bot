from __future__ import annotations

import html as _html
import os as _os
import re as _re
import subprocess as _subprocess
import sys as _sys
import threading as _threading
from typing import Optional

import telebot as _tb

from config import OWNER_USERNAME, DATA_DIR, bot, types
from helpers import should_ignore_text_triggers
from persistence import (
    create_guest_bot,
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
_COMMAND_NAME_RE = _re.compile(r"^[A-Za-zА-Яа-я0-9_]{1,30}$")
_GUEST_RUNTIME_SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "guest_runtime.py")
_MAX_COMMAND_RESPONSE_LEN = 3500

_PENDING_TOKEN_USERS: set[int] = set()
_PENDING_COMMAND_INPUT: dict[int, dict] = {}

_GUEST_PROCESSES: dict[int, _subprocess.Popen] = {}
_GUEST_PROCESSES_LOCK = _threading.Lock()

_AVAILABLE_MODULES: list[tuple[str, str]] = [
    ("commands", "Команды"),
]


def _is_owner(user: types.User | None) -> bool:
    if not user:
        return False
    return (user.username or "").lower() == (OWNER_USERNAME or "").lower()


def _normalize_command_name(name: str) -> str:
    return str(name or "").strip().lower()


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _is_command_name_valid(name: str) -> bool:
    return bool(_COMMAND_NAME_RE.fullmatch(_normalize_command_name(name)))


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
    if not bots:
        return (
            "<b>Guest-боты</b>\n\n"
            "У вас пока нет guest-ботов.\n"
            "Нажмите «Подключить guest-бота» и отправьте токен BotFather."
        )

    lines = ["<b>Guest-боты</b>\n"]
    for item in bots:
        uname = _html.escape(item.get("bot_username") or "unknown")
        enabled = bool(item.get("enabled"))
        status = "✅ включён" if enabled else "⛔ выключен"
        lines.append(f"• <b>@{uname}</b> — {status}")
    return "\n".join(lines)


def _guest_bots_menu_kb(user: types.User) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for item in list_guest_bots(owner_user_id=int(user.id)):
        guest_id = int(item.get("id") or 0)
        uname = _html.escape(item.get("bot_username") or str(guest_id))
        kb.add(types.InlineKeyboardButton(f"⚙️ @{uname}", callback_data=f"guestbot:open:{guest_id}"))
    kb.add(types.InlineKeyboardButton("➕ Подключить guest-бота", callback_data="guestbot:create"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="start:home"))
    return kb


def _guest_detail_text(entry: dict) -> str:
    guest_id = int(entry.get("id") or 0)
    uname = _html.escape(entry.get("bot_username") or "")
    dname = _html.escape(entry.get("display_name") or uname)
    status = "✅ активен" if bool(entry.get("enabled")) else "⛔ отключён"
    modules = ", ".join(str(v) for v in (entry.get("linked_modules") or [])) or "commands"
    commands_count = len(list_guest_commands(guest_id))
    return (
        f"<b>Guest-бот @{uname}</b>\n\n"
        f"<b>ID:</b> <code>{guest_id}</code>\n"
        f"<b>Название:</b> <code>{dname}</code>\n"
        f"<b>Статус:</b> {status}\n"
        f"<b>Модули:</b> <code>{_html.escape(modules)}</code>\n"
        f"<b>Команд:</b> <code>{commands_count}</code>"
    )


def _guest_detail_kb(entry: dict) -> types.InlineKeyboardMarkup:
    guest_id = int(entry.get("id") or 0)
    enabled = bool(entry.get("enabled"))
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🧩 Модули", callback_data=f"guestbot:modules:{guest_id}"))
    kb.add(types.InlineKeyboardButton("📋 Команды", callback_data=f"guestbot:commands:{guest_id}"))
    kb.add(
        types.InlineKeyboardButton(
            "⛔ Выключить" if enabled else "✅ Включить",
            callback_data=f"guestbot:toggle:{guest_id}",
        )
    )
    kb.add(types.InlineKeyboardButton("⬅️ К списку", callback_data="guestbot:list"))
    return kb


def _guest_modules_text(entry: dict) -> str:
    enabled_modules = set(str(m) for m in (entry.get("linked_modules") or []))
    lines = [f"<b>Модули guest-бота @{_html.escape(entry.get('bot_username') or '')}</b>\n"]
    lines.append("Выберите, какие функции доступны этому guest-боту.")
    for key, title in _AVAILABLE_MODULES:
        mark = "✅" if key in enabled_modules else "◻️"
        lines.append(f"{mark} <code>{_html.escape(title)}</code>")
    return "\n".join(lines)


def _guest_modules_kb(entry: dict) -> types.InlineKeyboardMarkup:
    guest_id = int(entry.get("id") or 0)
    enabled_modules = set(str(m) for m in (entry.get("linked_modules") or []))
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, title in _AVAILABLE_MODULES:
        mark = "✅" if key in enabled_modules else "◻️"
        kb.add(types.InlineKeyboardButton(f"{mark} {title}", callback_data=f"guestbot:modtog:{guest_id}:{key}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"guestbot:open:{guest_id}"))
    return kb


def _guest_commands_text(entry: dict) -> str:
    guest_id = int(entry.get("id") or 0)
    items = list_guest_commands(guest_id)
    lines = [f"<b>Команды guest-бота @{_html.escape(entry.get('bot_username') or '')}</b>\n"]
    if not items:
        lines.append("Команд пока нет.")
    else:
        for item in items[:20]:
            mark = "✅" if item.get("enabled") else "⛔"
            name = _html.escape(item.get("name") or "")
            text_preview = _html.escape((item.get("response_text") or "")[:50])
            lines.append(f"{mark} <code>{name}</code> — {text_preview}")
        if len(items) > 20:
            lines.append(f"... и ещё {len(items) - 20}")
    lines.append("\nФормат добавления: <code>имя_команды | текст ответа</code>")
    return "\n".join(lines)


def _guest_commands_kb(entry: dict) -> types.InlineKeyboardMarkup:
    guest_id = int(entry.get("id") or 0)
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Добавить / обновить", callback_data=f"guestbot:cmdadd:{guest_id}"))
    kb.add(types.InlineKeyboardButton("🗑️ Удалить", callback_data=f"guestbot:cmddel:{guest_id}"))
    for item in list_guest_commands(guest_id)[:10]:
        name = _normalize_command_name(item.get("name") or "")
        mark = "✅" if item.get("enabled") else "⛔"
        kb.add(types.InlineKeyboardButton(f"{mark} {name}", callback_data=f"guestbot:cmdtog:{guest_id}:{name}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"guestbot:open:{guest_id}"))
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
    kb.add(types.InlineKeyboardButton("Открыть BotFather", url="https://t.me/BotFather"))
    bot.send_message(
        chat_id,
        "<b>Подключение guest-бота</b>\n\n"
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

    if data == "guestbot:create":
        _begin_guest_creation(chat_id, call.from_user)
        bot.answer_callback_query(call.id, "Ожидаю токен guest-бота.", show_alert=False)
        return

    if data == "guestbot:list":
        try:
            bot.edit_message_text(
                _guest_bots_menu_text(call.from_user),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_guest_bots_menu_kb(call.from_user),
                disable_web_page_preview=True,
            )
        except Exception:
            _show_guest_bots_menu(chat_id, call.from_user)
        bot.answer_callback_query(call.id)
        return

    if data.startswith("guestbot:open:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        try:
            bot.edit_message_text(
                _guest_detail_text(entry),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_guest_detail_kb(entry),
            )
        except Exception:
            bot.send_message(chat_id, _guest_detail_text(entry), parse_mode="HTML", reply_markup=_guest_detail_kb(entry))
        bot.answer_callback_query(call.id)
        return

    if data.startswith("guestbot:toggle:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        enabled_now = bool(entry.get("enabled"))
        set_guest_bot_enabled(guest_id, not enabled_now)
        refreshed = get_guest_bot_by_id(guest_id) or entry
        if bool(refreshed.get("enabled")):
            _launch_guest_runtime(refreshed)
            bot.answer_callback_query(call.id, "Guest-бот включён.", show_alert=False)
        else:
            _stop_guest_runtime(guest_id)
            bot.answer_callback_query(call.id, "Guest-бот выключен.", show_alert=False)
        try:
            bot.edit_message_text(
                _guest_detail_text(refreshed),
                chat_id,
                msg_id,
                parse_mode="HTML",
                reply_markup=_guest_detail_kb(refreshed),
            )
        except Exception:
            pass
        return

    if data.startswith("guestbot:modules:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        bot.edit_message_text(
            _guest_modules_text(entry),
            chat_id,
            msg_id,
            parse_mode="HTML",
            reply_markup=_guest_modules_kb(entry),
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("guestbot:modtog:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        _, _, guest_id_str, module_key = parts
        guest_id = _safe_int(guest_id_str)
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        modules = [str(m) for m in (entry.get("linked_modules") or [])]
        if module_key in modules:
            modules = [m for m in modules if m != module_key]
        else:
            modules.append(module_key)
        update_guest_bot_modules(guest_id, modules)
        refreshed = get_guest_bot_by_id(guest_id) or entry
        bot.edit_message_text(
            _guest_modules_text(refreshed),
            chat_id,
            msg_id,
            parse_mode="HTML",
            reply_markup=_guest_modules_kb(refreshed),
        )
        bot.answer_callback_query(call.id, "Сохранено.", show_alert=False)
        return

    if data.startswith("guestbot:commands:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        bot.edit_message_text(
            _guest_commands_text(entry),
            chat_id,
            msg_id,
            parse_mode="HTML",
            reply_markup=_guest_commands_kb(entry),
        )
        bot.answer_callback_query(call.id)
        return

    if data.startswith("guestbot:cmdadd:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        _PENDING_COMMAND_INPUT[int(call.from_user.id)] = {"mode": "add", "guest_bot_id": guest_id}
        bot.send_message(
            chat_id,
            "Отправьте: <code>имя_команды | текст ответа</code>",
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id, "Ожидаю данные команды.", show_alert=False)
        return

    if data.startswith("guestbot:cmddel:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        guest_id = _safe_int(parts[2])
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        _PENDING_COMMAND_INPUT[int(call.from_user.id)] = {"mode": "del", "guest_bot_id": guest_id}
        bot.send_message(chat_id, "Отправьте имя команды для удаления.", parse_mode="HTML")
        bot.answer_callback_query(call.id, "Ожидаю имя команды.", show_alert=False)
        return

    if data.startswith("guestbot:cmdtog:"):
        parts = data.split(":", 3)
        if len(parts) < 4:
            bot.answer_callback_query(call.id, "Некорректные данные.", show_alert=False)
            return
        _, _, guest_id_str, cmd_name = parts
        guest_id = _safe_int(guest_id_str)
        entry = get_guest_bot_by_id(guest_id)
        if not entry or int(entry.get("owner_user_id") or 0) != int(call.from_user.id):
            bot.answer_callback_query(call.id, "Guest-бот не найден.", show_alert=False)
            return
        cmd_name = _normalize_command_name(cmd_name)
        commands = {c["name"]: c for c in list_guest_commands(guest_id)}
        current = commands.get(cmd_name)
        if not current:
            bot.answer_callback_query(call.id, "Команда не найдена.", show_alert=False)
            return
        set_guest_command_enabled(guest_id, cmd_name, not bool(current.get("enabled")))
        refreshed = get_guest_bot_by_id(guest_id) or entry
        bot.edit_message_text(
            _guest_commands_text(refreshed),
            chat_id,
            msg_id,
            parse_mode="HTML",
            reply_markup=_guest_commands_kb(refreshed),
        )
        bot.answer_callback_query(call.id, "Состояние команды изменено.", show_alert=False)
        return

    bot.answer_callback_query(call.id)


def _is_waiting_guest_input(m: types.Message) -> bool:
    if should_ignore_text_triggers(m):
        return False
    if m.chat.type != "private" or not _is_owner(m.from_user) or not m.text:
        return False
    uid = int(m.from_user.id)
    return uid in _PENDING_TOKEN_USERS or uid in _PENDING_COMMAND_INPUT


@bot.message_handler(func=_is_waiting_guest_input)
def on_guest_pending_input(m: types.Message):
    text = (m.text or "").strip()
    uid = int(m.from_user.id)
    lower_text = text.lower()
    if lower_text in {"/cancel", "отмена", "cancel"}:
        _PENDING_TOKEN_USERS.discard(uid)
        _PENDING_COMMAND_INPUT.pop(uid, None)
        bot.reply_to(m, "Операция отменена.")
        return

    if uid in _PENDING_TOKEN_USERS:
        token_match = _TOKEN_RE.search(text)
        if not token_match:
            bot.reply_to(m, "Не удалось распознать токен. Пришлите токен целиком.")
            return

        token = token_match.group(1)
        try:
            test_bot = _tb.TeleBot(token)
            me = test_bot.get_me()
        except Exception as e:
            bot.reply_to(
                m,
                f"❌ Невалидный токен: <code>{_html.escape(str(e))}</code>",
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
            bot.reply_to(m, f"❌ {err}")
            return

        _PENDING_TOKEN_USERS.discard(uid)
        _launch_guest_runtime(entry or {})
        final = entry or {}
        uname = _html.escape(final.get("bot_username") or "")
        bot.reply_to(
            m,
            (
                f"✅ Guest-бот <b>@{uname}</b> подключён.\n"
                "Создайте guest-команды в меню управления.\n"
                "В группах вызов: <code>@username_бота команда</code>."
            ),
            parse_mode="HTML",
        )
        _show_guest_bots_menu(m.chat.id, m.from_user)
        return

    state = _PENDING_COMMAND_INPUT.get(uid)
    if not state:
        return

    guest_bot_id = int(state.get("guest_bot_id") or 0)
    entry = get_guest_bot_by_id(guest_bot_id)
    if not entry or int(entry.get("owner_user_id") or 0) != uid:
        _PENDING_COMMAND_INPUT.pop(uid, None)
        bot.reply_to(m, "Guest-бот не найден.")
        return

    if state.get("mode") == "add":
        if "|" not in text:
            bot.reply_to(m, "Формат: <code>имя_команды | текст ответа</code>", parse_mode="HTML")
            return
        raw_name, raw_response = text.split("|", 1)
        cmd_name = _normalize_command_name(raw_name)
        response_text = raw_response.strip()
        if not _is_command_name_valid(cmd_name):
            bot.reply_to(m, "Имя команды: только буквы/цифры/_ и до 30 символов.")
            return
        if not response_text:
            bot.reply_to(m, "Текст ответа не должен быть пустым.")
            return
        if len(response_text) > _MAX_COMMAND_RESPONSE_LEN:
            bot.reply_to(m, f"Текст ответа слишком длинный (макс. {_MAX_COMMAND_RESPONSE_LEN} символов).")
            return
        ok = upsert_guest_command(guest_bot_id, cmd_name, response_text, enabled=True)
        if not ok:
            bot.reply_to(m, "Не удалось сохранить команду.")
            return
        _PENDING_COMMAND_INPUT.pop(uid, None)
        bot.reply_to(m, f"✅ Команда <code>{_html.escape(cmd_name)}</code> сохранена.", parse_mode="HTML")
        _show_guest_bots_menu(m.chat.id, m.from_user)
        return

    if state.get("mode") == "del":
        cmd_name = _normalize_command_name(text)
        if not _is_command_name_valid(cmd_name):
            bot.reply_to(m, "Некорректное имя команды.")
            return
        ok = delete_guest_command(guest_bot_id, cmd_name)
        _PENDING_COMMAND_INPUT.pop(uid, None)
        if ok:
            bot.reply_to(m, f"✅ Команда <code>{_html.escape(cmd_name)}</code> удалена.", parse_mode="HTML")
        else:
            bot.reply_to(m, "Не удалось удалить команду.")
        _show_guest_bots_menu(m.chat.id, m.from_user)
        return

    _PENDING_COMMAND_INPUT.pop(uid, None)
