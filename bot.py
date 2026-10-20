import os
import logging
import asyncio
import threading
import json
import re
import tempfile
import time as time_module
import random
from datetime import datetime, timedelta

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from flask import Flask
import pytz
import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")
PORT = int(os.getenv("PORT", 10000))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin")

if not TELEGRAM_TOKEN:
    raise ValueError("Не установлен TELEGRAM_BOT_TOKEN")
if not GOOGLE_API_KEY:
    raise ValueError("Не установлен GOOGLE_API_KEY")

MODEL_NAME_RAW = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MODEL_NAME = MODEL_NAME_RAW.strip().replace("models/", "").strip()
if MODEL_NAME.startswith("gemin-"):
    MODEL_NAME = "gemini-" + MODEL_NAME[6:]
FALLBACK_MODELS = ["gemini-3.8-flash", "gemini-1.5-flash", "gemini-1.5-flash-latest", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-exp", "gemini-1.5-pro", "gemini-2.5-pro", "gemini-2.0-flash-001", "gemini-1.5-flash-001"]
IS_NEW_KEY = GOOGLE_API_KEY.startswith("AQ.")

USE_NEW_SDK = False
genai_client = None
genai_old_model = None

try:
    from google import genai as new_genai
    from google.genai import types as new_types
    try:
        genai_client = new_genai.Client(api_key=GOOGLE_API_KEY, http_options=new_types.HttpOptions(api_version="v1"))
    except:
        genai_client = new_genai.Client(api_key=GOOGLE_API_KEY)
    USE_NEW_SDK = True
    logging.info(f"SDK google-genai (v1) для {GOOGLE_API_KEY[:6]}...")
except Exception as e:
    logging.warning(f"Новый SDK не завелся: {e}")
    USE_NEW_SDK = False

if not USE_NEW_SDK:
    try:
        import google.generativeai as genai_old
        genai_old.configure(api_key=GOOGLE_API_KEY)
        genai_old_model = genai_old.GenerativeModel(MODEL_NAME, system_instruction="Ты — Странник PRO. Отвечай по-русски, коротко с юмором.")
        logging.info(f"SDK google-generativeai для {MODEL_NAME}")
    except Exception as e:
        logging.error(f"Ни один SDK не завелся: {e}")

SYSTEM_PROMPT = "Ты — Странник PRO. Цифровой кочевник. Отвечай по-русски, дружелюбно, с юмором. Умеешь искать, голос, картинки, спутник."

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

chat_history = {}
tz = pytz.timezone(TIMEZONE)
TASKS_FILE = "tasks.json"

# --- Спутниковые красивые места (координаты) ---
RANDOM_SAT_PLACES = [
    {"name": "Байкал, Россия", "lat": 53.5, "lon": 108.0},
    {"name": "Гранд-Каньон, США", "lat": 36.1, "lon": -112.1},
    {"name": "Сахара, Африка", "lat": 23.0, "lon": 12.5},
    {"name": "Гималаи, Эверест", "lat": 27.98, "lon": 86.92},
    {"name": "Амазонка, Бразилия", "lat": -3.0, "lon": -60.0},
    {"name": "Вулкан Фудзи, Япония", "lat": 35.36, "lon": 138.72},
    {"name": "Великий Барьерный риф, Австралия", "lat": -18.2, "lon": 147.8},
    {"name": "Исландия, вулканы", "lat": 64.8, "lon": -19.0},
    {"name": "Пальма Джумейра, Дубай", "lat": 25.11, "lon": 55.13},
    {"name": "Антарктида, шельф", "lat": -77.0, "lon": 166.0},
    {"name": "Кападокия, Турция", "lat": 38.64, "lon": 34.83},
    {"name": "Мачу-Пикчу, Перу", "lat": -13.16, "lon": -72.54},
    {"name": "Санторини, Греция", "lat": 36.39, "lon": 25.46},
    {"name": "Нил, Египет", "lat": 26.0, "lon": 32.0},
    {"name": "Килиманджаро, Танзания", "lat": -3.07, "lon": 37.35},
    {"name": "Байконур, Казахстан", "lat": 45.6, "lon": 63.3},
    {"name": "Чернобыль, Украина", "lat": 51.38, "lon": 30.10},
    {"name": "Норвежские фьорды", "lat": 62.0, "lon": 7.0},
    {"name": "Гавайи, вулкан Килауэа", "lat": 19.4, "lon": -155.28},
    {"name": "Мальдивы", "lat": 3.2, "lon": 73.0},
]

def search_web(query: str, count: int = 5):
    try:
        with DDGS() as ddgs:
            return list(ddgs.text(query, max_results=count))
    except Exception as e:
        logger.error(f"Search error: {e}")
        return []

def format_search_context(results):
    if not results:
        return "Ничего не нашлось."
    ctx = "Результаты поиска:\n"
    for i, r in enumerate(results, 1):
        ctx += f"{i}. {r.get('title')}: {r.get('body')} ({r.get('href')})\n"
    return ctx

# --- Спутник ---
def geocode(place: str):
    """Находим координаты через Nominatim"""
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": place, "format": "json", "limit": 1}
        headers = {"User-Agent": "StrannikBot/1.0 (telegram bot)"}
        r = requests.get(url, params=params, headers=headers, timeout=10)
        data = r.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"]), data[0]["display_name"]
    except Exception as e:
        logger.error(f"Geocode error: {e}")
    return None

def satellite_image_url(lat: float, lon: float, delta: float = 0.05, size: str = "800,600"):
    """Esri World Imagery - бесплатный спутник без ключа"""
    lon1, lat1 = lon - delta, lat - delta
    lon2, lat2 = lon + delta, lat + delta
    # bbox в формате minX,minY,maxX,maxY
    url = (
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"
        f"?bbox={lon1},{lat1},{lon2},{lat2}"
        "&bboxSR=4326&imageSR=4326"
        f"&size={size}&format=jpg&f=image"
    )
    return url

# --- Gemini ---
async def _call_gemini_with_model(model_name: str, full_prompt: str) -> str:
    if USE_NEW_SDK and genai_client:
        def call_new():
            resp = genai_client.models.generate_content(model=model_name, contents=full_prompt)
            return resp.text
        return await asyncio.to_thread(call_new)
    else:
        def call_old():
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
    candidates = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_error = None
    for model_try in candidates:
        model_try = model_try.strip().replace("models/", "")
        if model_try.startswith("gemin-"):
            model_try = "gemini-" + model_try[6:]
        try:
            text = await _call_gemini_with_model(model_try, full_prompt)
            if model_try != MODEL_NAME:
                logger.info(f"Фолбэк {MODEL_NAME} -> {model_try}")
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
            if "Please update your code to use models/" in err:
                try:
                    suggested = err.split("models/")[1].split("'")[0].split('"')[0].split()[0].strip(" '\".,")
                    if suggested and suggested not in candidates:
                        candidates.insert(candidates.index(model_try)+1, suggested)
                except:
                    pass
            if "NOTFOUND" in err or "not found" in err.lower() or "not supported" in err.lower() or "INVALID_ARGUMENT" in err or "unexpected model" in err.lower() or "UNAVAILABLE" in err or "503" in err or "high demand" in err.lower() or "429" in err:
                logger.warning(f"Модель {model_try} не подошла, следующая")
                await asyncio.sleep(1)
                continue
            else:
                logger.error(f"Gemini {model_try}: {e}")
                if "503" in err or "UNAVAILABLE" in err:
                    continue
                break
    return f"⚠️ Странник споткнулся: {last_error}\nПопробуй позже."

async def ask_gemini_with_search(query: str, chat_id: int) -> str:
    results = await asyncio.to_thread(search_web, query, 5)
    ctx = format_search_context(results)
    prompt = f'Пользователь: "{query}"\n\n{ctx}\n\nОтветь на основе поиска, укажи ссылки. Если пусто — из знаний.'
    return await ask_gemini(prompt, chat_id)

async def ask_gemini_with_media(prompt: str, file_path: str, mime_type: str) -> str:
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
            if "NOTFOUND" in err or "not found" in err.lower() or "not supported" in err.lower() or "INVALID_ARGUMENT" in err or "UNAVAILABLE" in err or "503" in err or "high demand" in err.lower() or "429" in err:
                logger.warning(f"Медиа {model_try} перегружена, следующая")
                await asyncio.sleep(1)
                continue
            raise e
    raise last_err

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_ID:
        return True
    return str(update.effective_user.id) == str(ALLOWED_USER_ID)

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
        await update.message.reply_text("⛔ Только хозяину.")
        return
    user = update.effective_user.first_name
    sdk_name = "google-genai (AQ)" if USE_NEW_SDK else "google-generativeai"
    text = f"""Привет, {user} 👋 **Странник PRO + Спутник** 🌌🛰️

Что умею:
🔍 `/search <запрос>` — поиск
🎤 **Голос** — надиктуй
🖼️ **Фото** — кинь картинку
🛰️ `/sat <место>` — спутник над местом (напр. `/sat Байкал`)
🌍 `/earth` — рандомный красивый спутник Земли
📍 **Кинь геопозицию** (скрепка → Геопозиция) — пришлю спутник над тобой + что рядом
📋 `/task 10m текст` — напомню (пример: `/task 09:30 доброе утро`)
📋 `/tasks` — список задач | `/deltask 1` — удалить
📰 `/news <тема>` | 🌍 `/wander` | 🧠 `/ask` | 🧹 `/clear`

Кидай место или геопозицию — проверь спутник!"""
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
        await update.message.reply_text("Напиши: `/search курс биткоина сегодня`", parse_mode="Markdown")
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
    topics = ["новые ИИ инструменты 2025", "странные факты про космос", "интересные открытия в науке сегодня", "редкие места на Земле"]
    topic = random.choice(topics)
    msg = await update.message.reply_text(f"🎒 Брожу: *{topic}*", parse_mode="Markdown")
    answer = await ask_gemini_with_search(topic, update.effective_chat.id)
    await msg.edit_text(f"🌌 *Находка:*\n\n" + answer[:4000], parse_mode="Markdown")

async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Напиши: `/ask объясни кванты просто`", parse_mode="Markdown")
        return
    msg = await update.message.reply_text("🤔 Думаю...")
    answer = await ask_gemini(query, update.effective_chat.id)
    await msg.edit_text(answer[:4000], parse_mode="Markdown")

async def task_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Как: `/task 10m проверить почту` или `/task 09:30 доброе утро`", parse_mode="Markdown")
        return
    time_str = context.args[0]
    if len(context.args) >= 3 and re.match(r"\d{4}-\d{2}-\d{2}", time_str) and re.match(r"\d{1,2}:\d{2}", context.args[1]):
        time_str = f"{context.args[0]} {context.args[1]}"
        text = " ".join(context.args[2:])
    else:
        text = " ".join(context.args[1:])
    due = parse_time_string(time_str)
    if not due:
        await update.message.reply_text(f"Не понял `{time_str}`. Попробуй `10m`, `2h`, `09:30`", parse_mode="Markdown")
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
        await update.message.reply_text("📭 Нет задач. `/task 10m тест`", parse_mode="Markdown")
        return
    msg = "📋 **Твои задачи:**\n"
    for t in my_tasks:
        due = datetime.fromisoformat(t["due"])
        msg += f"#{t['id']} — {due.strftime('%d.%m %H:%M')} — {t['text']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def deltask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    if not context.args:
        await update.message.reply_text("Напиши: `/deltask 1`", parse_mode="Markdown")
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
        await update.message.reply_text("Не нашел.")

# --- Спутник команды ---
async def sat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    place = " ".join(context.args)
    if not place:
        await update.message.reply_text("Напиши место: `/sat Байкал` или `/sat Дубай` или кинь геопозицию 📍", parse_mode="Markdown")
        return
    msg = await update.message.reply_text(f"🛰️ Ищу спутник над: *{place}*...", parse_mode="Markdown")
    geo = await asyncio.to_thread(geocode, place)
    if not geo:
        await msg.edit_text(f"😕 Не нашел место `{place}`. Попробуй по-другому: `Москва`, `Байкал`", parse_mode="Markdown")
        return
    lat, lon, display = geo
    url = satellite_image_url(lat, lon, delta=0.05)
    # Отправляем фото
    try:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"🛰️ Спутник над: {display}\n📍 {lat:.4f}, {lon:.4f}\n🔗 Esri World Imagery")
        await msg.delete()
        # Добавляем интригу от ИИ про место
        info = await ask_gemini_with_search(f"интересные факты и свежие новости про {place} {display} коротко 3 пункта", update.effective_chat.id)
        await update.message.reply_text(f"📍 **{display}**\n\n{info[:3500]}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Sat error: {e}")
        await msg.edit_text(f"⚠️ Не смог загрузить спутник: {e}\nПопробуй другое место.")

async def earth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    place = random.choice(RANDOM_SAT_PLACES)
    msg = await update.message.reply_text(f"🌍 Лечу к: *{place['name']}*...", parse_mode="Markdown")
    url = satellite_image_url(place["lat"], place["lon"], delta=0.08 if "Сахара" in place["name"] or "Амазонка" in place["name"] else 0.05)
    caption = f"🌍 {place['name']}\n📍 {place['lat']}, {place['lon']}\n🛰️ Esri World Imagery"
    try:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=caption)
        await msg.delete()
        info = await ask_gemini_with_search(f"расскажи интересно про {place['name']} 3 факта + что видно со спутника", update.effective_chat.id)
        await update.message.reply_text(info[:3500], parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка спутника: {e}")

async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    loc = update.message.location
    lat, lon = loc.latitude, loc.longitude
    msg = await update.message.reply_text(f"📍 Ловлю спутник над тобой: {lat:.4f}, {lon:.4f}...")
    url = satellite_image_url(lat, lon, delta=0.02)  # поближе
    try:
        await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"🛰️ Спутник над тобой\n📍 {lat:.4f}, {lon:.4f}\nТвои координаты — никому не шлю, только тебе 😉")
        await msg.delete()
        # Что рядом?
        info = await ask_gemini_with_search(f"что находится по координатам {lat}, {lon} что рядом интересного", update.effective_chat.id)
        await update.message.reply_text(info[:3500], parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"Ошибка: {e}")

# --- Текст/голос/фото ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text
    if not text: return
    triggers = ["найди", "поищи", "что нового", "новости", "курс", "цена", "сколько стоит", "где купить", "актуальн", "сегодня", "сейчас", "спутник", "карта", "снимок"]
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
    status = await update.message.reply_text("🎤 Слушаю...")
    try:
        voice = update.message.voice or update.message.audio or update.message.video_note
        file = await voice.get_file()
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        await status.edit_text("🎧 Расшифровываю...")
        prompt = "Расшифруй дословно аудио на русском, верни только текст."
        transcript = await ask_gemini_with_media(prompt, temp_path, "audio/ogg")
        try: os.remove(temp_path)
        except: pass
        if not transcript or len(transcript.strip()) < 2:
            await status.edit_text("😕 Не расслышал, текстом попробуй.")
            return
        await status.edit_text(f"🎤 _{transcript.strip()}_", parse_mode="Markdown")
        triggers = ["найди", "поищи", "новости", "курс", "сегодня", "спутник"]
        use_search = any(t in transcript.lower() for t in triggers)
        answer = await ask_gemini_with_search(transcript, update.effective_chat.id) if use_search else await ask_gemini(transcript, update.effective_chat.id)
        for chunk in [answer[i:i+4000] for i in range(0, len(answer), 4000)]:
            await update.message.reply_text(chunk, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Voice: {e}")
        await status.edit_text(f"⚠️ {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    status = await update.message.reply_text("🖼️ Смотрю...")
    try:
        photo = update.message.photo[-1]
        file = await photo.get_file()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            temp_path = tf.name
        await file.download_to_drive(temp_path)
        caption = update.message.caption or ""
        await status.edit_text("👁️ Анализирую...")
        prompt = "Опиши подробно что на изображении по-русски, распознай текст если есть."
        if caption: prompt += f"\nПодпись: {caption}"
        answer = await ask_gemini_with_media(prompt, temp_path, "image/jpeg")
        try: os.remove(temp_path)
        except: pass
        await status.edit_text(f"🖼️ {answer[:3800]}", parse_mode="Markdown")
        if caption and len(caption) > 5:
            extra = await ask_gemini(caption + f"\n[Картинка: {answer[:1000]}]", update.effective_chat.id)
            await update.message.reply_text(extra[:4000], parse_mode="Markdown")
    except Exception as e:
        await status.edit_text(f"⚠️ {e}")

async def handle_document_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.document and update.message.document.mime_type and "image" in update.message.document.mime_type:
        await handle_photo(update, context)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} -> {context.error}")

app = Flask(__name__)
@app.route("/")
def health():
    tasks = load_tasks()
    active = len([t for t in tasks if not t["done"]])
    sdk = "new AQ" if USE_NEW_SDK else "old"
    return f"Странник+Спутник жив 🌌🛰️ | {MODEL_NAME} | Задач: {active}", 200

@app.route("/health")
def health2():
    return {"status": "ok", "bot": "strannik-sat", "model": MODEL_NAME, "tasks": len([t for t in load_tasks() if not t["done"]])}, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

# Фоновая проверка задач + ежедневный спутник в 9:00
async def check_due_tasks(application):
    sent_daily = set()  # чтобы не спамить дважды
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
                        await application.bot.send_message(chat_id=t["chat_id"], text=f"⏰ **Напоминание** (#{t['id']}):\n\n{t['text']}\n_На {due.strftime('%d.%m.%Y %H:%M')} {TIMEZONE}_", parse_mode="Markdown")
                        t["done"] = True
                        changed = True
                    except Exception as e:
                        logger.error(f"Task {t['id']}: {e}")
            if changed:
                save_tasks(tasks)

            # Ежедневный рандомный спутник в 9:00 по Берлину
            if now.hour == 9 and now.minute == 0:
                today_key = now.strftime("%Y-%m-%d")
                if today_key not in sent_daily:
                    # Шлем всем у кого есть задачи (или ALLOWED_USER_ID)
                    # Находим все чаты из tasks
                    chats = set([t["chat_id"] for t in tasks] + ([int(ALLOWED_USER_ID)] if ALLOWED_USER_ID and ALLOWED_USER_ID.isdigit() else []))
                    # Если никого - шлем в последний активный чат? Пропускаем
                    for chat_id in chats:
                        try:
                            place = random.choice(RANDOM_SAT_PLACES)
                            url = satellite_image_url(place["lat"], place["lon"], delta=0.05)
                            await application.bot.send_photo(chat_id=chat_id, photo=url, caption=f"🌍 Доброе утро! Случайный спутник: {place['name']} 📍 {place['lat']},{place['lon']} — пожелай хорошего дня!")
                        except Exception as e:
                            logger.error(f"Daily sat for {chat_id}: {e}")
                    sent_daily.add(today_key)
                    # Чистим старые ключи
                    if len(sent_daily) > 7:
                        sent_daily = {today_key}

        except Exception as e:
            logger.error(f"Scheduler: {e}")
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
    # Спутник
    application.add_handler(CommandHandler("sat", sat_cmd))
    application.add_handler(CommandHandler("satellite", sat_cmd))
    application.add_handler(CommandHandler("earth", earth_cmd))
    application.add_handler(CommandHandler("random", earth_cmd))
    application.add_handler(MessageHandler(filters.LOCATION, handle_location))
    # Медиа
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.IMAGE, handle_document_image))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    async def start_scheduler():
        asyncio.create_task(check_due_tasks(application))
    application.post_init = lambda app: start_scheduler()
    logger.info(f"Странник+Спутник запущен! {MODEL_NAME} TZ:{TIMEZONE}")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
