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
# ÐÐ°ÑÑ‚Ñ€Ð¾Ð¹ÐºÐ¸ - Ð’Ð¡Ð• ÑÐµÐºÑ€ÐµÑ‚Ñ‹ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ñ‡ÐµÑ€ÐµÐ· Ð¿ÐµÑ€ÐµÐ¼ÐµÐ½Ð½Ñ‹Ðµ Ð¾ÐºÑ€ÑƒÐ¶ÐµÐ½Ð¸Ñ (Render -> Environment)
# ===========================================================================
#   TELEGRAM_TOKEN   - Ñ‚Ð¾ÐºÐµÐ½ Ð¾Ñ‚ @BotFather                         (Ð¾Ð±ÑÐ·Ð°Ñ‚ÐµÐ»ÑŒÐ½Ð¾)
#   GEMINI_API_KEYS  - ÐºÐ»ÑŽÑ‡Ð¸ Gemini Ñ‡ÐµÑ€ÐµÐ· Ð·Ð°Ð¿ÑÑ‚ÑƒÑŽ                  (Ð¾Ð±ÑÐ·Ð°Ñ‚ÐµÐ»ÑŒÐ½Ð¾)
#   GEMINI_MODELS    - Ð¼Ð¾Ð´ÐµÐ»Ð¸ Ñ‡ÐµÑ€ÐµÐ· Ð·Ð°Ð¿ÑÑ‚ÑƒÑŽ (Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡. gemini-3.5-flash-lite)
#   ADMIN_ID         - Ñ‚Ð²Ñ–Ð¹ Telegram ID (Ð±ÐµÐ· Ð»Ð¸Ð¼Ð¸Ñ‚Ð¾Ð², Ð²Ð¸Ð´Ð¸Ñ‚ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ)
#   ALLOWED_IDS      - ID Ñ‡ÐµÑ€ÐµÐ· Ð·Ð°Ð¿ÑÑ‚ÑƒÑŽ; Ð¿ÑƒÑÑ‚Ð¾ = Ð´Ð¾ÑÑ‚ÑƒÐ¿ Ñƒ Ð²ÑÐµÑ…
#   DAILY_LIMIT      - Ð·Ð°Ð¿Ñ€Ð¾ÑÐ¾Ð² Ð² ÑÑƒÑ‚ÐºÐ¸ Ð½Ð° Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»Ñ (Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡. 40)
#   TOP_PAGES        - ÑÐºÐ¾Ð»ÑŒÐºÐ¾ ÑÑ‚Ñ€Ð°Ð½Ð¸Ñ† ÑƒÑ‡ÐµÐ±Ð½Ð¸ÐºÐ° Ð¾Ñ‚Ð´Ð°Ð²Ð°Ñ‚ÑŒ Ð¼Ð¾Ð´ÐµÐ»Ð¸ (Ð¿Ð¾ ÑƒÐ¼Ð¾Ð»Ñ‡. 2)
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
    raise SystemExit("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ð¾ TELEGRAM_TOKEN (Ð·Ð¼Ñ–Ð½Ð½Ð° ÑÐµÑ€ÐµÐ´Ð¾Ð²Ð¸Ñ‰Ð°) Ð°Ð±Ð¾ Ð²Ñ–Ð½ Ð½ÐµÐ¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¸Ð¹.")
if not API_KEYS:
    raise SystemExit("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ð¾ ÐºÐ»ÑŽÑ‡Ñ–Ð² Gemini: Ð²ÐºÐ°Ð¶Ð¸ GEMINI_API_KEYS (Ñ‡ÐµÑ€ÐµÐ· ÐºÐ¾Ð¼Ñƒ).")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOOKS_DIR = os.path.join(BASE_DIR, "ÐºÐ½Ð¸Ð³Ð¸")
DATA_DIR = os.path.join(BASE_DIR, "data")      # ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ° (Ð½Ð° Render Ð½Ðµ ÑÐ¾Ñ…Ñ€Ð°Ð½ÑÐµÑ‚ÑÑ)
CACHE_DIR = os.path.join(BASE_DIR, "cache")    # ÐºÑÑˆ Ñ‚ÐµÐºÑÑ‚Ð° ÑƒÑ‡ÐµÐ±Ð½Ð¸ÐºÐ¾Ð² (Ð¼Ð¾Ð¶Ð½Ð¾ ÐºÐ¾Ð¼Ð¼Ð¸Ñ‚Ð¸Ñ‚ÑŒ)
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
    "Ð°Ð»Ð³ÐµÐ±Ñ€Ð°": "Ð°Ð»Ð³ÐµÐ±Ñ€Ð°.pdf",
    "Ð±Ð¸Ð¾Ð»Ð¾Ð³Ð¸Ñ": "Ð±Ñ–Ð¾Ð»Ð¾Ð³Ñ–Ñ.pdf",
    "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ñ": "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ.pdf",
    "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ð¸Ñ": "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ñ–Ñ.pdf",
    "Ð·Ð°Ñ€ÑƒÐ±ÐµÐ¶ÐºÐ°": "Ð·Ð°Ñ€ÑƒÐ±ÐµÐ¶ÐºÐ°.pdf",
    "Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ñ": "Ñ–ÑÑ‚Ð¾Ñ€Ñ–Ñ.pdf",
    "Ð¼ÑƒÐ·Ñ‹ÐºÐ°": "Ð¼ÑƒÐ·Ð¸ÐºÐ°.pdf",
    "ÑƒÐºÑ€Ð»Ð¸Ñ‚": "Ð£ÐºÑ€ Ð»Ñ–Ñ‚.pdf",
    "ÑƒÐºÑ€Ð¼Ð¾Ð²": "ÑƒÐºÑ€ Ð¼Ð¾Ð²Ð°.pdf",
    "Ñ„Ð¸Ð·Ð¸ÐºÐ°": "Ñ„Ñ–Ð·Ð¸ÐºÐ°.pdf",
    "Ñ…Ð¸Ð¼Ð¸Ñ": "Ñ…Ñ–Ð¼Ñ–Ñ.pdf",
    "entrepreneurship": "Ð¿Ñ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾.pdf",
    "technology": "Ñ‚ÐµÑ…Ð½Ð¾Ð»Ð¾Ð³Ñ–Ñ—.pdf",
}

SUBJECT_LABELS_UA = {
    "Ð°Ð»Ð³ÐµÐ±Ñ€Ð°": "ÐÐ»Ð³ÐµÐ±Ñ€Ð°", "Ð±Ð¸Ð¾Ð»Ð¾Ð³Ð¸Ñ": "Ð‘Ñ–Ð¾Ð»Ð¾Ð³Ñ–Ñ", "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ñ": "Ð“ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ",
    "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ð¸Ñ": "Ð“ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ñ–Ñ", "Ð·Ð°Ñ€ÑƒÐ±ÐµÐ¶ÐºÐ°": "Ð—Ð°Ñ€ÑƒÐ±Ñ–Ð¶Ð½Ð° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°",
    "Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ñ": "Ð†ÑÑ‚Ð¾Ñ€Ñ–Ñ", "Ð¼ÑƒÐ·Ñ‹ÐºÐ°": "ÐœÑƒÐ·Ð¸ÐºÐ°", "ÑƒÐºÑ€Ð»Ð¸Ñ‚": "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°",
    "ÑƒÐºÑ€Ð¼Ð¾Ð²": "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "Ñ„Ð¸Ð·Ð¸ÐºÐ°": "Ð¤Ñ–Ð·Ð¸ÐºÐ°", "Ñ…Ð¸Ð¼Ð¸Ñ": "Ð¥Ñ–Ð¼Ñ–Ñ",
    "entrepreneurship": "ÐŸÑ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾ Ñ– Ñ„Ñ–Ð½Ð°Ð½ÑÐ¾Ð²Ð° Ð³Ñ€Ð°Ð¼Ð¾Ñ‚Ð½Ñ–ÑÑ‚ÑŒ",
    "technology": "Ð¢ÐµÑ…Ð½Ð¾Ð»Ð¾Ð³Ñ–Ñ—",
}

def subject_label(subject):
    return SUBJECT_LABELS_UA.get(subject, subject)

ALIASES = {
    "Ñ„Ñ–Ð·Ð¸ÐºÐ°": "Ñ„Ð¸Ð·Ð¸ÐºÐ°", "Ñ…Ñ–Ð¼Ñ–Ñ": "Ñ…Ð¸Ð¼Ð¸Ñ", "Ð±Ñ–Ð¾Ð»Ð¾Ð³Ñ–Ñ": "Ð±Ð¸Ð¾Ð»Ð¾Ð³Ð¸Ñ",
    "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ": "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ñ", "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ñ–Ñ": "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ð¸Ñ", "Ñ–ÑÑ‚Ð¾Ñ€Ñ–Ñ": "Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ñ",
    "Ð¼ÑƒÐ·Ð¸ÐºÐ°": "Ð¼ÑƒÐ·Ñ‹ÐºÐ°", "ÑƒÐºÑ€Ð¼Ð¾Ð²Ð°": "ÑƒÐºÑ€Ð¼Ð¾Ð²", "ÑƒÐºÑ€Ð»Ñ–Ñ‚": "ÑƒÐºÑ€Ð»Ð¸Ñ‚",
    "Ð·Ð°Ñ€ÑƒÐ±Ñ–Ð¶ÐºÐ°": "Ð·Ð°Ñ€ÑƒÐ±ÐµÐ¶ÐºÐ°", "Ð¿Ñ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾": "entrepreneurship",
    "Ñ‚ÐµÑ…Ð½Ð¾Ð»Ð¾Ð³Ñ–Ñ—": "technology", "Ð¿Ñ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾ Ñ– Ñ„Ñ–Ð½Ð°Ð½ÑÐ¾Ð²Ð° Ð³Ñ€Ð°Ð¼Ð¾Ñ‚Ð½Ñ–ÑÑ‚ÑŒ": "entrepreneurship",
}

SYSTEM_BASE = (
    "Ð¢Ð¸ Ñ€Ð¾Ð·ÑƒÐ¼Ð½Ð¸Ð¹ ÑˆÐºÑ–Ð»ÑŒÐ½Ð¸Ð¹ Ð¿Ð¾Ð¼Ñ–Ñ‡Ð½Ð¸Ðº. ÐÐ°ÑÐ°Ð¼Ð¿ÐµÑ€ÐµÐ´ ÑÐ¿Ð¸Ñ€Ð°Ð¹ÑÑ Ð½Ð° Ð½Ð°Ð´Ð°Ð½Ñ– "
    "Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ°. Ð¯ÐºÑ‰Ð¾ Ð² Ð½Ð¸Ñ… Ð½ÐµÐ¼Ð°Ñ” Ñ–Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ†Ñ–Ñ—, Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð½Ð° Ð¾ÑÐ½Ð¾Ð²Ñ– Ð·Ð°Ð³Ð°Ð»ÑŒÐ½Ð¸Ñ… Ð·Ð½Ð°Ð½ÑŒ. "
    "ÐžÑ„Ð¾Ñ€Ð¼Ð»ÑŽÐ¹ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ð¾Ð²Ð°Ð½Ð¾, Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÑŽÑ‡Ð¸ Ð›Ð˜Ð¨Ð• HTML-Ñ‚ÐµÐ³Ð¸ Telegram: "
    "<b>Ð¶Ð¸Ñ€Ð½Ð¸Ð¹</b> Ð´Ð»Ñ Ð³Ð¾Ð»Ð¾Ð²Ð½Ð¾Ñ— Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–, <i>ÐºÑƒÑ€ÑÐ¸Ð²</i>, <code>Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸</code>. "
    "Ð†Ð½ÑˆÑ– Ñ‚ÐµÐ³Ð¸ Ñ‚Ð° Markdown Ð½Ðµ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÐ¹. Ð¡Ð¸Ð¼Ð²Ð¾Ð»Ð¸ < Ñ– > Ñƒ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð°Ñ… Ð¿Ð¸ÑˆÐ¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸ "
    "(Â«Ð¼ÐµÐ½ÑˆÐµÂ», Â«Ð±Ñ–Ð»ÑŒÑˆÐµÂ») Ð°Ð±Ð¾ ÑÐº â‰¤ â‰¥. Ð Ð¾Ð·Ð´Ñ–Ð»ÑÐ¹ Ð±Ð»Ð¾ÐºÐ¸ Ð¿Ð¾Ñ€Ð¾Ð¶Ð½Ñ–Ð¼Ð¸ Ñ€ÑÐ´ÐºÐ°Ð¼Ð¸. "
    "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð›Ð˜Ð¨Ð• Ð£ÐšÐ ÐÐ‡ÐÐ¡Ð¬ÐšÐžÐ® ÐœÐžÐ’ÐžÐ®. Ð¯ÐºÑ‰Ð¾ ÐºÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‡ Ð¿Ñ€ÑÐ¼Ð¾ Ð¿Ñ€Ð¾ÑÐ¸Ñ‚ÑŒ Ñ–Ð½ÑˆÑƒ Ð¼Ð¾Ð²Ñƒ, Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÐ¹ Ñ—Ñ— Ð»Ð¸ÑˆÐµ Ð´Ð»Ñ Ñ†ÑŒÐ¾Ð³Ð¾ Ð·Ð°Ð¿Ð¸Ñ‚Ñƒ.  "
    "Ð¯ÐºÑ‰Ð¾ Ð¿Ð¾Ñ‚Ñ€Ñ–Ð±Ð½Ð¾Ð³Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ñƒ Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð°Ñ… Ð½ÐµÐ¼Ð°Ñ” â€” ÐÐ• Ð²Ñ–Ð´Ð¼Ð¾Ð²Ð»ÑÐ¹ÑÑ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ñ‚Ð¸ Ð¹ "
    "Ð½Ðµ Ð¼Ñ–Ñ€ÐºÑƒÐ¹ Ð¿Ñ€Ð¾ Ñ‚Ðµ, Ñ‡Ð¾Ð³Ð¾ Ð½ÐµÐ¼Ð°Ñ” Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÑƒ: Ñ€Ð¾Ð·Ð²â€™ÑÐ¶Ð¸ Ð·Ð° Ð·Ð°Ð³Ð°Ð»ÑŒÐ½Ð¸Ð¼Ð¸ Ð·Ð½Ð°Ð½Ð½ÑÐ¼Ð¸ Ñ‚Ð° Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð°Ð¼Ð¸ Ñ‚ÐµÐ¼Ð¸, "
    "Ð·Ð°Ð·Ð½Ð°Ñ‡Ð¸Ð²ÑˆÐ¸ Ñ†Ðµ ÐžÐ”ÐÐ˜Ðœ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¸Ð¼ Ñ€ÑÐ´ÐºÐ¾Ð¼ Ð½Ð° Ð¿Ð¾Ñ‡Ð°Ñ‚ÐºÑƒ: Â«ðŸ“Œ Ð£Ð¼Ð¾Ð²Ð¸ Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÑƒ Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾ â€” "
    "Ñ€Ð¾Ð·Ð²â€™ÑÐ·ÑƒÑŽ Ð·Ð° Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð°Ð¼Ð¸ Ñ‚ÐµÐ¼Ð¸. Ð”Ð»Ñ Ñ‚Ð¾Ñ‡Ð½Ð¾ÑÑ‚Ñ– Ð¼Ð¾Ð¶Ð½Ð° Ð½Ð°Ð´Ñ–ÑÐ»Ð°Ñ‚Ð¸ Ñ„Ð¾Ñ‚Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½ÑÂ». Ð”Ð°Ð»Ñ– "
    "Ð»Ð¸ÑˆÐµ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð°Ð½Ð½Ñ Ñƒ Ð·Ð²Ð¸Ñ‡Ð°Ð¹Ð½Ñ–Ð¹ ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ñ–. ÐÐµ ÑÐºÐ»Ð°Ð´Ð°Ð¹ ÑÐ¿Ð¸ÑÐºÑ–Ð² Ñ‚Ð¾Ð³Ð¾, Ñ‰Ð¾ Ñ” Ð½Ð° ÑÑ‚Ð¾Ñ€Ñ–Ð½ÐºÐ°Ñ…."
)

LENGTH_INSTR = {
    "normal": "",
    "short": "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð¼Ð°ÐºÑÐ¸Ð¼Ð°Ð»ÑŒÐ½Ð¾ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¾: Ð½Ðµ Ð±Ñ–Ð»ÑŒÑˆÐµ 5â€“6 Ñ€ÐµÑ‡ÐµÐ½ÑŒ.",
    "detail": "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð´Ð¾ÐºÐ»Ð°Ð´Ð½Ð¾ Ð¹ Ñ€Ð¾Ð·Ð³Ð¾Ñ€Ð½ÑƒÑ‚Ð¾, Ð¿Ð¾ÐºÑ€Ð¾ÐºÐ¾Ð²Ð¾.",
    "quick": ("Ð Ð•Ð–Ð˜Ðœ Ð¨Ð’Ð˜Ð”ÐšÐ˜Ð¥ Ð’Ð†Ð”ÐŸÐžÐ’Ð†Ð”Ð•Ð™. Ð”Ð°Ð¹ Ð›Ð˜Ð¨Ð• Ð¿Ñ–Ð´ÑÑƒÐ¼ÐºÐ¾Ð²Ñƒ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ: Ñ‡Ð¸ÑÐ»Ð¾, ÑÐ»Ð¾Ð²Ð¾ Ð°Ð±Ð¾ Ð²Ð°Ñ€Ñ–Ð°Ð½Ñ‚. "
              "Ð‘ÐµÐ· Ð¿Ñ€Ð¸Ð²Ñ–Ñ‚Ð°Ð½ÑŒ Ñ– Ð¿Ð¾ÑÑÐ½ÐµÐ½ÑŒ. Ð”Ð»Ñ Ð·Ð°Ð´Ð°Ñ‡Ñ– Ð´Ð¾Ð¿ÑƒÑÑ‚Ð¸Ð¼Ð¸Ð¹ Ð¾Ð´Ð¸Ð½ Ñ€ÑÐ´Ð¾Ðº Ñ–Ð· Ñ€Ð¾Ð·Ñ€Ð°Ñ…ÑƒÐ½ÐºÐ¾Ð¼. "
              "ÐÐ° Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ Ð½Ðµ Ð¿Ñ€Ð¾ Ð½Ð°Ð²Ñ‡Ð°Ð½Ð½Ñ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð¾Ð´Ð½Ð¸Ð¼ ÑÐ»Ð¾Ð²Ð¾Ð¼."),
}

MODE_INSTR = {
        "answer": ("Ð”Ð°Ð¹ Ð³Ð¾Ñ‚Ð¾Ð²Ñƒ Ñ‚Ð¾Ñ‡Ð½Ñƒ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ. Ð‘ÐµÐ· Ð²ÑÑ‚ÑƒÐ¿Ð½Ð¸Ñ… Ñ„Ñ€Ð°Ð· Ñ– Ð±ÐµÐ· Ð¾ÐºÑ€ÐµÐ¼Ð¾Ð³Ð¾ Ð¿Ð¾ÑÑÐ½ÐµÐ½Ð½Ñ ÑÑƒÑ‚Ñ–, "
               "Ð½Ðµ Ð¿Ð¸ÑˆÐ¸ ÑÐ»Ð¾Ð²Ð¾ Â«Ð¡ÑƒÑ‚ÑŒÂ». Ð¯ÐºÑ‰Ð¾ Ð¿Ð¾Ñ‚Ñ€Ñ–Ð±Ð½Ñ– Ð¾Ð±Ñ‡Ð¸ÑÐ»ÐµÐ½Ð½Ñ, Ð¿Ð¾Ð´Ð°Ð¹ Ñ—Ñ… ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¾ Ð·Ð° ÐºÑ€Ð¾ÐºÐ°Ð¼Ð¸."),
    "explain": (
        "ÐÐµ Ð¾Ð±Ð¼ÐµÐ¶ÑƒÐ¹ÑÑ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð´ÑŽ: Ð¿Ð¾ÑÑÐ½Ð¸ Ð¿Ð¾ÐºÑ€Ð¾ÐºÐ¾Ð²Ð¾, ÑÐº Ñ€Ð¾Ð·Ð²â€™ÑÐ·ÑƒÐ²Ð°Ñ‚Ð¸ Ñ‚Ð°ÐºÐµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ, "
        "ÑÐºÑ– Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð° Ñ‚Ð° Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÑŽÑ‚ÑŒÑÑ Ñ– Ñ‡Ð¾Ð¼Ñƒ, Ñ‰Ð¾Ð± ÑƒÑ‡ÐµÐ½ÑŒ Ð¼Ñ–Ð³ "
        "ÑÐ°Ð¼Ð¾ÑÑ‚Ñ–Ð¹Ð½Ð¾ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð°Ñ‚Ð¸ Ð¿Ð¾Ð´Ñ–Ð±Ð½Ðµ. ÐÐ°Ð¿Ñ€Ð¸ÐºÑ–Ð½Ñ†Ñ– Ð´Ð°Ð¹ Ð¿Ñ–Ð´ÑÑƒÐ¼ÐºÐ¾Ð²Ñƒ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ."
    ),
    "check": (
        "Ð£Ñ‡ÐµÐ½ÑŒ Ð½Ð°Ð´Ñ–ÑÐ»Ð°Ð² Ð²Ð»Ð°ÑÐ½Ð¸Ð¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº Ð°Ð±Ð¾ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ. ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ Ñ—Ñ—: ÑÐºÐ°Ð¶Ð¸, Ñ‡Ð¸ "
        "Ð²Ð¾Ð½Ð° Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð°, Ð·Ð½Ð°Ð¹Ð´Ð¸ ÐºÐ¾Ð½ÐºÑ€ÐµÑ‚Ð½Ñ– Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ¸ Ñ‚Ð° Ð¿Ð¾ÑÑÐ½Ð¸, ÑÐº Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾."
    ),
    "summary": (
        "Ð¡ÐºÐ»Ð°Ð´Ð¸ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÑƒ ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ð¾Ð²Ð°Ð½Ñƒ ÑˆÐ¿Ð°Ñ€Ð³Ð°Ð»ÐºÑƒ Ð´Ð»Ñ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð½Ñ Ð·Ð° "
        "Ð²ÐºÐ°Ð·Ð°Ð½Ð¾ÑŽ Ñ‚ÐµÐ¼Ð¾ÑŽ: Ð³Ð¾Ð»Ð¾Ð²Ð½Ñ– Ð²Ð¸Ð·Ð½Ð°Ñ‡ÐµÐ½Ð½Ñ, Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð°, Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸, 1â€“2 Ð¿Ñ€Ð¸ÐºÐ»Ð°Ð´Ð¸. "
        "Ð‘ÐµÐ· Ð·Ð°Ð¹Ð²Ð¾Ñ— Ð²Ð¾Ð´Ð¸."
    ),
    "essay": ("ÐÐ°Ð¿Ð¸ÑˆÐ¸ ÑˆÐºÑ–Ð»ÑŒÐ½Ð¸Ð¹ Ñ‚Ð²Ñ–Ñ€ Ð½Ð° Ð·Ð°Ð´Ð°Ð½Ñƒ Ñ‚ÐµÐ¼Ñƒ Ð´Ð»Ñ ÑƒÑ‡Ð½Ñ 8 ÐºÐ»Ð°ÑÑƒ: Ð²ÑÑ‚ÑƒÐ¿, "
              "2â€“3 Ð°Ð±Ð·Ð°Ñ†Ð¸ Ð¾ÑÐ½Ð¾Ð²Ð½Ð¾Ñ— Ñ‡Ð°ÑÑ‚Ð¸Ð½Ð¸ Ð· Ð°Ñ€Ð³ÑƒÐ¼ÐµÐ½Ñ‚Ð°Ð¼Ð¸ Ñ‚Ð° Ð¿Ñ€Ð¸ÐºÐ»Ð°Ð´Ð°Ð¼Ð¸, Ð²Ð¸ÑÐ½Ð¾Ð²Ð¾Ðº. ÐžÐ±ÑÑÐ³ Ð±Ð»Ð¸Ð·ÑŒÐºÐ¾ 200â€“250 ÑÐ»Ñ–Ð², "
              "ÑÐºÑ‰Ð¾ Ð½Ðµ Ð²ÐºÐ°Ð·Ð°Ð½Ð¾ Ñ–Ð½ÑˆÐµ. ÐŸÑ€Ð¾ÑÑ‚Ð¾ÑŽ Ð¶Ð¸Ð²Ð¾ÑŽ Ð¼Ð¾Ð²Ð¾ÑŽ ÑˆÐºÐ¾Ð»ÑÑ€Ð°, Ð±ÐµÐ· ÐºÐ°Ð½Ñ†ÐµÐ»ÑÑ€Ð¸Ð·Ð¼Ñ–Ð². "
              "Ð”Ð»Ñ ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾Ñ— Ð¼Ð¾Ð²Ð¸ Ñ‚Ð° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð¸ Ð¿Ð¸ÑˆÐ¸ ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾ÑŽ. Ð›Ð¸ÑˆÐµ Ñ‚ÐµÐºÑÑ‚ Ñ‚Ð²Ð¾Ñ€Ñƒ."

    ),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("pypdf").setLevel(logging.ERROR)
log = logging.getLogger("homework_bot")
log.info("ÐšÐ¾Ð½Ñ„Ñ–Ð³ÑƒÑ€Ð°Ñ†Ñ–ÑŽ Ñ‚Ð¾ÐºÐµÐ½Ð° Telegram Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶ÐµÐ½Ð¾")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=BOT_THREADS)
try:
    bot.set_my_commands([
        tg.BotCommand("hw", "Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ñ–Ð· ÑÐ°Ð¹Ñ‚Ñƒ ÑˆÐºÐ¾Ð»Ð¸"),
        tg.BotCommand("quiz", "Ð¢ÐµÑÑ‚ Ð·Ð° Ñ‚ÐµÐ¼Ð¾ÑŽ"),
        tg.BotCommand("explain", "ÐŸÐ¾ÑÑÐ½Ð¸Ñ‚Ð¸, ÑÐº Ñ€Ð¾Ð·Ð²â€™ÑÐ·ÑƒÐ²Ð°Ñ‚Ð¸"),
        tg.BotCommand("check", "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¼Ñ–Ð¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº"),
        tg.BotCommand("summary", "Ð¨Ð¿Ð°Ñ€Ð³Ð°Ð»ÐºÐ° Ð·Ð° Ñ‚ÐµÐ¼Ð¾ÑŽ"),
        tg.BotCommand("essay", "ÐÐ°Ð¿Ð¸ÑÐ°Ñ‚Ð¸ Ñ‚Ð²Ñ–Ñ€"),
        tg.BotCommand("schedule", "Ð Ð¾Ð·ÐºÐ»Ð°Ð´ ÑƒÑ€Ð¾ÐºÑ–Ð²"),
        tg.BotCommand("subject", "ÐžÐ±Ñ€Ð°Ñ‚Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚"),
        tg.BotCommand("reset", "ÐžÑ‡Ð¸ÑÑ‚Ð¸Ñ‚Ð¸ Ð¿Ð°Ð¼â€™ÑÑ‚ÑŒ Ð´Ñ–Ð°Ð»Ð¾Ð³Ñƒ"),
        tg.BotCommand("stats", "ÐœÐ¾Ñ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ°"),
        tg.BotCommand("top", "Ð ÐµÐ¹Ñ‚Ð¸Ð½Ð³ ÐºÐ»Ð°ÑÑƒ"),
        tg.BotCommand("cards", "ÐšÐ°Ñ€Ñ‚ÐºÐ¸ Ð´Ð»Ñ Ð·Ð°Ð¿Ð°Ð¼â€™ÑÑ‚Ð¾Ð²ÑƒÐ²Ð°Ð½Ð½Ñ"),
        tg.BotCommand("proofread", "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¾Ñ€Ñ„Ð¾Ð³Ñ€Ð°Ñ„Ñ–ÑŽ"),
        tg.BotCommand("remind", "ÐÐ°Ð³Ð°Ð´ÑƒÐ²Ð°Ð½Ð½Ñ Ð¿Ñ€Ð¾ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñƒ"),
        tg.BotCommand("pomodoro", "Ð¢Ð°Ð¹Ð¼ÐµÑ€ Ð½Ð°Ð²Ñ‡Ð°Ð½Ð½Ñ"),
        tg.BotCommand("class", "ÐžÐ±Ñ€Ð°Ñ‚Ð¸ ÐºÐ»Ð°Ñ"),
        tg.BotCommand("help", "Ð”Ð¾Ð²Ñ–Ð´ÐºÐ°"),
        tg.BotCommand("exam", "ÐŸÑ€Ð¾Ð±Ð½Ð° ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð°"),
        tg.BotCommand("formulas", "Ð¢Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»"),
    ])
except Exception:
    log.warning("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑƒÑÑ‚Ð°Ð½Ð¾Ð²Ð¸Ñ‚ÑŒ Ð¼ÐµÐ½ÑŽ ÐºÐ¾Ð¼Ð°Ð½Ð´")

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
    """ÐšÐ½Ð¾Ð¿ÐºÐ¸ ðŸ‘/ðŸ‘Ž Ð¿Ð¾Ð´ Ð¾Ñ‚Ð²ÐµÑ‚Ð¾Ð¼."""
    _, verdict, token = call.data.split(":")
    data = FB_CACHE.pop(token, None)
    if verdict == "bad":
        FB_LOG.append(datetime.now(KYIV).date().isoformat())
    try:
        bot.answer_callback_query(call.id, "Ð”ÑÐºÑƒÑŽ Ð·Ð° Ð¾Ñ†Ñ–Ð½ÐºÑƒ!" if verdict == "ok" else "Ð’Ñ€Ð°Ñ…ÑƒÑŽ, Ð´ÑÐºÑƒÑŽ.")
    except Exception:
        pass
    if verdict == "bad" and data and ADMIN_ID:
        try:
            bot.send_message(ADMIN_ID,
                             f"ðŸ‘Ž ÐŸÐ¾Ð³Ð°Ð½Ð° Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ (uid {data['uid']})\n\n"
                             f"Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ:\n{data['task']}\n\nÐ’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ:\n{data['answer'][:1500]}")
        except Exception:
            log.exception("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð¿ÐµÑ€ÐµÑÐ»Ð°Ñ‚Ð¸ Ð²Ñ–Ð´Ð³ÑƒÐº Ð°Ð´Ð¼Ñ–Ð½Ñ–ÑÑ‚Ñ€Ð°Ñ‚Ð¾Ñ€Ñƒ")
    try:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("ðŸ—‘", callback_data="delete_msg"))
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=kb)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("similar:"))
def on_similar(call):
    """ÐšÐ½Ð¾Ð¿ÐºÐ° ðŸ”: Ð¿Ñ€Ð¸Ð´ÑƒÐ¼Ð°Ñ‚ÑŒ Ð¿Ð¾Ñ…Ð¾Ð¶ÑƒÑŽ Ð·Ð°Ð´Ð°Ñ‡Ñƒ Ð½Ð° Ñ‚Ð¾ Ð¶Ðµ Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð¾."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "ÐÐµÐ¼Ð°Ñ” Ð´Ð¾ÑÑ‚ÑƒÐ¿Ñƒ.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Ð”ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.", show_alert=True)
        return
    data = SIM_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "ÐšÐ½Ð¾Ð¿ÐºÐ° Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð»Ð°, Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð¹ Ñ‰Ðµ Ñ€Ð°Ð·.")
        return
    subject, task = data
    bot.answer_callback_query(call.id, "ÐŸÑ€Ð¸Ð´ÑƒÐ¼ÑƒÑŽâ€¦")
    status = bot.send_message(call.message.chat.id, "ÐŸÑ€Ð¸Ð´ÑƒÐ¼ÑƒÑŽ Ð¿Ð¾Ð´Ñ–Ð±Ð½Ðµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñâ€¦ â±ï¸")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        path = book_path_any(subject)
        context, _ = find_context(path, task) if path else ("", [])
        prompt = (
            f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ°:\n{context or '(Ð½ÐµÐ¼Ð°Ñ”)'}\n\n"
            f"Ð—Ð°Ð²Ð´Ð°Ð½Ð½Ñ ÑƒÑ‡Ð½Ñ:\n{task}\n\n"
            "ÐŸÑ€Ð¸Ð´ÑƒÐ¼Ð°Ð¹ ÐŸÐžÐ”Ð†Ð‘ÐÐ• Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð½Ð° Ñ‚Ðµ ÑÐ°Ð¼Ðµ Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð¾, Ð°Ð»Ðµ Ð· Ñ–Ð½ÑˆÐ¸Ð¼Ð¸ Ñ‡Ð¸ÑÐ»Ð°Ð¼Ð¸ Ñ‚Ð° ÑƒÐ¼Ð¾Ð²Ð¾ÑŽ. "
            "Ð¡Ð¿Ð¾Ñ‡Ð°Ñ‚ÐºÑƒ Ð½Ð°Ð²ÐµÐ´Ð¸ ÑƒÐ¼Ð¾Ð²Ñƒ Ð½Ð¾Ð²Ð¾Ð³Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ, Ð¿Ð¾Ñ‚Ñ–Ð¼ Ñ€ÑÐ´Ð¾Ðº Â«Ð ÐžÐ—Ð’â€™Ð¯Ð—ÐÐÐÐ¯:Â» Ñ– ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐµ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð°Ð½Ð½Ñ Ð· Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð´ÑŽ."
        )
        answer = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.7)
        if not answer:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer,
                    uid=uid, task=answer[:500], subject=subject)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð¿Ð¾Ñ…Ð¾Ð¶ÐµÐ¹ Ð·Ð°Ð´Ð°Ñ‡Ð¸")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
@bot.callback_query_handler(func=lambda call: call.data.startswith("rw:"))
def on_rewrite(call):
    """ÐšÐ½Ð¾Ð¿ÐºÐ° Â«âœï¸ Ð¡Ð²Ð¾Ñ—Ð¼Ð¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸Â»: Ð¿ÐµÑ€ÐµÐ¿Ð¸ÑÐ°Ñ‚ÑŒ Ð¾Ñ‚Ð²ÐµÑ‚ Ð¶Ð¸Ð²Ñ‹Ð¼ ÑÐ·Ñ‹ÐºÐ¾Ð¼ ÑˆÐºÐ¾Ð»ÑŒÐ½Ð¸ÐºÐ°."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "ÐÐµÐ¼Ð°Ñ” Ð´Ð¾ÑÑ‚ÑƒÐ¿Ñƒ.", show_alert=True)
        return
    if limit_reached(uid):
        bot.answer_callback_query(call.id, "Ð”ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.", show_alert=True)
        return
    data = REWRITE_CACHE.get(call.data.split(":", 1)[1])
    if not data:
        bot.answer_callback_query(call.id, "ÐšÐ½Ð¾Ð¿ÐºÐ° Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð»Ð°, Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð¹ Ñ‰Ðµ Ñ€Ð°Ð·.")
        return
    task, answer = data
    bot.answer_callback_query(call.id, "ÐŸÐµÑ€ÐµÐ¿Ð¸ÑÑƒÑŽâ€¦")
    status = bot.send_message(call.message.chat.id, "ÐŸÐµÑ€ÐµÐ¿Ð¸ÑÑƒÑŽ ÑÐ²Ð¾Ñ—Ð¼Ð¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸â€¦ â±ï¸")
    bot.send_chat_action(call.message.chat.id, "typing")
    try:
        prompt = (
            "ÐžÑÑŒ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ ÑƒÑ‡Ð½Ñ Ñ‚Ð° Ð³Ð¾Ñ‚Ð¾Ð²Ð¸Ð¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº:\n\n"
            f"Ð—ÐÐ’Ð”ÐÐÐÐ¯:\n{task}\n\nÐ ÐžÐ—Ð’â€™Ð¯Ð—ÐÐÐÐ¯:\n{answer}\n\n"
            "ÐŸÐµÑ€ÐµÐ¿Ð¸ÑˆÐ¸ Ñ†ÐµÐ¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº Ñ‚Ð°Ðº, Ð½Ñ–Ð±Ð¸ ÑƒÑ‡ÐµÐ½ÑŒ ÑÐ°Ð¼ Ð·Ð°Ð¿Ð¸ÑÐ°Ð² Ð¹Ð¾Ð³Ð¾ Ð² Ð·Ð¾ÑˆÐ¸Ñ‚: "
            "Ð¿Ñ€Ð¾ÑÑ‚Ñ– Ð¶Ð¸Ð²Ñ– Ñ„Ñ€Ð°Ð·Ð¸, Ð¼Ð¾Ð¶Ð½Ð° Ð²Ñ–Ð´ Ð¿ÐµÑ€ÑˆÐ¾Ñ— Ð¾ÑÐ¾Ð±Ð¸ (Â«ÑÐ¿Ð¾Ñ‡Ð°Ñ‚ÐºÑƒ Ð·Ð½Ð°Ñ…Ð¾Ð´Ð¸Ð¼Ð¾...Â», Â«Ð¾Ñ‚Ñ€Ð¸Ð¼ÑƒÑ”Ð¼Ð¾...Â»). "
            "ÐžÐ‘ÐžÐ’â€™Ð¯Ð—ÐšÐžÐ’Ðž Ð·Ð±ÐµÑ€Ñ–Ð³Ð°Ð¹ Ð’Ð¡Ð† ÐºÑ€Ð¾ÐºÐ¸ Ð¾Ð±Ñ‡Ð¸ÑÐ»ÐµÐ½ÑŒ Ñ– Ð¿Ñ€Ð¾Ð¼Ñ–Ð¶Ð½Ñ– Ñ€ÐµÐ·ÑƒÐ»ÑŒÑ‚Ð°Ñ‚Ð¸ â€” "
            "ÑÐºÐ¾Ñ€Ð¾Ñ‡ÑƒÐ¹ Ð»Ð¸ÑˆÐµ ÑÐ»Ð¾Ð²ÐµÑÐ½Ñƒ Ð²Ð¾Ð´Ñƒ, Ð° Ð½Ðµ Ð¼Ð°Ñ‚ÐµÐ¼Ð°Ñ‚Ð¸Ñ‡Ð½Ñ– Ð¾Ð±Ñ‡Ð¸ÑÐ»ÐµÐ½Ð½Ñ. "
            "Ð¯ÐºÑ‰Ð¾ Ð² Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð°Ð½Ð½Ñ– Ñ” Ð¾Ð¿Ð¸Ñ ÐºÑ€ÐµÑÐ»ÐµÐ½Ð½Ñ/Ñ€Ð¸ÑÑƒÐ½ÐºÐ° ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸ (Ð´Ð°Ð½Ð¾, Ð·Ð½Ð°Ð¹Ñ‚Ð¸, Ð¿Ð¾Ð±ÑƒÐ´Ð¾Ð²Ð°) â€” Ð·Ð±ÐµÑ€ÐµÐ¶Ð¸ ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ñƒ. "
            "Ð¤Ð¾Ñ€Ð¼ÑƒÐ»Ð¸ Ñ‚Ð° Ð¾Ð´Ð¸Ð½Ð¸Ñ†Ñ– Ð²Ð¸Ð¼Ñ–Ñ€ÑŽÐ²Ð°Ð½Ð½Ñ Ð·Ð°Ð»Ð¸Ñˆ. ÐŸÑ–Ð´ÑÑƒÐ¼ÐºÐ¾Ð²Ð° Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð¼Ð°Ñ” Ð·Ð°Ð»Ð¸ÑˆÐ¸Ñ‚Ð¸ÑÑ Ñ‚Ð°ÐºÐ¾ÑŽ ÑÐ°Ð¼Ð¾ÑŽ. "
            "ÐÐµ Ð·Ð³Ð°Ð´ÑƒÐ¹ Ð¶Ð¾Ð´Ð½Ð¸Ñ… Ñ–Ð½ÑÑ‚Ñ€ÑƒÐºÑ†Ñ–Ð¹ Ñ– Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚ÑƒÐ²Ð°Ð½Ð½Ñ, Ð¿Ñ€Ð¾ÑÑ‚Ð¾ Ð¿Ð¾Ð´Ð°Ð¹ Ð¿ÐµÑ€ÐµÐ¿Ð¸ÑÐ°Ð½Ð¸Ð¹ Ñ‚ÐµÐºÑÑ‚. "
            "Ð‘ÐµÐ· HTML-Ñ‚ÐµÐ³Ñ–Ð², Ð±ÐµÐ· Markdown â€” Ð»Ð¸ÑˆÐµ Ð·Ð²Ð¸Ñ‡Ð°Ð¹Ð½Ð¸Ð¹ Ñ‚ÐµÐºÑÑ‚ Ñ– Ð¿ÐµÑ€ÐµÐ½Ð¾ÑÐ¸ Ñ€ÑÐ´ÐºÑ–Ð²."
        )
        answer2 = call_gemini([prompt], system_instruction=SYSTEM_BASE, temperature=0.6)
        if not answer2:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                                  call.message.chat.id, status.message_id)
            return
        register_use(uid, call.from_user.first_name)
        send_answer(call.message.chat.id, status, answer2)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð¿ÐµÑ€ÐµÐ¿Ð¸ÑÑ‹Ð²Ð°Ð½Ð¸Ñ")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  call.message.chat.id, status.message_id)
        except Exception:
            pass
# ===========================================================================
# Ð¡Ð¾ÑÑ‚Ð¾ÑÐ½Ð¸Ðµ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÐµÐ¹
# ===========================================================================
settings = defaultdict(lambda: {"length": "normal", "subject": None})
history = defaultdict(list)       # uid -> [(Ð²Ð¾Ð¿Ñ€Ð¾Ñ, Ð¾Ñ‚Ð²ÐµÑ‚), ...]
pending = {}                      # uid -> Ð¾Ñ‚Ð»Ð¾Ð¶ÐµÐ½Ð½Ñ‹Ð¹ Ð·Ð°Ð¿Ñ€Ð¾Ñ (Ð¶Ð´Ñ‘Ð¼ Ð²Ñ‹Ð±Ð¾Ñ€ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð°)
ANSWER_CACHE = LRU(int(os.environ.get("ANSWER_CACHE_MAX", "1200")))
ANSWER_CACHE_LOCK = threading.RLock()
_INFLIGHT = {}
_INFLIGHT_LOCK = threading.Lock()
_AUTO_CACHE = LRU(2000)
_AUTO_CACHE_LOCK = threading.Lock()
_CONTEXT_CACHE = LRU(int(os.environ.get("CONTEXT_CACHE_MAX", "1500")))
_CONTEXT_CACHE_LOCK = threading.Lock()
FB_CACHE = LRU(100)       # Ð¾Ñ‚Ð·Ñ‹Ð²Ñ‹ Ð¿Ð¾ ÐºÐ½Ð¾Ð¿ÐºÐ°Ð¼ ðŸ‘/ðŸ‘Ž
SIM_CACHE = LRU(100)      # Ð´Ð»Ñ ÐºÐ½Ð¾Ð¿ÐºÐ¸ Â«ðŸ” ÐŸÐ¾Ð´Ñ–Ð±Ð½Ðµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½ÑÂ»
REWRITE_CACHE = LRU(100)  # token -> (Ð·Ð°Ð´Ð°Ð½Ð¸Ðµ, Ð¿Ð¾Ð»Ð½Ñ‹Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚) Ð´Ð»Ñ ÐºÐ½Ð¾Ð¿ÐºÐ¸ Â«Ð¡Ð²Ð¾Ð¸Ð¼Ð¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸Â»
HW_CACHE = LRU(50)        # token -> [(Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚, Ñ‚ÐµÐºÑÑ‚ Ð·Ð°Ð´Ð°Ð½Ð¸Ñ), ...]
QDAY_CACHE = LRU(50)      # token -> Ð¾Ñ‚Ð²ÐµÑ‚
INLINE_CACHE = LRU(200)
last_request = {}                 # uid -> Ð²Ñ€ÐµÐ¼Ñ Ð¿Ð¾ÑÐ»ÐµÐ´Ð½ÐµÐ³Ð¾ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ°
START_TIME = time.time()
FB_LOG = []            # Ð´Ð°Ñ‚Ñ‹ Ð²ÑÐµÑ… ðŸ‘Ž â€” Ð´Ð»Ñ ÐµÐ¶ÐµÐ´Ð½ÐµÐ²Ð½Ð¾Ð³Ð¾ Ð¾Ñ‚Ñ‡Ñ‘Ñ‚Ð°
REMINDS_MEM = []       # Ð½Ð°Ð¿Ð¾Ð¼Ð¸Ð½Ð°Ð½Ð¸Ñ, ÐµÑÐ»Ð¸ Redis Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑŽÑ‡Ñ‘Ð½
POMODORO = {}          # uid -> Ñ‚Ð°Ð¹Ð¼ÐµÑ€Ñ‹ Ð¿Ð¾Ð¼Ð¸Ð´Ð¾Ñ€ÐºÐ¸
HW_DONE = {}           # token -> {Ð½Ð¾Ð¼ÐµÑ€Ð° Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð½Ñ‹Ñ… Ð·Ð°Ð´Ð°Ð½Ð¸Ð¹}
DOCS = LRU(50)         # uid -> {"text": ..., "question": ...} ÐºÐ¾Ð½Ñ‚ÐµÐºÑÑ‚ ÑƒÑ‡Ð¸Ñ‚ÐµÐ»Ñ

# ===========================================================================
# Ð¡Ñ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ° Ð¸ Ð»Ð¸Ð¼Ð¸Ñ‚Ñ‹
# ===========================================================================
KV_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
KV_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
log.info("Redis (Upstash): %s", "Ð¿Ð¾Ð´ÐºÐ»ÑŽÑ‡Ñ‘Ð½" if (KV_URL and KV_TOKEN) else "ÐÐ• Ð¿Ð¾Ð´ÐºÐ»ÑŽÑ‡Ñ‘Ð½ â€” Ñ€Ð°ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ñ/ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ° Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð² Ð¿Ð°Ð¼ÑÑ‚Ð¸!")


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
            log.warning("Redis: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¿Ñ€Ð¾Ñ‡Ð¸Ñ‚Ð°Ñ‚ÑŒ %s (Ð¿Ð¾Ð¿Ñ‹Ñ‚ÐºÐ° %d Ð¸Ð· 3)", key, attempt + 1)
            time.sleep(2)
    log.error("Redis Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿ÐµÐ½: %s Ð½Ðµ Ð±ÑƒÐ´ÐµÑ‚ ÑÐ¾Ñ…Ñ€Ð°Ð½ÑÑ‚ÑŒÑÑ Ð´Ð¾ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐºÐ° Ð±Ð¾Ñ‚Ð°", key)
    return default


def kv_save(key, value):
    if not kv_enabled():
        return False
    try:
        _kv(["SET", key, json.dumps(value, ensure_ascii=False)])
        return True
    except Exception:
        log.exception("Redis: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ %s", key)
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
            log.warning("Redis: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð·Ð°Ð³Ñ€ÑƒÐ·Ð¸Ñ‚ÑŒ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ, Ð¸ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÐ¼ Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½ÑƒÑŽ")
        local = _load_local_stats()
        if local.get("users"):
            try:
                if kv_save("hwbot:stats", local):
                    log.info("ÐŸÐµÑ€ÐµÐ½ÐµÑÐµÐ½Ð° Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ð°Ñ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ° Ð² Redis")
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
                log.warning("Redis: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ")
        else:
            tmp = STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
            os.replace(tmp, STATS_FILE)
    except Exception:
        with _stats_lock:
            _stats_dirty = True
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÑƒ")

def register_use(uid, name):
    """ÐÑ‚Ð¾Ð¼Ð°Ñ€Ð½Ñ‹Ð¹ ÑÑ‡Ñ‘Ñ‚Ñ‡Ð¸Ðº Ñ‡ÐµÑ€ÐµÐ· Redis (HINCRBY) + Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ñ‹Ð¹ fallback."""
    day = datetime.now(KYIV).date().isoformat()
    k = f"hwbot:day:{day}"
    try:
        if kv_enabled():
            _kv(["HINCRBY", k, str(uid), 1])
            _kv(["EXPIRE", k, 90 * 86400])
            return
    except Exception:
        log.exception("kv_register")
    # Ð›Ð¾ÐºÐ°Ð»ÑŒÐ½Ñ‹Ð¹ fallback
    global _stats_dirty
    with _stats_lock:
        u = stats["users"].setdefault(
            str(uid), {"name": name, "total": 0, "days": {}}
        )
        u["name"] = name
        u["total"] += 1
        used = u["days"].get(day, 0)
        u["days"][day] = used + 1
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
# ÐÑ‚Ð¾Ð¼Ð°Ñ€Ð½Ñ‹Ðµ Ð¾Ð´Ð½Ð¾Ñ€Ð°Ð·Ð¾Ð²Ñ‹Ðµ Ñ„Ð»Ð°Ð³Ð¸ (Redis SET NX EX)
# ===========================================================================
_once_mem = set()                 # Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ñ‹Ðµ Ð¾Ð´Ð½Ð¾Ñ€Ð°Ð·Ð¾Ð²Ñ‹Ðµ Ñ„Ð»Ð°Ð³Ð¸ (ÐµÑÐ»Ð¸ Redis Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑŽÑ‡Ñ‘Ð½)


def once(name):
    """Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ True Ñ€Ð¾Ð²Ð½Ð¾ Ð¾Ð´Ð¸Ð½ Ñ€Ð°Ð· Ð² ÐºÐ°Ð»ÐµÐ½Ð´Ð°Ñ€Ð½Ñ‹Ð¹ Ð´ÐµÐ½ÑŒ (Ð¿Ð¾ ÐšÐ¸ÐµÐ²Ñƒ).

    ÐÑ‚Ð¾Ð¼Ð°Ñ€Ð½Ñ‹Ð¹ Ñ„Ð»Ð°Ð³: Redis SET NX EX (Ð²Ñ‹Ð¶Ð¸Ð²Ð°ÐµÑ‚ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐº),
    Ð»Ð¾ÐºÐ°Ð»ÑŒÐ½Ñ‹Ð¹ fallback â€” set() Ð² Ð¿Ð°Ð¼ÑÑ‚Ð¸.
    Ð˜ÑÐ¿Ð¾Ð»ÑŒÐ·ÑƒÐµÑ‚ÑÑ Ð´Ð»Ñ ÐµÐ¶ÐµÐ´Ð½ÐµÐ²Ð½Ñ‹Ñ… Ñ€Ð°ÑÑÑ‹Ð»Ð¾Ðº/Ð¾Ñ‚Ñ‡Ñ‘Ñ‚Ð¾Ð²/Ð²Ð¾Ð¿Ñ€Ð¾ÑÐ° Ð´Ð½Ñ.
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
        return True  # Ð¿Ñ€Ð¸ ÑÐ±Ð¾Ðµ Redis â€” Ð½Ðµ Ð±Ð»Ð¾ÐºÐ¸Ñ€ÑƒÐµÐ¼


def _load_flag(key, default):
    """Ð§Ñ‚ÐµÐ½Ð¸Ðµ Ð¾Ð´Ð½Ð¾Ñ€Ð°Ð·Ð¾Ð²Ð¾Ð³Ð¾ Ñ„Ð»Ð°Ð³Ð° (ÑÐ¾Ð²Ð¼ÐµÑÑ‚Ð¸Ð¼Ð¾ÑÑ‚ÑŒ)."""
    if kv_enabled():
        return kv_load(key, default)
    return key in _once_mem if _once_mem else default


def _save_flag(key, value):
    """Ð—Ð°Ð¿Ð¸ÑÑŒ Ð¾Ð´Ð½Ð¾Ñ€Ð°Ð·Ð¾Ð²Ð¾Ð³Ð¾ Ñ„Ð»Ð°Ð³Ð° (ÑÐ¾Ð²Ð¼ÐµÑÑ‚Ð¸Ð¼Ð¾ÑÑ‚ÑŒ)."""
    if kv_enabled():
        kv_save(key, value)
    else:
        _once_mem.add(key)


# ===========================================================================
# Ð‘ÐµÐ·Ð¾Ð¿Ð°ÑÐ½Ð°Ñ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²ÐºÐ° Ð¾Ñ‚Ð²ÐµÑ‚Ð¾Ð² (HTML Telegram)
# ===========================================================================
ALLOWED_TAGS = ("b", "i", "u", "code", "pre")


def sanitize_html(text):
    """ÐžÑÑ‚Ð°Ð²Ð»ÑÐµÑ‚ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ñ€Ð°Ð·Ñ€ÐµÑˆÑ‘Ð½Ð½Ñ‹Ðµ Ñ‚ÐµÐ³Ð¸, Ð²ÑÑ‘ Ð¾ÑÑ‚Ð°Ð»ÑŒÐ½Ð¾Ðµ ÑÐºÑ€Ð°Ð½Ð¸Ñ€ÑƒÐµÑ‚."""
    text = re.sub(r"<(/?)strong>", r"<\1b>", text, flags=re.I)
    text = re.sub(r"<(/?)em>", r"<\1i>", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = html.escape(text, quote=False)
    for t in ALLOWED_TAGS:
        text = text.replace(f"&lt;{t}&gt;", f"<{t}>").replace(f"&lt;/{t}&gt;", f"</{t}>")
    return text


def strip_tags(text):
    """Ð£Ð±Ð¸Ñ€Ð°ÐµÐ¼ Ñ‚ÐµÐ³Ð¸: ÑÐ½Ð°Ñ‡Ð°Ð»Ð° Ñ€Ð°ÑÐºÑ€Ñ‹Ð²Ð°ÐµÐ¼ HTML-ÑÑƒÑ‰Ð½Ð¾ÑÑ‚Ð¸, Ð¿Ð¾Ñ‚Ð¾Ð¼ Ð¾Ð±Ñ€ÐµÐ·Ð°ÐµÐ¼ Ñ‚ÐµÐ³Ð¸."""
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


from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton


def send_answer(chat_id, status_msg, text, uid=None, task=None, subject=None):
    """Ð¡Ð½Ð°Ñ‡Ð°Ð»Ð° Ð¿Ñ€Ð¾Ð±ÑƒÐµÑ‚ HTML; ÐµÑÐ»Ð¸ Telegram Ð½Ðµ Ð¿Ñ€Ð¸Ð½ÑÐ» Ñ€Ð°Ð·Ð¼ÐµÑ‚ÐºÑƒ - ÑˆÐ»Ñ‘Ñ‚ Ñ‡Ð¸ÑÑ‚Ñ‹Ð¼ Ñ‚ÐµÐºÑÑ‚Ð¾Ð¼."""
    token = uuid.uuid4().hex[:8]

    markup = InlineKeyboardMarkup()
    if uid in history and history[uid] and history[uid][-1][1] == "PENDING":
        history[uid][-1] = (task or history[uid][-1][0], text)
        if len(history[uid]) > HISTORY_TURNS:
            history[uid] = history[uid][-HISTORY_TURNS:]
        FB_CACHE.set(token, {"uid": uid, "task": (task or "")[:500], "answer": text[:2000]})
        if subject and task:
            SIM_CACHE.set(token, (subject, task[:500]))
            markup.add(InlineKeyboardButton("ðŸ” ÐŸÐ¾Ð´Ñ–Ð±Ð½Ðµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ", callback_data=f"similar:{token}"))
            REWRITE_CACHE.set(token, (task[:1500], text[:2500]))
            markup.add(InlineKeyboardButton("âœï¸ Ð¡Ð²Ð¾Ñ—Ð¼Ð¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸", callback_data=f"rw:{token}"))
        markup.row(InlineKeyboardButton("ðŸ‘", callback_data=f"fb:ok:{token}"),
                   InlineKeyboardButton("ðŸ‘Ž", callback_data=f"fb:bad:{token}"))
    markup.add(InlineKeyboardButton("ðŸ—‘", callback_data="delete_msg"))

    for n, chunk in enumerate(split_message(sanitize_html(text))):
        first = n == 0
        try:
            if first:
                bot.edit_message_text(chunk, chat_id, status_msg.message_id, parse_mode="HTML", reply_markup=markup)
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML", reply_markup=markup)
            continue
        except Exception as e:
            log.warning("HTML Ð½Ðµ Ð¿Ñ€Ð¾ÑˆÑ‘Ð», Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð»ÑÑŽ Ð±ÐµÐ· Ñ€Ð°Ð·Ð¼ÐµÑ‚ÐºÐ¸: %s", e)
        plain = strip_tags(chunk)
        try:
            if first:
                bot.edit_message_text(plain, chat_id, status_msg.message_id, reply_markup=markup)
            else:
                bot.send_message(chat_id, plain, reply_markup=markup)
        except Exception:
            bot.send_message(chat_id, plain, reply_markup=markup)

# ===========================================================================
# PDF: Ð·Ð°Ð³Ñ€ÑƒÐ·ÐºÐ°, ÐºÑÑˆ, Ð¿Ð¾Ð¸ÑÐº Ð½ÑƒÐ¶Ð½Ñ‹Ñ… ÑÑ‚Ñ€Ð°Ð½Ð¸Ñ†
# ===========================================================================
_pages_cache = {}
_index_cache = {}
_load_lock = threading.Lock()
_index_lock = threading.Lock()


def load_pages(pdf_path):
    """Ð§Ð¸Ñ‚Ð°ÐµÑ‚ ÑƒÑ‡ÐµÐ±Ð½Ð¸Ðº Ð¾Ð´Ð¸Ð½ Ñ€Ð°Ð·. ÐšÑÑˆ Ð½Ð° Ð´Ð¸ÑÐºÐµ Ð¿Ñ€Ð¸Ð²ÑÐ·Ð°Ð½ Ðº Ð¸Ð¼ÐµÐ½Ð¸ Ñ„Ð°Ð¹Ð»Ð° Ð¸ ÐµÐ³Ð¾ Ñ€Ð°Ð·Ð¼ÐµÑ€Ñƒ
    (Ð° Ð½Ðµ Ðº Ð¿ÑƒÑ‚Ð¸ Ð¸ Ð²Ñ€ÐµÐ¼ÐµÐ½Ð¸ Ð¸Ð·Ð¼ÐµÐ½ÐµÐ½Ð¸Ñ), Ð¿Ð¾ÑÑ‚Ð¾Ð¼Ñƒ Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚ Ð¸ Ð¿Ð¾ÑÐ»Ðµ Ð´ÐµÐ¿Ð»Ð¾Ñ Ð½Ð° ÑÐµÑ€Ð²ÐµÑ€."""
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
                if d.get("size") == size and d.get("mtime_ns") == mtime_ns:
                    _pages_cache[pdf_path] = tuple(d["pages"])
                    return _pages_cache[pdf_path]
            except Exception:
                log.warning("ÐšÑÑˆ Ð¿Ð¾Ð²Ñ€ÐµÐ¶Ð´Ñ‘Ð½, Ñ‡Ð¸Ñ‚Ð°ÑŽ PDF Ð·Ð°Ð½Ð¾Ð²Ð¾: %s", name)

        pages = []
        try:
            reader = PdfReader(pdf_path)
            for page in reader.pages:
                pages.append((page.extract_text() or "").strip())
        except Exception:
            log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ñ‡Ñ‚ÐµÐ½Ð¸Ñ PDF: %s", pdf_path)

        pages = tuple(pages)
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"size": size, "mtime_ns": mtime_ns, "pages": list(pages)}, f, ensure_ascii=False)
        except Exception:
            log.warning("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ ÐºÑÑˆ Ð´Ð»Ñ %s", name)

        _pages_cache[pdf_path] = pages
        return pages


@lru_cache(maxsize=8192)
def normalize_task(text):
    return re.sub(r"\s+", " ", (text or "").strip().casefold())


@lru_cache(maxsize=8192)
def stems(text):
    """Ð“Ñ€ÑƒÐ±Ð°Ñ Ð¾ÑÐ½Ð¾Ð²Ð° ÑÐ»Ð¾Ð²Ð° (Ð¿ÐµÑ€Ð²Ñ‹Ðµ 5 Ð±ÑƒÐºÐ²) - Ð¿Ð¾Ð¼Ð¾Ð³Ð°ÐµÑ‚ Ñ Ð¿Ð°Ð´ÐµÐ¶Ð°Ð¼Ð¸."""
    return tuple(w[:5] for w in re.findall(r"[a-zÐ°-ÑÑ‘Ñ–Ñ—Ñ”Ò‘0-9]{3,}", text.lower()))


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
    """BM25-Ð¿Ð¾Ð´Ð¾Ð±Ð½Ñ‹Ð¹ IDF Ð´Ð»Ñ ÐºÐ°Ð¶Ð´Ð¾Ð³Ð¾ Ñ‚ÐµÑ€Ð¼Ð¸Ð½Ð°."""
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
        # BM25-Ð¿Ð¾Ð´Ð¾Ð±Ð½Ñ‹Ð¹ Ð²ÐµÑ: IDF * Ð¼Ð¸Ð½Ð¸Ð¼Ð°Ð»ÑŒÐ½Ð°Ñ Ñ‡Ð°ÑÑ‚Ð¾Ñ‚Ð° (cap 5)
        score = sum(idf.get(s, 0) * min(counts[s], 5) for s in query)
        page = pages[i]
        for n in numbers:
            # "56." Ð¸Ð»Ð¸ "56)" Ð² Ð½Ð°Ñ‡Ð°Ð»Ðµ ÑÑ‚Ñ€Ð¾ÐºÐ¸ ÑƒÑ‡ÐµÐ±Ð½Ð¸ÐºÐ° = Ð½Ð¾Ð¼ÐµÑ€ Ð·Ð°Ð´Ð°Ñ‡Ð¸, ÑÐ°Ð¼Ñ‹Ð¹ ÑÐ¸Ð»ÑŒÐ½Ñ‹Ð¹ ÑÐ¸Ð³Ð½Ð°Ð»
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
        chunk = f"[Ð¡Ñ‚Ñ€Ð°Ð½Ð¸Ñ†Ð° {i + 1}]\n{pages[i]}"
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
    # Ð¡Ñ€Ð°Ð·Ñƒ ÑÑ‚Ñ€Ð¾Ð¸Ð¼ Ð¸Ð½Ð´ÐµÐºÑ, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð¿ÐµÑ€Ð²Ñ‹Ð¹ Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÐµÐ»ÑŒ Ð½Ðµ Ð¿Ð»Ð°Ñ‚Ð¸Ð» Ð·Ð° ÐµÐ³Ð¾ ÑÐ¾Ð·Ð´Ð°Ð½Ð¸Ðµ.
    page_index(path)
    return key, n, time.perf_counter() - t, True


def preload_books():
    """ÐŸÐ°Ñ€Ð°Ð»Ð»ÐµÐ»ÑŒÐ½Ð¾ Ð¿Ñ€Ð¾Ð³Ñ€ÐµÐ²Ð°ÐµÑ‚ PDF Ð¸ Ð¸Ð½Ð´ÐµÐºÑÑ‹ Ð¿Ñ€Ð¸ ÑÑ‚Ð°Ñ€Ñ‚Ðµ, Ð½Ðµ Ð±Ð»Ð¾ÐºÐ¸Ñ€ÑƒÑ polling."""
    items = list(SUBJECTS.items())
    workers = max(1, min(PRELOAD_WORKERS, len(items)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pdf-preload") as pool:
        for key, n, elapsed, ok in pool.map(_preload_one, items):
            if ok:
                log.info("Ð£Ñ‡ÐµÐ±Ð½Ð¸Ðº '%s' Ð³Ð¾Ñ‚Ð¾Ð²: %d ÑÑ‚Ñ€., %.1f ÑÐµÐº", key, n, elapsed)
            else:
                log.warning("Ð¤Ð°Ð¹Ð» Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½: %s", os.path.join(BOOKS_DIR, SUBJECTS[key]))


# ===========================================================================
# Gemini (Ð½ÐµÑÐºÐ¾Ð»ÑŒÐºÐ¾ ÐºÐ»ÑŽÑ‡ÐµÐ¹ Ñ Ñ€Ð¾Ñ‚Ð°Ñ†Ð¸ÐµÐ¹)
# ===========================================================================
_key_blocked = {}   # ÐºÐ»ÑŽÑ‡ -> Ð²Ñ€ÐµÐ¼Ñ, Ð´Ð¾ ÐºÐ¾Ñ‚Ð¾Ñ€Ð¾Ð³Ð¾ Ð¾Ð½ ÑÑ‡Ð¸Ñ‚Ð°ÐµÑ‚ÑÑ Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾Ð½Ñ‹Ð¼
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
        "thinking_config": gt.ThinkingConfig(thinking_budget=-1),
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
            log.warning("ÐšÐ»ÑŽÑ‡ #%d: Ð¾ÑˆÐ¸Ð±ÐºÐ° Ð¸Ð½Ð¸Ñ†Ð¸Ð°Ð»Ð¸Ð·Ð°Ñ†Ð¸Ð¸: %s", idx, e)
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
                    # ÐŸÑ€Ð¾Ð²ÐµÑ€ÑÐµÐ¼ r.text â€” safety Ð¸Ð»Ð¸ Ð¿ÑƒÑÑ‚Ð¾Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚ Ð²Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ None Ð±ÐµÐ· Ð¿ÐµÑ€ÐµÐ±Ð¾Ñ€Ð° ÐºÐ»ÑŽÑ‡ÐµÐ¹
                    text = r.text if hasattr(r, "text") and r.text else None
                    if text:
                        log.info("ÐšÐ»ÑŽÑ‡ #%d, %s Ð¾Ñ‚Ð²ÐµÑ‚Ð¸Ð»Ð° Ð·Ð° %.2f ÑÐµÐº", idx, model, elapsed)
                        return text
                    log.warning("ÐšÐ»ÑŽÑ‡ #%d, %s: Ð¿ÑƒÑÑ‚Ð¾Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚ (safety/Ð¾Ð±Ñ€ÐµÐ·ÐºÐ°)", idx, model)
                except Exception as e:
                    last_error = e
                    err = str(e)
                    is_daily = "PerDay" in err or "per day" in err.lower()
                    log.warning("ÐšÐ»ÑŽÑ‡ #%d | %s | Ð¿Ð¾Ð¿Ñ‹Ñ‚ÐºÐ° %d: %s",
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

    log.error("Ð’ÑÐµ ÐºÐ»ÑŽÑ‡Ð¸ Ð¸ Ð¼Ð¾Ð´ÐµÐ»Ð¸ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ñ‹: %s", last_error)
    return None


def extract_task(media):
    """Ð”Ð¾ÑÑ‚Ð°Ñ‘Ñ‚ Ñ‚ÐµÐºÑÑ‚ Ð·Ð°Ð´Ð°Ð½Ð¸Ñ Ð¸Ð· Ñ„Ð¾Ñ‚Ð¾ Ð¸Ð»Ð¸ Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ð¾Ð³Ð¾ ÑÐ¾Ð¾Ð±Ñ‰ÐµÐ½Ð¸Ñ."""
    return call_gemini(
        [
            "Ð¯ÐºÑ‰Ð¾ Ñ†Ðµ Ð·Ð¾Ð±Ñ€Ð°Ð¶ÐµÐ½Ð½Ñ, Ð´Ð¾ÑÐ»Ñ–Ð²Ð½Ð¾ Ð¿ÐµÑ€ÐµÐ¿Ð¸ÑˆÐ¸ Ñ‚ÐµÐºÑÑ‚ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ (Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸ "
            "ÑÐ¸Ð¼Ð²Ð¾Ð»Ð°Ð¼Ð¸ Unicode). Ð¯ÐºÑ‰Ð¾ Ñ†Ðµ Ð°ÑƒÐ´Ñ–Ð¾, Ñ€Ð¾Ð·ÑˆÐ¸Ñ„Ñ€ÑƒÐ¹ Ð¼Ð¾Ð²Ð»ÐµÐ½Ð½Ñ. ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð»Ð¸ÑˆÐµ "
            "Ñ‚ÐµÐºÑÑ‚ Ð±ÐµÐ· ÐºÐ¾Ð¼ÐµÐ½Ñ‚Ð°Ñ€Ñ–Ð².",
            *media,
        ],
        temperature=0.0,
    )


def history_text(uid):
    return "\n".join(f"Ð£Ñ‡ÐµÐ½Ð¸Ðº: {q[:600]}\nÐŸÐ¾Ð¼Ð¾Ñ‰Ð½Ð¸Ðº: {a[:600]}" for q, a in history[uid][-HISTORY_TURNS:])


def build_prompt(mode, context, task, uid):
    hist = history_text(uid)
    mode_text = MODE_INSTR[mode]
    if settings[uid]["length"] == "quick" and mode == "answer":
        mode_text = "Ð”Ð°Ð¹ Ð»Ð¸ÑˆÐµ Ð¿Ñ–Ð´ÑÑƒÐ¼ÐºÐ¾Ð²Ñƒ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ, Ð±ÐµÐ· Ð¿Ð¾ÑÑÐ½ÐµÐ½ÑŒ."
    doc_entry = DOCS.get(uid)
    doc = f"Ð”Ð¾ÐºÑƒÐ¼ÐµÐ½Ñ‚ Ð²Ñ–Ð´ ÑƒÑ‡Ð¸Ñ‚ÐµÐ»Ñ:\n{doc_entry['text'][:MAX_PDF_CHARS]}\n\n" \
          if doc_entry and time.time() - doc_entry.get("ts", 0) < 1800 else ""
    return (
        f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ°:\n{context or '(Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð½Ð¸Ñ… Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ñ–Ð² Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾)'}\n\n"
        + doc
        + (f"ÐŸÐ¾Ð¿ÐµÑ€ÐµÐ´Ð½Ñ–Ð¹ Ð´Ñ–Ð°Ð»Ð¾Ð³:\n{hist}\n\n" if hist else "")
        + f"Ð¢Ð²Ð¾Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ: {mode_text}\n\nÐŸÐ¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ ÑƒÑ‡Ð½Ñ: {task}"
    )


def build_system(uid):
    return (SYSTEM_BASE + " " + LENGTH_INSTR[settings[uid]["length"]]).strip()


# ===========================================================================
# Ð’ÑÐ¿Ð¾Ð¼Ð¾Ð³Ð°Ñ‚ÐµÐ»ÑŒÐ½Ñ‹Ðµ Ñ„ÑƒÐ½ÐºÑ†Ð¸Ð¸
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
    """'Ñ„Ð¸Ð·Ð¸ÐºÐ° Ð·Ð°Ð´Ð°Ñ‡Ð° 3.5' -> ('Ñ„Ð¸Ð·Ð¸ÐºÐ°', 'Ð·Ð°Ð´Ð°Ñ‡Ð° 3.5')"""
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
    kb.add(tg.InlineKeyboardButton("ðŸ” Ð’Ð¸Ð·Ð½Ð°Ñ‡Ð¸Ñ‚Ð¸ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ð¾", callback_data="subj:auto"))
    return kb


def gate(message, cooldown=True):
    """ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð´Ð¾ÑÑ‚ÑƒÐ¿Ð°, Ð°Ð½Ñ‚Ð¸ÑÐ¿Ð°Ð¼Ð° Ð¸ Ð°Ð´Ñ€ÐµÑÐ¾Ð²Ð°Ð½Ð½Ð¾ÑÑ‚Ð¸ (Ð² Ð³Ñ€ÑƒÐ¿Ð¿Ð°Ñ…)."""
    uid = message.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.reply_to(message, f"Ð£ Ñ‚ÐµÐ±Ðµ Ð½ÐµÐ¼Ð°Ñ” Ð´Ð¾ÑÑ‚ÑƒÐ¿Ñƒ Ð´Ð¾ Ð±Ð¾Ñ‚Ð°. Ð¢Ð²Ñ–Ð¹ ID: {uid}")
        return False
    # Ð’ Ð³Ñ€ÑƒÐ¿Ð¿Ð¾Ð²Ð¸Ñ… Ñ‡Ð°Ñ‚Ð°Ñ… Ð±Ð¾Ñ‚ Ñ€ÐµÐ°Ð³ÑƒÑ” Ñ‚Ñ–Ð»ÑŒÐºÐ¸ ÐºÐ¾Ð»Ð¸ Ð¹Ð¾Ð³Ð¾ Ð¿Ñ€ÑÐ¼Ð¾ address (mention/reply)
    if not addressed(message):
        return False
    if cooldown:
        now = time.time()
        if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
            bot.reply_to(message, "Ð—Ð°Ñ‡ÐµÐºÐ°Ð¹ ÐºÑ–Ð»ÑŒÐºÐ° ÑÐµÐºÑƒÐ½Ð´ Ð¿ÐµÑ€ÐµÐ´ Ð½Ð°ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¼ Ð·Ð°Ð¿Ð¸Ñ‚Ð¾Ð¼.")
            return False
        last_request[uid] = now
    return True


@lru_cache(maxsize=128)
def book_path(subject):
    path = os.path.join(BOOKS_DIR, SUBJECTS[subject])
    return path if os.path.exists(path) else None


def _all_files_for_subject(stem):
    """Returns list of candidate filenames sharing the same language-agnostic stem."""
    try:
        prefix = re.split(r'[\-_ ]+', stem)[0].lower()
    except Exception:
        prefix = stem.lower()[:6]
    candidates = [SUBJECTS.get(stem)]
    for k, fn in SUBJECTS.items():
        if k != stem and fn.endswith('.pdf'):
            fname = os.path.splitext(fn)[0]
            if fname.startswith(prefix) and len(fname) > len(prefix) + 1:
                candidates.append(fn)
    return [c for c in candidates if c and os.path.isfile(os.path.join(BOOKS_DIR, c))]


def book_path_any(subject):
    """Try primary file; fall back to any sibling file with the same stem."""
    primary = book_path(subject)
    if primary:
        return primary
    stem = SUBJECTS.get(subject)
    if not stem:
        return None
    for candidate in _all_files_for_subject(stem):
        p = os.path.join(BOOKS_DIR, candidate)
        if os.path.exists(p):
            return p
    return None


# ===========================================================================
# ÐžÑÐ½Ð¾Ð²Ð½Ð¾Ð¹ ÐºÐ¾Ð½Ð²ÐµÐ¹ÐµÑ€
# ===========================================================================

    # Ð¡Ð»Ð¾Ð²Ð½Ð¸Ðº Ñ–Ð· Ñ‚Ð²Ð¾Ñ—Ð¼ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´Ð¾Ð¼ 8-Ð‘

    # ===========================================================================
    # Ð Ð¾Ð·ÐºÐ»Ð°Ð´ Ð¸ Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸ÐºÐ¸ Ð´Ð¾Ð¼Ð°ÑˆÐºÐ¸ (Ð’Ð«ÐÐ•Ð¡Ð•ÐÐž Ð˜Ð— PROCESS ÐÐÐ’Ð•Ð Ð¥)
    # ===========================================================================

# ÐšÐ»Ð°ÑÑ‹ ÑˆÐºÐ¾Ð»Ñ‹: Ð¸Ð¼Ñ -> ID Ð½Ð° ÑÐ°Ð¹Ñ‚Ðµ sch3.edu.vn.ua (Ñ‚Ð¾Ð»ÑŒÐºÐ¾ 8-Ð‘)
CLASSES = {
"8-Ð‘": "24",
}
DEFAULT_CLASS = "8-Ð‘"
# Ð Ð¾Ð·ÐºÐ»Ð°Ð´ 8-Ð‘ Ð²Ð¸Ð²ÐµÐ´ÐµÐ½Ð¸Ð¹ Ñ–Ð· Ñ€ÐµÐ°Ð»ÑŒÐ½Ð¸Ñ… Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ–Ñ… Ð·Ð°Ð²Ð´Ð°Ð½ÑŒ (Ð»Ð¸ÑÑ‚Ð¾Ð¿Ð°Ð´ 2026)
SCHEDULES = {
    "8-Ð‘": {
        "0": ["Ð“ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ", "ÐŸÑ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾ Ñ– Ñ„Ñ–Ð½Ð°Ð½ÑÐ¾Ð²Ð° Ð³Ñ€Ð°Ð¼Ð¾Ñ‚Ð½Ñ–ÑÑ‚ÑŒ", "Ð‘Ñ–Ð¾Ð»Ð¾Ð³Ñ–Ñ", "Ð¤Ñ–Ð·Ð¸ÐºÐ° Ñ– Ð°ÑÑ‚Ñ€Ð¾Ð½Ð¾Ð¼Ñ–Ñ", "Ð“ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ñ–Ñ", "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°"],
        "1": ["Ð†Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ð¸ÐºÐ°", "Ð¤Ñ–Ð·Ð¸ÐºÐ° Ñ– Ð°ÑÑ‚Ñ€Ð¾Ð½Ð¾Ð¼Ñ–Ñ", "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "Ð†ÑÑ‚Ð¾Ñ€Ñ–Ñ Ð£ÐºÑ€Ð°Ñ—Ð½Ð¸", "ÐÐ»Ð³ÐµÐ±Ñ€Ð°", "ÐÐ½Ð³Ð»Ñ–Ð¹ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°"],
        "2": ["Ð¢ÐµÑ…Ð½Ð¾Ð»Ð¾Ð³Ñ–Ñ—", "ÐÐ»Ð³ÐµÐ±Ñ€Ð°", "ÐÐ½Ð³Ð»Ñ–Ð¹ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "Ð†Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ð¸ÐºÐ°", "Ð—Ð°Ñ€ÑƒÐ±Ñ–Ð¶Ð½Ð° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°", "Ð—Ð´Ð¾Ñ€Ð¾Ð²'Ñ, Ð±ÐµÐ·Ð¿ÐµÐºÐ° Ñ‚Ð° Ð´Ð¾Ð±Ñ€Ð¾Ð±ÑƒÑ‚"],
        "3": ["Ð¥Ñ–Ð¼Ñ–Ñ", "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "ÐÐ½Ð³Ð»Ñ–Ð¹ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "Ð“ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ", "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°", "Ð†ÑÑ‚Ð¾Ñ€Ñ–Ñ Ð£ÐºÑ€Ð°Ñ—Ð½Ð¸", "Ð¤Ñ–Ð·Ð¸Ñ‡Ð½Ð° ÐºÑƒÐ»ÑŒÑ‚ÑƒÑ€Ð°"],
        "4": ["ÐÐ½Ð³Ð»Ñ–Ð¹ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°", "ÐÐ»Ð³ÐµÐ±Ñ€Ð°", "Ð†ÑÑ‚Ð¾Ñ€Ñ–Ñ Ð£ÐºÑ€Ð°Ñ—Ð½Ð¸", "Ð—Ð°Ñ€ÑƒÐ±Ñ–Ð¶Ð½Ð° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°"],
    }
}
@bot.message_handler(commands=['hw'])
def hw_menu(message):
    if not gate(message, cooldown=False):
        return
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("ðŸ“… ÐÐ° Ð·Ð°Ð²Ñ‚Ñ€Ð°", callback_data="hw_auto"))
    markup.row(InlineKeyboardButton("ÐŸÐ", callback_data="hw_0"),
               InlineKeyboardButton("Ð’Ð¢", callback_data="hw_1"),
               InlineKeyboardButton("Ð¡Ð ", callback_data="hw_2"))
    markup.row(InlineKeyboardButton("Ð§Ð¢", callback_data="hw_3"),
               InlineKeyboardButton("ÐŸÐ¢", callback_data="hw_4"))
    markup.add(InlineKeyboardButton("ðŸ—‘ Ð—Ð°ÐºÑ€Ð¸Ñ‚Ð¸", callback_data="delete_msg"))
    bot.send_message(message.chat.id, "ÐÐ° ÑÐºÐ¸Ð¹ Ð´ÐµÐ½ÑŒ Ð·Ñ–Ð±Ñ€Ð°Ñ‚Ð¸ Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ?", reply_markup=markup)

@bot.message_handler(commands=["schedule"])
def cmd_schedule(message):
    if not gate(message, cooldown=False):
        return
    class_name = user_class(message.chat.id)
    sched = SCHEDULES.get(class_name)
    if not sched:
        bot.reply_to(message, f"Ð Ð¾Ð·ÐºÐ»Ð°Ð´ Ð´Ð»Ñ {html.escape(class_name)} Ð¿Ð¾ÐºÐ¸ Ð½Ðµ Ð´Ð¾Ð´Ð°Ð½Ð¾, "
                              "Ð°Ð»Ðµ Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð¿Ñ€Ð°Ñ†ÑŽÑ”: /hw")
        return
    names = ["ÐŸÐ¾Ð½ÐµÐ´Ñ–Ð»Ð¾Ðº", "Ð’Ñ–Ð²Ñ‚Ð¾Ñ€Ð¾Ðº", "Ð¡ÐµÑ€ÐµÐ´Ð°", "Ð§ÐµÑ‚Ð²ÐµÑ€", "ÐŸâ€™ÑÑ‚Ð½Ð¸Ñ†Ñ"]
    text = f"ðŸ“… <b>Ð Ð¾Ð·ÐºÐ»Ð°Ð´ {html.escape(class_name)}</b>\n\n" + "\n\n".join(
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
# ÐÐ²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ðµ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´Ð°Ð½Ð½Ñ: Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´ Ð·Ð°Ð´Ð°Ð½Ð¾ Ð² ÐºÐ¾Ð´Ñ–,/site Ð½Ðµ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÑ”Ñ‚ÑŒÑÑ
# ===========================================================================
def _merge_classes_and_schedules():
    pass


def fetch_task_text(url):
    """Ð”Ð¾ÑÑ‚Ð°Ñ‘Ñ‚ Ñ‚ÐµÐºÑÑ‚ Ð¿Ð¾ÑÐ»Ðµ Ð·Ð°Ð³Ð¾Ð»Ð¾Ð²ÐºÐ° 'Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ' Ð´Ð¾ Ð½Ð¾Ð²Ð¾ÑÑ‚ÐµÐ¹/Ð¿Ð¾Ð´Ð²Ð°Ð»Ð° ÑÐ°Ð¹Ñ‚Ð°.
    Ð ÐµÐ·ÑƒÐ»ÑŒÑ‚Ð°Ñ‚ ÐºÑÑˆÐ¸Ñ€ÑƒÐµÑ‚ÑÑ 10 Ð¼Ð¸Ð½ÑƒÑ‚, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð½Ðµ Ð´Ñ‘Ñ€Ð³Ð°Ñ‚ÑŒ ÑÐ°Ð¹Ñ‚ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€Ð½Ð¾."""
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

        m = re.search(r'Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ”\s+Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ', page, re.I)
        if not m:
            return ""
        text = page[m.end():]

        # Ð¾Ð±Ñ€ÐµÐ·Ð°ÐµÐ¼ Ð¿Ð¾ Ð¿ÐµÑ€Ð²Ð¾Ð¼Ñƒ Ð¸Ð· Ð¼Ð°Ñ€ÐºÐµÑ€Ð¾Ð²: Ð±Ð¾ÐºÐ¾Ð²Ð°Ñ ÐºÐ¾Ð»Ð¾Ð½ÐºÐ° Ð½Ð¾Ð²Ð¾ÑÑ‚ÐµÐ¹ Ð¸Ð»Ð¸ Ð¿Ð¾Ð´Ð²Ð°Ð» ÑÐ°Ð¹Ñ‚Ð°
        cut = len(text)
        for marker in (r'ÐžÐ¡Ð¢ÐÐÐÐ†\s+ÐÐžÐ’Ð˜ÐÐ˜', r'ÐŸÑ€Ð¾\s+Ð½Ð°Ñ', r'ÐšÐ¾Ð¼ÑƒÐ½Ð°Ð»ÑŒÐ½Ð¸Ð¹\s+Ð·Ð°ÐºÐ»Ð°Ð´'):
            s = re.search(marker, text, re.I)
            if s:
                cut = min(cut, s.start())

        result = text[:cut].strip()
        _TASK_CACHE[url] = (time.time(), result)
        # _TASK_CACHE â€” ÑƒÐ¿Ñ€Ð¾Ñ‰Ñ‘Ð½Ð½Ñ‹Ð¹ dict (TTL 10 Ð¼Ð¸Ð½, Ñ€ÐµÐ´ÐºÐ¸Ðµ Ð²Ñ‹Ð·Ð¾Ð²Ñ‹), Ð±ÐµÐ· LRU
        return result
    except Exception:
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¾Ñ‚ÐºÑ€Ñ‹Ñ‚ÑŒ %s", url)
        return ""

_TASK_CACHE = {}  # url -> (ts, text) â€” ÐºÑÑˆ fetch_task_text Ð½Ð° 10 Ð¼Ð¸Ð½


def is_future(lesson_date):
    """True, ÐµÑÐ»Ð¸ Ð´Ð°Ñ‚Ð° ÑƒÑ€Ð¾ÐºÐ° (Ð´Ð´.Ð¼Ð¼.Ð³Ð³Ð³Ð³) Ð¿Ð¾Ð·Ð¶Ðµ ÑÐµÐ³Ð¾Ð´Ð½ÑÑˆÐ½ÐµÐ¹ Ð¿Ð¾ ÐšÐ¸ÐµÐ²Ñƒ."""
    try:
        d = datetime.strptime(lesson_date.strip(), "%d.%m.%Y").date()
        return d > datetime.now(KYIV).date()
    except ValueError:
        return False  # ÐµÑÐ»Ð¸ Ð´Ð°Ñ‚Ñƒ Ð½Ðµ Ñ€Ð°Ð·Ð¾Ð±Ñ€Ð°Ñ‚ÑŒ, Ð·Ð°Ð´Ð°Ð½Ð¸Ðµ Ð½Ðµ ÑÐºÑ€Ñ‹Ð²Ð°ÐµÐ¼
DAY_NAMES = ["Ð¿Ð¾Ð½ÐµÐ´Ñ–Ð»Ð¾Ðº", "Ð²Ñ–Ð²Ñ‚Ð¾Ñ€Ð¾Ðº", "ÑÐµÑ€ÐµÐ´Ñƒ", "Ñ‡ÐµÑ‚Ð²ÐµÑ€", "Ð¿â€™ÑÑ‚Ð½Ð¸Ñ†ÑŽ"]


def next_school_day():
    """Ð˜Ð½Ð´ÐµÐºÑ ÑÐ»ÐµÐ´ÑƒÑŽÑ‰ÐµÐ³Ð¾ ÑƒÑ‡ÐµÐ±Ð½Ð¾Ð³Ð¾ Ð´Ð½Ñ (0=ÐŸÐ ... 4=ÐŸÐ¢) Ð¿Ð¾ ÐšÐ¸ÐµÐ²Ñƒ. Ð’Ñ‹Ñ…Ð¾Ð´Ð½Ñ‹Ðµ Ð¿Ñ€Ð¾Ð¿ÑƒÑÐºÐ°ÐµÑ‚."""
    d = datetime.now(KYIV).date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.weekday()

# ÐÐ°Ð·Ð²Ð°Ð½Ð¸Ðµ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð° Ð² Ñ€Ð°ÑÐ¿Ð¸ÑÐ°Ð½Ð¸Ð¸ (ÑƒÐºÑ€.) -> ÐºÐ»ÑŽÑ‡ ÑƒÑ‡ÐµÐ±Ð½Ð¸ÐºÐ° Ð² SUBJECTS.
# Ð•ÑÐ»Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð° Ñ‚ÑƒÑ‚ Ð½ÐµÑ‚ (Ð°Ð½Ð³Ð»., Ð¸Ð½Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ð¸ÐºÐ°, Ñ„Ð¸Ð·ÐºÑƒÐ»ÑŒÑ‚ÑƒÑ€Ð°...), Ñ€ÐµÑˆÐ°ÐµÐ¼ Ð±ÐµÐ· ÑƒÑ‡ÐµÐ±Ð½Ð¸ÐºÐ°.
SUBJECT_KEYS = {
    "ÐÐ»Ð³ÐµÐ±Ñ€Ð°": "Ð°Ð»Ð³ÐµÐ±Ñ€Ð°",
    "ÐÐ½Ð³Ð»Ñ–Ð¹ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°": "none",
    "Ð‘Ñ–Ð¾Ð»Ð¾Ð³Ñ–Ñ": "Ð±Ð¸Ð¾Ð»Ð¾Ð³Ð¸Ñ",
    "Ð“ÐµÐ¾Ð³Ñ€Ð°Ñ„Ñ–Ñ": "Ð³ÐµÐ¾Ð³Ñ€Ð°Ñ„Ð¸Ñ",
    "Ð“ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ñ–Ñ": "Ð³ÐµÐ¾Ð¼ÐµÑ‚Ñ€Ð¸Ñ",
    "Ð—Ð°Ñ€ÑƒÐ±Ñ–Ð¶Ð½Ð° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°": "Ð·Ð°Ñ€ÑƒÐ±ÐµÐ¶ÐºÐ°",
    "Ð†ÑÑ‚Ð¾Ñ€Ñ–Ñ Ð£ÐºÑ€Ð°Ñ—Ð½Ð¸": "Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ñ",
    "ÐœÐ¸ÑÑ‚ÐµÑ†Ñ‚Ð²Ð¾": "Ð¼ÑƒÐ·Ñ‹ÐºÐ°",
    "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð»Ñ–Ñ‚ÐµÑ€Ð°Ñ‚ÑƒÑ€Ð°": "ÑƒÐºÑ€Ð»Ð¸Ñ‚",
    "Ð£ÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ° Ð¼Ð¾Ð²Ð°": "ÑƒÐºÑ€Ð¼Ð¾Ð²",
    "Ð¤Ñ–Ð·Ð¸ÐºÐ° Ñ– Ð°ÑÑ‚Ñ€Ð¾Ð½Ð¾Ð¼Ñ–Ñ": "Ñ„Ð¸Ð·Ð¸ÐºÐ°",
    "Ð¥Ñ–Ð¼Ñ–Ñ": "Ñ…Ð¸Ð¼Ð¸Ñ",
    "ÐŸÑ–Ð´Ð¿Ñ€Ð¸Ñ”Ð¼Ð½Ð¸Ñ†Ñ‚Ð²Ð¾ Ñ– Ñ„Ñ–Ð½Ð°Ð½ÑÐ¾Ð²Ð° Ð³Ñ€Ð°Ð¼Ð¾Ñ‚Ð½Ñ–ÑÑ‚ÑŒ": "entrepreneurship",
    "Ð¢ÐµÑ…Ð½Ð¾Ð»Ð¾Ð³Ñ–Ñ—": "technology",
    "Ð—Ð´Ð¾Ñ€Ð¾Ð²'Ñ, Ð±ÐµÐ·Ð¿ÐµÐºÐ° Ñ‚Ð° Ð´Ð¾Ð±Ñ€Ð¾Ð±ÑƒÑ‚": "none",
}
# Ð›Ð°Ñ‚Ð¸Ð½ÑÐºÐ¸Ðµ Ð±ÑƒÐºÐ²Ñ‹, Ð¿Ð¾Ñ…Ð¾Ð¶Ð¸Ðµ Ð½Ð° ÐºÐ¸Ñ€Ð¸Ð»Ð»Ð¸Ñ†Ñƒ (A B C E H K M O P T X), Ð¿Ñ€Ð¸Ð²Ð¾Ð´Ð¸Ð¼ Ðº ÐºÐ¸Ñ€Ð¸Ð»Ð»Ð¸Ñ†Ðµ.
# Ð—Ð°Ñ‰Ð¸Ñ‚Ð° Ð¾Ñ‚ Ð½ÐµÐ²Ð¸Ð´Ð¸Ð¼Ð¾Ð³Ð¾ Ð±Ð°Ð³Ð°: "6-Ð’" Ð² CLASSES Ð¸ SCHEDULES Ð½Ð°Ð±Ñ€Ð°Ð½Ñ‹ Ñ€Ð°Ð·Ð½Ñ‹Ð¼Ð¸ Ð°Ð»Ñ„Ð°Ð²Ð¸Ñ‚Ð°Ð¼Ð¸.
_HOMO = str.maketrans("ABCEHKMOPTX", "АВСЕНКМОРТХ")


def canon_class(name):
    """ÐšÐ°Ð½Ð¾Ð½Ð¸Ñ‡Ð½Ð¾Ðµ Ð¸Ð¼Ñ ÐºÐ»Ð°ÑÑÐ°: Ð²ÐµÑ€Ñ…Ð½Ð¸Ð¹ Ñ€ÐµÐ³Ð¸ÑÑ‚Ñ€, ÐºÐ¸Ñ€Ð¸Ð»Ð»Ð¸Ñ†Ð°, Ð±ÐµÐ· Ð´ÐµÑ„Ð¸ÑÐ¾Ð² Ð¸ Ð¿Ñ€Ð¾Ð±ÐµÐ»Ð¾Ð². '6-Ð’' == '6Ð’'."""
    return name.upper().translate(_HOMO).replace("-", "").replace("â€“", "").replace(" ", "").strip()


CLASSES = {canon_class(k): v for k, v in CLASSES.items()}
SCHEDULES = {canon_class(k): v for k, v in SCHEDULES.items()}
DEFAULT_CLASS = canon_class(DEFAULT_CLASS)


def _validate_classes():
    for _name in SCHEDULES:
        if _name not in CLASSES:
            log.warning("SCHEDULES: ÐºÐ»Ð°ÑÑ %r Ð¾Ñ‚ÑÑƒÑ‚ÑÑ‚Ð²ÑƒÐµÑ‚ Ð² CLASSES - Ð¿Ñ€Ð¾Ð²ÐµÑ€ÑŒ Ð±ÑƒÐºÐ²Ñ‹ (ÐºÐ¸Ñ€Ð¸Ð»Ð»Ð¸Ñ†Ð°/latinica)!", _name)


_validate_classes()


def _hw_kb(token, items, delete_data="delete_msg"):
    done = HW_DONE.setdefault(token, set())
    markup = InlineKeyboardMarkup()
    for i, (subject, text) in enumerate(items):
        if text:
            label = subject if len(subject) <= 18 else subject[:17] + "â€¦"
            markup.row(
                InlineKeyboardButton(f"âœï¸ {label}", callback_data=f"solve:{token}:{i}"),
                InlineKeyboardButton("â˜‘ï¸" if i in done else "âœ…", callback_data=f"hwdone:{token}:{i}"),
            )
    markup.add(InlineKeyboardButton("ðŸ—‘", callback_data=delete_data))
    return markup


def hw_markup(items, delete_data="delete_msg"):
    """ÐšÐ½Ð¾Ð¿ÐºÐ¸ 'âœï¸ ÐŸÑ€ÐµÐ´Ð¼ÐµÑ‚' (Ñ€ÐµÑˆÐ¸Ñ‚ÑŒ), 'âœ…' (Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð¾) Ð¸ ÐºÐ½Ð¾Ð¿ÐºÐ° ÑƒÐ´Ð°Ð»ÐµÐ½Ð¸Ñ."""
    token = uuid.uuid4().hex[:8]
    HW_CACHE.set(token, items)
    return _hw_kb(token, items, delete_data)

def build_hw_text(day_index, class_name=None):
    """Ð’Ð¾Ð·Ð²Ñ€Ð°Ñ‰Ð°ÐµÑ‚ (HTML-Ñ‚ÐµÐºÑÑ‚ Ð´Ð¾Ð¼Ð°ÑˆÐºÐ¸, ÑÐ¿Ð¸ÑÐ¾Ðº [(Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚, Ñ‚ÐµÐºÑÑ‚)]). Ð•ÑÐ»Ð¸ Ð·Ð°Ð´Ð°Ð½Ð¸Ð¹ Ð½ÐµÑ‚: (None, [])."""
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

    hw_text = f"ðŸ“š <b>Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ {html.escape(class_name)} Ð½Ð° {DAY_NAMES[int(day_index)]}:</b>\n\n"
    items = []
    for subject, text in zip(subs_list, texts):
        lesson_date, task_link = latest_tasks[subject]
        hw_text += f"ðŸ”¹ <b>{html.escape(subject)}</b> ({html.escape(lesson_date)})\n"
        if text:
            hw_text += html.escape(text[:400]) + "\n"
        else:
            hw_text += "(Ñ‚ÐµÐºÑÑ‚ Ð½Ðµ Ð½Ð°Ð¹Ð´ÐµÐ½)\n"
        hw_text += f'<a href="{html.escape(task_link, quote=True)}">ðŸ”—</a>\n\n'
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

    bot.edit_message_text("Ð¡ÐºÐ°Ð½ÑƒÑŽ ÑÐ°Ð¹Ñ‚ ÑˆÐºÐ¾Ð»Ð¸â€¦ ðŸ•µï¸â€â™‚ï¸", chat_id, msg_id)

    try:
        hw_text, items = build_hw_text(day_index, user_class(chat_id))
        if not hw_text:
            bot.edit_message_text("ÐÐ° Ñ†ÐµÐ¹ Ð´ÐµÐ½ÑŒ ÑÐ²Ñ–Ð¶Ð¸Ñ… Ð·Ð°Ð²Ð´Ð°Ð½ÑŒ Ð½Ð° ÑÐ°Ð¹Ñ‚Ñ– Ð¿Ð¾ÐºÐ¸ Ð½ÐµÐ¼Ð°Ñ”.", chat_id, msg_id)
            return

        markup = hw_markup(items)
        chunks = split_message(hw_text)
        for i, chunk in enumerate(chunks):
            m = markup if i == len(chunks) - 1 else None  # ÐºÐ½Ð¾Ð¿ÐºÐ¸ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð¿Ð¾Ð´ Ð¿Ð¾ÑÐ»ÐµÐ´Ð½ÐµÐ¹ Ñ‡Ð°ÑÑ‚ÑŒÑŽ
            if i == 0:
                bot.edit_message_text(chunk, chat_id, msg_id, parse_mode="HTML",
                                      reply_markup=m, disable_web_page_preview=True)
            else:
                bot.send_message(chat_id, chunk, parse_mode="HTML",
                                 reply_markup=m, disable_web_page_preview=True)
    except Exception as e:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð² /hw")
        bot.edit_message_text(f"Ð¡Ð°Ð¹Ñ‚ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹. ÐŸÐ¾Ð¼Ð¸Ð»ÐºÐ°: {e}", chat_id, msg_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("solve:"))
def on_solve(call):
    """ÐšÐ½Ð¾Ð¿ÐºÐ° 'âœï¸ ÐŸÑ€ÐµÐ´Ð¼ÐµÑ‚' Ð¿Ð¾Ð´ Ð´Ð¾Ð¼Ð°ÑˆÐºÐ¾Ð¹: Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð»ÑÐµÑ‚ Ð·Ð°Ð´Ð°Ð½Ð¸Ðµ Ð½Ð° Ñ€ÐµÑˆÐµÐ½Ð¸Ðµ."""
    uid = call.from_user.id
    if ALLOWED_IDS and uid not in ALLOWED_IDS and uid != ADMIN_ID:
        bot.answer_callback_query(call.id, "ÐÐµÐ¼Ð°Ñ” Ð´Ð¾ÑÑ‚ÑƒÐ¿Ñƒ.", show_alert=True)
        return
    try:
        _, token, idx = call.data.split(":")
        items = HW_CACHE.get(token)
        if not items:
            bot.answer_callback_query(call.id, "Ð¡Ð¿Ð¸ÑÐ¾Ðº Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð², Ð²Ñ–Ð´ÐºÑ€Ð¸Ð¹ /hw Ñ‰Ðµ Ñ€Ð°Ð·.", show_alert=True)
            return
        subject_ua, task_text = items[int(idx)]
    except Exception:
        bot.answer_callback_query(call.id, "Ð¡Ð¿Ð¸ÑÐ¾Ðº Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð², Ð²Ñ–Ð´ÐºÑ€Ð¸Ð¹ /hw Ñ‰Ðµ Ñ€Ð°Ð·.", show_alert=True)
        return
    now = time.time()
    if now - last_request.get(uid, 0) < COOLDOWN_SECONDS:
        bot.answer_callback_query(call.id, "Ð—Ð°Ñ‡ÐµÐºÐ°Ð¹ ÐºÑ–Ð»ÑŒÐºÐ° ÑÐµÐºÑƒÐ½Ð´.")
        return
    last_request[uid] = now

    keys = SUBJECT_KEYS
    subject = keys.get(subject_ua, "none")
    bot.answer_callback_query(call.id, "Ð Ð¾Ð·Ð²'ÑÐ·ÑƒÑŽâ€¦")
    task = f"{subject_ua}. Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ: {task_text[:1500]}"
    # Ð•ÑÐ»Ð¸ Ð² Ð·Ð°Ð´Ð°Ð½Ð¸Ð¸ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ Ð½Ð¾Ð¼ÐµÑ€Ð° Ð±ÐµÐ· Ñ‚ÐµÐºÑÑ‚Ð° - Ð¿Ñ€ÐµÐ´ÑƒÐ¿Ñ€ÐµÐ´Ð¸Ð¼, Ñ‡Ñ‚Ð¾ ÑƒÑÐ»Ð¾Ð²Ð¸Ñ Ð¼Ð¾Ð³ÑƒÑ‚ Ð½Ðµ Ð½Ð°Ð¹Ñ‚Ð¸ÑÑŒ
    if re.search(r"[â„–N]\s*â„–?\s*\d+|\d{1,3}\s*[,.-]\s*\d{1,3}", task_text) and len(task_text) < 120:
        bot.send_message(call.message.chat.id,
                         f"â„¹ï¸ Ð£ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ– Ð»Ð¸ÑˆÐµ Ð½Ð¾Ð¼ÐµÑ€Ð¸ ({subject_ua}). Ð¯ÐºÑ‰Ð¾ Ð² ÑƒÐ¼Ð¾Ð²Ñ– Ð½Ðµ Ð²Ð¸ÑÑ‚Ð°Ñ‡Ð¸Ñ‚ÑŒ Ð´Ð°Ð½Ð¸Ñ… â€” "
                         "Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð¾Ñ‚Ð¾ ÑÑ‚Ð¾Ñ€Ñ–Ð½ÐºÐ¸ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Ð· Ñ†Ð¸Ð¼Ð¸ Ð½Ð¾Ð¼ÐµÑ€Ð°Ð¼Ð¸ â€” Ñ€Ð¾Ð·Ð²â€™ÑÐ¶Ñƒ Ñ‚Ð¾Ñ‡Ð½Ð¾.")
    process(call.message.chat.id, call.message.message_id, uid,
            call.from_user.first_name, "answer", subject, task, [])

@bot.callback_query_handler(func=lambda call: call.data.startswith("hwdone:"))
def on_hw_done(call):
    """ÐšÐ½Ð¾Ð¿ÐºÐ° âœ… Ð¿Ð¾Ð´ Ð´Ð¾Ð¼Ð°ÑˆÐºÐ¾Ð¹: Ð¾Ñ‚Ð¼ÐµÑ‚Ð¸Ñ‚ÑŒ Ð·Ð°Ð´Ð°Ð½Ð¸Ðµ Ð²Ñ‹Ð¿Ð¾Ð»Ð½ÐµÐ½Ð½Ñ‹Ð¼."""
    try:
        _, token, idx = call.data.split(":")
        idx = int(idx)
        items = HW_CACHE.get(token)
    except Exception:
        bot.answer_callback_query(call.id, "Ð¡Ð¿Ð¸ÑÐ¾Ðº Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð², Ð²Ñ–Ð´ÐºÑ€Ð¸Ð¹ /hw Ñ‰Ðµ Ñ€Ð°Ð·.", show_alert=True)
        return
    if not items:
        bot.answer_callback_query(call.id, "Ð¡Ð¿Ð¸ÑÐ¾Ðº Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð², Ð²Ñ–Ð´ÐºÑ€Ð¸Ð¹ /hw Ñ‰Ðµ Ñ€Ð°Ð·.", show_alert=True)
        return
    done = HW_DONE.setdefault(token, set())
    if idx in done:
        done.discard(idx)
        msg = "ÐŸÐ¾Ð²ÐµÑ€Ð½ÑƒÐ² Ð´Ð¾ ÑÐ¿Ð¸ÑÐºÑƒ."
    else:
        done.add(idx)
        left = sum(1 for i, (s, t) in enumerate(items) if t and i not in done)
        msg = "Ð£ÑÐµ Ð²Ð¸ÐºÐ¾Ð½Ð°Ð½Ð¾! ðŸŽ‰" if left == 0 else f"ÐœÐ¾Ð»Ð¾Ð´ÐµÑ†ÑŒ! ðŸ’ª Ð—Ð°Ð»Ð¸ÑˆÐ¸Ð»Ð¾ÑÑ: {left}"
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id,
                                      reply_markup=_hw_kb(token, items))
    except Exception:
        pass
    bot.answer_callback_query(call.id, msg)


# ===========================================================================
# Ð•Ð¶ÐµÐ´Ð½ÐµÐ²Ð½Ð°Ñ Ñ€Ð¾Ð·ÑÐ¸Ð»ÐºÐ° Ð´Ð¾Ð¼Ð°ÑˆÐ½ÑŒÐ¾Ð³Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ
# ===========================================================================
SEND_HOUR, SEND_MINUTE = 16, 0          # Ð²Ñ€ÐµÐ¼Ñ Ñ€Ð°ÑÑÑ‹Ð»ÐºÐ¸ Ð¿Ð¾ ÐšÐ¸ÐµÐ²Ñƒ
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
# Ð’Ñ‹Ð±Ñ€Ð°Ð½Ð½Ñ‹Ð¹ ÐºÐ»Ð°ÑÑ Ð´Ð»Ñ ÐºÐ°Ð¶Ð´Ð¾Ð³Ð¾ Ñ‡Ð°Ñ‚Ð° (Ð¿ÐµÑ€ÐµÐ¶Ð¸Ð²Ð°ÐµÑ‚ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐº Ñ‡ÐµÑ€ÐµÐ· Redis)
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
    """ÐšÐ»Ð°Ñ Ñ‡Ð°Ñ‚Ð° Ñ Ð·Ð°Ñ‰Ð¸Ñ‚Ð¾Ð¹ Ð¾Ñ‚ ÑƒÐ´Ð°Ð»Ñ‘Ð½Ð½Ñ‹Ñ…/Ð¿ÐµÑ€ÐµÐ¸Ð¼ÐµÐ½Ð¾Ð²Ð°Ð½Ð½Ñ‹Ñ… ÐºÐ»Ð°ÑÑÐ¾Ð²."""
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
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐ¾Ñ…Ñ€Ð°Ð½Ð¸Ñ‚ÑŒ Ð¿Ð¾Ð´Ð¿Ð¸ÑÑ‡Ð¸ÐºÐ¾Ð²")


def broadcast_targets():
    """Ð§Ð°Ñ‚Ñ‹ Ð´Ð»Ñ Ñ€Ð°ÑÑÑ‹Ð»ÐºÐ¸: Ð¿ÐµÑ€ÐµÐ¼ÐµÐ½Ð½Ð°Ñ BROADCAST_IDS + Ð¿Ð¾Ð´Ð¿Ð¸ÑÐ°Ð²ÑˆÐ¸ÐµÑÑ Ñ‡ÐµÑ€ÐµÐ· /subscribe.
    Ð•ÑÐ»Ð¸ Ð½Ð¸ÐºÐ¾Ð³Ð¾ Ð½ÐµÑ‚ - ÑˆÐ»Ñ‘Ð¼ Ð°Ð´Ð¼Ð¸Ð½Ñƒ."""
    ids = {int(x) for x in _split_env("BROADCAST_IDS")} | set(subs)
    if not ids and ADMIN_ID:
        ids = {ADMIN_ID}
    return ids


@bot.callback_query_handler(func=lambda call: call.data == "del_one")
def delete_one(call):
    """Ð£Ð´Ð°Ð»ÑÐµÑ‚ Ñ‚Ð¾Ð»ÑŒÐºÐ¾ ÑÐ°Ð¼Ð¾ ÑÐ¾Ð¾Ð±Ñ‰ÐµÐ½Ð¸Ðµ (Ð´Ð»Ñ Ñ€Ð°ÑÑÑ‹Ð»ÐºÐ¸, Ð³Ð´Ðµ Ð¿Ñ€ÐµÐ´Ñ‹Ð´ÑƒÑ‰ÐµÐµ ÑÐ¾Ð¾Ð±Ñ‰ÐµÐ½Ð¸Ðµ Ñ‡ÑƒÐ¶Ð¾Ðµ)."""
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
        bot.answer_callback_query(call.id, "Ð¢Ð°ÐºÐ¾Ð³Ð¾ ÐºÐ»Ð°ÑÑƒ Ð½ÐµÐ¼Ð°Ñ”.", show_alert=True)
        return
    USER_CLASS[call.message.chat.id] = name
    class_save()
    bot.answer_callback_query(call.id, f"ÐšÐ»Ð°Ñ: {name}")
    try:
        bot.edit_message_text(f"âœ… ÐšÐ»Ð°Ñ Ð¾Ð±Ñ€Ð°Ð½Ð¾: <b>{html.escape(name)}</b>\n"
                              "Ð”Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ: /hw Â· Ñ€Ð¾Ð·ÑÐ¸Ð»ÐºÐ°: /subscribe Â· Ð·Ð¼Ñ–Ð½Ð¸Ñ‚Ð¸: /class",
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
    bot.reply_to(message, f"Ð“Ð¾Ñ‚Ð¾Ð²Ð¾! ÐšÐ¾Ð¶Ð½Ð¾Ð³Ð¾ Ð½Ð°Ð²Ñ‡Ð°Ð»ÑŒÐ½Ð¾Ð³Ð¾ Ð´Ð½Ñ Ð² {SEND_HOUR}:{SEND_MINUTE:02d} "
                          "Ð½Ð°Ð´ÑÐ¸Ð»Ð°Ñ‚Ð¸Ð¼Ñƒ Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð½Ð° Ð½Ð°ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹ Ð´ÐµÐ½ÑŒ. Ð’Ñ–Ð´Ð¿Ð¸ÑÐ°Ñ‚Ð¸ÑÑ: /unsubscribe")


@bot.message_handler(commands=["unsubscribe"])
def cmd_unsubscribe(message):
    if not gate(message, cooldown=False):
        return
    with _subs_lock:
        subs.discard(message.chat.id)
        _save_subs()
    bot.reply_to(message, "Ð Ð¾Ð·ÑÐ¸Ð»ÐºÑƒ Ð²Ð¸Ð¼ÐºÐ½ÐµÐ½Ð¾.")


RETRY_ATTEMPTS = 4          # ÑÐºÐ¾Ð»ÑŒÐºÐ¾ Ñ€Ð°Ð· Ð¿Ñ€Ð¾Ð±ÑƒÐµÐ¼ Ð´Ð¾ÑÑ‚ÑƒÑ‡Ð°Ñ‚ÑŒÑÑ Ð´Ð¾ ÑÐ°Ð¹Ñ‚Ð° ÑˆÐºÐ¾Ð»Ñ‹
RETRY_DELAY = 10 * 60       # Ð¿Ð°ÑƒÐ·Ð° Ð¼ÐµÐ¶Ð´Ñƒ Ð¿Ð¾Ð¿Ñ‹Ñ‚ÐºÐ°Ð¼Ð¸, ÑÐµÐºÑƒÐ½Ð´


def send_daily():
    day_index = next_school_day()
    built = {}   # ÐºÐ»Ð°ÑÑ -> (hw_text, items, site_ok); ÑÐ°Ð¹Ñ‚ Ð´Ñ‘Ñ€Ð³Ð°ÐµÐ¼ Ð¾Ð´Ð¸Ð½ Ñ€Ð°Ð· Ð½Ð° ÐºÐ»Ð°ÑÑ
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
                    log.exception("Ð Ð°ÑÑÑ‹Ð»ÐºÐ°: ÑÐ°Ð¹Ñ‚ Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿ÐµÐ½ (Ð¿Ð¾Ð¿Ñ‹Ñ‚ÐºÐ° %d Ð¸Ð· %d)", attempt, RETRY_ATTEMPTS)
                    if attempt < RETRY_ATTEMPTS:
                        time.sleep(RETRY_DELAY)
            built[cname] = (hw_text, items, site_ok)
        hw_text, items, site_ok = built[cname]
        if not site_ok:
            try:
                bot.send_message(chat_id, "âš ï¸ Ð¡Ð°Ð¹Ñ‚ ÑˆÐºÐ¾Ð»Ð¸ Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, Ð¾Ñ‚Ñ€Ð¸Ð¼Ð°Ñ‚Ð¸ Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð½Ðµ Ð²Ð´Ð°Ð»Ð¾ÑÑ. "
                                          "Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾ÑŽ /hw.")
            except Exception:
                log.exception("Ð Ð°ÑÑÑ‹Ð»ÐºÐ°: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð¸Ñ‚ÑŒ ÑƒÐ²ÐµÐ´Ð¾Ð¼Ð»ÐµÐ½Ð¸Ðµ Ð² Ñ‡Ð°Ñ‚ %s", chat_id)
            continue
        if not hw_text:
            log.info("Ð Ð°ÑÑÑ‹Ð»ÐºÐ°: Ñ‡Ð°Ñ‚ %s (ÐºÐ»Ð°ÑÑ %s): Ð·Ð°Ð´Ð°Ð½Ð¸Ð¹ Ð½ÐµÑ‚", chat_id, cname)
            continue
        chunks = split_message(hw_text)
        try:
            markup = hw_markup(items, "del_one")
            for i, chunk in enumerate(chunks):
                bot.send_message(chat_id, chunk, parse_mode="HTML",
                                 reply_markup=markup if i == len(chunks) - 1 else None,
                                 disable_web_page_preview=True)
        except Exception:
            log.exception("Ð Ð°ÑÑÑ‹Ð»ÐºÐ°: Ð½Ðµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð¸Ñ‚ÑŒ Ð² Ñ‡Ð°Ñ‚ %s", chat_id)




def broadcast_loop():
    """Ð Ð°ÑÑÑ‹Ð»ÐºÐ°: Ñ€Ð°Ð· Ð² ÑÑƒÑ‚ÐºÐ¸, Ð² Ð¾ÐºÐ½Ð¾ SEND_HOUR..SEND_HOUR+10 (Ð¿Ð¾ ÐšÐ¸ÐµÐ²Ñƒ)."""
    start = SEND_HOUR * 60 + SEND_MINUTE
    while True:
        try:
            now = datetime.now(KYIV)
            minutes = now.hour * 60 + now.minute
            # Ð¾ÐºÐ½Ð¾ 10 Ð¼Ð¸Ð½ÑƒÑ‚: ÐµÑÐ»Ð¸ Ð±Ð¾Ñ‚ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÑ‚Ð¸Ñ‚ÑÑ Ð¿Ð¾Ð·Ð¶Ðµ, ÑÑ‚Ð°Ñ€ÑƒÑŽ Ñ€Ð°ÑÑÑ‹Ð»ÐºÑƒ Ð½Ðµ ÑˆÐ»Ñ‘Ð¼
            if now.weekday() < 5 and start <= minutes < start + 10:
                if once("broadcast"):
                    send_daily()
        except Exception:
            log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð¿Ð»Ð°Ð½Ð¸Ñ€Ð¾Ð²Ñ‰Ð¸ÐºÐ°")
        time.sleep(20)


@bot.callback_query_handler(func=lambda call: call.data.startswith("subj:"))
def on_subject_pick(call):
    """ÐÐ°Ð¶Ð°Ñ‚Ð¸Ðµ Ð½Ð° ÐºÐ½Ð¾Ð¿ÐºÑƒ Ð²Ñ‹Ð±Ð¾Ñ€Ð° Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð° (Ñ€Ð°Ð½ÑŒÑˆÐµ Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸ÐºÐ° Ð½Ðµ Ð±Ñ‹Ð»Ð¾)."""
    uid = call.from_user.id
    req = pending.pop(uid, None)
    if not req:
        bot.answer_callback_query(call.id, "Ð—Ð°Ð¿Ð¸Ñ‚ Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð², Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ñ‰Ðµ Ñ€Ð°Ð·.")
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
        bot.send_message(chat_id, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.",
                         reply_to_message_id=reply_to)
        return

    register_use(uid, name)
    history[uid].append((task, "PENDING"))

    status = bot.send_message(chat_id, "Ð¨ÑƒÐºÐ°ÑŽ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ñƒ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÑƒâ€¦ â±ï¸", reply_to_message_id=reply_to)
    bot.send_chat_action(chat_id, "typing")

    try:
        if media:
            extracted = extract_task(media)
            if extracted:
                task = f"{task}\n{extracted}".strip()
            elif not task:
                bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ñ€Ð¾Ð·Ñ–Ð±Ñ€Ð°Ñ‚Ð¸ Ñ„Ð¾Ñ‚Ð¾ Ð°Ð±Ð¾ Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ðµ Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                      chat_id, status.message_id)
                return

        auto_note = ""
        general = False  # True = Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ñ”Ð¼Ð¾ Ð±ÐµÐ· Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ°
        if subject == "none":
            subject, general = None, True
        elif subject == "auto":
            subject = auto_detect(task)
            if subject:
                auto_note = f"ðŸ“š ÐŸÑ€ÐµÐ´Ð¼ÐµÑ‚: {subject}\n\n"
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
                bot.edit_message_text(f"Ð¤Ð°Ð¹Ð» Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Â«{SUBJECTS[subject]}Â» Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾ Ð² Ð¿Ð°Ð¿Ñ†Ñ– Â«ÐºÐ½Ð¸Ð³Ð¸Â».",
                                      chat_id, status.message_id)
                return
            pages = load_pages(path)
            if not any(pages):
                log.warning("Ð£Ñ‡ÐµÐ±Ð½Ð¸Ðº Ð±ÐµÐ· Ñ‚ÐµÐºÑÑ‚Ð¾Ð²Ð¾Ð³Ð¾ ÑÐ»Ð¾Ñ (ÑÐºÐ°Ð½): %s", path)
                general = True
                context, pages = "", []
                auto_note += (f"ðŸ“Œ ÐŸÑ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸Ðº Â«{SUBJECTS[subject]}Â» Ð²Ð¸ÑÐ²Ð¸Ð²ÑÑ ÑÐºÐ°Ð½Ð¾Ð¼ Ð±ÐµÐ· Ñ‚ÐµÐºÑÑ‚Ð¾Ð²Ð¾Ð³Ð¾ ÑˆÐ°Ñ€Ñƒ, "
                              "Ñ‚Ð¾Ð¼Ñƒ Ñ€Ð¾Ð·Ð²â€™ÑÐ·ÑƒÑŽ Ð·Ð° Ð·Ð°Ð³Ð°Ð»ÑŒÐ½Ð¸Ð¼Ð¸ Ð·Ð½Ð°Ð½Ð½ÑÐ¼Ð¸. Ð”Ð»Ñ Ñ‚Ð¾Ñ‡Ð½Ð¾ÑÑ‚Ñ– Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð¾Ñ‚Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ.\n\n")
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
                raise RuntimeError("Gemini Ð²ÐµÑ€Ð½ÑƒÐ» Ð¿ÑƒÑÑ‚Ð¾Ð¹ Ð¾Ñ‚Ð²ÐµÑ‚")
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
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚ÐºÐ¸ Ð·Ð°Ð¿Ñ€Ð¾ÑÐ°")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.", chat_id, status.message_id)
        except Exception:
            pass
def parse_json(raw):
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    return json.loads(raw)
# ===========================================================================
# /setschedule - Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´ Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶ÑƒÑ”Ñ‚ÑŒÑÑ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ð¾ Ð· ÐºÐ¾Ð´Ñƒ (Ñ„Ñ–ÐºÑÐ¾Ð²Ð°Ð½Ð¸Ð¹ Ð´Ð»Ñ 8-Ð‘)
# ===========================================================================
@bot.message_handler(commands=["setschedule"])
def cmd_setschedule(message):
    bot.reply_to(message, "âš ï¸ Ð Ð¾Ð·ÐºÐ»Ð°Ð´ Ð´Ð»Ñ 8-Ð‘ Ð²Ð¶Ðµ Ð·Ð°Ð´Ð°Ð½Ð¸Ð¹ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ð¾ Ð² ÐºÐ¾Ð´Ñ–.\n"
                           "Ð”Ð¾ÑÑ‚ÑƒÐ¿Ð½Ñ– ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¸:\n"
                           "/hw â€” Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ\n"
                           "/schedule â€” Ð¿Ð¾ÐºÐ°Ð·Ð°Ñ‚Ð¸ Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´")


@bot.message_handler(commands=["addclass"])
def cmd_addclass(message):
    bot.reply_to(message, "âŒ ÐšÐ¾Ð¼Ð°Ð½Ð´Ñƒ /addclass Ð²Ð¸Ð¼ÐºÐ½ÐµÐ½Ð¾. Ð Ð¾Ð·ÐºÐ»Ð°Ð´ Ð´Ð»Ñ 8-Ð‘ Ð·Ð°Ð´Ð°Ð½Ð¸Ð¹ Ð°Ð²Ñ‚Ð¾Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ð¾.")


def run_quiz(message, subject, topic):
    uid = message.from_user.id
    chat_id = message.chat.id
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Ð¤Ð°Ð¹Ð» Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Â«{SUBJECTS[subject]}Â» Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾.")
        return

    status = bot.reply_to(message, "Ð“Ð¾Ñ‚ÑƒÑŽ Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñâ€¦ â±ï¸")
    bot.send_chat_action(chat_id, "typing")
    try:
        pages = load_pages(path)
        if topic:
            context, _ = find_context(path, topic)
        else:
            rich = [p for p in pages if len(p) > 400]
            context = "\n\n".join(random.sample(rich, min(4, len(rich)))) if rich else ""
        if not context:
            bot.edit_message_text("ÐÐµ Ð·Ð½Ð°Ð¹ÑˆÐ¾Ð² Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð½Ð¸Ñ… ÑÑ‚Ð¾Ñ€Ñ–Ð½Ð¾Ðº Ð·Ð° Ñ†Ñ–Ñ”ÑŽ Ñ‚ÐµÐ¼Ð¾ÑŽ.", chat_id, status.message_id)
            return

        prompt = (
            "ÐÐ° Ð¾ÑÐ½Ð¾Ð²Ñ– Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ñ–Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° ÑÐºÐ»Ð°Ð´Ð¸ 5 Ñ‚ÐµÑÑ‚Ð¾Ð²Ð¸Ñ… Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½ÑŒ Ñ–Ð· 4 Ð²Ð°Ñ€Ñ–Ð°Ð½Ñ‚Ð°Ð¼Ð¸ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ– "
            "(Ñ€Ñ–Ð²Ð½Ð¾ Ð¾Ð´Ð¸Ð½ Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¸Ð¹). ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON-Ð¼Ð°ÑÐ¸Ð² Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð²Ð¸Ð³Ð»ÑÐ´Ñƒ "
            '[{"q": "Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ", "options": ["a","b","c","d"], "correct": 0, "explain": "Ð¿Ð¾ÑÑÐ½ÐµÐ½Ð½Ñ Ð´Ð¾ 150 ÑÐ¸Ð¼Ð²Ð¾Ð»Ñ–Ð²"}]. '
            "Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð¿Ð¸ÑˆÐ¸ ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾ÑŽ. ÐÐµ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÐ¹ HTML.\n\n"
            f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸:\n{context}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.", chat_id, status.message_id)
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
                log.warning("ÐŸÑ€Ð¾Ð¿ÑƒÑ‰ÐµÐ½ Ð½ÐµÐºÐ¾Ñ€Ñ€ÐµÐºÑ‚Ð½Ñ‹Ð¹ Ð²Ð¾Ð¿Ñ€Ð¾Ñ: %s", it)

        register_use(uid, message.from_user.first_name)
        settings[uid]["subject"] = subject
        if sent:
            bot.delete_message(chat_id, status.message_id)
        else:
            bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ ÑÐºÐ»Ð°ÑÑ‚Ð¸ Ñ‚ÐµÑÑ‚, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.", chat_id, status.message_id)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ñ‚ÐµÑÑ‚Ð°")
        bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ° Ð¿Ñ–Ð´ Ñ‡Ð°Ñ ÑÑ‚Ð²Ð¾Ñ€ÐµÐ½Ð½Ñ Ñ‚ÐµÑÑ‚Ñƒ.", chat_id, status.message_id)


# ===========================================================================
# ÐšÐ¾Ð¼Ð°Ð½Ð´Ð¸
# ===========================================================================
HELP_TEXT = (
    "<b>Ð¯Ðº ÐºÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‚Ð¸ÑÑ</b>\n"
    "ÐŸÐ¸ÑˆÐ¸: <code>Ñ„Ñ–Ð·Ð¸ÐºÐ° Ð·Ð°Ð´Ð°Ñ‡Ð° 3.5</code> â€” Ð°Ð±Ð¾ Ð¿Ñ€Ð¾ÑÑ‚Ð¾ Ñ‚ÐµÐºÑÑ‚ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ (Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚ Ð·Ð°Ð¿Ð°Ð¼â€™ÑÑ‚Ð°Ñ”Ñ‚ÑŒÑÑ).\n"
    "ÐœÐ¾Ð¶Ð½Ð° Ð½Ð°Ð´Ñ–ÑÐ»Ð°Ñ‚Ð¸ <b>Ñ„Ð¾Ñ‚Ð¾</b>, <b>Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ðµ Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ</b> Ð°Ð±Ð¾ <b>PDF-Ñ„Ð°Ð¹Ð»</b> Ð²Ñ–Ð´ ÑƒÑ‡Ð¸Ñ‚ÐµÐ»Ñ.\n"
    "Ð©Ð¾Ð± Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ñ‚Ð¸ Ð¿Ñ€Ð¾ PDF â€” Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð°Ð¹Ð» Ñ–Ð· Ð¿Ñ–Ð´Ð¿Ð¸ÑÐ¾Ð¼: Â«Ð¿Ð¾ÑÑÐ½Ð¸Â», Â«Ñ€Ð¾Ð·Ð²â€™ÑÐ¶Ð¸ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ 3Â».\n\n"
    "<b>ÐšÐ¾Ð¼Ð°Ð½Ð´Ð¸</b>\n"
    "/hw â€” Ð´Ð¾Ð¼Ð°ÑˆÐ½Ñ” Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ñ–Ð· ÑÐ°Ð¹Ñ‚Ñƒ ÑˆÐºÐ¾Ð»Ð¸ (Ñ” Â«ÐÐ° Ð·Ð°Ð²Ñ‚Ñ€Ð°Â»)\n"
    "/subscribe, /unsubscribe â€” Ñ€Ð¾Ð·ÑÐ¸Ð»ÐºÐ° Ð´Ð¾Ð¼Ð°ÑˆÐ½ÑŒÐ¾Ð³Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð¾ 16:00\n"
    "/schedule â€” Ñ€Ð¾Ð·ÐºÐ»Ð°Ð´ ÑƒÑ€Ð¾ÐºÑ–Ð²\n"
    "/class — бот працює лише з 8-Б 🏫\n"
    "/subjects â€” ÑÐ¿Ð¸ÑÐ¾Ðº Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ñ–Ð²\n"
    "/subject Ñ„Ñ–Ð·Ð¸ÐºÐ° â€” Ð¾Ð±Ñ€Ð°Ñ‚Ð¸ Ð¿Ð¾Ñ‚Ð¾Ñ‡Ð½Ð¸Ð¹ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚\n"
    "/explain [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ â€” Ð¿Ð¾ÑÑÐ½Ð¸Ñ‚Ð¸, Ð¯Ðš Ñ€Ð¾Ð·Ð²â€™ÑÐ·ÑƒÐ²Ð°Ñ‚Ð¸\n"
    "/check [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº â€” Ð¿ÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ñ‚Ð²Ñ–Ð¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº (Ð¼Ð¾Ð¶Ð½Ð° Ñ„Ð¾Ñ‚Ð¾ Ð· Ð¿Ñ–Ð´Ð¿Ð¸ÑÐ¾Ð¼ /check)\n"
    "/summary [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] Ñ‚ÐµÐ¼Ð° â€” ÑˆÐ¿Ð°Ñ€Ð³Ð°Ð»ÐºÐ° Ð´Ð»Ñ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð½Ñ\n"
    "/quiz [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] [Ñ‚ÐµÐ¼Ð°] â€” Ñ‚ÐµÑÑ‚ Ñ–Ð· 5 Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½ÑŒ\n"
    "/top â€” Ñ€ÐµÐ¹Ñ‚Ð¸Ð½Ð³ ÐºÐ»Ð°ÑÑƒ ðŸ†\n"
    "ÐŸÑ–Ð´ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð´ÑŽ: ðŸ” Ð¿Ð¾Ð´Ñ–Ð±Ð½Ðµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Â· âœï¸ ÑÐ²Ð¾Ñ—Ð¼Ð¸ ÑÐ»Ð¾Ð²Ð°Ð¼Ð¸ Â· ðŸ‘/ðŸ‘Ž\n"
    "/exam [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] [Ñ‚ÐµÐ¼Ð°] â€” Ð¿Ñ€Ð¾Ð±Ð½Ð° ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° ðŸ“\n"
    "/formulas [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] â€” Ñ‚Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ„Ð¾Ñ€Ð¼ÑƒÐ» ðŸ§®\n"
    "/cards [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] [Ñ‚ÐµÐ¼Ð°] â€” ÐºÐ°Ñ€Ñ‚ÐºÐ¸ Ð´Ð»Ñ Ð·Ð°Ð¿Ð°Ð¼â€™ÑÑ‚Ð¾Ð²ÑƒÐ²Ð°Ð½Ð½Ñ ðŸƒ\n"
    "/cards â€” Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€Ð¸Ñ‚Ð¸ Ð½Ð°ÐºÐ¾Ð¿Ð¸Ñ‡ÐµÐ½Ñ– ÐºÐ°Ñ€Ñ‚ÐºÐ¸\n"
    "/proofread [Ñ‚ÐµÐºÑÑ‚] â€” Ð¿ÐµÑ€ÐµÐ²Ñ–Ñ€Ð¸Ñ‚Ð¸ Ð¾Ñ€Ñ„Ð¾Ð³Ñ€Ð°Ñ„Ñ–ÑŽ âœï¸\n"
    "/remind 20.05 ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° â€” Ð½Ð°Ð³Ð°Ð´ÑƒÐ²Ð°Ð½Ð½Ñ â°\n"
    "/pomodoro 25 â€” Ñ‚Ð°Ð¹Ð¼ÐµÑ€ Ð½Ð°Ð²Ñ‡Ð°Ð½Ð½Ñ/Ð¿ÐµÑ€ÐµÑ€Ð²Ð¸ ðŸ…\n"
    "/menu â€” Ð¿Ð¾ÐºÐ°Ð·Ð°Ñ‚Ð¸ ÐºÐ½Ð¾Ð¿ÐºÐ¸ Ð¾ÑÐ½Ð¾Ð²Ð½Ð¸Ñ… ÐºÐ¾Ð¼Ð°Ð½Ð´\n"
    "/short, /detail, /normal â€” Ð´Ð¾Ð²Ð¶Ð¸Ð½Ð° Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÐµÐ¹\n"
    "/essay [Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚] Ñ‚ÐµÐ¼Ð° â€” Ð½Ð°Ð¿Ð¸ÑÐ°Ñ‚Ð¸ Ñ‚Ð²Ñ–Ñ€\n"
    "/quick â€” Ñ€ÐµÐ¶Ð¸Ð¼ Ð»Ð¸ÑˆÐµ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¸Ñ… Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÐµÐ¹ (Ð²Ð¸Ð¹Ñ‚Ð¸: /normal)\n"
    "/reset â€” Ð¾Ñ‡Ð¸ÑÑ‚Ð¸Ñ‚Ð¸ Ð¿Ð°Ð¼â€™ÑÑ‚ÑŒ Ð´Ñ–Ð°Ð»Ð¾Ð³Ñƒ\n"
    "/stats â€” Ñ‚Ð²Ð¾Ñ ÑÑ‚Ð°Ñ‚Ð¸ÑÑ‚Ð¸ÐºÐ°\n"
    "/id â€” Ñ‚Ð²Ñ–Ð¹ Telegram ID"
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
            "ÐžÐ±ÐµÑ€Ð¸ ÐºÐ¾Ð¼Ð°Ð½Ð´Ñƒ Ð°Ð±Ð¾ Ð¿Ñ€Ð¾ÑÑ‚Ð¾ Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ñ‚ÐµÐºÑÑ‚Ð¾Ð¼ Ñ‡Ð¸ Ñ„Ð¾Ñ‚Ð¾:",
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
    bot.reply_to(message, f"Ð¢Ð²Ñ–Ð¹ Telegram ID: {message.from_user.id}")


@bot.message_handler(commands=["subjects"])
def cmd_subjects(message):
    if not gate(message, cooldown=False):
        return
    cname = user_class(message.chat.id)
    keys = SUBJECT_KEYS
    if not keys:
        bot.send_message(
            message.chat.id,
            f"ðŸ“š Ð”Ð»Ñ {html.escape(cname)} Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ¸ Ð¿Ð¾ÐºÐ¸ Ð½Ðµ Ð¿Ñ–Ð´ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¾ â€” Ñ– Ñ†Ðµ Ð½Ðµ Ð·Ð°Ð²Ð°Ð¶Ð°Ñ”:\n"
            "â€¢ Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ <b>Ñ„Ð¾Ñ‚Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ</b> â€” Ñ€Ð¾Ð·Ð²'ÑÐ¶Ñƒ Ð¹ Ð¿Ð¾ÑÑÐ½ÑŽ\n"
            "â€¢ Ð°Ð±Ð¾ Ð½Ð°Ð¿Ð¸ÑˆÐ¸ Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ñ‚ÐµÐºÑÑ‚Ð¾Ð¼\n"
            "â€¢ Ð¿Ñ€Ð°Ñ†ÑŽÑŽÑ‚ÑŒ /hw, /cards, /quiz, /exam, /formulas\n\n"
            "Ð©Ð¾Ð± Ñ Ñ€Ð¾Ð·Ð²'ÑÐ·ÑƒÐ²Ð°Ð² Ñ‚Ð¾Ñ‡Ð½Ñ–ÑˆÐµ Ð·Ð° Ð¿Ñ€Ð¾Ð³Ñ€Ð°Ð¼Ð¾ÑŽ â€” Ð¿Ñ€Ð¾ÑÑ‚Ð¾ Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð¾Ñ‚Ð¾ ÑÑ‚Ð¾Ñ€Ñ–Ð½ÐºÐ¸ Ñ–Ð· Ð·Ð°Ð²Ð´Ð°Ð½Ð½ÑÐ¼.",
            parse_mode="HTML")
        return
    bot.send_message(message.chat.id, f"Ð”Ð¾ÑÑ‚ÑƒÐ¿Ð½Ñ– Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð¸:\n"
                                      + "\n".join(f"â€¢ {s}" for s in SUBJECTS))


@bot.message_handler(commands=["subject"])
def cmd_subject(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        cur = settings[uid]["subject"]
        bot.reply_to(message, f"ÐŸÐ¾Ñ‚Ð¾Ñ‡Ð½Ð¸Ð¹ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚: {cur or 'Ð½Ðµ Ð¾Ð±Ñ€Ð°Ð½Ð¾'}. Ð—Ð¼Ñ–Ð½Ð¸Ñ‚Ð¸: /subject Ñ„Ñ–Ð·Ð¸ÐºÐ°")
        return
    key = resolve_subject(args[1])
    if not key:
        bot.reply_to(message, "ÐÐµ Ð·Ð½Ð°ÑŽ Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð°. Ð¡Ð¿Ð¸ÑÐ¾Ðº: /subjects")
        return
    settings[uid]["subject"] = key
    bot.reply_to(message, f"ÐŸÑ€ÐµÐ´Ð¼ÐµÑ‚ Ð¾Ð±Ñ€Ð°Ð½Ð¾: {subject_label(key)}. Ð¢ÐµÐ¿ÐµÑ€ Ð¼Ð¾Ð¶Ð½Ð° Ð¿Ð¸ÑÐ°Ñ‚Ð¸ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð±ÐµÐ· Ð½Ð°Ð·Ð²Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð°.")


def _set_length(message, value, text):
    if gate(message, cooldown=False):
        settings[message.from_user.id]["length"] = value
        bot.reply_to(message, text)


@bot.message_handler(commands=["short"])
def cmd_short(message):
    _set_length(message, "short", "Ð ÐµÐ¶Ð¸Ð¼: ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÑ– Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–.")


@bot.message_handler(commands=["detail"])
def cmd_detail(message):
    _set_length(message, "detail", "Ð ÐµÐ¶Ð¸Ð¼: Ð´Ð¾ÐºÐ»Ð°Ð´Ð½Ñ– Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–.")


@bot.message_handler(commands=["normal"])
def cmd_normal(message):
    _set_length(message, "normal", "Ð ÐµÐ¶Ð¸Ð¼: Ð·Ð²Ð¸Ñ‡Ð°Ð¹Ð½Ñ– Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–.")


@bot.message_handler(commands=["quick"])
def cmd_quick(message):
    _set_length(message, "quick", "Ð ÐµÐ¶Ð¸Ð¼: Ð»Ð¸ÑˆÐµ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÑ– Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–. Ð’Ð¸Ð¹Ñ‚Ð¸: /normal")


@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    if gate(message, cooldown=False):
        uid = message.from_user.id
        history[uid].clear()
        DOCS.pop(uid, None)
        bot.reply_to(message, "ÐŸÐ°Ð¼â€™ÑÑ‚ÑŒ Ð´Ñ–Ð°Ð»Ð¾Ð³Ñƒ Ð¾Ñ‡Ð¸Ñ‰ÐµÐ½Ð¾.")


@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    if not gate(message, cooldown=False):
        return
    uid = message.from_user.id
    u = stats["users"].get(str(uid), {"total": 0})
    text = (
        f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¾ ÑÑŒÐ¾Ð³Ð¾Ð´Ð½Ñ–: {min(used_today(uid), DAILY_LIMIT)}/{DAILY_LIMIT}\n"
        f"Ð£ÑÑŒÐ¾Ð³Ð¾ Ð·Ð°Ð¿Ð¸Ñ‚Ñ–Ð²: {u['total']}"
    )
    if uid == ADMIN_ID:
        rows = sorted(stats["users"].items(), key=lambda kv: kv[1]["total"], reverse=True)[:15]
        text += "\n\nÐ£ÑÑ– ÐºÐ¾Ñ€Ð¸ÑÑ‚ÑƒÐ²Ð°Ñ‡Ñ–:\n" + "\n".join(
            f"{v['name']} ({k}): {v['total']} Ð²ÑÐµÐ³Ð¾, {v['days'].get(datetime.now(KYIV).date().isoformat(), 0)} ÑÐµÐ³Ð¾Ð´Ð½Ñ"
            for k, v in rows
        )
    bot.reply_to(message, text)
def streak_days(u):
    """Ð¡ÐºÐ¾Ð»ÑŒÐºÐ¾ Ð´Ð½ÐµÐ¹ Ð¿Ð¾Ð´Ñ€ÑÐ´ Ñ‡ÐµÐ»Ð¾Ð²ÐµÐº Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ð»ÑÑ Ð±Ð¾Ñ‚Ð¾Ð¼."""
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
        bot.reply_to(message, "ÐŸÐ¾ÐºÐ¸ Ð¿Ð¾Ñ€Ð¾Ð¶Ð½ÑŒÐ¾ â€” Ñ€Ð¾Ð·Ð²â€™ÑÐ¶Ð¸ Ð¿ÐµÑ€ÑˆÐµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ!")
        return
    medals = ["ðŸ¥‡", "ðŸ¥ˆ", "ðŸ¥‰"]
    lines = [
        f"{medals[i] if i < 3 else str(i + 1) + '.'} {html.escape(str(v['name']))} â€” {v['total']} ðŸ”¥{streak_days(v)}"
        for i, (k, v) in enumerate(rows)
    ]
    bot.reply_to(message, "ðŸ† <b>Ð¢Ð¾Ð¿ ÐºÐ»Ð°ÑÑƒ</b>\n\n" + "\n".join(lines), parse_mode="HTML")
# ===========================================================================
# ÐšÐ°Ñ€Ñ‚Ð¾Ñ‡ÐºÐ¸ Ñ Ð¸Ð½Ñ‚ÐµÑ€Ð²Ð°Ð»ÑŒÐ½Ñ‹Ð¼ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð¸ÐµÐ¼ (/cards)
# ===========================================================================
CARDS_PER_SET = 8
BOXES = [1, 2, 4, 7, 16]   # Ñ‡ÐµÑ€ÐµÐ· ÑÐºÐ¾Ð»ÑŒÐºÐ¾ Ð´Ð½ÐµÐ¹ ÑÐ½Ð¾Ð²Ð° Ð¿Ð¾ÐºÐ°Ð·Ð°Ñ‚ÑŒ ÐºÐ°Ñ€Ñ‚Ð¾Ñ‡ÐºÑƒ

CARDS_MEM = {}             # Ñ…Ñ€Ð°Ð½Ð¸Ð»Ð¸Ñ‰Ðµ, ÐµÑÐ»Ð¸ Redis Ð½Ðµ Ð¿Ð¾Ð´ÐºÐ»ÑŽÑ‡Ñ‘Ð½


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

    # /cards Ð±ÐµÐ· Ð°Ñ€Ð³ÑƒÐ¼ÐµÐ½Ñ‚Ð¾Ð² - ÑÐµÑÑÐ¸Ñ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð¸Ñ
    if len(parts) < 2:
        all_cards = cards_load(uid)
        order = [i for i, c in enumerate(all_cards) if c.get("due", today) <= today]
        if not order:
            extra = f" Ð¡Ð»ÐµÐ´ÑƒÑŽÑ‰ÐµÐµ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð¸Ðµ ÐºÐ°Ñ€Ñ‚Ð¾Ñ‡ÐµÐº: {min((c['due'] for c in all_cards), default='')}" if all_cards else ""
            bot.reply_to(message, "ÐŸÐ¾ÐºÐ¸ Ð½ÐµÐ¼Ð°Ñ” Ñ‡Ð¾Ð³Ð¾ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÑŽÐ²Ð°Ñ‚Ð¸ ðŸŽ‰" + extra +
                                  "\nÐ¡Ñ‚Ð²Ð¾Ñ€Ð¸Ñ‚Ð¸ ÐºÐ°Ñ€Ñ‚ÐºÐ¸: /cards Ñ„Ñ–Ð·Ð¸ÐºÐ° <Ñ‚ÐµÐ¼Ð°>")
            return
        CARD_SESSION[uid] = {"cards": all_cards, "order": order, "pos": 0,
                             "chat": message.chat.id}
        show_card(uid)
        return

    subject = resolve_subject(parts[1])
    if not subject:
        bot.reply_to(message, "ÐÐµ Ð·Ð½Ð°ÑŽ Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚Ð°. Ð¡Ð¿Ð¸ÑÐ¾Ðº: /subjects")
        return
    topic = parts[2].strip() if len(parts) > 2 else ""
    if not topic:
        bot.reply_to(message, "Ð’ÐºÐ°Ð¶Ð¸ Ñ‚ÐµÐ¼Ñƒ: /cards Ñ„Ñ–Ð·Ð¸ÐºÐ° Ñ‚Ð¸ÑÐº")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Ð¤Ð°Ð¹Ð» Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Â«{SUBJECTS[subject]}Â» Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾.")
        return

    status = bot.reply_to(message, "Ð¡ÐºÐ»Ð°Ð´Ð°ÑŽ ÐºÐ°Ñ€Ñ‚ÐºÐ¸â€¦ â±ï¸")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        context, _ = find_context(path, topic)
        prompt = (
            "ÐÐ° Ð¾ÑÐ½Ð¾Ð²Ñ– Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ñ–Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° ÑÐºÐ»Ð°Ð´Ð¸ 8 ÐºÐ°Ñ€Ñ‚Ð¾Ðº Ð´Ð»Ñ Ð·Ð°Ð¿Ð°Ð¼â€™ÑÑ‚Ð¾Ð²ÑƒÐ²Ð°Ð½Ð½Ñ Ð·Ð° Ñ‚ÐµÐ¼Ð¾ÑŽ "
            f"Â«{topic}Â». ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON-Ð¼Ð°ÑÐ¸Ð² Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð²Ð¸Ð³Ð»ÑÐ´Ñƒ "
            '[{"q": "Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð°Ð±Ð¾ Ñ‚ÐµÑ€Ð¼Ñ–Ð½", "a": "ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ° Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð°Ð±Ð¾ Ð²Ð¸Ð·Ð½Ð°Ñ‡ÐµÐ½Ð½Ñ"}]. '
            "Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÑ–, Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ– Ð´Ð¾ 25 ÑÐ»Ñ–Ð², ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾ÑŽ, Ð±ÐµÐ· HTML.\n\n"
            f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸:\n{context or '(Ð½ÐµÐ¼Ð°Ñ”)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.4)
        if not raw:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
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
            bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ ÑÐºÐ»Ð°ÑÑ‚Ð¸ ÐºÐ°Ñ€Ñ‚ÐºÐ¸, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
            return
        cards_save(uid, all_cards)
        register_use(uid, message.from_user.first_name)
        bot.edit_message_text(
            f"âœ… Ð”Ð¾Ð±Ð°Ð²Ð¸Ð» {added} ÐºÐ°Ñ€Ñ‚Ð¾Ñ‡ÐµÐº Ð¿Ð¾ Ñ‚ÐµÐ¼Ðµ Â«{topic}Â».\n"
            "ÐŸÐ¾Ð²Ñ‚Ð¾Ñ€ÑŽÐ²Ð°Ñ‚Ð¸: /cards â€” Ð½Ð¾Ð²Ñ– ÐºÐ°Ñ€Ñ‚ÐºÐ¸ Ð·â€™ÑÐ²Ð»ÑÑ‚Ð¸Ð¼ÑƒÑ‚ÑŒÑÑ Ð·Ð° Ð³Ñ€Ð°Ñ„Ñ–ÐºÐ¾Ð¼ Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€ÐµÐ½Ð½Ñ.",
            message.chat.id, status.message_id)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° ÐºÐ°Ñ€Ñ‚Ð¾Ñ‡ÐµÐº")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def show_card(uid):
    s = CARD_SESSION.get(uid)
    if not s:
        return
    card = s["cards"][s["order"][s["pos"]]]
    text = (f"ðŸƒ ÐšÐ°Ñ€Ñ‚Ð¾Ñ‡ÐºÐ° {s['pos'] + 1}/{len(s['order'])}\n\n"
            f"<b>{html.escape(card['q'])}</b>")
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("ÐŸÐ¾ÐºÐ°Ð·Ð°Ñ‚Ð¸ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ", callback_data="card:show"))
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
    kb.row(InlineKeyboardButton("âœ… Ð—Ð½Ð°ÑŽ", callback_data="card:ok"),
           InlineKeyboardButton("âŒ ÐÐµ Ð·Ð½Ð°ÑŽ", callback_data="card:no"))
    try:
        bot.edit_message_text(
            f"ðŸƒ ÐšÐ°Ñ€Ñ‚Ð¾Ñ‡ÐºÐ° {s['pos'] + 1}/{len(s['order'])}\n\n"
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
    bot.answer_callback_query(call.id, "ÐœÐ¾Ð»Ð¾Ð´ÐµÑ†ÑŒ! ðŸ’ª" if good else "Ð—â€™ÑÐ²Ð¸Ñ‚ÑŒÑÑ Ð·Ð½Ð¾Ð²Ñƒ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
    if s["pos"] < len(s["order"]):
        show_card(uid)
    else:
        CARD_SESSION.pop(uid, None)
        bot.send_message(call.message.chat.id,
                         "ðŸŽ‰ Ð¡ÐµÑÑ–ÑŽ Ð·Ð°Ð²ÐµÑ€ÑˆÐµÐ½Ð¾! ÐÐ°Ð¶Ð¼Ð¸ /cards Ð·Ð°Ð²Ñ‚Ñ€Ð° â€” Ð¿Ð¾ÐºÐ°Ð¶Ñƒ ÑÐ»ÐµÐ´ÑƒÑŽÑ‰Ð¸Ðµ.")


# ===========================================================================
# /proofread - Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð¾Ñ€Ñ„Ð¾Ð³Ñ€Ð°Ñ„Ð¸Ð¸
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
        bot.reply_to(message, "ÐŸÐ¸ÑˆÐ¸ Ñ‚Ð°Ðº: /proofread <Ñ‚ÐµÐºÑÑ‚>\n"
                              "ÐÐ±Ð¾ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð½Ð° Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¾ÑŽ /proofread")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    status = bot.reply_to(message, "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÑÑŽâ€¦ â±ï¸")
    bot.send_chat_action(message.chat.id, "typing")
    prompt = (
        "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ Ñ‚ÐµÐºÑÑ‚ Ð½Ð° Ð¾Ñ€Ñ„Ð¾Ð³Ñ€Ð°Ñ„Ñ–Ñ‡Ð½Ñ–, Ð¿ÑƒÐ½ÐºÑ‚ÑƒÐ°Ñ†Ñ–Ð¹Ð½Ñ– Ñ‚Ð° Ð³Ñ€Ð°Ð¼Ð°Ñ‚Ð¸Ñ‡Ð½Ñ– Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ¸. "
        "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾ÑŽ. ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸:\n"
        "1) Ð¡Ð¿Ð¸ÑÐ¾Ðº Ð¿Ð¾Ð¼Ð¸Ð»Ð¾Ðº Ð·Ð° Ð¿ÑƒÐ½ÐºÑ‚Ð°Ð¼Ð¸ Ñƒ Ñ„Ð¾Ñ€Ð¼Ð°Ñ‚Ñ– Â«Ð½ÐµÐ¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾ â†’ Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾ (Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð¾)Â».\n"
        "2) Ð ÑÐ´Ð¾Ðº Â«Ð’Ð˜ÐŸÐ ÐÐ’Ð›Ð•ÐÐ˜Ð™ Ð¢Ð•ÐšÐ¡Ð¢:Â» Ñ– Ð²ÐµÑÑŒ Ñ‚ÐµÐºÑÑ‚ Ð±ÐµÐ· Ð¿Ð¾Ð¼Ð¸Ð»Ð¾Ðº.\n"
        "Ð¯ÐºÑ‰Ð¾ Ð¿Ð¾Ð¼Ð¸Ð»Ð¾Ðº Ð½ÐµÐ¼Ð°Ñ” â€” Ñ‚Ð°Ðº Ñ– ÑÐºÐ°Ð¶Ð¸. ÐÐµ Ð²Ð¸ÐºÐ¾Ñ€Ð¸ÑÑ‚Ð¾Ð²ÑƒÐ¹ HTML.\n\n"
        f"Ð¢ÐµÐºÑÑ‚:\n{task[:3000]}"
    )
    answer = call_gemini([prompt], system_instruction=SYSTEM_BASE)
    if not answer:
        bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                              message.chat.id, status.message_id)
        return
    register_use(uid, message.from_user.first_name)
    send_answer(message.chat.id, status, answer, uid=uid, task=task[:500])

# ===========================================================================
# /remind - Ð½Ð°Ð¿Ð¾Ð¼Ð¸Ð½Ð°Ð½Ð¸Ñ Ð¾ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñ‹Ñ…
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
    if text.lower() in ("ÑÐ¿Ð¸ÑÐ¾Ðº", "list"):
        mine = [r for r in reminds_load() if r["chat"] == message.chat.id]
        if not mine:
            bot.reply_to(message, "ÐÐ°Ð³Ð°Ð´ÑƒÐ²Ð°Ð½ÑŒ Ð½ÐµÐ¼Ð°Ñ”. Ð”Ð¾Ð´Ð°Ñ‚Ð¸: /remind 20.05 ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° Ð· Ñ„Ñ–Ð·Ð¸ÐºÐ¸")
            return
        lines = [f"â€¢ {r['due']} â€” {r['text']}" for r in sorted(mine, key=lambda x: x["due"])]
        bot.reply_to(message, "â° ÐÐ°Ð³Ð°Ð´ÑƒÐ²Ð°Ð½Ð½Ñ:\n" + "\n".join(lines))
        return
    m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?", text)
    if not m:
        bot.reply_to(message, "Ð’ÐºÐ°Ð¶Ð¸ Ð´Ð°Ñ‚Ñƒ Ð¹ Ñ‰Ð¾ Ð½Ð°Ð³Ð°Ð´Ð°Ñ‚Ð¸:\n/remind 20.05 ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° Ð· Ñ„Ñ–Ð·Ð¸ÐºÐ¸\n"
                              "ÐŸÐ¾ÐºÐ°Ð·Ð°Ñ‚Ð¸ ÑÐ¿Ð¸ÑÐ¾Ðº: /remind ÑÐ¿Ð¸ÑÐ¾Ðº")
        return
    d_, mo_, y_ = m.groups()
    year = int(y_) if y_ else _today().year
    try:
        due = datetime(year, int(mo_), int(d_)).date()
    except ValueError:
        bot.reply_to(message, "Ð¢Ð°ÐºÐ¾Ñ— Ð´Ð°Ñ‚Ð¸ Ð½Ðµ Ð±ÑƒÐ²Ð°Ñ”. ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´: /remind 20.05 ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð°")
        return
    if not y_ and due < _today():
        due = datetime(year + 1, int(mo_), int(d_)).date()
    what = (text[:m.start()] + " " + text[m.end():]).strip() or "ÐÐ°Ð¿Ð¾Ð¼Ð¸Ð½Ð°Ð½Ð¸Ðµ"
    reminds = reminds_load()
    reminds.append({"chat": message.chat.id, "uid": message.from_user.id,
                    "text": what[:300], "due": due.isoformat(), "warned": False})
    reminds_save(reminds)
    bot.reply_to(message, f"ðŸ‘Œ Ð—Ð°Ð¿Ð¸ÑÐ°Ð²: {what}\nÐÐ°Ð³Ð°Ð´Ð°ÑŽ {due.strftime('%d.%m.%Y')} Ñ– Ð·Ð° 2 Ð´Ð½Ñ– Ð´Ð¾.")


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
                bot.send_message(r["chat"], f"âš ï¸ Ð§ÐµÑ€ÐµÐ· 2 Ð´Ð½Ñ–: {r['text']}")
            except Exception:
                pass
            r["warned"] = True
            changed = True
            keep.append(r)
        elif left == 0:
            try:
                bot.send_message(r["chat"], f"â€¼ï¸ Ð¡ÑŒÐ¾Ð³Ð¾Ð´Ð½Ñ–: {r['text']}")
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
# /pomodoro - Ñ‚Ð°Ð¹Ð¼ÐµÑ€ ÑƒÑ‡Ñ‘Ð±Ñ‹
# ===========================================================================
@bot.message_handler(commands=["pomodoro"])
def cmd_pomodoro(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split()
    if len(args) > 1 and args[1].lower() in ("stop", "ÑÑ‚Ð¾Ð¿"):
        s = POMODORO.pop(uid, None)
        if s:
            s["t1"].cancel()
            if s.get("t2"):
                s["t2"].cancel()
            bot.reply_to(message, "ðŸ… Ð¢Ð°Ð¹Ð¼ÐµÑ€ Ð·ÑƒÐ¿Ð¸Ð½ÐµÐ½Ð¾.")
        else:
            bot.reply_to(message, "Ð¢Ð°Ð¹Ð¼ÐµÑ€ Ð½Ðµ Ð±ÑƒÐ² Ð·Ð°Ð¿ÑƒÑ‰ÐµÐ½Ð¸Ð¹.")
        return
    try:
        mins = int(args[1]) if len(args) > 1 else 25
    except ValueError:
        mins = 25
    if not 5 <= mins <= 90:
        bot.reply_to(message, "Ð’ÑÑ‚Ð°Ð½Ð¾Ð²Ð¸ Ð²Ñ–Ð´ 5 Ð´Ð¾ 90 Ñ…Ð²Ð¸Ð»Ð¸Ð½: /pomodoro 25")
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
            bot.send_message(chat, f"â° {mins} Ñ…Ð²Ð¸Ð»Ð¸Ð½ Ð¼Ð¸Ð½ÑƒÐ»Ð¾! ÐŸÐµÑ€ÐµÑ€Ð²Ð° {brk} Ð¼Ð¸Ð½ â˜•ï¸")
        except Exception:
            pass
        t2 = threading.Timer(brk * 60, back_to_work)
        t2.daemon = True
        t2.start()
        POMODORO[uid]["t2"] = t2

    def back_to_work():
        POMODORO.pop(uid, None)
        try:
            bot.send_message(chat, "ðŸ… ÐŸÐµÑ€ÐµÑ€Ð²Ñƒ Ð·Ð°Ð²ÐµÑ€ÑˆÐµÐ½Ð¾ â€” Ð´Ð¾ Ð½Ð°Ð²Ñ‡Ð°Ð½Ð½Ñ!")
        except Exception:
            pass

    t1 = threading.Timer(mins * 60, on_break)
    t1.daemon = True
    t1.start()
    POMODORO[uid] = {"t1": t1, "t2": None}
    bot.reply_to(message, f"ðŸ… ÐŸÐ¾Ñ‡Ð¸Ð½Ð°Ñ”Ð¼Ð¾: {mins} Ñ…Ð² Ð½Ð°Ð²Ñ‡Ð°Ð½Ð½Ñ â†’ {brk} Ñ…Ð² Ð¿ÐµÑ€ÐµÑ€Ð²Ð¸.\nÐ—ÑƒÐ¿Ð¸Ð½Ð¸Ñ‚Ð¸: /pomodoro stop")


# ===========================================================================
# Ð•Ð¶ÐµÐ´Ð½ÐµÐ²Ð½Ñ‹Ð¹ Ð¾Ñ‚Ñ‡Ñ‘Ñ‚ Ð°Ð´Ð¼Ð¸Ð½Ñƒ + Ñ„Ð¾Ð½Ð¾Ð²Ñ‹Ð¹ Ñ†Ð¸ÐºÐ»
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
    redis_ok = "Ð½Ðµ Ð¿Ñ–Ð´ÐºÐ»ÑŽÑ‡ÐµÐ½Ð¾"
    if kv_enabled():
        try:
            _kv(["PING"])
            redis_ok = "Ñ€Ð°Ð±Ð¾Ñ‚Ð°ÐµÑ‚"
        except Exception:
            redis_ok = "ÐÐ• ÐžÐ¢Ð’Ð•Ð§ÐÐ•Ð¢"
    try:
        bot.send_message(ADMIN_ID,
                         f"ðŸ“Š Ð—Ð²Ñ–Ñ‚ Ð·Ð° {today}\n"
                         f"Ð—Ð°Ð¿Ð¸Ñ‚Ñ–Ð²: {reqs} (Ð°ÐºÑ‚Ð¸Ð²Ð½Ð¸Ñ… ÑƒÑ‡Ð½Ñ–Ð²: {active})\n"
                         f"ðŸ‘Ž Ð¿Ð¾Ð³Ð°Ð½Ð¸Ñ… Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÐµÐ¹: {FB_LOG.count(today)}\n"
                         f"Redis: {redis_ok}\n"
                         f"Ð‘Ð¾Ñ‚ Ð¿Ñ€Ð°Ñ†ÑŽÑ”: {(time.time() - START_TIME) / 3600:.1f} Ñ‡")
    except Exception:
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð¸Ñ‚ÑŒ Ð¾Ñ‚Ñ‡Ñ‘Ñ‚ Ð°Ð´Ð¼Ð¸Ð½Ñƒ")


def daily_loop():
    while True:
        try:
            _check_reminds()
            _daily_admin_report()
            _question_of_the_day()
            _flush_stats()
        except Exception:
            log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° daily_loop")
        time.sleep(300)
# ===========================================================================
# ÐŸÑ€Ð¸Ñ‘Ð¼ PDF-Ñ„Ð°Ð¹Ð»Ð¾Ð² Ð¾Ñ‚ ÑƒÑ‡Ð¸Ñ‚ÐµÐ»Ñ
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
        bot.reply_to(message, "ÐŸÐ¾ÐºÐ¸ Ñ‰Ð¾ Ð²Ð¼Ñ–ÑŽ Ð¿Ñ€Ð°Ñ†ÑŽÐ²Ð°Ñ‚Ð¸ Ð»Ð¸ÑˆÐµ Ð· PDF. ÐÐ°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð°Ð¹Ð» .pdf")
        return
    if (doc.file_size or 0) > 20 * 1024 * 1024:
        bot.reply_to(message, "Ð¤Ð°Ð¹Ð» Ð±Ñ–Ð»ÑŒÑˆÐ¸Ð¹ Ð·Ð° 20 ÐœÐ‘ â€” Telegram Ð½Ðµ Ð´Ð¾Ð·Ð²Ð¾Ð»Ð¸Ñ‚ÑŒ Ð¹Ð¾Ð³Ð¾ Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ð¸Ñ‚Ð¸.")
        return
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    status = bot.reply_to(message, "Ð§Ð¸Ñ‚Ð°ÑŽ Ñ„Ð°Ð¹Ð»â€¦ â±ï¸")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        info = bot.get_file(doc.file_id)
        data = bot.download_file(info.file_path)
        reader = PdfReader(BytesIO(data))
        text = "\n".join((p.extract_text() or "").strip() for p in reader.pages).strip()
    except Exception:
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ Ð¿Ñ€Ð¾Ñ‡Ð¸Ñ‚Ð°Ñ‚ÑŒ PDF")
        bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð¿Ñ€Ð¾Ñ‡Ð¸Ñ‚Ð°Ñ‚Ð¸ PDF. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð½Ð°Ð´Ñ–ÑÐ»Ð°Ñ‚Ð¸ Ñ„Ð¾Ñ‚Ð¾ ÑÑ‚Ð¾Ñ€Ñ–Ð½Ð¾Ðº.",
                              message.chat.id, status.message_id)
        return
    if not text:
        bot.edit_message_text("Ð£ PDF Ð½ÐµÐ¼Ð°Ñ” Ñ‚ÐµÐºÑÑ‚Ñƒ (ÑÑ…Ð¾Ð¶Ðµ, Ñ†Ðµ ÑÐºÐ°Ð½). ÐÐ°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð¾Ñ‚Ð¾ ÑÑ‚Ð¾Ñ€Ñ–Ð½Ð¾Ðº.",
                              message.chat.id, status.message_id)
        return
    question = (message.caption or "").strip()
    if not question:
        head = html.escape(text[:600])
        DOCS.set(uid, {"text": text[:MAX_PDF_CHARS], "ts": time.time()})
        bot.edit_message_text(
            f"ðŸ“„ Ð¤Ð°Ð¹Ð» Ð¿Ñ€Ð¾Ñ‡Ð¸Ñ‚Ð°Ð½Ð¾. Ð—Ð°Ð´Ð°Ð²Ð°Ð¹ Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð·Ð° Ð½Ð¸Ð¼ (Ð·Ð±ÐµÑ€Ñ–Ð³Ð°Ñ”Ñ‚ÑŒÑÑ 30 Ñ…Ð²).\n\n"
            f"<b>ÐŸÐ¾Ñ‡Ð°Ñ‚Ð¾Ðº Ð´Ð¾ÐºÑƒÐ¼ÐµÐ½Ñ‚Ð°:</b>\n{head}",
            message.chat.id, status.message_id, parse_mode="HTML")
        return
    try:
        answer = call_gemini(
            [f"Ð”Ð¾ÐºÑƒÐ¼ÐµÐ½Ñ‚ Ð²Ñ–Ð´ ÑƒÑ‡Ð¸Ñ‚ÐµÐ»Ñ (Ð¼Ð¾Ð¶Ðµ Ð±ÑƒÑ‚Ð¸ Ð¾Ð±Ñ€Ñ–Ð·Ð°Ð½Ð¸Ð¹):\n{text[:MAX_PDF_CHARS]}\n\n"
             f"Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð°Ð±Ð¾ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ ÑƒÑ‡Ð½Ñ Ð·Ð° Ð´Ð¾ÐºÑƒÐ¼ÐµÐ½Ñ‚Ð¾Ð¼: {question}\n\n"
             "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð·Ð° Ð´Ð¾ÐºÑƒÐ¼ÐµÐ½Ñ‚Ð¾Ð¼. ÐžÑ„Ð¾Ñ€Ð¼Ð¸ ÑÑ‚Ñ€ÑƒÐºÑ‚ÑƒÑ€Ð¾Ð²Ð°Ð½Ð¾, HTML-Ñ‚ÐµÐ³Ð¸ Telegram: <b>, <i>, <code>."],
            system_instruction=SYSTEM_BASE)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° Ð·Ð°Ð¿Ñ€Ð¾ÑÐ° Ð¿Ð¾ PDF")
        answer = None
    if not answer:
        bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                              message.chat.id, status.message_id)
        return
    register_use(uid, message.from_user.first_name)
    send_answer(message.chat.id, status, answer, uid=uid, task=question[:500])

# ===========================================================================
# Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð´Ð½Ñ (ÑˆÐ»Ñ‘Ð¼ Ð² 18:00 Ð²ÑÐµÐ¼ Ð¿Ð¾Ð´Ð¿Ð¸ÑÑ‡Ð¸ÐºÐ°Ð¼ Ñ€Ð°ÑÑÑ‹Ð»ÐºÐ¸)
# ===========================================================================
QDAY_HOUR = 18
_qday_last = None


@bot.callback_query_handler(func=lambda call: call.data.startswith("qday:"))
def on_qday_answer(call):
    token = call.data.split(":", 1)[1]
    answer = QDAY_CACHE.get(token)
    bot.answer_callback_query(call.id)
    if answer:
        bot.send_message(call.message.chat.id, f"ðŸ’¡ {html.escape(answer)}")
    else:
        bot.send_message(call.message.chat.id, "Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð·Ð°ÑÑ‚Ð°Ñ€Ñ–Ð»Ð° (Ð±Ð¾Ñ‚ Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐºÐ°Ð²ÑÑ).")


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
        "ÐŸÑ€Ð¸Ð´ÑƒÐ¼Ð°Ð¹ Ð·Ð° Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¾Ð¼ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° ÐžÐ”ÐÐ• Ñ†Ñ–ÐºÐ°Ð²Ðµ Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð´Ð»Ñ ÑƒÑ‡Ð½Ñ 8 ÐºÐ»Ð°ÑÑƒ "
        "Ñ– ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÑƒ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð½Ð° Ð½ÑŒÐ¾Ð³Ð¾ (Ð´Ð¾ 40 ÑÐ»Ñ–Ð²). ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð²Ð¸Ð³Ð»ÑÐ´Ñƒ "
        '{"q": "Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ", "a": "Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ"}.\n\n'
        f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚ Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° ({subj}):\n{page[:3000]}",
        json_mode=True, temperature=0.8)
    if not raw:
        log.warning("Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð´Ð½Ñ: Ð˜Ð˜ Ð½Ðµ Ð¾Ñ‚Ð²ÐµÑ‚Ð¸Ð»")
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
    kb.add(InlineKeyboardButton("ðŸ’¡ ÐŸÐ¾ÐºÐ°Ð·Ð°Ñ‚Ð¸ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ", callback_data=f"qday:{token}"))
    text = f"â“ <b>Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð´Ð½Ñ</b> ({html.escape(subj)})\n\n{html.escape(q)}"
    for chat_id in broadcast_targets():
        try:
            bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            log.exception("Ð—Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð´Ð½Ñ: Ð½Ðµ Ð¾Ñ‚Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð¾ÑÑŒ Ð² %s", chat_id)
# ===========================================================================
# /exam - Ð¿Ñ€Ð¾Ð±Ð½Ð° ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð°
def _alive(d, uid, ttl=1800):
    """ÐŸÑ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð¸ Ð¾Ñ‡Ð¸ÑÑ‚ÐºÐ° ÑƒÑÑ‚Ð°Ñ€ÐµÐ²ÑˆÐ¸Ñ… Ð·Ð°Ð¿Ð¸ÑÐµÐ¹ ÑÐ¾ÑÑ‚Ð¾ÑÐ½Ð¸Ñ Ñ TTL."""
    s = d.get(uid)
    if s and time.time() - s.get("ts", 0) > ttl:
        d.pop(uid, None)
        return False
    return bool(s)


# ===========================================================================
EXAM_QCOUNT = 5
EXAM = {}   # uid -> {"items", "answers", "pos", "chat", "subject", "ts"}
EXAM_TTL = 1800   # 30 Ñ…Ð²Ð¸Ð»Ð¸Ð½: ÑÐºÑ‰Ð¾ ÑƒÑ‡ÐµÐ½ÑŒ Ð½Ðµ Ð¿Ñ€Ð¾Ð´Ð¾Ð²Ð¶ÑƒÑ” â€” ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° Ð²Ð¸Ð´Ð°Ð»ÑÑ”Ñ‚ÑŒÑÑ


@bot.message_handler(commands=["exam"])
def cmd_exam(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=2)
    if len(args) > 1 and args[1].lower() in ("ÑÑ‚Ð¾Ð¿", "stop"):
        if EXAM.pop(uid, None):
            bot.reply_to(message, "ÐšÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñƒ Ð¿ÐµÑ€ÐµÑ€Ð²Ð°Ð½Ð¾. ÐŸÐ¾Ñ‡Ð°Ñ‚Ð¸ Ð·Ð½Ð¾Ð²Ñƒ: /exam Ñ„Ñ–Ð·Ð¸ÐºÐ°")
        else:
            bot.reply_to(message, "ÐšÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° Ñ‰Ðµ Ð½Ðµ Ñ€Ð¾Ð·Ð¿Ð¾Ñ‡Ð¸Ð½Ð°Ð»Ð°ÑÑ. ÐŸÐ¾Ñ‡Ð°Ñ‚Ð¸: /exam Ñ„Ñ–Ð·Ð¸ÐºÐ°")
        return
    if _alive(FORMULA, uid):
        bot.reply_to(message, "Ð¡Ð¿Ð¾Ñ‡Ð°Ñ‚ÐºÑƒ Ð·Ð°Ð²ÐµÑ€ÑˆÐ¸ Ñ‚Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ„Ð¾Ñ€Ð¼ÑƒÐ» (/formulas ÑÑ‚Ð¾Ð¿).")
        return
    if _alive(EXAM, uid):
        bot.reply_to(message, "ÐšÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð° Ð²Ð¶Ðµ Ñ‚Ñ€Ð¸Ð²Ð°Ñ” â€” Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ Ð½Ð° Ð¿Ð¾Ñ‚Ð¾Ñ‡Ð½Ðµ Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ Ð°Ð±Ð¾ /exam ÑÑ‚Ð¾Ð¿.")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Ð’ÐºÐ°Ð¶Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚: /exam Ñ„Ñ–Ð·Ð¸ÐºÐ° [Ñ‚ÐµÐ¼Ð°]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Ð¤Ð°Ð¹Ð» Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Â«{SUBJECTS[subject]}Â» Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾.")
        return
    status = bot.reply_to(message, "Ð¡ÐºÐ»Ð°Ð´Ð°ÑŽ Ð²Ð°Ñ€Ñ–Ð°Ð½Ñ‚ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð¾Ñ—â€¦ â±ï¸")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"ÐÐ° Ð¾ÑÐ½Ð¾Ð²Ñ– Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ñ–Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° ÑÐºÐ»Ð°Ð´Ð¸ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñƒ Ð· {EXAM_QCOUNT} Ð·Ð°Ð²Ð´Ð°Ð½ÑŒ "
            "Ñ€Ñ–Ð²Ð½Ñ 8 ÐºÐ»Ð°ÑÑƒ Ð· ÐšÐžÐ ÐžÐ¢ÐšÐ˜ÐœÐ˜ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑÐ¼Ð¸ (Ñ‡Ð¸ÑÐ»Ð¾, ÑÐ»Ð¾Ð²Ð¾, Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð° Ð°Ð±Ð¾ 1 Ñ€ÐµÑ‡ÐµÐ½Ð½Ñ). "
            'ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON-Ð¼Ð°ÑÐ¸Ð² Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð²Ð¸Ð³Ð»ÑÐ´Ñƒ [{"q": "Ñ‚ÐµÐºÑÑ‚ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ", "a": "ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ° ÐµÑ‚Ð°Ð»Ð¾Ð½Ð½Ð° Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ"}]. '
            "Ð—Ð°Ð²Ð´Ð°Ð½Ð½Ñ ÑƒÐºÑ€Ð°Ñ—Ð½ÑÑŒÐºÐ¾ÑŽ, Ñ€Ñ–Ð·Ð½Ð¾Ñ— ÑÐºÐ»Ð°Ð´Ð½Ð¾ÑÑ‚Ñ–, Ð±ÐµÐ· HTML.\n\n"
            f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸:\n{context or '(Ð½ÐµÐ¼Ð°Ñ”)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("q") and it.get("a")][:EXAM_QCOUNT]
        if len(items) < 3:
            bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ ÑÐºÐ»Ð°ÑÑ‚Ð¸ Ð²Ð°Ñ€Ñ–Ð°Ð½Ñ‚, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
            return
        EXAM[uid] = {"items": items, "answers": [], "pos": 0, "ts": time.time(),
                     "chat": message.chat.id, "subject": subject}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _exam_ask(uid)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° /exam")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _exam_ask(uid):
    s = EXAM.get(uid)
    if not s:
        return
    q = s["items"][s["pos"]]["q"]
    text = (f"ðŸ“ <b>ÐšÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð°</b> ({html.escape(subject_label(s['subject']))}) Â· "
            f"Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ {s['pos'] + 1} Ñ–Ð· {len(s['items'])}\n\n"
            f"{html.escape(str(q))}\n\n"
            "<i>ÐÐ°Ð¿Ð¸ÑˆÐ¸ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð½Ð°ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¼ Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½ÑÐ¼. ÐŸÐµÑ€ÐµÑ€Ð²Ð°Ñ‚Ð¸: /exam ÑÑ‚Ð¾Ð¿</i>")
    bot.send_message(s["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: _alive(EXAM, m.from_user.id)
                      and not m.text.startswith("/")
                      and m.chat.id == EXAM[m.from_user.id]["chat"],
                      content_types=["text"])
def on_exam_answer(message):
    uid = message.from_user.id
    s = EXAM[uid]
    s["ts"] = time.time()   # Ð¶Ð¸Ð²Ð¸Ð¼ TTL Ð¿Ñ€Ð¸ ÐºÐ¾Ð¶Ð½Ñ–Ð¹ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–
    s["answers"].append(message.text.strip()[:500])
    s["pos"] += 1
    if s["pos"] < len(s["items"]):
        _exam_ask(uid)
        return
    # Ð²ÑÐµ Ð¾Ñ‚Ð²ÐµÑ‚Ñ‹ ÑÐ¾Ð±Ñ€Ð°Ð½Ñ‹ - Ð¾Ð´Ð½Ð° Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ° Ð½Ð° Ð²ÑÐµ Ð·Ð°Ð´Ð°Ð½Ð¸Ñ
    bot.send_chat_action(s["chat"], "typing")
    status = bot.send_message(s["chat"], "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÑÑŽ Ñ€Ð¾Ð±Ð¾Ñ‚Ñƒâ€¦ â±ï¸")
    qa = "\n\n".join(
        f"{i + 1}. Ð—Ð°Ð²Ð´Ð°Ð½Ð½Ñ: {it['q']}\nÐ•Ñ‚Ð°Ð»Ð¾Ð½: {it['a']}\nÐ’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ ÑƒÑ‡Ð½Ñ: {a or '(Ð½ÐµÐ¼Ð°Ñ” Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–)'}"
        for i, (it, a) in enumerate(zip(s["items"], s["answers"])))
    prompt = (
        "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñƒ ÑƒÑ‡Ð½Ñ. Ð—Ð° ÐºÐ¾Ð¶Ð½Ðµ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ Ð¿Ð¾ÑÑ‚Ð°Ð² Ð±Ð°Ð» Ð²Ñ–Ð´ 0 Ð´Ð¾ 2 "
        "(0 â€” Ð½ÐµÐ¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾, 1 â€” Ñ‡Ð°ÑÑ‚ÐºÐ¾Ð²Ð¾ Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾, 2 â€” Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾; Ð·Ð°Ñ€Ð°Ñ…Ð¾Ð²ÑƒÐ¹ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–, Ñ‰Ð¾ Ð·Ð±Ñ–Ð³Ð°ÑŽÑ‚ÑŒÑÑ "
        "Ð·Ð° Ð·Ð¼Ñ–ÑÑ‚Ð¾Ð¼, Ð½Ð°Ð²Ñ–Ñ‚ÑŒ ÑÐºÑ‰Ð¾ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»ÑŽÐ²Ð°Ð½Ð½Ñ Ñ–Ð½ÑˆÐµ). ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON: "
        '[{"n": 1, "score": 2, "comment": "ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¸Ð¹ ÐºÐ¾Ð¼ÐµÐ½Ñ‚Ð°Ñ€ Ð´Ð¾ 100 ÑÐ¸Ð¼Ð²Ð¾Ð»Ð¾Ð²"}].\n\n' + qa)
    raw = call_gemini(prompt, json_mode=True, temperature=0.1)
    EXAM.pop(uid, None)
    if not raw:
        bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹ â€” Ñ€Ð¾Ð±Ð¾Ñ‚Ñƒ Ð½Ðµ Ð¿ÐµÑ€ÐµÐ²Ñ–Ñ€ÐµÐ½Ð¾. "
                              "Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ Ñ‰Ðµ Ñ€Ð°Ð·: /exam",
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
        lines.append(f"{'âœ…' if sc == 2 else 'ðŸŸ¡' if sc == 1 else 'âŒ'} "
                     f"Ð—Ð°Ð²Ð´Ð°Ð½Ð½Ñ {g.get('n', len(lines) + 1)}: {sc}/2 â€” "
                     f"{html.escape(str(g.get('comment', '')))}")
    while len(lines) < len(s["items"]):
        lines.append(f"âŒ Ð—Ð°Ð²Ð´Ð°Ð½Ð½Ñ {len(lines) + 1}: 0/2 â€” Ð½Ðµ Ð¿ÐµÑ€ÐµÐ²Ñ–Ñ€ÐµÐ½Ð¾")
    mx = 2 * len(s["items"])
    grade = max(1, min(12, round((earned / mx if mx else 0) * 12)))
    verdict = ("ðŸŽ‰ Ð§ÑƒÐ´Ð¾Ð²Ð° Ñ€Ð¾Ð±Ð¾Ñ‚Ð°!" if grade >= 10
               else "ðŸ‘ Ð”Ð¾Ð±Ñ€Ðµ, Ð°Ð»Ðµ Ñ” ÐºÑƒÐ´Ð¸ Ñ€Ð¾ÑÑ‚Ð¸." if grade >= 7
               else "ðŸ“– ÐœÐ°Ñ‚ÐµÑ€Ñ–Ð°Ð» Ñ‚Ñ€ÐµÐ±Ð° Ð¿Ð¾Ð²Ñ‚Ð¾Ñ€Ð¸Ñ‚Ð¸: /summary Ð°Ð±Ð¾ /cards Ð´Ð¾Ð¿Ð¾Ð¼Ð¾Ð¶ÑƒÑ‚ÑŒ.")
    text = (f"ðŸ“ <b>Ð ÐµÐ·ÑƒÐ»ÑŒÑ‚Ð°Ñ‚ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ð¾Ñ—</b> ({html.escape(subject_label(s['subject']))})\n\n"
            + "\n".join(lines)
            + f"\n\n<b>Ð‘Ð°Ð»Ð¸: {earned}/{mx} Â· ÐžÑ†Ñ–Ð½ÐºÐ°: {grade}/12</b>\n{verdict}")
    try:
        bot.edit_message_text(text, s["chat"], status.message_id, parse_mode="HTML")
    except Exception:
        bot.send_message(s["chat"], text, parse_mode="HTML")


# ===========================================================================
# /formulas - Ñ‚Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»
# ===========================================================================
FORMULA_ROUNDS = 5
FORMULA = {}   # uid -> {"items", "pos", "right", "chat", "subject"}


@bot.message_handler(commands=["formulas"])
def cmd_formulas(message):
    if not gate(message):
        return
    uid = message.from_user.id
    args = message.text.split(maxsplit=2)
    if len(args) > 1 and args[1].lower() in ("ÑÑ‚Ð¾Ð¿", "stop"):
        if FORMULA.pop(uid, None):
            bot.reply_to(message, "Ð¢Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ð·ÑƒÐ¿Ð¸Ð½ÐµÐ½Ð¾.")
        else:
            bot.reply_to(message, "Ð¢Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ‰Ðµ Ð½Ðµ Ð·Ð°Ð¿ÑƒÑÐºÐ°Ð²ÑÑ. ÐŸÐ¾Ñ‡Ð°Ñ‚Ð¸: /formulas Ñ„Ñ–Ð·Ð¸ÐºÐ°")
        return
    if _alive(EXAM, uid):
        bot.reply_to(message, "Ð¡Ð¿Ð¾Ñ‡Ð°Ñ‚ÐºÑƒ Ð·Ð°Ð²ÐµÑ€ÑˆÐ¸ ÐºÐ¾Ð½Ñ‚Ñ€Ð¾Ð»ÑŒÐ½Ñƒ (/exam ÑÑ‚Ð¾Ð¿).")
        return
    subject = resolve_subject(args[1]) if len(args) > 1 else settings[uid].get("subject")
    if not subject:
        bot.reply_to(message, "Ð’ÐºÐ°Ð¶Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚: /formulas Ñ„Ñ–Ð·Ð¸ÐºÐ° [Ñ‚ÐµÐ¼Ð°]")
        return
    topic = args[2].strip() if len(args) > 2 else ""
    if limit_reached(uid):
        bot.reply_to(message, f"Ð‘ÐµÐ·ÐºÐ¾ÑˆÑ‚Ð¾Ð²Ð½Ð¸Ð¹ Ð´ÐµÐ½Ð½Ð¸Ð¹ Ð»Ñ–Ð¼Ñ–Ñ‚ ({DAILY_LIMIT}) Ð²Ð¸Ñ‡ÐµÑ€Ð¿Ð°Ð½Ð¾. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð·Ð°Ð²Ñ‚Ñ€Ð°.")
        return
    path = book_path_any(subject)
    if not path:
        bot.reply_to(message, f"Ð¤Ð°Ð¹Ð» Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Â«{SUBJECTS[subject]}Â» Ð½Ðµ Ð·Ð½Ð°Ð¹Ð´ÐµÐ½Ð¾.")
        return
    status = bot.reply_to(message, "ÐžÐ±Ð¸Ñ€Ð°ÑŽ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸â€¦ â±ï¸")
    bot.send_chat_action(message.chat.id, "typing")
    try:
        if topic:
            context, _ = find_context(path, topic)
        else:
            pages = [p for p in load_pages(path) if len(p) > 400]
            context = "\n\n".join(random.sample(pages, min(3, len(pages)))) if pages else ""
        prompt = (
            f"ÐÐ° Ð¾ÑÐ½Ð¾Ð²Ñ– Ñ„Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ñ–Ð² Ð¿Ñ–Ð´Ñ€ÑƒÑ‡Ð½Ð¸ÐºÐ° Ð²Ð¸Ð±ÐµÑ€Ð¸ {FORMULA_ROUNDS} Ñ„Ð¾Ñ€Ð¼ÑƒÐ» Ð°Ð±Ð¾ Ð¿Ñ€Ð°Ð²Ð¸Ð» "
            "Ñ€Ñ–Ð²Ð½Ñ 8 ÐºÐ»Ð°ÑÑƒ. ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON-Ð¼Ð°ÑÐ¸Ð² Ñ‚Ð°ÐºÐ¾Ð³Ð¾ Ð²Ð¸Ð³Ð»ÑÐ´Ñƒ "
            '[{"term": "Ð½Ð°Ð·Ð²Ð° Ð²ÐµÐ»Ð¸Ñ‡Ð¸Ð½Ð¸ Ð°Ð±Ð¾ Ð¿Ñ€Ð°Ð²Ð¸Ð»Ð°", "formula": "ÑÐ°Ð¼Ð° Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð° Ñ‚ÐµÐºÑÑ‚Ð¾Ð¼"}]. '
            "Ð‘ÐµÐ· HTML.\n\n"
            f"Ð¤Ñ€Ð°Ð³Ð¼ÐµÐ½Ñ‚Ð¸:\n{context or '(Ð½ÐµÐ¼Ð°Ñ”)'}"
        )
        raw = call_gemini(prompt, json_mode=True, temperature=0.5)
        if not raw:
            bot.edit_message_text("Ð¨Ð† Ð·Ð°Ñ€Ð°Ð· Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ð¿Ñ–Ð·Ð½Ñ–ÑˆÐµ.",
                                  message.chat.id, status.message_id)
            return
        items = [it for it in parse_json(raw) if it.get("term") and it.get("formula")][:FORMULA_ROUNDS]
        if len(items) < 3:
            bot.edit_message_text("ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð·Ñ–Ð±Ñ€Ð°Ñ‚Ð¸ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð¸, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
            return
        FORMULA[uid] = {"items": items, "pos": 0, "right": 0,
                        "chat": message.chat.id, "subject": subject, "ts": time.time()}
        register_use(uid, message.from_user.first_name)
        bot.delete_message(message.chat.id, status.message_id)
        _formula_ask(uid)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° /formulas")
        try:
            bot.edit_message_text("Ð¡Ñ‚Ð°Ð»Ð°ÑÑ Ð¿Ð¾Ð¼Ð¸Ð»ÐºÐ°. Ð¡Ð¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass


def _formula_ask(uid):
    f = FORMULA.get(uid)
    if not f:
        return
    it = f["items"][f["pos"]]
    text = (f"ðŸ§® <b>Ð¢Ñ€ÐµÐ½Ð°Ð¶ÐµÑ€ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»</b> ({html.escape(subject_label(f['subject']))}) Â· "
            f"{f['pos'] + 1}/{len(f['items'])} Â· Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾: {f['right']}\n\n"
            f"ÐÐ°Ð¿Ð¸ÑˆÐ¸ Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ñƒ: <b>{html.escape(str(it['term']))}</b>\n\n"
            "<i>Ð£ Ð±ÑƒÐ´ÑŒ-ÑÐºÐ¾Ð¼Ñƒ Ð·Ð°Ð¿Ð¸ÑÑ–: E=mvÂ²/2 Ð¸Ð»Ð¸ E = m*v^2/2. Ð’Ð¸Ð¹Ñ‚Ð¸: /formulas ÑÑ‚Ð¾Ð¿</i>")
    bot.send_message(f["chat"], text, parse_mode="HTML")


@bot.message_handler(func=lambda m: _alive(FORMULA, m.from_user.id)
                      and not m.text.startswith("/")
                      and m.chat.id == FORMULA[m.from_user.id]["chat"],
                      content_types=["text"])
def on_formula_answer(message):
    uid = message.from_user.id
    f = FORMULA[uid]
    f["ts"] = time.time()   # Ð¶Ð¸Ð²Ð¸Ð¼ TTL Ð¿Ñ€Ð¸ ÐºÐ¾Ð¶Ð½Ñ–Ð¹ Ð²Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ñ–
    it = f["items"][f["pos"]]
    status = bot.reply_to(message, "ÐŸÐµÑ€ÐµÐ²Ñ–Ñ€ÑÑŽâ€¦ â±ï¸")
    prompt = (
        f"Ð’ÐµÐ»Ð¸Ñ‡Ð¸Ð½Ð°: {it['term']}\nÐ•Ñ‚Ð°Ð»Ð¾Ð½Ð½Ð° Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð°: {it['formula']}\n"
        f"Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ ÑƒÑ‡Ð½Ñ: {message.text[:200]}\n\n"
        "Ð¤Ð¾Ñ€Ð¼ÑƒÐ»Ð¸ Ð²Ð²Ð°Ð¶Ð°ÑŽÑ‚ÑŒÑÑ Ð¾Ð´Ð½Ð°ÐºÐ¾Ð²Ð¸Ð¼Ð¸, ÑÐºÑ‰Ð¾ Ð·Ð±Ñ–Ð³Ð°Ñ”Ñ‚ÑŒÑÑ Ð·Ð¼Ñ–ÑÑ‚ (Ñ€Ñ–Ð·Ð½Ñ– Ð¿Ð¾Ð·Ð½Ð°Ñ‡ÐµÐ½Ð½Ñ Ñ‚Ð° Ñ„Ð¾Ñ€Ð¼Ð° "
        "Ð·Ð°Ð¿Ð¸ÑÑƒ Ð´Ð¾Ð¿ÑƒÑÑ‚Ð¸Ð¼Ñ–). ÐŸÐ¾Ð²ÐµÑ€Ð½Ð¸ Ð›Ð˜Ð¨Ð• JSON: "
        '{"match": true/false, "correct": "Ð¿Ñ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð° Ñ„Ð¾Ñ€Ð¼ÑƒÐ»Ð° Ñ‚ÐµÐºÑÑ‚Ð¾Ð¼"}')
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
            mark = "âœ… Ð’ÐµÑ€Ð½Ð¾!"
        else:
            mark = f"âŒ ÐÐµ Ñ‚Ðµ. ÐŸÑ€Ð°Ð²Ð¸Ð»ÑŒÐ½Ð¾: <b>{html.escape(correct)}</b>"
        try:
            bot.edit_message_text(mark, message.chat.id, status.message_id, parse_mode="HTML")
            bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass
    else:
        try:
            bot.edit_message_text("Ð¨Ð† Ð½ÐµÐ´Ð¾ÑÑ‚ÑƒÐ¿Ð½Ð¸Ð¹, Ñ€Ð°ÑƒÐ½Ð´ Ð¿Ñ€Ð¾Ð¿ÑƒÑ‰ÐµÐ½Ð¾.",
                                  message.chat.id, status.message_id)
        except Exception:
            pass
    if f["pos"] < len(f["items"]):
        _formula_ask(uid)
    else:
        FORMULA.pop(uid, None)
        r, n = f["right"], len(f["items"])
        bot.send_message(f["chat"], f"ðŸ ÐŸÑ–Ð´ÑÑƒÐ¼Ð¾Ðº: {r} Ñ–Ð· {n}. "
                         + ("ðŸ’ª Ð§ÑƒÐ´Ð¾Ð²Ð¾!" if r == n else "Ð©Ðµ Ð¾Ð´Ð½Ð° ÑÐ¿Ñ€Ð¾Ð±Ð°: /formulas"))

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


bot.message_handler(commands=["explain"])(_mode_command("explain", "ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´: /explain Ñ„Ñ–Ð·Ð¸ÐºÐ° Ð·Ð°Ð´Ð°Ñ‡Ð° 3.5"))
bot.message_handler(commands=["check"])(_mode_command("check", "ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´: /check Ð°Ð»Ð³ÐµÐ±Ñ€Ð° Ð¼Ñ–Ð¹ Ñ€Ð¾Ð·Ð²â€™ÑÐ·Ð¾Ðº: x = 5... (Ð°Ð±Ð¾ Ð½Ð°Ð´Ñ–ÑˆÐ»Ð¸ Ñ„Ð¾Ñ‚Ð¾ Ð· Ð¿Ñ–Ð´Ð¿Ð¸ÑÐ¾Ð¼ /check)"))
bot.message_handler(commands=["summary"])(_mode_command("summary", "ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´: /summary Ð¸ÑÑ‚Ð¾Ñ€Ð¸Ñ ÐŸÐµÑ€Ð²Ð°Ñ Ð¼Ð¸Ñ€Ð¾Ð²Ð°Ñ Ð²Ð¾Ð¹Ð½Ð°"))
bot.message_handler(commands=["essay"])(_mode_command("essay", "ÐŸÑ€Ð¸ÐºÐ»Ð°Ð´: /essay ÑƒÐºÑ€Ð»Ð¸Ñ‚ Ð¾Ð±Ñ€Ð°Ð· Ð¢Ð°Ñ€Ð°ÑÐ° Ð‘ÑƒÐ»ÑŒÐ±Ð¸, 250 ÑÐ»Ñ–Ð²"))


@bot.message_handler(commands=["quiz"])
def cmd_quiz(message):
    if not gate(message):
        return
    args = message.text.split(maxsplit=1)
    subject, topic = split_subject(args[1] if len(args) > 1 else "")
    subject = subject or settings[message.from_user.id]["subject"]
    if not subject:
        bot.reply_to(message, "Ð’ÐºÐ°Ð¶Ð¸ Ð¿Ñ€ÐµÐ´Ð¼ÐµÑ‚: /quiz Ñ„Ñ–Ð·Ð¸ÐºÐ° Ð°Ð±Ð¾ /quiz Ñ„Ñ–Ð·Ð¸ÐºÐ° Ð·Ð°ÐºÐ¾Ð½Ð¸ ÐÑŒÑŽÑ‚Ð¾Ð½Ð°")
        return
    run_quiz(message, subject, topic)


# ===========================================================================
# Ð¤Ð¾Ñ‚Ð¾, Ð³Ð¾Ð»Ð¾ÑÐ¾Ð²Ñ‹Ðµ, Ñ‚ÐµÐºÑÑ‚, ÐºÐ½Ð¾Ð¿ÐºÐ¸
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
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐºÐ°Ñ‡Ð°Ñ‚ÑŒ Ñ„Ð¾Ñ‚Ð¾")
        bot.reply_to(message, "ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ð¸Ñ‚Ð¸ Ñ„Ð¾Ñ‚Ð¾, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.")
        return
    dispatch(message, mode, subject, task, [gt.Part.from_bytes(data=data, mime_type="image/jpeg")])


@bot.message_handler(content_types=["voice", "audio"])
def on_voice(message):
    if not gate(message):
        return
    obj = message.voice or message.audio
    if obj.duration and obj.duration > MAX_VOICE_SECONDS:
        bot.reply_to(message, f"Ð“Ð¾Ð»Ð¾ÑÐ¾Ð²Ðµ Ð¿Ð¾Ð²Ñ–Ð´Ð¾Ð¼Ð»ÐµÐ½Ð½Ñ Ð½Ð°Ð´Ñ‚Ð¾ Ð´Ð¾Ð²Ð³Ðµ (Ð¼Ð°ÐºÑÐ¸Ð¼ÑƒÐ¼ {MAX_VOICE_SECONDS} Ñ).")
        return
    mime = "audio/ogg" if message.voice else (obj.mime_type or "audio/mpeg")
    try:
        data = bot.download_file(bot.get_file(obj.file_id).file_path)
    except Exception:
        log.exception("ÐÐµ ÑƒÐ´Ð°Ð»Ð¾ÑÑŒ ÑÐºÐ°Ñ‡Ð°Ñ‚ÑŒ Ð°ÑƒÐ´Ð¸Ð¾")
        bot.reply_to(message, "ÐÐµ Ð²Ð´Ð°Ð»Ð¾ÑÑ Ð·Ð°Ð²Ð°Ð½Ñ‚Ð°Ð¶Ð¸Ñ‚Ð¸ Ð°ÑƒÐ´Ñ–Ð¾, ÑÐ¿Ñ€Ð¾Ð±ÑƒÐ¹ Ñ‰Ðµ Ñ€Ð°Ð·.")
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
        hint = f" ÐœÐ¾Ð¶Ð»Ð¸Ð²Ð¾, Ñ‚Ð¸ Ð¼Ð°Ð² Ð½Ð° ÑƒÐ²Ð°Ð·Ñ– /{close[0]}?" if close else ""
        bot.reply_to(message, f"ÐÐµ Ð·Ð½Ð°ÑŽ Ñ‚Ð°ÐºÐ¾Ñ— ÐºÐ¾Ð¼Ð°Ð½Ð´Ð¸.{hint}\nÐ¡Ð¿Ð¸ÑÐ¾Ðº: /help")
        return
    if not gate(message):
        return
    uid = message.from_user.id
    subject, task = split_subject(message.text)
    if subject and not task:
        settings[uid]["subject"] = subject
        bot.reply_to(message, f"ÐŸÑ€ÐµÐ´Ð¼ÐµÑ‚ Ð¾Ð±Ñ€Ð°Ð½Ð¾: {subject_label(subject)}. Ð¢ÐµÐ¿ÐµÑ€ Ð¿Ð¸ÑˆÐ¸ Ð·Ð°Ð²Ð´Ð°Ð½Ð½Ñ.")
        return
    subject = subject or settings[uid]["subject"]
    dispatch(message, "answer", subject, task, [])


def _get_bot_username():
    try:
        return bot.get_me().username
    except Exception:
        return None


BOT_USERNAME = None  # Ð·Ð°Ð¿Ð¾Ð»Ð½ÑÐµÑ‚ÑÑ Ð¿Ñ€Ð¸ ÑÑ‚Ð°Ñ€Ñ‚Ðµ Ð±Ð¾Ñ‚Ð°


def addressed(m):
    """True, ÐµÑÐ»Ð¸ Ð±Ð¾Ñ‚ Ð°Ð´Ñ€ÐµÑÐ¾Ð²Ð°Ð½ Ð² Ð³Ñ€ÑƒÐ¿Ð¿Ð¾Ð²Ð¾Ð¼ Ñ‡Ð°Ñ‚Ðµ."""
    if m.chat.type == "private":
        return True
    text = (m.text or "") + "@"
    return f"@{BOT_USERNAME}" in text or bool(
        m.reply_to_message
        and m.reply_to_message.from_user
        and m.reply_to_message.from_user.username == BOT_USERNAME
    )


# ===========================================================================
# ÐœÐ¸Ð½Ð¸-Ð²ÐµÐ±-ÑÐµÑ€Ð²ÐµÑ€ (Ð½ÑƒÐ¶ÐµÐ½ Render, Ñ‡Ñ‚Ð¾Ð±Ñ‹ ÑÑ‡Ð¸Ñ‚Ð°Ñ‚ÑŒ ÑÐµÑ€Ð²Ð¸Ñ Ð¶Ð¸Ð²Ñ‹Ð¼; Ð½Ð° Ð½ÐµÐ³Ð¾ Ð¶Ðµ
# Ð¼Ð¾Ð¶Ð½Ð¾ Ð½Ð°Ð¿Ñ€Ð°Ð²Ð¸Ñ‚ÑŒ UptimeRobot, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Ð±ÐµÑÐ¿Ð»Ð°Ñ‚Ð½Ñ‹Ð¹ ÑÐµÑ€Ð²Ð¸Ñ Ð½Ðµ Ð·Ð°ÑÑ‹Ð¿Ð°Ð»)
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
        log.exception("Ð’ÐµÐ±-ÑÐµÑ€Ð²ÐµÑ€ Ð½Ðµ Ð·Ð°Ð¿ÑƒÑÑ‚Ð¸Ð»ÑÑ (Ð¿Ð¾Ñ€Ñ‚ %s)", port)


def self_ping():
    """ÐšÐ°Ð¶Ð´Ñ‹Ðµ 10 Ð¼Ð¸Ð½ ÑÑ‚ÑƒÑ‡Ð¸Ñ‚ÑÑ Ð² ÑÐ²Ð¾Ð¹ health-URL, Ñ‡Ñ‚Ð¾Ð±Ñ‹ Render Ð½Ðµ Ð·Ð°ÑÑ‹Ð¿Ð°Ð»."""
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
# Ð—Ð°Ð¿ÑƒÑÐº
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
        if time.time() - _last_inline_gen.get(uid, 0) < 2:  # Ð½Ðµ Ñ‡Ð°Ñ‰Ðµ Ñ€Ð°Ð·Ð° Ð² 2 ÑÐµÐº
            bot.answer_inline_query(query.id, [], cache_time=0)
            return
        _last_inline_gen[uid] = time.time()
        if limit_reached(uid):
            bot.answer_inline_query(query.id, [], cache_time=1)
            return
        answer = call_gemini([f"Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´Ð°Ð¹ ÐºÐ¾Ñ€Ð¾Ñ‚ÐºÐ¾ (Ð´Ð¾ 100 ÑÐ»Ñ–Ð²) Ð½Ð° Ð·Ð°Ð¿Ð¸Ñ‚Ð°Ð½Ð½Ñ ÑƒÑ‡Ð½Ñ:\n{q[:500]}"],
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
            description="Ð’Ñ–Ð´Ð¿Ð¾Ð²Ñ–Ð´ÑŒ Ð¨Ð† Â· Ð½Ð°Ñ‚Ð¸ÑÐ½Ð¸, Ñ‰Ð¾Ð± Ð½Ð°Ð´Ñ–ÑÐ»Ð°Ñ‚Ð¸ Ð² Ñ‡Ð°Ñ‚")
        bot.answer_inline_query(query.id, [art], cache_time=10)
    except Exception:
        log.exception("ÐžÑˆÐ¸Ð±ÐºÐ° inline")

# ===========================================================================
# Catch-all callback â€” Ð²ÑÐµÐ³Ð´Ð° Ð¿Ð¾ÑÐ»ÐµÐ´Ð½Ð¸Ð¹ Ð¾Ð±Ñ€Ð°Ð±Ð¾Ñ‚Ñ‡Ð¸Ðº
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
    # BOT_USERNAME Ð½ÑƒÐ¶ÐµÐ½ Ð´Ð»Ñ Ð¿Ñ€Ð¾Ð²ÐµÑ€ÐºÐ¸ Â«Ð°Ð´Ñ€ÐµÑÐ¾Ð²Ð°Ð½ Ð±Ð¾Ñ‚ÑƒÂ» Ð² Ð³Ñ€ÑƒÐ¿Ð¿Ð¾Ð²Ñ‹Ñ… Ñ‡Ð°Ñ‚Ð°Ñ….
    BOT_USERNAME = _get_bot_username()
    log.info("Ð‘Ð¾Ñ‚ Ð·Ð°Ð¿ÑƒÑ‰ÐµÐ½Ð¾. ÐœÐ¾Ð´ÐµÐ»Ð¸: %s | ÐºÐ»ÑŽÑ‡ÐµÐ¹ Gemini: %d | @%s",
             ", ".join(MODELS), len(API_KEYS), BOT_USERNAME)

    while True:
        try:
            bot.infinity_polling(skip_pending=True)
        except Exception:
            log.exception("Polling ÑƒÐ¿Ð°Ð», Ð¿ÐµÑ€ÐµÐ·Ð°Ð¿ÑƒÑÐº Ñ‡ÐµÑ€ÐµÐ· 10 ÑÐµÐº")
            time.sleep(3)
