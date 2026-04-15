"""
webhook.py — HTTP-сервер для приёма апдейтов Telegram через webhook.

Используется вместо long polling при MODE=webhook (значение по умолчанию).

Эндпоинт:
  POST <WEBHOOK_PATH>  (по умолчанию /telegram/webhook)

Безопасность:
  Заголовок X-Telegram-Bot-Api-Secret-Token проверяется, если задан
  WEBHOOK_SECRET_TOKEN.  При несовпадении сервер отвечает 403.

Жизненный цикл:
  setup_webhook()        — регистрирует webhook в Telegram API при старте.
  delete_webhook()       — снимает webhook (опционально при остановке).
  run_webhook_server()   — запускает Flask (блокирующий вызов).
  request_shutdown()     — устанавливает флаг остановки; вызывается из
                           фонового треда (например, clone-disable watcher).
"""
from __future__ import annotations

import logging
import os
import threading

import telebot
from flask import Flask, Response, request

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Создаём Flask-приложение один раз на уровне модуля.
# ---------------------------------------------------------------------------
_flask_app = Flask(__name__)

# Событие-флаг для graceful shutdown сервера из фонового треда.
_shutdown_event = threading.Event()


def request_shutdown() -> None:
    """Попросить HTTP-сервер завершить работу (thread-safe)."""
    logger.info("[Webhook] Запрошена остановка сервера.")
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@_flask_app.post(config.WEBHOOK_PATH)  # type: ignore[misc]
def telegram_webhook() -> Response:
    # Проверяем секретный токен (если задан).
    if config.WEBHOOK_SECRET_TOKEN:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != config.WEBHOOK_SECRET_TOKEN:
            logger.warning(
                "[Webhook] Неверный секретный токен от %s", request.remote_addr
            )
            return Response("Forbidden", status=403)

    raw = request.get_data(as_text=True)
    if not raw:
        return Response("OK", status=200)

    try:
        update = telebot.types.Update.de_json(raw)
        update_id = getattr(update, "update_id", "?")
        update_type = _detect_update_type(update)
        logger.info("[Webhook] Update id=%s type=%s", update_id, update_type)
        config.bot.process_new_updates([update])
    except Exception:
        # Никогда не возвращаем 5xx — Telegram будет повторять запрос.
        logger.exception(
            "[Webhook] Ошибка при обработке update (raw[:200]=%s)", raw[:200]
        )

    return Response("OK", status=200)


def _detect_update_type(update: telebot.types.Update) -> str:
    """Возвращает строковый тип апдейта для логирования."""
    for field in (
        "message", "edited_message", "channel_post", "edited_channel_post",
        "inline_query", "chosen_inline_result", "callback_query",
        "shipping_query", "pre_checkout_query", "poll", "poll_answer",
        "my_chat_member", "chat_member", "chat_join_request",
    ):
        if getattr(update, field, None) is not None:
            return field
    return "unknown"


# ---------------------------------------------------------------------------
# Webhook registration / deregistration
# ---------------------------------------------------------------------------

def setup_webhook() -> None:
    """Зарегистрировать webhook в Telegram API.

    Вызывать перед запуском HTTP-сервера.
    Если WEBHOOK_URL не задан — выводит предупреждение и не вызывает API.
    """
    if not config.WEBHOOK_URL:
        logger.warning(
            "[Webhook] WEBHOOK_URL не задан — webhook не зарегистрирован. "
            "Задайте переменную окружения WEBHOOK_URL и перезапустите."
        )
        return

    full_url = config.WEBHOOK_URL.rstrip("/") + config.WEBHOOK_PATH
    kwargs: dict = {
        "url": full_url,
        "allowed_updates": [
            "message", "edited_message", "channel_post", "edited_channel_post",
            "callback_query", "inline_query", "chosen_inline_result",
            "shipping_query", "pre_checkout_query", "poll", "poll_answer",
            "my_chat_member", "chat_member", "chat_join_request",
        ],
        "drop_pending_updates": False,
    }
    if config.WEBHOOK_SECRET_TOKEN:
        kwargs["secret_token"] = config.WEBHOOK_SECRET_TOKEN

    try:
        config.bot.set_webhook(**kwargs)
        logger.info("[Webhook] Webhook зарегистрирован: %s", full_url)
    except Exception:
        logger.exception("[Webhook] Не удалось зарегистрировать webhook")
        raise


def delete_webhook() -> None:
    """Снять webhook из Telegram API (опционально при остановке)."""
    try:
        config.bot.delete_webhook()
        logger.info("[Webhook] Webhook снят.")
    except Exception:
        logger.exception("[Webhook] Ошибка при снятии webhook")


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_webhook_server() -> None:
    """Запустить Flask-сервер (блокирующий вызов).

    Проверяет флаг _shutdown_event каждые 0.5 с; как только флаг выставлен —
    выходит (используется clone-disable watcher).
    Werkzeug запускается в daemon-треде, чтобы основной поток мог следить
    за флагом остановки.
    """
    logger.info(
        "[Webhook] Запуск HTTP-сервера на %s:%s (путь %s)",
        config.HOST, config.PORT, config.WEBHOOK_PATH,
    )

    server_thread = threading.Thread(
        target=_flask_app.run,
        kwargs={
            "host": config.HOST,
            "port": config.PORT,
            "debug": False,
            "use_reloader": False,
            "threaded": True,
        },
        daemon=True,
        name="webhook-flask",
    )
    server_thread.start()

    # Ждём либо сигнала остановки, либо завершения потока сервера.
    while not _shutdown_event.is_set():
        server_thread.join(timeout=0.5)
        if not server_thread.is_alive():
            break

    logger.info("[Webhook] HTTP-сервер остановлен.")
