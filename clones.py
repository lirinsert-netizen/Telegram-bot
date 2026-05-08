"""
clones.py — Управление клонами бота (только основной бот, IS_CLONE=0).

Команды (только ЛС владельца):
  /clones                         — список всех клонов с кнопками «Отключить» / «Включить»
  /clone_register TOKEN           — зарегистрировать клон по токену из BotFather и запустить
  /clone_unlink <username|bot_id> — удалить клон из реестра
  /newbot <display_name> <username> — создать нового бота через BotFather и запустить как клон
  /newguest [TOKEN]               — зарегистрировать гостевого бота по токену и включить guest-режим

Архитектура клонов:
  Клон — это тот же бот с тем же кодом, запущенный как дочерний процесс.
  Переменные окружения клона: BOT_TOKEN=<token> IS_CLONE=1 DATA_DIR=<shared_dir>
  Все клоны и основной бот используют один общий DATA_DIR для синхронизации реестра.

  Кнопка «Отключить»: меняет статус клона на «disabled» в реестре.
  Клон видит это изменение (проверяет реестр каждые 10 с) и прекращает работу.
  Кнопка «Включить»: меняет статус на «running» и перезапускает процесс клона.

Этот модуль импортируется только когда IS_CLONE=False (см. main.py).
"""
from __future__ import annotations

import html as _html
import os as _os
import re as _re
import subprocess as _subprocess
import sys as _sys
import threading as _threading
import time

import telebot as _tb

from config import (
    bot,
    types,
    OWNER_USERNAME,
    DATA_DIR,
    call_mtproto_sync,
    tg_client,
    _ensure_tg_client_connected,
)
from persistence import (
    CLONES,
    save_clones,
)
from helpers import should_ignore_text_triggers

# ──────────────────────────── subprocess management ───────────────────────────

# Path to the bot's entry point, resolved relative to this file.
_MAIN_SCRIPT = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "main.py")

# In-memory map of running clone subprocesses: bot_id -> Popen
_CLONE_PROCESSES: dict[int, _subprocess.Popen] = {}
_CLONE_PROC_LOCK = _threading.Lock()

# Regex to extract a Telegram bot token from BotFather's reply.
_TOKEN_RE = _re.compile(r'\b(\d{8,10}:[A-Za-z0-9_-]{35,})\b')
_GUEST_REG_CANCEL_WORDS = {"отмена", "cancel", "/cancel"}
_PENDING_GUEST_REGISTRATION: set[int] = set()


def _normalize_role(value: object) -> str:
    role = str(value).strip().lower()
    return "guest" if role == "guest" else "clone"


def _set_pending_guest_registration(user_id: int) -> None:
    _PENDING_GUEST_REGISTRATION.add(int(user_id))


def _clear_pending_guest_registration(user_id: int) -> None:
    _PENDING_GUEST_REGISTRATION.discard(int(user_id))


def start_guest_registration_prompt(chat_id: int, user: types.User | None) -> bool:
    if not _is_owner(user):
        return False

    _set_pending_guest_registration(user.id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Открыть BotFather", url="https://t.me/BotFather"))
    bot.send_message(
        chat_id,
        "<b>Подключение гостевого бота</b>\n\n"
        "Создай нового бота в @BotFather или возьми уже готовый токен, "
        "затем просто отправь сюда токен следующим сообщением.\n"
        "Пример токена: <code>123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789</code>\n\n"
        "Также можно сразу использовать: <code>/newguest TOKEN</code>\n\n"
        "<i>После регистрации бот запустится в guest-режиме. "
        "Пользовательские команды будут работать в формате "
        "<code>@username_бота имя_команды</code>.</i>\n\n"
        "Для отмены отправь <code>/cancel</code> или слово <code>отмена</code>.",
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    return True


def _launch_clone_process(entry: dict) -> "_subprocess.Popen | None":
    """Spawn a clone bot as a child process.

    Sets entry['pid'] on success and updates _CLONE_PROCESSES.
    Returns the Popen object, or None if launching failed.
    """
    token = entry.get("token")
    try:
        bot_id = int(entry.get("bot_id", 0))
    except (TypeError, ValueError):
        bot_id = 0
    if not token or not bot_id:
        return None

    env = _os.environ.copy()
    env["BOT_TOKEN"] = token
    env["IS_CLONE"] = "1"
    role = _normalize_role(entry.get("role", "clone"))
    if role == "guest":
        env["IS_GUEST_BOT"] = "1"
    else:
        env.pop("IS_GUEST_BOT", None)

    log_path = _os.path.join(DATA_DIR, f"clone_{bot_id}.log")
    try:
        log_file = open(log_path, "a")  # noqa: WPS515 — intentionally kept open by child
    except OSError:
        log_file = _subprocess.DEVNULL  # type: ignore[assignment]

    try:
        proc = _subprocess.Popen(
            [_sys.executable, _MAIN_SCRIPT],
            env=env,
            stdout=log_file,
            stderr=log_file,
        )
    except Exception as e:
        print(f"[CLONE LAUNCH] Failed to launch clone bot_id={bot_id}: {e}")
        if hasattr(log_file, "close"):
            log_file.close()
        return None

    # Parent closes its copy; the child's inherited fd keeps the file open.
    if hasattr(log_file, "close"):
        log_file.close()

    with _CLONE_PROC_LOCK:
        _CLONE_PROCESSES[bot_id] = proc
    entry["pid"] = proc.pid
    return proc


def _is_clone_running(bot_id: int) -> bool:
    """Return True if the tracked subprocess for bot_id is still alive."""
    with _CLONE_PROC_LOCK:
        proc = _CLONE_PROCESSES.get(bot_id)
    return proc is not None and proc.poll() is None


def autostart_clones() -> None:
    """Launch all registered non-disabled clones. Called once on main bot startup."""
    entries = CLONES.get("clones") or []
    started = 0
    for entry in entries:
        if entry.get("status") == "disabled":
            continue
        try:
            bot_id = int(entry.get("bot_id", 0))
        except (TypeError, ValueError):
            continue
        if not bot_id or _is_clone_running(bot_id):
            continue
        proc = _launch_clone_process(entry)
        if proc:
            started += 1
            print(f"[CLONE AUTOSTART] @{entry.get('username')} PID={proc.pid}")
    if started:
        save_clones()


# ─────────────────────── BotFather newbot integration ─────────────────────────

async def _botfather_create_bot(name: str, username: str) -> tuple[str, str] | tuple[None, str]:
    """Interact with @BotFather via Telethon to create a new bot.

    Returns (token, final_username) on success, or (None, error_message) on failure.
    """
    await _ensure_tg_client_connected()
    async with tg_client.conversation("@BotFather", timeout=60) as conv:
        await conv.send_message("/newbot")
        r = await conv.get_response()
        text = r.text or ""

        # BotFather's first reply should ask for the bot's display name.
        if "name" not in text.lower() and "alright" not in text.lower():
            return None, f"Неожиданный ответ BotFather: {text[:200]}"

        await conv.send_message(name)
        r = await conv.get_response()
        text = r.text or ""

        # BotFather should now ask for the username.
        if "username" not in text.lower():
            return None, text[:300]

        clean_username = username.lower().lstrip("@")
        if not clean_username.endswith("bot"):
            clean_username += "bot"

        await conv.send_message(clean_username)
        r = await conv.get_response()
        text = r.text or ""

        m = _TOKEN_RE.search(text)
        if m:
            return m.group(1), clean_username

        # Username may be taken or invalid — return BotFather's error.
        return None, text[:300]


# ─────────────────────────── вспомогательные функции ─────────────────────────

def _is_owner(user: types.User | None) -> bool:
    if not user:
        return False
    return (user.username or "").lower() == (OWNER_USERNAME or "").lower()


def _find_clone(ref: str) -> dict | None:
    """Найти запись клона по username (без @) или bot_id."""
    ref = ref.strip().lstrip("@").lower()
    for entry in CLONES.get("clones", []):
        if str(entry.get("username", "")).lower() == ref:
            return entry
        if str(entry.get("bot_id", "")) == ref:
            return entry
    return None


def get_clone_entry_by_bot_id(bot_id: int) -> dict | None:
    """Найти запись клона по bot_id (используется клонами для самопроверки)."""
    for entry in CLONES.get("clones", []):
        try:
            if int(entry.get("bot_id", 0)) == int(bot_id):
                return entry
        except (TypeError, ValueError):
            pass
    return None


def _build_clones_keyboard(entries: list[dict]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for e in entries:
        bot_id = e.get("bot_id", "")
        username = e.get("username") or str(bot_id)
        status = e.get("status", "running")
        if status == "disabled":
            btn = types.InlineKeyboardButton(
                f"✅ Включить @{username}",
                callback_data=f"clone_enable:{bot_id}",
            )
        else:
            btn = types.InlineKeyboardButton(
                f"🔴 Отключить @{username}",
                callback_data=f"clone_disable:{bot_id}",
            )
        kb.add(btn)
    return kb


def _format_clones_text(entries: list[dict]) -> str:
    lines = ["<b>Список клонов:</b>\n"]
    for e in entries:
        status = e.get("status", "running")
        status_text = {
            "running": "✅ активен",
            "disabled": "🔴 отключён",
        }.get(status, f"⏸ {status}")
        username = _html.escape(str(e.get("username") or "?"))
        name = _html.escape(str(e.get("name") or ""))
        bot_id = e.get("bot_id", "?")
        role = _normalize_role(e.get("role", "clone"))
        role_label = "гость" if role == "guest" else "клон"
        lines.append(
            f"• <b>@{username}</b> ({name}) — ID: <code>{bot_id}</code>\n"
            f"  Тип: <i>{role_label}</i>\n"
            f"  Статус: <i>{status_text}</i>"
        )
    return "\n".join(lines)


def _register_bot_token(m: types.Message, token: str, role: str = "clone") -> bool:
    role = _normalize_role(role)
    is_guest = role == "guest"
    role_title = "Гостевой бот" if is_guest else "Клон"
    role_title_lower = "гостевой бот" if is_guest else "клон"

    try:
        test_bot = _tb.TeleBot(token)
        me = test_bot.get_me()
        bot_id = me.id
        bot_username = me.username or ""
        bot_name = me.first_name or bot_username
    except Exception as e:
        bot.reply_to(
            m,
            f"❌ Не удалось получить информацию о боте по токену:\n<code>{_html.escape(str(e))}</code>",
            parse_mode="HTML",
        )
        return False

    existing = _find_clone(str(bot_id)) or (bot_username and _find_clone(bot_username))
    if existing:
        bot.reply_to(
            m,
            f"{role_title} <b>@{_html.escape(bot_username)}</b> уже зарегистрирован.",
            parse_mode="HTML",
        )
        return False

    entry: dict = {
        "bot_id": bot_id,
        "username": bot_username,
        "name": bot_name,
        "token": token,
        "role": role,
        "status": "running",
        "created_at": int(time.time()),
    }
    CLONES["clones"].append(entry)
    save_clones()

    proc = _launch_clone_process(entry)
    if proc:
        save_clones()
        note = ""
        if is_guest:
            note = (
                "\n\n<i>Гостевой режим активирован.</i>\n"
                f"<code>@{_html.escape(bot_username)} имя_команды</code>"
            )
        bot.reply_to(
            m,
            f"✅ {role_title} <b>@{_html.escape(bot_username)}</b> (ID: <code>{bot_id}</code>) "
            f"зарегистрирован и запущен!\n"
            f"PID: <code>{proc.pid}</code>{note}",
            parse_mode="HTML",
        )
        return True

    env_lines = [
        f"<code>BOT_TOKEN={_html.escape(token)}</code>",
        "<code>IS_CLONE=1</code>",
        f"<code>IS_GUEST_BOT={'1' if is_guest else '0'}</code>",
        "<code>DATA_DIR=&lt;shared_data_dir&gt;</code>",
    ]
    bot.reply_to(
        m,
        f"✅ {role_title} <b>@{_html.escape(bot_username)}</b> (ID: <code>{bot_id}</code>) зарегистрирован.\n\n"
        f"⚠️ Автозапуск не удался. Для запуска задеплой {role_title_lower} со следующими переменными окружения:\n"
        f"{chr(10).join(env_lines)}\n\n"
        f"<i>Все клоны и основной бот должны использовать один и тот же DATA_DIR.</i>",
        parse_mode="HTML",
    )
    return True


# ─────────────────────────── команды ─────────────────────────────────────────

@bot.message_handler(commands=["clones"])
def cmd_clones(m: types.Message):
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return

    entries = CLONES.get("clones") or []
    if not entries:
        bot.reply_to(
            m,
            "Клонов нет.\n\nЧтобы добавить клон:\n<code>/clone_register TOKEN</code>",
            parse_mode="HTML",
        )
        return

    kb = _build_clones_keyboard(entries)
    bot.reply_to(m, _format_clones_text(entries), parse_mode="HTML", reply_markup=kb)


@bot.message_handler(commands=["clone_register"])
def cmd_clone_register(m: types.Message):
    """
    /clone_register TOKEN
    Регистрирует клон по токену, полученному от BotFather (/newbot).
    """
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Открыть BotFather", url="https://t.me/BotFather"))
        bot.reply_to(
            m,
            "<b>Создание клона</b>\n\n"
            "1. Перейди в @BotFather и создай нового бота командой /newbot\n"
            "2. Скопируй полученный токен\n"
            "3. Отправь: <code>/clone_register TOKEN</code>",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    _register_bot_token(m, parts[1].strip(), role="clone")


@bot.message_handler(commands=["clone_unlink"])
def cmd_clone_unlink(m: types.Message):
    """
    /clone_unlink <username|bot_id>
    Удаляет клон из реестра и освобождает все его группы.
    """
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            m,
            "Использование: <code>/clone_unlink &lt;username|bot_id&gt;</code>",
            parse_mode="HTML",
        )
        return

    ref = parts[1].strip()
    entry = _find_clone(ref)
    if not entry:
        bot.reply_to(m, f"Клон <code>{_html.escape(ref)}</code> не найден.", parse_mode="HTML")
        return

    username = _html.escape(str(entry.get("username") or ref))
    bot_id = int(entry.get("bot_id") or 0)

    # Освобождаем группы, привязанные к этому клону
    _unassign_groups_for_bot(bot_id)

    CLONES["clones"] = [
        e for e in CLONES["clones"]
        if str(e.get("bot_id")) != str(bot_id)
    ]
    save_clones()

    bot.reply_to(
        m,
        f"Клон <b>@{username}</b> (ID <code>{bot_id}</code>) удалён из реестра.",
        parse_mode="HTML",
    )


# ─────────────────────── callback: включить / отключить ──────────────────────

@bot.callback_query_handler(
    func=lambda c: c.data and c.data.startswith(("clone_disable:", "clone_enable:"))
)
def _clone_toggle_callback(call: types.CallbackQuery):
    if not _is_owner(call.from_user):
        bot.answer_callback_query(call.id, "Недостаточно прав.")
        return

    data = call.data or ""
    action, _, bot_id_str = data.partition(":")
    bot_id_str = bot_id_str.strip()

    entry = _find_clone(bot_id_str)
    if not entry:
        bot.answer_callback_query(call.id, "Клон не найден.")
        return

    username = str(entry.get("username") or bot_id_str)

    if action == "clone_disable":
        entry["status"] = "disabled"
        save_clones()
        bot.answer_callback_query(call.id, f"Клон @{username} отключён.")
    elif action == "clone_enable":
        entry["status"] = "running"
        save_clones()
        # Relaunch the subprocess if it is not already running.
        bot_id_int = int(bot_id_str) if bot_id_str.isdigit() else 0
        if bot_id_int and not _is_clone_running(bot_id_int):
            proc = _launch_clone_process(entry)
            if proc:
                save_clones()
        bot.answer_callback_query(call.id, f"Клон @{username} включён.")
    else:
        bot.answer_callback_query(call.id)
        return

    # Обновляем сообщение со списком клонов
    entries = CLONES.get("clones") or []
    kb = _build_clones_keyboard(entries)
    try:
        bot.edit_message_text(
            _format_clones_text(entries),
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass


@bot.message_handler(commands=["newbot"])
def cmd_newbot(m: types.Message):
    """
    /newbot <display_name> <username>
    Создаёт нового бота через BotFather (Telethon MTProto), затем регистрирует
    и запускает его как клон.
    Последнее слово — username (@…bot), всё перед ним — display name.
    """
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return

    parts = m.text.split()
    if len(parts) < 3:
        bot.reply_to(
            m,
            "<b>Создание нового бота через BotFather</b>\n\n"
            "Использование: <code>/newbot Название @username</code>\n\n"
            "Пример: <code>/newbot Мой клон myclone_bot</code>\n\n"
            "<i>Последнее слово — username (суффикс «bot» добавляется автоматически).\n"
            "Требуется авторизованная MTProto-сессия (Telethon).</i>",
            parse_mode="HTML",
        )
        return

    username = parts[-1].strip().lstrip("@")
    display_name = " ".join(parts[1:-1]).strip()
    _start_new_bot_creation(m, display_name, username, role="clone")


@bot.message_handler(commands=["newguest"])
def cmd_newguest(m: types.Message):
    """
    /newguest [TOKEN]
    Регистрирует готового гостевого бота по токену и запускает его как гостя.
    """
    if should_ignore_text_triggers(m):
        return
    if m.chat.type != "private" or not _is_owner(m.from_user):
        return

    parts = m.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        start_guest_registration_prompt(m.chat.id, m.from_user)
        return

    if _register_bot_token(m, parts[1].strip(), role="guest"):
        _clear_pending_guest_registration(m.from_user.id)


def _is_waiting_for_guest_token(m: types.Message) -> bool:
    if should_ignore_text_triggers(m):
        return False
    if m.chat.type != "private" or not _is_owner(m.from_user) or not m.text:
        return False
    if int(m.from_user.id) not in _PENDING_GUEST_REGISTRATION:
        return False

    command = m.text.strip().split(maxsplit=1)[0].lower()
    return command in _GUEST_REG_CANCEL_WORDS or not command.startswith("/")


@bot.message_handler(func=_is_waiting_for_guest_token)
def on_guest_token_message(m: types.Message):
    text = m.text.strip()
    lower_text = text.lower()
    if lower_text in _GUEST_REG_CANCEL_WORDS:
        _clear_pending_guest_registration(m.from_user.id)
        bot.reply_to(m, "Подключение гостевого бота отменено.")
        return

    token_match = _TOKEN_RE.search(text)
    if not token_match:
        bot.reply_to(
            m,
            "Не вижу токен BotFather. Отправь токен целиком одним сообщением или напиши <code>отмена</code>.",
            parse_mode="HTML",
        )
        return

    if _register_bot_token(m, token_match.group(1), role="guest"):
        _clear_pending_guest_registration(m.from_user.id)


def _start_new_bot_creation(m: types.Message, display_name: str, username: str, role: str = "clone") -> None:
    role = _normalize_role(role)
    is_guest = role == "guest"
    role_word = "гостя" if is_guest else "бота"
    role_label = "гостя" if is_guest else "клона"

    wait_msg = bot.reply_to(m, f"⏳ Создаю {role_word} через @BotFather…")

    def _do_create() -> None:
        try:
            result = call_mtproto_sync(
                _botfather_create_bot(display_name, username),
                timeout=120,
            )
        except Exception as e:
            bot.edit_message_text(
                f"❌ Ошибка при общении с @BotFather:\n<code>{_html.escape(str(e))}</code>",
                wait_msg.chat.id, wait_msg.message_id,
                parse_mode="HTML",
            )
            return

        token, info = result  # (token, username) or (None, error_msg)
        if token is None:
            bot.edit_message_text(
                f"❌ Не удалось создать бота.\n\nОтвет BotFather:\n{_html.escape(info)}",
                wait_msg.chat.id, wait_msg.message_id,
                parse_mode="HTML",
            )
            return

        final_username = info  # BotFather confirmed this username
        try:
            test_bot = _tb.TeleBot(token)
            me = test_bot.get_me()
            bot_id = me.id
            bot_username = me.username or final_username
            bot_name = me.first_name or bot_username
        except Exception as e:
            bot.edit_message_text(
                f"⚠️ Токен получен, но не удалось проверить бота:\n"
                f"<code>{_html.escape(str(e))}</code>\n\n"
                f"Токен: <code>{_html.escape(token)}</code>",
                wait_msg.chat.id, wait_msg.message_id,
                parse_mode="HTML",
            )
            return

        if _find_clone(str(bot_id)) or _find_clone(bot_username):
            bot.edit_message_text(
                f"Бот <b>@{_html.escape(bot_username)}</b> уже зарегистрирован.",
                wait_msg.chat.id, wait_msg.message_id,
                parse_mode="HTML",
            )
            return

        entry: dict = {
            "bot_id": bot_id,
            "username": bot_username,
            "name": bot_name,
            "token": token,
            "role": role,
            "status": "running",
            "created_at": int(time.time()),
        }
        CLONES["clones"].append(entry)
        save_clones()

        proc = _launch_clone_process(entry)
        if proc:
            save_clones()
            pid_info = f"\nPID: <code>{proc.pid}</code>"
        else:
            pid_info = "\n⚠️ Автозапуск не удался — запусти бота вручную."

        note = ""
        if is_guest:
            note = (
                "\n\n<i>В гостевом режиме пользовательские команды работают только так:</i>\n"
                f"<code>@{_html.escape(bot_username)} имя_команды</code>"
            )

        bot.edit_message_text(
            f"✅ Бот <b>@{_html.escape(bot_username)}</b> создан через BotFather "
            f"и запущен как {role_label}!\n"
            f"ID: <code>{bot_id}</code>{pid_info}{note}",
            wait_msg.chat.id, wait_msg.message_id,
            parse_mode="HTML",
        )

    _threading.Thread(target=_do_create, daemon=False, name=f"new{role}-{username}").start()


# ─────────────────────────── вспомогательные ─────────────────────────────────

def _unassign_groups_for_bot(bot_id: int) -> None:
    """Удаляет все записи chat_bot_assignment для данного bot_id."""
    from persistence import _db_connect, _DB_LOCK
    try:
        conn = _db_connect()
        with _DB_LOCK:
            conn.execute(
                "DELETE FROM chat_bot_assignment WHERE bot_id = ?",
                (int(bot_id),),
            )
            conn.commit()
    except Exception as e:
        print(f"[UNASSIGN GROUPS] Error for bot_id={bot_id}: {e}")
