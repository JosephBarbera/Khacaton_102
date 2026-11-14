# Афиша культуры Башкортостана — Telegram-бот

Информационный сервис о культурных мероприятиях Республики Башкортостан.

## Запуск

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # вставьте токен от @BotFather в BOT_TOKEN
python import_events.py data/events_sample.json
python bot.py
```

## Структура

- `bot.py` — логика бота: меню, фильтры, поиск, карточки, подписки, обработка ошибок;
- `db.py` — схема и запросы SQLite (поля события — по разделу 8.1 ТЗ);
- `import_events.py` — идемпотентный импорт CSV/JSON с отчётом;
- `data/events_sample.json` — тестовые события (даты относительные: «+3 19:00»).

