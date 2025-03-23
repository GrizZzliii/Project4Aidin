import sqlite3
import logging
import asyncio
import configparser
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta, time

# Чтение конфигурационного файла
config = configparser.ConfigParser()
config.read('config.ini')

# Настройки из config.ini
TOKEN = config.get('Telegram', 'token')
ADMIN_IDS = set(map(int, config.get('Telegram', 'admin_ids').split(',')))

# Логирование
logging.basicConfig(level=logging.INFO)

# База данных
conn = sqlite3.connect("reports.db", check_same_thread=False)
cursor = conn.cursor()

# Создание таблицы users для хранения информации о зарегистрированных пользователях
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    user_name TEXT
)
""")

# Создание таблицы reports для хранения отчетов
cursor.execute("""
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    completed_task TEXT,
    next_task TEXT,
    timestamp TEXT,
    FOREIGN KEY(user_id) REFERENCES users(user_id)
)
""")
conn.commit()

# Инициализация бота
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Планировщик задач
scheduler = AsyncIOScheduler()

# Состояния для FSM
class RegistrationState(StatesGroup):
    name = State()  # Состояние для ввода имени пользователя

class ReportState(StatesGroup):
    completed_task = State()
    next_task = State()

# Клавиатура с кнопками
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📝 Начать отчёт")],
        [KeyboardButton(text="📜 Просмотреть отчёты")]
    ], resize_keyboard=True)
    return keyboard

# Проверка, зарегистрирован ли пользователь
def is_user_registered(user_id):
    cursor.execute("SELECT user_name FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    return result is not None and result[0] is not None

# Команда /start
@dp.message(Command("start"))
async def start_command(message: Message, state: FSMContext):
    user_id = message.from_user.id

    if is_user_registered(user_id):
        # Если пользователь уже зарегистрирован, показываем основное меню
        await message.answer("С возвращением! Используйте кнопки ниже для управления.", reply_markup=get_main_keyboard())
    else:
        # Если пользователь не зарегистрирован, запрашиваем имя
        await message.answer("Привет! Пожалуйста, введите ваше имя для регистрации.")
        await state.set_state(RegistrationState.name)

# Принимаем имя пользователя
@dp.message(RegistrationState.name)
async def get_user_name(message: Message, state: FSMContext):
    user_name = message.text
    user_id = message.from_user.id

    # Сохраняем имя пользователя в таблице users
    cursor.execute("INSERT OR REPLACE INTO users (user_id, user_name) VALUES (?, ?)", (user_id, user_name))
    conn.commit()

    await message.answer(f"✅ Спасибо, {user_name}! Теперь вы зарегистрированы.", reply_markup=get_main_keyboard())
    await state.clear()

# Начало отчёта
@dp.message(lambda message: message.text == "📝 Начать отчёт")
async def start_report(message: Message, state: FSMContext):
    await message.answer("📌 Опишите, что вы уже сделали.", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(ReportState.completed_task)

# Принимаем выполненную работу
@dp.message(ReportState.completed_task)
async def get_completed_task(message: Message, state: FSMContext):
    current_state = await state.get_state()
    logging.info(f"Текущее состояние: {current_state}")  # Лог состояния
    await state.update_data(completed_task=message.text)
    await message.answer("📅 Теперь укажите, чем собираетесь заняться.")
    await state.set_state(ReportState.next_task)

# Принимаем предстоящую работу и сохраняем отчёт
@dp.message(ReportState.next_task)
async def get_next_task(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_name = cursor.execute("SELECT user_name FROM users WHERE user_id = ?", (user_id,)).fetchone()[0]
    data = await state.get_data()
    completed_task = data["completed_task"]
    next_task = message.text

    # Получаем текущее локальное время (UTC+6)
    local_time = datetime.now() + timedelta(hours=6)
    timestamp = local_time.strftime("%Y-%m-%d %H:%M:%S")

    # Записываем в таблицу reports
    cursor.execute("""INSERT INTO reports (user_id, completed_task, next_task, timestamp)
                  VALUES (?, ?, ?, ?)""",
               (user_id, completed_task, next_task, timestamp))
    conn.commit()

    await message.answer("✅ Отчёт сохранён!", reply_markup=get_main_keyboard())
    await state.clear()

# Просмотр всех отчётов (только для админов)
@dp.message(lambda message: message.text == "📜 Просмотреть отчёты")
async def view_reports(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ У вас нет прав для просмотра отчётов.", reply_markup=get_main_keyboard())
        return
    
    cursor.execute("""
        SELECT users.user_name, reports.completed_task, reports.next_task, reports.timestamp
        FROM reports
        JOIN users ON reports.user_id = users.user_id
        ORDER BY reports.timestamp DESC
    """)
    reports = cursor.fetchall()
    if not reports:
        await message.answer("📭 Отчётов пока нет.", reply_markup=get_main_keyboard())
        return

    response = "📜 Отчёты:\n\n" + "\n\n".join(
        [f"👤 {r[0]}\n🕒 {r[3]}\n✅ {r[1]}\n⏭ {r[2]}" for r in reports]
    )
    await message.answer(response, reply_markup=get_main_keyboard())

# Функция для отправки отчётов администраторам
async def send_reports_to_admins():
    cursor.execute("""
        SELECT users.user_name, reports.completed_task, reports.next_task, reports.timestamp
        FROM reports
        JOIN users ON reports.user_id = users.user_id
        ORDER BY reports.timestamp DESC
    """)
    reports = cursor.fetchall()
    if not reports:
        return

    response = "📜 Отчёты за сегодня:\n\n" + "\n\n".join(
        [f"👤 {r[0]}\n🕒 {r[3]}\n✅ {r[1]}\n⏭ {r[2]}" for r in reports]
    )

    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, response)

# Планирование отправки отчётов
def schedule_reports():
    # Отправка в 9:00 каждый будний день
    scheduler.add_job(send_reports_to_admins, 'cron', day_of_week='mon-fri', hour=9, minute=0)
    
    # Отправка в 17:30 каждый будний день
    scheduler.add_job(send_reports_to_admins, 'cron', day_of_week='mon-fri', hour=17, minute=30)

# Функция запуска бота
async def main():
    schedule_reports()  # Запуск планировщика
    scheduler.start()
    await dp.start_polling(bot)

# Запуск
if __name__ == "__main__":
    asyncio.run(main())