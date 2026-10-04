import uuid
import hashlib
import html
import requests
import json
import logging
import os
import random
import re
import threading
import time
from collections import Counter, defaultdict
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    KYIV = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV = timezone(timedelta(hours=3))
from difflib import get_close_matches
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import telebot
from telebot import types as tg
from google import genai
from google.genai import types as gt
from pypdf import PdfReader
# ===========================================================================
# Настройки - ВСЕ секреты только через переменные окружения (Render -> Environment)
# ===========================================================================
#   TELEGRAM_TOKEN   - токен от @BotFather                         (обязательно)
#   GEMINI_API_KEYS  - ключи Gemini через запятую                  (обязательно)
#   GEMINI_MODELS    - модели через запятую (по умолч. gemini-3.5-flash-lite)
#   ADMIN_ID         - твой Telegram ID (без лимитов, видит статистику)
#   ALLOWED_IDS      - ID через запятую; пусто = доступ у всех
#   DAILY_LIMIT      - запросов в сутки на пользователя (по умолч. 40)
#   TOP_PAGES        - сколько страниц учебника отдавать модели (по умолч. 2)
def _split_env(name):
    raw = os.environ.get(name, "")
    return [x.strip() for x in re.split(r"[,\n;]+", raw) if x.strip()]


TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
API_KEYS = _split_env("GEMINI_API_KEYS") or _split_env("GEMINI_API_KEY")
MODELS = _split_env("GEMINI_MODELS") or ["gemini-3.5-flash-lite"]
ALLOWED_IDS = {int(x) for x in _split_env("ALLOWED_IDS")}
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "40"))
TOP_PAGES = int(os.environ.get("TOP_PAGES", "2"))

print("Длина токена:", len(TELEGRAM_TOKEN), "| двоеточие:", ":" in TELEGRAM_TOKEN)
if ":" not in TELEGRAM_TOKEN:
    raise SystemExit("Не задан TELEGRAM_TOKEN (переменная окружения) или он неверный.")
if not API_KEYS:
    raise SystemExit("Не заданы ключи Gemini: укажи GEMINI_API_KEYS (через запятую).")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(BASE_DIR, "книги")
DATA_DIR = os.path.join(BASE_DIR, "data")      # статистика (на Render не сохраняется)
CACHE_DIR = os.path.join(BASE_DIR, "cache")    # кэш текста учебников (можно коммитить)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_CONTEXT_CHARS = 20000
COOLDOWN_SECONDS = 4
TG_LIMIT = 4000
HISTORY_TURNS = 4
MAX_VOICE_SECONDS = 90

SUBJECTS = {
    "алгебра": "алгебра.pdf",
    "биология": "біологія.pdf",
    "география": "географія.pdf",
    "геометрия": "геометрія.pdf",
    "зарубежка": "зарубежка.pdf",
    "история": "історія.pdf",
    "музыка": "музика.pdf",
    "укрлит": "Укр літ.pdf",
    "укрмов": "укр мова.pdf",
    "физика": "фізика.pdf",
    "химия": "хімія.pdf",
}

ALIASES = {
    "фізика": "физика", "хімія": "химия", "біологія": "биология",
    "географія": "география", "геометрія": "геометрия", "історія": "история",
    "музика": "музыка", "укрмова": "укрмов", "укрліт": "укрлит",
    "зарубіжка": "зарубежка",
}

SYSTEM_BASE = (
    "Ты умный школьный помощник. Опирайся в первую очередь на предоставленные "
    "фрагменты учебника. Если в них нет информации, отвечай по общим знаниям. "
    "Оформляй ответ структурированно, используя ТОЛЬКО HTML-теги Telegram: "
    "<b>жирный</b> для главного ответа, <i>курсив</i>, <code>формулы</code>. "
    "Другие теги и Markdown не используй. Символы < и > в формулах пиши словами "
    "(«меньше», «больше») или как ≤ ≥. Разделяй блоки пустыми строками. "
    "Отвечай на том языке, на котором задан вопрос."
    "Если нужного задания во фрагментах не оказалось — НЕ отказывайся отвечать и "
    "не рассуждай о том, чего нет в учебнике: реши по общим знаниям и правилам темы, "
    "отметив это ОДНОЙ короткой строкой в начале: «📌 Условия в учебнике не нашлось — "
    "решаю по правилам темы. Для точности можно прислать фото задания». Всё, дальше "
    "только решение по обычной структуре. Никаких списков того, что есть на страницах."
)

LENGTH_INSTR = {
    "normal": "",
    "short": "Отвечай максимально коротко: не больше 5-6 предложений.",
    "detail": "Отвечай подробно и развёрнуто, по шагам.",
    "quick": ("РЕЖИМ БЫСТРЫХ ОТВЕТОВ. Дай ТОЛЬКО итоговый ответ: число, слово или вариант. "
              "Без приветствий и пояснений. Для задачи допустима одна строка с расчётом. "
              "На сообщения не по учёбе отвечай одним словом."),
}

MODE_INSTR = {
        "answer": ("Дай готовый точный ответ. Без вводных фраз и без отдельного пояснения сути, "
               "не пиши слово «Суть». Если нужны выкладки, дай их кратко по шагам."),
    "explain": (
        "Не ограничивайся ответом: объясни пошагово, как решать такое задание, "
        "какие правила и формулы используются и почему, чтобы ученик смог "
        "решить похожее сам. В конце дай итоговый ответ."
    ),
    "check": (
        "Ученик прислал своё решение или ответ. Проверь его: скажи, верно ли "
        "оно, найди конкретные ошибки, объясни, как правильно."
    ),
    "summary": (
        "Составь краткую структурированную шпаргалку для повторения по "
        "указанной теме: главные определения, правила, формулы, 1-2 примера. "
        "Без воды."
    ),
    "essay": ("Напиши школьное сочинение на заданную тему для ученика 8 класса: вступление, "
              "2-3 абзаца основной части с аргументами и примерами, вывод. Объём около 200-250 слов, "
              "если не указано иное. Простой живой язык школьника, без канцелярита. "
              "Для украинской мови и литературы пиши по-украински. Только текст сочинения."

    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("pypdf").setLevel(logging.ERROR)
log = logging.getLogger("homework_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
try:
    bot.set_my_commands([
        tg.BotCommand("hw", "Домашка с сайта школы"),
        tg.BotCommand("quiz", "Тест по теме"),
        tg.BotCommand("explain", "Объяснить, как решать"),
        tg.BotCommand("check", "Проверить моё решение"),
        tg.BotCommand("summary", "Шпаргалка по теме"),
        tg.BotCommand("essay", "Написать сочинение"),
        tg.BotCommand("schedule", "Расписание уроков"),
        tg.BotCommand("subject", "Выбрать предмет"),
        tg.BotCommand("reset", "Очистить память диалога"),
        tg.BotCommand("stats", "Моя статистика"),
        tg.BotCommand("top", "Рейтинг класса"),
        tg.BotCommand("cards", "Карточки для запоминания"),
        tg.BotCommand("proofread", "Проверить орфографию"),
        tg.BotCommand("remind", "Напоминание о контрольной"),
        tg.BotCommand("pomodoro", "Таймер учёбы"),
        tg.BotCommand("class", "Выбрать класс"),
        tg.BotCommand("help", "Справка"),
        tg.BotCommand("exam", "Пробная контрольная"),
        tg.BotCommand("formulas", "Тренажёр формул"),
    ])
except Exception:
    log.warning("Не удалось установить меню команд")

@bot.callback_query_handler(func=lambda call: call.data == "delete_msg")
def shredder(call):
    chat_id = call.message.chat.id
    try:
        bot.delete_message(chat_id, call.message.message_id)
    except Exception:
        pass
    reply = call.message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        try:
            bot.delete_message(chat_id, reply.message_id)
        except Exception:
            pass
@bot.callback_query_handler(func=lambda call: call.data.startswith("fb:"))
def on_feedback(call):
    """Кнопки 👍/👎 под ответом."""
    _, verdict, token = call.data.split(":")
    data = FB_CACHE.pop(token, None)
    if verdict == "bad":
        FB_LOG.append(datetime.now(KYIV).date().isoformat())
    try:
        bot.answer_callback_query(call.id, "Спасибо за оценку!" if verdict == "ok" else "Учту, спасибо.")
    except Exception:
        pass
    if verdict == "bad" and data and ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID,
                             f"👎 Плохой ответ (uid {data['uid']})\n\n"
                             f"Вопрос:\n{data['task']}\n\nОтвет:\n{data['answer'][:1500]}")
        except Exception:
            log.exception("Не удалось переслать отзыв админу")
    try:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🗑", callback_data="delete_msg"))
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("similar:"))
def on_similar(call):
    """Кнопка 🔁: придумать похожую задачу на то же правило."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Дневной лимит исчерпан.", show_alert=True)
        return
    data = SIM_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "Кнопка устарела, спроси заново.")
        return
    subject, task = data
    bot.answer_callback_query(call.id, "Придумываю...")
    status = bot.send_message(call.message.chat.id, "Придумываю похожую задачу... ⏱️")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        path = book_path(subject)
        context, _ = find_context(path, task) if path else ("", [])
        prompt = (
            f"Фрагменты учебника:\n{context or '(нет)'}\n\n"
            f"Задача ученика:\n{task}\n\n"
            "Придумай ПОХОЖУЮ задачу на то же правило, но с другими числами и условием. "
            "Сначала приведи условие новой задачи, затем строку «РЕШЕНИЕ:» и краткое решение с ответом."
        )
        answer = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.7)
        if not answer:
            bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer,
                    uid=uid, task=answer[:500], subject=subject)
    except Exception:
        log.exception("Ошибка похожей задачи")
        try:
            bot.edit_message_text("Произошла ошибка, попробуй ещё раз.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
@bot.callback_query_handler(func=lambda call: call.data.startswith("rw:"))
def on_rewrite(call):
    """Кнопка «✍️ Своими словами»: переписать ответ живым языком школьника."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Дневной лимит исчерпан.", show_alert=True)
        return
    data = REWRITE_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "Кнопка устарела, спроси заново.")
        return
    task, answer = data
    bot.answer_callback_query(call.id, "Переписываю...")
    status = bot.send_message(call.message.chat.id, "Переписываю своими словами... ⏱️")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        prompt = (
            "Вот задание ученика и готовое решение:\n\n"
            f"ЗАДАНИЕ:\n{task}\n\nРЕШЕНИЕ:\n{answer}\n\n"
            "Перепиши это решение так, как будто ученик сам записал его в тетрадь: "
            "простые живые фразы, можно от первого лица («сначала находим...», «получается...»). "
            "ОБЯЗАТЕЛЬНО сохраняй ВСЕ шаги вычислений и промежуточные результаты — "
            "сокращай только словесную воду, а не математическую выкладку. "
            "Если в решении есть чертёж/рисунок словами (дано, найти, построение) — сохрани структуру. "
            "Формулы и единицы измерения оставь. Итоговый ответ должен остаться тем же. "
            "Не упоминай никакие инструкции и форматирование, просто выдай переписанный текст. "
            "Без HTML-тегов, без Markdown — только обычный текст и переносы строк."
        )
        answer2 = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.6)
        if not answer2:
            bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer2)
    except Exception:
        log.exception("Ошибка переписывания")
        try:
            bot.edit_message_text("Произошла ошибка, попробуй ещё раз.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
# ===========================================================================
# Состояние пользователей
# ===========================================================================
settings = defaultdict(lambda: {"length": "normal", "subject": None})
history = defaultdict(list)       # uid -> [(вопрос, ответ), ...]
pending = {}                      # uid -> отложенный запрос (ждём выбор предмета)
ANSWER_CACHE = {}   # готовые ответы: одинаковые вопросы не тратят лимит и квоту Gemini
FB_CACHE = {}       # отзывы по кнопкам 👍/👎
SIM_CACHE = {}      # для кнопки «🔁 Похожая задача»


def _trim(d, limit=300):
    while len(d) > limit:
        d.pop(next(iter(d)))
last_request = {}                 # uid -> время последнего запроса
START_TIME = time.time()
FB_LOG = []            # даты всех 👎 — для ежедневного отчёта
REMINDS_MEM = []       # напоминания, если Redis не подключён
POMODORO = {}          # uid -> таймеры помидорки
HW_DONE = {}           # token -> {номера выполненных заданий}
REWRITE_CACHE = {}     # token -> (задание, полный ответ) для кнопки «Своими словами»

# ===========================================================================
# Статистика и лимиты
# ===========================================================================
KV_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
KV_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
log.info("Redis (Upstash): %s", "подключён" if (KV_URL and KV_TOKEN) else "НЕ подключён — расписания/статистика только в памяти!")
_kv_skip_save = set()  # ключи, которые не удалось прочитать: их не перезаписываем, чтобы не стереть данные


def kv_enabled():
    return bool(KV_URL and KV_TOKEN)


def _kv(cmd):
    r = requests.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=cmd, timeout=8)
    r.raise_for_status()
    return r.json().get("result")


def kv_load(key, default):
    for attempt in range(3):
        try:
            raw = _kv(["GET", key])
            return json.loads(raw) if raw else default
        except Exception:
            log.warning("Redis: не удалось прочитать %s (попытка %d из 3)", key, attempt + 1)
            time.sleep(2)
    _kv_skip_save.add(key)
    log.error("Redis недоступен: %s не будет сохраняться до перезапуска бота", key)
    return default


def kv_save(key, value):
    if key in _kv_skip_save:
        return
    try:
        _kv(["SET", key, json.dumps(value, ensure_ascii=False)])
    except Exception:
        log.exception("Redis: не удалось сохранить %s", key)


STATS_FILE = os.path.join(DATA_DIR, "stats.json")
_stats_lock = threading.Lock()


def _load_stats():
    if kv_enabled():
        return kv_load("hwbot:stats", {"users": {}})
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


stats = _load_stats()
def register_use(uid, name):
    today = datetime.now(KYIV).date().isoformat()
    with _stats_lock:
        u = stats["users"].setdefault(str(uid), {"name": name, "total": 0, "days": {}})
        u["name"] = name
        u["total"] += 1
        u["days"][today] = u["days"].get(today, 0) + 1
        if kv_enabled():
            kv_save("hwbot:stats", stats)
        else:
            try:
                with open(STATS_FILE, "w", encoding="utf-8") as f:
                    json.dump(stats, f, ensure_ascii=False, indent=1)
            except Exception:
                log.exception("Не удалось сохранить статистику")


def used_today(uid):
    u = stats["users"].get(str(uid))
    return u["days"].get(datetime.now(KYIV).date().isoformat(), 0) if u else 0


def limit_reached(uid):
    return uid != ADMIN_ID and used_today(uid) >= DAILY_LIMIT


# ===========================================================================
# Безопасная отправка ответов (HTML Telegram)
# ===========================================================================
ALLOWED_TAGS = ("b", "i", "u", "code", "pre")


def sanitize_html(text):
    """Оставляет только разрешённые теги, всё остальное экранирует."""
    text = re.sub(r"<(/?)strong>", r"<\1b>", text, flags=re.I)
    text = re.sub(r"<(/?)em>", r"<\1i>", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = html.escape(text, quote=False)
    for t in ALLOWED_TAGS:
        text = text.replace(f"&lt;{t}&gt;", f"<{t}>").replace(f"&lt;/{t}&gt;", f"</{t}>")
    return text


def strip_tags(text):
    return re.sub(r"<[^>]+>", "", html.unescape(text))


def split_message(text, limit=TG_LIMIT):
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def send_answer(chat_id, status_msg, text, uid=None, task=None, subject=None):
    """Сначала пробует HTML; если Telegram не принял разметку - шлёт чистым текстом."""

    markup = InlineKeyboardMarkup()
    if uid:
        token = uuid.uuid4().hex[:8]
        FB_CACHE[token] = {"uid": uid, "task": (task or "")[:500], "answer": text[:2000]}
        _trim(FB_CACHE, 100)
        if subject and task:
            SIM_CACHE[token] = (subject, task[:500])
            _trim(SIM_CACHE, 100)
            markup.add(InlineKeyboardButton("🔁 Похожая задача", callback_data=f"similar:{token}"))
            REWRITE_CACHE[token] = (task[:1500], text[:2500])
            _trim(REWRITE_CACHE, 100)
            markup.add(InlineKeyboardButton("✍️ Своими словами", callback_data=f"rw:{token}"))
        markup.row(InlineKeyboardButton("👍", callback_data=f"fb:ok:{token}"),
                   InlineKeyboardButton("👎", callback_data=f"fb:bad:{token}"))
    markup.add(InlineKeyboardButton("🗑", callback_data="delete_msg"))

    for n, chunk in enumerate(split_message(sanitize_html(text))):
        first = n == 0
        try:
            if first:
                bot.edit_message_text(chunk, chat_id, status_msg.message_id, parse_mode="HTML", reply_markup=markup)
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=markup)
            continue
        except Exception as e:
            log.warning("HTML не прошёл, отправляю без разметки: %s", e)
        plain = strip_tags(chunk)
        try:
            if first:
                bot.edit_message_text(plain, chat_id, status_msg.message_id, reply_markup=markup)
            else:
                bot.send_message(chat_id, plain, reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, plain, reply_markup=markup)

# ===========================================================================
# PDF: загрузка, кэш, поиск нужных страниц
# ===========================================================================
_pages_cache = {}
_index_cache = {}
_load_lock = threading.Lock()


def load_pages(pdf_path):
    """Читает учебник один раз. Кэш на диске привязан к имени файла и его размеру
    (а не к пути и времени изменения), поэтому работает и после деплоя на сервер."""
    if pdf_path in _pages_cache:
        return _pages_cache[pdf_path]
    with _load_lock:
        if pdf_path in _pages_cache:
            return _pages_cache[pdf_path]

        name = os.path.basename(pdf_path)
        cache_file = os.path.join(CACHE_DIR, hashlib.md5(name.encode("utf-8")).hexdigest() + ".json")
        size = os.path.getsize(pdf_path)

        if os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("size") == size:
                    _pages_cache[pdf_path] = tuple(d["pages"])
                    return _pages_cache[pdf_path]
            except Exception:
                log.warning("Кэш повреждён, читаю PDF заново: %s", name)

        pages = []
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                pages.append((page.extract_text() or "").strip())
        except Exception:
            log.exception("Ошибка чтения PDF: %s", pdf_path)

        pages = tuple(pages)
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"size": size, "pages": list(pages)}, f, ensure_ascii=False)
        except Exception:
            log.warning("Не удалось сохранить кэш для %s", name)

        _pages_cache[pdf_path] = pages
        return pages


def stems(text):
    """Грубая основа слова (первые 5 букв) - помогает с падежами."""
    return [w[:5] for w in re.findall(r"[a-zа-яёіїєґ0-9]{3,}", text.lower())]


def page_index(pdf_path):
    if pdf_path not in _index_cache:
        _index_cache[pdf_path] = [Counter(stems(p)) for p in load_pages(pdf_path)]
    return _index_cache[pdf_path]


def score_pages(pdf_path, task, use_numbers=True):
    pages = load_pages(pdf_path)
    index = page_index(pdf_path)
    query = set(stems(task))
    numbers = re.findall(r"\d+(?:[.,]\d+)?", task) if use_numbers else []
    scored = []
    for i, counts in enumerate(index):
        score = sum(min(counts[s], 5) for s in query)
        page = pages[i]
        for n in numbers:
            # "56." или "56)" в начале строки учебника = номер задачи, самый сильный сигнал
            if re.fullmatch(r"\d{1,3}", n) and re.search(r"(?m)^\s*" + n + r"\s*[.)]", page):
                score += 12
            elif n.replace(",", ".") in page:
                score += 8
        if score > 0:
            scored.append((score, i))
    scored.sort(reverse=True)
    return scored


def find_context(pdf_path, task):
    """Возвращает (текст страниц, номера страниц PDF)."""
    pages = load_pages(pdf_path)
    best = sorted(i for sc, i in score_pages(pdf_path, task)[:TOP_PAGES] if sc >= 10)
    chunks, used, total = [], [], 0
    for i in best:
        chunk = f"[Страница {i + 1}]\n{pages[i]}"
        if total + len(chunk) > MAX_CONTEXT_CHARS:
            break
        chunks.append(chunk)
        used.append(i + 1)
        total += len(chunk)
    return "\n\n".join(chunks), used


def auto_detect(task):
    """Определяет предмет по тексту задания."""
    best, best_score = None, 0
    for key, fname in SUBJECTS.items():
        path = os.path.join(BOOKS_DIR, fname)
        if not os.path.exists(path):
            continue
        s = sum(sc for sc, _ in score_pages(path, task, use_numbers=False)[:3])
        if s > best_score:
            best, best_score = key, s
    return best if best_score >= 10 else None


def preload_books():
    """Фоновая загрузка всех учебников при старте."""
    for key, fname in SUBJECTS.items():
        path = os.path.join(BOOKS_DIR, fname)
        if os.path.exists(path):
            t = time.time()
            n = len(load_pages(path))
            log.info("Учебник '%s' готов: %d стр., %.1f сек", key, n, time.time() - t)
        else:
            log.warning("Файл не найден: %s", path)


# ===========================================================================
# Gemini (несколько ключей с ротацией)
# ===========================================================================
_key_blocked = {}   # ключ -> время, до которого он считается исчерпанным
_key_lock = threading.Lock()


def ordered_keys():
    """Свободные ключи в случайном порядке (нагрузка делится поровну), затем заблокированные."""
    now = time.time()
    with _key_lock:
        ready = [k for k in API_KEYS if _key_blocked.get(k, 0) <= now]
    blocked = [k for k in API_KEYS if k not in ready]
    random.shuffle(ready)
    return ready + blocked


def call_gemini(contents, system_instruction=None, json_mode=False, temperature=0.3):
    cfg = {"temperature": temperature}
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if json_mode:
        cfg["response_mime_type"] = "application/json"

    last_error = None
    for api_key in ordered_keys():
        idx = API_KEYS.index(api_key) + 1
        try:
            gclient = genai.Client(api_key=api_key)
        except Exception as e:
            log.warning("Ключ #%d: ошибка инициализации: %s", idx, e)
            continue

        for model in MODELS:
            for attempt in range(2):
                t0 = time.time()
                try:
                    r = gclient.models.generate_content(
                        model=model, contents=contents,
                        config=gt.GenerateContentConfig(**cfg),
                    )
                    log.info("Ключ #%d, %s ответила за %.1f сек", idx, model, time.time() - t0)
                    return r.text or None
                except Exception as e:
                    last_error = e
                    err = str(e)
                    log.warning("Ключ #%d | %s: %s", idx, model, err[:200])
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        with _key_lock:
                            _key_blocked[api_key] = time.time() + 60   # минуту не трогаем этот ключ
                        break
                    if any(code in err for code in ("400", "403", "404")):
                        break   # неверный ключ/модель - повторять бессмысленно
                    time.sleep(1)

    log.error("Все ключи и модели недоступны: %s", last_error)
    return None


def extract_task(media):
    """Достаёт текст задания из фото или голосового сообщения."""
    return call_gemini(
        [
            "Если это изображение, дословно перепиши текст задания (формулы "
            "символами Unicode). Если это аудио, расшифруй речь. Верни только "
            "текст без комментариев.",
            *media,
        ],
        temperature=0.0,
    )


def history_text(uid):
    return "\n".join(f"Ученик: {q[:600]}\nПомощник: {a[:600]}" for q, a in history[uid][-HISTORY_TURNS:])


def build_prompt(mode, context, task, uid):
    hist = history_text(uid)
    mode_text = MODE_INSTR[mode]
    if settings[uid]["length"] == "quick" and mode == "answer":
        mode_text = "Дай только итоговый ответ, без пояснений."
    return (
        f"Фрагменты учебника:\n{context or '(подходящих фрагментов не найдено)'}\n\n"
        + (f"Предыдущий диалог:\n{hist}\n\n" if hist else "")
        + f"Твоя задача: {mode_text}\n\nСообщение ученика: {task}"
    )


def build_system(uid):
    return (SYSTEM_BASE + " " + LENGTH_INSTR[settings[uid]["length"]]).strip()


# ===========================================================================
# Вспомогательные функции
# ===========================================================================
def resolve_subject(word):
    word = word.lower().strip(".,:;!?")
    word = ALIASES.get(word, word)
    if word in SUBJECTS:
        return word
    if len(word) >= 4:
        close = get_close_matches(word, list(SUBJECTS) + list(ALIASES), n=1, cutoff=0.82)
        if close:
            c = ALIASES.get(close[0], close[0])
            return c if c in SUBJECTS else None
    return None


def split_subject(text):
    """'физика задача 3.5' -> ('физика', 'задача 3.5')"""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None, ""
    key = resolve_subject(parts[0])
    if key:
        return key, parts[1].strip() if len(parts) > 1 else ""
    return None, text.strip()


def subjects_keyboard():
    kb = tg.InlineKeyboardMarkup(row_width=3)
    kb.add(*[tg.InlineKeyboardButton(s.capitalize(), callback_data=f"subj:{s}") for s in SUBJECTS])
    kb.add(tg.InlineKeyboardButton("🔍 Определить автоматически", callback_data="subj:auto"))
    return kb


def gate(message, cooldown=True):
    """Проверка доступа и антиспама."""
    uid = message.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.reply_to(message, f"У тебя нет доступа к боту. Твой ID: {uid}")
        return False
    if cooldown:
        now = time.time()
        if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
            bot.reply_to(message, "Подожди пару секунд перед следующим запросом.")
            return False
        last_request[uid] = now
    return True


def book_path(subject):
    path = os.path.join(BOOKS_DIR, SUBJECTS[subject])
    return path if os.path.exists(path) else None


# ===========================================================================
# Основной конвейер
# ===========================================================================

    # Словарь с твоим расписанием 8-Б

    # ===========================================================================
    # Расписание и обработчики домашки (ВЫНЕСЕНО ИЗ PROCESS НАВЕРХ)
    # ===========================================================================

# Классы школы: имя -> ID на сайте sch3.edu.vn.ua
CLASSES = {
"10-А": "31",
"10-Б": "33",
"11-А": "34",
"11-Б": "28",
"1-А": "1",
"1-Б": "2",
"1-В": "5",
"2-А": "6",
"2-Б": "7",
"2-В": "8",
"3-А": "9",
"3-Б": "10",
"3-В": "11",
"4-А": "12",
"4-Б": "13",
"4-В": "14",
"5-А": "15",
"5-Б": "16",
"5-В": "17",
"6-А": "18",
"6-Б": "19",
"6-В": "29",
"7-А": "20",
"7-Б": "21",
"7-В": "22",
"8-А": "23",
"8-Б": "24",
"8-В": "30",
"9-А": "25",
"9-Б": "26",
"9-В": "32",
}
DEFAULT_CLASS = "8-Б"
SCHEDULES = {}   # все расписания теперь через /setschedule (Redis)
# у классов без расписания бот покажет все свежие задания с сайта
@bot.message_handler(commands=['hw'])
def hw_menu(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📅 На завтра", callback_data="hw_auto"))
    markup.row(InlineKeyboardButton("ПН", callback_data="hw_0"),
               InlineKeyboardButton("ВТ", callback_data="hw_1"),
               InlineKeyboardButton("СР", callback_data="hw_2"))
    markup.row(InlineKeyboardButton("ЧТ", callback_data="hw_3"),
               InlineKeyboardButton("ПТ", callback_data="hw_4"))
    markup.add(InlineKeyboardButton("🗑 Закрыть", callback_data="delete_msg"))
    bot.send_message(message.chat.id, "На какой день собрать домашку?", reply_markup=markup)

@bot.message_handler(commands=["schedule"])
def cmd_schedule(message):
    if not gate(message, cooldown=False):
        return
    class_name = user_class(message.chat.id)
    sched = SCHEDULES.get(class_name)
    if not sched:
        bot.reply_to(message, f"Расписание для {html.escape(class_name)} пока не добавлено, "
                              "но домашка работает: /hw")
        return
    names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]
    text = f"📅 <b>Расписание {html.escape(class_name)}</b>\n\n" + "\n\n".join(
        f"<b>{names[int(d)]}</b>\n"
        + "\n".join(f"{i}. {html.escape(s)}" for i, s in enumerate(lessons, 1))
        for d, lessons in sched.items()
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


from concurrent.futures import ThreadPoolExecutor

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
# ===========================================================================
# Автоматизация классов: автопоиск с сайта, /addclass, расписания из Redis
# ===========================================================================
_EXTRA_CLASSES_MEM = {}
_SCHED_MEM = {}


def discover_classes():
    """Тянет список классов (имя -> ID) с выпадающего списка сайта школы."""
    try:
        r = requests.get("https://sch3.edu.vn.ua/disted", headers=HEADERS, timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        found = {}
        for sel in soup.find_all('select'):
            for o in sel.find_all('option'):
                val = (o.get('value') or '').strip()
                name = canon_class(o.text.strip())
                if val.isdigit() and re.fullmatch(r"\d{1,2}-?[А-ЯІЇЄҐ]{1,2}", name):
                    found[name] = val
        log.info("Сайт: найдено классов: %d", len(found))
        return found
    except Exception:
        log.warning("Сайт: не удалось автообнаружить классы — работает ручной CLASSES")
        return {}


def site_subjects(class_name):
    """Названия предметов, которые встречаются на сайте у этого класса."""
    class_id = CLASSES.get(class_name)
    if not class_id:
        return []
    try:
        response = requests.post("https://sch3.edu.vn.ua/disted",
                                 data={'class': class_id}, headers=HEADERS, timeout=10)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        subs = []
        for row in soup.find_all('tr'):
            cols = row.find_all('td')
            if len(cols) >= 5:
                s = cols[3].text.strip()
                if s and s not in subs:
                    subs.append(s)
        return subs
    except Exception:
        log.exception("Не удалось получить предметы с сайта для %s", class_name)
        return []


def _merge_classes_and_schedules():
    for k, v in discover_classes().items():
        CLASSES.setdefault(k, v)          # ручные записи кода не перебиваем
    extras = kv_load("hwbot:extra_classes", {}) if kv_enabled() else _EXTRA_CLASSES_MEM
    for k, v in (extras or {}).items():
        CLASSES[canon_class(k)] = v
    saved = kv_load("hwbot:schedules", {}) if kv_enabled() else _SCHED_MEM
    for k, v in (saved or {}).items():
        if isinstance(v, dict) and v:
            SCHEDULES[canon_class(k)] = v


threading.Thread(target=_merge_classes_and_schedules, daemon=True).start()


def fetch_task_text(url):
    """Достаёт текст после заголовка 'Домашнє завдання' до новостей/подвала сайта."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'html.parser')
        for t in soup(['script', 'style']):
            t.decompose()
        page = soup.get_text('\n', strip=True)

        m = re.search(r'Домашнє\s+завдання', page, re.I)
        if not m:
            return ""
        text = page[m.end():]

        # обрезаем по первому из маркеров: боковая колонка новостей или подвал сайта
        cut = len(text)
        for marker in (r'ОСТАННІ\s+НОВИНИ', r'Про\s+нас', r'Комунальний\s+заклад'):
            s = re.search(marker, text, re.I)
            if s:
                cut = min(cut, s.start())

        return text[:cut].strip()
    except Exception:
        log.exception("Не удалось открыть %s", url)
        return ""

def is_future(lesson_date):
    """True, если дата урока (дд.мм.гггг) позже сегодняшней по Киеву."""
    try:
        d = datetime.strptime(lesson_date.strip(), "%d.%m.%Y").date()
        return d > datetime.now(KYIV).date()
    except ValueError:
        return False  # если дату не разобрать, задание не скрываем
DAY_NAMES = ["понедельник", "вторник", "среду", "четверг", "пятницу"]


def next_school_day():
    """Индекс следующего учебного дня (0=ПН ... 4=ПТ) по Киеву. Выходные пропускает."""
    d = datetime.now(KYIV).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.weekday()

# Название предмета в расписании (укр.) -> ключ учебника в SUBJECTS.
# Если предмета тут нет (англ., информатика, физкультура...), решаем без учебника.
SUBJECT_KEYS = {
    "Алгебра": "алгебра",
    "Біологія": "биология",
    "Географія": "география",
    "Геометрія": "геометрия",
    "Зарубіжна література": "зарубежка",
    "Всесвітня історія": "история",
    "Історія України": "история",
    "Мистецтво": "музыка",
    "Українська література": "укрлит",
    "Українська мова": "укрмов",
    "Фізика і астрономія": "физика",
    "Хімія": "химия",
}
# Свои учебники по классам. Класса нет в словаре или словарь пустой = решаем без учебников.
SUBJECT_KEYS_BY_CLASS = {
    "6-В": {},   # пока нет учебников за 6 класс - всё по общим знаниям
}
# Классы без учебников: по предметам не из mapping бот просит фото задания
CLASSES_WITHOUT_BOOKS = {k for k, v in SUBJECT_KEYS_BY_CLASS.items() if not v}
# Латинские буквы, похожие на кириллицу (A B C E H K M O P T X), приводим к кириллице.
# Защита от невидимого бага: "6-В" в CLASSES и SCHEDULES набраны разными алфавитами.
_HOMO = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")


def canon_class(name):
    """Каноничное имя класса: верхний регистр, кириллица, без дефисов и пробелов. '6-В' == '6В'."""
    return name.upper().translate(_HOMO).replace("-", "").replace("–", "").replace(" ", "").strip()


CLASSES = {canon_class(k): v for k, v in CLASSES.items()}
SCHEDULES = {canon_class(k): v for k, v in SCHEDULES.items()}
SUBJECT_KEYS_BY_CLASS = {canon_class(k): v for k, v in SUBJECT_KEYS_BY_CLASS.items()}
DEFAULT_CLASS = canon_class(DEFAULT_CLASS)
CLASSES_WITHOUT_BOOKS = {k for k, v in SUBJECT_KEYS_BY_CLASS.items() if not v}
def _validate_classes():
    for _name in SCHEDULES:
        if _name not in CLASSES:
            log.warning("SCHEDULES: класс %r отсутствует в CLASSES - проверь буквы (кириллица/латиница)!", _name)
    for _name in SUBJECT_KEYS_BY_CLASS:
        if _name not in CLASSES:
            log.warning("SUBJECT_KEYS_BY_CLASS: класс %r отсутствует в CLASSES!", _name)


_validate_classes()
threading.Thread(target=_merge_classes_and_schedules, daemon=True).start()

HW_CACHE = {}  # token -> [(предмет, текст задания), ...]; живёт в памяти, до перезапуска бота

def _hw_kb(token, items, delete_data="delete_msg"):
    done = HW_DONE.setdefault(token, set())
    markup = InlineKeyboardMarkup()
    for i, (subject, text) in enumerate(items):
        if text:
            label = subject if len(subject) <= 18 else subject[:17] + "…"
            markup.row(
                InlineKeyboardButton(f"✏️ {label}", callback_data=f"solve:{token}:{i}"),
                InlineKeyboardButton("☑️" if i in done else "✅", callback_data=f"hwdone:{token}:{i}"),
            )
    markup.add(InlineKeyboardButton("🗑", callback_data=delete_data))
    return markup


def hw_markup(items, delete_data="delete_msg"):
    """Кнопки '✏️ Предмет' (решить), '✅' (выполнено) и кнопка удаления."""
    token = uuid.uuid4().hex[:8]
    HW_CACHE[token] = items
    while len(HW_CACHE) > 50:  # храним последние 50 списков
        HW_CACHE.pop(next(iter(HW_CACHE)))
    return _hw_kb(token, items, delete_data)

def build_hw_text(day_index, class_name=None):
    """Возвращает (HTML-текст домашки, список [(предмет, текст)]). Если заданий нет: (None, [])."""
    class_name = class_name or DEFAULT_CLASS
    class_id = CLASSES.get(class_name)
    if not class_id:
        return None, []
    subjects_today = SCHEDULES.get(class_name, {}).get(str(day_index))

    url = "https://sch3.edu.vn.ua/disted"
    response = requests.post(url, data={'class': class_id}, headers=HEADERS, timeout=10)
    response.encoding = 'utf-8'
    soup = BeautifulSoup(response.text, 'html.parser')

    latest_tasks = {}
    for row in soup.find_all('tr'):
        cols = row.find_all('td')
        if len(cols) >= 5:
            lesson_date = cols[0].text.strip()
            subject = cols[3].text.strip()
            if (subjects_today is None or subject in subjects_today) and subject not in latest_tasks and not is_future(
                    lesson_date):
                a_tag = row.find('a')
                if a_tag and 'href' in a_tag.attrs:
                    href = a_tag['href']
                    task_link = href if href.startswith("http") else "https://sch3.edu.vn.ua" + href
                else:
                    task_link = "https://sch3.edu.vn.ua/disted"
                latest_tasks[subject] = (lesson_date, task_link)

    if not latest_tasks:
        return None, []

    subs_list = ([s for s in subjects_today if s in latest_tasks]
                 if subjects_today is not None else list(latest_tasks))
    with ThreadPoolExecutor(max_workers=6) as ex:
        texts = list(ex.map(lambda s: fetch_task_text(latest_tasks[s][1]), subs_list))

    hw_text = f"📚 <b>Домашка {html.escape(class_name)} на {DAY_NAMES[int(day_index)]}:</b>\n\n"
    items = []
    for subject, text in zip(subs_list, texts):
        lesson_date, task_link = latest_tasks[subject]
        hw_text += f"🔹 <b>{html.escape(subject)}</b> ({html.escape(lesson_date)})\n"
        if text:
            hw_text += html.escape(text[:400]) + "\n"
        else:
            hw_text += "(текст не найден)\n"
        hw_text += f'<a href="{html.escape(task_link, quote=True)}">🔗</a>\n\n'
        items.append((subject, text))
    return hw_text, items

@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_"))
def parse_smart_hw(call):
    key = call.data.split("_")[1]
    day_index = str(next_school_day()) if key == "auto" else key
    chat_id = call.message.chat.id
    msg_id = call.message.message_id

    try:
        bot.answer_callback_query(call.id)
    except Exception:
        pass

    bot.edit_message_text("Сканирую сайт школы... 🕵️‍♂️", chat_id, msg_id)

    try:
        hw_text, items = build_hw_text(day_index, user_class(chat_id))
        if not hw_text:
            bot.edit_message_text("На этот день свежих заданий на сайте пока нет.", chat_id, msg_id)
            return

        markup = hw_markup(items)
        chunks = split_message(hw_text)
        for i, chunk in enumerate(chunks):
            m = markup if i == len(chunks) - 1 else None  # кнопки только под последней частью
            if i == 0:
                bot.edit_message_text(chunk, chat_id, msg_id, parse_mode="HTML",
                                      reply_markup=m, disable_web_page_preview=True)
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML",
                                 reply_markup=m, disable_web_page_preview=True)
    except Exception as e:
        log.exception("Ошибка в /hw")
        bot.edit_message_text(f"Сайт недоступен. Ошибка: {e}", chat_id, msg_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("solve:"))
def on_solve(call):
    """Кнопка '✏️ Предмет' под домашкой: отправляет задание на решение."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "Нет доступа.", show_alert=True)
        return
    try:
        _, token, idx = call.data.split(":")
        subject_ua, task_text = HW_CACHE[token][int(idx)]
    except Exception:
        bot.answer_callback_query(call.id, "Список устарел, открой /hw заново.", show_alert=True)
        return
    now = time.time()
    if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
        bot.answer_callback_query(call.id, "Подожди пару секунд.")
        return
    last_request[uid] = now

    keys = SUBJECT_KEYS_BY_CLASS.get(
        user_class(call.message.chat.id), SUBJECT_KEYS)
    subject = keys.get(subject_ua, "none")
    if subject == "none" and user_class(call.message.chat.id) in CLASSES_WITHOUT_BOOKS:
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            f"📸 По «{subject_ua}» у меня пока нет учебника для этого класса.\n"
            "Пришли фото задания следующим сообщением — решу и объясню "
            "(можно и просто текстом написать).")
        return
    bot.answer_callback_query(call.id, "Решаю…")
    task = f"{subject_ua}. Домашнее задание: {task_text[:1500]}"
    # Если в задании только номера без текста - предупредим, что условия могут не найтись
    if re.search(r"[№N]\s*№?\s*\d+|\d{1,3}\s*[,.-]\s*\d{1,3}", task_text) and len(task_text) < 120:
        bot.send_message(call.message.chat.id,
                         f"ℹ️ В задании только номера ({subject_ua}). Если в ответе условия не хватит — "
                         "пришли фото страницы учебника с этими номерами, решу точно.")
    process(call.message.chat.id, call.message.message_id, uid,
            call.from_user.first_name, "answer", subject, task, [])

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwdone:"))
def on_hw_done(call):
    """Кнопка ✅ под домашкой: отметить задание выполненным."""
    try:
        _, token, idx = call.data.split(":")
        idx = int(idx)
        items = HW_CACHE[token]
    except Exception:
        bot.answer_callback_query(call.id, "Список устарел, открой /hw заново.", show_alert=True)
        return
    done = HW_DONE.setdefault(token, set())
    if idx in done:
        done.discard(idx)
        msg = "Вернул в список."
    else:
        done.add(idx)
        left = sum(1 for i, (s, t) in enumerate(items) if t and i not in done)
        msg = "Всё выполнено! 🎉" if left == 0 else f"Молодец! 💪 Осталось: {left}"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=_hw_kb(token, items))
    except Exception:
        pass
    bot.answer_callback_query(call.id, msg)


# ===========================================================================
# Ежедневная рассылка домашки
# ===========================================================================
SEND_HOUR, SEND_MINUTE = 16, 0          # время рассылки по Киеву
SUBS_FILE = os.path.join(DATA_DIR, "subs.json")
_subs_lock = threading.Lock()


def _load_subs():
    if kv_enabled():
        return {int(x) for x in kv_load("hwbot:subs", [])}
    try:
        with open(SUBS_FILE, encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()


subs = _load_subs()
# Выбранный класс для каждого чата (переживает перезапуск через Redis)
CLASS_KEY = "hwbot:classes"


def _load_classes():
    if kv_enabled():
        raw = kv_load(CLASS_KEY, {})
        try:
            return {int(k): v for k, v in raw.items()}
        except Exception:
            return {}
    return {}


USER_CLASS = _load_classes()

def user_class(chat_id):
    """Класс чата с защитой от удалённых/переименованных классов."""
    c = canon_class(USER_CLASS.get(chat_id, DEFAULT_CLASS))
    return c if c in CLASSES else DEFAULT_CLASS
def class_save():
    if kv_enabled():
        kv_save(CLASS_KEY, {str(k): v for k, v in USER_CLASS.items()})


def _save_subs():
    if kv_enabled():
        kv_save("hwbot:subs", sorted(subs))
        return
    try:
        with open(SUBS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(subs), f)
    except Exception:
        log.exception("Не удалось сохранить подписчиков")


def broadcast_targets():
    """Чаты для рассылки: переменная BROADCAST_IDS + подписавшиеся через /subscribe.
    Если никого нет - шлём админу."""
    ids = {int(x) for x in _split_env("BROADCAST_IDS")} | set(subs)
    if not ids and ADMIN_ID:
        ids = {ADMIN_ID}
    return ids


@bot.callback_query_handler(func=lambda call: call.data == "del_one")
def delete_one(call):
    """Удаляет только само сообщение (для рассылки, где предыдущее сообщение чужое)."""
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass

def class_keyboard():
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(*[InlineKeyboardButton(name, callback_data=f"setclass:{name}") for name in CLASSES])
    return kb


@bot.callback_query_handler(func=lambda call: call.data.startswith("setclass:"))
def on_setclass(call):
    name = call.data.split(":", 1)[1]
    if name not in CLASSES:
        bot.answer_callback_query(call.id, "Такого класса нет.", show_alert=True)
        return
    USER_CLASS[call.message.chat.id] = name
    class_save()
    bot.answer_callback_query(call.id, f"Класс: {name}")
    try:
        bot.edit_message_text(f"✅ Класс выбран: <b>{html.escape(name)}</b>\n"
                              "Домашка: /hw · рассылка: /subscribe · сменить: /class",
                              call.message.chat.id, call.message.message_id, parse_mode="HTML")
    except Exception:
        pass


@bot.message_handler(commands=["class"])
def cmd_class(message):
    if not gate(message, cooldown=False):
        return
    cur = user_class(message.chat.id)
    bot.send_message(message.chat.id,
                     f"Сейчас выбран: <b>{html.escape(cur)}</b>. Выбери свой класс:",
                     parse_mode="HTML", reply_markup=class_keyboard())


@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(message):
    if not gate(message, cooldown=False):
        return
    with _subs_lock:
        subs.add(message.chat.id)
        _save_subs()
    bot.reply_to(message, f"Готово! Каждый учебный день в {SEND_HOUR}:{SEND_MINUTE:02d} "
                          "буду присылать домашку на следующий день. Отписаться: /unsubscribe")


@bot.message_handler(commands=["unsubscribe"])
def cmd_unsubscribe(message):
    if not gate(message, cooldown=False):
        return
    with _subs_lock:
        subs.discard(message.chat.id)
        _save_subs()
    bot.reply_to(message, "Рассылка отключена.")


RETRY_ATTEMPTS = 4          # сколько раз пробуем достучаться до сайта школы
RETRY_DELAY = 10 * 60       # пауза между попытками, секунд


def send_daily():
    day_index = next_school_day()
    built = {}   # класс -> (hw_text, items, site_ok); сайт дёргаем один раз на класс
    for chat_id in broadcast_targets():
        cname = user_class(chat_id)
        if cname not in built:
            hw_text, items, site_ok = None, [], False
            for attempt in range(1, RETRY_ATTEMPTS + 1):
                try:
                    hw_text, items = build_hw_text(day_index, cname)
                    site_ok = True
                    break
                except Exception:
                    log.exception("Рассылка: сайт недоступен (попытка %d из %d)", attempt, RETRY_ATTEMPTS)
                    if attempt < RETRY_ATTEMPTS:
                        time.sleep(RETRY_DELAY)
            built[cname] = (hw_text, items, site_ok)
        hw_text, items, site_ok = built[cname]
        if not site_ok:
            try:
                bot.send_message(chat_id, "⚠️ Сайт школы сейчас недоступен, домашку получить не удалось. "
                                          "Попробуй позже командой /hw.")
            except Exception:
                log.exception("Рассылка: не удалось отправить уведомление в чат %s", chat_id)
            continue
        if not hw_text:
            log.info("Рассылка: чат %s (класс %s): заданий нет", chat_id, cname)
            continue
        chunks = split_message(hw_text)
        try:
            markup = hw_markup(items, "del_one")
            for i, chunk in enumerate(chunks):
                bot.send_message(chat_id, chunk, parse_mode="HTML",
                                 reply_markup=markup if i == len(chunks) - 1 else None,
                                 disable_web_page_preview=True)
        except Exception:
            log.exception("Рассылка: не удалось отправить в чат %s", chat_id)




def broadcast_loop():
    last_sent = None
    start = SEND_HOUR * 60 + SEND_MINUTE
    while True:
        try:
            now = datetime.now(KYIV)
            minutes = now.hour * 60 + now.minute
            # окно 10 минут: если бот перезапустится позже, старую рассылку не шлём
            if now.weekday() < 5 and start <= minutes < start + 10 and last_sent != now.date():
                last_sent = now.date()
                send_daily()
        except Exception:
            log.exception("Ошибка планировщика")
        time.sleep(20)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subj:"))
def on_subject_pick(call):
    """Нажатие на кнопку выбора предмета (раньше обработчика не было)."""
    uid = call.from_user.id
    req = pending.pop(uid, None)
    if not req:
        bot.answer_callback_query(call.id, "Запрос устарел, отправь задание заново.")
        return
    subject = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    process(call.message.chat.id, req["reply_to"], uid, call.from_user.first_name,
            req["mode"], subject, req["task"], req["media"])


def dispatch(message, mode, subject, task, media):
    uid = message.from_user.id
    process(message.chat.id, message.message_id, uid, message.from_user.first_name,
            mode, subject or "auto", task, media)


def process(chat_id, reply_to, uid, name, mode, subject, task, media):
    if limit_reached(uid):
        bot.send_message(chat_id, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан. Приходи завтра!",
                         reply_to_message_id=reply_to)
        return

    status = bot.send_message(chat_id, "Ищу ответ в учебнике... ⏱️", reply_to_message_id=reply_to)
    bot.send_chat_action(chat_id, "typing")

    try:
        if media:
            extracted = extract_task(media)
            if extracted:
                task = f"{task}\n{extracted}".strip()
            elif not task:
                bot.edit_message_text("Не удалось разобрать фото или голосовое. Попробуй ещё раз.",
                                      chat_id, status.message_id)
                return

        auto_note = ""
        general = False  # True = отвечаем без учебника
        if subject == "none":
            subject, general = None, True
        elif subject == "auto":
            if user_class(chat_id) in CLASSES_WITHOUT_BOOKS:
                general = True  # у класса нет наших учебников - отвечаем без них
            else:
                subject = auto_detect(task)
                if subject:
                    auto_note = f"📚 Предмет: {subject}\n\n"
                else:
                    general = True

        if general:
            context, pages = "", []
        else:
            path = book_path(subject)
            if not path:
                bot.edit_message_text(f"Файл учебника «{SUBJECTS[subject]}» не найден в папке «книги».",
                                      chat_id, status.message_id)
                return
            if not any(load_pages(path)):
                bot.edit_message_text("Не удалось извлечь текст из PDF (возможно, это скан без текстового слоя).",
                                      chat_id, status.message_id)
                return
            context, pages = find_context(path, task)
        prompt = build_prompt(mode, context, task, uid)
        cache_key = None
        if not media:
            cache_key = hashlib.md5(
                f"{subject}|{mode}|{settings[uid]['length']}|{task}".encode("utf-8")
            ).hexdigest()
            if cache_key in ANSWER_CACHE:
                send_answer(chat_id, status, ANSWER_CACHE[cache_key],
                            uid=uid, task=task,
                            subject=subject if subject and subject != "none" else None)
                return
        answer = call_gemini([prompt, *media], system_instruction=build_system(uid))
        if not answer:
            bot.edit_message_text("Сейчас ИИ перегружен или недоступен. Попробуй ещё раз через минуту.",
                                  chat_id, status.message_id)
            return

        register_use(uid, name)
        if subject:
            settings[uid]["subject"] = subject
        history[uid].append((task, answer))
        history[uid] = history[uid][-HISTORY_TURNS:]

        quick = settings[uid]["length"] == "quick"
        footer = f"\n\n📖 Использованы страницы PDF: {', '.join(map(str, pages))}" if pages and not quick else ""
        full_text = auto_note + answer + footer
        if cache_key:
            ANSWER_CACHE[cache_key] = full_text
            _trim(ANSWER_CACHE)
        send_answer(chat_id, status, full_text, uid=uid, task=task,
                    subject=subject if subject and subject != "none" else None)

    except Exception:
        log.exception("Ошибка обработки запроса")
        try:
            bot.edit_message_text("Произошла ошибка. Попробуй ещё раз.", chat_id, status.message_id)
        except Exception:
            pass
def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)
# ===========================================================================
# /setschedule - расписание класса без правок кода (хранится в Redis)
# ===========================================================================
@bot.message_handler(commands=["setschedule"])
def cmd_setschedule(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Команда только для админа.")
        return
    parts = message.text.split(maxsplit=2)
    cname_raw = parts[1] if len(parts) >= 2 else ""
    text = parts[2].strip() if len(parts) >= 3 else ""
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    if not cname_raw:
        bot.reply_to(message, "Так: /setschedule 6-В <текст расписания>\n"
                              "Или reply на сообщение с расписанием: /setschedule 6-В")
        return
    if cname_raw.lower() in ("список", "list"):
        mode = "Redis — переживёт перезапуск" if kv_enabled() else "ПАМЯТЬ (Redis не подключён — пропадёт при перезапуске!)"
        bot.reply_to(message, f"Залито расписаний: {len(SCHEDULES)} · хранилище: {mode}\n"
                              + (", ".join(sorted(SCHEDULES)) or "пусто"))
        return
    cname = canon_class(cname_raw)
    if cname not in CLASSES:
        bot.reply_to(message, f"Класса {html.escape(cname)} нет. Список: /class. "
                              f"Добавить: /addclass 6-Г <ID>")
        return
    if text.lower() in ("сброс", "удалить", "delete"):
        SCHEDULES.pop(cname, None)
        if kv_enabled():
            store = kv_load("hwbot:schedules", {})
            store.pop(cname, None)
            kv_save("hwbot:schedules", store)
        bot.reply_to(message, f"Расписание {cname} удалено — класс перейдёт в режим "
                              "«все свежие задания с сайта».")
        return
    if not text:
        bot.reply_to(message, "Не вижу текст расписания: пришли его в команде или reply на сообщение.")
        return
    status = bot.reply_to(message, "Разбираю расписание и сверяю с сайтом... ⏱️")
    site_list = site_subjects(cname)
    prompt = (
        "Ниже — расписание уроков, присланное текстом (может быть неаккуратным), "
        "и точный список названий предметов со школьного сайта.\n"
        "Составь расписание по дням ПН-ПТ. Каждому предмету подбери НАИБОЛЕЕ БЛИЗКОЕ "
        "название из списка сайта (сокращения раскрывай: «Фіз. виховання» = «Фізична "
        "культура», «Укр літ.» = «Українська література»). Если предмет не похож ни на "
        "один из списка — оставь как есть. Повторяющийся в один день предмет пиши один раз. "
        'Верни ТОЛЬКО JSON: {"0": ["предмет", ...], "1": [...], "2": [...], "3": [...], "4": [...]}, '
        "где 0=ПН, 1=ВТ, 2=СР, 3=ЧТ, 4=ПТ.\n\n"
        f"Названия с сайта: {'; '.join(site_list) if site_list else '(сайт недоступен)'}\n\n"
        f"Расписание:\n{text[:3000]}"
    )
    raw = call_gemini(prompt, json_mode=True, temperature=0.0)
    if not raw:
        bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                              message.chat.id, status.message_id)
        return
    try:
        sched_raw = parse_json(raw)
    except Exception:
        bot.edit_message_text("Не получилось разобрать расписание, пришли текст аккуратнее.",
                              message.chat.id, status.message_id)
        return
    sched = {}
    for i in range(5):
        day = sched_raw.get(str(i))
        if isinstance(day, list):
            items = [str(s).strip()[:40] for s in day if str(s).strip()]
            if items:
                sched[str(i)] = items[:12]
    if not sched:
        bot.edit_message_text("В тексте не нашлось предметов по дням. Проверь и пришли ещё раз.",
                              message.chat.id, status.message_id)
        return
    SCHEDULES[cname] = sched
    if kv_enabled():
        store = kv_load("hwbot:schedules", {})
        store[cname] = sched
        kv_save("hwbot:schedules", store)
    else:
        _SCHED_MEM[cname] = sched
    names = ["ПН", "ВТ", "СР", "ЧТ", "ПТ"]
    show = "\n\n".join(f"<b>{names[i]}</b>: " + (", ".join(sched.get(str(i), [])) or "—")
                       for i in range(5))
    warn = ("\n\n⚠️ Сайт был недоступен — проверь названия: если они не как на сайте, "
            "фильтр «на завтра» может работать неточно.") if not site_list else ""
    how = "в Redis — переживёт перезапуск" if kv_enabled() else "в ПАМЯТЬ — Redis не подключён, пропадёт при перезапуске!"
    bot.edit_message_text(f"✅ Расписание {html.escape(cname)} сохранено {how}:\n\n{show}{warn}",
                          message.chat.id, status.message_id, parse_mode="HTML")


@bot.message_handler(commands=["addclass"])
def cmd_addclass(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "Команда только для админа.")
        return
    parts = message.text.split()
    if len(parts) != 3 or not parts[2].isdigit():
        bot.reply_to(message, "Так: /addclass 6-Г 27\n(ID — внутренний номер класса на сайте)")
        return
    name = canon_class(parts[1])
    CLASSES[name] = parts[2]
    if kv_enabled():
        extras = kv_load("hwbot:extra_classes", {})
        extras[name] = parts[2]
        kv_save("hwbot:extra_classes", extras)
    bot.reply_to(message, f"✅ Класс {name} добавлен (ID {parts[2]}). "
                          "Дальше: /setschedule для расписания, /class — для выбора.")


def run_quiz(message, subject, topic):
    uid = message.from_user.id
    chat_id = message.chat.id
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    path = book_path(subject)
    if not path:
        bot.reply_to(message, f"Файл учебника «{SUBJECTS[subject]}» не найден.")
        return

    status = bot.reply_to(message, "Готовлю вопросы... ⏱️")
    bot.send_chat_action(chat_id, "typing")
    try:
        pages = load_pages(path)
        if topic:
            context, _ = find_context(path, topic)
        else:
            rich = [p for p in pages if len(p) > 400]
            context = "\n\n".join(random.sample(rich, min(4, len(rich)))) if rich else ""
        if not context:
            bot.edit_message_text("Не нашёл подходящих страниц по этой теме.", chat_id, status.message_id)
            return

        prompt = (
            "На основе фрагментов учебника составь 5 тестовых вопросов с 4 вариантами ответа "
            "(ровно один верный). Верни ТОЛЬКО JSON-массив вида "
            '[{"q": "вопрос", "options": ["a","b","c","d"], "correct": 0, "explain": "пояснение до 150 символов"}]. '
            "Вопросы пиши на языке учебника. Не используй HTML.\n\n"
            f"Фрагменты:\n{context}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ИИ сейчас недоступен. Попробуй позже.", chat_id, status.message_id)
            return

        items = parse_json(raw)
        sent = 0
        for it in items[:5]:
            try:
                opts = [str(o)[:100] for o in it["options"]][:4]
                correct_text = opts[int(it["correct"])]
                random.shuffle(opts)
                bot.send_poll(
                    chat_id, str(it["q"])[:300], opts,
                    type="quiz", correct_option_id=opts.index(correct_text),
                    explanation=str(it.get("explain", ""))[:200], is_anonymous=False,
                )
                sent += 1
            except Exception:
                log.warning("Пропущен некорректный вопрос: %s", it)

        register_use(uid, message.from_user.first_name)
        settings[uid]["subject"] = subject
        if sent:
            bot.delete_message(chat_id, status.message_id)
        else:
            bot.edit_message_text("Не получилось составить тест, попробуй ещё раз.", chat_id, status.message_id)
    except Exception:
        log.exception("Ошибка теста")
        bot.edit_message_text("Произошла ошибка при создании теста.", chat_id, status.message_id)


# ===========================================================================
# Команды
# ===========================================================================
HELP_TEXT = (
    "<b>Как пользоваться</b>\n"
    "Пиши: <code>физика задача 3.5</code> — или просто текст задания (предмет запомнится).\n"
    "Можно прислать <b>фото</b>, <b>голосовое</b> или <b>PDF-файл</b> от учителя.\n"
    "Чтобы спросить по PDF — пришли файл с подписью: «объясни», «реши задание 3».\n\n"
    "<b>Команды</b>\n"
    "/hw — домашка с сайта школы (есть «На завтра»)\n"
    "/subscribe, /unsubscribe — рассылка домашки в 16:00\n"
    "/schedule — расписание уроков\n"
    "/class — выбрать свой класс 🏫\n"
    "/subjects — список предметов\n"
    "/subject физика — выбрать текущий предмет\n"
    "/explain [предмет] задание — объяснить, КАК решать\n"
    "/check [предмет] решение — проверить твоё решение (можно фото с подписью /check)\n"
    "/summary [предмет] тема — шпаргалка для повторения\n"
    "/quiz [предмет] [тема] — тест из 5 вопросов\n"
    "/top — рейтинг класса 🏆\n"
    "Под ответом: 🔁 похожая задача · ✍️ своими словами · 👍/👎\n"
    "/exam [предмет] [тема] — пробная контрольная 📝\n"
    "/formulas [предмет] — тренажёр формул 🧮\n"
    "/cards [предмет] [тема] — карточки для запоминания 🃏\n"
    "/cards — повторить накопившиеся карточки\n"
    "/proofread [текст] — проверить орфографию ✍️\n"
    "/remind 20.05 контрольная — напоминание ⏰\n"
    "/pomodoro 25 — таймер учёба/перерыв 🍅\n"
    "/short, /detail, /normal — длина ответов\n"
    "/essay [предмет] тема — написать сочинение\n"
    "/quick — режим только коротких ответов (выйти: /normal)\n"
    "/reset — очистить память диалога\n"
    "/stats — твоя статистика\n"
    "/id — твой Telegram ID"
)


@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    if gate(message, cooldown=False):
        bot.send_message(message.chat.id, HELP_TEXT, parse_mode="HTML")
        if message.text.strip().startswith("/start") and message.chat.id not in USER_CLASS:
            bot.send_message(message.chat.id,
                             "И выбери свой класс, чтобы домашка собиралась правильно:",
                             reply_markup=class_keyboard())


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Твой Telegram ID: {message.from_user.id}")


@bot.message_handler(commands=["subjects"])
def cmd_subjects(message):
    if not gate(message, cooldown=False):
        return
    cname = user_class(message.chat.id)
    keys = SUBJECT_KEYS_BY_CLASS.get(cname, SUBJECT_KEYS)
    if not keys:
        bot.send_message(
            message.chat.id,
            f"📚 Для {html.escape(cname)} учебники пока не подключены — и это не мешает:\n"
            "• пришли <b>фото задания</b> — решу и объясню\n"
            "• или напиши вопрос текстом\n"
            "• работают /hw, /cards, /quiz, /exam, /formulas\n\n"
            "Чтобы я решал точнее по программе — просто кинь фото страницы с заданием.",
            parse_mode="HTML")
        return
    bot.send_message(message.chat.id, f"Доступные предметы ({html.escape(cname)}):\n"
                                      + "\n".join(f"• {s}" for s in SUBJECTS))


@bot.message_handler(commands=["subject"])
def cmd_subject(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cur = settings[uid]["subject"]
        bot.reply_to(message, f"Текущий предмет: {cur or 'не выбран'}. Сменить: /subject физика")
        return
    key = resolve_subject(args[1])
    if not key:
        bot.reply_to(message, "Не знаю такой предмет. Список: /subjects")
        return
    if user_class(message.chat.id) in CLASSES_WITHOUT_BOOKS:
        bot.reply_to(message, "Для твоего класса учебники не подключены, выбирать предмет не нужно — "
                              "просто пришли фото задания или напиши вопрос.")
        return
    settings[uid]["subject"] = key
    bot.reply_to(message, f"Предмет выбран: {key}. Теперь можно писать задания без названия предмета.")


def _set_length(message, value, text):
    if gate(message, cooldown=False):
        settings[message.from_user.id]["length"] = value
        bot.reply_to(message, text)


@bot.message_handler(commands=["short"])
def cmd_short(message):
    _set_length(message, "short", "Режим: короткие ответы.")


@bot.message_handler(commands=["detail"])
def cmd_detail(message):
    _set_length(message, "detail", "Режим: подробные ответы.")


@bot.message_handler(commands=["normal"])
def cmd_normal(message):
    _set_length(message, "normal", "Режим: обычные ответы.")


@bot.message_handler(commands=["quick"])
def cmd_quick(message):
    _set_length(message, "quick", "Режим: только короткие ответы. Выйти: /normal")


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if gate(message, cooldown=False):
        history[message.from_user.id].clear()
        bot.reply_to(message, "Память диалога очищена.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    u = stats["users"].get(str(uid), {"total": 0})
    text = f"Сегодня: {used_today(uid)}/{DAILY_LIMIT}\nВсего запросов: {u['total']}"
    if uid == ADMIN_ID:
        rows = sorted(stats["users"].items(), key=lambda kv: kv[1]["total"], reverse=True)[:15]
        text += "\n\nВсе пользователи:\n" + "\n".join(
            f"{v['name']} ({k}): {v['total']} всего, {v['days'].get(datetime.now(KYIV).date().isoformat(), 0)} сегодня"
            for k, v in rows
        )
    bot.reply_to(message, text)
def streak_days(u):
    """Сколько дней подряд человек пользовался ботом."""
    d = datetime.now(KYIV).date()
    if not u["days"].get(d.isoformat()):
        d -= timedelta(days=1)
    s = 0
    while u["days"].get(d.isoformat()):
        s += 1
        d -= timedelta(days=1)
    return s
@bot.message_handler(commands=["top"])
def cmd_top(message):
    if not gate(message, cooldown=False):
        return
    rows = sorted(stats["users"].items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]
    if not rows:
        bot.reply_to(message, "Пока пусто — реши первое задание!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(str(v['name']))} — {v['total']} 🔥{streak_days(v)}"
        for i, (k, v) in enumerate(rows)
    ]
    bot.reply_to(message, "🏆 <b>Топ класса</b>\n\n" + "\n".join(lines), parse_mode="HTML")
# ===========================================================================
# Карточки с интервальным повторением (/cards)
# ===========================================================================
CARDS_PER_SET = 8
BOXES = [1, 2, 4, 7, 16]   # через сколько дней снова показать карточку

CARDS_MEM = {}             # хранилище, если Redis не подключён


def cards_load(uid):
    if kv_enabled():
        return kv_load(f"hwbot:cards:{uid}", [])
    return CARDS_MEM.get(uid, [])


def cards_save(uid, cards):
    if kv_enabled():
        kv_save(f"hwbot:cards:{uid}", cards)
    else:
        CARDS_MEM[uid] = cards


CARD_SESSION = {}   # uid -> {"cards": [...], "order": [...], "pos": int, "chat": id}


def _today():
    return datetime.now(KYIV).date()


@bot.message_handler(commands=["cards"])
def cmd_cards(message):
    if not gate(message):
        return
    uid = message.from_user.id
    parts = message.text.split(maxsplit=2)
    today = _today().isoformat()

    # /cards без аргументов - сессия повторения
    if len(parts) < 2:
        all_cards = cards_load(uid)
        order = [i for i, c in enumerate(all_cards) if c.get("due", today) <= today]
        if not order:
            extra = f" Следующее повторение карточек: {min((c['due'] for c in all_cards), default='')}" if all_cards else ""
            bot.reply_to(message, "Повторять пока нечего 🎉" + extra +
                                  "\nСоздать карточки: /cards физика <тема>")
            return
        CARD_SESSION[uid] = {"cards": all_cards, "order": order, "pos": 0,
                             "chat": message.chat.id}
        show_card(uid)
        return

    subject = resolve_subject(parts[1])
    if not subject:
        bot.reply_to(message, "Не знаю такой предмет. Список: /subjects")
        return
    topic = parts[2].strip() if len(parts) > 2 else ""
    if not topic:
        bot.reply_to(message, "Укажи тему: /cards физика давление")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    path = book_path(subject)
    if not path:
        bot.reply_to(message, f"Файл учебника «{SUBJECTS[subject]}» не найден.")
        return

    status = bot.reply_to(message, "Составляю карточки... ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        context, _ = find_context(path, topic)
        prompt = (
            "На основе фрагментов учебника составь 8 карточек для запоминания по теме "
            f"«{topic}». Верни ТОЛЬКО JSON-массив вида "
            '[{"q": "вопрос или термин", "a": "короткий ответ или определение"}]. '
            "Вопросы короткие, ответы до 25 слов, на языке учебника, без HTML.\n\n"
            f"Фрагменты:\n{context or '(нет)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.4)
        if not raw:
            bot.edit_message_text("ИИ сейчас недоступен. Попробуй позже.",
                                  message.chat.id, status.message_id)
            return
        items = parse_json(raw)
        all_cards = cards_load(uid)
        added = 0
        for it in items[:CARDS_PER_SET]:
            if it.get("q") and it.get("a"):
                all_cards.append({"q": str(it["q"])[:300], "a": str(it["a"])[:600],
                                  "box": 0, "due": _today().isoformat()})
                added += 1
        if not added:
            bot.edit_message_text("Не получилось составить карточки, попробуй ещё раз.",
                                  message.chat.id, status.message_id)
            return
        cards_save(uid, all_cards)
        register_use(uid, message.from_user.first_name)
        bot.edit_message_text(
            f"✅ Добавил {added} карточек по теме «{topic}».\n"
            "Повторять: /cards — новые будут появляться по расписанию повторений.",
            message.chat.id, status.message_id)
    except Exception:
        log.exception("Ошибка карточек")
        try:
            bot.edit_message_text("Произошла ошибка. Попробуй ещё раз.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def show_card(uid):
    s = CARD_SESSION.get(uid)
    if not s:
        return
    card = s["cards"][s["order"][s["pos"]]]
    text = (f"🃏 Карточка {s['pos'] + 1}/{len(s['order'])}\n\n"
            f"<b>{html.escape(card['q'])}</b>")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Показать ответ", callback_data="card:show"))
    try:
        bot.send_message(s["chat"], text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        bot.send_message(s["chat"], strip_tags(text), reply_markup=kb)


@bot.callback_query_handler(func=lambda call: call.data == "card:show")
def card_show(call):
    uid = call.from_user.id
    s = CARD_SESSION.get(uid)
    bot.answer_callback_query(call.id)
    if not s or call.message.chat.id != s["chat"]:
        return
    card = s["cards"][s["order"][s["pos"]]]
    kb = InlineKeyboardMarkup()
    kb.row(InlineKeyboardButton("✅ Знаю", callback_data="card:ok"),
           InlineKeyboardButton("❌ Не знаю", callback_data="card:no"))
    try:
        bot.edit_message_text(
            f"🃏 Карточка {s['pos'] + 1}/{len(s['order'])}\n\n"
            f"<b>{html.escape(card['q'])}</b>\n\n{html.escape(card['a'])}",
            call.message.chat.id, call.message.message_id,
            parse_mode="HTML", reply_markup=kb)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data in ("card:ok", "card:no"))
def card_grade(call):
    uid = call.from_user.id
    s = CARD_SESSION.get(uid)
    if not s or call.message.chat.id != s["chat"]:
        bot.answer_callback_query(call.id)
        return
    good = call.data.endswith("ok")
    card = s["cards"][s["order"][s["pos"]]]
    if good:
        card["box"] = min(card.get("box", 0) + 1, len(BOXES) - 1)
    else:
        card["box"] = 0
    card["due"] = (_today() + timedelta(days=BOXES[card["box"]])).isoformat()
    s["pos"] += 1
    cards_save(uid, s["cards"])
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Молодец! 💪" if good else "Появится снова завтра.")
    if s["pos"] < len(s["order"]):
        show_card(uid)
    else:
        CARD_SESSION.pop(uid, None)
        bot.send_message(call.message.chat.id,
                         "🎉 Сессия завершена! Нажми /cards завтра — покажу следующие.")


# ===========================================================================
# /proofread - проверка орфографии
# ===========================================================================
@bot.message_handler(commands=["proofread"])
def cmd_proofread(message):
    if not gate(message):
        return
    uid = message.from_user.id
    if message.reply_to_message and message.reply_to_message.text:
        task = message.reply_to_message.text
    else:
        parts = message.text.split(maxsplit=1)
        task = parts[1].strip() if len(parts) > 1 else ""
    if not task:
        bot.reply_to(message, "Пиши так: /proofread <текст>\n"
                              "Или ответь (reply) на сообщение командой /proofread")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    status = bot.reply_to(message, "Проверяю... ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    prompt = (
        "Проверь текст на орфографические, пунктуационные и грамматические ошибки "
        "(язык определяй сам: украинский или русский). Верни:\n"
        "1) Список ошибок по пунктам в виде «неправильно → правильно (правило)».\n"
        "2) Строку «ИСПРАВЛЕННЫЙ ТЕКСТ:» и текст без ошибок целиком.\n"
        "Если ошибок нет — так и скажи. Не используй HTML.\n\n"
        f"Текст:\n{task[:3000]}"
    )
    answer = call_gemini([prompt], system_instruction=SYSTEM_BASE)
    if not answer:
        bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                              message.chat.id, status.message_id)
        return
    register_use(uid, message.from_user.first_name)
    send_answer(message.chat.id, status, answer, uid=uid, task=task[:500])

# ===========================================================================
# /remind - напоминания о контрольных
# ===========================================================================
def reminds_load():
    if kv_enabled():
        return kv_load("hwbot:reminds", [])
    return REMINDS_MEM


def reminds_save(r):
    if kv_enabled():
        kv_save("hwbot:reminds", r)
    else:
        REMINDS_MEM[:] = r


@bot.message_handler(commands=["remind"])
def cmd_remind(message):
    if not gate(message):
        return
    parts = message.text.split(maxsplit=1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if text.lower() in ("список", "list"):
        mine = [r for r in reminds_load() if r["chat"] == message.chat.id]
        if not mine:
            bot.reply_to(message, "Напоминаний нет. Добавить: /remind 20.05 контрольная по физике")
            return
        lines = [f"• {r['due']} — {r['text']}" for r in sorted(mine, key=lambda x: x["due"])]
        bot.reply_to(message, "⏰ Напоминания:\n" + "\n".join(lines))
        return
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if not m:
        bot.reply_to(message, "Укажи дату и что напомнить:\n/remind 20.05 контрольная по физике\n"
                              "Показать список: /remind список")
        return
    d_, mo_, y_ = m.groups()
    year = int(y_) if y_ else _today().year
    try:
        due = datetime(year, int(mo_), int(d_)).date()
    except ValueError:
        bot.reply_to(message, "Такой даты не бывает. Пример: /remind 20.05 контрольная")
        return
    if not y_ and due < _today():
        due = datetime(year + 1, int(mo_), int(d_)).date()
    what = (text[:m.start()] + " " + text[m.end():]).strip() or "Напоминание"
    reminds = reminds_load()
    reminds.append({"chat": message.chat.id, "uid": message.from_user.id,
                    "text": what[:300], "due": due.isoformat(), "warned": False})
    reminds_save(reminds)
    bot.reply_to(message, f"👌 Записал: {what}\nНапомню {due.strftime('%d.%m.%Y')} и за 2 дня до.")


def _check_reminds():
    today = _today()
    reminds = reminds_load()
    if not reminds:
        return
    changed, keep = False, []
    for r in reminds:
        try:
            left = (datetime.fromisoformat(r["due"]).date() - today).days
        except ValueError:
            continue
        if left == 2 and not r.get("warned"):
            try:
                bot.send_message(r["chat"], f"⚠️ Через 2 дня: {r['text']}")
            except Exception:
                pass
            r["warned"] = True
            changed = True
            keep.append(r)
        elif left == 0:
            try:
                bot.send_message(r["chat"], f"‼️ Сегодня: {r['text']}")
            except Exception:
                pass
            changed = True
        elif left > 0:
            keep.append(r)
        else:
            changed = True
    if changed:
        reminds_save(keep)


# ===========================================================================
# /pomodoro - таймер учёбы
# ===========================================================================
@bot.message_handler(commands=["pomodoro"])
def cmd_pomodoro(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ("stop", "стоп"):
        s = POMODORO.pop(uid, None)
        if s:
            s["t1"].cancel()
            if s.get("t2"):
                s["t2"].cancel()
            bot.reply_to(message, "🍅 Таймер остановлен.")
        else:
            bot.reply_to(message, "Таймер и не был запущен.")
        return
    try:
        mins = int(args[1]) if len(args) > 1 else 25
    except ValueError:
        mins = 25
    if not 5 <= mins <= 90:
        bot.reply_to(message, "Ставь от 5 до 90 минут: /pomodoro 25")
        return
    old = POMODORO.pop(uid, None)
    if old:
        old["t1"].cancel()
        if old.get("t2"):
            old["t2"].cancel()
    chat = message.chat.id
    brk = max(3, mins // 5)

    def on_break():
        try:
            bot.send_message(chat, f"⏰ {mins} минут вышли! Перерыв {brk} мин ☕️")
        except Exception:
            pass
        t2 = threading.Timer(brk * 60, back_to_work)
        t2.daemon = True
        t2.start()
        POMODORO[uid]["t2"] = t2

    def back_to_work():
        POMODORO.pop(uid, None)
        try:
            bot.send_message(chat, "🍅 Перерыв закончен — за учёбу!")
        except Exception:
            pass

    t1 = threading.Timer(mins * 60, on_break)
    t1.daemon = True
    t1.start()
    POMODORO[uid] = {"t1": t1, "t2": None}
    bot.reply_to(message, f"🍅 Погнали: {mins} мин учёбы → {brk} мин перерыва.\nОстановить: /pomodoro stop")


# ===========================================================================
# Ежедневный отчёт админу + фоновый цикл
# ===========================================================================
ADMIN_REPORT_HOUR = 20
_last_report = None


def _daily_admin_report():
    global _last_report
    if not ADMIN_ID:
        return
    now = datetime.now(KYIV)
    if now.hour < ADMIN_REPORT_HOUR or _last_report == now.date():
        return
    _last_report = now.date()
    today = now.date().isoformat()
    reqs = sum(u["days"].get(today, 0) for u in stats["users"].values())
    active = sum(1 for u in stats["users"].values() if u["days"].get(today, 0))
    redis_ok = "не подключён"
    if kv_enabled():
        try:
            _kv(["PING"])
            redis_ok = "работает"
        except Exception:
            redis_ok = "НЕ ОТВЕЧАЕТ"
    try:
        bot.send_message(ADMIN_ID,
                         f"📊 Отчёт за {today}\n"
                         f"Запросов: {reqs} (активных учеников: {active})\n"
                         f"👎 плохих ответов: {FB_LOG.count(today)}\n"
                         f"Redis: {redis_ok}\n"
                         f"Бот живёт: {(time.time() - START_TIME) / 3600:.1f} ч")
    except Exception:
        log.exception("Не удалось отправить отчёт админу")


def daily_loop():
    while True:
        try:
            _check_reminds()
            _daily_admin_report()
            _question_of_the_day()
        except Exception:
            log.exception("Ошибка daily_loop")
        time.sleep(300)
# ===========================================================================
# Приём PDF-файлов от учителя
# ===========================================================================
MAX_PDF_CHARS = 15000


@bot.message_handler(content_types=["document"])
def on_document(message):
    if not gate(message):
        return
    uid = message.from_user.id
    doc = message.document
    name = doc.file_name or "file.pdf"
    if not (name.lower().endswith(".pdf") or doc.mime_type == "application/pdf"):
        bot.reply_to(message, "Пока умею только PDF. Пришли файл .pdf")
        return
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        bot.reply_to(message, "Файл больше 20 МБ — Telegram не даст его скачать.")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    status = bot.reply_to(message, "Читаю файл... ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)
        reader = PdfReader(BytesIO(data))
        text = "\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()
    except Exception:
        log.exception("Не удалось прочитать PDF")
        bot.edit_message_text("Не получилось прочитать PDF. Попробуй прислать фото страниц.",
                              message.chat.id, status.message_id)
        return
    if not text:
        bot.edit_message_text("В PDF нет текста (похоже, это скан). Пришли фото страниц.",
                              message.chat.id, status.message_id)
        return
    question = (message.caption or "").strip()
    if not question:
        head = html.escape(text[:600])
        bot.edit_message_text(
            "📄 Файл прочитал. Теперь перешли этот же файл с подписью, что с ним делать, "
            "например: «объясни тему», «реши задание 3», «составь тест по файлу».\n\n"
            f"<b>Начало документа:</b>\n{head}",
            message.chat.id, status.message_id, parse_mode="HTML")
        return
    try:
        answer = call_gemini(
            [f"Документ от учителя (может быть обрезан):\n{text[:MAX_PDF_CHARS]}\n\n"
             f"Вопрос или задание ученика по документу: {question}\n\n"
             "Ответь по документу. Оформи структурированно, HTML-теги Telegram: <b>, <i>, <code>."],
            system_instruction=SYSTEM_BASE)
    except Exception:
        log.exception("Ошибка запроса по PDF")
        answer = None
    if not answer:
        bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                              message.chat.id, status.message_id)
        return
    register_use(uid, message.from_user.first_name)
    send_answer(message.chat.id, status, answer, uid=uid, task=question[:500])

# ===========================================================================
# Вопрос дня (шлём в 18:00 всем подписчикам рассылки)
# ===========================================================================
QDAY_HOUR = 18
_qday_last = None
QDAY_CACHE = {}                    # token -> ответ


@bot.callback_query_handler(func=lambda call: call.data.startswith("qday:"))
def on_qday_answer(call):
    token = call.data.split(":", 1)[1]
    answer = QDAY_CACHE.get(token)
    bot.answer_callback_query(call.id)
    if answer:
        bot.send_message(call.message.chat.id, f"💡 {html.escape(answer)}")
    else:
        bot.send_message(call.message.chat.id, "Ответ устарел (бот перезапускался).")


def _question_of_the_day():
    global _qday_last
    now = datetime.now(KYIV)
    if now.hour < QDAY_HOUR or _qday_last == now.date():
        return
    _qday_last = now.date()        # флаг ставим сразу: один раз в день, без повторов
    subjects = [s for s in SUBJECTS if book_path(s)]
    if not subjects:
        return
    subj = random.choice(subjects)
    pages = [p for p in load_pages(book_path(subj)) if len(p) > 400]
    if not pages:
        return
    page = random.choice(pages)
    raw = call_gemini(
        "Придумай по фрагменту учебника ОДИН интересный вопрос для ученика 8 класса "
        "и короткий ответ на него (до 40 слов). Верни ТОЛЬКО JSON вида "
        '{"q": "вопрос", "a": "ответ"}.\n\n'
        f"Фрагмент учебника ({subj}):\n{page[:3000]}",
        json_mode=True, temperature=0.8)
    if not raw:
        log.warning("Вопрос дня: ИИ не ответил")
        return
    try:
        it = parse_json(raw)
        q, a = str(it["q"]).strip(), str(it["a"]).strip()
    except Exception:
        return
    if not q or not a:
        return
    token = uuid.uuid4().hex[:8]
    QDAY_CACHE[token] = a
    _trim(QDAY_CACHE, 50)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💡 Показать ответ", callback_data=f"qday:{token}"))
    text = f"❓ <b>Вопрос дня</b> ({html.escape(subj)})\n\n{html.escape(q)}"
    for chat_id in broadcast_targets():
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("Вопрос дня: не отправилось в %s", chat_id)
# ===========================================================================
# /exam - пробная контрольная
# ===========================================================================
EXAM_QCOUNT = 5
EXAM = {}   # uid -> {"items", "answers", "pos", "chat", "subject"}


@bot.message_handler(commands=["exam"])
def cmd_exam(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=2)
    if len(args) > 1 and args[1].lower() in ("стоп", "stop"):
        if EXAM.pop(uid, None):
            bot.reply_to(message, "Экзамен прерван. Начать заново: /exam физика")
        else:
            bot.reply_to(message, "Экзамен и не шёл. Начать: /exam физика")
        return
    if uid in FORMULA:
        bot.reply_to(message, "Сначала закончи тренажёр формул (/formulas стоп).")
        return
    if uid in EXAM:
        bot.reply_to(message, "Экзамен уже идёт — отвечай на текущий вопрос или /exam стоп.")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Укажи предмет: /exam физика [тема]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    path = book_path(subject)
    if not path:
        bot.reply_to(message, f"Файл учебника «{SUBJECTS[subject]}» не найден.")
        return
    status = bot.reply_to(message, "Составляю вариант контрольной... ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"На основе фрагментов учебника составь контрольную из {EXAM_QCOUNT} заданий "
            "уровня 8 класса с КОРОТКИМИ ответами (число, слово, формула или 1 предложение). "
            'Верни ТОЛЬКО JSON-массив вида [{"q": "текст задания", "a": "короткий эталонный ответ"}]. '
            "Задания на языке учебника, разной сложности, без HTML.\n\n"
            f"Фрагменты:\n{context or '(нет)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("q") and it.get("a")][:EXAM_QCOUNT]
        if len(items) < 3:
            bot.edit_message_text("Не получилось составить вариант, попробуй ещё раз.",
                                  message.chat.id, status.message_id)
            return
        EXAM[uid] = {"items": items, "answers": [], "pos": 0,
                     "chat": message.chat.id, "subject": subject}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _exam_ask(uid)
    except Exception:
        log.exception("Ошибка /exam")
        try:
            bot.edit_message_text("Произошла ошибка. Попробуй ещё раз.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _exam_ask(uid):
    s = EXAM.get(uid)
    if not s:
        return
    q = s["items"][s["pos"]]["q"]
    text = (f"📝 <b>Контрольная</b> ({html.escape(s['subject'])}) · "
            f"задание {s['pos'] + 1} из {len(s['items'])}\n\n"
            f"{html.escape(str(q))}\n\n"
            "<i>Ответ напиши следующим сообщением. Прервать: /exam стоп</i>")
    bot.send_message(s["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in EXAM
                     and not m.text.startswith("/")
                     and m.chat.id == EXAM[m.from_user.id]["chat"],
                     content_types=["text"])
def on_exam_answer(message):
    uid = message.from_user.id
    s = EXAM[uid]
    s["answers"].append(message.text.strip()[:500])
    s["pos"] += 1
    if s["pos"] < len(s["items"]):
        _exam_ask(uid)
        return
    # все ответы собраны - одна проверка на все задания
    bot.send_chat_action(s["chat"], "typing")
    status = bot.send_message(s["chat"], "Проверяю работу... ⏱️")
    qa = "\n\n".join(
        f"{i + 1}. Задание: {it['q']}\nЭталон: {it['a']}\nОтвет ученика: {a or '(нет ответа)'}"
        for i, (it, a) in enumerate(zip(s["items"], s["answers"])))
    prompt = (
        "Проверь контрольную ученика. По каждому заданию поставь балл от 0 до 2 "
        "(0 - неверно, 1 - частично верно, 2 - верно; засчитывай ответы, совпадающие "
        "по смыслу, даже если формулировка другая). Верни ТОЛЬКО JSON: "
        '[{"n": 1, "score": 2, "comment": "краткий комментарий до 100 символов"}].\n\n' + qa)
    raw = call_gemini(prompt, json_mode=True, temperature=0.1)
    EXAM.pop(uid, None)
    if not raw:
        bot.edit_message_text("ИИ сейчас недоступен — работа не проверена. "
                              "Попробуй позже ещё раз: /exam",
                              s["chat"], status.message_id)
        return
    try:
        grades = parse_json(raw)
    except Exception:
        grades = []
    earned, lines = 0, []
    for g in grades[:len(s["items"])]:
        try:
            sc = max(0, min(2, int(g.get("score", 0))))
        except Exception:
            sc = 0
        earned += sc
        lines.append(f"{'✅' if sc == 2 else '🟡' if sc == 1 else '❌'} "
                     f"Задание {g.get('n', len(lines) + 1)}: {sc}/2 — "
                     f"{html.escape(str(g.get('comment', '')))}")
    while len(lines) < len(s["items"]):
        lines.append(f"❌ Задание {len(lines) + 1}: 0/2 — не проверено")
    mx = 2 * len(s["items"])
    grade = max(1, min(12, round((earned / mx if mx else 0) * 12)))
    verdict = ("🎉 Отличная работа!" if grade >= 10
               else "👍 Хорошо, но есть куда расти." if grade >= 7
               else "📖 Материал надо повторить: /summary или /cards помогут.")
    text = (f"📝 <b>Результат контрольной</b> ({html.escape(s['subject'])})\n\n"
            + "\n".join(lines)
            + f"\n\n<b>Баллы: {earned}/{mx} · Оценка: {grade}/12</b>\n{verdict}")
    try:
        bot.edit_message_text(text, s["chat"], status.message_id, parse_mode="HTML")
    except Exception:
        bot.send_message(s["chat"], text, parse_mode="HTML")


# ===========================================================================
# /formulas - тренажёр формул
# ===========================================================================
FORMULA_ROUNDS = 5
FORMULA = {}   # uid -> {"items", "pos", "right", "chat", "subject"}


@bot.message_handler(commands=["formulas"])
def cmd_formulas(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=2)
    if len(args) > 1 and args[1].lower() in ("стоп", "stop"):
        if FORMULA.pop(uid, None):
            bot.reply_to(message, "Тренажёр остановлен.")
        else:
            bot.reply_to(message, "Тренажёр и не шёл. Начать: /formulas физика")
        return
    if uid in EXAM:
        bot.reply_to(message, "Сначала закончи контрольную (/exam стоп).")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Укажи предмет: /formulas физика [тема]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан.")
        return
    path = book_path(subject)
    if not path:
        bot.reply_to(message, f"Файл учебника «{SUBJECTS[subject]}» не найден.")
        return
    status = bot.reply_to(message, "Выбираю формулы... ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"На основе фрагментов учебника выбери {FORMULA_ROUNDS} формул или правил "
            "уровня 8 класса. Верни ТОЛЬКО JSON-массив вида "
            '[{"term": "название величины или правила", "formula": "сама формула текстом"}]. '
            "Без HTML.\n\n"
            f"Фрагменты:\n{context or '(нет)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ИИ сейчас недоступен, попробуй позже.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("term") and it.get("formula")][:FORMULA_ROUNDS]
        if len(items) < 3:
            bot.edit_message_text("Не получилось собрать формулы, попробуй ещё раз.",
                                  message.chat.id, status.message_id)
            return
        FORMULA[uid] = {"items": items, "pos": 0, "right": 0,
                        "chat": message.chat.id, "subject": subject}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _formula_ask(uid)
    except Exception:
        log.exception("Ошибка /formulas")
        try:
            bot.edit_message_text("Произошла ошибка. Попробуй ещё раз.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _formula_ask(uid):
    f = FORMULA.get(uid)
    if not f:
        return
    it = f["items"][f["pos"]]
    text = (f"🧮 <b>Тренажёр формул</b> ({html.escape(f['subject'])}) · "
            f"{f['pos'] + 1}/{len(f['items'])} · верно: {f['right']}\n\n"
            f"Напиши формулу: <b>{html.escape(str(it['term']))}</b>\n\n"
            "<i>Любой записью: E=mv²/2 или E = m*v^2/2. Выйти: /formulas стоп</i>")
    bot.send_message(f["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: m.from_user.id in FORMULA
                     and not m.text.startswith("/")
                     and m.chat.id == FORMULA[m.from_user.id]["chat"],
                     content_types=["text"])
def on_formula_answer(message):
    uid = message.from_user.id
    f = FORMULA[uid]
    it = f["items"][f["pos"]]
    status = bot.reply_to(message, "Проверяю... ⏱️")
    prompt = (
        f"Величина: {it['term']}\nЭталонная формула: {it['formula']}\n"
        f"Ответ ученика: {message.text[:200]}\n\n"
        "Формулы считаются равными, если совпадает смысл (разные обозначения и форма "
        'записи допустимы). Верни ТОЛЬКО JSON: '
        '{"match": true/false, "correct": "правильная формула текстом"}')
    raw = call_gemini(prompt, json_mode=True, temperature=0.0)
    ok, correct = False, str(it["formula"])
    if raw:
        try:
            res = parse_json(raw)
            ok = bool(res.get("match"))
            if res.get("correct"):
                correct = str(res["correct"])
        except Exception:
            pass
    f["pos"] += 1
    if raw:
        if ok:
            f["right"] += 1
            mark = "✅ Верно!"
        else:
            mark = f"❌ Не то. Правильно: <b>{html.escape(correct)}</b>"
        try:
            bot.edit_message_text(mark, message.chat.id, status.message_id, parse_mode="HTML")
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
    else:
        try:
            bot.edit_message_text("ИИ недоступен, раунд пропущен.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass
    if f["pos"] < len(f["items"]):
        _formula_ask(uid)
    else:
        FORMULA.pop(uid, None)
        r, n = f["right"], len(f["items"])
        bot.send_message(f["chat"], f"🏁 Итог: {r} из {n}. "
                         + ("💪 Отлично!" if r == n else "Ещё заход: /formulas"))

def _mode_command(mode, usage):
    def handler(message):
        if not gate(message):
            return
        args = message.text.split(maxsplit=1)
        subject, task = split_subject(args[1] if len(args) > 1 else "")
        subject = subject or settings[message.from_user.id]["subject"]
        if not task:
            bot.reply_to(message, usage)
            return
        dispatch(message, mode, subject, task, [])
    return handler


bot.message_handler(commands=["explain"])(_mode_command("explain", "Пример: /explain физика задача 3.5"))
bot.message_handler(commands=["check"])(_mode_command("check", "Пример: /check алгебра моё решение: x = 5... (или отправь фото с подписью /check)"))
bot.message_handler(commands=["summary"])(_mode_command("summary", "Пример: /summary история Первая мировая война"))
bot.message_handler(commands=["essay"])(_mode_command("essay", "Пример: /essay укрлит образ Тараса Бульбы, 250 слов"))


@bot.message_handler(commands=["quiz"])
def cmd_quiz(message):
    if not gate(message):
        return
    args = message.text.split(maxsplit=1)
    subject, topic = split_subject(args[1] if len(args) > 1 else "")
    subject = subject or settings[message.from_user.id]["subject"]
    if not subject:
        bot.reply_to(message, "Укажи предмет: /quiz физика или /quiz физика законы Ньютона")
        return
    run_quiz(message, subject, topic)


# ===========================================================================
# Фото, голосовые, текст, кнопки
# ===========================================================================
@bot.message_handler(content_types=["photo"])
def on_photo(message):
    if not gate(message):
        return
    caption = (message.caption or "").strip()
    mode = "answer"
    m = re.match(r"^/(explain|check)(@\w+)?\s*(.*)", caption, re.S)
    if m:
        mode, caption = m.group(1), m.group(3)
    subject, task = split_subject(caption)
    subject = subject or settings[message.from_user.id]["subject"]
    try:
        data = bot.download_file(bot.get_file(message.photo[-1].file_id).file_path)
    except Exception:
        log.exception("Не удалось скачать фото")
        bot.reply_to(message, "Не удалось скачать фото, попробуй ещё раз.")
        return
    dispatch(message, mode, subject, task, [gt.Part.from_bytes(data=data, mime_type="image/jpeg")])


@bot.message_handler(content_types=["voice", "audio"])
def on_voice(message):
    if not gate(message):
        return
    obj = message.voice or message.audio
    if obj.duration and obj.duration > MAX_VOICE_SECONDS:
        bot.reply_to(message, f"Голосовое слишком длинное (максимум {MAX_VOICE_SECONDS} сек).")
        return
    mime = "audio/ogg" if message.voice else (obj.mime_type or "audio/mpeg")
    try:
        data = bot.download_file(bot.get_file(obj.file_id).file_path)
    except Exception:
        log.exception("Не удалось скачать аудио")
        bot.reply_to(message, "Не удалось скачать аудио, попробуй ещё раз.")
        return
    subject = settings[message.from_user.id]["subject"]
    dispatch(message, "answer", subject, "", [gt.Part.from_bytes(data=data, mime_type=mime)])


@bot.message_handler(content_types=["text"])
def on_text(message):
    if message.text.startswith("/"):
        cmd = message.text.split()[0].split("@")[0].lstrip("/").lower()
        close = get_close_matches(cmd, ["hw", "quiz", "explain", "check", "summary", "essay",
                                        "schedule", "subjects", "subject", "top", "stats",
                                        "short", "detail", "normal", "quick", "reset",
                                        "subscribe", "unsubscribe", "help", "class", "cards",
                                        "proofread", "remind", "pomodoro", "exam", "formulas",
                                        "id"], n=1, cutoff=0.6)
        hint = f" Может, ты имел в виду /{close[0]}?" if close else ""
        bot.reply_to(message, f"Не знаю такую команду.{hint}\nСписок: /help")
        return
    if not gate(message):
        return
    uid = message.from_user.id
    subject, task = split_subject(message.text)
    if subject and not task:
        settings[uid]["subject"] = subject
        bot.reply_to(message, f"Предмет выбран: {subject}. Теперь пиши задание.")
        return
    subject = subject or settings[uid]["subject"]
    dispatch(message, "answer", subject, task, [])# Словарь с твоим расписанием 8-Б

# ===========================================================================
# Мини-веб-сервер (нужен Render, чтобы считать сервис живым; на него же
# можно направить UptimeRobot, чтобы бесплатный сервис не засыпал)
# ===========================================================================
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    try:
        HTTPServer(("0.0.0.0", port), Ping).serve_forever()
    except Exception:
        log.exception("Веб-сервер не запустился (порт %s)", port)


# ===========================================================================
# Запуск
# ===========================================================================
if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=preload_books, daemon=True).start()
    threading.Thread(target=broadcast_loop, daemon=True).start()
    threading.Thread(target=daily_loop, daemon=True).start()
    log.info("Бот запущен. Модели: %s | ключей Gemini: %d", ", ".join(MODELS), len(API_KEYS))
    # ===========================================================================
    # Инлайн-режим: @имя_бота вопрос - в любом чате
    # ===========================================================================
    INLINE_CACHE = {}
    _last_inline_gen = {}


    @bot.inline_handler(func=lambda q: len(q.query.strip()) >= 10)
    def on_inline(query):
        uid = query.from_user.id
        if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
            bot.answer_inline_query(query.id, [], cache_time=1)
            return
        q = query.query.strip()
        key = q.lower()
        hit = INLINE_CACHE.get(key)
        if hit and time.time() - hit[0] < 1800:
            title, text = hit[1]
        else:
            if time.time() - _last_inline_gen.get(uid, 0) < 2:  # не чаще раза в 2 сек
                bot.answer_inline_query(query.id, [], cache_time=0)
                return
            _last_inline_gen[uid] = time.time()
            if limit_reached(uid):
                bot.answer_inline_query(query.id, [], cache_time=1)
                return
            answer = call_gemini([f"Ответь кратко (до 100 слов) на вопрос ученика:\n{q[:500]}"],
                                 system_instruction=SYSTEM_BASE, temperature=0.2)
            if not answer:
                bot.answer_inline_query(query.id, [], cache_time=1)
                return
            register_use(uid, query.from_user.first_name)
            text = sanitize_html(answer)
            title = q[:64]
            INLINE_CACHE[key] = (time.time(), (title, text))
            _trim(INLINE_CACHE, 200)
        try:
            art = tg.InlineQueryResultArticle(
                query.id, title,
                tg.InputTextMessageContent(text, parse_mode="HTML"),
                description="Ответ ИИ · нажми, чтобы отправить в чат")
            bot.answer_inline_query(query.id, [art], cache_time=10)
        except Exception:
            log.exception("Ошибка inline")
    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception:
            log.exception("Polling упал, перезапуск через 10 сек")
            time.sleep(10)
