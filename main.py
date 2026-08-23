import json
import random
import sqlite3
import os
import time
import asyncio
import re
import shutil
from datetime import datetime, timedelta
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# ===== РЕБУСЫ =====
from rebus import expression_to_blocks, draw_rebus_from_blocks, load_dictionary, split_into_parts, find_image_case_insensitive

# ===== НАСТРОЙКИ =====
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5206039766
QUIZ_FILE = "quizzes.json"
MEMES_FILE = "memes.json"
BASE_QUIZZES_DB = "base_quizzes.db"
USERS_DB = "quiz_users.db"

# Хранилище активных ребусов
active_rebuses = {}

# ===== РЕДКОСТИ =====
RARITY_REWARDS = {
    "common": 1,
    "uncommon": 2,
    "rare": 3,
    "epic": 5,
    "legendary": 10
}

RARITY_EMOJIS = {
    "common": "⬜ Глорповский",
    "uncommon": "🟩 Пустотный",
    "rare": "🟦 Организационный",
    "epic": "🟪 от Междумирца",
    "legendary": "⬛ Прямиком из Тюрьмы Времени"
}

RARITY_EMOJI_ONLY = {
    "common": "⬜",
    "uncommon": "🟩",
    "rare": "🟦",
    "epic": "🟪",
    "legendary": "⬛"
}

RANKS = [
    {"name": "Первичная материя", "min_score": 0, "emoji": "🔷"},
    {"name": "Мироходец", "min_score": 10, "emoji": "☀️"},
    {"name": "Багрянник", "min_score": 25, "emoji": "🩸"},
    {"name": "Первый", "min_score": 50, "emoji": "🎭"},
    {"name": "Сотрудник Организации", "min_score": 100, "emoji": "💎"},
    {"name": "Кондуктор Синклита", "min_score": 150, "emoji": "🔥"},
    {"name": "Гендиректор Организации", "min_score": 200, "emoji": "👑"},
    {"name": "Единый Таймлайн", "min_score": 300, "emoji": "🎆"},
    {"name": "Программист ST", "min_score": 400, "emoji": "💻"},
    {"name": "Сценарист ST", "min_score": 500, "emoji": "📖"},
]

def get_rank(score):
    for rank in reversed(RANKS):
        if score >= rank["min_score"]:
            return rank
    return RANKS[0]

# ===== БАЗА ДАННЫХ =====
def init_user_db():
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY,
                  first_name TEXT,
                  total INTEGER DEFAULT 0,
                  rank TEXT DEFAULT "Новичок")''')
    c.execute('''CREATE TABLE IF NOT EXISTS completions
                 (user_id INTEGER,
                  quiz_id TEXT,
                  completed_at TIMESTAMP,
                  PRIMARY KEY (user_id, quiz_id))''')
    c.execute('''CREATE TABLE IF NOT EXISTS quiz_stats
                 (user_id INTEGER PRIMARY KEY,
                  score INTEGER DEFAULT 0,
                  today_plays INTEGER DEFAULT 0,
                  last_play_date TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS rebus_solves
                 (user_id INTEGER PRIMARY KEY,
                  user_name TEXT,
                  solves INTEGER DEFAULT 0)''')
    conn.commit()
    conn.close()
    print("✅ База пользователей инициализирована")

def init_base_quizzes_db():
    conn = sqlite3.connect(BASE_QUIZZES_DB)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS base_quizzes
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  question TEXT,
                  options TEXT,
                  correct_option_id INTEGER,
                  rarity TEXT DEFAULT 'common',
                  date TEXT)''')
    conn.commit()
    conn.close()
    print("✅ База вопросов инициализирована")

# ===== ФУНКЦИИ ДЛЯ РЕБУСОВ =====
def add_rebus_solve(user_id, user_name):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''INSERT INTO rebus_solves (user_id, user_name, solves)
                 VALUES (?, ?, 1)
                 ON CONFLICT(user_id) DO UPDATE SET
                 solves = solves + 1,
                 user_name = excluded.user_name''',
              (user_id, user_name))
    conn.commit()
    conn.close()

# ===== ОСТАЛЬНЫЕ ФУНКЦИИ =====
def get_user_stats(user_id):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('SELECT score, today_plays, last_play_date FROM quiz_stats WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return {"score": row[0], "today_plays": row[1], "last_play_date": row[2]}
    return {"score": 0, "today_plays": 0, "last_play_date": None}

def update_user_stats(user_id, score, today_plays, last_play_date):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''INSERT INTO quiz_stats (user_id, score, today_plays, last_play_date)
                 VALUES (?, ?, ?, ?)
                 ON CONFLICT(user_id) DO UPDATE SET
                 score = excluded.score,
                 today_plays = excluded.today_plays,
                 last_play_date = excluded.last_play_date''',
              (user_id, score, today_plays, last_play_date))
    conn.commit()
    conn.close()

def get_played_question_ids(user_id):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    today = datetime.now().date().isoformat()
    c.execute('''SELECT quiz_id FROM completions
                 WHERE user_id = ? AND DATE(completed_at) = ?''', (user_id, today))
    rows = c.fetchall()
    conn.close()
    return [row[0] for row in rows]

def mark_question_as_played(user_id, quiz_id):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO completions (user_id, quiz_id, completed_at) VALUES (?, ?, ?)',
              (user_id, quiz_id, datetime.now()))
    conn.commit()
    conn.close()

def get_random_question(user_id):
    played_ids = get_played_question_ids(user_id)
    conn = sqlite3.connect(BASE_QUIZZES_DB)
    c = conn.cursor()

    if played_ids:
        placeholders = ','.join(['?'] * len(played_ids))
        c.execute(f'''
            SELECT id, question, options, correct_option_id, rarity FROM base_quizzes
            WHERE id NOT IN ({placeholders})
            ORDER BY RANDOM() LIMIT 1
        ''', played_ids)
    else:
        c.execute('SELECT id, question, options, correct_option_id, rarity FROM base_quizzes ORDER BY RANDOM() LIMIT 1')

    row = c.fetchone()
    conn.close()
    return row

def add_base_quiz(question, options, correct_option_id):
    rarity_roll = random.random()
    if rarity_roll < 0.60:
        rarity = "common"
    elif rarity_roll < 0.85:
        rarity = "uncommon"
    elif rarity_roll < 0.95:
        rarity = "rare"
    elif rarity_roll < 0.99:
        rarity = "epic"
    else:
        rarity = "legendary"

    conn = sqlite3.connect(BASE_QUIZZES_DB)
    c = conn.cursor()
    c.execute('INSERT INTO base_quizzes (question, options, correct_option_id, rarity, date) VALUES (?, ?, ?, ?, ?)',
              (question, options, correct_option_id, rarity, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return rarity

def count_quizzes_by_rarity():
    conn = sqlite3.connect(BASE_QUIZZES_DB)
    c = conn.cursor()
    c.execute('SELECT rarity, COUNT(*) FROM base_quizzes GROUP BY rarity')
    result = dict(c.fetchall())
    conn.close()
    return result

def parse_quiz_line(line):
    match = re.match(r'^(.+?)\s*\((.+)\)\s*$', line.strip())
    if not match:
        return None

    question = match.group(1).strip()
    options = [opt.strip() for opt in match.group(2).split(';') if opt.strip()]

    if len(options) < 2:
        return None

    correct_option_id = None
    cleaned = []
    for i, opt in enumerate(options):
        if opt.endswith('*'):
            correct_option_id = i
            cleaned.append(opt[:-1].strip())
        else:
            cleaned.append(opt)

    if correct_option_id is None:
        correct_option_id = 0

    return question, cleaned, correct_option_id

# ===== ЗАГРУЗКА МЕМОВ =====
def load_memes():
    if not os.path.exists(MEMES_FILE):
        return []
    with open(MEMES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_memes(memes):
    with open(MEMES_FILE, "w", encoding="utf-8") as f:
        json.dump(memes, f, ensure_ascii=False, indent=2)

# ===== АНТИСПАМ =====
antispam = {}

def check_antispam(user_id):
    now = time.time()
    user = antispam.get(user_id, {"blocked_until": 0, "last_command": 0, "count": 0})

    if user["blocked_until"] > now:
        wait = int(user["blocked_until"] - now)
        return False, f"🚫 *Стоп!* Ты в спам-бане `{wait}` сек."

    if now - user["last_command"] < 2.0:
        user["count"] += 1
        user["last_command"] = now
        antispam[user_id] = user

        if user["count"] >= 2:
            user["blocked_until"] = now + 20
            user["count"] = 0
            antispam[user_id] = user
            return False, "🚫 *Спам-детект!* Блокировка на 20 сек."
        else:
            return False, ""

    user["count"] = 0
    user["last_command"] = now
    antispam[user_id] = user
    return True, ""

def antispam_decorator(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id

        # Очищаем активный ребус при любой команде (включая /rebus)
        if update.message and update.message.text and update.message.text.startswith('/'):
            if user_id in active_rebuses:
                del active_rebuses[user_id]

        allowed, msg = check_antispam(user_id)
        if not allowed:
            if msg:
                await update.message.reply_text(msg, parse_mode="Markdown")
            return
        return await func(update, context)
    return wrapper

RP_TRIGGERS = {
    "чай джейса": {
        "type": "self",
        "responses": [
            "{user} умер от переизбытка маны ☠️",
            "{user} выпил чай Джейса и почувствовал прилив сил! 🧙"
        ]
    },
    "пузатый лис": {
        "type": "target",
        "responses": [
            "{user} выпил одуванчиковое пиво вместе с {target} 🍺",
            "{user} и {target} устроили пивную дуэль! 🍻"
        ]
    },
    "тюрьма времени": {
        "type": "target",
        "responses": [
            "{user} запечатал в самой ужасной тюрьме {target} 🔳",
            "{user} не удалось запечатать {target}! 🤯"
        ]
    },
    "прочесть дневник джодаха": {
        "type": "self",
        "responses": [
            "{user} открыл дневник и ужаснулся от количества поглощённых душ 😨",
            "{user} открыл дневник, но не смог ничего прочесть из-за шифра 😕 "
        ]
    },
     "обнуление": {
        "type": "target",
        "responses": [
            "{user} понизил до нулевого уровня {target} 🔮 ",
            "{user} не вышло обнулить из-за пелены {target}! "
        ]
    },
     "отвязка": {
        "type": "self",
        "responses": [
            "{user} успешно отвязался от времени 🕧",
            "{user} неудачно провел операцию и погиб 💀"
        ]
    },
     "смотрящий": {
        "type": "self",
        "responses": [
            "{user} провел увлекательную беседу со Временем 👀",
            "{user} посмотрел на Смотрящего и заметил лёгкий интерес в его взгляде 👀",
            "{user} посмотрел на Смотрящего и заметил безразличие в его взгляде 👀",
            "{user} посмотрел на Смотрящего и ничего не увидел в его взгляде 👀",
            "{user} посмотрел на Смотрящего и продрог от холода 👀",
            "{user} посмотрел на Смотрящего и заметил дружелюбие в его взгляде 👀",
            "{user} посмотрел на Смотрящего и понял что ему осталось недолго 👀",
            "{user} посмотрел на Смотрящего и подвергся концентрированному времени👀",
            
            
            
            
            
            
        ]
    },
     "аномалия": {
        "type": "self",
        "responses": [
            "{user} увидел аномалию с Мистером О'Флафферти и был испепелён его лазерами💥 ",
            "{user} увидел аномалию врат Отправления и даже не обратил внимания 🟣",
            "{user} увидел аномалию с очками Джона и теперь ходит с модными очками 😎",
            "{user} увидел аномалию со Смотрящим и повеяло холодом ❄️",
            "{user} увидел аномалию с отвязкой и едва не погиб от перегрузки 😵‍💫 ",
            "{user} увидел аномалию с ужасными монстрами и активировал Искру ✴️",
            "{user} увидел аномалию со скинтонитом и решил взять ее с собой (это была ошибка)❌ ",
            "{user} увидел аномалию с мечом Путешественника и решил что он идеально подходит для нарезки салата (и не только) ⚔️",
            "{user} увидел аномалию с Воплощёнными и закрылся в сонном измерении на месяц 🏠 ",
            "{user} увидел аномалию с масками Первых и на него нахлынули старые воспоминания 🎭 ",
            "{user} увидел аномалию с взломанным Г.Л.А.С и решил что повременит с белым пространством ⬜️ ",
            "{user} благодаря аномалии смог узнать имя другого существа и навалял ему 🤩 ",
            "{user} услышал от Риз очередную чересчур знакомую фразу, что же это может значить? 🤔",
            "{user} увидел аномалию и переместился в белое пространство 👻 ",
            "{user} не увидел никаких аномалий ☹️",
        ]
    },
     "трескануть орешки": {
        "type": "target",
        "responses": [
            "{user} потрескал орешки с {target} 🌰 ",
            "{user} не потрескали орешки с {target} так как белка все украла 😭"
        ]
    },
     "орешки": {
        "type": "self",
        "responses": [
            "{user} покушал орешки биг боб 🌰 ",
            "{user} не поел орешков биг боб, так как попался гнилой орешек 🤮 "
        ]
    },
     "глорп": {
        "type": "self",
        "responses": [
            "{user} ГЛОООРПНУЛСЯ по полной 🌕",
            "{user} слегка ГЛОООРПНУЛ 🌘"
        ]
    },
     "видомния": {
        "type": "target",
        "responses": [
            "{user} отправил в Видомнию поганца {target} ☄️ ",
            "{user} не вышло отправить в Видомнию {target} 🌚"
        ]
    },
     "скинтонит": {
        "type": "self",
        "responses": [
            "{user} адаптировался к влиянию кристаллической пустоты ☸️ ",
            "{user} не смог справиться с тьмой и превратился в даска 😵 "
        ]
    },
     "скинт": {
        "type": "self",
        "responses": [
            "{user} посидел возле Путеводного скинта и восстановился ☺️",
            "{user} заразил скинт своей кровью и стал еще сильнее 😈",
            "{user} был отвергнут Первой Матерью и распался на частицы по бесконечным вселенным 🌪",
            "{user} съел скинт и увидел будущее своей ветки 👁 ",
            
        ]
    },
     "очищение": {
        "type": "target",
        "responses": [
            "{user} придал Очищению {target} 🧟 ",
            "{user} не смог осквернить душу и искру {target} 🛡"
        ]
    },
     "тысяча глаз": {
        "type": "self",
        "responses": [
            "{user} использовал Тысячу Глаз и узрел истинное будущее 👁 ",
            "{user} не смог использовать Тысячу глаз из-за перешёптывания демонов в голове 👹 ",
            "{user} использовал Тысячу Глаз, но понял, что никто не способен видеть всех вариантов будущего 😰"
        ]
    
    
    }
}

# ===== КОМАНДЫ =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 *Бот викторин и ребусов*\n\n"
        "/quiz — случайная викторина (рейтинг)\n"
        "/rebus — отгадай ребус\n"
        "/mm — случайный мем\n"
        "/stats — моя статистика\n"
        "/top — топ игроков\n"
        "/rebustop — топ ребусников\n"
        "/donate — поддержать разработку\n"
        "/help — помощь",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Помощь по командам:*\n\n"
        "/quiz — викторина с рейтингом\n"
        "/rebus — отгадай ребус\n"
        "/mm — случайный мем\n"
        "/stats — моя статистика\n"
        "/top — топ-10 игроков\n"
        "/rebustop — топ-10 ребусников\n"
        "/donate — поддержать разработку\n"
        "/help — это сообщение\n\n"
        "🎯 *Как получить рейтинг:*\n"
        "Напиши /quiz и выбери правильный ответ.\n"
        "✅ Правильный ответ: +баллы (зависит от редкости)\n"
        "❌ Неправильный ответ: –1 балл\n"
        "🎮 Ограничение: 5 викторин в день\n\n"
        "🧩 *Как отгадать ребус:*\n"
        "Напиши /rebus, посмотри на картинку и напиши слово в чат.",
        parse_mode="Markdown"
    )

async def donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("💳 Поддержать разработку", url="https://finance.ozon.ru/apps/sbp/ozonbankpay/019da166-0117-7486-83c4-ba6b6a587f43")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "💸 *Поддержать разработку бота*\n\n"
        "Если тебе нравятся викторины — можешь отправить донат.\n\n"
        "Спасибо за поддержку! ❤️",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

# ===== ВИКТОРИНА =====
@antispam_decorator
async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    first_name = update.effective_user.first_name
    today = datetime.now().date().isoformat()
    
    stats = get_user_stats(user_id)
    if stats["last_play_date"] != today:
        stats["today_plays"] = 0
        stats["last_play_date"] = today
        update_user_stats(user_id, stats["score"], 0, today)
    
    if stats["today_plays"] >= 5:
        await update.message.reply_text("❌ Ты уже прошёл 5 викторин сегодня! Возвращайся завтра.")
        return
    
    # Проверяем, есть ли активный вопрос У ЭТОГО ПОЛЬЗОВАТЕЛЯ
    active = context.user_data.get('quiz_question')
    if active and active.get("user_id") == user_id:
        await update.message.reply_text("❌ У тебя уже есть активный вопрос! Ответь на него, чтобы получить новый.")
        return
    
    # Если активный вопрос от другого пользователя — игнорируем
    if active and active.get("user_id") != user_id:
        # Не блокируем, просто продолжаем
        pass
    
    stats["today_plays"] += 1
    update_user_stats(user_id, stats["score"], stats["today_plays"], today)
    
    row = get_random_question(user_id)
    if not row:
        await update.message.reply_text("📭 В базе нет новых вопросов! Добавь через /basequiz")
        return
    
    question_id, question, options_raw, correct_option_id, rarity = row
    options = options_raw.split('|||') if options_raw else []
    reward = RARITY_REWARDS.get(rarity, 1)
    
    quiz_data = {
        "user_id": user_id,
        "question_id": question_id,
        "question": question,
        "options": options,
        "correct_option_id": correct_option_id,
        "reward": reward,
        "rarity": rarity
    }
    context.user_data['quiz_question'] = quiz_data
    
    keyboard = []
    for i, opt in enumerate(options):
        button_text = opt[:35] + "…" if len(opt) > 35 else opt
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"quiz_ans_{i}")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    rank = get_rank(stats["score"])
    await update.message.reply_text(
        f"❓ *{question}*\n\n"
        f"{RARITY_EMOJIS.get(rarity, '')}\n"
        f"🎁 Награда: +{reward} баллов\n\n"
        f"🏆 Твои баллы: {stats['score']}\n"
        f"🎖️ Ранг: {rank['emoji']} {rank['name']}\n"
        f"🎮 Осталось попыток сегодня: {5 - stats['today_plays']}",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )
    
async def handle_quiz_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    first_name = query.from_user.first_name
    
    q = context.user_data.get('quiz_question')
    if not q:
        # Если вопроса нет — просто игнорируем, ничего не меняем
        return
    
    # === ЕСЛИ ЧУЖОЙ НАЖАЛ ===
    if q.get("user_id") != user_id:
        await context.bot.send_message(
            chat_id=user_id,
            text="⛔ Аттатата! Это не твой квиз, проказник! 😡"
        )
        return  # Выходим, НЕ ТРОГАЕМ СООБЩЕНИЕ
    
    # === ДАЛЬШЕ ДЛЯ ВЛАДЕЛЬЦА ===
    selected = int(query.data.split("_")[-1])
    correct = q["correct_option_id"]
    reward = q.get("reward", 1)
    rarity = q.get("rarity", "common")
    question_id = q.get("question_id")
    
    stats = get_user_stats(user_id)
    old_rank = get_rank(stats["score"])
    
    if selected == correct:
        stats["score"] += reward
        new_rank = get_rank(stats["score"])
        update_user_stats(user_id, stats["score"], stats["today_plays"], datetime.now().date().isoformat())
        mark_question_as_played(user_id, question_id)
        
        rank_up_msg = ""
        if new_rank["min_score"] > old_rank["min_score"]:
            rank_up_msg = f"\n\n🎉 **ПОВЫШЕНИЕ РАНГА!**\n{old_rank['emoji']} {old_rank['name']} → {new_rank['emoji']} {new_rank['name']}"
        
        await query.edit_message_text(
            f"✅ *Правильно!* +{reward} баллов {RARITY_EMOJI_ONLY.get(rarity, '')}{rank_up_msg}\n\n"
            f"🏆 Баллы: {stats['score']}\n"
            f"🎖️ Ранг: {new_rank['emoji']} {new_rank['name']}",
            parse_mode="Markdown"
        )
    else:
        stats["score"] -= 1
        update_user_stats(user_id, stats["score"], stats["today_plays"], datetime.now().date().isoformat())
        mark_question_as_played(user_id, question_id)
        
        correct_answer = q["options"][correct]
        await query.edit_message_text(
            f"❌ *Неправильно!* –1 балл\n\n"
            f"Правильный ответ: *{correct_answer}*\n\n"
            f"🏆 Баллы: {stats['score']}\n"
            f"🎖️ Ранг: {old_rank['emoji']} {old_rank['name']}",
            parse_mode="Markdown"
        )
    
    del context.user_data['quiz_question']
# ===== СТАТИСТИКА =====
@antispam_decorator
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    stats_data = get_user_stats(user_id)
    rank = get_rank(stats_data["score"])
    today = datetime.now().date().isoformat()

    if stats_data["last_play_date"] != today:
        remaining = 5
    else:
        remaining = 5 - stats_data["today_plays"]

    rarity_counts = count_quizzes_by_rarity()
    rarity_names = {"common": "Глорповский", "uncommon": "Пустотный", "rare": "Организационный", "epic": "От Междумирца", "legendary": "Прямиком из Тюрьмы времени"}
    rarity_text = "\n".join([f"{RARITY_EMOJI_ONLY.get(r, '')} {rarity_names.get(r, r)}: {rarity_counts.get(r, 0)}" for r in ["common", "uncommon", "rare", "epic", "legendary"]])

    photo = None
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        if photos.total_count > 0:
            photo = photos.photos[0][-1].file_id
    except:
        pass

    text = (
        f"📊 *Статистика {user.first_name}*\n\n"
        f"🏆 Баллы: {stats_data['score']}\n"
        f"🎖️ Ранг: {rank['emoji']} {rank['name']}\n"
        f"🎮 Осталось попыток сегодня: {remaining}/5\n\n"
        f"📚 *Вопросы в базе:*\n{rarity_text}"
    )

    if photo:
        await update.message.reply_photo(photo=photo, caption=text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")

@antispam_decorator
async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''
        SELECT qs.user_id, u.first_name, qs.score
        FROM quiz_stats qs
        LEFT JOIN users u ON qs.user_id = u.user_id
        ORDER BY qs.score DESC LIMIT 10
    ''')
    top_users = c.fetchall()
    conn.close()

    if not top_users:
        await update.message.reply_text("❌ Пока никого нет в рейтинге")
        return

    message = "🏆 *Топ-10 игроков:*\n\n"
    for i, (user_id, name, score) in enumerate(top_users, 1):
        # Если имя пустое или "Неизвестный" — пробуем получить через Telegram API
        if not name or name == "Неизвестный":
            try:
                chat = await context.bot.get_chat(user_id)
                name = chat.first_name or chat.username or "Неизвестный"
                # Обновляем в базе
                conn = sqlite3.connect(USERS_DB)
                c2 = conn.cursor()
                c2.execute('UPDATE users SET first_name = ? WHERE user_id = ?', (name, user_id))
                conn.commit()
                conn.close()
            except:
                name = "Неизвестный"

        rank = get_rank(score)
        message += f"{i}. *{name}* — {score} баллов ({rank['emoji']} {rank['name']})\n"

    await update.message.reply_text(message, parse_mode="Markdown")
# ===== МЕМЫ =====
@antispam_decorator
async def mm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memes = load_memes()
    if not memes:
        await update.message.reply_text("❌ Мемов пока нет")
        return

    m = random.choice(memes)
    if 'img_url' in m and m['img_url']:
        await update.message.reply_photo(photo=m['img_url'], caption=f"😂 *Мем от {m['date']}*", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"😂 *Мем от {m['date']}*\n\n👉 [Смотреть мем]({m['link']})", parse_mode="Markdown", disable_web_page_preview=True)

# ===== РЕБУСЫ =====
async def rebus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dictionary = load_dictionary("words.txt")
    if not dictionary:
        await update.message.reply_text("❌ База слов пуста")
        return

    candidates = [w for w in dictionary if 3 <= len(w) <= 6]
    if not candidates:
        candidates = list(dictionary)

    random.shuffle(candidates)

    for target_word in candidates[:30]:
        variants = split_into_parts(target_word, dictionary, max_parts=2)
        if not variants:
            continue

        variant = variants[0]
        expression = variant["expression"]
        blocks_data = expression_to_blocks(expression)

        missing = False
        for block in blocks_data:
            if find_image_case_insensitive(block["word"]) is None:
                missing = True
                break
        if missing:
            continue

        try:
            img = draw_rebus_from_blocks(
                blocks_data,
                images_dir="images",
                font_path="fonts/minecraft.ttf",
                frame_text="ТРЯСЛО993",
                frame_padding=30,
                letter_spacing_h=5,
                letter_spacing_v=7
            )

            if img:
                bio = BytesIO()
                img.save(bio, format='PNG')
                bio.seek(0)

                sent_message = await update.message.reply_photo(
                    photo=bio,
                    caption=f"🧩 *Отгадай слово ({len(target_word)} букв)*\n\nПодсказка: первая буква — «{target_word[0]}»",
                    parse_mode="Markdown"
                )

                active_rebuses[update.effective_user.id] = {
                    "word": target_word,
                    "message_id": sent_message.message_id,
                    "chat_id": update.message.chat_id
                }
                return
        except Exception as e:
            print(f"Ошибка при {target_word}: {e}")
            continue

    await update.message.reply_text(
        "❌ *Не удалось собрать ребус*\n\nПопробуй позже.",
        parse_mode="Markdown"
    )

async def rebus_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''SELECT user_name, solves FROM rebus_solves ORDER BY solves DESC LIMIT 10''')
    top = c.fetchall()
    conn.close()

    if not top:
        await update.message.reply_text("❌ Пока никто не отгадал ни одного ребуса")
        return

    message = "🏆 *Топ ребусников:*\n\n"
    for i, (name, solves) in enumerate(top, 1):
        word = "ребус" if solves == 1 else "ребусов"
        message += f"{i}. *{name}* — {solves} {word}\n"

    await update.message.reply_text(message, parse_mode="Markdown")

async def check_rebus_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    answer = update.message.text.strip().lower()

    active = active_rebuses.get(user_id)
    if not active:
        return

    if answer == active["word"].lower():
        user_name = update.effective_user.first_name
        add_rebus_solve(user_id, user_name)

        await update.message.reply_text(
            f"✅ *{user_name}*, правильно! +1 очко!\n🎉 Загаданное слово: *{active['word']}*",
            parse_mode="Markdown"
        )
        del active_rebuses[user_id]
    else:
        await update.message.reply_text(
            "❌ Неправильно. Попробуй ещё раз или напиши /rebus для нового ребуса.",
            parse_mode="Markdown"
        )

# ===== АДМИН-КОМАНДЫ =====
@antispam_decorator
async def editstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📝 *Использование:* `/editstats <user_id> количество`\n"
            "Пример: `/editstats 123456789 15`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(context.args[0])
        new_score = int(context.args[1])
    except:
        await update.message.reply_text("❌ Оба аргумента должны быть числами")
        return

    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()

    c.execute('''
        INSERT INTO quiz_stats (user_id, score, today_plays, last_play_date)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            score = excluded.score,
            today_plays = 0,
            last_play_date = excluded.last_play_date
    ''', (target_user_id, new_score, datetime.now().date().isoformat()))

    c.execute('''
        INSERT INTO users (user_id, first_name, total, rank)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            first_name = excluded.first_name,
            total = excluded.total,
            rank = excluded.rank
    ''', (target_user_id, "Неизвестный", new_score, get_rank(new_score)["name"]))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ *Статистика обновлена:*\n\n"
        f"🆔 *ID:* {target_user_id}\n"
        f"🏆 *Баллы:* {new_score}\n"
        f"🎖️ *Ранг:* {get_rank(new_score)['emoji']} {get_rank(new_score)['name']}",
        parse_mode="Markdown"
    )

@antispam_decorator
async def edittop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''
        SELECT qs.user_id, u.first_name, qs.score
        FROM quiz_stats qs
        LEFT JOIN users u ON qs.user_id = u.user_id
        ORDER BY qs.score DESC LIMIT 10
    ''')
    top_users = c.fetchall()
    conn.close()

    if not top_users:
        await update.message.reply_text("❌ Топ пуст")
        return

    message = "🏆 *Топ-10 игроков (для админа):*\n\n"
    for user_id, name, score in top_users:
        name = name or "Неизвестный"
        rank = get_rank(score)
        message += f"🆔 `{user_id}` — *{name}* — {score} баллов ({rank['emoji']} {rank['name']})\n"

    message += "\n📝 *Изменить статистику:* `/editstats <user_id> количество`"
    await update.message.reply_text(message, parse_mode="Markdown")

@antispam_decorator
async def base_quiz_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return
    
    context.user_data['step'] = 'waiting_for_base_quiz'
    await update.message.reply_text(
        "📝 *Отправь викторины в формате:*\n\n"
        "`Вопрос 1 (А; Б*; В; Г)`\n"
        "`Вопрос 2 (А*; Б; В; Г)`\n"
        "`Вопрос 3 (А; Б; В*; Г)`\n\n"
        "Где * — правильный ответ.\n"
        "Каждая викторина с новой строки.\n\n"
        "📎 *Или отправь текстовый файл (.txt) с таким же содержимым.*",
        parse_mode=None
    )

@antispam_decorator
async def backup_base(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    if not os.path.exists(BASE_QUIZZES_DB):
        await update.message.reply_text("❌ База вопросов не найдена")
        return

    with open(BASE_QUIZZES_DB, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"base_quizzes_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
            caption="📦 Бэкап базы вопросов"
        )

@antispam_decorator
async def backup_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute('''
        SELECT qs.user_id, u.first_name, qs.score, qs.today_plays, qs.last_play_date
        FROM quiz_stats qs
        LEFT JOIN users u ON qs.user_id = u.user_id
        ORDER BY qs.score DESC
    ''')
    data = c.fetchall()
    conn.close()

    if not data:
        await update.message.reply_text("❌ Нет данных для бэкапа")
        return

    backup_data = []
    for row in data:
        backup_data.append({
            "user_id": row[0],
            "first_name": row[1] or "Неизвестный",
            "score": row[2],
            "today_plays": row[3],
            "last_play_date": row[4]
        })

    with open("top_backup.json", "w", encoding="utf-8") as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)

    with open("top_backup.json", "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"top_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            caption="📦 Бэкап топа викторин"
        )

    os.remove("top_backup.json")

@antispam_decorator
async def reset_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("DELETE FROM users")
    c.execute("DELETE FROM quiz_stats")
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ Топ и статистика полностью сброшены!")

# ===== ОБРАБОТЧИКИ ТЕКСТА И ДОКУМЕНТОВ =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
     # === RP-ПРОВЕРКА (ПЕРВОЙ) ===
    text = update.message.text.strip().lower()
    user_name = update.effective_user.first_name

    for trigger, responses in RP_TRIGGERS.items():
        if trigger in text:
            reply = random.choice(responses).replace("{user}", user_name)
            await update.message.reply_text(reply)
            return
    # --- Сначала проверяем, не ответ ли на ребус ---
    user_id = update.effective_user.id
    if user_id in active_rebuses:
        await check_rebus_answer(update, context)
        return

    # --- Потом проверяем, не ждём ли мы basequiz ---
    if step == 'waiting_for_base_quiz':
        text = update.message.text
        lines = text.strip().split('\n')
        added = 0
        errors = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            parsed = parse_quiz_line(line)
            if parsed:
                question, options, correct_option_id = parsed
                rarity = add_base_quiz(question, '|||'.join(options), correct_option_id)
                added += 1
            else:
                errors.append(f"❌ `{line[:40]}...`")

        result = f"✅ *Добавлено викторин: {added}*"
        if errors:
            result += f"\n\n⚠️ *Не удалось распарсить:*\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                result += f"\n... и ещё {len(errors) - 5} ошибок"

        await update.message.reply_text(result, parse_mode=None)
        context.user_data['step'] = None
        return

    # Если ничего не ждём
    await update.message.reply_text(
        "❓ Я не понял.\n\n"
        "Команды:\n"
        "/quiz — викторина\n"
        "/rebus — ребус\n"
        "/mm — мем\n"
        "/stats — статистика\n"
        "/top — топ\n"
        "/rebustop — топ ребусников\n"
        "/help — помощь"
    )

async def check_rebus_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    answer = update.message.text.strip().lower()

    active = active_rebuses.get(user_id)
    if not active:
        return  # нет активного ребуса — просто игнорируем

    if answer == active["word"].lower():
        user_name = update.effective_user.first_name
        add_rebus_solve(user_id, user_name)

        await update.message.reply_text(
            f"✅ *{user_name}*, правильно! +1 очко!\n🎉 Загаданное слово: *{active['word']}*",
            parse_mode="Markdown"
        )
        del active_rebuses[user_id]
    else:
        await update.message.reply_text(
            "❌ Неправильно. Попробуй ещё раз или напиши /rebus для нового ребуса.",
            parse_mode="Markdown"
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Если бот не ждёт файл для basequiz — просто игнорируем
    if context.user_data.get('step') != 'waiting_for_base_quiz':
        return  # ← ПРОСТО МОЛЧИМ
    
    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Отправь текстовый файл (.txt)")
        return
    
    await update.message.reply_text("📥 Загружаю файл...")
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"temp_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        await file.download_to_drive(file_path)
        
        with open(file_path, 'r', encoding='utf-8') as f:
            text = f.read()
        
        os.remove(file_path)
        
        lines = text.strip().split('\n')
        added = 0
        errors = []
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            parsed = parse_quiz_line(line)
            if parsed:
                question, options, correct_option_id = parsed
                rarity = add_base_quiz(question, '|||'.join(options), correct_option_id)
                added += 1
            else:
                errors.append(f"❌ `{line[:40]}...`")
        
        result = f"✅ *Добавлено викторин из файла: {added}*"
        if errors:
            result += f"\n\n⚠️ *Не удалось распарсить:*\n" + "\n".join(errors[:5])
            if len(errors) > 5:
                result += f"\n... и ещё {len(errors) - 5} ошибок"
        
        await update.message.reply_text(result, parse_mode=None)
        context.user_data['step'] = None
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")
@antispam_decorator
async def restore_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "❌ *Как восстановить топ:*\n\n"
            "1. Отправь файл `top_backup_*.json`\n"
            "2. Нажми на него → 'Ответить'\n"
            "3. Напиши `/restore_top`\n\n"
            "📌 Команда должна быть ответом на сообщение с файлом!",

        )
        return

    document = reply.document
    if not document.file_name.endswith('.json'):
        await update.message.reply_text("❌ Файл должен быть в формате `.json`")
        return

    await update.message.reply_text("📥 Загружаю файл...")

    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"restore_top_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await file.download_to_drive(file_path)

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not data or not isinstance(data, list):
            await update.message.reply_text("❌ Файл повреждён или не содержит данных")
            os.remove(file_path)
            return

        conn = sqlite3.connect(USERS_DB)
        c = conn.cursor()

        # Очищаем старые данные перед восстановлением
        c.execute("DELETE FROM quiz_stats")
        c.execute("DELETE FROM users")

        restored = 0
        for item in data:
            user_id = item.get("user_id")
            first_name = item.get("first_name", "Неизвестный")
            score = item.get("score", 0)
            today_plays = item.get("today_plays", 0)
            last_play_date = item.get("last_play_date", datetime.now().date().isoformat())

            if not user_id:
                continue

            # Восстанавливаем в quiz_stats
            c.execute('''
                INSERT INTO quiz_stats (user_id, score, today_plays, last_play_date)
                VALUES (?, ?, ?, ?)
            ''', (user_id, score, today_plays, last_play_date))

            # Восстанавливаем в users
            rank = get_rank(score)
            c.execute('''
                INSERT INTO users (user_id, first_name, total, rank)
                VALUES (?, ?, ?, ?)
            ''', (user_id, first_name, score, rank["name"]))

            restored += 1

        conn.commit()
        conn.close()

        os.remove(file_path)

        await update.message.reply_text(
            f"✅ *Топ восстановлен!*\n\n"
            f"📊 Восстановлено записей: {restored}\n"
            f"📁 Файл: {document.file_name}\n\n"
            f"Теперь можно проверить через `/top`",

        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка восстановления: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)

@antispam_decorator
async def update_names(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    await update.message.reply_text("🔄 Обновляю имена пользователей...")

    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()

    # Получаем всех пользователей из quiz_stats
    c.execute("SELECT user_id FROM quiz_stats")
    users = c.fetchall()

    updated = 0
    for (user_id,) in users:
        try:
            chat = await context.bot.get_chat(user_id)
            first_name = chat.first_name or "Неизвестный"

            c.execute('''
                UPDATE users SET first_name = ? WHERE user_id = ?
            ''', (first_name, user_id))

            if c.rowcount == 0:
                # Если пользователя нет в users — создаём
                c.execute('''
                    INSERT INTO users (user_id, first_name, total, rank)
                    SELECT ?, ?, score, rank FROM quiz_stats WHERE user_id = ?
                ''', (user_id, first_name, user_id))

            updated += 1
            print(f"✅ Обновлён: {first_name} (ID: {user_id})")

        except Exception as e:
            print(f"❌ Не удалось обновить {user_id}: {e}")

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ *Обновлено имён: {updated}*\n\n"
        f"Теперь проверь `/top`",
        parse_mode="Markdown"
    )

@antispam_decorator
async def backup_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    if not os.path.exists(BASE_QUIZZES_DB):
        await update.message.reply_text("❌ База вопросов не найдена")
        return

    with open(BASE_QUIZZES_DB, 'rb') as f:
        await update.message.reply_document(
            document=f,
            filename=f"quizzes_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
            caption="📦 Бэкап базы вопросов (с редкостями)"
        )

@antispam_decorator
async def restore_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return

    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "❌ Как восстановить базу вопросов:\n\n"
            "1. Отправь файл quizzes_backup_*.db\n"
            "2. Нажми на него → 'Ответить'\n"
            "3. Напиши /restore_quizzes\n\n"
            "Команда должна быть ответом на сообщение с файлом!"
        )
        return

    document = reply.document
    if not document.file_name.endswith('.db'):
        await update.message.reply_text("❌ Файл должен быть в формате .db")
        return

    await update.message.reply_text("📥 Загружаю файл...")

    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"restore_quizzes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        await file.download_to_drive(file_path)

        # Проверяем, что файл — это SQLite база с таблицей base_quizzes
        try:
            conn_check = sqlite3.connect(file_path)
            c_check = conn_check.cursor()
            c_check.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='base_quizzes'")
            if not c_check.fetchone():
                await update.message.reply_text("❌ Файл не содержит таблицу base_quizzes")
                os.remove(file_path)
                conn_check.close()
                return
            conn_check.close()
        except:
            await update.message.reply_text("❌ Файл повреждён или это не SQLite база")
            os.remove(file_path)
            return

        # Заменяем текущую базу
        shutil.copy2(file_path, BASE_QUIZZES_DB)

        # Проверяем, сколько записей загружено
        conn = sqlite3.connect(BASE_QUIZZES_DB)
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM base_quizzes')
        count = c.fetchone()[0]

        # Считаем по редкостям
        c.execute('SELECT rarity, COUNT(*) FROM base_quizzes GROUP BY rarity')
        rarity_stats = dict(c.fetchall())
        conn.close()

        os.remove(file_path)

        rarity_text = "\n".join([f"  {RARITY_EMOJIS.get(r, r)}: {count}" for r, count in rarity_stats.items()])

        await update.message.reply_text(
            f"✅ База вопросов восстановлена!\n\n"
            f"📊 Загружено вопросов: {count}\n"
            f"📁 Файл: {document.file_name}\n\n"
            f"📚 Распределение по редкостям:\n{rarity_text}\n\n"
            f"Теперь можно играть через /quiz"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка восстановления: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)

@antispam_decorator
async def editrebus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text(
            "📝 *Использование:* `/editrebus <user_id> количество`\n"
            "Пример: `/editrebus 123456789 15`\n\n"
            "⚠️ Меняет количество решённых ребусов у пользователя.",
            parse_mode="Markdown"
        )
        return
    
    try:
        target_user_id = int(context.args[0])
        new_solves = int(context.args[1])
    except:
        await update.message.reply_text("❌ Оба аргумента должны быть числами")
        return
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    
    # Проверяем, есть ли пользователь в таблице rebus_solves
    c.execute('SELECT user_name FROM rebus_solves WHERE user_id = ?', (target_user_id,))
    row = c.fetchone()
    
    if row:
        user_name = row[0]
        c.execute('UPDATE rebus_solves SET solves = ? WHERE user_id = ?', (new_solves, target_user_id))
        await update.message.reply_text(f"🔄 Обновлён пользователь {user_name} (ID: {target_user_id}) → {new_solves} ребусов")
    else:
        # Если пользователя нет — создаём
        await update.message.reply_text(
            f"❌ Пользователь с ID {target_user_id} не найден в топе ребусов.\n\n"
            f"Сначала он должен отгадать хотя бы один ребус через /rebus,\n"
            f"или укажи имя вручную:\n"
            f"`/editrebus_name {target_user_id} Имя {new_solves}`",
            parse_mode="Markdown"
        )
        conn.close()
        return
    
    conn.commit()
    conn.close()
    
    await update.message.reply_text(
        f"✅ *Статистика ребусов обновлена:*\n\n"
        f"🆔 *ID:* {target_user_id}\n"
        f"🧩 *Решено ребусов:* {new_solves}",
        parse_mode="Markdown"
    )

@antispam_decorator
async def backup_rebus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return
    
    conn = sqlite3.connect(USERS_DB)
    c = conn.cursor()
    c.execute("SELECT user_id, user_name, solves FROM rebus_solves ORDER BY solves DESC")
    data = c.fetchall()
    conn.close()
    
    if not data:
        await update.message.reply_text("❌ Нет данных о ребусах")
        return
    
    backup_data = [{"user_id": row[0], "user_name": row[1], "solves": row[2]} for row in data]
    
    with open("rebus_backup.json", "w", encoding="utf-8") as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)
    
    with open("rebus_backup.json", "rb") as f:
        await update.message.reply_document(
            document=f,
            filename=f"rebus_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
            caption="📦 Бэкап топа ребусников"
        )
    
    os.remove("rebus_backup.json")

@antispam_decorator
async def restore_rebus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет прав")
        return
    
    reply = update.message.reply_to_message
    if not reply or not reply.document:
        await update.message.reply_text(
            "❌ Как восстановить топ ребусов:\n\n"
            "1. Отправь файл rebus_backup_*.json\n"
            "2. Нажми на него -> 'Ответить'\n"
            "3. Напиши /restore_rebus\n\n"
            "Команда должна быть ответом на сообщение с файлом!"
        )
        return
    
    document = reply.document
    if not document.file_name.endswith('.json'):
        await update.message.reply_text("❌ Файл должен быть в формате .json")
        return
    
    await update.message.reply_text("📥 Загружаю файл...")
    
    try:
        file = await context.bot.get_file(document.file_id)
        file_path = f"restore_rebus_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        await file.download_to_drive(file_path)
        
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        if not data or not isinstance(data, list):
            await update.message.reply_text("❌ Файл повреждён или не содержит данных")
            os.remove(file_path)
            return
        
        conn = sqlite3.connect(USERS_DB)
        c = conn.cursor()
        
        # Очищаем старые данные
        c.execute("DELETE FROM rebus_solves")
        
        restored = 0
        for item in data:
            user_id = item.get("user_id")
            user_name = item.get("user_name", "Неизвестный")
            solves = item.get("solves", 0)
            
            if not user_id:
                continue
            
            c.execute('''
                INSERT INTO rebus_solves (user_id, user_name, solves)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    user_name = excluded.user_name,
                    solves = excluded.solves
            ''', (user_id, user_name, solves))
            
            restored += 1
        
        conn.commit()
        conn.close()
        os.remove(file_path)
        
        await update.message.reply_text(
            f"✅ Топ ребусов восстановлен!\n\n"
            f"Восстановлено записей: {restored}\n"
            f"Файл: {document.file_name}\n\n"
            f"Теперь можно проверить через /rebustop"
        )
        
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка восстановления: {e}")
        if os.path.exists(file_path):
            os.remove(file_path)

async def rp_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("🔥 rp_command ВЫЗВАНА")  # ← ДИАГНОСТИКА
    print(f"📦 RP_TRIGGERS: {RP_TRIGGERS}")  # ← ЧТО В СЛОВАРЕ
    
    if not context.args:
        await update.message.reply_text("📝 /rp текст")
        return
    
    full_text = " ".join(context.args).lower()
    print(f"📩 Текст: {full_text}")  # ← ЧТО ПРИШЛО
    
    user_name = update.effective_user.first_name
    target_name = None
    
    import re
    mention_match = re.search(r'@(\w+)', full_text)
    if mention_match:
        target_username = mention_match.group(1)
        try:
            conn = sqlite3.connect(USERS_DB)
            c = conn.cursor()
            c.execute('SELECT first_name FROM users WHERE username LIKE ?', (f'%{target_username}%',))
            row = c.fetchone()
            conn.close()
            if row:
                target_name = row[0]
            else:
                target_name = f"@{target_username}"
        except:
            target_name = f"@{target_username}"
    
    if not target_name and update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_name = target_user.first_name or target_user.username or "Кто-то"
    
    clean_text = re.sub(r'@\w+', '', full_text).strip()
    if not clean_text:
        clean_text = full_text
    
    print(f"🔎 Чистый текст: {clean_text}")  # ← ЧТО ИЩЕМ
    
    for trigger, data in RP_TRIGGERS.items():
        print(f"🔍 Проверяю: '{trigger}' в '{clean_text}'")  # ← ПОИСК
        if trigger in clean_text:
            print(f"✅ Найдено: {trigger}")  # ← ЕСЛИ НАШЛО
            rp_type = data.get("type", "self")
            responses = data["responses"]
            
            if rp_type == "self":
                reply = random.choice(responses).replace("{user}", user_name)
                reply = reply.replace("{target}", "никого")
                await update.message.reply_text(reply)
                return
            
            if rp_type == "target":
                if not target_name:
                    await update.message.reply_text(
                        "❌ Для этого действия нужен второй пользователь.\n"
                        "Укажи @username или ответь на сообщение."
                    )
                    return
                reply = random.choice(responses).replace("{user}", user_name).replace("{target}", target_name)
                await update.message.reply_text(reply)
                return
    
    print("❌ Ничего не найдено")  # ← ЕСЛИ НЕ НАШЛО
    await update.message.reply_text("❌ Не нашёл такой RP-фразы")

@antispam_decorator
async def rplist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not RP_TRIGGERS:
        await update.message.reply_text("📭 Список RP-команд пуст.")
        return
    
    # Формируем список всех команд с типами
    all_commands = []
    for trigger, data in RP_TRIGGERS.items():
        rp_type = data.get("type", "self")
        label = "👤" if rp_type == "self" else "👥"
        all_commands.append(f"{label} `{trigger}`")
    
    # Сохраняем в context для пагинации
    context.user_data['rp_list'] = all_commands
    
    page_size = 10
    total_pages = (len(all_commands) + page_size - 1) // page_size
    page = 0
    
    start = page * page_size
    end = min(start + page_size, len(all_commands))
    
    message = f"📋 *RP-команды (👤 - личная команда, 👥 - интерактивная команда)  (стр. {page + 1}/{total_pages})*\n\n"
    message += "\n".join(all_commands[start:end])
    message += "\n\n📝 `/rp текст` — использовать команду"
    
    keyboard = []
    nav_row = []
    if total_pages > 1:
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"rplist_{page - 1}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"rplist_{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await update.message.reply_text(message, parse_mode="Markdown", reply_markup=reply_markup)

async def rplist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    page = int(query.data.split("_")[1])
    
    # Берём список из context
    all_commands = context.user_data.get('rp_list', [])
    if not all_commands:
        # Если список потерялся — пересобираем
        for trigger, data in RP_TRIGGERS.items():
            rp_type = data.get("type", "self")
            label = "👤" if rp_type == "self" else "👥"
            all_commands.append(f"{label} `{trigger}`")
        context.user_data['rp_list'] = all_commands
    
    page_size = 10
    total_pages = (len(all_commands) + page_size - 1) // page_size
    
    if page < 0:
        page = 0
    if page >= total_pages:
        page = total_pages - 1
    
    start = page * page_size
    end = min(start + page_size, len(all_commands))
    
    message = f"📋 *RP-команды (👤 - личная команда, 👥 - интерактивная команда) (стр. {page + 1}/{total_pages})*\n\n"
    message += "\n".join(all_commands[start:end])
    message += "\n\n📝 `/rp текст` — использовать команду"
    
    keyboard = []
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"rplist_{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"rplist_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)
    
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    
    await query.edit_message_text(message, parse_mode="Markdown", reply_markup=reply_markup)

# ===== ЗАПУСК =====
if __name__ == "__main__":
    init_user_db()
    init_base_quizzes_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("donate", donate))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CallbackQueryHandler(handle_quiz_answer, pattern="quiz_ans_"))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("mm", mm))
    app.add_handler(CommandHandler("rebus", rebus))
    app.add_handler(CommandHandler("rebustop", rebus_top))
    app.add_handler(CommandHandler("editstats", editstats))
    app.add_handler(CommandHandler("edittop", edittop))
    app.add_handler(CommandHandler("basequiz", base_quiz_command))
    app.add_handler(CommandHandler("backup_base", backup_base))
    app.add_handler(CommandHandler("backup_top", backup_top))
    app.add_handler(CommandHandler("reset_top", reset_top))
    # app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_rebus_answer))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CommandHandler("restore_top", restore_top))
    app.add_handler(CommandHandler("update_names", update_names))
    app.add_handler(CommandHandler("backup_quizzes", backup_quizzes))
    app.add_handler(CommandHandler("restore_quizzes", restore_quizzes))
    app.add_handler(CommandHandler("editrebus", editrebus))
    app.add_handler(CommandHandler("backup_rebus", backup_rebus))
    app.add_handler(CommandHandler("restore_rebus", restore_rebus))
    app.add_handler(CommandHandler("rp", rp_command))
    app.add_handler(CommandHandler("rplist", rplist))
    app.add_handler(CallbackQueryHandler(rplist_callback, pattern="rplist_"))

    print("✅ Бот запущен!")
    app.run_polling()
