# MAX ↔ Telegram Bridge

Мост между мессенджером **MAX** (oneme / web.max.ru) и **Telegram**. Зеркалит все
твои чаты MAX в Telegram-группу с топиками и позволяет полноценно общаться из
Telegram. Работает напрямую через отреверсенный WebSocket-протокол MAX (на базе
[vkmax](https://github.com/nsdkinx/vkmax)) — **без браузера/Playwright**, один
Docker-контейнер (MAX-клиент + Telegram-бот в одном процессе).

---

## Возможности

**Двусторонняя переписка**
- Каждый чат MAX = отдельный **топик** в Telegram-супергруппе
- Имена топиков: имя собеседника (для диалогов) / название группы
- История последних сообщений при создании топика
- Имя отправителя показывается только в группах (в личке — без него)

**Медиа**

| Тип | MAX → Telegram | Telegram → MAX |
|---|---|---|
| Текст | ✅ | ✅ |
| Фото | ✅ | ✅ |
| Видео | ✅ | ✅ (MAX транскодирует ~1-2 мин) |
| Файл/документ | ✅ | ✅ |
| Голосовое | ✅ | ⛔ web-протокол MAX не умеет отправлять |
| Кружок (видеосообщение) | ✅ | 🟡 уходит обычным видео |
| Cached видео (okcdn) | 🟡 превью + пометка | — |

**Интерактив**
- **Прочтение** — что доставлено в Telegram, помечается прочитанным в MAX (бейдж не растёт)
- **Реакции** — поставил/снял реакцию в Telegram → то же в MAX
- **Редактирование** — правишь сообщение в Telegram → правится в MAX

**Надёжность**
- Авто-reconnect с backoff при разрыве WS
- Healthcheck, graceful shutdown, non-root контейнер
- Дедупликация сообщений, persistent-сессия

---

## Архитектура

```
┌──────────────── Docker контейнер (один процесс, asyncio) ────────────────┐
│                                                                          │
│   WebMaxClient ──WS──► wss://ws-api.oneme.ru/websocket  (web-отпечаток)   │
│        │  ▲                                                               │
│        ▼  │  (прямой вызов в памяти)                                      │
│   aiogram Bot ──polling──► Telegram (forum topics)                       │
│                                                                          │
│   SQLite (data/bridge.db): topics, names, seen, msg_map                   │
│   session/session.json: device_id + auth_token                           │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## ⚠️ Важные ограничения (прочитать до установки)

1. **Сервер должен быть НЕ в РФ.** Telegram заблокирован в России — с РФ-сервера
   `api.telegram.org` недоступен (таймаут). MAX (`oneme.ru`) при этом работает
   откуда угодно. Поэтому хост — например, Европа.
2. **Сессия MAX берётся из браузера.** MAX требует капчу при SMS-входе, а её
   автоматизировать нельзя. Поэтому ты логинишься в `web.max.ru` сам (проходишь
   капчу как человек), а мост переиспользует выданный токен.
3. **Токен может протухнуть** → разово повторить получение сессии (см. ниже).
4. **Тестовый/отдельный аккаунт** — рекомендуется не использовать основной номер.

---

## Установка

### Шаг 1. Сервер

- Linux (Ubuntu 24.04+), **вне РФ**
- Docker + Docker Compose v2
- Проверь доступность обоих сервисов:
  ```bash
  curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org   # ждём 302
  curl -s -o /dev/null -w "%{http_code}\n" https://ws-api.oneme.ru     # ждём 200
  ```

### Шаг 2. Получение сессии MAX (через браузер)

1. Открой `https://web.max.ru` в браузере, войди своим аккаунтом MAX
   (пройди капчу + SMS как обычно), дождись списка чатов.
2. Открой **DevTools** (`⌥⌘I` / F12) → вкладка **Console**.
   (Браузер может попросить напечатать `allow pasting` — сделай это.)
3. Выполни сниппет — он соберёт `session.json` в буфер обмена:
   ```js
   copy(JSON.stringify({
     device_id: localStorage["__oneme_device_id"],
     auth_token: JSON.parse(localStorage["__oneme_auth"]).token,
     phone: "+7XXXXXXXXXX"
   }, null, 2));
   console.log("session.json скопирован в буфер");
   ```
4. Сохрани буфер в файл на сервере (минуя локальный файл и логи):
   ```bash
   pbpaste | ssh myserver 'cat > ~/max-tg-bridge/session/session.json'   # macOS
   # или вставь руками в session/session.json
   ```

> `device_id` и `auth_token` — пара, переносятся вместе. Один и тот же `device_id`
> используется всегда. Параллельно держать вкладку web.max.ru открытой можно
> (MAX допускает несколько соединений с одним device_id).

### Шаг 3. Telegram-бот и группа

1. **Бот**: у [@BotFather](https://t.me/BotFather) → `/newbot` → получи **токен**.
2. **Группа**: создай группу, добавь бота. Настройки → **Edit** → включи **Topics/Темы**
   (группа станет супергруппой-форумом).
3. **Права бота**: Администраторы → бот → включи **«Управление темами» (Manage Topics)**.
4. **Узнай chat_id группы**: напиши что-нибудь в группу, затем:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   ```
   Найди `chat.id` (вид `-100XXXXXXXXXX`). Либо, если знаешь короткий id,
   проверь `-100` + короткий через `getChat?chat_id=-100...`.

### Шаг 4. Конфигурация

```bash
git clone <repo> ~/max-tg-bridge && cd ~/max-tg-bridge
cp .env.example .env
```

Заполни `.env`:
```dotenv
TZ=Europe/Moscow                  # таймзона: и контейнер, и web-отпечаток MAX
MAX_PHONE=+7XXXXXXXXXX            # номер аккаунта MAX (опционально)
TELEGRAM_BOT_TOKEN=123456:AA...   # от @BotFather
TELEGRAM_GROUP_ID=-100XXXXXXXXXX  # id супергруппы
HUMAN_DELAY_MIN=1.0               # антибан: задержка отправки в MAX, сек
HUMAN_DELAY_MAX=5.0
HISTORY_DEPTH=10                  # сколько последних сообщений лить в новый топик
TOPIC_THROTTLE=2.0                # пауза между созданием топиков, сек
IGNORED_CHAT_IDS=0                # служебные чаты (через запятую)
SESSION_HOST_DIR=./session        # пути volume на хосте (для переносимости)
DATA_HOST_DIR=./data
```

Положи `session/session.json` (из Шага 2) в `./session/`.

### Шаг 5. Запуск

```bash
mkdir -p session data
chown -R 1000:1000 session data    # контейнер работает под non-root uid 1000
docker compose up -d --build
docker compose logs -f             # следи за стартом: login, init_topics
```

При первом старте мост создаст топики на каждый чат и зальёт историю — это
может занять время (Telegram лимитит создание топиков). Дальше всё в реальном
времени.

Проверка статуса:
```bash
docker ps                          # должен быть healthy
docker compose logs --tail 20
```

---

## Обновление сессии (токен протух)

Если мост перестал логиниться в MAX (`login_by_token` ошибка) — токен истёк.
Повтори **Шаг 2** (получи свежий `session.json` из браузера), положи на сервер,
перезапусти:
```bash
docker compose restart
```

---

## Перенос на другой сервер

Образ stateless — вся специфика в `.env` и `session/`:
```bash
# на новом сервере (тоже вне РФ):
git clone <repo> ~/max-tg-bridge && cd ~/max-tg-bridge
# скопировать .env и session/session.json со старого
scp old:~/max-tg-bridge/.env .
scp old:~/max-tg-bridge/session/session.json session/
mkdir -p data && chown -R 1000:1000 session data
docker compose up -d --build
```
> На старом сервере останови (`docker compose down`), чтобы не было двух
> зеркал в одну группу (дубли).

---

## Структура проекта

```
config.py        конфиг: web-отпечаток + настройки (env-driven)
max_client.py    WebMaxClient: web ClientHello, ACK, reconnect,
                 download/upload медиа, read/reaction/edit (opcodes)
storage.py       SQLite: topics, names, seen (дедуп), msg_map
bridge.py        вся логика моста (MAX↔TG, топики, медиа, интерактив)
main.py          точка входа
login.py         (опц.) логин по SMS — НЕ работает из-за капчи, см. Шаг 2
probe.py         диагностика: слушать входящие пакеты
dump_chats.py    диагностика: схема ChatSync
healthcheck.py   HEALTHCHECK по heartbeat-файлу
Dockerfile, docker-compose.yml
```

`session/` и `data/` и `.env` — в `.gitignore` (секреты/состояние не коммитятся).

---

## Деплой-цикл при разработке

```bash
# правка локально → синк → пересборка
rsync -az --exclude '.git' --exclude 'session' --exclude 'data' --exclude '.env' \
  ./ myserver:~/max-tg-bridge/
ssh myserver 'cd ~/max-tg-bridge && chown -R 1000:1000 session data \
  && docker compose down && docker compose build && docker compose up -d'
```

Отладочные env: `VKMAX_LOG=INFO` (логи vkmax), `DEBUG_PACKETS=1` (сырые пакеты).

---

## Траблшутинг

| Симптом | Причина / решение |
|---|---|
| `api.telegram.org` timeout | Сервер в РФ — Telegram заблокирован. Нужен хост вне РФ |
| `captcha.validation-failed` при login.py | MAX требует капчу — используй сессию из браузера (Шаг 2) |
| `not enough rights to create a topic` | Боту не выдано право «Управление темами» |
| `login_by_token` ошибка | Токен протух — обнови сессию |
| Видео идёт 2-3 мин | MAX транскодирует на своей стороне — норма |
| Голосовое из TG не отправляется | web-протокол MAX не поддерживает отправку голосовых |
| Permission denied на session/data | `chown -R 1000:1000 session data` (non-root контейнер) |

---

## Заметки

- vkmax — неофициальный реверс, протокол MAX может меняться (vkmax запинен на commit).
- Неофициальный клиент может нарушать ToS MAX — используй на свой риск,
  желательно на отдельном аккаунте.
- Анти-бан: человеческие задержки отправки, web-отпечаток как у `web.max.ru`,
  ACK на server-push, стабильный reconnect.
