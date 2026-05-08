# Telegram-bot2

Многофункциональный бот-менеджер для Telegram-групп с поддержкой модерации,
статистики, ролей, настроек приветствий, клонов и многого другого.

---

## Быстрый старт

### Переменные окружения

Скопируйте `.env.example` → `.env` и заполните:

```dotenv
# Обязательные
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
API_ID=12345
API_HASH=abcdef1234567890abcdef1234567890

# Данные владельца
OWNER_USERNAME=your_telegram_username   # без @

# Директория данных (JSON/SQLite)
DATA_DIR=/data

# ── Режим запуска ──────────────────────────────────────────────────────────
# webhook  — рекомендуется для production (по умолчанию)
# polling  — long polling, удобен для локальной отладки без ngrok
MODE=webhook

# ── Webhook-настройки (нужны только при MODE=webhook) ─────────────────────
# Полный публичный HTTPS URL вашего сервера (без пути)
WEBHOOK_URL=https://your-domain.com
# Путь эндпоинта (обычно не нужно менять)
WEBHOOK_PATH=/telegram/webhook
# Секрет для заголовка X-Telegram-Bot-Api-Secret-Token (рекомендуется задать)
WEBHOOK_SECRET_TOKEN=your_random_secret_here
# Порт HTTP-сервера
PORT=8000
```

---

## Запуск в режиме webhook

### Локально (с ngrok)

1. [Установите ngrok](https://ngrok.com/download) и авторизуйтесь.

2. Пробросьте туннель на порт бота:
   ```bash
   ngrok http 8000
   ```
   ngrok выдаст URL вида `https://xxxx.ngrok-free.app`.

3. Задайте переменные и запустите бота:
   ```bash
   export BOT_TOKEN=...
   export API_ID=...
   export API_HASH=...
   export MODE=webhook
   export WEBHOOK_URL=https://xxxx.ngrok-free.app   # URL из ngrok
   export WEBHOOK_SECRET_TOKEN=mysecret
   export PORT=8000
   python main.py
   ```

   Или через `.env` + `python-dotenv`:
   ```bash
   cp .env.example .env  # заполните .env
   python -m dotenv run python main.py
   ```

   Либо используйте [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/):
   ```bash
   cloudflared tunnel --url http://localhost:8000
   ```

### Production (VPS + Nginx + systemd)

1. **Nginx** — проксируйте HTTPS → HTTP на порт бота:
   ```nginx
   server {
       listen 443 ssl;
       server_name your-domain.com;
       # ... ssl_certificate ...

       location /telegram/webhook {
           proxy_pass http://127.0.0.1:8000;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
       }
   }
   ```

2. **systemd** — `/etc/systemd/system/telegram-bot2.service`:
   ```ini
   [Unit]
   Description=Telegram Bot2
   After=network.target

   [Service]
   User=bot
   WorkingDirectory=/opt/telegram-bot2
   EnvironmentFile=/opt/telegram-bot2/.env
   ExecStart=/opt/telegram-bot2/.venv/bin/python main.py
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```

   ```bash
   systemctl daemon-reload
   systemctl enable --now telegram-bot2
   journalctl -u telegram-bot2 -f
   ```

3. Задайте все переменные в `/opt/telegram-bot2/.env` (см. раздел выше).

### Render / Fly.io

Задайте переменные окружения в панели сервиса.  
`WEBHOOK_URL` = публичный URL вашего деплоя (например, `https://my-bot.onrender.com`).  
`PORT` = значение из настроек платформы (обычно `10000` для Render, `8080` для Fly).

---

## Запуск в режиме polling (fallback)

Удобен для локальной отладки без публичного URL:

```bash
MODE=polling BOT_TOKEN=... API_ID=... API_HASH=... python main.py
```

В режиме polling переменные `WEBHOOK_URL`, `WEBHOOK_PATH`, `WEBHOOK_SECRET_TOKEN`, `PORT`
**не нужны** и игнорируются.

---

## Установка зависимостей

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Smoke-тест webhook

Проверяет, что эндпоинт корректно отвечает на запросы:

```bash
python tests/smoke_webhook.py
```

---

## Guest-боты: тест-команды и формат вызова

Для владельца бота в ЛС доступно меню `/guestbots` → `Команды`.

- Кнопка `Тест-команды` автоматически создаёт:
  - `test` → проверка guest-ответа
  - `ping` → быстрый ответ `pong`
  - `guest_help` → краткая подсказка
- Кнопка `Инструкция` отправляет формат вызова.

Поддерживаемые варианты guest-вызова:

- `@username_бота test`
- `/test@username_бота`
- `test`

---

## Деплой на vm.u1host.com

См. [DEPLOY_U1HOST.md](DEPLOY_U1HOST.md).

---

## Поиск и выгрузка музыки

- Команда: `/music название` или `/музыка название`
- Бот выводит результаты поиска кнопками (YouTube + SoundCloud).
- Нажатие на кнопку запускает выгрузку выбранного трека в формате MP3.
- Для функции требуется установленный `ffmpeg` в окружении запуска.
- В группе функцию можно отключить командой `/musicoff` (только владелец группы, владелец бота, dev или администратор с правом «Управление настройками группы»).

---

## Выгрузка TikTok по ссылке

- Пользователь отправляет одну ссылку TikTok в чат.
- Бот отвечает кнопками: `Видео`, `Звук`, `Отмена`.
- Кнопки `Видео` и `Звук` может нажимать только автор ссылки.
- Для TikTok-постов с фото бот отправляет фото: одно фото одиночным сообщением, несколько — альбомами.
- Если фото в посте больше 10, бот делит отправку на несколько альбомов (лимит Telegram: до 10 медиа в альбоме).
- Результат отправляется ответом на сообщение со ссылкой; если исходное сообщение уже удалено — отправляется просто в чат.
- Для выгрузки звука нужен установленный `ffmpeg`.
- В группе функцию можно отключить командой `/tiktokoff` (только владелец группы, владелец бота, dev или администратор с правом «Управление настройками группы»).

---

## Важно

- Токены и секреты храните только в `.env`. Не коммитьте `.env` в репозиторий.
- Если токен ранее был в коде — перевыпустите `BOT_TOKEN` через BotFather.
- `WEBHOOK_SECRET_TOKEN` защищает эндпоинт от посторонних запросов; в production
  его **рекомендуется** задавать.
