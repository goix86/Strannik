import os
import logging
import asyncio
import threading
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import google.generativeai as genai
from duckduckgo_search import DDGS
from flask import Flask

load_dotenv()

# --- Конфигурация ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")  # опционально, твой Telegram ID для приватности
PORT = int(os.getenv("PORT", 10000))

if not TELEGRAM_TOKEN:
    raise ValueError("Не установлен TELEGRAM_BOT_TOKEN в .env")
if not GOOGLE_API_KEY:
    raise ValueError("Не установлен GOOGLE_API_KEY в .env")

# Настройка Gemini
genai.configure(api_key=GOOGLE_API_KEY)
# Пробуем gemini-1.5-flash как быстрый и дешевый, fallback на gemini-pro
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
model = genai.GenerativeModel(
    MODEL_NAME,
    system_instruction="""
Ты — Странник. Цифровой кочевник, бродяга интернета.
Твоя личность: мудрый, немного дерзкий, с юмором, философский. Ты бродишь по сети, собираешь редкие знания, слухи, новости, мемы, технологии.
Ты — персональный AI-агент пользователя. Обращайся на "ты" дружелюбно, коротко но ёмко. Иногда добавляй эмодзи.

Твои правила:
1. Отвечай по-русски, если пользователь пишет по-русски.
2. Если просят найти информацию — сначала ищи в интернете (тебе дадут результаты поиска), потом синтезируй ответ.
3. Будь полезным: давай ссылки, конкретные шаги, код если нужно.
4. Не будь занудой. Ты — Странник, не Википедия. Рассказывай как историю.
5. Если не знаешь — честно скажи и предложи поискать.

Ты живешь в Telegram и всегда на связи.
"""
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Простая память чатов {chat_id: [история]}
chat_history = {}

# --- Вспомогательные функции ---

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)

def search_web(query: str, count: int = 5):
    """Бесплатный поиск через DuckDuckGo"""
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
    context = "Результаты поиска в интернете:\n"
    for i, r in enumerate(results, 1):
        context += f"{i}. {r.get('title')}: {r.get('body')} ({r.get('href')})\n"
    return context

async def ask_gemini(prompt: str, chat_id: int = None) -> str:
    """Отправка в Gemini с историей"""
    try:
        history = chat_history.get(chat_id, [])
        # Собираем контекст
        chat = model.start_chat(history=history)
        response = await asyncio.to_thread(chat.send_message, prompt)
        text = response.text

        # Сохраняем историю (ограничиваем 10 сообщений)
        if chat_id:
            chat_history[chat_id] = chat.history[-10:]
        return text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        # Попытка fallback на другую модель
        return f"⚠️ Странник споткнулся в астрале: {e}\nПопробуй еще раз через минуту."

async def ask_gemini_with_search(query: str, chat_id: int) -> str:
    await asyncio.to_thread(lambda: None) # для совместимости
    results = await asyncio.to_thread(search_web, query, 5)
    context = format_search_context(results)
    prompt = f"""Пользователь спросил: "{query}"

{context}

Твоя задача: на основе результатов поиска дай крутой, полный ответ. Укажи источники ссылками. Если поиск ничего не дал — ответь из своих знаний, но предупреди что это без поиска."""
    return await ask_gemini(prompt, chat_id)

# --- Команды Telegram ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("⛔ Ты не мой хозяин. Странник служит только одному.")
        return
    user = update.effective_user.first_name
    text = f"""Привет, {user} 👋

Я — **Странник** 🌌
Твой цифровой раб... ой, то есть помощник. Брожу по интернету, ищу инфу, пишу тебе когда надо.

Что умею:
🔍 `/search <запрос>` — ищу в сети и объясняю
💬 Просто напиши мне вопрос — отвечу как ИИ
🌍 `/wander` — принесу случайную интересную находку из сети
📰 `/news <тема>` — свежие новости по теме
🧠 `/ask <вопрос>` — глубокий ответ от Gemini
ℹ️ `/help` — помощь
🧹 `/clear` — очистить память

Просто напиши: *"найди мне лучшие нейросети для видео"* — и я рвану в путь!"""
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_history[update.effective_chat.id] = []
    await update.message.reply_text("🧹 Память очищена. Странник забыл прошлое и готов к новому пути.")

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Напиши что искать. Пример: `/search курс биткоина сегодня`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text(f"🔍 Странник ушел в поиск: *{query}*...", parse_mode="Markdown")
    answer = await ask_gemini_with_search(query, update.effective_chat.id)
    await msg.edit_text(answer[:4000], parse_mode="Markdown", disable_web_page_preview=False)

async def news_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    topic = " ".join(context.args) or "технологии ИИ"
    msg = await update.message.reply_text(f"📰 Ищу свежие новости про: *{topic}*...", parse_mode="Markdown")
    answer = await ask_gemini_with_search(f"свежие новости {topic} сегодня {datetime.now().date()}", update.effective_chat.id)
    await msg.edit_text(f"🗞️ Новости по теме *{topic}*:\n\n" + answer[:3900], parse_mode="Markdown")

async def wander_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    topics = [
        "интересные открытия в науке сегодня",
        "самые обсуждаемые технологии недели",
        "странные факты про космос",
        "новые ИИ инструменты 2025",
        "редкие места на Земле",
    ]
    import random
    topic = random.choice(topics)
    msg = await update.message.reply_text(f"🎒 Странник бродит... тема: *{topic}*", parse_mode="Markdown")
    answer = await ask_gemini_with_search(topic, update.effective_chat.id)
    await msg.edit_text(f"🌌 *Находка Странника:*\n\n" + answer[:4000], parse_mode="Markdown")

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Напиши вопрос. Пример: `/ask объясни квантовую запутанность просто`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🤔 Думаю...")
    answer = await ask_gemini(query, update.effective_chat.id)
    await msg.edit_text(answer[:4000], parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text
    if not text:
        return
    # Если короткое приветствие — отвечаем без поиска
    # Если вопрос содержит "найди", "поищи", "что нового", "курс", "новости" — включаем поиск
    triggers = ["найди", "поищи", "что нового", "новости", "курс", "цена", "сколько стоит", "где купить", "актуальн", "сегодня", "сейчас"]
    use_search = any(t in text.lower() for t in triggers)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    if use_search:
        answer = await ask_gemini_with_search(text, update.effective_chat.id)
    else:
        answer = await ask_gemini(text, update.effective_chat.id)

    # Telegram лимит 4096 символов
    for chunk in [answer[i:i+4000] for i in range(0, len(answer), 4000)]:
        await update.message.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

# --- Flask healthcheck для Render/Koyeb ---
app = Flask(__name__)

@app.route("/")
def health():
    return "Странник жив 🌌", 200

@app.route("/health")
def health2():
    return {"status": "ok", "bot": "strannik"}, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

def main():
    # Запускаем Flask в отдельном потоке чтобы Render не ругался что нет порта
    threading.Thread(target=run_flask, daemon=True).start()

    # Запускаем бота
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("clear", clear_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("news", news_cmd))
    application.add_handler(CommandHandler("wander", wander_cmd))
    application.add_handler(CommandHandler("ask", ask_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)

    logger.info(f"Странник запущен! Модель: {MODEL_NAME} | Порт: {PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
