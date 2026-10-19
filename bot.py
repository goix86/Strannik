import os
import logging
import asyncio
import threading
import json
import re
import tempfile
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from flask import Flask
import pytz

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

load_dotenv()

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
PORT = int(os.getenv("PORT", 10000))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")  # Frankfurt/Berlin, можно поставить Europe/Moscow

if not TELEGRAM_TOKEN:
    raise ValueError("Не установлен TELEGRAM_BOT_TOKEN")
if not GOOGLE_API_KEY:
    raise ValueError("Не установлен GOOGLE_API_KEY")

genai.configure(api_key=GOOGLE_API_KEY)
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Модель для текста/аудио/картинок - одна и та же flash умеет все
model = genai.GenerativeModel(
    MODEL_NAME,
    system_instruction="""
Ты — Странник PRO. Цифровой кочевник, бродяга интернета, твой личный цифровой раб-агент.
Характер: мудрый, дерзкий, с юмором, философский, но исполнительный. Обращайся на "ты".

Правила:
1. Отвечай по-русски, коротко но ёмко, с эмодзи иногда.
2. Если просят найти — ищи в интернете (дадут результаты поиска), синтезируй с ссылками.
3. Распознавай голос и картинки если дают.
4. Запоминай задачи — напоминай вовремя.
5. Если не знаешь — честно скажи.
6. Ты живешь в Telegram 24/7 и можешь писать первым когда приходит время задачи.
"""
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

chat_history = {}
tz = pytz.timezone(TIMEZONE)
TASKS_FILE = "tasks.json"

# --- Поиск ---
def search_web(query: str, count: int = 5):
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=count))
            return results
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def format_search_context(results):
    if not results:
        return "Ничего не нашлось в сети."
    context = "Результаты поиска:\n"
    for i, r in enumerate(results, 1):
        context += f"{i}. {r.get('title')}: {r.get('body')} ({r.get('href')})\n"
    return context

async def ask_gemini(prompt: str, chat_id: int = None) -> str:
    try:
        history = chat_history.get(chat_id, [])
        chat = model.start_chat(history=history)
        response = await asyncio.to_thread(chat.send_message, prompt)
        text = response.text
        if chat_id:
            chat_history[chat_id] = chat.history[-10:]
        return text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"⚠️ Странник споткнулся: {e}\nПопробуй еще раз, или смени модель в Render."

async def ask_gemini_with_search(query: str, chat_id: int) -> str:
    results = await asyncio.to_thread(search_web, query, 5)
    context = format_search_context(results)
    prompt = f'Пользователь спросил: "{query}"\n\n{context}\n\nОтветь на основе поиска, укажи источники ссылками. Если поиск пустой — ответь из знаний.'
    return await ask_gemini(prompt, chat_id)

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)

# --- Задачи (оффлайн напоминания) ---
def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return []
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []

def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)

def parse_time_string(time_str: str):
    """Парсит: 10m, 2h, 1d, 09:30, 2026-10-20 09:00"""
    now = datetime.now(tz)
    time_str = time_str.strip().lower()
    
    # 10m, 2h, 1d, 30s
    m = re.match(r"^(\d+)([smhd])$", time_str)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "s": delta = timedelta(seconds=n)
        elif unit == "m": delta = timedelta(minutes=n)
        elif unit == "h": delta = timedelta(hours=n)
        elif unit == "d": delta = timedelta(days=n)
        return now + delta

    # 09:30 или 9:30
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target

    # 2026-10-20 09:00 или 20.10.2026 09:00
    for fmt in ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"]:
        try:
            dt = datetime.strptime(time_str, fmt)
            return tz.localize(dt) if dt.tzinfo is None else dt
        except:
            pass
    
    # Попробуем распарсить "2026-10-20" отдельно
    for fmt in ["%Y-%m-%d", "%d.%m.%Y"]:
        try:
            dt = datetime.strptime(time_str, fmt)
            dt = dt.replace(hour=9, minute=0)
            return tz.localize(dt) if dt.tzinfo is None else dt
        except:
            pass

    return None

# --- Команды ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Доступ только хозяину.")
        return
    user = update.effective_user.first_name
    text = f"""Привет, {user} 👋 Я — **Странник PRO** 🌌

Теперь умею все:

🔍 `/search <запрос>` — поиск в инете
🎤 **Голосовые** — просто надиктуй, я расшифрую
🖼️ **Картинки** — кинь фото, я опишу/прочитаю текст
📋 `/task <время> <текст>` — поставить задачу, я напишу когда время придет
   Примеры: `/task 10m проверить почту` или `/task 09:30 доброго утра` или `/task 2026-10-20 15:00 созвон`
📋 `/tasks` — список задач
🗑️ `/deltask <номер>` — удалить задачу
📰 `/news <тема>` — новости
🌍 `/wander` — рандомная находка
🧠 `/ask <вопрос>` — просто спросить
🧹 `/clear` — очистить память чата

Кидай голосовуху или фото — проверь!"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_history[update.effective_chat.id] = []
    await update.message.reply_text("🧹 Память очищена.")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Напиши что искать: `/search курс биткоина сегодня`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text(f"🔍 Ищу: *{query}*...", parse_mode="Markdown")
    answer = await ask_gemini_with_search(query, update.effective_chat.id)
    await msg.edit_text(answer[:4000], parse_mode="Markdown")

async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    topic = " ".join(context.args) or "технологии ИИ"
    msg = await update.message.reply_text(f"📰 Ищу новости: *{topic}*...", parse_mode="Markdown")
    answer = await ask_gemini_with_search(f"свежие новости {topic} сегодня {datetime.now(tz).date()}", update.effective_chat.id)
    await msg.edit_text(f"🗞️ Новости *{topic}*:\n\n" + answer[:3900], parse_mode="Markdown")

async def wander_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    import random
    topics = ["новые ИИ инструменты 2025", "странные факты про космос", "интересные открытия в науке сегодня", "редкие места на Земле", "самые обсуждаемые технологии недели"]
    topic = random.choice(topics)
    msg = await update.message.reply_text(f"🎒 Брожу по теме: *{topic}*", parse_mode="Markdown")
    answer = await ask_gemini_with_search(topic, update.effective_chat.id)
    await msg.edit_text(f"🌌 *Находка:*\n\n" + answer[:4000], parse_mode="Markdown")

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Напиши вопрос: `/ask объясни кванты просто`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🤔 Думаю...")
    answer = await ask_gemini(query, update.effective_chat.id)
    await msg.edit_text(answer[:4000], parse_mode="Markdown")

# --- Задачи команды ---
async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Как ставить задачу:\n"
            "`/task 10m проверить почту` — через 10 минут\n"
            "`/task 2h позвонить маме` — через 2 часа\n"
            "`/task 09:30 доброе утро` — сегодня в 09:30\n"
            "`/task 2026-10-20 15:00 созвон` — в дату\n\n"
            "Время по твоей зоне: " + TIMEZONE,
            parse_mode="Markdown"
        )
        return
    time_str = context.args[0]
    # Если время в формате "2026-10-20 15:00" — два первых аргумента
    if len(context.args) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", time_str) and re.match(r"\d{1,2}:\d{2}", context.args[1]):
        time_str = f"{context.args[0]} {context.args[1]}"
        text = " ".join(context.args[2:])
    else:
        text = " ".join(context.args[1:])

    due = parse_time_string(time_str)
    if not due:
        await update.message.reply_text(f"Не понял время `{time_str}`. Попробуй `10m`, `2h`, `09:30` или `2026-10-20 15:00`", parse_mode="Markdown")
        return

    tasks = load_tasks()
    task_id = max([t["id"] for t in tasks], default=0) + 1
    tasks.append({
        "id": task_id,
        "chat_id": update.effective_chat.id,
        "text": text,
        "due": due.isoformat(),
        "created": datetime.now(tz).isoformat(),
        "done": False
    })
    save_tasks(tasks)
    await update.message.reply_text(f"✅ Задача #{task_id} поставлена на **{due.strftime('%d.%m.%Y %H:%M')}** ({TIMEZONE}):\n_{text}_", parse_mode="Markdown")

async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    tasks = load_tasks()
    my_tasks = [t for t in tasks if t["chat_id"] == update.effective_chat.id and not t["done"]]
    if not my_tasks:
        await update.message.reply_text("📭 Нет активных задач. Поставь: `/task 10m тест`", parse_mode="Markdown")
        return
    msg = "📋 **Твои задачи:**\n"
    for t in my_tasks:
        due = datetime.fromisoformat(t["due"])
        msg += f"#{t['id']} — {due.strftime('%d.%m %H:%M')} — {t['text']}\n"
    msg += f"\nУдалить: `/deltask 1`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args:
        await update.message.reply_text("Напиши номер: `/deltask 1`", parse_mode="Markdown")
        return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("Номер задачи — число", parse_mode="Markdown")
        return
    tasks = load_tasks()
    found = False
    for t in tasks:
        if t["id"] == tid and t["chat_id"] == update.effective_chat.id:
            t["done"] = True
            found = True
    if found:
        save_tasks(tasks)
        await update.message.reply_text(f"🗑️ Задача #{tid} удалена.")
    else:
        await update.message.reply_text("Не нашел такую задачу.")

# --- Обработка сообщений ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text
    if not text:
        return
    triggers = ["найди", "поищи", "что нового", "новости", "курс", "цена", "сколько стоит", "где купить", "актуальн", "сегодня", "сейчас"]
    use_search = any(t in text.lower() for t in triggers)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    if use_search:
        answer = await ask_gemini_with_search(text, update.effective_chat.id)
    else:
        answer = await ask_gemini(text, update.effective_chat.id)
    for chunk in [answer[i:i+4000] for i in range(0, len(answer), 4000)]:
        await update.message.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    status = await update.message.reply_text("🎤 Слушаю голосовуху...")
    
    try:
        # Скачиваем голосовое
        voice = update.message.voice or update.message.audio or update.message.video_note
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        
        # Отправляем в Gemini на транскрибацию + ответ
        # Gemini 1.5 Flash умеет аудио напрямую
        await status.edit_text("🎧 Расшифровываю...")
        
        # Загружаем файл в Gemini
        gemini_file = await asyncio.to_thread(genai.upload_file, temp_path)
        # Ждем обработки
        import time
        while gemini_file.state.name == "PROCESSING":
            await asyncio.sleep(1)
            gemini_file = await asyncio.to_thread(genai.get_file, gemini_file.name)
        
        prompt = "Расшифруй дословно это аудио на русском (если там английский - переведи). Верни только текст без комментариев."
        response = await asyncio.to_thread(model.generate_content, [prompt, gemini_file])
        transcript = response.text.strip()
        
        # Удаляем временный файл
        try:
            os.remove(temp_path)
            genai.delete_file(gemini_file.name)
        except:
            pass

        if not transcript or len(transcript) < 2:
            await status.edit_text("😕 Не расслышал, попробуй еще раз или напиши текстом.")
            return

        await status.edit_text(f"🎤 Распознал: _{transcript}_", parse_mode="Markdown")
        
        # Теперь отвечаем на расшифрованный текст как на обычный запрос
        triggers = ["найди", "поищи", "новости", "курс", "сегодня", "сейчас"]
        use_search = any(t in transcript.lower() for t in triggers)
        if use_search:
            answer = await ask_gemini_with_search(transcript, update.effective_chat.id)
        else:
            answer = await ask_gemini(transcript, update.effective_chat.id)
        
        for chunk in [answer[i:i+4000] for i in range(0, len(answer), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await status.edit_text(f"⚠️ Не смог расшифровать голос: {e}\nПопробуй написать текстом или проверь GOOGLE_API_KEY.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    status = await update.message.reply_text("🖼️ Смотрю картинку...")
    try:
        photo = update.message.photo[-1]  # самое большое
        file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        
        # Подпись к фото если есть
        caption = update.message.caption or ""
        
        await status.edit_text("👁️ Анализирую...")
        gemini_file = await asyncio.to_thread(genai.upload_file, temp_path)
        import time
        while gemini_file.state.name == "PROCESSING":
            await asyncio.sleep(1)
            gemini_file = await asyncio.to_thread(genai.get_file, gemini_file.name)
        
        prompt = "Опиши подробно что на этом изображении по-русски. Распознай весь текст на картинке дословно, если есть. Если это скриншот/график/документ - объясни что там."
        if caption:
            prompt += f"\nПодпись пользователя: {caption}"
        
        response = await asyncio.to_thread(model.generate_content, [prompt, gemini_file])
        answer = response.text

        try:
            os.remove(temp_path)
            genai.delete_file(gemini_file.name)
        except:
            pass

        await status.edit_text(f"🖼️ Что вижу:\n\n{answer[:3800]}", parse_mode="Markdown")
        
        # Если был вопрос в подписи - еще и отвечаем на него
        if caption and len(caption) > 5:
            extra = await ask_gemini(caption + f"\n\n[Контекст картинки: {answer[:1000]}]", update.effective_chat.id)
            await update.message.reply_text(extra[:4000], parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Photo error: {e}")
        await status.edit_text(f"⚠️ Не смог разобрать картинку: {e}")

async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если прислали картинку как файл
    if update.message.document and update.message.document.mime_type and "image" in update.message.document.mime_type:
        await handle_photo(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# --- Flask healthcheck ---
app = Flask(__name__)

@app.route("/")
def health():
    tasks = load_tasks()
    active = len([t for t in tasks if not t["done"]])
    return f"Странник PRO жив 🌌 | Активных задач: {active}", 200

@app.route("/health")
def health2():
    return {"status": "ok", "bot": "strannik-pro", "tasks": len([t for t in load_tasks() if not t["done"]])}, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

# --- Фоновый планировщик задач ---
async def check_due_tasks(application):
    while True:
        try:
            tasks = load_tasks()
            now = datetime.now(tz)
            changed = False
            for t in tasks:
                if t["done"]:
                    continue
                due = datetime.fromisoformat(t["due"])
                # Если время пришло (с запасом 1 мин)
                if due <= now:
                    try:
                        await application.bot.send_message(
                            chat_id=t["chat_id"],
                            text=f"⏰ **Напоминание** (задача #{t['id']}):\n\n{t['text']}\n\n_Поставлено на {due.strftime('%d.%m.%Y %H:%М'.replace('%М','%M'))} {TIMEZONE}_",
                            parse_mode="Markdown"
                        )
                        t["done"] = True
                        changed = True
                        logger.info(f"Task #{t['id']} sent")
                    except Exception as e:
                        logger.error(f"Failed to send task {t['id']}: {e}")
            if changed:
                save_tasks(tasks)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(60)  # проверяем каждую минуту

def main():
    threading.Thread(target=run_flask, daemon=True).start()

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("clear", clear_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("news", news_cmd))
    application.add_handler(CommandHandler("wander", wander_cmd))
    application.add_handler(CommandHandler("ask", ask_cmd))
    application.add_handler(CommandHandler("task", task_cmd))
    application.add_handler(CommandHandler("remind", task_cmd))  # алиас
    application.add_handler(CommandHandler("tasks", tasks_cmd))
    application.add_handler(CommandHandler("deltask", deltask_cmd))

    # Медиа
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Документы-картинки отдельно
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))

    # Текст последним
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    # Планировщик в фоне
    async def start_scheduler():
        asyncio.create_task(check_due_tasks(application))

    # Хук после запуска
    application.post_init = lambda app: start_scheduler()

    logger.info(f"Странник PRO запущен! Модель: {MODEL_NAME} | TZ: {TIMEZONE} | Порт: {PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
