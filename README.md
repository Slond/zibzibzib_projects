# zibzibzib Projects

FastAPI с разными проектами

## Модули

- **Dashboard** (`/`) — главная страница с плитками сервисов, админка
- **Finance** (`/finance`) — учет личных финансов с webhook для iOS Shortcuts
- **VPN** (`/vpn`) — администраторская панель AmneziaWG 3.1: выдача ключей и трафик
- **Weather** (`/weather`) — мониторинг датчиков Yandex Smart Home
- **Активность** (`/activity`) — личный дневник: заголовок окна с Mac/Windows и геолокация с iPhone. Данные изолированы по пользователю

## Быстрый старт

```bash
docker network create web_net

cp .env.example .env

docker compose up -d --build
```

Приложение будет доступно на http://localhost:8199

## Миграции базы данных

Миграции Alembic применяются автоматически при старте. Поддерживаются пустые и существующие базы без версии. Перед обновлением сохраните резервную копию SQLite. Для ручного запуска:

```bash
# Применить все миграции
alembic upgrade head

# Создать новую миграцию (автогенерация)
alembic revision --autogenerate -m "описание"

# Откат: восстановить резервную копию базы и предыдущую версию приложения

# Посмотреть текущую версию
alembic current
```

## Структура

```
zibzibzib_projects/
├── app/
│   ├── main.py           # FastAPI app, shared auth routes
│   ├── config.py         # Configuration
│   ├── database.py       # All SQLAlchemy models
│   ├── auth.py           # Unified authentication
│   ├── routers/
│   │   ├── dashboard.py  # Dashboard & admin
│   │   ├── finance.py    # Finance tracker
│   │   ├── weather.py    # Weather monitoring
│   │   └── activity.py   # Personal activity diary
│   ├── services/
│   │   ├── yandex_client.py
│   │   └── scheduler.py
│   └── templates/
│       ├── base.html
│       ├── login.html
│       ├── change_password.html
│       ├── dashboard/
│       ├── finance/
│       └── weather/
├── data/                 # SQLite database
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## База данных

Единая SQLite база с таблицами:

**Общие:**
- `users` — пользователи системы
- `services` — зарегистрированные сервисы (модули)
- `user_service_access` — матрица доступа пользователей к сервисам

**Финансы:**
- `finance_accounts` — аккаунты для финансов (семьи)
- `finance_events` — события (ежедневные траты, поездки и т.д.)
- `finance_event_members` — участники событий
- `transactions` — финансовые транзакции (с мультивалютой)
- `finance_categories` — категории расходов
- `category_tags` — теги для группировки категорий
- `category_budgets` — месячные бюджеты по категориям
- `recurring_transactions` — шаблоны повторяющихся транзакций
- `exchange_rates` — кэш курсов валют

**Погода/IoT:**
- `devices` — IoT устройства
- `measurements` — показания датчиков
- `weather_display_settings` — настройки отображения

## Администрирование

1. Войти под admin аккаунтом
2. Перейти в Админку (ссылка в header)
3. Вкладки:
   - **Пользователи** — создание, сброс пароля, удаление
   - **Аккаунты** — finance аккаунты (семьи)
   - **Сервисы** — модули системы
   - **Матрица доступа** — кому какой доступ

## iOS Shortcut для Finance

[Example](https://www.icloud.com/shortcuts/773c5b9e124b4221a1758515440ba7ee)

### Получение webhook токена

1. В приложении: Finance → События → выбрать событие → Настройки
2. Скопировать Webhook URL

### API Endpoints

```
POST /finance/api/webhook/{token}
  Body: {"amount": -1500, "category": "Продукты", "description": "...", "currency": "KZT"}
  Response: {"status": "ok", "transaction_id": 123, ...}

GET /finance/api/webhook/{token}/categories
  Response: {"categories": [{"name": "Продукты", "icon": "🛒"}, ...]}
```

### Создание Shortcut (упрощенный вариант)

1. **Запросить ввод** (Число) → "Сумма расхода" → сохранить в `сумма`
2. **Список** → ваши категории → **Выбрать из списка** → сохранить в `категория`
3. **Запросить ввод** (Текст) → "Описание" → сохранить в `описание`
4. **Получить содержимое URL**:
   - URL: `https://ваш-домен/finance/api/webhook/ВАШ_ТОКЕН`
   - Метод: POST
   - Тело (JSON): `{"amount": -(сумма), "category": "(категория)", "description": "(описание)"}`
5. **Показать уведомление** → "Расход добавлен"

### С динамическими категориями

Добавить в начало:
1. **Получить содержимое URL** → GET .../categories
2. **Получить значение словаря** → ключ `categories`
3. **Выбрать из списка**
4. **Получить значение словаря** → ключ `name`

## Nginx (пример)

```nginx
server {
    listen 443 ssl;
    server_name <Site>;

    location / {
        proxy_pass http://localhost:8199;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## Переменные окружения

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| SECRET_KEY | Секретный ключ для сессий | change-me-in-production |
| ADMIN_EMAIL | Email администратора | admin@example.com |
| ADMIN_PASSWORD | Начальный пароль админа | admin123 |
| YANDEX_TOKEN | OAuth токен Yandex Smart Home | - |
| POLL_INTERVAL_SECONDS | Интервал опроса датчиков | 60 |
| ACTIVITY_DIR | Каталог данных активности | ./data/activity |

## Активность

Вкладка видна администратору сразу. Остальным её выдают в матрице доступа. Места, устройства и события одного пользователя не читаются другим, в том числе администратором.

Компьютер: на карточке устройства есть команда. `curl` с токеном этого устройства скачивает `client.py` с `GET /activity/api/client/{token}` в папку клиента и ставит запуск при входе в систему. Клиент отправляет приложение и заголовок окна. У браузера это открытая вкладка. Если оба компьютера активны одновременно, этот отрезок дня помечается «рядом». Телефон: Shortcut на `POST /activity/api/location/{token}` только с координатами. iOS не отдаёт имя открытого приложения.

## Курсы и PWA

Ключ Open Exchange Rates задаётся через `OPENEXCHANGERATES_APP_ID` в `.env`.
Курс берётся из API или сохранённой истории. Если подтверждённого курса нет,
операция возвращает ошибку, а существующие операции без конвертации явно отмечаются
в аналитике. Бюджеты задаются в KZT; событие имеет приоритет над общим бюджетом.
Мобильное приложение устанавливается через браузер. Финансовые данные, скриншоты
и VPN-ключи не кешируются; для сохранения операций нужно подключение к интернету.

## Фоновая обработка активности

Codex CLI запускается на хосте и должен быть авторизован (`codex login status`).
Шаблон `scripts/activity-codex.service` использует путь `/opt/zibzibzib_projects`;
для другого пути замените `WorkingDirectory` и `ExecStart`. Установите службу:

```bash
sudo cp scripts/activity-codex.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now activity-codex.service
```

Удалите старую cron-строку `activity_codex_watch.py`, чтобы использовать один
способ запуска. Рабочий процесс блокирует повторный запуск, восстанавливает
зависшие запросы и повторяет неудачные попытки через пять минут.
`ACTIVITY_DB` и `ACTIVITY_BREAKDOWN_DIR` позволяют задать пути на хосте.

## Импорт VPN

Используйте `vpn://` или QR из панели в AmneziaVPN с поддержкой AWG 3.1.
Импорт `.conf` в некоторых версиях Amnezia теряет `HeaderProtectionKey`, что
приводит к отсутствию handshake. Существующий профиль в этом случае нужно
переимпортировать через `vpn://`. Сам ключ сервера менять не требуется.
