"""
main.py — Точка входа бота.

Порядок импорта КРИТИЧЕСКИ ВАЖЕН для pyTelegramBotAPI:
первый зарегистрированный обработчик имеет приоритет.

  config        — только константы, нет обработчиков
  persistence   — слой данных, нет обработчиков
  helpers       — инфраструктура + ранние обработчики:
                    dbg_users, rs_* callbacks, verify-команды,
                    on_new_members (ContinueHandling — должен быть ПЕРВЫМ из new_chat_members)
  cmd_basic     — /start, pm, /ping, /log, /broadcast, профили, награды,
                    теги, должности, /closechat, /openchat
  moderation    — /мут, /бан, /кик, /варн, punish_un, modlist, /adminstats
  pin           — /pin, /spin, /npin, /unpin, pin-callbacks
  settings_ui   — /settings, welcome/farewell/rules/cleanup,
                    on_welcome_new_members (new_chat_members — ПОСЛЕ helpers.on_new_members),
                    left_chat_member, rules trigger, cleanup handlers
  clones        — управление клонами (только основной бот, IS_CLONE=False);
                    ДОЛЖЕН быть до handlers, чтобы /clone_* команды регистрировались
                    раньше all_other (иначе all_other перехватывает их первым)
  guest_bots    — управление отдельными guest-ботами (только основной бот, IS_CLONE=False)
  handlers      — group stats UI, approve/deny_group, my_chat_member,
                    главный callback_handler, all_other (ДОЛЖЕН БЫТЬ ПОСЛЕДНИМ)

Режим запуска: long polling (infinity_polling).

Клоны (IS_CLONE=1):
  Клон — отдельный процесс/контейнер с тем же кодом.
  На старте клон проверяет свой статус в реестре. Если статус «disabled» — завершается.
  Фоновый тред клона проверяет статус каждые 30 с и останавливает бота при отключении.
"""
from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

import config       # noqa: F401
import persistence  # noqa: F401 — при импорте подключается кеш get_chat(user_id)
import helpers      # noqa: F401
import cmd_basic    # noqa: F401
import moderation   # noqa: F401
import pin          # noqa: F401
import settings_ui  # noqa: F401
import antispam     # noqa: F401
import banned_words  # noqa: F401

from config import bot, IS_CLONE, get_bot_me

if not IS_CLONE:
    import clones   # noqa: F401  — команды /clones, /clone_register и т.д.
    import guest_bots  # noqa: F401

import handlers     # noqa: F401  — all_other ДОЛЖЕН БЫТЬ ПОСЛЕДНИМ


def _start_clone_disable_watcher(my_bot_id: int) -> None:
    """Фоновый тред клона: каждые 10 с проверяет статус в реестре.
    Если статус «disabled» — останавливает бота.
    """
    import threading
    import time
    from config import CLONES_FILE
    from persistence import load_json_file

    def _watch() -> None:
        while True:
            time.sleep(10)
            try:
                fresh = load_json_file(CLONES_FILE, {"clones": []})
                for entry in (fresh.get("clones") or []):
                    try:
                        eid = int(entry.get("bot_id", 0))
                    except (TypeError, ValueError):
                        continue
                    if eid == my_bot_id and entry.get("status") == "disabled":
                        logger.info("[CLONE] Статус «disabled» — останавливаю бота.")
                        bot.stop_polling()
                        return
            except Exception as e:
                logger.warning("[CLONE WATCHER] Ошибка проверки статуса: %s", e)

    threading.Thread(target=_watch, daemon=True, name="clone-disable-watch").start()


if __name__ == "__main__":
    mode_label = "клон" if IS_CLONE else "основной"
    logger.info("Запуск бота (%s), режим=polling…", mode_label)

    if IS_CLONE:
        from config import CLONES_FILE
        from persistence import load_json_file

        # Получаем ID этого бота
        try:
            _me = get_bot_me()
            _my_bot_id = _me.id
        except Exception as _e:
            logger.error("[CLONE] Не удалось получить get_bot_me(): %s", _e)
            sys.exit(1)

        # Проверяем статус при старте
        _clones_data = load_json_file(CLONES_FILE, {"clones": []})
        for _entry in (_clones_data.get("clones") or []):
            try:
                _eid = int(_entry.get("bot_id", 0))
            except (TypeError, ValueError):
                continue
            if _eid == _my_bot_id and _entry.get("status") == "disabled":
                logger.info("[CLONE] Клон %s отключён в реестре. Завершаю.", _me.username)
                sys.exit(0)

        # Запускаем фоновый тред мониторинга
        _start_clone_disable_watcher(_my_bot_id)

    else:
        # Основной бот: запускаем все зарегистрированные клоны.
        clones.autostart_clones()
        guest_bots.autostart_guest_bots()

    logger.info("[Polling] Запуск long polling…")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
