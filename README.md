Возможности
Ответы/объяснения/проверка решений по фото, голосовым, тексту и PDF
Поиск по PDF-учебникам (11 предметов) — ответ со ссылками на страницы
/hw — домашка с сайта школы по классам (31 класс, автопоиск с сайта)
Расписания классов через Redis, без правок кода (/setschedule)
Тренажёры: /quiz, /exam (контрольная с оценкой /12), /formulas, /cards (интервальное повторение)
/proofread — орфография, /remind — напоминания, /pomodoro — таймер
Рейтинг класса со стриками (/top), вопрос дня, отчёты админу
Инлайн-режим: @бот вопрос в любом чате
Стек
Python 3.12 · pyTelegramBotAPI · Google Gemini (ротация ключей, фолбэк-модели)· Upstash Redis · pypdf/BeautifulSoup · Render

Запуск
Переменные окружения: TELEGRAM_TOKEN, GEMINI_API_KEYS, ADMIN_ID,UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN. Старт: `python script.py
