import os
import sys
import logging
from flask import Flask
from threading import Thread
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from playwright.async_api import async_playwright

# Настройка подробного логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER (Keep-Alive) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- ОБРАБОТЧИКИ TELEGRAM-БОТА ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    keyboard = [
        [InlineKeyboardButton("🔑 Войти через Госуслуги", callback_data="login_gosuslugi")],
        [InlineKeyboardButton("🍪 Ввести Cookie вручную", callback_data="login_cookie")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 Привет! Я бот для работы с Дневник.ру.\nВыбери способ авторизации:",
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на инлайн-кнопки"""
    query = update.callback_query
    await query.answer()

    if query.data == "login_gosuslugi":
        print("=== [LOG] 1. Пользователь нажал 'Войти через Госуслуги'", flush=True)
        await query.edit_message_text("🔄 Инициализация браузера Chromium...")
        
        try:
            print("=== [LOG] 2. Запуск Playwright...", flush=True)
            async with async_playwright() as p:
                # Оптимизированные аргументы запуска для бессерверных Linux-сред
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--no-zygote',
                        '--single-process'
                    ]
                )
                print("=== [LOG] 3. Браузер успешно запущен!", flush=True)
                
                context_bw = await browser.new_context(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    viewport={'width': 1280, 'height': 720}
                )
                page = await context_bw.new_page()

                print("=== [LOG] 4. Переход на страницу Госуслуг...", flush=True)
                await query.edit_message_text("🔗 Подключаюсь к серверу авторизации...")

                # Таймаут 30 секунд для предотвращения вечного зависания
                await page.goto(
                    "https://login.dnevnik.ru/login/gosuslugi", 
                    timeout=30000, 
                    wait_until="domcontentloaded"
                )

                print("=== [LOG] 5. Страница входа успешно загружена!", flush=True)
                await query.edit_message_text("✅ Страница авторизации загружена! Ожидаю данные...")

                # Сохраняем состояние при необходимости
                await browser.close()

        except Exception as e:
            error_msg = str(e)
            print(f"=== [ERROR] Ошибка Playwright: {error_msg}", flush=True)
            
            if "Timeout" in error_msg:
                await query.edit_message_text(
                    "⚠️ Превышено время ожидания ответа от Госуслуг.\n\n"
                    "Зарубежные IP-адреса Render могут блокироваться Госуслугами. "
                    "Воспользуйтесь кнопкой 'Ввести Cookie вручную'."
                )
            else:
                await query.edit_message_text(f"❌ Произошла ошибка при запуске: {error_msg}")

    elif query.data == "login_cookie":
        print("=== [LOG] Пользователь выбрал вход по Cookie", flush=True)
        await query.edit_message_text("✉️ Пожалуйста, отправьте ваши Cookie из Дневник.ру текстом.")

# --- ЗАПУСК ПРИЛОЖЕНИЯ ---

def main():
    # Запуск Flask в отдельном потоке для порта Render
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Получение токена бота из переменных окружения
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ ОШИБКА: Переменная BOT_TOKEN не найдена в Environment Variables!", flush=True)
        sys.exit(1)

    print("🤖 Бот успешно запущен и готов к работе!", flush=True)
    
    # Инициализация Telegram-бота
    application = ApplicationBuilder().token(token).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_callback))

    application.run_polling()

if __name__ == "__main__":
    main()
