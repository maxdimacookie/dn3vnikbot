import os
import sys
import logging
import asyncio
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
from playwright.async_api import async_playwright

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- ХРАНИЛИЩЕ СОСТОЯНИЙ И СЕССИЙ PLAYWRIGHT ---
# Структура: user_sessions[user_id] = { 'status': ..., 'playwright': ..., 'browser': ..., 'page': ... }
user_sessions = {}

async def close_user_session(user_id: int):
    """Безопасное закрытие браузера Playwright для конкретного пользователя"""
    session = user_sessions.get(user_id)
    if session:
        try:
            if session.get('browser'):
                await session['browser'].close()
            if session.get('playwright'):
                await session['playwright'].stop()
        except Exception as e:
            print(f"=== [ERROR] Ошибка при закрытии сессии: {e}", flush=True)
        finally:
            user_sessions.pop(user_id, None)

# --- КЛАВИАТУРЫ ---

def get_main_menu_keyboard():
    """Главный экран с выбором способа входа"""
    keyboard = [
        [InlineKeyboardButton("🔑 Войти через Госуслуги", callback_data="login_gosuslugi")],
        [InlineKeyboardButton("🍪 Ввести Cookie вручную", callback_data="login_cookie")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard():
    """Кнопка отмены во время ввода данных"""
    keyboard = [
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel_auth")]
    ]
    return InlineKeyboardMarkup(keyboard)

# --- ОБРАБОТЧИКИ TELEGRAM ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    user_id = update.effective_user.id
    await close_user_session(user_id)
    
    await update.message.reply_text(
        "👋 Привет! Я бот для работы с Дневник.ру.\nВыбери способ авторизации:",
        reply_markup=get_main_menu_keyboard()
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на инлайн-кнопки"""
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "login_gosuslugi":
        print(f"=== [LOG] Пользователь {user_id} нажал 'Войти через Госуслуги'", flush=True)
        
        # Сбрасываем старую сессию, если она была
        await close_user_session(user_id)
        user_sessions[user_id] = {'status': 'WAITING_CREDENTIALS'}

        await query.edit_message_text(
            "🔐 **Авторизация через Госуслуги**\n\n"
            "Пришлите ваши **Логин** (телефон/email) и **Пароль** в формате двух строчек:\n\n"
            "`+79001234567`\n"
            "`ВашПароль`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard()
        )

    elif query.data == "login_cookie":
        await close_user_session(user_id)
        user_sessions[user_id] = {'status': 'WAITING_COOKIE'}
        
        await query.edit_message_text(
            "✉️ Отправьте ваши Cookie из Дневник.ру сообщением.",
            reply_markup=get_cancel_keyboard()
        )

    elif query.data == "cancel_auth":
        print(f"=== [LOG] Пользователь {user_id} нажал 'Отмена'", flush=True)
        await close_user_session(user_id)
        
        await query.edit_message_text(
            "❌ Авторизация отменена.\nВыбери способ авторизации:",
            reply_markup=get_main_menu_keyboard()
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений (логин/пароль, SMS-код или Cookie)"""
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()
    session = user_sessions.get(user_id)

    if not session or session.get('status') is None:
        await update.message.reply_text(
            "Пожалуйста, выберите действие в меню:",
            reply_markup=get_main_menu_keyboard()
        )
        return

    status = session.get('status')

    # ШАГ 1: Получение Логина и Пароля
    if status == 'WAITING_CREDENTIALS':
        lines = text.split('\n')
        if len(lines) < 2:
            await update.message.reply_text(
                "⚠️ Отправьте данные в две строчки:\nПервая строчка — Логин\nВторая строчка — Пароль",
                reply_markup=get_cancel_keyboard()
            )
            return

        login = lines[0].strip()
        password = lines[1].strip()

        msg = await update.message.reply_text(
            "🔄 Открываю страницу Госуслуг...",
            reply_markup=get_cancel_keyboard()
        )

        session['status'] = 'PROCESSING'
        
        # Запуск Playwright в фоновом режиме
        asyncio.create_task(process_gosuslugi_login(user_id, login, password, msg, context))

    # ШАГ 2: Ввод 2FA / SMS-кода
    elif status == 'WAITING_SMS':
        sms_code = text.strip()
        msg = await update.message.reply_text(
            "⏳ Ввожу SMS-код...",
            reply_markup=get_cancel_keyboard()
        )
        session['status'] = 'PROCESSING'
        
        asyncio.create_task(process_gosuslugi_sms(user_id, sms_code, msg, context))

    elif status == 'WAITING_COOKIE':
        await update.message.reply_text("✅ Cookie получены! Сохраняю...")
        await close_user_session(user_id)

# --- ЛОГИКА PLAYWRIGHT ---

async def process_gosuslugi_login(user_id: int, login: str, password: str, status_msg, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session:
        return

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            channel="chromium",
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
        )
        context_bw = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context_bw.new_page()

        # Сохраняем ссылки для управления сессией
        session.update({'playwright': pw, 'browser': browser, 'page': page})

        await status_msg.edit_text("🔗 Загружаю форму входа...", reply_markup=get_cancel_keyboard())
        await page.goto("https://login.dnevnik.ru/login/gosuslugi", timeout=30000, wait_until="domcontentloaded")

        await status_msg.edit_text("✍️ Ввожу логин и пароль...", reply_markup=get_cancel_keyboard())
        
        # Заполнение полей логина и пароля
        await page.fill('input[id="login"]', login)
        await page.fill('input[id="password"]', password)
        await page.click('button[type="submit"]')

        await asyncio.sleep(4)

        # Проверка: требуется ли SMS-код (2FA)
        if "code" in page.url or await page.is_visible('input[name="code"]'):
            session['status'] = 'WAITING_SMS'
            await status_msg.edit_text(
                "📲 **Введите SMS-код**, отправленный Госуслугами:",
                parse_mode="Markdown",
                reply_markup=get_cancel_keyboard()
            )
        else:
            await finalize_login(user_id, status_msg)

    except Exception as e:
        print(f"=== [ERROR] Ошибка входа через Госуслуги: {e}", flush=True)
        await status_msg.edit_text(
            f"❌ Ошибка авторизации: {e}\n\nПопробуйте снова или воспользуйтесь кнопкой Отмена.",
            reply_markup=get_main_menu_keyboard()
        )
        await close_user_session(user_id)

async def process_gosuslugi_sms(user_id: int, sms_code: str, status_msg, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session or not session.get('page'):
        await status_msg.edit_text("❌ Сессия была сброшена. Начните заново.", reply_markup=get_main_menu_keyboard())
        return

    page = session['page']
    try:
        await page.fill('input[name="code"]', sms_code)
        await page.click('button[type="submit"]')
        await asyncio.sleep(5)

        await finalize_login(user_id, status_msg)

    except Exception as e:
        print(f"=== [ERROR] Ошибка ввода SMS: {e}", flush=True)
        await status_msg.edit_text(
            f"❌ Неверный код или ошибка отправки: {e}",
            reply_markup=get_cancel_keyboard()
        )
        session['status'] = 'WAITING_SMS'

async def finalize_login(user_id: int, status_msg):
    session = user_sessions.get(user_id)
    if not session or not session.get('page'):
        return

    page = session['page']
    try:
        cookies = await page.context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        await status_msg.edit_text(
            "🎉 **Успешный вход!**\n\nСессия Дневник.ру сохранена.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
    finally:
        await close_user_session(user_id)

# --- ЗАПУСК ПРИЛОЖЕНИЯ ---

def main():
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ ОШИБКА: Переменная BOT_TOKEN не найдена!", flush=True)
        sys.exit(1)

    print("🤖 Бот успешно запущен!", flush=True)

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
