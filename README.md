# Странник — твой AI-агент в Telegram 🌌

Готовый бот на Python + Gemini + поиск в интернете. Пишешь ему в личку — он отвечает, ищет, приносит новости.

## ⚠️ СРОЧНО: Твои ключи утекли!

Ты скинул в чат живой токен бота и Google API ключ. **Любой может угнать твоего бота.**

1. Иди в [@BotFather](https://t.me/BotFather) -> `/mybots` -> выбери бота -> `API Token` -> `Revoke current token`
2. Пересоздай Google API ключ в https://aistudio.google.com/app/apikey
3. Новые ключи вставляй ТОЛЬКО в `.env` и в переменные окружения на хостинге, никогда в чат!

## Что умеет бот
- `/search <запрос>` — ищет в DuckDuckGo + отвечает через Gemini с ссылками
- Просто сообщение — умный ответ от Gemini с памятью диалога
- `/news <тема>` — свежие новости
- `/wander` — рандомная интересная находка (как сталкер)
- `/ask` — глубокий вопрос без поиска
- `/clear` — очистить память

Персонаж: дерзкий, мудрый Странник-интернет-бродяга.

## Локальный запуск (2 минуты)

```bash
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env
python bot.py
```

## Деплой на Render (бесплатно)

Render любит чтоб был открытый порт, поэтому в `bot.py` уже встроен Flask healthcheck.

1. Залей этот проект на GitHub (создай репозиторий `strannik-bot`, закинь `bot.py`, `requirements.txt`, `Dockerfile`)
2. Зайди на https://dashboard.render.com -> `New +` -> `Background Worker` (или `Web Service`)
   - Если выберешь **Web Service** — он не уснет благодаря Flask на порту 10000.
   - Если **Background Worker** — Flask не обязателен, но не мешает.
3. Подключи GitHub репозиторий
4. Настройки:
   - Runtime: `Python 3` или `Docker`
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
5. Вкладка `Environment` -> добавь переменные:
   - `TELEGRAM_BOT_TOKEN` = твой новый токен
   - `GOOGLE_API_KEY` = твой новый ключ
   - `GEMINI_MODEL` = `gemini-1.5-flash`
   - `ALLOWED_USER_ID` = твой ID (опционально)
   - `PORT` = `10000`
6. Deploy -> смотри логи. Должно быть `Странник запущен!`

> Бесплатный Render засыпает через 15 мин без трафика. Чтобы не засыпал — используй [UptimeRobot](https://uptimerobot.com) пинговать твой `https://твой-сервис.onrender.com/health` каждые 5 минут.

## Деплой на Koyeb (лучше, не спит)

Koyeb дает бесплатный инстанс который не спит — идеально для бота.

1. https://app.koyeb.com -> `Create Service` -> `GitHub`
2. Выбери репозиторий
3. Builder: `Dockerfile` (или `Buildpack` Python)
4. Run command: `python bot.py`
5. `Environment variables` — те же 3 ключа
6. Port: `10000` (HTTP)
7. Health check: path `/health`
8. Deploy. Готово!

Koyeb не требует пинга, бот будет онлайн 24/7.

## Как сделать бота приватным?

Узнай свой ID: напиши [@userinfobot](https://t.me/userinfobot) в телеге, он пришлет `Your user ID: 123456789`
Вставь его в `ALLOWED_USER_ID`. Тогда бот будет игнорить всех кроме тебя — твой личный раб.

## Хочешь прокачать Странника?

Идеи что добавить дальше (скажи мне — допишу код):
- ⏰ Напоминания и ежедневный дайджест в 9:00 по Москве
- 📈 Мониторинг курса крипты/новостей и алерты тебе
- 🧠 Долгая память + база знаний (Notion/Google Sheets)
- 🔊 Озвучка ответов голосом
- 👥 Режим "исследователь": бот сам ходит по 10 сайтам и делает сводку

## Частые ошибки
- `401 Unauthorized` — неверный токен Telegram, перевыпусти
- `API key not valid` — ключ Gemini неверный. Твой `AQ.Ab8...` похож на Google OAuth токен, а нужен ключ вида `AIza...` с https://aistudio.google.com/app/apikey
- Бот молчит — смотри логи на Render/Koyeb, пиши `/start` ему в личку
