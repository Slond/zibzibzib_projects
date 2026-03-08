# zibzibzib Projects

FastAPI с разными проектами

## Модули

- **Dashboard** (`/`) — главная страница с плитками сервисов, админка
- **Finance** (`/finance`) — учет личных финансов с webhook для iOS Shortcuts
- **Weather** (`/weather`) — мониторинг датчиков Yandex Smart Home

## Быстрый старт

```bash
docker network create web_net

cp .env.example .env

docker compose up -d
```

Приложение будет доступно на http://localhost:8199

## Миграции базы данных

Используется Alembic для миграций:

```bash
# Применить все миграции
alembic upgrade head

# Создать новую миграцию (автогенерация)
alembic revision --autogenerate -m "описание"

# Откатить миграцию
alembic downgrade -1

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
│   │   └── weather.py    # Weather monitoring
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

Подробная инструкция: см. `.cursor/plans/ios_shortcut_form_*.plan.md`

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
