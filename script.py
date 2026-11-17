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
import urllib.request
from functools import lru_cache
from collections import Counter, defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor
import math
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

KYIV = None
for _tz in ("Europe/Kyiv", "Europe/Kiev"):
    try:
        from zoneinfo import ZoneInfo
        KYIV = ZoneInfo(_tz)
        break
    except Exception:
        pass
if KYIV is None:
    KYIV = timezone(timedelta(hours=2))
from difflib import get_close_matches
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO

import telebot
from telebot import types as tg
from google import genai
from google.genai import types as gt
from pypdf import PdfReader


class LRU:
    """Bounded thread-safe LRU cache.

    Replaces every ad-hoc ``dict`` + ``_trim`` pattern that is NOT thread-safe
    (``RuntimeError: dict changed size`` when two threads trim concurrently).
    """

    def __init__(self, maxsize):
        self.d = OrderedDict()
        self.n = maxsize
        self.l = threading.RLock()

    def get(self, key, default=None):
        with self.l:
            if key in self.d:
                self.d.move_to_end(key)
                return self.d[key]
            return default

    def set(self, key, value):
        with self.l:
            self.d[key] = value
            self.d.move_to_end(key)
            while len(self.d) > self.n:
                self.d.popitem(last=False)

    def pop(self, key, default=None):
        with self.l:
            return self.d.pop(key, default)

    def __contains__(self, key):
        with self.l:
            return key in self.d

    def __len__(self):
        with self.l:
            return len(self.d)

    def clear(self):
        with self.l:
            self.d.clear()

    def items(self):
        with self.l:
            return list(self.d.items())

    def keys(self):
        with self.l:
            return list(self.d.keys())

    def values(self):
        with self.l:
            return list(self.d.values())


# ===========================================================================
# Настройки - ВСЕ секреты только через переменные окружения (Render -> Environment)
# ===========================================================================
#   TELEGRAM_TOKEN   - токен от @BotFather                         (обязательно)
#   GEMINI_API_KEYS  - ключи Gemini через запятую                  (обязательно)
#   GEMINI_MODELS    - модели через запятую (по умолч. gemini-3.5-flash-lite)
#   ADMIN_ID         - твій Telegram ID (без лимитов, видит статистику)
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

if ":" not in TELEGRAM_TOKEN:
    raise SystemExit("Не задано TELEGRAM_TOKEN (змінна середовища) або він неправильний.")
if not API_KEYS:
    raise SystemExit("Не задано ключів Gemini: вкажи GEMINI_API_KEYS (через кому).")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(BASE_DIR, "книги")
DATA_DIR = os.path.join(BASE_DIR, "data")      # статистика (на Render не сохраняется)
CACHE_DIR = os.path.join(BASE_DIR, "cache")    # кэш текста учебников (можно коммитить)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_CONTEXT_CHARS = 20000
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "1.2"))
TG_LIMIT = 4000
BOT_THREADS = int(os.environ.get("BOT_THREADS", "24"))
PRELOAD_WORKERS = int(os.environ.get("PRELOAD_WORKERS", "4"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "8"))
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
    "entrepreneurship": "підприємництво.pdf",
    "technology": "технології.pdf",
}

SUBJECT_LABELS_UA = {
    "алгебра": "Алгебра", "биология": "Біологія", "география": "Географія",
    "геометрия": "Геометрія", "зарубежка": "Зарубіжна література",
    "история": "Історія", "музыка": "Музика", "укрлит": "Українська література",
    "укрмов": "Українська мова", "физика": "Фізика", "химия": "Хімія",
    "entrepreneurship": "Підприємництво і фінансова грамотність",
    "technology": "Технології",
}

def subject_label(subject):
    return SUBJECT_LABELS_UA.get(subject, subject)

ALIASES = {
    "фізика": "физика", "хімія": "химия", "біологія": "биология",
    "географія": "география", "геометрія": "геометрия", "історія": "история",
    "музика": "музыка", "укрмова": "укрмов", "укрліт": "укрлит",
    "зарубіжка": "зарубежка", "підприємництво": "entrepreneurship",
    "технології": "technology", "підприємництво і фінансова грамотність": "entrepreneurship",
}

SYSTEM_BASE = (
    "Ти розумний шкільний помічник. Насамперед спирайся на надані "
    "фрагменти підручника. Якщо в них немає інформації, відповідай на основі загальних знань. "
    "Оформлюй відповідь структуровано, використовуючи ЛИШЕ HTML-теги Telegram: "
    "<b>жирний</b> для головної відповіді, <i>курсив</i>, <code>формули</code>. "
    "Інші теги та Markdown не використовуй. Символи < і > у формулах пиши словами "
    "(«менше», «більше») або як ≤ ≥. Розділяй блоки порожніми рядками. "
    "Відповідай ЛИШЕ УКРАЇНСЬКОЮ МОВОЮ. Якщо користувач прямо просить іншу мову, використовуй її лише для цього запиту.  "
    "Якщо потрібного завдання у фрагментах немає — НЕ відмовляйся відповідати й "
    "не міркуй про те, чого немає в підручнику: розв’яжи за загальними знаннями та правилами теми, "
    "зазначивши це ОДНИМ коротким рядком на початку: «📌 Умови в підручнику не знайдено — "
    "розв’язую за правилами теми. Для точності можна надіслати фото завдання». Далі "
    "лише розв’язання у звичайній структурі. Не складай списків того, що є на сторінках."
)

LENGTH_INSTR = {
    "normal": "",
    "short": "Відповідай максимально коротко: не більше 5–6 речень.",
    "detail": "Відповідай докладно й розгорнуто, покроково.",
    "quick": ("РЕЖИМ ШВИДКИХ ВІДПОВІДЕЙ. Дай ЛИШЕ підсумкову відповідь: число, слово або варіант. "
              "Без привітань і пояснень. Для задачі допустимий один рядок із розрахунком. "
              "На повідомлення не про навчання відповідай одним словом."),
}

MODE_INSTR = {
        "answer": ("Дай готову точну відповідь. Без вступних фраз і без окремого пояснення суті, "
               "не пиши слово «Суть». Якщо потрібні обчислення, подай їх коротко за кроками."),
    "explain": (
        "Не обмежуйся відповіддю: поясни покроково, як розв’язувати таке завдання, "
        "які правила та формули використовуються і чому, щоб учень міг "
        "самостійно розв’язати подібне. Наприкінці дай підсумкову відповідь."
    ),
    "check": (
        "Учень надіслав власний розв’язок або відповідь. Перевір її: скажи, чи "
        "вона правильна, знайди конкретні помилки та поясни, як правильно."
    ),
    "summary": (
        "Склади коротку структуровану шпаргалку для повторення за "
        "вказаною темою: головні визначення, правила, формули, 1–2 приклади. "
        "Без зайвої води."
    ),
    "essay": ("Напиши шкільний твір на задану тему для учня 8 класу: вступ, "
              "2–3 абзаци основної частини з аргументами та прикладами, висновок. Обсяг близько 200–250 слів, "
              "якщо не вказано інше. Простою живою мовою школяра, без канцеляризмів. "
              "Для української мови та літератури пиши українською. Лише текст твору."

    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("pypdf").setLevel(logging.ERROR)
log = logging.getLogger("homework_bot")
log.info("Конфігурацію токена Telegram завантажено")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=BOT_THREADS)
try:
    bot.set_my_commands([
        tg.BotCommand("hw", "Домашнє завдання із сайту школи"),
        tg.BotCommand("quiz", "Тест за темою"),
        tg.BotCommand("explain", "Пояснити, як розв’язувати"),
        tg.BotCommand("check", "Перевірити мій розв’язок"),
        tg.BotCommand("summary", "Шпаргалка за темою"),
        tg.BotCommand("essay", "Написати твір"),
        tg.BotCommand("schedule", "Розклад уроків"),
        tg.BotCommand("subject", "Обрати предмет"),
        tg.BotCommand("reset", "Очистити пам’ять діалогу"),
        tg.BotCommand("stats", "Моя статистика"),
        tg.BotCommand("top", "Рейтинг класу"),
        tg.BotCommand("cards", "Картки для запам’ятовування"),
        tg.BotCommand("proofread", "Перевірити орфографію"),
        tg.BotCommand("remind", "Нагадування про контрольну"),
        tg.BotCommand("pomodoro", "Таймер навчання"),
        tg.BotCommand("class", "Обрати клас"),
        tg.BotCommand("help", "Довідка"),
        tg.BotCommand("exam", "Пробна контрольна"),
        tg.BotCommand("formulas", "Тренажер формул"),
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
        bot.answer_callback_query(call.id, "Дякую за оцінку!" if verdict == "ok" else "Врахую, дякую.")
    except Exception:
        pass
    if verdict == "bad" and data and ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID,
                             f"👎 Погана відповідь (uid {data['uid']})\n\n"
                             f"Запитання:\n{data['task']}\n\nВідповідь:\n{data['answer'][:1500]}")
        except Exception:
            log.exception("Не вдалося переслати відгук адміністратору")
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
        bot.answer_callback_query(call.id, "Немає доступу.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Денний ліміт вичерпано. Спробуй завтра.", show_alert=True)
        return
    data = SIM_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "Кнопка застаріла, запитай ще раз.")
        return
    subject, task = data
    bot.answer_callback_query(call.id, "Придумую…")
    status = bot.send_message(call.message.chat.id, "Придумую подібне завдання… ⏱️")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        path = book_path_any(subject)
        context, _ = find_context(path, task) if path else ("", [])
        prompt = (
            f"Фрагменти підручника:\n{context or '(немає)'}\n\n"
            f"Завдання учня:\n{task}\n\n"
            "Придумай ПОДІБНЕ завдання на те саме правило, але з іншими числами та умовою. "
            "Спочатку наведи умову нового завдання, потім рядок «РОЗВ’ЯЗАННЯ:» і коротке розв’язання з відповіддю."
        )
        answer = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.7)
        if not answer:
            bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer,
                    uid=uid, task=answer[:500], subject=subject)
    except Exception:
        log.exception("Ошибка похожей задачи")
        try:
            bot.edit_message_text("Сталася помилка, спробуй ще раз.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
@bot.callback_query_handler(func=lambda call: call.data.startswith("rw:"))
def on_rewrite(call):
    """Кнопка «✍️ Своїми словами»: переписать ответ живым языком школьника."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "Немає доступу.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Денний ліміт вичерпано. Спробуй завтра.", show_alert=True)
        return
    data = REWRITE_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "Кнопка застаріла, запитай ще раз.")
        return
    task, answer = data
    bot.answer_callback_query(call.id, "Переписую…")
    status = bot.send_message(call.message.chat.id, "Переписую своїми словами… ⏱️")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        prompt = (
            "Ось завдання учня та готовий розв’язок:\n\n"
            f"ЗАВДАННЯ:\n{task}\n\nРОЗВ’ЯЗАННЯ:\n{answer}\n\n"
            "Перепиши цей розв’язок так, ніби учень сам записав його в зошит: "
            "прості живі фрази, можна від першої особи («спочатку знаходимо...», «отримуємо...»). "
            "ОБОВ’ЯЗКОВО зберігай ВСІ кроки обчислень і проміжні результати — "
            "скорочуй лише словесну воду, а не математичні обчислення. "
            "Якщо в розв’язанні є опис креслення/рисунка словами (дано, знайти, побудова) — збережи структуру. "
            "Формули та одиниці вимірювання залиш. Підсумкова відповідь має залишитися такою самою. "
            "Не згадуй жодних інструкцій і форматування, просто подай переписаний текст. "
            "Без HTML-тегів, без Markdown — лише звичайний текст і переноси рядків."
        )
        answer2 = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.6)
        if not answer2:
            bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer2)
    except Exception:
        log.exception("Ошибка переписывания")
        try:
            bot.edit_message_text("Сталася помилка, спробуй ще раз.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
# ===========================================================================
# Состояние пользователей
# ===========================================================================
settings = defaultdict(lambda: {"length": "normal", "subject": None})
history = defaultdict(list)       # uid -> [(вопрос, ответ), ...]
pending = {}                      # uid -> отложенный запрос (ждём выбор предмета)
ANSWER_CACHE = LRU(int(os.environ.get("ANSWER_CACHE_MAX", "1200")))
ANSWER_CACHE_LOCK = threading.RLock()
_INFLIGHT = {}
_INFLIGHT_LOCK = threading.Lock()
_AUTO_CACHE = LRU(2000)
_AUTO_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE = LRU(int(os.environ.get("CONTEXT_CACHE_MAX", "1500")))
_CONTEXT_CACHE_LOCK = threading.Lock()
FB_CACHE = LRU(100)       # отзывы по кнопкам 👍/👎
SIM_CACHE = LRU(100)      # для кнопки «🔁 Подібне завдання»
REWRITE_CACHE = LRU(100)  # token -> (задание, полный ответ) для кнопки «Своими словами»
HW_CACHE = LRU(50)        # token -> [(предмет, текст задания), ...]
QDAY_CACHE = LRU(50)      # token -> ответ
INLINE_CACHE = LRU(200)
last_request = {}                 # uid -> время последнего запроса
START_TIME = time.time()
FB_LOG = []            # даты всех 👎 — для ежедневного отчёта
REMINDS_MEM = []       # напоминания, если Redis не подключён
POMODORO = {}          # uid -> таймеры помидорки
HW_DONE = {}           # token -> {номера выполненных заданий}
DOCS = LRU(50)         # uid -> {"text": ..., "question": ...} контекст учителя

# ===========================================================================
# Статистика и лимиты
# ===========================================================================
KV_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
KV_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
log.info("Redis (Upstash): %s", "подключён" if (KV_URL and KV_TOKEN) else "НЕ подключён — расписания/статистика только в памяти!")


def kv_enabled():
    return bool(KV_URL and KV_TOKEN)


def _kv(cmd):
    r = requests.post(KV_URL, headers={"Authorization": f"Bearer {KV_TOKEN}"}, json=cmd, timeout=3)
    r.raise_for_status()
    return r.json().get("result")


def kv_load(key, default):
    if not kv_enabled():
        return default
    for attempt in range(3):
        try:
            raw = _kv(["GET", key])
            return json.loads(raw) if raw else default
        except Exception:
            log.warning("Redis: не удалось прочитать %s (попытка %d из 3)", key, attempt + 1)
            time.sleep(2)
    log.error("Redis недоступен: %s не будет сохраняться до перезапуска бота", key)
    return default


def kv_save(key, value):
    if not kv_enabled():
        return False
    try:
        _kv(["SET", key, json.dumps(value, ensure_ascii=False)])
        return True
    except Exception:
        log.exception("Redis: не удалось сохранить %s", key)
        return False


STATS_FILE = os.path.join(DATA_DIR, "stats.json")
_stats_lock = threading.Lock()


def _load_local_stats():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


def _load_stats():
    if kv_enabled():
        try:
            remote = kv_load("hwbot:stats", None)
            if remote is not None:
                return remote
        except Exception:
            log.warning("Redis: не удалось загрузить статистику, используем локальную")
        local = _load_local_stats()
        if local.get("users"):
            try:
                if kv_save("hwbot:stats", local):
                    log.info("Перенесена локальная статистика в Redis")
            except Exception:
                pass
        return local
    return _load_local_stats()


stats = _load_stats()
_stats_dirty = False
_stats_last_flush = 0.0
_STATS_FLUSH_SECONDS = 15

def _flush_stats(force=False):
    global _stats_dirty, _stats_last_flush
    now = time.time()
    if not _stats_dirty or (not force and now - _stats_last_flush < _STATS_FLUSH_SECONDS):
        return
    snapshot = None
    with _stats_lock:
        if not _stats_dirty:
            return
        snapshot = json.loads(json.dumps(stats, ensure_ascii=False))
        _stats_dirty = False
        _stats_last_flush = now
    try:
        if kv_enabled():
            if not kv_save("hwbot:stats", snapshot):
                log.warning("Redis: не удалось сохранить статистику")
        else:
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATS_FILE)
    except Exception:
        with _stats_lock:
            _stats_dirty = True
        log.exception("Не удалось сохранить статистику")

def register_use(uid, name):
    """Атомарный счётчик через Redis (HINCRBY) + локальный fallback. Всегда обновляет локальную статистику."""
    day = datetime.now(KYIV).date().isoformat()
    k = f"hwbot:day:{day}"
    if kv_enabled():
        try:
            _kv(["HINCRBY", k, str(uid), 1])
            _kv(["EXPIRE", k, 90 * 86400])
        except Exception:
            log.exception("kv_register")
    # Локальный fallback (всегда, чтобы /top, /stats и отчёты админу работали)
    global _stats_dirty
    with _stats_lock:
        u = stats["users"].setdefault(
            str(uid), {"name": name, "total": 0, "days": {}}
        )
        u["name"] = name
        u["total"] += 1
        u["days"][day] = u["days"].get(day, 0) + 1
        _stats_dirty = True
    if time.time() - _stats_last_flush >= _STATS_FLUSH_SECONDS:
        threading.Thread(target=_flush_stats, daemon=True).start()


def used_today(uid):
    day = datetime.now(KYIV).date().isoformat()
    k = f"hwbot:day:{day}"
    try:
        if kv_enabled():
            val = _kv(["HGET", k, str(uid)])
            return int(val) if val else 0
    except Exception:
        pass
    u = stats["users"].get(str(uid))
    return u["days"].get(day, 0) if u else 0


def limit_reached(uid):
    return uid != ADMIN_ID and used_today(uid) >= DAILY_LIMIT

# ===========================================================================
# Атомарные одноразовые флаги (Redis SET NX EX)
# ===========================================================================
_once_mem = set()                 # локальные одноразовые флаги (если Redis не подключён)


def once(name):
    """Возвращает True ровно один раз в календарный день (по Киеву).

    Атомарный флаг: Redis SET NX EX (выживает перезапуск),
    локальный fallback — set() в памяти.
    Используется для ежедневных рассылок/отчётов/вопроса дня.
    """
    key = f"hwbot:once:{name}:{datetime.now(KYIV).date().isoformat()}"
    if not kv_enabled():
        if key in _once_mem:
            return False
        _once_mem.add(key)
        return True
    try:
        return _kv(["SET", key, "1", "NX", "EX", 172800]) == "OK"
    except Exception:
        return True  # при сбое Redis — не блокируем


def _load_flag(key, default):
    """Чтение одноразового флага (совместимость)."""
    if kv_enabled():
        return kv_load(key, default)
    return key in _once_mem if _once_mem else default


def _save_flag(key, value):
    """Запись одноразового флага (совместимость)."""
    if kv_enabled():
        kv_save(key, value)
    else:
        _once_mem.add(key)


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
    """Убираем теги: сначала раскрываем HTML-сущности, потом обрезаем теги."""
    return re.sub(r"<[^>]+>", "", html.unescape(text or ""))


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


def send_answer(chat_id, status_msg, text, uid=None, task=None, subject=None):
    """Сначала пробует HTML; если Telegram не принял разметку - шлёт чистым текстом.
    Кнопки (👍/👎, 🔁 Подібне, ✍️ Своїми словами) добавляются только под последним куском ответа."""
    token = uuid.uuid4().hex[:8]
    markup = InlineKeyboardMarkup()
    if uid is not None:
        FB_CACHE.set(token, {"uid": uid, "task": (task or "")[:500], "answer": text[:2000]})
        if subject and task:
            SIM_CACHE.set(token, (subject, task[:500]))
            REWRITE_CACHE.set(token, (task[:1500], text[:2500]))
            markup.add(InlineKeyboardButton("🔁 Подібне завдання", callback_data=f"similar:{token}"))
            markup.add(InlineKeyboardButton("✍️ Своїми словами", callback_data=f"rw:{token}"))
        markup.row(InlineKeyboardButton("👍", callback_data=f"fb:ok:{token}"),
                   InlineKeyboardButton("👎", callback_data=f"fb:bad:{token}"))
    markup.add(InlineKeyboardButton("🗑", callback_data="delete_msg"))

    chunks = split_message(sanitize_html(text))
    for n, chunk in enumerate(chunks):
        first, m = n == 0, (markup if n == len(chunks) - 1 else None)
        try:
            if first:
                bot.edit_message_text(chunk, chat_id, status_msg.message_id, parse_mode="HTML", reply_markup=m)
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=m)
            continue
        except Exception as e:
            log.warning("HTML не прошёл, отправляю без разметки: %s", e)
        plain = strip_tags(chunk)
        try:
            if first:
                bot.edit_message_text(plain, chat_id, status_msg.message_id, reply_markup=m)
            else:
                bot.send_message(chat_id, plain, reply_markup=m)
        except Exception:
            bot.send_message(chat_id, plain, reply_markup=m)

# ===========================================================================
# PDF: загрузка, кэш, поиск нужных страниц
# ===========================================================================
_pages_cache = {}
_index_cache = {}
_load_lock = threading.Lock()
_index_lock = threading.Lock()


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
        stat = os.stat(pdf_path)
        size = stat.st_size
        mtime_ns = stat.st_mtime_ns

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
                json.dump({"size": size, "mtime_ns": mtime_ns, "pages": list(pages)}, f, ensure_ascii=False)
        except Exception:
            log.warning("Не удалось сохранить кэш для %s", name)

        _pages_cache[pdf_path] = pages
        return pages


@lru_cache(maxsize=8192)
def normalize_task(text):
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


@lru_cache(maxsize=8192)
def stems(text):
    """Грубая основа слова (первые 5 букв) - помогает с падежами."""
    return tuple(w[:5] for w in re.findall(r"[a-zа-яёіїєґ0-9]{3,}", text.lower()))


def page_index(pdf_path):
    idx = _index_cache.get(pdf_path)
    if idx is not None:
        return idx
    with _index_lock:
        idx = _index_cache.get(pdf_path)
        if idx is None:
            idx = [Counter(stems(p)) for p in load_pages(pdf_path)]
            _index_cache[pdf_path] = idx
    return idx


_idf_cache = {}
_idf_lock = threading.Lock()


def _idf(path):
    """BM25-подобный IDF для каждого термина."""
    with _idf_lock:
        if path not in _idf_cache:
            idx = page_index(path)
            n = len(idx)
            df = Counter(s for c in idx for s in c)
            _idf_cache[path] = {
                s: math.log(1 + (n - d + 0.5) / (d + 0.5))
                for s, d in df.items()
            }
        return _idf_cache[path]


def score_pages(pdf_path, task, use_numbers=True):
    pages = load_pages(pdf_path)
    index = page_index(pdf_path)
    query = set(stems(task))
    idf = _idf(pdf_path)
    numbers = re.findall(r"\d+(?:[.,]\d+)?", task) if use_numbers else []
    scored = []
    for i, counts in enumerate(index):
        # BM25-подобный вес: IDF * минимальная частота (cap 5)
        score = sum(idf.get(s, 0) * min(counts[s], 5) for s in query)
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
    key = (pdf_path, normalize_task(task), TOP_PAGES, MAX_CONTEXT_CHARS)
    with _CONTEXT_CACHE_LOCK:
        hit = _CONTEXT_CACHE.get(key)
        if hit is not None:
            return hit
    pages = load_pages(pdf_path)
    best = sorted(i for sc, i in score_pages(pdf_path, task)[:TOP_PAGES] if sc >= 5)
    chunks, used, total = [], [], 0
    for i in best:
        chunk = f"[Страница {i + 1}]\n{pages[i]}"
        if total + len(chunk) > MAX_CONTEXT_CHARS:
            break
        chunks.append(chunk); used.append(i + 1); total += len(chunk)
    result = ("\n\n".join(chunks), used)
    _CONTEXT_CACHE.set(key, result)
    return result


def auto_detect(task):
    key = normalize_task(task)
    hit = _AUTO_CACHE.get(key)
    if hit is not None:
        return hit
    best, best_score = None, 0
    for key_name, fname in SUBJECTS.items():
        path = os.path.join(BOOKS_DIR, fname)
        if not os.path.exists(path):
            continue
        score = sum(sc for sc, _ in score_pages(path, task, use_numbers=False)[:3])
        if score > best_score:
            best, best_score = key_name, score
    result = best if best_score >= 5 else None
    _AUTO_CACHE.set(key, result)
    return result


def _preload_one(item):
    key, fname = item
    path = os.path.join(BOOKS_DIR, fname)
    if not os.path.exists(path):
        return key, 0, 0.0, False
    t = time.perf_counter()
    n = len(load_pages(path))
    # Сразу строим индекс, чтобы первый пользователь не платил за его создание.
    page_index(path)
    return key, n, time.perf_counter() - t, True


def preload_books():
    """Параллельно прогревает PDF и индексы при старте, не блокируя polling."""
    items = list(SUBJECTS.items())
    workers = max(1, min(PRELOAD_WORKERS, len(items)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf-preload") as pool:
        for key, n, elapsed, ok in pool.map(_preload_one, items):
            if ok:
                log.info("Учебник '%s' готов: %d стр., %.1f сек", key, n, elapsed)
            else:
                log.warning("Файл не найден: %s", os.path.join(BOOKS_DIR, SUBJECTS[key]))


# ===========================================================================
# Gemini (несколько ключей с ротацией)
# ===========================================================================
_key_blocked = {}   # ключ -> время, до которого он считается вичерпаноным
_key_lock = threading.Lock()
_clients = {}
_clients_lock = threading.Lock()
_GEMINI_SEM = threading.BoundedSemaphore(int(os.environ.get("GEMINI_CONCURRENCY", "8")))
GEMINI_RETRIES = int(os.environ.get("GEMINI_RETRIES", "1"))

def _get_client(api_key):
    client = _clients.get(api_key)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(api_key)
        if client is None:
            client = genai.Client(api_key=api_key)
            _clients[api_key] = client
    return client

def ordered_keys():
    now = time.time()
    with _key_lock:
        ready = [k for k in API_KEYS if _key_blocked.get(k, 0) <= now]
    blocked = [k for k in API_KEYS if k not in ready]
    random.shuffle(ready)
    return ready + blocked

def call_gemini(contents, system_instruction=None, json_mode=False, temperature=0.3):
    cfg = {
        "temperature": temperature,
        "max_output_tokens": int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "2048")),
        "thinking_config": gt.ThinkingConfig(thinking_budget=int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))),
    }
    if system_instruction:
        cfg["system_instruction"] = system_instruction
    if json_mode:
        cfg["response_mime_type"] = "application/json"

    last_error = None
    for api_key in ordered_keys():
        idx = API_KEYS.index(api_key) + 1
        try:
            gclient = _get_client(api_key)
        except Exception as e:
            log.warning("Ключ #%d: ошибка инициализации: %s", idx, e)
            continue

        for model in MODELS:
            for attempt in range(GEMINI_RETRIES + 1):
                t0 = time.time()
                try:
                    with _GEMINI_SEM:
                        r = gclient.models.generate_content(
                            model=model,
                            contents=contents,
                            config=gt.GenerateContentConfig(**cfg),
                        )
                    elapsed = time.time() - t0
                    # Проверяем r.text — safety или пустой ответ возвращает None без перебора ключей
                    text = r.text if hasattr(r, "text") and r.text else None
                    if text:
                        log.info("Ключ #%d, %s ответила за %.2f сек", idx, model, elapsed)
                        return text
                    log.warning("Ключ #%d, %s: пустой ответ (safety/обрезка)", idx, model)
                except Exception as e:
                    last_error = e
                    err = str(e)
                    is_daily = "PerDay" in err or "per day" in err.lower()
                    log.warning("Ключ #%d | %s | попытка %d: %s",
                                idx, model, attempt + 1, err[:200])
                    if "429" in err or "RESOURCE_EXHAUSTED" in err:
                        block_time = 3600 if is_daily else 30
                        with _key_lock:
                            _key_blocked[api_key] = time.time() + block_time
                        break
                    if any(code in err for code in ("400", "401", "403", "404")):
                        break
                    if attempt < GEMINI_RETRIES:
                        time.sleep(0.35)

    log.error("Все ключи и модели недоступны: %s", last_error)
    return None


def extract_task(media):
    """Достаёт текст задания из фото или голосового сообщения."""
    return call_gemini(
        [
            "Якщо це зображення, дослівно перепиши текст завдання (формули "
            "символами Unicode). Якщо це аудіо, розшифруй мовлення. Поверни лише "
            "текст без коментарів.",
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
        mode_text = "Дай лише підсумкову відповідь, без пояснень."
    doc_entry = DOCS.get(uid)
    doc = f"Документ від учителя:\n{doc_entry['text'][:MAX_PDF_CHARS]}\n\n" \
          if doc_entry and time.time() - doc_entry.get("ts", 0) < 1800 else ""
    return (
        f"Фрагменти підручника:\n{context or '(відповідних фрагментів не знайдено)'}\n\n"
        + doc
        + (f"Попередній діалог:\n{hist}\n\n" if hist else "")
        + f"Твоє завдання: {mode_text}\n\nПовідомлення учня: {task}"
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
    kb.add(*[tg.InlineKeyboardButton(subject_label(s), callback_data=f"subj:{s}") for s in SUBJECTS])
    kb.add(tg.InlineKeyboardButton("🔍 Визначити автоматично", callback_data="subj:auto"))
    return kb


def gate(message, cooldown=True):
    """Проверка доступа, антиспама и адресованности (в группах)."""
    uid = message.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.reply_to(message, f"У тебе немає доступу до бота. Твій ID: {uid}")
        return False
    # В группових чатах бот реагує тільки коли його прямо address (mention/reply)
    if not addressed(message):
        return False
    if cooldown:
        now = time.time()
        if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
            bot.reply_to(message, "Зачекай кілька секунд перед наступним запитом.")
            return False
        last_request[uid] = now
    return True


def _norm_name(s):
    return unicodedata.normalize("NFC", s).casefold()


def book_path_any(subject):
    """Возвращает путь к учебнику: точное совпадение по SUBJECTS, иначе поиск в папке с учётом
    нормализации Unicode/регистра (Укр літ.pdf vs Укр Літ.pdf). Никогда не подставляет другой предмет."""
    fname = SUBJECTS.get(subject)
    if not fname:
        return None
    exact = os.path.join(BOOKS_DIR, fname)
    if os.path.isfile(exact):
        return exact
    try:
        want = _norm_name(fname)
        for f in os.listdir(BOOKS_DIR):
            if _norm_name(f) == want:
                return os.path.join(BOOKS_DIR, f)
    except OSError:
        pass
    return None


# ===========================================================================
# Основной конвейер
# ===========================================================================

    # Словник із твоїм розкладом 8-Б

    # ===========================================================================
    # Розклад и обработчики домашки (ВЫНЕСЕНО ИЗ PROCESS НАВЕРХ)
    # ===========================================================================

# Класы школы: имя -> ID на сайте sch3.edu.vn.ua (только 8-Б)
CLASSES = {
"8-Б": "24",
}
DEFAULT_CLASS = "8-Б"
# Розклад 8-Б виведений із реальних домашніх завдань (листопад 2026)
SCHEDULES = {
    "8-Б": {
        "0": ["Географія", "Підприємництво і фінансова грамотність", "Біологія", "Фізика і астрономія", "Геометрія", "Українська література"],
        "1": ["Інформатика", "Фізика і астрономія", "Українська мова", "Історія України", "Алгебра", "Англійська мова"],
        "2": ["Технології", "Алгебра", "Англійська мова", "Інформатика", "Зарубіжна література", "Здоров'я, безпека та добробут"],
        "3": ["Хімія", "Українська мова", "Англійська мова", "Географія", "Українська література", "Історія України", "Фізична культура"],
        "4": ["Англійська мова", "Українська мова", "Алгебра", "Історія України", "Зарубіжна література"],
    }
}
@bot.message_handler(commands=['hw'])
def hw_menu(message):
    if not gate(message, cooldown=False):
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("📅 На завтра", callback_data="hw_auto"))
    markup.row(InlineKeyboardButton("ПН", callback_data="hw_0"),
               InlineKeyboardButton("ВТ", callback_data="hw_1"),
               InlineKeyboardButton("СР", callback_data="hw_2"))
    markup.row(InlineKeyboardButton("ЧТ", callback_data="hw_3"),
               InlineKeyboardButton("ПТ", callback_data="hw_4"))
    markup.add(InlineKeyboardButton("🗑 Закрити", callback_data="delete_msg"))
    bot.send_message(message.chat.id, "На який день зібрати домашнє завдання?", reply_markup=markup)

@bot.message_handler(commands=["schedule"])
def cmd_schedule(message):
    if not gate(message, cooldown=False):
        return
    class_name = user_class(message.chat.id)
    sched = SCHEDULES.get(class_name)
    if not sched:
        bot.reply_to(message, f"Розклад для {html.escape(class_name)} поки не додано, "
                              "але домашнє завдання працює: /hw")
        return
    names = ["Понеділок", "Вівторок", "Середа", "Четвер", "П’ятниця"]
    text = f"📅 <b>Розклад {html.escape(class_name)}</b>\n\n" + "\n\n".join(
        f"<b>{names[int(d)]}</b>\n"
        + "\n".join(f"{i}. {html.escape(s)}" for i, s in enumerate(lessons, 1))
        for d, lessons in sched.items()
    )
    bot.send_message(message.chat.id, text, parse_mode="HTML")


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) homework-bot/2.0',
    'Accept-Encoding': 'gzip, deflate',
    'Connection': 'keep-alive',
}
_http_local = threading.local()

def http_session():
    session = getattr(_http_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _http_local.session = session
    return session

# ===========================================================================
# Автоматичне розкладання: розклад задано в коді,/site не використовується
# ===========================================================================
def _merge_classes_and_schedules():
    pass


def fetch_task_text(url):
    """Достаёт текст после заголовка 'Домашнє завдання' до новостей/подвала сайта.
    Результат кэшируется 10 минут, чтобы не дёргать сайт повторно."""
    cached = _TASK_CACHE.get(url)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    try:
        r = http_session().get(url, timeout=HTTP_TIMEOUT)
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

        result = text[:cut].strip()
        _TASK_CACHE[url] = (time.time(), result)
        # _TASK_CACHE — упрощённый dict (TTL 10 мин, редкие вызовы), без LRU
        return result
    except Exception:
        log.exception("Не удалось открыть %s", url)
        return ""

_TASK_CACHE = {}  # url -> (ts, text) — кэш fetch_task_text на 10 мин


def is_future(lesson_date):
    """True, если дата урока (дд.мм.гггг) позже сегодняшней по Киеву."""
    try:
        d = datetime.strptime(lesson_date.strip(), "%d.%m.%Y").date()
        return d > datetime.now(KYIV).date()
    except ValueError:
        return False  # если дату не разобрать, задание не скрываем
DAY_NAMES = ["понеділок", "вівторок", "середу", "четвер", "п’ятницю"]


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
    "Англійська мова": "none",
    "Біологія": "биология",
    "Географія": "география",
    "Геометрія": "геометрия",
    "Зарубіжна література": "зарубежка",
    "Історія України": "история",
    "Мистецтво": "музыка",
    "Українська література": "укрлит",
    "Українська мова": "укрмов",
    "Фізика і астрономія": "физика",
    "Хімія": "химия",
    "Підприємництво і фінансова грамотність": "entrepreneurship",
    "Технології": "technology",
    "Здоров'я, безпека та добробут": "none",
}
# Латинские буквы, похожие на кириллицу (A B C E H K M O P T X), приводим к кириллице.
# Защита от невидимого бага: "6-В" в CLASSES и SCHEDULES набраны разными алфавитами.
_HOMO = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")


def canon_class(name):
    """Каноничное имя класса: верхний регистр, кириллица, без дефисов и пробелов. '6-В' == '6В'."""
    return name.upper().translate(_HOMO).replace("-", "").replace("–", "").replace(" ", "").strip()


CLASSES = {canon_class(k): v for k, v in CLASSES.items()}
SCHEDULES = {canon_class(k): v for k, v in SCHEDULES.items()}
DEFAULT_CLASS = canon_class(DEFAULT_CLASS)


def _validate_classes():
    for _name in SCHEDULES:
        if _name not in CLASSES:
            log.warning("SCHEDULES: класс %r отсутствует в CLASSES - проверь буквы (кириллица/latinica)!", _name)


_validate_classes()


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
    HW_CACHE.set(token, items)
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

    hw_text = f"📚 <b>Домашнє завдання {html.escape(class_name)} на {DAY_NAMES[int(day_index)]}:</b>\n\n"
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

    bot.edit_message_text("Сканую сайт школи… 🕵️‍♂️", chat_id, msg_id)

    try:
        hw_text, items = build_hw_text(day_index, user_class(chat_id))
        if not hw_text:
            bot.edit_message_text("На цей день свіжих завдань на сайті поки немає.", chat_id, msg_id)
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
        bot.edit_message_text(f"Сайт недоступний. Помилка: {e}", chat_id, msg_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("solve:"))
def on_solve(call):
    """Кнопка '✏️ Предмет' под домашкой: отправляет задание на решение."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "Немає доступу.", show_alert=True)
        return
    try:
        _, token, idx = call.data.split(":")
        items = HW_CACHE.get(token)
        if not items:
            bot.answer_callback_query(call.id, "Список застарів, відкрий /hw ще раз.", show_alert=True)
            return
        subject_ua, task_text = items[int(idx)]
    except Exception:
        bot.answer_callback_query(call.id, "Список застарів, відкрий /hw ще раз.", show_alert=True)
        return
    now = time.time()
    if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
        bot.answer_callback_query(call.id, "Зачекай кілька секунд.")
        return
    last_request[uid] = now

    keys = SUBJECT_KEYS
    subject = keys.get(subject_ua, "none")
    bot.answer_callback_query(call.id, "Розв'язую…")
    task = f"{subject_ua}. Домашнє завдання: {task_text[:1500]}"
    # Если в задании только номера без текста - предупредим, что условия могут не найтись
    if re.search(r"[№N]\s*№?\s*\d+|\d{1,3}\s*[,.-]\s*\d{1,3}", task_text) and len(task_text) < 120:
        bot.send_message(call.message.chat.id,
                         f"ℹ️ У завданні лише номери ({subject_ua}). Якщо в умові не вистачить даних — "
                         "надішли фото сторінки підручника з цими номерами — розв’яжу точно.")
    process(call.message.chat.id, call.message.message_id, uid,
            call.from_user.first_name, "answer", subject, task, [])

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwdone:"))
def on_hw_done(call):
    """Кнопка ✅ под домашкой: отметить задание выполненным."""
    try:
        _, token, idx = call.data.split(":")
        idx = int(idx)
        items = HW_CACHE.get(token)
    except Exception:
        bot.answer_callback_query(call.id, "Список застарів, відкрий /hw ще раз.", show_alert=True)
        return
    if not items:
        bot.answer_callback_query(call.id, "Список застарів, відкрий /hw ще раз.", show_alert=True)
        return
    done = HW_DONE.setdefault(token, set())
    if idx in done:
        done.discard(idx)
        msg = "Повернув до списку."
    else:
        done.add(idx)
        left = sum(1 for i, (s, t) in enumerate(items) if t and i not in done)
        msg = "Усе виконано! 🎉" if left == 0 else f"Молодець! 💪 Залишилося: {left}"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=_hw_kb(token, items))
    except Exception:
        pass
    bot.answer_callback_query(call.id, msg)


# ===========================================================================
# Ежедневная розсилка домашнього завдання
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
    """Клас чата с защитой от удалённых/переименованных классов."""
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
    """Не використовується — бот працює лише з 8-Б."""
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(*[InlineKeyboardButton(name, callback_data=f"setclass:{name}") for name in CLASSES])
    return kb


@bot.callback_query_handler(func=lambda call: call.data.startswith("setclass:"))
def on_setclass(call):
    name = call.data.split(":", 1)[1]
    if name not in CLASSES:
        bot.answer_callback_query(call.id, "Такого класу немає.", show_alert=True)
        return
    USER_CLASS[call.message.chat.id] = name
    class_save()
    bot.answer_callback_query(call.id, f"Клас: {name}")
    try:
        bot.edit_message_text(f"✅ Клас обрано: <b>{html.escape(name)}</b>\n"
                              "Домашнє завдання: /hw · розсилка: /subscribe · змінити: /class",
                              call.message.chat.id, call.message.message_id, parse_mode="HTML")
    except Exception:
        pass


@bot.message_handler(commands=["class"])
def cmd_class(message):
    bot.send_message(message.chat.id,
                     "⚠️ Бот працює лише з класом <b>8-Б</b>. Змінити клас неможливо.",
                     parse_mode="HTML")


@bot.message_handler(commands=["subscribe"])
def cmd_subscribe(message):
    if not gate(message, cooldown=False):
        return
    with _subs_lock:
        subs.add(message.chat.id)
        _save_subs()
    bot.reply_to(message, f"Готово! Кожного навчального дня в {SEND_HOUR}:{SEND_MINUTE:02d} "
                          "надсилатиму домашнє завдання на наступний день. Відписатися: /unsubscribe")


@bot.message_handler(commands=["unsubscribe"])
def cmd_unsubscribe(message):
    if not gate(message, cooldown=False):
        return
    with _subs_lock:
        subs.discard(message.chat.id)
        _save_subs()
    bot.reply_to(message, "Розсилку вимкнено.")


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
                bot.send_message(chat_id, "⚠️ Сайт школи зараз недоступний, отримати домашнє завдання не вдалося. "
                                          "Спробуй пізніше командою /hw.")
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
    """Рассылка: раз в сутки, в окно SEND_HOUR..SEND_HOUR+10 (по Киеву)."""
    start = SEND_HOUR * 60 + SEND_MINUTE
    while True:
        try:
            now = datetime.now(KYIV)
            minutes = now.hour * 60 + now.minute
            # окно 10 минут: если бот перезапустится позже, старую рассылку не шлём
            if now.weekday() < 5 and start <= minutes < start + 10:
                if once("broadcast"):
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
        bot.answer_callback_query(call.id, "Запит застарів, надішли завдання ще раз.")
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


def _show_subjects_keyboard(chat_id, msg_id, mode, task, media, uid):
    """Показывает клавиатуру выбора предмета и сохраняет запрос в pending."""
    pending[uid] = {"reply_to": msg_id, "mode": mode, "task": task, "media": media,
                    "uid": uid}
    bot.edit_message_text("Обери предмет:", chat_id, msg_id, reply_markup=subjects_keyboard())


def process(chat_id, reply_to, uid, name, mode, subject, task, media):
    if limit_reached(uid):
        bot.send_message(chat_id, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.",
                         reply_to_message_id=reply_to)
        return

    register_use(uid, name)
    history[uid].append((task, "PENDING"))

    status = bot.send_message(chat_id, "Шукаю відповідь у підручнику… ⏱️", reply_to_message_id=reply_to)
    bot.send_chat_action(chat_id, "typing")

    try:
        if media:
            extracted = extract_task(media)
            if extracted:
                task = f"{task}\n{extracted}".strip()
            elif not task:
                bot.edit_message_text("Не вдалося розібрати фото або голосове повідомлення. Спробуй ще раз.",
                                      chat_id, status.message_id)
                return

        auto_note = ""
        general = False  # True = відповідаємо без підручника
        if subject == "none":
            subject, general = None, True
        elif subject == "auto":
            subject = auto_detect(task)
            if subject:
                auto_note = f"📚 Предмет: {subject}\n\n"
            else:
                general = True

        if general:
            context, pages = "", []
        elif subject is None:
            _show_subjects_keyboard(chat_id, status.message_id, mode, task, media, uid)
            return
        else:
            path = book_path_any(subject)
            if not path:
                bot.edit_message_text(f"Файл підручника «{SUBJECTS[subject]}» не знайдено в папці «книги».",
                                      chat_id, status.message_id)
                return
            pages = load_pages(path)
            if not any(pages):
                log.warning("Учебник без текстового слоя (скан): %s", path)
                general = True
                context, pages = "", []
                auto_note += (f"📌 Підручник «{SUBJECTS[subject]}» виявився сканом без текстового шару, "
                              "тому розв’язую за загальними знаннями. Для точності надішли фото завдання.\n\n")
            else:
                context, pages = find_context(path, task)
        prompt = build_prompt(mode, context, task, uid)
        hist_sig = hashlib.md5(history_text(uid).encode()).hexdigest()[:8]
        cache_key = None if media else hashlib.md5(
            f"{subject}|{mode}|{settings[uid]['length']}|{normalize_task(task)}|{hist_sig}".encode()).hexdigest()
        subj_arg = subject if subject and subject != "none" else None

        def reply(text, raw=None):
            register_use(uid, name)
            history[uid].append((task, raw or text))
            if len(history[uid]) > HISTORY_TURNS:
                history[uid] = history[uid][-HISTORY_TURNS:]
            send_answer(chat_id, status, text, uid=uid, task=task, subject=subj_arg)
        flight = None
        leader = True
        if cache_key:
            with _INFLIGHT_LOCK:
                flight = _INFLIGHT.get(cache_key)
                if flight is None:
                    flight = {"event": threading.Event(), "answer": None, "error": None}
                    _INFLIGHT[cache_key] = flight
                else:
                    leader = False
            if not leader:
                got = flight["event"].wait(timeout=float(os.environ.get("INFLIGHT_TIMEOUT", "45")))
                if not got:
                    # follower тайм-аут — leader не справился, не дублируем запрос
                    with _INFLIGHT_LOCK:
                        _INFLIGHT.pop(cache_key, None)
                    raise RuntimeError("INFLIGHT timeout: leader не ответил вовремя")
                if flight["answer"] is not None:
                    reply(flight["answer"])
                    return
                if flight["error"] is not None:
                    raise flight["error"]

        try:
            answer = call_gemini([prompt, *media], system_instruction=build_system(uid), temperature=0.25)
            if not answer:
                raise RuntimeError("Gemini вернул пустой ответ")
        except Exception as exc:
            if cache_key and leader:
                with _INFLIGHT_LOCK:
                    item = _INFLIGHT.pop(cache_key, None)
                    if item:
                        item["error"] = exc
                        item["event"].set()
            raise
        full_text = auto_note + answer
        if cache_key:
            ANSWER_CACHE.set(cache_key, full_text)
            if leader:
                with _INFLIGHT_LOCK:
                    item = _INFLIGHT.pop(cache_key, None)
                    if item:
                        item["answer"] = full_text
                        item["event"].set()
        reply(full_text, answer)

    except Exception:
        log.exception("Ошибка обработки запроса")
        try:
            bot.edit_message_text("Сталася помилка. Спробуй ще раз.", chat_id, status.message_id)
        except Exception:
            pass
def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)
# ===========================================================================
# /setschedule - розклад завантажується автоматично з коду (фіксований для 8-Б)
# ===========================================================================
@bot.message_handler(commands=["setschedule"])
def cmd_setschedule(message):
    bot.reply_to(message, "⚠️ Розклад для 8-Б вже заданий автоматично в коді.\n"
                           "Доступні команди:\n"
                           "/hw — домашнє завдання\n"
                           "/schedule — показати розклад")


@bot.message_handler(commands=["addclass"])
def cmd_addclass(message):
    bot.reply_to(message, "❌ Команду /addclass вимкнено. Розклад для 8-Б заданий автоматично.")


def run_quiz(message, subject, topic):
    uid = message.from_user.id
    chat_id = message.chat.id
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Файл підручника «{SUBJECTS[subject]}» не знайдено.")
        return

    status = bot.reply_to(message, "Готую запитання… ⏱️")
    bot.send_chat_action(chat_id, "typing")
    try:
        pages = load_pages(path)
        if topic:
            context, _ = find_context(path, topic)
        else:
            rich = [p for p in pages if len(p) > 400]
            context = "\n\n".join(random.sample(rich, min(4, len(rich)))) if rich else ""
        if not context:
            bot.edit_message_text("Не знайшов відповідних сторінок за цією темою.", chat_id, status.message_id)
            return

        prompt = (
            "На основі фрагментів підручника склади 5 тестових запитань із 4 варіантами відповіді "
            "(рівно один правильний). Поверни ЛИШЕ JSON-масив такого вигляду "
            '[{"q": "запитання", "options": ["a","b","c","d"], "correct": 0, "explain": "пояснення до 150 символів"}]. '
            "Запитання пиши українською. Не використовуй HTML.\n\n"
            f"Фрагменти:\n{context}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ШІ зараз недоступний. Спробуй пізніше.", chat_id, status.message_id)
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
            bot.edit_message_text("Не вдалося скласти тест, спробуй ще раз.", chat_id, status.message_id)
    except Exception:
        log.exception("Ошибка теста")
        bot.edit_message_text("Сталася помилка під час створення тесту.", chat_id, status.message_id)


# ===========================================================================
# Команди
# ===========================================================================
HELP_TEXT = (
    "<b>Як користуватися</b>\n"
    "Пиши: <code>фізика задача 3.5</code> — або просто текст завдання (предмет запам’ятається).\n"
    "Можна надіслати <b>фото</b>, <b>голосове повідомлення</b> або <b>PDF-файл</b> від учителя.\n"
    "Щоб запитати про PDF — надішли файл із підписом: «поясни», «розв’яжи завдання 3».\n\n"
    "<b>Команди</b>\n"
    "/hw — домашнє завдання із сайту школи (є «На завтра»)\n"
    "/subscribe, /unsubscribe — розсилка домашнього завдання о 16:00\n"
    "/schedule — розклад уроків\n"
    "/class — бот працює лише з 8-Б 🏫\n"
    "/subjects — список предметів\n"
    "/subject фізика — обрати поточний предмет\n"
    "/explain [предмет] завдання — пояснити, ЯК розв’язувати\n"
    "/check [предмет] розв’язок — перевірити твій розв’язок (можна фото з підписом /check)\n"
    "/summary [предмет] тема — шпаргалка для повторення\n"
    "/quiz [предмет] [тема] — тест із 5 запитань\n"
    "/top — рейтинг класу 🏆\n"
    "Під відповіддю: 🔁 подібне завдання · ✍️ своїми словами · 👍/👎\n"
    "/exam [предмет] [тема] — пробна контрольна 📝\n"
    "/formulas [предмет] — тренажер формул 🧮\n"
    "/cards [предмет] [тема] — картки для запам’ятовування 🃏\n"
    "/cards — повторити накопичені картки\n"
    "/proofread [текст] — перевірити орфографію ✍️\n"
    "/remind 20.05 контрольна — нагадування ⏰\n"
    "/pomodoro 25 — таймер навчання/перерви 🍅\n"
    "/menu — показати кнопки основних команд\n"
    "/short, /detail, /normal — довжина відповідей\n"
    "/essay [предмет] тема — написати твір\n"
    "/quick — режим лише коротких відповідей (вийти: /normal)\n"
    "/reset — очистити пам’ять діалогу\n"
    "/stats — твоя статистика\n"
    "/id — твій Telegram ID"
)


def main_menu_keyboard():
    markup = tg.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        tg.KeyboardButton("/hw"), tg.KeyboardButton("/schedule"),
        tg.KeyboardButton("/quiz"), tg.KeyboardButton("/cards"),
        tg.KeyboardButton("/exam"), tg.KeyboardButton("/formulas"),
        tg.KeyboardButton("/remind"), tg.KeyboardButton("/pomodoro"),
        tg.KeyboardButton("/subjects"),
        tg.KeyboardButton("/help"),
    )
    return markup


@bot.message_handler(commands=["menu"])
def cmd_menu(message):
    if gate(message, cooldown=False):
        bot.send_message(
            message.chat.id,
            "Обери команду або просто надішли завдання текстом чи фото:",
            reply_markup=main_menu_keyboard(),
        )


@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    if gate(message, cooldown=False):
        bot.send_message(
            message.chat.id,
            HELP_TEXT,
            parse_mode="HTML",
            reply_markup=main_menu_keyboard(),
        )
        if message.text.strip().startswith("/start"):
            bot.send_message(message.chat.id,
                             "⚠️ Бот працює лише з класом <b>8-Б</b>.",
                             parse_mode="HTML")


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Твій Telegram ID: {message.from_user.id}")


@bot.message_handler(commands=["subjects"])
def cmd_subjects(message):
    if not gate(message, cooldown=False):
        return
    cname = user_class(message.chat.id)
    keys = SUBJECT_KEYS
    if not keys:
        bot.send_message(
            message.chat.id,
            f"📚 Для {html.escape(cname)} підручники поки не підключено — і це не заважає:\n"
            "• надішли <b>фото завдання</b> — розв'яжу й поясню\n"
            "• або напиши запитання текстом\n"
            "• працюють /hw, /cards, /quiz, /exam, /formulas\n\n"
            "Щоб я розв'язував точніше за програмою — просто надішли фото сторінки із завданням.",
            parse_mode="HTML")
        return
    bot.send_message(message.chat.id, f"Доступні предмети:\n"
                                      + "\n".join(f"• {s}" for s in SUBJECTS))


@bot.message_handler(commands=["subject"])
def cmd_subject(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cur = settings[uid]["subject"]
        bot.reply_to(message, f"Поточний предмет: {cur or 'не обрано'}. Змінити: /subject фізика")
        return
    key = resolve_subject(args[1])
    if not key:
        bot.reply_to(message, "Не знаю такого предмета. Список: /subjects")
        return
    settings[uid]["subject"] = key
    bot.reply_to(message, f"Предмет обрано: {subject_label(key)}. Тепер можна писати завдання без назви предмета.")


def _set_length(message, value, text):
    if gate(message, cooldown=False):
        settings[message.from_user.id]["length"] = value
        bot.reply_to(message, text)


@bot.message_handler(commands=["short"])
def cmd_short(message):
    _set_length(message, "short", "Режим: короткі відповіді.")


@bot.message_handler(commands=["detail"])
def cmd_detail(message):
    _set_length(message, "detail", "Режим: докладні відповіді.")


@bot.message_handler(commands=["normal"])
def cmd_normal(message):
    _set_length(message, "normal", "Режим: звичайні відповіді.")


@bot.message_handler(commands=["quick"])
def cmd_quick(message):
    _set_length(message, "quick", "Режим: лише короткі відповіді. Вийти: /normal")


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if gate(message, cooldown=False):
        uid = message.from_user.id
        history[uid].clear()
        DOCS.pop(uid, None)
        bot.reply_to(message, "Пам’ять діалогу очищено.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    u = stats["users"].get(str(uid), {"total": 0})
    text = (
        f"Безкоштовно сьогодні: {min(used_today(uid), DAILY_LIMIT)}/{DAILY_LIMIT}\n"
        f"Усього запитів: {u['total']}"
    )
    if uid == ADMIN_ID:
        rows = sorted(stats["users"].items(), key=lambda kv: kv[1]["total"], reverse=True)[:15]
        text += "\n\nУсі користувачі:\n" + "\n".join(
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
        bot.reply_to(message, "Поки порожньо — розв’яжи перше завдання!")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(str(v['name']))} — {v['total']} 🔥{streak_days(v)}"
        for i, (k, v) in enumerate(rows)
    ]
    bot.reply_to(message, "🏆 <b>Топ класу</b>\n\n" + "\n".join(lines), parse_mode="HTML")
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
            bot.reply_to(message, "Поки немає чого повторювати 🎉" + extra +
                                  "\nСтворити картки: /cards фізика <тема>")
            return
        CARD_SESSION[uid] = {"cards": all_cards, "order": order, "pos": 0,
                             "chat": message.chat.id}
        show_card(uid)
        return

    subject = resolve_subject(parts[1])
    if not subject:
        bot.reply_to(message, "Не знаю такого предмета. Список: /subjects")
        return
    topic = parts[2].strip() if len(parts) > 2 else ""
    if not topic:
        bot.reply_to(message, "Вкажи тему: /cards фізика тиск")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Файл підручника «{SUBJECTS[subject]}» не знайдено.")
        return

    status = bot.reply_to(message, "Складаю картки… ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        context, _ = find_context(path, topic)
        prompt = (
            "На основі фрагментів підручника склади 8 карток для запам’ятовування за темою "
            f"«{topic}». Поверни ЛИШЕ JSON-масив такого вигляду "
            '[{"q": "запитання або термін", "a": "коротка відповідь або визначення"}]. '
            "Запитання короткі, відповіді до 25 слів, українською, без HTML.\n\n"
            f"Фрагменти:\n{context or '(немає)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.4)
        if not raw:
            bot.edit_message_text("ШІ зараз недоступний. Спробуй пізніше.",
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
            bot.edit_message_text("Не вдалося скласти картки, спробуй ще раз.",
                                  message.chat.id, status.message_id)
            return
        cards_save(uid, all_cards)
        register_use(uid, message.from_user.first_name)
        bot.edit_message_text(
            f"✅ Добавил {added} карточек по теме «{topic}».\n"
            "Повторювати: /cards — нові картки з’являтимуться за графіком повторення.",
            message.chat.id, status.message_id)
    except Exception:
        log.exception("Ошибка карточек")
        try:
            bot.edit_message_text("Сталася помилка. Спробуй ще раз.",
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
    kb.add(InlineKeyboardButton("Показати відповідь", callback_data="card:show"))
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
    bot.answer_callback_query(call.id, "Молодець! 💪" if good else "З’явиться знову завтра.")
    if s["pos"] < len(s["order"]):
        show_card(uid)
    else:
        CARD_SESSION.pop(uid, None)
        bot.send_message(call.message.chat.id,
                         "🎉 Сесію завершено! Нажми /cards завтра — покажу следующие.")


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
                              "Або відповідай на повідомлення командою /proofread")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    status = bot.reply_to(message, "Перевіряю… ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    prompt = (
        "Перевір текст на орфографічні, пунктуаційні та граматичні помилки. "
        "Відповідай українською. Поверни:\n"
        "1) Список помилок за пунктами у форматі «неправильно → правильно (правило)».\n"
        "2) Рядок «ВИПРАВЛЕНИЙ ТЕКСТ:» і весь текст без помилок.\n"
        "Якщо помилок немає — так і скажи. Не використовуй HTML.\n\n"
        f"Текст:\n{task[:3000]}"
    )
    answer = call_gemini([prompt], system_instruction=SYSTEM_BASE)
    if not answer:
        bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
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
            bot.reply_to(message, "Нагадувань немає. Додати: /remind 20.05 контрольна з фізики")
            return
        lines = [f"• {r['due']} — {r['text']}" for r in sorted(mine, key=lambda x: x["due"])]
        bot.reply_to(message, "⏰ Нагадування:\n" + "\n".join(lines))
        return
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if not m:
        bot.reply_to(message, "Вкажи дату й що нагадати:\n/remind 20.05 контрольна з фізики\n"
                              "Показати список: /remind список")
        return
    d_, mo_, y_ = m.groups()
    year = int(y_) if y_ else _today().year
    try:
        due = datetime(year, int(mo_), int(d_)).date()
    except ValueError:
        bot.reply_to(message, "Такої дати не буває. Приклад: /remind 20.05 контрольна")
        return
    if not y_ and due < _today():
        due = datetime(year + 1, int(mo_), int(d_)).date()
    what = (text[:m.start()] + " " + text[m.end():]).strip() or "Напоминание"
    reminds = reminds_load()
    reminds.append({"chat": message.chat.id, "uid": message.from_user.id,
                    "text": what[:300], "due": due.isoformat(), "warned": False})
    reminds_save(reminds)
    bot.reply_to(message, f"👌 Записав: {what}\nНагадаю {due.strftime('%d.%m.%Y')} і за 2 дні до.")


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
                bot.send_message(r["chat"], f"⚠️ Через 2 дні: {r['text']}")
            except Exception:
                pass
            r["warned"] = True
            changed = True
            keep.append(r)
        elif left == 0:
            try:
                bot.send_message(r["chat"], f"‼️ Сьогодні: {r['text']}")
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
            bot.reply_to(message, "🍅 Таймер зупинено.")
        else:
            bot.reply_to(message, "Таймер не був запущений.")
        return
    try:
        mins = int(args[1]) if len(args) > 1 else 25
    except ValueError:
        mins = 25
    if not 5 <= mins <= 90:
        bot.reply_to(message, "Встанови від 5 до 90 хвилин: /pomodoro 25")
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
            bot.send_message(chat, f"⏰ {mins} хвилин минуло! Перерва {brk} мин ☕️")
        except Exception:
            pass
        t2 = threading.Timer(brk * 60, back_to_work)
        t2.daemon = True
        t2.start()
        POMODORO[uid]["t2"] = t2

    def back_to_work():
        POMODORO.pop(uid, None)
        try:
            bot.send_message(chat, "🍅 Перерву завершено — до навчання!")
        except Exception:
            pass

    t1 = threading.Timer(mins * 60, on_break)
    t1.daemon = True
    t1.start()
    POMODORO[uid] = {"t1": t1, "t2": None}
    bot.reply_to(message, f"🍅 Починаємо: {mins} хв навчання → {brk} хв перерви.\nЗупинити: /pomodoro stop")


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
    today_iso = now.date().isoformat()
    if now.hour < ADMIN_REPORT_HOUR or _last_report == today_iso:
        return
    if not once("hwbot:admin_report"):
        return
    _last_report = today_iso
    today = today_iso
    reqs = sum(u["days"].get(today, 0) for u in stats["users"].values())
    active = sum(1 for u in stats["users"].values() if u["days"].get(today, 0))
    redis_ok = "не підключено"
    if kv_enabled():
        try:
            _kv(["PING"])
            redis_ok = "работает"
        except Exception:
            redis_ok = "НЕ ОТВЕЧАЕТ"
    try:
        bot.send_message(ADMIN_ID,
                         f"📊 Звіт за {today}\n"
                         f"Запитів: {reqs} (активних учнів: {active})\n"
                         f"👎 поганих відповідей: {FB_LOG.count(today)}\n"
                         f"Redis: {redis_ok}\n"
                         f"Бот працює: {(time.time() - START_TIME) / 3600:.1f} ч")
    except Exception:
        log.exception("Не удалось отправить отчёт админу")


def daily_loop():
    while True:
        try:
            _check_reminds()
            _daily_admin_report()
            _question_of_the_day()
            _flush_stats()
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
        bot.reply_to(message, "Поки що вмію працювати лише з PDF. Надішли файл .pdf")
        return
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        bot.reply_to(message, "Файл більший за 20 МБ — Telegram не дозволить його завантажити.")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    status = bot.reply_to(message, "Читаю файл… ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)
        reader = PdfReader(BytesIO(data))
        text = "\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()
    except Exception:
        log.exception("Не удалось прочитать PDF")
        bot.edit_message_text("Не вдалося прочитати PDF. Спробуй надіслати фото сторінок.",
                              message.chat.id, status.message_id)
        return
    if not text:
        bot.edit_message_text("У PDF немає тексту (схоже, це скан). Надішли фото сторінок.",
                              message.chat.id, status.message_id)
        return
    question = (message.caption or "").strip()
    if not question:
        head = html.escape(text[:600])
        DOCS.set(uid, {"text": text[:MAX_PDF_CHARS], "ts": time.time()})
        bot.edit_message_text(
            f"📄 Файл прочитано. Задавай питання за ним (зберігається 30 хв).\n\n"
            f"<b>Початок документа:</b>\n{head}",
            message.chat.id, status.message_id, parse_mode="HTML")
        return
    try:
        answer = call_gemini(
            [f"Документ від учителя (може бути обрізаний):\n{text[:MAX_PDF_CHARS]}\n\n"
             f"Запитання або завдання учня за документом: {question}\n\n"
             "Відповідай за документом. Оформи структуровано, HTML-теги Telegram: <b>, <i>, <code>."],
            system_instruction=SYSTEM_BASE)
    except Exception:
        log.exception("Ошибка запроса по PDF")
        answer = None
    if not answer:
        bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
                              message.chat.id, status.message_id)
        return
    register_use(uid, message.from_user.first_name)
    send_answer(message.chat.id, status, answer, uid=uid, task=question[:500])

# ===========================================================================
# Запитання дня (шлём в 18:00 всем подписчикам рассылки)
# ===========================================================================
QDAY_HOUR = 18
_qday_last = None


@bot.callback_query_handler(func=lambda call: call.data.startswith("qday:"))
def on_qday_answer(call):
    token = call.data.split(":", 1)[1]
    answer = QDAY_CACHE.get(token)
    bot.answer_callback_query(call.id)
    if answer:
        bot.send_message(call.message.chat.id, f"💡 {html.escape(answer)}")
    else:
        bot.send_message(call.message.chat.id, "Відповідь застаріла (бот перезапускався).")


def _question_of_the_day():
    now = datetime.now(KYIV)
    if now.hour < QDAY_HOUR or not once("qday"):
        return
    subjects = [s for s in SUBJECTS if book_path_any(s)]
    if not subjects:
        return
    subj = random.choice(subjects)
    pages = [p for p in load_pages(book_path_any(subj)) if len(p) > 400]
    if not pages:
        return
    page = random.choice(pages)
    raw = call_gemini(
        "Придумай за фрагментом підручника ОДНЕ цікаве запитання для учня 8 класу "
        "і коротку відповідь на нього (до 40 слів). Поверни ЛИШЕ JSON такого вигляду "
        '{"q": "запитання", "a": "відповідь"}.\n\n'
        f"Фрагмент підручника ({subj}):\n{page[:3000]}",
        json_mode=True, temperature=0.8)
    if not raw:
        log.warning("Запитання дня: ИИ не ответил")
        return
    try:
        it = parse_json(raw)
        q, a = str(it["q"]).strip(), str(it["a"]).strip()
    except Exception:
        return
    if not q or not a:
        return
    token = uuid.uuid4().hex[:8]
    QDAY_CACHE.set(token, a)
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("💡 Показати відповідь", callback_data=f"qday:{token}"))
    text = f"❓ <b>Запитання дня</b> ({html.escape(subj)})\n\n{html.escape(q)}"
    for chat_id in broadcast_targets():
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("Запитання дня: не отправилось в %s", chat_id)
# ===========================================================================
# /exam - пробна контрольна
def _alive(d, uid, ttl=1800):
    """Проверка и очистка устаревших записей состояния с TTL."""
    s = d.get(uid)
    if s and time.time() - s.get("ts", 0) > ttl:
        d.pop(uid, None)
        return False
    return bool(s)


# ===========================================================================
EXAM_QCOUNT = 5
EXAM = {}   # uid -> {"items", "answers", "pos", "chat", "subject", "ts"}
EXAM_TTL = 1800   # 30 хвилин: якщо учень не продовжує — контрольна видаляється


@bot.message_handler(commands=["exam"])
def cmd_exam(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=2)
    if len(args) > 1 and args[1].lower() in ("стоп", "stop"):
        if EXAM.pop(uid, None):
            bot.reply_to(message, "Контрольну перервано. Почати знову: /exam фізика")
        else:
            bot.reply_to(message, "Контрольна ще не розпочиналася. Почати: /exam фізика")
        return
    if _alive(FORMULA, uid):
        bot.reply_to(message, "Спочатку заверши тренажер формул (/formulas стоп).")
        return
    if _alive(EXAM, uid):
        bot.reply_to(message, "Контрольна вже триває — відповідай на поточне запитання або /exam стоп.")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Вкажи предмет: /exam фізика [тема]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Файл підручника «{SUBJECTS[subject]}» не знайдено.")
        return
    status = bot.reply_to(message, "Складаю варіант контрольної… ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"На основі фрагментів підручника склади контрольну з {EXAM_QCOUNT} завдань "
            "рівня 8 класу з КОРОТКИМИ відповідями (число, слово, формула або 1 речення). "
            'Поверни ЛИШЕ JSON-масив такого вигляду [{"q": "текст завдання", "a": "коротка еталонна відповідь"}]. '
            "Завдання українською, різної складності, без HTML.\n\n"
            f"Фрагменти:\n{context or '(немає)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("q") and it.get("a")][:EXAM_QCOUNT]
        if len(items) < 3:
            bot.edit_message_text("Не вдалося скласти варіант, спробуй ще раз.",
                                  message.chat.id, status.message_id)
            return
        EXAM[uid] = {"items": items, "answers": [], "pos": 0, "ts": time.time(),
                     "chat": message.chat.id, "subject": subject}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _exam_ask(uid)
    except Exception:
        log.exception("Ошибка /exam")
        try:
            bot.edit_message_text("Сталася помилка. Спробуй ще раз.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _exam_ask(uid):
    s = EXAM.get(uid)
    if not s:
        return
    q = s["items"][s["pos"]]["q"]
    text = (f"📝 <b>Контрольна</b> ({html.escape(subject_label(s['subject']))}) · "
            f"завдання {s['pos'] + 1} із {len(s['items'])}\n\n"
            f"{html.escape(str(q))}\n\n"
            "<i>Напиши відповідь наступним повідомленням. Перервати: /exam стоп</i>")
    bot.send_message(s["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: _alive(EXAM, m.from_user.id)
                      and not m.text.startswith("/")
                      and m.chat.id == EXAM[m.from_user.id]["chat"],
                      content_types=["text"])
def on_exam_answer(message):
    uid = message.from_user.id
    s = EXAM[uid]
    s["ts"] = time.time()   # живим TTL при кожній відповіді
    s["answers"].append(message.text.strip()[:500])
    s["pos"] += 1
    if s["pos"] < len(s["items"]):
        _exam_ask(uid)
        return
    # все ответы собраны - одна проверка на все задания
    bot.send_chat_action(s["chat"], "typing")
    status = bot.send_message(s["chat"], "Перевіряю роботу… ⏱️")
    qa = "\n\n".join(
        f"{i + 1}. Завдання: {it['q']}\nЕталон: {it['a']}\nВідповідь учня: {a or '(немає відповіді)'}"
        for i, (it, a) in enumerate(zip(s["items"], s["answers"])))
    prompt = (
        "Перевір контрольну учня. За кожне завдання постав бал від 0 до 2 "
        "(0 — неправильно, 1 — частково правильно, 2 — правильно; зараховуй відповіді, що збігаються "
        "за змістом, навіть якщо формулювання інше). Поверни ЛИШЕ JSON: "
        '[{"n": 1, "score": 2, "comment": "короткий коментар до 100 символов"}].\n\n' + qa)
    raw = call_gemini(prompt, json_mode=True, temperature=0.1)
    EXAM.pop(uid, None)
    if not raw:
        bot.edit_message_text("ШІ зараз недоступний — роботу не перевірено. "
                              "Спробуй пізніше ще раз: /exam",
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
                     f"Завдання {g.get('n', len(lines) + 1)}: {sc}/2 — "
                     f"{html.escape(str(g.get('comment', '')))}")
    while len(lines) < len(s["items"]):
        lines.append(f"❌ Завдання {len(lines) + 1}: 0/2 — не перевірено")
    mx = 2 * len(s["items"])
    grade = max(1, min(12, round((earned / mx if mx else 0) * 12)))
    verdict = ("🎉 Чудова робота!" if grade >= 10
               else "👍 Добре, але є куди рости." if grade >= 7
               else "📖 Матеріал треба повторити: /summary або /cards допоможуть.")
    text = (f"📝 <b>Результат контрольної</b> ({html.escape(subject_label(s['subject']))})\n\n"
            + "\n".join(lines)
            + f"\n\n<b>Бали: {earned}/{mx} · Оцінка: {grade}/12</b>\n{verdict}")
    try:
        bot.edit_message_text(text, s["chat"], status.message_id, parse_mode="HTML")
    except Exception:
        bot.send_message(s["chat"], text, parse_mode="HTML")


# ===========================================================================
# /formulas - тренажер формул
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
            bot.reply_to(message, "Тренажер зупинено.")
        else:
            bot.reply_to(message, "Тренажер ще не запускався. Почати: /formulas фізика")
        return
    if _alive(EXAM, uid):
        bot.reply_to(message, "Спочатку заверши контрольну (/exam стоп).")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Вкажи предмет: /formulas фізика [тема]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Безкоштовний денний ліміт ({DAILY_LIMIT}) вичерпано. Спробуй завтра.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Файл підручника «{SUBJECTS[subject]}» не знайдено.")
        return
    status = bot.reply_to(message, "Обираю формули… ⏱️")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"На основі фрагментів підручника вибери {FORMULA_ROUNDS} формул або правил "
            "рівня 8 класу. Поверни ЛИШЕ JSON-масив такого вигляду "
            '[{"term": "назва величини або правила", "formula": "сама формула текстом"}]. '
            "Без HTML.\n\n"
            f"Фрагменти:\n{context or '(немає)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("ШІ зараз недоступний, спробуй пізніше.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("term") and it.get("formula")][:FORMULA_ROUNDS]
        if len(items) < 3:
            bot.edit_message_text("Не вдалося зібрати формули, спробуй ще раз.",
                                  message.chat.id, status.message_id)
            return
        FORMULA[uid] = {"items": items, "pos": 0, "right": 0,
                        "chat": message.chat.id, "subject": subject, "ts": time.time()}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _formula_ask(uid)
    except Exception:
        log.exception("Ошибка /formulas")
        try:
            bot.edit_message_text("Сталася помилка. Спробуй ще раз.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _formula_ask(uid):
    f = FORMULA.get(uid)
    if not f:
        return
    it = f["items"][f["pos"]]
    text = (f"🧮 <b>Тренажер формул</b> ({html.escape(subject_label(f['subject']))}) · "
            f"{f['pos'] + 1}/{len(f['items'])} · правильно: {f['right']}\n\n"
            f"Напиши формулу: <b>{html.escape(str(it['term']))}</b>\n\n"
            "<i>У будь-якому записі: E=mv²/2 или E = m*v^2/2. Вийти: /formulas стоп</i>")
    bot.send_message(f["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: _alive(FORMULA, m.from_user.id)
                      and not m.text.startswith("/")
                      and m.chat.id == FORMULA[m.from_user.id]["chat"],
                      content_types=["text"])
def on_formula_answer(message):
    uid = message.from_user.id
    f = FORMULA[uid]
    f["ts"] = time.time()   # живим TTL при кожній відповіді
    it = f["items"][f["pos"]]
    status = bot.reply_to(message, "Перевіряю… ⏱️")
    prompt = (
        f"Величина: {it['term']}\nЕталонна формула: {it['formula']}\n"
        f"Відповідь учня: {message.text[:200]}\n\n"
        "Формули вважаються однаковими, якщо збігається зміст (різні позначення та форма "
        "запису допустимі). Поверни ЛИШЕ JSON: "
        '{"match": true/false, "correct": "правильна формула текстом"}')
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
            mark = f"❌ Не те. Правильно: <b>{html.escape(correct)}</b>"
        try:
            bot.edit_message_text(mark, message.chat.id, status.message_id, parse_mode="HTML")
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
    else:
        try:
            bot.edit_message_text("ШІ недоступний, раунд пропущено.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass
    if f["pos"] < len(f["items"]):
        _formula_ask(uid)
    else:
        FORMULA.pop(uid, None)
        r, n = f["right"], len(f["items"])
        bot.send_message(f["chat"], f"🏁 Підсумок: {r} із {n}. "
                         + ("💪 Чудово!" if r == n else "Ще одна спроба: /formulas"))

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


bot.message_handler(commands=["explain"])(_mode_command("explain", "Приклад: /explain фізика задача 3.5"))
bot.message_handler(commands=["check"])(_mode_command("check", "Приклад: /check алгебра мій розв’язок: x = 5... (або надішли фото з підписом /check)"))
bot.message_handler(commands=["summary"])(_mode_command("summary", "Приклад: /summary история Первая мировая война"))
bot.message_handler(commands=["essay"])(_mode_command("essay", "Приклад: /essay укрлит образ Тараса Бульби, 250 слів"))


@bot.message_handler(commands=["quiz"])
def cmd_quiz(message):
    if not gate(message):
        return
    args = message.text.split(maxsplit=1)
    subject, topic = split_subject(args[1] if len(args) > 1 else "")
    subject = subject or settings[message.from_user.id]["subject"]
    if not subject:
        bot.reply_to(message, "Вкажи предмет: /quiz фізика або /quiz фізика закони Ньютона")
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
    m = re.match(r"^/(explain|check|summary|quiz|exam)(@\w+)?\s*(.*)", caption, re.S)
    if m:
        mode, caption = m.group(1), m.group(3)
    subject, task = split_subject(caption)
    subject = subject or settings[message.from_user.id]["subject"]
    try:
        data = bot.download_file(bot.get_file(message.photo[-1].file_id).file_path)
    except Exception:
        log.exception("Не удалось скачать фото")
        bot.reply_to(message, "Не вдалося завантажити фото, спробуй ще раз.")
        return
    dispatch(message, mode, subject, task, [gt.Part.from_bytes(data=data, mime_type="image/jpeg")])


@bot.message_handler(content_types=["voice", "audio"])
def on_voice(message):
    if not gate(message):
        return
    obj = message.voice or message.audio
    if obj.duration and obj.duration > MAX_VOICE_SECONDS:
        bot.reply_to(message, f"Голосове повідомлення надто довге (максимум {MAX_VOICE_SECONDS} с).")
        return
    mime = "audio/ogg" if message.voice else (obj.mime_type or "audio/mpeg")
    try:
        data = bot.download_file(bot.get_file(obj.file_id).file_path)
    except Exception:
        log.exception("Не удалось скачать аудио")
        bot.reply_to(message, "Не вдалося завантажити аудіо, спробуй ще раз.")
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
                                        "id", "menu"], n=1, cutoff=0.6)
        hint = f" Можливо, ти мав на увазі /{close[0]}?" if close else ""
        bot.reply_to(message, f"Не знаю такої команди.{hint}\nСписок: /help")
        return
    if not gate(message):
        return
    uid = message.from_user.id
    subject, task = split_subject(message.text)
    if subject and not task:
        settings[uid]["subject"] = subject
        bot.reply_to(message, f"Предмет обрано: {subject_label(subject)}. Тепер пиши завдання.")
        return
    subject = subject or settings[uid]["subject"]
    dispatch(message, "answer", subject, task, [])


def _get_bot_username():
    try:
        return bot.get_me().username
    except Exception:
        return None


BOT_USERNAME = None  # заполняется при старте бота


def addressed(m):
    """True, если бот адресован в групповом чате."""
    if m.chat.type == "private":
        return True
    text = (m.text or "") + "@"
    return f"@{BOT_USERNAME}" in text or bool(
        m.reply_to_message
        and m.reply_to_message.from_user
        and m.reply_to_message.from_user.username == BOT_USERNAME
    )


# ===========================================================================
# Мини-веб-сервер (нужен Render, чтобы считать сервис живым; на него же
# можно направить UptimeRobot, чтобы бесплатный сервис не засыпал)
# ===========================================================================
class Ping(BaseHTTPRequestHandler):
    def __init__(self, request, client_address, server):
        super().__init__(request, client_address, server)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass


def run_web():
    port = int(os.environ.get("PORT", "10000"))
    try:
        HTTPServer(("0.0.0.0", port), Ping).serve_forever()
    except Exception:
        log.exception("Веб-сервер не запустился (порт %s)", port)


def self_ping():
    """Каждые 10 мин стучится в свой health-URL, чтобы Render не засыпал."""
    url = os.environ.get("SELF_PING_URL", "")
    if not url:
        return
    while True:
        time.sleep(600)
        try:
            urllib.request.urlopen(url, timeout=15)
        except Exception:
            pass


# ===========================================================================
# Запуск
# ===========================================================================
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
        answer = call_gemini([f"Відповідай коротко (до 100 слів) на запитання учня:\n{q[:500]}"],
                             system_instruction=SYSTEM_BASE, temperature=0.2)
        if not answer:
            bot.answer_inline_query(query.id, [], cache_time=1)
            return
        register_use(uid, query.from_user.first_name)
        text = sanitize_html(answer)
        title = q[:64]
        INLINE_CACHE.set(key, (time.time(), (title, text)))
    try:
        art = tg.InlineQueryResultArticle(
            query.id, title,
            tg.InputTextMessageContent(text, parse_mode="HTML"),
            description="Відповідь ШІ · натисни, щоб надіслати в чат")
        bot.answer_inline_query(query.id, [art], cache_time=10)
    except Exception:
        log.exception("Ошибка inline")

# ===========================================================================
# Catch-all callback — всегда последний обработчик
# ===========================================================================
@bot.callback_query_handler(func=lambda call: bool(call.data))
def on_unhandled_callback(call):
    bot.answer_callback_query(
        call.id,
        "\u0426\u044f \u043a\u043d\u043e\u043f\u043a\u0430 \u0431\u0456\u043b\u044c\u0448\u0435 \u043d\u0435 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u0430. \u041d\u0430\u043f\u0438\u0448\u0438 /help.",
        show_alert=True,
    )


if __name__ == "__main__":
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    threading.Thread(target=preload_books, daemon=True).start()
    threading.Thread(target=broadcast_loop, daemon=True).start()
    threading.Thread(target=daily_loop, daemon=True).start()
    # BOT_USERNAME нужен для проверки «адресован боту» в групповых чатах.
    BOT_USERNAME = _get_bot_username()
    log.info("Бот запущено. Модели: %s | ключей Gemini: %d | @%s",
             ", ".join(MODELS), len(API_KEYS), BOT_USERNAME)

    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception:
            log.exception("Polling упал, перезапуск через 10 сек")
            time.sleep(3)