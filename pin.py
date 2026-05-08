"""
pin.py — Команды закрепления сообщений:
  /pin, /spin, /npin, /unpin, очередь операций,
  pin-callback обработчики.
"""
from __future__ import annotations
import time
import threading
import asyncio

from config import (
    os, json, re, datetime,
    Any, Dict, List, Optional, Tuple,
    types, apihelper, telebot, ContinueHandling,
    ApiTelegramException, InlineKeyboardMarkup, InlineKeyboardButton,
    bot, bot_raw, tg_client,
    TOKEN, OWNER_USERNAME, DATA_DIR,
    COMMAND_PREFIXES, MAX_MSG_LEN,
    MessageService, PeerChannel, PeerChat,
    get_user_id_by_username_mtproto,
    call_mtproto_sync,
    get_bot_me,
)
from persistence import (
    VERIFY_ADMINS, VERIFY_DEV,
    CLOSE_CHAT_STATE, GROUP_STATS, GROUP_SETTINGS,
    CHAT_SETTINGS, MODERATION, PENDING_GROUPS,
    USERS, GLOBAL_USERS, PROFILES,
    CHAT_ROLES, ROLE_PERMS,
    STATS,
    _OPERATION_QUEUE, _OPERATION_QUEUE_LOCK,
    _OPERATION_QUEUE_ACTIVE, _OPERATION_QUEUE_NEXT_ID,
    OPERATION_QUEUE_MAX_RETRIES, OPERATION_QUEUE_MAX_BACKOFF_SECONDS,
    save_chat_settings, save_moderation,
    save_users, save_global_users,
    tg_get_chat, tg_get_chat_member,
    tg_invalidate_member_cache, tg_invalidate_chat_cache,
    tg_invalidate_chat_member_caches,
    load_json_file, save_json_file, throttled_save_json_file,
    _is_duplicate_callback_query,
    GLOBAL_LAST_SEEN_UPDATE_SECONDS,
)
from helpers import *

# ==== УДАЛЕНИЕ СЕРВИСНОГО СООБЩЕНИЯ О ПИНЕ (TELETHON) ==== 
from cmd_basic import _broadcast_send_payload_once


async def _mt_delete_last_pin_service_message(chat_id: int):
    """
    Удаляет последнее сервисное сообщение о закреплении в данном чате.
    Для супергрупп используем PeerChannel(chat_id), для обычных групп — PeerChat(-chat_id).
    Используем минимальную задержку (0.05 с) вместо 0.3 с — Telegram создаёт
    service-message практически мгновенно после закрепления.
    """
    try:
        await tg_client.start()

        # минимальная задержка: Telegram создаёт service-message сразу после пина
        await asyncio.sleep(0.05)

        # определяем peer
        if str(chat_id).startswith("-100"):
            # супергруппа: id вида -100..., Telethon ждёт положительный id канала
            peer = PeerChannel(-chat_id)
        elif chat_id < 0:
            # обычная группа: отрицательный id -> PeerChat с положительным
            peer = PeerChat(-chat_id)
        else:
            # ЛС/канал по прямому id (на всякий случай)
            peer = chat_id

        # ищем самое верхнее сервисное сообщение о пине (с таймаутом 5 с)
        async def _search_and_delete():
            async for msg in tg_client.iter_messages(peer, limit=30):
                if isinstance(msg, MessageService) and getattr(msg, "action", None):
                    if msg.action.__class__.__name__ == "MessageActionPinMessage":
                        await msg.delete()
                        break

        await asyncio.wait_for(_search_and_delete(), timeout=5.0)
    except asyncio.TimeoutError:
        print(f"[Telethon] Таймаут поиска сервисного сообщения о пине в чате {chat_id}")
    except Exception as e:
        print(f"[Telethon] Не удалось удалить сервисное сообщение о пине: {e}")


def _try_delete_last_bot_service_pin(chat_id: int):
    """
    Запускает удаление сервисного сообщения о пине полностью в фоне,
    не блокируя вызывающий поток. Использует постоянный MTProto-loop
    вместо создания нового event loop на каждый вызов.
    """
    def _run_in_thread():
        call_mtproto_sync(_mt_delete_last_pin_service_message(chat_id))

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()


# ==== ЗАКРЕПЛЕНИЕ / ОТКРЕПЛЕНИЕ СООБЩЕНИЙ ==== 

PIN_INTERFACE_EXPIRE_SECONDS = 60
PIN_NOTIFY_KEEP_SERVICE_SECONDS = 20
PIN_NOTIFY_KEEP_SERVICE_UNTIL: dict[int, float] = {}


def _mark_keep_pin_service_message(chat_id: int):
    PIN_NOTIFY_KEEP_SERVICE_UNTIL[chat_id] = time.time() + PIN_NOTIFY_KEEP_SERVICE_SECONDS


def _should_keep_pin_service_message(chat_id: int) -> bool:
    until_ts = float(PIN_NOTIFY_KEEP_SERVICE_UNTIL.get(chat_id) or 0)
    if until_ts <= 0:
        return False
    if time.time() > until_ts:
        PIN_NOTIFY_KEEP_SERVICE_UNTIL.pop(chat_id, None)
        return False
    PIN_NOTIFY_KEEP_SERVICE_UNTIL.pop(chat_id, None)
    return True


def _user_can_do_pin_perm(chat_id: int, user_id: int, perm_name: str) -> tuple[bool, str, str | None]:
    """
    Общая проверка для закрепа/открепа.
    Возвращает (allowed, status, err_text_for_command_or_None).
    status: 'ok' / 'no_rank' / 'no_perm' / другое.
    Для КОМАНД:
      - 'no_rank' -> молчим,
      - 'no_perm' -> err_text,
      - ok -> err_text None.
    Для КНОПОК:
      - 'no_rank' -> False, callback молчит,
      - 'no_perm' -> False, текст отдаём в колбэк.
    """
    status, allowed = check_role_permission(chat_id, user_id, perm_name)
    if allowed:
        return True, 'ok', None

    if status == 'no_rank':
        return False, 'no_rank', None

    if status == 'no_perm':
        if perm_name == PERM_PIN:
            return False, 'no_perm', "У вашей должности нет права закреплять сообщения."
        if perm_name == PERM_UNPIN:
            return False, 'no_perm', "У вашей должности нет права откреплять сообщения."
        return False, 'no_perm', "У вашей должности нет нужного права."

    return False, status, "Вы не можете управлять закрепами."


def _bot_can_pin(chat_id: int) -> bool:
    """
    Проверяем, есть ли у бота право закреплять сообщения.
    """
    try:
        me = get_bot_me()
        member = bot.get_chat_member(chat_id, me.id)
        can_pin = getattr(member, "can_pin_messages", None)
        if member.status in ("administrator", "creator") and (can_pin is None or can_pin):
            return True
        if member.status in ("administrator", "creator") and can_pin is None:
            return True
        return False
    except Exception:
        return False


def _pin_message(chat_id: int, reply_msg_id: int, silent: bool) -> bool:
    """
    Пытаемся закрепить сообщение reply_msg_id.
    """
    if not _bot_can_pin(chat_id):
        return False
    try:
        _pin_message_or_raise(chat_id, reply_msg_id, silent)
        return True
    except ApiTelegramException:
        return False
    except Exception:
        return False


def _unpin_message(chat_id: int, msg_id: int | None = None) -> bool:
    """
    Открепить либо конкретное сообщение (если msg_id дан),
    либо последнее закреплённое (если msg_id None).
    """
    try:
        _unpin_message_or_raise(chat_id, msg_id)
        return True
    except ApiTelegramException:
        return False
    except Exception:
        return False


def _pin_message_or_raise(chat_id: int, reply_msg_id: int, silent: bool) -> None:
    if not _bot_can_pin(chat_id):
        raise RuntimeError("У бота нет прав для закрепления сообщений.")
    bot.pin_chat_message(chat_id, reply_msg_id, disable_notification=silent)


def _unpin_message_or_raise(chat_id: int, msg_id: int | None = None) -> None:
    if msg_id is not None:
        bot.unpin_chat_message(chat_id, msg_id)
    else:
        bot.unpin_all_chat_messages(chat_id)


def _next_operation_queue_id() -> int:
    global _OPERATION_QUEUE_NEXT_ID
    with _OPERATION_QUEUE_LOCK:
        _OPERATION_QUEUE_NEXT_ID += 1
        return _OPERATION_QUEUE_NEXT_ID


def get_operation_queue_size() -> int:
    with _OPERATION_QUEUE_LOCK:
        active_count = len(_OPERATION_QUEUE_ACTIVE)
    return _OPERATION_QUEUE.qsize() + active_count


def get_operation_queue_stats() -> dict[str, int]:
    with _OPERATION_QUEUE_LOCK:
        active_count = len(_OPERATION_QUEUE_ACTIVE)
    queued_count = _OPERATION_QUEUE.qsize()
    return {
        "queued": queued_count,
        "active": active_count,
        "total": queued_count + active_count,
    }


def enqueue_operation(kind: str, payload: dict[str, Any]) -> tuple[int, int]:
    operation_id = _next_operation_queue_id()
    task = {
        "id": operation_id,
        "kind": kind,
        "created_at": time.time(),
    }
    task.update(payload)
    _OPERATION_QUEUE.put(task)
    return operation_id, _OPERATION_QUEUE.qsize()


def _extract_retry_after_seconds(exc: Exception) -> int | None:
    seconds = getattr(exc, "seconds", None)
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds)

    raw_parts = [
        str(getattr(exc, "description", "") or ""),
        str(exc or ""),
    ]
    raw_text = " ".join(part for part in raw_parts if part).lower()
    for pattern in (
        r"retry after (\d+)",
        r"flood(?:[ _-]?wait)?(?: of)? (\d+)",
        r"too many requests.*?(\d+)",
    ):
        match = _re.search(pattern, raw_text)
        if match:
            try:
                value = int(match.group(1))
            except Exception:
                value = 0
            if value > 0:
                return value
    return None


def _is_retryable_telegram_exception(exc: Exception) -> bool:
    class_name = exc.__class__.__name__.lower()
    if "floodwait" in class_name:
        return True

    if not isinstance(exc, ApiTelegramException):
        return False

    description = str(getattr(exc, "description", "") or exc).lower()
    retry_markers = (
        "retry after",
        "too many requests",
        "flood",
        "temporarily unavailable",
        "timeout",
        "timed out",
    )
    return any(marker in description for marker in retry_markers)


def _operation_backoff_seconds(exc: Exception, attempt: int) -> float:
    retry_after = _extract_retry_after_seconds(exc)
    if retry_after is not None:
        return float(min(max(1, retry_after), OPERATION_QUEUE_MAX_BACKOFF_SECONDS))
    return min(float(2 ** max(0, attempt - 1)) + random.uniform(0.0, 0.25), float(OPERATION_QUEUE_MAX_BACKOFF_SECONDS))


def _run_operation_with_retry(fn, *, label: str):
    last_error: Exception | None = None
    for attempt in range(1, OPERATION_QUEUE_MAX_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            last_error = exc
            if attempt >= OPERATION_QUEUE_MAX_RETRIES or not _is_retryable_telegram_exception(exc):
                raise
            delay = _operation_backoff_seconds(exc, attempt)
            print(f"[OP_QUEUE] retry {attempt}/{OPERATION_QUEUE_MAX_RETRIES} for {label} after {delay:.2f}s: {exc}")
            time.sleep(delay)

    if last_error is not None:
        raise last_error


def _notify_operation_failure(chat_id: int, text: str, *, reply_to_message_id: int | None = None) -> None:
    try:
        bot.send_message(
            chat_id,
            premium_prefix(text),
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_to_message_id=reply_to_message_id,
        )
    except Exception:
        pass


def _process_broadcast_queue_task(task: dict[str, Any]) -> None:
    panel_chat_id = int(task["panel_chat_id"])
    panel_message_id = int(task["panel_message_id"])
    html_text = str(task.get("html_text") or "")
    media = list(task.get("media") or [])
    buttons = dict(task.get("buttons") or {})
    targets = list(task.get("targets") or [])

    sent_ok = 0
    sent_err = 0
    for uid in targets:
        try:
            _run_operation_with_retry(
                lambda uid=uid: _broadcast_send_payload_once(uid, html_text, media, buttons),
                label=f"broadcast:{uid}",
            )
            sent_ok += 1
        except Exception:
            sent_err += 1
        time.sleep(0.03)

    summary = (
        f'<tg-emoji emoji-id="{EMOJI_SENT_OK_ID}">✅</tg-emoji> <b>Сообщение отправлено!</b>\n\n'
        f"<b>Успешно:</b> <code>{sent_ok}</code>\n"
        f"<b>Ошибок:</b> <code>{sent_err}</code>"
    )

    try:
        bot.edit_message_text(
            summary,
            chat_id=panel_chat_id,
            message_id=panel_message_id,
            parse_mode='HTML',
            disable_web_page_preview=True,
        )
    except Exception:
        try:
            bot.send_message(panel_chat_id, summary, parse_mode='HTML', disable_web_page_preview=True)
        except Exception:
            pass


def _process_pin_apply_queue_task(task: dict[str, Any]) -> None:
    chat_id = int(task["chat_id"])
    iface_msg_id = int(task["iface_msg_id"])
    reply_msg_id = int(task["reply_msg_id"])
    silent = bool(task.get("silent", False))

    _run_operation_with_retry(
        lambda: _pin_message_or_raise(chat_id, reply_msg_id, silent),
        label=f"pin:{chat_id}:{reply_msg_id}",
    )

    if silent:
        _try_delete_last_bot_service_pin(chat_id)
    else:
        _mark_keep_pin_service_message(chat_id)

    try:
        bot.delete_message(chat_id, iface_msg_id)
    except Exception:
        pass


def _process_pin_repin_queue_task(task: dict[str, Any]) -> None:
    chat_id = int(task["chat_id"])
    iface_msg_id = int(task["iface_msg_id"])
    reply_msg_id = int(task["reply_msg_id"])

    _run_operation_with_retry(
        lambda: _unpin_message_or_raise(chat_id, reply_msg_id),
        label=f"repin:{chat_id}:{reply_msg_id}",
    )

    try:
        bot.delete_message(chat_id, iface_msg_id)
    except Exception:
        pass

    fake_trigger = types.Message(message_id=iface_msg_id, chat=types.Chat(chat_id, 'group'), date=0)
    fake_target = types.Message(message_id=reply_msg_id, chat=types.Chat(chat_id, 'group'), date=0)
    _send_pin_interface(chat_id, fake_trigger, fake_target)


def _dispatch_operation_queue_task(task: dict[str, Any]) -> None:
    kind = str(task.get("kind") or "")
    if kind == "broadcast_send":
        _process_broadcast_queue_task(task)
        return
    if kind == "pin_apply":
        _process_pin_apply_queue_task(task)
        return
    if kind == "pin_repin":
        _process_pin_repin_queue_task(task)
        return
    raise RuntimeError(f"Неизвестный тип операции очереди: {kind}")


def _operation_queue_worker():
    while True:
        task = _OPERATION_QUEUE.get()
        task_id = int(task.get("id") or 0)
        with _OPERATION_QUEUE_LOCK:
            _OPERATION_QUEUE_ACTIVE[task_id] = task
        try:
            _dispatch_operation_queue_task(task)
        except Exception as exc:
            print(f"[OP_QUEUE] task {task_id} failed: {exc}")
            kind = str(task.get("kind") or "")
            if kind == "broadcast_send":
                _notify_operation_failure(int(task.get("panel_chat_id") or 0), "Не удалось завершить рассылку.")
            elif kind in ("pin_apply", "pin_repin"):
                _notify_operation_failure(
                    int(task.get("chat_id") or 0),
                    "Не удалось выполнить операцию закрепа.",
                    reply_to_message_id=int(task.get("reply_msg_id") or 0) or None,
                )
        finally:
            with _OPERATION_QUEUE_LOCK:
                _OPERATION_QUEUE_ACTIVE.pop(task_id, None)
            _OPERATION_QUEUE.task_done()


_OPERATION_QUEUE_THREAD = threading.Thread(target=_operation_queue_worker, daemon=True)
_OPERATION_QUEUE_THREAD.start()


def _is_exact_command(m: types.Message, names: list[str]) -> bool:
    """
    Текст — ровно команда из списка (без аргументов),
    с префиксом из COMMAND_PREFIXES или русское слово без префикса.
    """
    if should_ignore_text_triggers(m):
        return False
    text = (m.text or "").strip()
    if not text:
        return False

    lower = text.lower()

    # без префикса (русские)
    for name in names:
        if all('а' <= ch <= 'я' or ch == 'ё' for ch in name.lower()) and lower == name:
            return True

    # с префиксами
    for prefix in COMMAND_PREFIXES:
        for name in names:
            if lower == f"{prefix}{name}":
                return True

    return False


def _get_last_pinned_message_id(chat_id: int) -> int | None:
    """
    Получаем последнее закреплённое сообщение через Bot API. [file:195]
    """
    try:
        chat = bot.get_chat(chat_id)
        pinned = getattr(chat, "pinned_message", None)
        if isinstance(pinned, types.Message):
            return pinned.message_id
    except Exception:
        pass
    return None


def _build_pin_interface_keyboard(chat_id: int, iface_msg_id: int, replied_msg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "С уведомлением",
            callback_data=f"pin_notify:{chat_id}:{iface_msg_id}:{replied_msg_id}",
            icon_custom_emoji_id=str(EMOJI_PIN_NOTIFY_ID)
        ),
    )
    kb.row(
        InlineKeyboardButton(
            "Без уведомления",
            callback_data=f"pin_silent:{chat_id}:{iface_msg_id}:{replied_msg_id}",
            icon_custom_emoji_id=str(EMOJI_PIN_SILENT_ID)
        ),
    )
    kb.row(
        InlineKeyboardButton(
            "Отменить",
            callback_data=f"pin_cancel:{chat_id}:{iface_msg_id}",
            icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
        )
    )
    return kb


def _build_pin_repin_keyboard(chat_id: int, iface_msg_id: int, replied_msg_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(
            "Открепить и закрепить снова",
            callback_data=f"pin_repin:{chat_id}:{iface_msg_id}:{replied_msg_id}",
            icon_custom_emoji_id=str(EMOJI_PIN_REPIN_ID)
        )
    )
    kb.row(
        InlineKeyboardButton(
            "Отменить",
            callback_data=f"pin_cancel:{chat_id}:{iface_msg_id}",
            icon_custom_emoji_id=str(EMOJI_ROLE_SETTINGS_CANCEL_ID)
        )
    )
    return kb


def _send_pin_interface(chat_id: int, trigger_msg: types.Message, target_msg: types.Message):
    """
    Отправить сообщение с выбором типа закрепа.
    Если target_msg уже закреплено — даём кнопку «Открепить и закрепить снова».
    Сообщение интерфейса отправляем reply на команду и добавляем имя закрепляющего.
    """
    if not _bot_can_pin(chat_id):
        return bot.reply_to(
            trigger_msg,
            premium_prefix("У бота нет прав для закрепления сообщений."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    actor_name = link_for_user(chat_id, trigger_msg.from_user.id)

    current_pinned_id = _get_last_pinned_message_id(chat_id)
    if current_pinned_id == target_msg.message_id:
        text = (
            "<b>Это сообщение уже закреплено.</b>\n"
            f"Закрепляет: {actor_name}"
        )
        sent = bot.reply_to(
            trigger_msg,
            text,
            parse_mode='HTML',
            disable_web_page_preview=True,
            reply_markup=_build_pin_repin_keyboard(chat_id, 0, target_msg.message_id)
        )
        kb_fixed = _build_pin_repin_keyboard(chat_id, sent.message_id, target_msg.message_id)
        try:
            bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=kb_fixed)
        except Exception:
            pass
        return

    text = (
        "<b>Выберите как закрепить это сообщение:</b>\n"
        f"Закрепляет: {actor_name}"
    )
    sent = bot.reply_to(
        trigger_msg,
        text,
        parse_mode='HTML',
        disable_web_page_preview=True,
        reply_markup=_build_pin_interface_keyboard(chat_id, 0, target_msg.message_id)
    )
    kb_fixed = _build_pin_interface_keyboard(chat_id, sent.message_id, target_msg.message_id)
    try:
        bot.edit_message_reply_markup(chat_id, sent.message_id, reply_markup=kb_fixed)
    except Exception:
        pass


# ==== КОМАНДЫ ПИНА / СПИН / НПИН ====


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and _is_exact_command(m, ["pin", "пин", "закреп", "закрепить"]))
def cmd_pin(m: types.Message):
    add_stat_message(m)
    add_stat_command('pin')

    # Проверка одобрения группы
    if not check_group_approval(m):
        return

    if not m.reply_to_message:
        return

    chat_id = m.chat.id
    user_id = m.from_user.id

    allowed, status, err_text = _user_can_do_pin_perm(chat_id, user_id, PERM_PIN)
    if not allowed:
        if status == 'no_perm' and err_text:
            return bot.reply_to(
                m,
                premium_prefix(err_text),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    _send_pin_interface(chat_id, m, m.reply_to_message)


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and _is_exact_command(m, ["spin"]))
def cmd_spin(m: types.Message):
    """
    /spin — тихое закрепление сразу.
    """
    add_stat_message(m)
    add_stat_command('spin')

    # Проверка одобрения группы
    if not check_group_approval(m):
        return

    if not m.reply_to_message:
        return

    chat_id = m.chat.id
    user_id = m.from_user.id

    allowed, status, err_text = _user_can_do_pin_perm(chat_id, user_id, PERM_PIN)
    if not allowed:
        if status == 'no_perm' and err_text:
            return bot.reply_to(
                m,
                premium_prefix(err_text),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    if not _bot_can_pin(chat_id):
        return bot.reply_to(
            m,
            premium_prefix("У бота нет прав для закрепления сообщений."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    ok = _pin_message(chat_id, m.reply_to_message.message_id, silent=True)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось закрепить сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    _try_delete_last_bot_service_pin(chat_id)


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and _is_exact_command(m, ["npin"]))
def cmd_npin(m: types.Message):
    """
    /npin — закрепление с уведомлением сразу.
    """
    add_stat_message(m)
    add_stat_command('npin')

    if not m.reply_to_message:
        return

    chat_id = m.chat.id
    user_id = m.from_user.id

    allowed, status, err_text = _user_can_do_pin_perm(chat_id, user_id, PERM_PIN)
    if not allowed:
        if status == 'no_perm' and err_text:
            return bot.reply_to(
                m,
                premium_prefix(err_text),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    if not _bot_can_pin(chat_id):
        return bot.reply_to(
            m,
            premium_prefix("У бота нет прав для закрепления сообщений."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    ok = _pin_message(chat_id, m.reply_to_message.message_id, silent=False)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось закрепить сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    _mark_keep_pin_service_message(chat_id)


# ==== КОМАНДЫ ОТКРЕПЛЕНИЯ ====


@bot.message_handler(func=lambda m: m.chat.type in ['group', 'supergroup'] and _is_exact_command(m, ["unpin", "анпин", "откреп", "открепить"]))
def cmd_unpin(m: types.Message):
    add_stat_message(m)
    add_stat_command('unpin')

    # Проверка одобрения группы
    if not check_group_approval(m):
        return

    if not m.reply_to_message:
        return

    chat_id = m.chat.id
    user_id = m.from_user.id

    allowed, status, err_text = _user_can_do_pin_perm(chat_id, user_id, PERM_UNPIN)
    if not allowed:
        if status == 'no_perm' and err_text:
            txt = err_text.replace("закреплять", "откреплять")
            return bot.reply_to(
                m,
                premium_prefix(txt),
                parse_mode='HTML',
                disable_web_page_preview=True
            )
        return

    if not _bot_can_pin(chat_id):
        return bot.reply_to(
            m,
            premium_prefix("У бота нет прав для закрепления сообщений."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    ok = _unpin_message(chat_id, m.reply_to_message.message_id)
    if not ok:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось открепить сообщение."),
            parse_mode='HTML',
            disable_web_page_preview=True
        )

    emoji_saved = '<tg-emoji emoji-id="5427009714745517609">✅</tg-emoji>'
    text = f"{emoji_saved} Сообщение откреплено."
    bot.reply_to(m, text, parse_mode='HTML', disable_web_page_preview=True)


# ==== CALLBACK-КНОПКИ ПИНА ====


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith(("pin_notify:", "pin_silent:", "pin_cancel:", "pin_repin:")))
def cb_pin_interface(c: types.CallbackQuery):
    if _is_duplicate_callback_query(c):
        return
    data = c.data or ""
    user = c.from_user

    try:
        if data.startswith("pin_cancel:"):
            _, chat_id_s, iface_msg_id_s = data.split(":", 2)
            chat_id = int(chat_id_s)
            iface_msg_id = int(iface_msg_id_s)

            allowed, status, _ = _user_can_do_pin_perm(chat_id, user.id, PERM_PIN)
            if not allowed:
                if status == 'no_perm':
                    bot.answer_callback_query(
                        c.id,
                        "У вашей должности нет права управлять закрепами.",
                        show_alert=True
                    )
                return

            try:
                bot.delete_message(chat_id, iface_msg_id)
            except Exception:
                pass
            bot.answer_callback_query(c.id)
            return

        if data.startswith("pin_repin:"):
            _, chat_id_s, iface_msg_id_s, reply_msg_id_s = data.split(":", 3)
            chat_id = int(chat_id_s)
            iface_msg_id = int(iface_msg_id_s)
            reply_msg_id = int(reply_msg_id_s)

            allowed, status, _ = _user_can_do_pin_perm(chat_id, user.id, PERM_PIN)
            if not allowed:
                if status == 'no_perm':
                    bot.answer_callback_query(
                        c.id,
                        "У вашей должности нет права закреплять сообщения.",
                        show_alert=True
                    )
                return

            if not _bot_can_pin(chat_id):
                bot.answer_callback_query(
                    c.id,
                    "У бота нет прав для закрепления сообщений.",
                    show_alert=True
                )
                return

            _, queue_size = enqueue_operation(
                "pin_repin",
                {
                    "chat_id": chat_id,
                    "iface_msg_id": iface_msg_id,
                    "reply_msg_id": reply_msg_id,
                    "actor_id": user.id,
                },
            )

            bot.answer_callback_query(c.id, f"Операция поставлена в очередь ({queue_size}).")
            return

        mode, chat_id_s, iface_msg_id_s, reply_msg_id_s = data.split(":", 3)
        chat_id = int(chat_id_s)
        iface_msg_id = int(iface_msg_id_s)
        reply_msg_id = int(reply_msg_id_s)
        silent = (mode == "pin_silent")

        allowed, status, _ = _user_can_do_pin_perm(chat_id, user.id, PERM_PIN)
        if not allowed:
            if status == 'no_perm':
                bot.answer_callback_query(
                    c.id,
                    "У вашей должности нет права закреплять сообщения.",
                    show_alert=True
                )
            return

        if not _bot_can_pin(chat_id):
            bot.answer_callback_query(
                c.id,
                "У бота нет прав для закрепления сообщений.",
                show_alert=True
            )
            return

        _, queue_size = enqueue_operation(
            "pin_apply",
            {
                "chat_id": chat_id,
                "iface_msg_id": iface_msg_id,
                "reply_msg_id": reply_msg_id,
                "actor_id": user.id,
                "silent": silent,
            },
        )

        bot.answer_callback_query(c.id, f"Закрепление поставлено в очередь ({queue_size}).")

    except Exception:
        try:
            bot.answer_callback_query(c.id)
        except Exception:
            pass


# ============================================


__all__ = [name for name in globals() if not name.startswith('__')]
