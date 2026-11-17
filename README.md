# School Homework Bot

Telegram bot for Ukrainian school homework help (class 8-Б) using Google Gemini AI and PDF textbook parsing.

## Environment Variables

- `TELEGRAM_TOKEN` — Telegram bot token from @BotFather (required)
- `GEMINI_API_KEYS` — comma-separated Gemini API keys (required)
- `GEMINI_MODELS` — comma-separated model names (default: gemini-3.5-flash-lite)
- `ADMIN_ID` — your Telegram ID (no limits, sees statistics)
- `ALLOWED_IDS` — comma-separated user IDs; empty = everyone has access
- `DAILY_LIMIT` — requests per user per day (default: 40)
- `TOP_PAGES` — textbook pages to return to the model (default: 2)
- `UPSTASH_REDIS_REST_URL` — Redis REST URL for schedules and stats
- `UPSTASH_REDIS_REST_TOKEN` — Redis REST token
- `SELF_PING_URL` — URL used to keep the service awake (self-ping)

## Features

- Homework fetching from school website
- PDF textbook search with BM25-like IDF scoring
- Multi-key Gemini API with rotation and retry
- Thread-safe LRU caches
- Atomic Redis-backed daily flags
- Group chat mention detection
- Document sharing (PDF from teacher)

## Structure

- `script.py` — main bot code
- `cache/` — cached PDF text (gitignored)
- `data/` — statistics (not persisted on Render)
- `книги/` — PDF textbooks