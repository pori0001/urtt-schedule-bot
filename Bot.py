import re
import requests
from bs4 import BeautifulSoup
import datetime
import asyncio
import hashlib
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
URL = "https://akademiks.urtt.ru/lk/all-schedules/student/is-326"
API_TOKEN = os.getenv('BOT_TOKEN')
if not API_TOKEN:
    raise ValueError("Токен бота не найден! Добавьте переменную окружения BOT_TOKEN.")
bot = Bot(token=API_TOKEN)
dp = Dispatcher()    
scheduler = AsyncIOScheduler()
current_schedule_hash = None 
subscribers = set() 
def format_pars(num_str, raw_text):
    lesson_time = "Неизвестно"
    subject = raw_text.strip()
    cabinet = "Неизвестно"
    teacher = "Неизвестно"

    time_pattern = r'(\d{2}:\d{2}\s*-\s*\d{2}:\d{2})'
    match_time = re.search(time_pattern, raw_text)

    if match_time:
        lesson_time = match_time.group(1).strip()
        
        subject_part = raw_text[:match_time.start()].strip()
        if subject_part:
            subject = subject_part
            
        after = raw_text[match_time.end():].strip()
        
        if "," in after:
            parts = after.split(",", 1)
            cabinet = parts[0].strip()
            teacher = parts[1].strip()
        elif after:
            cabinet = after.strip()

    return (
        f"*{num_str} ПАРА*\n"
        f"⏰ Время: {lesson_time}\n"
        f"📖 Предмет: {subject}\n"
        f"🚪 Кабинет: {cabinet}\n"
        f"👤 Преподаватель: {teacher}\n"
        f"----------------------------"
    )

def get_schedule(mode='today'):
    try:
        response = requests.get(URL, timeout=10)
        soup = BeautifulSoup(response.text, 'html.parser')
        table = soup.find('table')
        
        if not table:
             return "❌ Таблица не найдена."

        rows = table.find_all('tr')
        headers = [cell.get_text(strip=True).lower() for cell in rows[0].find_all(['th', 'td'])]
        
        days_map = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
        if mode == 'today':
            today_num = datetime.datetime.now().weekday()
            
            if today_num == 6:
                return "🎉 Сегодня воскресенье, пар нет!"
                
            today = days_map[today_num]
            
            col_index = -1
            for i, header in enumerate(headers):
                if today in header:
                    col_index = i
                    break
                    
            if col_index == -1:
                return f"⚠ В расписании не найдена колонка для дня: {today.capitalize()}"
                
            s_message = f"📅 *Расписание на сегодня ({today.capitalize()}):*\n\n"
            has_less = False
            
            for row in rows[1:]:
                cols = row.find_all(['td', 'th'])
                
                if len(cols) > col_index:
                    num_str = cols[0].get_text(strip=True)
                    raw_text = cols[col_index].get_text(strip=True)
                    
                    if raw_text and "нет пары" not in raw_text.lower() and raw_text != "-":
                        has_less = True
                        form_less = format_pars(num_str, raw_text)
                        s_message += form_less + "\n"
                        
            if not has_less:
                return "🎉 На сегодня пар не найдено (или выходной)!"
                
            return s_message
        
        elif mode == 'week':
            s_message = "🗓 *РАСПИСАНИЕ НА НЕДЕЛЮ*\n\n"
            found_any = False
            
            for day in days_map[:6]: 
                col_index = -1
                for i, header in enumerate(headers):
                    if day in header: 
                        col_index = i
                        break
                
                if col_index != -1:
                    day_has_lessons = False
                    day_text = f"📍 *{day.upper()}*\n"
                    
                    for row in rows[1:]:
                        cols = row.find_all(['td', 'th'])
                        if len(cols) > col_index:
                            num_str = cols[0].get_text(strip=True)
                            raw_text = cols[col_index].get_text(strip=True)
                            
                            if raw_text and "нет пары" not in raw_text.lower() and raw_text != "-":
                                day_has_lessons = True
                                found_any = True
                                time_pattern = r'(\d{2}:\d{2}\s*-\s*\d{2}:\d{2})'
                                match_time = re.search(time_pattern, raw_text)
                                
                                if match_time:
                                    time_str = match_time.group(1).strip()
                                    subj = raw_text[:match_time.start()].strip()
                                    after = raw_text[match_time.end():].strip()
                                    if "," in after:
                                        parts = after.split(",", 1)
                                        cabinet = parts[0].strip()
                                        teacher = parts[1].strip()

                                    day_text += f"*{num_str}.* {time_str} | {subj} | {cabinet} | {teacher}\n"
                                else:
                                    day_text += f"*{num_str}.* {raw_text}\n"
                                    
                    if day_has_lessons:
                        s_message += day_text + "\n"
                        
            if not found_any: 
                return "🎉 На этой неделе пар не найдено!"
                
            return s_message

    except Exception as e:
        return f"⚠ Ошибка при подключении к сайту: {e}"
def get_schedule_hash():
    """
    Скачивает неделю, превращает текст в уникальный короткий хэш (MD5).
    Если на сайте поменяется хотя бы пробел, хэш изменится.
    """
    week_schedule_text = get_schedule(mode='week')
    
    if "⚠ Ошибка" in week_schedule_text or "❌ Таблица не найдена" in week_schedule_text:
        return None
    return hashlib.md5(week_schedule_text.encode('utf-8')).hexdigest()

async def check_for_updates():
    """Фоновая задача, которая проверяет изменения"""
    global current_schedule_hash 
    
    print("🔄 Проверка обновлений расписания...")
    new_hash = get_schedule_hash()
    
    if new_hash is None:
        return
    if current_schedule_hash is None:
        current_schedule_hash = new_hash
        print("✅ Исходное расписание сохранено в памяти.")
        return
    if new_hash != current_schedule_hash:
        print("❗ ОБНАРУЖЕНЫ ИЗМЕНЕНИЯ В РАСПИСАНИИ!")
        current_schedule_hash = new_hash
        notification_text = "🚨 *ВНИМАНИЕ!*\nРасписание на сайте было изменено!\nНажмите 'На неделю', чтобы посмотреть актуальные данные."
        
        for user_id in subscribers:
            try:
                await bot.send_message(user_id, notification_text, parse_mode="Markdown")
            except Exception as e:
                print(f"Не удалось отправить уведомление {user_id}: {e}")

def get_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.button(text="Сегодня")
    builder.button(text="Неделя")
    builder.button(text="🔔 Включить уведомления")
    builder.adjust(2, 1)
    return builder.as_markup(resize_keyboard=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для просмотра расписания.\nНажми кнопку ниже, чтобы узнать, какие сегодня пары.", 
        reply_markup=get_keyboard()
    )

@dp.message(F.text == "Сегодня")
async def show_today(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action='typing')
    
    text = get_schedule(mode='today') 
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "Неделя")
async def show_week(message: types.Message):
    await bot.send_chat_action(chat_id=message.chat.id, action='typing')
    
    text = get_schedule(mode='week') 
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🔔 Включить уведомления")
async def subscribe_notifications(message: types.Message):
    user_id = message.from_user.id
    if user_id in subscribers:
        await message.answer("Ты уже подписан на уведомления! ✅")
    else:
        subscribers.add(user_id)
        await message.answer("Уведомления включены! 🔔\nЕсли расписание на сайте изменится, я тебе напишу.")

async def main():
    print("Бот запускается...")
    global current_schedule_hash
    current_schedule_hash = get_schedule_hash()
    print("Слепок расписания создан.")

    scheduler.add_job(check_for_updates, 'interval', minutes=15)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True) 
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
