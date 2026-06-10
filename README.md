# MAX ↔ Telegram bridge

Мост между мессенджером MAX (oneme/web.max.ru) и Telegram. Читает чаты MAX
через отреверсенный WebSocket-протокол (на базе [vkmax](https://github.com/nsdkinx/vkmax))
и зеркалит их в Telegram-группу с топиками. Без браузера/Playwright.

Полный план и разведка протокола: Obsidian `self-hosting/max-telegram-bridge`.

## Статус

| Этап | Статус |
|---|---|
| 1. Логин + web-отпечаток | код готов, логин делается на сервере |
| 2. MAX → Telegram | TODO |
| 3. Telegram → MAX | TODO |
| 4. Forum topics маппинг | TODO |

## Архитектура

```
vkmax (WS oneme.ru) ──► WebMaxClient ──► bridge ──► aiogram ──► Telegram (topics)
        ▲                                                            │
        └────────────── send_message ◄──── bridge ◄──── ответ в топик ┘
```

## Web-отпечаток (анти-бан)

`WebMaxClient` (max_client.py) мимикрирует под `web.max.ru`:
- `config.WEB_FINGERPRINT` — снят с живого клиента (deviceType WEB, WEBPUSH,
  appVersion 26.6.6, macOS Chrome/146, timezone Europe/Moscow)
- свой **постоянный** `device_id` (не копия браузерной сессии)
- **ACK** (`cmd:1`) на server-push opcode 128 — чего нет в стоковом vkmax
- keepalive opcode 1 каждые 30с (из vkmax)

## Файлы

- `config.py` — отпечаток, токены, настройки (через env / .env)
- `max_client.py` — `WebMaxClient` (патч ClientHello + ACK + persist сессии)
- `login.py` — разовый логин по SMS, сохраняет `session/session.json`
- `probe.py` — подключиться по сохранённой сессии и логировать входящие пакеты
- `main.py` — оркестратор: MAX-клиент + Telegram-бот в одном asyncio loop (мост)
- `Dockerfile`, `docker-compose.yml` — один контейнер на оба компонента

## Запуск через Docker (на сервере РФ)

Один контейнер держит и MAX-клиент, и Telegram-бота (один процесс, asyncio).
Контейнер с `TZ=Europe/Moscow` (консистентно с web-отпечатком).

```bash
cp .env.example .env          # вписать MAX_PHONE, TELEGRAM_BOT_TOKEN, TELEGRAM_GROUP_ID

# 1) РАЗОВЫЙ интерактивный логин по SMS (создаёт session/session.json в volume):
docker compose run --rm -it bridge python login.py

# (опционально) проверить приём входящих:
docker compose run --rm bridge python probe.py

# 2) Запустить мост:
docker compose up -d --build
docker compose logs -f
```

Логин вынесен в отдельную команду, т.к. требует ручного ввода SMS-кода.
Дальше сессия живёт в volume `./session` → основной контейнер релогинится
по токену (`login_by_token`) без SMS.

### Без Docker (локальная отладка)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python login.py && python main.py
```

`session/session.json` (device_id + auth_token) и `data/` в git не коммитятся.

## Заметки

- Логиниться **с РФ-сервера** (deviceId/token привяжутся к РФ-IP).
- Тестовый аккаунт, не основной номер.
- vkmax — неофициальный реверс, протокол может меняться.
