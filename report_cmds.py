"""
report_cmds.py — Репорты пользователей:
  /report (reply only), @admin (reply only), /reports (управление модулем)
"""
from __future__ import annotations

import html as _html
from typing import Optional

from config import bot, types, get_bot_me
from persistence import CHAT_SETTINGS, save_chat_settings
from helpers import (
    add_stat_command,
    add_stat_message,
    check_group_approval,
    check_role_permission,
    cooldown_hit,
    is_channel_post_message,
    is_chat_admin,
    is_group_approved,
    match_command_aliases,
    mention_html_by_id,
    premium_prefix,
    reply_cooldown_message,
    should_ignore_text_triggers,
    _is_special_actor,
    PERM_VIEW_LISTS,
)


REPORTS_COOLDOWN_SECONDS = 30
REPORT_MENTION_ALIASES = {"@admin", "@админ"}


def _get_reports_state(chat_id: int) -> dict:
    cid = str(int(chat_id))
    chat = CHAT_SETTINGS.setdefault(cid, {})
    state = chat.get("reports")
    if not isinstance(state, dict):
        state = {}
    state.setdefault("enabled", True)
    state.setdefault("blocked_users", [])
    if not isinstance(state.get("blocked_users"), list):
        state["blocked_users"] = []
    chat["reports"] = state
    return state


def _set_reports_state(chat_id: int, state: dict) -> None:
    CHAT_SETTINGS.setdefault(str(int(chat_id)), {})["reports"] = state
    save_chat_settings()


def _extract_reason(text: str | None) -> str:
    parts = (text or "").split(maxsplit=1)
    return (parts[1].strip() if len(parts) > 1 else "")


def _report_message_link(chat_id: int, message_id: int) -> str | None:
    chat_s = str(int(chat_id))
    if not chat_s.startswith("-100"):
        return None
    return f"https://t.me/c/{chat_s[4:]}/{int(message_id)}"


def _can_manage_reports(chat_id: int, user: types.User | None) -> bool:
    if not user:
        return False
    if _is_special_actor(chat_id, user):
        return True
    if is_chat_admin(chat_id, int(user.id)):
        return True
    _, allowed = check_role_permission(chat_id, int(user.id), PERM_VIEW_LISTS)
    return bool(allowed)


def _is_report_mention_trigger(m: types.Message) -> bool:
    if should_ignore_text_triggers(m):
        return False
    text = (m.text or "").strip()
    if not text:
        return False
    first = text.split(maxsplit=1)[0].lower()
    return first in REPORT_MENTION_ALIASES


def _build_admin_mentions(chat_id: int) -> list[str]:
    mentions: list[str] = []
    try:
        admins = bot.get_chat_administrators(chat_id)
    except Exception:
        return mentions
    for adm in admins or []:
        user = getattr(adm, "user", None)
        if not user:
            continue
        if getattr(user, "is_bot", False):
            continue
        display = _html.escape(getattr(user, "full_name", None) or getattr(user, "first_name", None) or "Админ")
        mentions.append(mention_html_by_id(int(user.id), display))
    return mentions


def _report_target_user(m: types.Message) -> types.User | None:
    reply = getattr(m, "reply_to_message", None)
    if not reply:
        return None
    if is_channel_post_message(reply):
        return None
    return getattr(reply, "from_user", None)


def _process_report(m: types.Message, reason_override: Optional[str] = None) -> None:
    add_stat_message(m)
    add_stat_command("report")

    if m.chat.type not in ("group", "supergroup"):
        return
    if not check_group_approval(m):
        return
    if not m.from_user:
        return
    if should_ignore_text_triggers(m):
        return

    state = _get_reports_state(m.chat.id)
    if not bool(state.get("enabled", True)):
        return bot.reply_to(
            m,
            premium_prefix("Репорты в этом чате отключены."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    blocked = {int(x) for x in (state.get("blocked_users") or []) if str(x).lstrip("-").isdigit()}
    if int(m.from_user.id) in blocked:
        return bot.reply_to(
            m,
            premium_prefix("Вам запрещено отправлять репорты в этом чате."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if _is_special_actor(m.chat.id, m.from_user) or is_chat_admin(m.chat.id, int(m.from_user.id)):
        return bot.reply_to(
            m,
            premium_prefix("Администраторы не используют репорт — примените мод-команду напрямую."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    target = _report_target_user(m)
    if not target:
        return bot.reply_to(
            m,
            premium_prefix("Репорт отправляется только ответом на сообщение пользователя."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if int(target.id) == int(m.from_user.id):
        return bot.reply_to(m, premium_prefix("Нельзя отправить репорт на самого себя."), parse_mode="HTML")

    try:
        me = get_bot_me()
        if int(target.id) == int(me.id):
            return bot.reply_to(m, premium_prefix("Нельзя отправить репорт на бота."), parse_mode="HTML")
    except Exception:
        pass

    if is_chat_admin(m.chat.id, int(target.id)) or _is_special_actor(m.chat.id, target):
        return bot.reply_to(
            m,
            premium_prefix("Нельзя отправить репорт на администратора."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    wait_seconds = cooldown_hit("user", int(m.from_user.id), "report", REPORTS_COOLDOWN_SECONDS)
    if wait_seconds > 0:
        return reply_cooldown_message(m, wait_seconds, scope="user", bucket=int(m.from_user.id), action="report")

    admin_mentions = _build_admin_mentions(m.chat.id)
    if not admin_mentions:
        return bot.reply_to(
            m,
            premium_prefix("Не удалось уведомить администраторов."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    reporter_name = _html.escape(m.from_user.full_name or m.from_user.first_name or "Пользователь")
    target_name = _html.escape(target.full_name or target.first_name or "Пользователь")
    reporter_mention = mention_html_by_id(int(m.from_user.id), reporter_name)
    target_mention = mention_html_by_id(int(target.id), target_name)
    reason = (reason_override or _extract_reason(m.text)).strip()
    reason_html = _html.escape(reason) if reason else "не указана"
    msg_link = _report_message_link(m.chat.id, int(m.reply_to_message.message_id))
    link_line = f'\n<b>Сообщение:</b> <a href="{msg_link}">перейти</a>' if msg_link else ""
    mentions_block = " ".join(admin_mentions[:25])
    text = (
        f"🚨 <b>Новый репорт</b>\n\n"
        f"<b>От:</b> {reporter_mention}\n"
        f"<b>На:</b> {target_mention}\n"
        f"<b>Причина:</b> {reason_html}"
        f"{link_line}\n\n"
        f"{mentions_block}"
    )
    bot.send_message(
        m.chat.id,
        text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_to_message_id=getattr(m.reply_to_message, "message_id", None),
    )
    return bot.reply_to(
        m,
        premium_prefix("Репорт отправлен администраторам."),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@bot.message_handler(func=lambda m: m.chat.type in ["group", "supergroup"] and match_command_aliases(m, ["reports", "репорты"]))
def cmd_reports(m: types.Message):
    add_stat_message(m)
    add_stat_command("reports")

    if not is_group_approved(m.chat.id):
        return bot.reply_to(
            m,
            "⏳ Бот находится на модерации. Ожидание подтверждения от разработчика.",
            parse_mode="HTML",
        )
    if not m.from_user:
        return
    if should_ignore_text_triggers(m):
        return
    if not _can_manage_reports(m.chat.id, m.from_user):
        return bot.reply_to(
            m,
            premium_prefix("Команда доступна только администраторам чата."),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    state = _get_reports_state(m.chat.id)
    args = (m.text or "").split()
    action = (args[1].strip().lower() if len(args) > 1 else "")
    blocked = [int(x) for x in (state.get("blocked_users") or []) if str(x).lstrip("-").isdigit()]

    if not action:
        status = "включены" if bool(state.get("enabled", True)) else "выключены"
        return bot.reply_to(
            m,
            premium_prefix(
                "Статус репортов: "
                f"<b>{status}</b>\n\n"
                "Использование:\n"
                "<code>/reports on</code>\n"
                "<code>/reports off</code>\n"
                "<code>/reports block</code> (reply)\n"
                "<code>/reports unblock</code> (reply)\n"
                "<code>/reports showblocklist</code>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if action in {"on", "yes", "true", "1"}:
        state["enabled"] = True
        _set_reports_state(m.chat.id, state)
        return bot.reply_to(m, premium_prefix("Репорты включены."), parse_mode="HTML")

    if action in {"off", "no", "false", "0"}:
        state["enabled"] = False
        _set_reports_state(m.chat.id, state)
        return bot.reply_to(m, premium_prefix("Репорты выключены."), parse_mode="HTML")

    if action in {"showblocklist", "blocklist", "list"}:
        if not blocked:
            return bot.reply_to(m, premium_prefix("Список блокировки репортов пуст."), parse_mode="HTML")
        lines = ["<b>Заблокированы для /report:</b>"]
        for uid in blocked[:100]:
            lines.append(f"• {mention_html_by_id(uid, str(uid))} [<code>{uid}</code>]")
        return bot.reply_to(
            m,
            "\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    if action in {"block", "unblock"}:
        target = _report_target_user(m)
        if not target:
            return bot.reply_to(
                m,
                premium_prefix("Используйте reply на сообщение пользователя."),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        target_id = int(target.id)
        if action == "block":
            if target_id not in blocked:
                blocked.append(target_id)
            state["blocked_users"] = blocked
            _set_reports_state(m.chat.id, state)
            return bot.reply_to(
                m,
                premium_prefix(f"Пользователь [<code>{target_id}</code>] заблокирован для /report."),
                parse_mode="HTML",
            )
        if target_id in blocked:
            blocked.remove(target_id)
        state["blocked_users"] = blocked
        _set_reports_state(m.chat.id, state)
        return bot.reply_to(
            m,
            premium_prefix(f"Пользователь [<code>{target_id}</code>] разблокирован для /report."),
            parse_mode="HTML",
        )

    return bot.reply_to(
        m,
        premium_prefix("Неизвестный аргумент. Используйте <code>/reports</code> для справки."),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@bot.message_handler(func=lambda m: m.chat.type in ["group", "supergroup"] and match_command_aliases(m, ["report", "репорт"]))
def cmd_report(m: types.Message):
    _process_report(m)


@bot.message_handler(func=lambda m: m.chat.type in ["group", "supergroup"] and bool(m.text) and _is_report_mention_trigger(m))
def cmd_report_via_admin_mention(m: types.Message):
    text = (m.text or "").strip()
    parts = text.split(maxsplit=1)
    reason = parts[1].strip() if len(parts) > 1 else ""
    _process_report(m, reason_override=reason)
