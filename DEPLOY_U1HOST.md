# Быстрый деплой Telegram-bot2 на vm.u1host.com

Если длинные инструкции неудобны, используйте быстрый вариант ниже.

## Вариант «почти в один клик»

Сейчас рабочая точка входа бота: `main.py`. Systemd-сервис в репозитории уже настроен на него.

### 1) Подключитесь к серверу
```bash
ssh root@vm.u1host.com
```

### 2) Запустите автоскрипт установки
```bash
git clone https://github.com/vuducngo290-code/Telegram-bot2.git /tmp/telegram-bot2
bash /tmp/telegram-bot2/deploy/install_vm_u1host.sh https://github.com/vuducngo290-code/Telegram-bot2.git
```

Скрипт сам:
- установит Python и зависимости,
- создаст пользователя `bot`,
- развернет проект в `/opt/telegram-bot2`,
- настроит автозапуск (`systemd`).

### 3) Один раз заполните `.env`
```bash
nano /opt/telegram-bot2/.env
```

Нужно заполнить:
- `BOT_TOKEN`
- `API_ID`
- `API_HASH`
- `OWNER_USERNAME`

Обычно можно оставить как есть:
- `DATA_DIR=/opt/telegram-bot2/data`
- `TG_SESSION_NAME=/opt/telegram-bot2/data/user_session`

После сохранения запустите:
```bash
systemctl restart telegram-bot2
systemctl status telegram-bot2 --no-pager
```

Логи:
```bash
journalctl -u telegram-bot2 -f
```

## Обновление бота (в будущем)
```bash
su - bot -c "cd /opt/telegram-bot2 && git pull && . .venv/bin/activate && pip install -r requirements.txt"
systemctl restart telegram-bot2
```

## Важно
- Токены храните только в `.env`.
- Если токен раньше был в коде, перевыпустите `BOT_TOKEN` через BotFather.
