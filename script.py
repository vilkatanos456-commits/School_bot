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
from datetime import date
from difflib import get_close_matches
from http.server import BaseHTTPRequestHandler, HTTPServer

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
)

LENGTH_INSTR = {
    "normal": "",
    "short": "Отвечай максимально коротко: не больше 5-6 предложений.",
    "detail": "Отвечай подробно и развёрнуто, по шагам.",
}

MODE_INSTR = {
    "answer": "Дай готовый точный ответ, затем кратко поясни его суть.",
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
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("pypdf").setLevel(logging.ERROR)
log = logging.getLogger("homework_bot")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

@bot.callback_query_handler(func=lambda call: call.data == "delete_msg")
def shredder(call):
    try:
        # Удаляем ответ бота
        bot.delete_message(call.message.chat.id, call.message.message_id)
        # Удаляем твое сообщение с вопросом/фоткой (оно идет прямо перед ответом)
        bot.delete_message(call.message.chat.id, call.message.message_id - 1)
    except Exception:
        pass # Если не вышло удалить, бот просто проигнорирует ошибку и не крашнется
        pass # Если не вышло удалить, бот просто проигнорирует ошибку и не крашнется

# ===========================================================================
# Состояние пользователей
# ===========================================================================
settings = defaultdict(lambda: {"length": "normal", "subject": None})
history = defaultdict(list)       # uid -> [(вопрос, ответ), ...]
pending = {}                      # uid -> отложенный запрос (ждём выбор предмета)
last_request = {}                 # uid -> время последнего запроса

# ===========================================================================
# Статистика и лимиты
# ===========================================================================
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
_stats_lock = threading.Lock()


def _load_stats():
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}


stats = _load_stats()


def register_use(uid, name):
    today = date.today().isoformat()
    with _stats_lock:
        u = stats["users"].setdefault(str(uid), {"name": name, "total": 0, "days": {}})
        u["name"] = name
        u["total"] += 1
        u["days"][today] = u["days"].get(today, 0) + 1
        try:
            with open(STATS_FILE, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=1)
        except Exception:
            log.exception("Не удалось сохранить статистику")


def used_today(uid):
    u = stats["users"].get(str(uid))
    return u["days"].get(date.today().isoformat(), 0) if u else 0


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


def send_answer(chat_id, status_msg, text):
    """Сначала пробует HTML; если Telegram не принял разметку - шлёт чистым текстом."""

    # Создаем кнопку-шредер
    markup = InlineKeyboardMarkup()
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
        score += 8 * sum(1 for n in numbers if n in pages[i])
        if score > 0:
            scored.append((score, i))
    scored.sort(reverse=True)
    return scored


def find_context(pdf_path, task):
    """Возвращает (текст страниц, номера страниц PDF)."""
    pages = load_pages(pdf_path)
    best = sorted(i for _, i in score_pages(pdf_path, task)[:TOP_PAGES])
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
    return best


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
    return (
        f"Фрагменты учебника:\n{context or '(подходящих фрагментов не найдено)'}\n\n"
        + (f"Предыдущий диалог:\n{hist}\n\n" if hist else "")
        + f"Твоя задача: {MODE_INSTR[mode]}\n\nСообщение ученика: {task}"
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
def dispatch(message, mode, subject, task, media):
    """Если предмет неизвестен - показывает кнопки, иначе запускает обработку."""
    uid = message.from_user.id
    if subject is None:
        pending[uid] = {"mode": mode, "task": task, "media": media, "reply_to": message.message_id}
        bot.reply_to(message, "Выбери предмет:", reply_markup=subjects_keyboard())
        return
    process(message.chat.id, message.message_id, uid, message.from_user.first_name,
            mode, subject, task, media)


def process(chat_id, reply_to, uid, name, mode, subject, task, media):
    if limit_reached(uid):
        bot.send_message(chat_id, f"Дневной лимит ({DAILY_LIMIT} запросов) исчерпан. Приходи завтра!",
                         reply_to_message_id=reply_to)
        return

    # Словарь с твоим расписанием 8-Б

    # ===========================================================================
    # Расписание и обработчики домашки (ВЫНЕСЕНО ИЗ PROCESS НАВЕРХ)
    # ===========================================================================
SCHEDULE = {
    "0": ["Географія", "Біологія", "Геометрія", "Фінансова грамотність", "Фізика", "Українська література"],
    "1": ["Фізика", "Алгебра", "Англійська мова", "Інформатика", "Українська мова", "Історія"],
    "2": ["Алгебра", "Англійська мова", "Фіз. виховання", "Зарубіжна література", "Основи здоров'я", "Інформатика",
          "Трудове навчання"],
    "3": ["Фіз. виховання", "Історія", "Українська мова", "Українська література", "Географія", "Хімія",
          "Англійська мова"],
    "4": ["Англійська мова", "Українська мова", "Біологія", "Алгебра", "Фіз. виховання", "Мистецтво", "Хімія"],
}


@bot.message_handler(commands=['hw'])
def hw_menu(message):
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("ПН", callback_data="hw_0"),
               InlineKeyboardButton("ВТ", callback_data="hw_1"),
               InlineKeyboardButton("СР", callback_data="hw_2"))
    markup.row(InlineKeyboardButton("ЧТ", callback_data="hw_3"),
               InlineKeyboardButton("ПТ", callback_data="hw_4"))
    markup.add(InlineKeyboardButton("🗑 Закрыть", callback_data="delete_msg"))
    bot.send_message(message.chat.id, "На какой день собрать домашку?", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_"))
def parse_smart_hw(call):
    day_index = call.data.split("_")[1]
    subjects_today = SCHEDULE[day_index]

    bot.edit_message_text("Сканирую сайт школы... 🕵️‍♂️", call.message.chat.id, call.message.message_id)

    try:
        url = "https://sch3.edu.vn.ua/disted"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.post(url, data={'class': '24'}, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        latest_tasks = {}

        rows = soup.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 5:
                lesson_date = cols[0].text.strip()
                subject = cols[3].text.strip()

                # Проверяем, есть ли этот предмет в расписании на выбранный день
                if subject in subjects_today:
                    # Ищем тег ссылки <a> внутри строки
                    a_tag = row.find('a')
                    if a_tag and 'href' in a_tag.attrs:
                        task_link = "https://sch3.edu.vn.ua" + a_tag['href']
                    else:
                        task_link = "https://sch3.edu.vn.ua/disted"

                    # Берем самую первую (самую свежую) запись для предмета
                    if subject not in latest_tasks:
                        latest_tasks[subject] = (lesson_date, task_link)

        hw_text = f"📚 **Актуальная домашка:**\n\n"
        for subject in subjects_today:
            if subject in latest_tasks:
                lesson_date, task_link = latest_tasks[subject]
                hw_text += f"🔹 **{subject}** ({lesson_date})\n[📂 Открыть задание]({task_link})\n\n"

        if len(latest_tasks) == 0:
            hw_text = "На этот день свежих заданий на сайте пока нет."

        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🗑", callback_data="delete_msg"))

        bot.edit_message_text(hw_text, call.message.chat.id, call.message.message_id, parse_mode='Markdown',
                              reply_markup=markup)

    except Exception as e:
        bot.edit_message_text(f"Сайт недоступен. Ошибка: {e}", call.message.chat.id, call.message.message_id)
def dispatch(message, mode, subject, task, media):
    """Если предмет неизвестен - показывает кнопки, иначе запускает обработку."""
    uid = message.from_user.id
    if subject is None:
        pending[uid] = {"mode": mode, "task": task, "media": media, "reply_to": message.message_id}
        bot.reply_to(message, "Выбери предмет:", reply_markup=subjects_keyboard())
        return
    process(message.chat.id, message.message_id, uid, message.from_user.first_name,
            mode, subject, task, media)


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
        if subject == "auto":
            subject = auto_detect(task)
            if not subject:
                bot.edit_message_text("Не смог определить предмет. Напиши его явно, например: физика " + task[:40],
                                      chat_id, status.message_id)
                return
            auto_note = f"📚 Предмет: {subject}\n\n"

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
        answer = call_gemini([prompt, *media], system_instruction=build_system(uid))
        if not answer:
            bot.edit_message_text("Сейчас ИИ перегружен или недоступен. Попробуй ещё раз через минуту.",
                                  chat_id, status.message_id)
            return

        register_use(uid, name)
        settings[uid]["subject"] = subject
        history[uid].append((task, answer))
        history[uid] = history[uid][-HISTORY_TURNS:]

        footer = f"\n\n📖 Использованы страницы PDF: {', '.join(map(str, pages))}" if pages else ""
        send_answer(chat_id, status, auto_note + answer + footer)

    except Exception:
        log.exception("Ошибка обработки запроса")
        try:
            bot.edit_message_text("Произошла ошибка. Попробуй ещё раз.", chat_id, status.message_id)
        except Exception:
            pass

# ===========================================================================
# Тесты (викторина опросами Telegram)
# ===========================================================================
def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)


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
    "Можно прислать <b>фото</b> задания или <b>голосовое</b> сообщение.\n\n"
    "<b>Команды</b>\n"
    "/subjects — список предметов\n"
    "/subject физика — выбрать текущий предмет\n"
    "/explain [предмет] задание — объяснить, КАК решать\n"
    "/check [предмет] решение — проверить твоё решение (можно фото с подписью /check)\n"
    "/summary [предмет] тема — шпаргалка для повторения\n"
    "/quiz [предмет] [тема] — тест из 5 вопросов\n"
    "/short, /detail, /normal — длина ответов\n"
    "/reset — очистить память диалога\n"
    "/stats — твоя статистика\n"
    "/id — твой Telegram ID"
)


@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    if gate(message, cooldown=False):
        bot.send_message(message.chat.id, HELP_TEXT, parse_mode="HTML")


@bot.message_handler(commands=["id"])
def cmd_id(message):
    bot.reply_to(message, f"Твой Telegram ID: {message.from_user.id}")


@bot.message_handler(commands=["subjects"])
def cmd_subjects(message):
    if gate(message, cooldown=False):
        bot.send_message(message.chat.id, "Доступные предметы:\n" + "\n".join(f"• {s}" for s in SUBJECTS))


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
            f"{v['name']} ({k}): {v['total']} всего, {v['days'].get(date.today().isoformat(), 0)} сегодня"
            for k, v in rows
        )
    bot.reply_to(message, text)


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
        bot.reply_to(message, "Не знаю такую команду. Список команд: /help")
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
    dispatch(message, "answer", subject, task, [])


@bot.callback_query_handler(func=lambda call: call.data.startswith("hw_"))
def parse_smart_hw(call):
    day_index = call.data.split("_")[1]
    subjects_today = SCHEDULE[day_index]

    bot.edit_message_text("Сканирую сайт школы... 🕵️‍♂️", call.message.chat.id, call.message.message_id)

    try:
        url = "https://sch3.edu.vn.ua/disted"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.post(url, data={'class': '24'}, headers=headers, timeout=10)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')

        # Словарь, чтобы хранить только самую свежую домашку по каждому предмету
        latest_tasks = {}

        rows = soup.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 5:
                lesson_date = cols[0].text.strip()
                subject = cols[3].text.strip()

                # Проверяем, есть ли этот предмет в расписании на этот день
                if subject in subjects_today:
                    task = cols[-1].text.strip()

                    # Если предмет еще не записан или дата новее (простая проверка по строке даты)
                    # Чтобы не показывало старье, берем только то, что записано впервые (сайт обычно идет от новых к старым)
                    if subject not in latest_tasks:
                        latest_tasks[subject] = (lesson_date, task)

        hw_text = f"📚 **Актуальная домашка:**\n\n"
        for subject in subjects_today:
            if subject in latest_tasks:
                lesson_date, task = latest_tasks[subject]
                hw_text += f"🔹 **{subject}** ({lesson_date})\n{task}\n\n"

        if len(latest_tasks) == 0:
            hw_text = "На этот день свежих заданий на сайте пока нет."

        from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🗑", callback_data="delete_msg"))

        bot.edit_message_text(hw_text, call.message.chat.id, call.message.message_id, reply_markup=markup)

    except Exception as e:
        bot.edit_message_text(f"Сайт недоступен. Ошибка: {e}", call.message.chat.id, call.message.message_id)

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

    try:
        bot.set_my_commands([
            tg.BotCommand("help", "Как пользоваться"),
            tg.BotCommand("subjects", "Список предметов"),
            tg.BotCommand("subject", "Выбрать предмет"),
            tg.BotCommand("explain", "Объяснить, как решать"),
            tg.BotCommand("check", "Проверить моё решение"),
            tg.BotCommand("summary", "Шпаргалка по теме"),
            tg.BotCommand("quiz", "Тест из 5 вопросов"),
            tg.BotCommand("short", "Короткие ответы"),
            tg.BotCommand("detail", "Подробные ответы"),
            tg.BotCommand("reset", "Очистить память диалога"),
            tg.BotCommand("stats", "Статистика"),
        ])
    except Exception:
        log.warning("Не удалось установить меню команд")

    threading.Thread(target=preload_books, daemon=True).start()
    log.info("Бот запущен. Модели: %s | ключей Gemini: %d", ", ".join(MODELS), len(API_KEYS))
    bot.infinity_polling(skip_pending=True)
