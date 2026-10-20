import os
import logging
import asyncio
import threading
import json
import re
import tempfile
import time as time_module
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
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
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")

if not TELEGRAM_TOKEN:
    raise ValueError("Не установлен TELEGRAM_BOT_TOKEN")
if not GOOGLE_API_KEY:
    raise ValueError("Не установлен GOOGLE_API_KEY")

MODEL_NAME_RAW = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
MODEL_NAME = MODEL_NAME_RAW.strip().replace("models/", "").strip()
if MODEL_NAME.startswith("gemin-"):
    MODEL_NAME = "gemini-" + MODEL_NAME[6:]
# Фолбэк - Гугл в Oct 2026 советует 3.8-flash вместо 2.0, пробуем все
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro", "gemini-2.5-pro", "gemini-2.0-flash-001", "gemini-1.5-flash-001"]
IS_NEW_KEY = GOOGLE_API_KEY.startswith("AQ.")

# --- Настройка Gemini: пробуем новую библиотеку для AQ ключей ---
USE_NEW_SDK = False
genai_client = None
genai_old_model = None

try:
    # Новая библиотека google-genai для AQ. ключей - используем API v1, не v1beta
    from google import genai as new_genai
    from google.genai import types as new_types
    try:
        genai_client = new_genai.Client(api_key=GOOGLE_API_KEY, http_options=new_types.HttpOptions(api_version="v1"))
    except:
        genai_client = new_genai.Client(api_key=GOOGLE_API_KEY)
    USE_NEW_SDK = True
    logging.info(f"Используем новый SDK google-genai (v1) для ключа {GOOGLE_API_KEY[:6]}... (AQ key)")
except Exception as e:
    logging.warning(f"Не удалось инициализировать новый SDK: {e}, пробуем старый")
    USE_NEW_SDK = False

if not USE_NEW_SDK:
    try:
        import google.generativeai as genai_old
        genai_old.configure(api_key=GOOGLE_API_KEY)
        genai_old_model = genai_old.GenerativeModel(
            MODEL_NAME,
            system_instruction="Ты — Странник PRO. Цифровой кочевник, бродяга интернета. Отвечай по-русски, коротко с юмором, будь полезным. Умеешь искать, распознавать голос и картинки, ставить задачи."
        )
        logging.info(f"Используем старый SDK google-generativeai для модели {MODEL_NAME}")
    except Exception as e:
        logging.error(f"Не удалось инициализировать ни один Gemini SDK: {e}")

SYSTEM_PROMPT = "Ты — Странник PRO. Цифровой кочевник, бродяга интернета. Отвечай по-русски, дружелюбно, с юмором, коротко но ёмко. Умеешь искать, распознавать голос и картинки, ставить задачи."

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

chat_history = {}  # {chat_id: [{"role": "user", "text": "..."}, ...]}
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

# --- Универсальный вызов Gemini с авто-фолбэком ---
async def _call_gemini_with_model(model_name: str, full_prompt: str) -> str:
    if USE_NEW_SDK and genai_client:
        def call_new():
            resp = genai_client.models.generate_content(model=model_name, contents=full_prompt)
            return resp.text
        return await asyncio.to_thread(call_new)
    else:
        def call_old():
            # Для старого SDK создаем модель под конкретное имя
            import google.generativeai as genai_old_tmp
            m = genai_old_tmp.GenerativeModel(model_name, system_instruction=SYSTEM_PROMPT)
            chat = m.start_chat(history=[])
            resp = chat.send_message(full_prompt)
            return resp.text
        return await asyncio.to_thread(call_old)

async def ask_gemini(prompt: str, chat_id: int = None) -> str:
    history = chat_history.get(chat_id, [])
    full_prompt = SYSTEM_PROMPT + "\n\n"
    for h in history[-6:]:
        full_prompt += f"{h['role']}: {h['text']}\n"
    full_prompt += f"user: {prompt}\nassistant:"

    # Список моделей для попытки: сначала то что в env, потом фолбэки
    # Если ошибка подсказала модель (models/gemini-3.8-flash) - добавляем её первой
    suggested = None
    # Попробуем вытащить из последней ошибки если была
    candidates = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_error = None
    for model_try in candidates:
        # Чистим каждую попытку
        model_try = model_try.strip().replace("models/", "")
        if model_try.startswith("gemin-"):
            model_try = "gemini-" + model_try[6:]
        try:
            text = await _call_gemini_with_model(model_try, full_prompt)
            # Если успешно и это не основная модель - логируем
            if model_try != MODEL_NAME:
                logger.info(f"Фолбэк с {MODEL_NAME} на {model_try} сработал")
            # Сохраняем историю
            if chat_id:
                if chat_id not in chat_history:
                    chat_history[chat_id] = []
                chat_history[chat_id].append({"role": "user", "text": prompt})
                chat_history[chat_id].append({"role": "assistant", "text": text})
                chat_history[chat_id] = chat_history[chat_id][-10:]
            return text
        except Exception as e:
            err = str(e)
            last_error = e
            # Вытаскиваем подсказку Гугла: Please use models/gemini-3.8-flash
            if "Please update your code to use models/" in err:
                try:
                    suggested = err.split("models/")[1].split("'")[0].split('"')[0].split()[0].strip(" '\".,")
                    if suggested and suggested not in candidates:
                        candidates.insert(candidates.index(model_try)+1, suggested)
                        logger.info(f"Гугл подсказал модель {suggested}, добавляю в очередь")
                except:
                    pass
            if "NOTFOUND" in err or "not found" in err.lower() or "not supported" in err.lower() or "INVALID_ARGUMENT" in err or "unexpected model" in err.lower():
                logger.warning(f"Модель {model_try} не подошла, пробую следующую")
                continue
            else:
                logger.error(f"Gemini error on {model_try}: {e}")
                break

    # Если все упали
    return f"⚠️ Странник споткнулся: {last_error}\n\nПопробуй `{candidates[1]}` — авто-перебор не помог. Проверь что GOOGLE_API_KEY рабочий и модель существует."

async def ask_gemini_with_search(query: str, chat_id: int) -> str:
    results = await asyncio.to_thread(search_web, query, 5)
    context = format_search_context(results)
    prompt = f'Пользователь спросил: "{query}"\n\n{context}\n\nОтветь на основе поиска, укажи источники ссылками. Если поиск пустой — ответь из знаний.'
    return await ask_gemini(prompt, chat_id)

async def ask_gemini_with_media(prompt: str, file_path: str, mime_type: str) -> str:
    """Для фото и аудио - с фолбэком моделей"""
    candidates = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_err = None
    for model_try in candidates:
        try:
            if USE_NEW_SDK and genai_client:
                def call_new_media():
                    uploaded = genai_client.files.upload(file=file_path)
                    retries = 10
                    while hasattr(uploaded, 'state') and getattr(uploaded.state, 'name', '') == "PROCESSING" and retries > 0:
                        time_module.sleep(1)
                        uploaded = genai_client.files.get(name=uploaded.name)
                        retries -= 1
                    resp = genai_client.models.generate_content(model=model_try.strip(), contents=[prompt, uploaded])
                    try:
                        genai_client.files.delete(name=uploaded.name)
                    except:
                        pass
                    return resp.text
                text = await asyncio.to_thread(call_new_media)
                return text
            else:
                import google.generativeai as genai_old
                def call_old_media():
                    uploaded = genai_old.upload_file(file_path, mime_type=mime_type)
                    while getattr(uploaded.state, 'name', '') == "PROCESSING":
                        time_module.sleep(1)
                        uploaded = genai_old.get_file(uploaded.name)
                    m = genai_old.GenerativeModel(model_try.strip())
                    resp = m.generate_content([prompt, uploaded])
                    try:
                        genai_old.delete_file(uploaded.name)
                    except:
                        pass
                    return resp.text
                text = await asyncio.to_thread(call_old_media)
                return text
        except Exception as e:
            last_err = e
            err = str(e)
            if "NOTFOUND" in err or "not found" in err.lower() or "not supported" in err.lower() or "INVALID_ARGUMENT" in err:
                logger.warning(f"Медиа модель {model_try} не подошла, пробую следующую")
                continue
            raise e
    raise last_err

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)

# --- Задачи ---
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
    now = datetime.now(tz)
    time_str = time_str.strip().lower()
    m = re.match(r"^(\d+)([smhd])$", time_str)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        if unit == "s": delta = timedelta(seconds=n)
        elif unit == "m": delta = timedelta(minutes=n)
        elif unit == "h": delta = timedelta(hours=n)
        elif unit == "d": delta = timedelta(days=n)
        return now + delta
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str)
    if m:
        h, minute = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    for fmt in ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S"]:
        try:
            dt = datetime.strptime(time_str, fmt)
            return tz.localize(dt) if dt.tzinfo is None else dt
        except:
            pass
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
    sdk_name = "google-genai (AQ)" if USE_NEW_SDK else "google-generativeai (AIza)"
    text = f"""Привет, {user} 👋 Я — **Странник PRO** 🌌
_SDK: {sdk_name} | Модель: {MODEL_NAME}_

Что умею:
🔍 `/search <запрос>` — поиск в инете
🎤 **Голосовые** — просто надиктуй
🖼️ **Картинки** — кинь фото, я опишу
📋 `/task <время> <текст>` — напомню, даже если ты оффлайн
   Примеры: `/task 10m проверить почту` или `/task 09:30 доброе утро`
📋 `/tasks` — список задач | 🗑️ `/deltask <номер>` — удалить
📰 `/news <тема>` — новости | 🌍 `/wander` — находка
🧠 `/ask <вопрос>` — спросить | 🧹 `/clear` — очистить память

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

async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Как: `/task 10m проверить почту` или `/task 09:30 доброе утро` или `/task 2026-10-20 15:00 созвон`", parse_mode="Markdown")
        return
    time_str = context.args[0]
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
    tasks.append({"id": task_id, "chat_id": update.effective_chat.id, "text": text, "due": due.isoformat(), "created": datetime.now(tz).isoformat(), "done": False})
    save_tasks(tasks)
    await update.message.reply_text(f"✅ Задача #{task_id} на **{due.strftime('%d.%m.%Y %H:%M')}** ({TIMEZONE}):\n_{text}_", parse_mode="Markdown")

async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    tasks = load_tasks()
    my_tasks = [t for t in tasks if t["chat_id"] == update.effective_chat.id and not t["done"]]
    if not my_tasks:
        await update.message.reply_text("📭 Нет задач. Поставь: `/task 10m тест`", parse_mode="Markdown")
        return
    msg = "📋 **Твои задачи:**\n"
    for t in my_tasks:
        due = datetime.fromisoformat(t["due"])
        msg += f"#{t['id']} — {due.strftime('%d.%m %H:%M')} — {t['text']}\n"
    msg += "\nУдалить: `/deltask 1`"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args:
        await update.message.reply_text("Напиши номер: `/deltask 1`", parse_mode="Markdown")
        return
    try:
        tid = int(context.args[0])
    except:
        await update.message.reply_text("Номер — число", parse_mode="Markdown")
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

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text
    if not text: return
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
        voice = update.message.voice or update.message.audio or update.message.video_note
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        await status.edit_text("🎧 Расшифровываю...")
        prompt = "Расшифруй дословно это аудио на русском (если английский - переведи). Верни только текст без комментариев."
        transcript = await ask_gemini_with_media(prompt, temp_path, "audio/ogg")
        try:
            os.remove(temp_path)
        except:
            pass
        if not transcript or len(transcript.strip()) < 2:
            await status.edit_text("😕 Не расслышал, попробуй еще раз текстом.")
            return
        await status.edit_text(f"🎤 Распознал: _{transcript.strip()}_", parse_mode="Markdown")
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
        await status.edit_text(f"⚠️ Не смог расшифровать: {e}\nПроверь GOOGLE_API_KEY или попробуй текстом.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    status = await update.message.reply_text("🖼️ Смотрю картинку...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        caption = update.message.caption or ""
        await status.edit_text("👁️ Анализирую...")
        prompt = "Опиши подробно что на изображении по-русски. Распознай весь текст дословно если есть. Если скриншот/график/документ - объясни."
        if caption:
            prompt += f"\nПодпись: {caption}"
        answer = await ask_gemini_with_media(prompt, temp_path, "image/jpeg")
        try:
            os.remove(temp_path)
        except:
            pass
        await status.edit_text(f"🖼️ Что вижу:\n\n{answer[:3800]}", parse_mode="Markdown")
        if caption and len(caption) > 5:
            extra = await ask_gemini(caption + f"\n\n[Контекст картинки: {answer[:1000]}]", update.effective_chat.id)
            await update.message.reply_text(extra[:4000], parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Photo error: {e}")
        await status.edit_text(f"⚠️ Не разобрал картинку: {e}")

async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document and update.message.document.mime_type and "image" in update.message.document.mime_type:
        await handle_photo(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")

app = Flask(__name__)
@app.route("/")
def health():
    tasks = load_tasks()
    active = len([t for t in tasks if not t["done"]])
    sdk = "new AQ" if USE_NEW_SDK else "old AIza"
    return f"Странник PRO жив 🌌 | SDK: {sdk} | Модель: {MODEL_NAME} | Задач: {active}", 200

@app.route("/health")
def health2():
    return {"status": "ok", "bot": "strannik-pro", "sdk": "new" if USE_NEW_SDK else "old", "model": MODEL_NAME, "tasks": len([t for t in load_tasks() if not t["done"]])}, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

async def check_due_tasks(application):
    while True:
        try:
            tasks = load_tasks()
            now = datetime.now(tz)
            changed = False
            for t in tasks:
                if t["done"]: continue
                due = datetime.fromisoformat(t["due"])
                if due <= now:
                    try:
                        await application.bot.send_message(chat_id=t["chat_id"], text=f"⏰ **Напоминание** (#{t['id']}):\n\n{t['text']}\n\n_На {due.strftime('%d.%m.%Y %H:%M')} {TIMEZONE}_", parse_mode="Markdown")
                        t["done"] = True
                        changed = True
                    except Exception as e:
                        logger.error(f"Failed task {t['id']}: {e}")
            if changed:
                save_tasks(tasks)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(60)

def main():
    threading.Thread(target=run_flask, daemon=True).start()
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("clear", clear_cmd))
    application.add_handler(CommandHandler("search", search_cmd))
    application.add_handler(CommandHandler("news", news_cmd))
    application.add_handler(CommandHandler("wander", wander_cmd))
    application.add_handler(CommandHandler("ask", ask_cmd))
    application.add_handler(CommandHandler("task", task_cmd))
    application.add_handler(CommandHandler("remind", task_cmd))
    application.add_handler(CommandHandler("tasks", tasks_cmd))
    application.add_handler(CommandHandler("deltask", deltask_cmd))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    async def start_scheduler():
        asyncio.create_task(check_due_tasks(application))
    application.post_init = lambda app: start_scheduler()
    logger.info(f"Странник PRO запущен! SDK={'NEW' if USE_NEW_SDK else 'OLD'} Модель: {MODEL_NAME} TZ: {TIMEZONE} Порт: {PORT}")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
