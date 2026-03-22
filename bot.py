import os
import asyncio
import re
import datetime
import pytz
import aiosqlite
import aiohttp
import pandas as pd
from bs4 import BeautifulSoup
from aiogram import BaseMiddleware
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# 1. КОНФИГУРАЦИЯ И КОНСТАНТЫ
# ==========================================
API_TOKEN = os.getenv('BOT_TOKEN')
if os.path.exists('/data'):
    DB_NAME = '/data/schedule.db'
else:
    DB_NAME = 'schedule.db'
ADMIN_ID = 8529027886
BASE_URL = "https://akademiks.urtt.ru/lk/all-schedules/student/"
CONSULTATION_URL = "https://docs.google.com/spreadsheets/d/1xKpaWPKBcv-emcRpAfYRCK767D8dLiYnJjnzvOgBies/htmlview"
TZ_EKB = pytz.timezone('Asia/Yekaterinburg')

ALL_GROUPS = [
    "bi-129", "bi-130", "bi-227", "bi-228", "bi-325", "bi-326", "bi-423", "bi-424",
    "d-126", "d-127", "d-224", "d-225", "d-322", "d-323", "d-420", "d-421",
    "is-131", "is-132", "is-133", "is-134", "is-227", "is-228", "is-229", "is-230",
    "is-323", "is-324", "is-325", "is-326", "is-416", "is-417", "is-418", "is-419", "is-421",
    "l-119", "l-218", "l-220", "l-316", "l-317",
    "oi-105", "oi-106", "oi-203", "oi-204",
    "r-453", "r-454",
    "pm-108", "pm-109", "pm-110", "pm-111", "pm-204", "pm-205", "pm-206", "pm-207",
    "pm-303", "pm-402", "pm-501",
    "pt-472", "pt-473",
    "re-106", "re-107", "re-204", "re-205", "re-301", "re-302", "re-303",
    "ca-115", "ca-116", "ca-117", "ca-212", "ca-213", "ca-214", "ca-309", "ca-310", "ca-311",
    "ca-405", "ca-406", "ca-407",
    "e-168", "e-169", "e-266", "e-267", "e-363", "e-364", "e-365", "e-461", "e-462"
]

# Кастомные словари для точного перевода аббревиатур (чтобы избегать ошибок транслитераторов)
PREFIX_RU_TO_EN = {'ис':'is', 'пр':'pr', 'рэ':'re', 'пм':'pm', 'пт':'pt', 'са':'ca', 'ои':'oi', 'би':'bi', 'э':'e', 'д':'d', 'л':'l', 'р':'r'}
PREFIX_EN_TO_RU = {v: k for k, v in PREFIX_RU_TO_EN.items()}

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

class UserState(StatesGroup):
    choosing_group = State()
    waiting_for_cabinet = State()
    waiting_for_teacher = State()

# ==========================================
# 2. УМНАЯ НОРМАЛИЗАЦИЯ ДАННЫХ
# ==========================================
def normalize_group(user_input):
    """Очищает мусор и возвращает кортеж: (англ_для_БД, ру_для_текста)"""
    clean = re.sub(r'[\s\-]', '', user_input.lower())
    match = re.match(r'([а-яёa-z]+)(\d{3})', clean)
    if not match: return None, None
    
    letters, nums = match.groups()
    is_ru = bool(re.search('[а-яё]', letters))
    
    en_let = PREFIX_RU_TO_EN.get(letters, letters) if is_ru else letters
    ru_let = PREFIX_EN_TO_RU.get(letters, letters) if not is_ru else letters
        
    en_group = f"{en_let}-{nums}"
    if en_group not in ALL_GROUPS: 
        return None, None
        
    ru_group = f"{ru_let.upper()}-{nums}"
    return en_group, ru_group

# ==========================================
# 3. БАЗА ДАННЫХ
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users 
                            (telegram_id INTEGER PRIMARY KEY, group_name TEXT, notifications INTEGER DEFAULT 1)''')
        
        # Пытаемся добавить новую колонку. Если она уже есть, просто игнорируем ошибку.
        try:
            await db.execute('ALTER TABLE users ADD COLUMN last_active_date TEXT')
        except aiosqlite.OperationalError:
            pass # Колонка уже существует

        await db.execute('''CREATE TABLE IF NOT EXISTS schedule 
                            (group_name TEXT, day_name TEXT, lesson_num TEXT, time_str TEXT, subject TEXT, cabinet TEXT, teacher TEXT)''')
        # Индексы
        await db.execute('CREATE INDEX IF NOT EXISTS idx_group_day ON schedule(group_name, day_name)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_cabinet ON schedule(cabinet, day_name)')
        await db.commit()

# ==========================================
# 4. АСИНХРОННЫЙ ПАРСЕР (МЕГА-СКОРОСТЬ)
# ==========================================
def parse_html_to_lessons(html, group_name):
    """Извлекает пары из HTML кода"""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table')
    if not table: return []

    days_map = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота"]
    rows = table.find_all('tr')
    headers = [cell.get_text(strip=True).lower() for cell in rows[0].find_all(['th', 'td'])]
    
    lessons = []
    for day in days_map:
        col_idx = next((i for i, h in enumerate(headers) if day in h), -1)
        if col_idx == -1: continue
            
        for row in rows[1:]:
            cols = row.find_all(['td', 'th'])
            if len(cols) > col_idx:
                raw_text = cols[col_idx].get_text(strip=True)
                if not raw_text or "нет пары" in raw_text.lower() or raw_text == "-": continue
                
                # Парсинг строки
                match_time = re.search(r'(\d{2}:\d{2}\s*-\s*\d{2}:\d{2})', raw_text)
                time_str = match_time.group(1).strip() if match_time else "Неизвестно"
                subj_part = raw_text[:match_time.start()].strip() if match_time else raw_text.strip()
                after = raw_text[match_time.end():].strip() if match_time else ""
                
                cab, tchr = "Неизвестно", "Неизвестно"
                if re.search(r'дистант|дистанционно', after, re.IGNORECASE):
                    cab = "🌐 Дистант"
                elif "," in after:
                    cab, tchr = map(lambda x: x.strip(' ,.()'), after.split(",", 1))
                elif after:
                    cab = after.strip(' ,.()')
                    
                subj_part = re.sub(r'\(совм.*?\)', '', subj_part, flags=re.IGNORECASE).strip()
                num_str = cols[0].get_text(strip=True)
                
                lessons.append((group_name, day, num_str, time_str, subj_part, cab, tchr))
    return lessons

async def fetch_and_update_all():
    """Фоновая задача: параллельно качает все группы, ищет КОНКРЕТНЫЕ изменения и обновляет БД"""
    start_time = datetime.datetime.now(TZ_EKB)
    print(f"[{start_time.strftime('%H:%M:%S')}] 🔄 Начинаю параллельный парсинг всех групп...")
    
    all_new_lessons = []
    sem = asyncio.Semaphore(15) 
    
    async def fetch_group(session, group):
        async with sem:
            try:
                async with session.get(BASE_URL + group) as resp:
                    if resp.status == 200:
                        return parse_html_to_lessons(await resp.text(), group)
            except Exception: pass
        return []

    # Асинхронно скачиваем всё
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_group(session, g) for g in ALL_GROUPS]
        results = await asyncio.gather(*tasks)
        for res in results: all_new_lessons.extend(res)

    # Работаем с БД
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        
        # 1. Получаем старое расписание
        async with db.execute("SELECT * FROM schedule") as cursor:
            old_rows = await cursor.fetchall()
            
        # Группируем старые данные в словари для быстрого поиска конкретной пары
        # Формат: old_data['is-326'][('понедельник', '1')] = row
        old_data = {g: {} for g in ALL_GROUPS}
        for r in old_rows:
            old_data[r['group_name']][(r['day_name'], r['lesson_num'])] = r

        # Группируем новые данные аналогичным образом
        # Формат: new_data['is-326'][('понедельник', '1')] = кортеж_данных
        new_data = {g: {} for g in ALL_GROUPS}
        for r in all_new_lessons:
            # r[0] = group, r[1] = day, r[2] = lesson_num
            new_data[r[0]][(r[1], r[2])] = r

        changed_groups_count = 0
        
        # 2. Ищем ТОЧНЫЕ изменения для каждой группы
        for group in ALL_GROUPS:
            changes_text = []
            
            old_group_schedule = old_data[group]
            new_group_schedule = new_data[group]
            
            # А. Проверяем добавленные и измененные пары
            for key, new_lesson in new_group_schedule.items():
                day, num = key
                # Распаковываем кортеж: (group, day, num, time_str, subject, cabinet, teacher)
                _, _, _, new_time, new_subj, new_cab, new_tchr = new_lesson
                
                if key in old_group_schedule:
                    old_lesson = old_group_schedule[key]
                    diffs = []
                    
                    # Поштучно ищем отличия
                    if old_lesson['subject'] != new_subj:
                        diffs.append(f"📖 Предмет: {old_lesson['subject']} ➡️ *{new_subj}*")
                    if old_lesson['cabinet'] != new_cab:
                        diffs.append(f"🚪 Кабинет: {old_lesson['cabinet']} ➡️ *{new_cab}*")
                    if old_lesson['teacher'] != new_tchr:
                        diffs.append(f"👤 Препод: {old_lesson['teacher']} ➡️ *{new_tchr}*")
                    if old_lesson['time_str'] != new_time:
                        diffs.append(f"⏰ Время: {old_lesson['time_str']} ➡️ *{new_time}*")
                        
                    if diffs:
                        changes_text.append(f"📍 *{day.capitalize()}*, {num} пара:\n" + "\n".join(diffs))
                else:
                    # Ключа нет в старом расписании — добавили новую пару
                    changes_text.append(f"➕ *{day.capitalize()}*, {num} пара: Добавлена новая!\n📖 {new_subj} (Каб: {new_cab})")
            
            # Б. Проверяем удаленные (отмененные) пары
            for key, old_lesson in old_group_schedule.items():
                if key not in new_group_schedule:
                    day, num = key
                    changes_text.append(f"➖ *{day.capitalize()}*, {num} пара: Отменена!\n📖 Было: {old_lesson['subject']}")
            
            # Если для группы нашлись изменения:
            if changes_text or (set(old_group_schedule.keys()) != set(new_group_schedule.keys())):
                changed_groups_count += 1
                
                # Обновляем БД (удаляем старое, пишем полностью новое для этой группы)
                await db.execute("DELETE FROM schedule WHERE group_name = ?", (group,))
                records_to_insert = list(new_group_schedule.values())
                if records_to_insert:
                    await db.executemany(
                        "INSERT INTO schedule (group_name, day_name, lesson_num, time_str, subject, cabinet, teacher) VALUES (?, ?, ?, ?, ?, ?, ?)", 
                        records_to_insert
                    )
                
                # Рассылаем уведомления пользователям этой группы
                if old_rows and changes_text: # Не рассылаем, если это самый первый запуск (когда старой БД вообще не было)
                    current_time = datetime.datetime.now(TZ_EKB)
                    current_day = current_time.weekday()
                    current_hour = current_time.hour
                    if current_day == 6 or (current_day == 5 and current_hour >= 18):
                        continue
                    async with db.execute("SELECT telegram_id FROM users WHERE group_name = ? AND notifications = 1", (group,)) as cursor:
                        users = await cursor.fetchall()
                        for (uid,) in users:
                            try: 
                                _, ru_name = normalize_group(group)
                                msg_text = f"🚨 *Внимание!*\nВ расписании *{ru_name}* произошли изменения:\n\n" + "\n\n".join(changes_text)
                                
                                # Защита от ошибки Telegram (если изменений слишком много, режем текст)
                                if len(msg_text) > 4000:
                                    msg_text = msg_text[:4000] + "\n\n... (изменений слишком много)"
                                    
                                await bot.send_message(uid, msg_text, parse_mode="Markdown")
                            except Exception: pass

        await db.commit()

    seconds = (datetime.datetime.now(TZ_EKB) - start_time).total_seconds()
    print(f"✅ Парсинг завершен за {seconds:.1f} сек. Изменено групп: {changed_groups_count}")

async def fetch_consultations(teacher_query: str):
    """Ищет консультации по фамилии преподавателя (используя pandas для парсинга CSV)"""
    sheet_id = "1xKpaWPKBcv-emcRpAfYRCK767D8dLiYnJjnzvOgBies"
    export_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

    try:
        # Для асинхронности лучше запускать синхронный pandas в отдельном потоке, 
        # но для небольшого файла можно оставить и так.
        df = pd.read_csv(export_url, header=None)
    except Exception as e:
        return f"Ошибка при скачивании таблицы: {e}"

    days_of_week = df.iloc[0, 3:9].values
    dates = df.iloc[1, 3:9].values

    found_consultations = []
    teacher_query_lower = teacher_query.lower().strip()

    # Парсим всю таблицу
    for i in range(2, len(df) - 1, 2):
        row_time = df.iloc[i]
        row_room = df.iloc[i+1]
        
        teacher = row_time[1]
        
        if pd.isna(teacher):
            continue
            
        # Сразу проверяем, совпадает ли фамилия, чтобы не парсить лишнее
        if teacher_query_lower in str(teacher).lower():
            for col_idx in range(3, 9):
                time = row_time[col_idx]
                room = row_room[col_idx]
                
                if pd.notna(time):
                    found_consultations.append({
                        'teacher': str(teacher).strip(),
                        'day': f"{str(dates[col_idx-3]).strip()} ({str(days_of_week[col_idx-3]).strip()})",
                        'time': str(time).strip(),
                        'cabinet': str(room).strip() if pd.notna(room) else "Не указан"
                    })
                    
    return found_consultations

class ActivityMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.Update, data: dict):
        # Нас интересуют только входящие сообщения от пользователей
        if event.message and event.message.from_user:
            user_id = event.message.from_user.id
            # Берем текущую дату по Екатеринбургу (только дату, без времени)
            today_str = datetime.datetime.now(TZ_EKB).strftime("%Y-%m-%d")
            
            # Записываем активность в фоне, не блокируя работу бота
            async with aiosqlite.connect(DB_NAME) as db:
                # Обновляем дату только если юзер уже есть в базе (зареган)
                await db.execute(
                    "UPDATE users SET last_active_date = ? WHERE telegram_id = ?", 
                    (today_str, user_id)
                )
                await db.commit()
                
        # Передаем управление дальше, хендлерам
        return await handler(event, data)

# Регистрируем middleware
dp.update.middleware(ActivityMiddleware())
# ==========================================
# 5. ХЕНДЛЕРЫ И ЛОГИКА ТЕЛЕГРАМ
# ==========================================
async def get_main_keyboard(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT notifications FROM users WHERE telegram_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            notif_status = row[0] if row else 1

    b = ReplyKeyboardBuilder()
    b.button(text="📅 Сегодня")
    b.button(text="🗓 На неделю")
    b.button(text="🚪 Поиск по кабинету")
    b.button(text="🔕 Выключить уведомления" if notif_status else "🔔 Включить уведомления")
    b.button(text="⚙️ Сменить группу")
    b.button(text="🧑‍🏫 Консультации")
    b.adjust(2, 2, 1, 1)
    return b.as_markup(resize_keyboard=True)


@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT group_name FROM users WHERE telegram_id = ?", (message.from_user.id,)) as cursor:
            user = await cursor.fetchone()
            
    if user:
        _, ru_name = normalize_group(user[0])
        await message.answer(f"Привет! Твоя группа: *{ru_name}*", reply_markup=await get_main_keyboard(message.from_user.id), parse_mode="Markdown")
    else:
        await message.answer("Привет! Напиши свою группу (например: *ис-326*):", parse_mode="Markdown")
        await state.set_state(UserState.choosing_group)

# --- АДМИН-ПАНЕЛЬ (Онлайн) ---
@dp.message(Command("online"))
async def cmd_online(message: types.Message):
    if message.from_user.id != ADMIN_ID: # Не забудь убедиться, что переменная ADMIN_ID задана в начале кода!
        return 

    # Какая дата сегодня по Екб?
    today_str = datetime.datetime.now(TZ_EKB).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect(DB_NAME) as db:
        # Считаем, сколько юзеров имеют сегодняшнюю дату в колонке last_active_date
        async with db.execute("SELECT COUNT(*) FROM users WHERE last_active_date = ?", (today_str,)) as cursor:
            dau_count = (await cursor.fetchone())[0]

    await message.answer(f"📈 *Online (DAU)*\nСегодня ботом воспользовались: *{dau_count}* чел.", parse_mode="Markdown")

@dp.message(UserState.choosing_group)
async def process_group(message: types.Message, state: FSMContext):
    en_grp, ru_grp = normalize_group(message.text)
    if not en_grp:
        return await message.answer("⚠ Неверный формат или группы нет в базе. Пример: *ис-326*", parse_mode="Markdown")

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO users (telegram_id, group_name) VALUES (?, COALESCE((SELECT group_name FROM users WHERE telegram_id = ?), ?))", (message.from_user.id, message.from_user.id, en_grp))
        await db.execute("UPDATE users SET group_name = ? WHERE telegram_id = ?", (en_grp, message.from_user.id))
        await db.commit()
        
    await state.clear()
    await message.answer(f"✅ Группа *{ru_grp}* сохранена!", reply_markup=await get_main_keyboard(message.from_user.id), parse_mode="Markdown")

@dp.message(F.text == "⚙️ Сменить группу")
async def change_group(message: types.Message, state: FSMContext):
    await message.answer("Напиши новую группу:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(UserState.choosing_group)

@dp.message(F.text.in_({"🔔 Включить уведомления", "🔕 Выключить уведомления"}))
async def toggle_notif(message: types.Message):
    status = 1 if "Включить" in message.text else 0
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET notifications = ? WHERE telegram_id = ?", (status, message.from_user.id))
        await db.commit()
    await message.answer("Уведомления ВКЛЮЧЕНЫ ✅" if status else "Уведомления ВЫКЛЮЧЕНЫ 🔕", reply_markup=await get_main_keyboard(message.from_user.id))

# --- ПОИСК ПО КАБИНЕТУ ---
@dp.message(F.text == "🚪 Поиск по кабинету")
async def ask_cab(message: types.Message, state: FSMContext):
    await message.answer("Введите номер кабинета (например: *119*):", parse_mode="Markdown")
    await state.set_state(UserState.waiting_for_cabinet)

@dp.message(UserState.waiting_for_cabinet)
async def process_cab(message: types.Message, state: FSMContext):
    cabinet = message.text.strip()
    await state.clear()
    day_name = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][datetime.datetime.now(TZ_EKB).weekday()]
    
    if day_name == "воскресенье": return await message.answer("Сегодня выходной!")
        
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT group_name, lesson_num, time_str, subject, teacher FROM schedule WHERE cabinet = ? AND day_name = ? ORDER BY CAST(lesson_num AS INTEGER)", (cabinet, day_name)) as c:
            lessons = await c.fetchall()
            
    if not lessons: return await message.answer(f"В кабинете *{cabinet}* сегодня пар нет.", parse_mode="Markdown")
    
    msg = f"🚪 *Кабинет {cabinet} на сегодня ({day_name}):*\n\n"
    for r in lessons:
        _, ru_grp = normalize_group(r['group_name'])
        msg += f"*{r['lesson_num']} пара* ({r['time_str']}) | 👥 *{ru_grp}*\n📖 {r['subject']}\n👤 {r['teacher']}\n---\n"
    await message.answer(msg, parse_mode="Markdown")
@dp.message(F.text == "🧑‍🏫 Консультации")
async def ask_teacher(message: types.Message, state: FSMContext):
    await message.answer(
        "Введите фамилию и инициалы преподавателя (например: *Иванов И.И.*):",
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(UserState.waiting_for_teacher)

@dp.message(UserState.waiting_for_teacher)
async def process_teacher_consultation(message: types.Message, state: FSMContext):
    teacher_query = message.text
    await state.clear()
    
    await bot.send_chat_action(chat_id=message.chat.id, action='typing')
    
    # Получаем и обрабатываем результат
    result = await fetch_consultations(teacher_query)

    main_keyboard = await get_main_keyboard(message.from_user.id)

    if isinstance(result, str): # Если функция вернула текст ошибки
        return await message.answer(result, reply_markup=main_keyboard)
    
    if not result:
        return await message.answer(
            f"😕 Консультации для преподавателя '{teacher_query}' не найдены. Попробуйте ввести только фамилию.",
            reply_markup=main_keyboard
        )
        
    # Формируем красивое сообщение
    response_text = f"🧑‍🏫 Найденные консультации по запросу '{teacher_query}':\n\n"
    for cons in result:
        response_text += (
            f"👤 *{cons['teacher']}*\n"
            f"🗓 День: *{cons['day']}*\n"
            f"⏰ Время: *{cons['time']}*\n"
            f"🚪 Кабинет: *{cons['cabinet']}*\n"
            "--------------------\n"
        )

    # Защита от слишком длинных сообщений
    if len(response_text) > 4000:
        response_text = response_text[:4000] + "\n...(слишком много результатов)..."
        
    await message.answer(response_text, parse_mode="Markdown", reply_markup=main_keyboard)

# --- РАСПИСАНИЕ СТУДЕНТА (СОВМЕЩЕНКА) ---
async def get_user_schedule(message: types.Message, mode: str):
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT group_name FROM users WHERE telegram_id = ?", (message.from_user.id,)) as c:
            u = await c.fetchone()
    if not u: 
        await message.answer("Укажи группу! /start")
        return None, None, None

    en_grp = u['group_name']
    _, ru_grp = normalize_group(en_grp)
    
    sql = """
        SELECT s1.day_name, s1.lesson_num, s1.time_str, s1.subject, s1.cabinet, s1.teacher,
            (SELECT GROUP_CONCAT(s2.group_name, ',') FROM schedule s2 
             WHERE s2.day_name = s1.day_name AND s2.lesson_num = s1.lesson_num 
               AND s2.cabinet = s1.cabinet AND s2.group_name != s1.group_name 
               AND s2.cabinet NOT IN ('Неизвестно', '🌐 Дистант', '')) AS joint
        FROM schedule s1 WHERE s1.group_name = ?
    """
    
    if mode == 'today':
        day_name = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][datetime.datetime.now(TZ_EKB).weekday()]
        if day_name == "воскресенье": return ru_grp, day_name, []
        
        sql += " AND s1.day_name = ? ORDER BY CAST(s1.lesson_num AS INTEGER)"
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (en_grp, day_name)) as c: return ru_grp, day_name, await c.fetchall()
    else:
        sql += " ORDER BY CASE s1.day_name WHEN 'понедельник' THEN 1 WHEN 'вторник' THEN 2 WHEN 'среда' THEN 3 WHEN 'четверг' THEN 4 WHEN 'пятница' THEN 5 WHEN 'суббота' THEN 6 END, CAST(s1.lesson_num AS INTEGER)"
        async with aiosqlite.connect(DB_NAME) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, (en_grp,)) as c: return ru_grp, "неделя", await c.fetchall()

@dp.message(F.text == "📅 Сегодня")
async def show_today(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action='typing')
    ru_grp, day_name, lessons = await get_user_schedule(message, 'today')
    if not ru_grp: return
    if not lessons: return await message.answer(f"🎉 Для *{ru_grp}* сегодня пар нет!", parse_mode="Markdown")

    msg = f"📅 *Расписание {ru_grp} на {day_name.capitalize()}:*\n\n"
    for r in lessons:
        joint_str = ""
        if r['joint']:
            j_list = [normalize_group(g)[1] for g in r['joint'].split(',')]
            joint_str = f"\n🤝 Совместно с: *{', '.join(j_list)}*"
        msg += f"*{r['lesson_num']} ПАРА* | ⏰ {r['time_str']}\n📖 {r['subject']}\n🚪 Каб: {r['cabinet']} | 👤 {r['teacher']}{joint_str}\n----------------------------\n"
    await message.answer(msg, parse_mode="Markdown")

@dp.message(F.text == "🗓 На неделю")
async def show_week(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action='typing')
    ru_grp, _, lessons = await get_user_schedule(message, 'week')
    if not ru_grp: return
    if not lessons: return await message.answer(f"🎉 Для *{ru_grp}* пар нет!", parse_mode="Markdown")

    msg = f"🗓 *РАСПИСАНИЕ НА НЕДЕЛЮ ({ru_grp}):*\n"
    cur_day = ""
    for r in lessons:
        if r['day_name'] != cur_day:
            cur_day = r['day_name']
            msg += f"\n📍 *{cur_day.upper()}*\n"
        joint_str = f" [Совм: {', '.join([normalize_group(g)[1] for g in r['joint'].split(',')])}]" if r['joint'] else ""
        msg += f"*{r['lesson_num']}.* {r['time_str']} | {r['subject']} | Каб: {r['cabinet']}{joint_str}\n"

    if len(msg) > 4000:
        await message.answer(msg[:4000], parse_mode="Markdown")
    else:
        await message.answer(msg, parse_mode="Markdown")

# ==========================================
# 6. ЗАПУСК БОТА
# ==========================================
async def main():
    await init_db()
    
    # Первичный парсинг, если база пустая
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM schedule") as cursor:
            if (await cursor.fetchone())[0] == 0:
                await fetch_and_update_all()
                
    scheduler.add_job(fetch_and_update_all, 'interval', minutes=5)
    scheduler.start()
    
    print("🚀 Бот запущен!")
    await bot.delete_webhook(drop_pending_updates=True) 
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
