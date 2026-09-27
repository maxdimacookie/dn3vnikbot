import logging
import os
import threading
import asyncio
import datetime
from flask import Flask
import requests
from bs4 import BeautifulSoup
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- НАСТРОЙКА ВЕБ-СЕРВЕРА ДЛЯ RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "Бот работает!"

@app.route('/health')
def health():
    return "OK", 200

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
# ----------------------------------------

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# Токен берется из переменных окружения Render
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")

user_sessions = {}

def get_keyboard():
    keyboard = [
        [KeyboardButton("📚 Получить ДЗ")],
        [KeyboardButton("🚪 Выйти")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0

    if user_id in user_sessions and user_sessions[user_id].get("logged_in"):
        await update.message.reply_text(
            "Вы уже вошли в систему! Нажмите кнопку ниже, чтобы получить ДЗ.",
            reply_markup=get_keyboard()
        )
        return

    await update.message.reply_text(
        "Привет! Я бот для получения домашнего задания с Dnevnik.ru.\n\n"
        "Отправьте ваш **логин** и **пароль** от Dnevnik.ru через пробел:\n"
        "`логин пароль`",
        parse_mode="Markdown"
    )

def fetch_homework(session: requests.Session) -> str:
    """Парсинг ДЗ со страницы Саратовской области (Маркс)"""
    target_url = "https://dnevnik.ru/r/saratov/marks"

    try:
        resp = session.get(target_url, timeout=12)
        soup = BeautifulSoup(resp.text, 'html.parser')

        hw_items = []
        
        # 1. Основной поиск по элементам ДЗ
        for row in soup.find_all(['tr', 'div', 'p', 'td'], class_=['homework', 'task', 'work', 'work-item', 'dnevnik-hw']):
            text_content = row.get_text(strip=True)
            if text_content and text_content not in hw_items:
                hw_items.append(text_content)

        # 2. Резервный поиск по таблицам (исправлена ошибка tables.find_all)
        if not hw_items:
            tables = soup.find_all('table')
            if tables:
                for tr in tables[0].find_all('tr'):
                    tds = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                    if len(tds) >= 2:
                        line = " — ".join(filter(None, tds))
                        if line and line not in hw_items:
                            hw_items.append(line)

        if hw_items:
            return "📋 **Ваше домашнее задание:**\n\n" + "\n\n".join(hw_items[:15])
        else:
            return f"ℹ️ Не удалось найти блоки с ДЗ. Возможно, страница изменилась или список пуст.\nСсылка: {target_url}"

    except Exception as e:
        return f"⚠️ Ошибка при запросе к Dnevnik.ru: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    if text == "📚 Получить ДЗ":
        if user_id not in user_sessions or not user_sessions[user_id].get("logged_in"):
            await update.message.reply_text("Сначала войдите в аккаунт! Введите: `логин пароль`", parse_mode="Markdown")
            return
        await update.message.reply_text("⏳ Запрашиваю данные с Дневника...")
        data = fetch_homework(user_sessions[user_id]["session"])
        await update.message.reply_text(data, parse_mode="Markdown")
        return

    if text == "🚪 Выйти":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("Вы вышли из аккаунта. Чтобы войти снова, введите `логин пароль`.")
        return

    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        await update.message.reply_text("⚠️ Неверный формат. Отправьте логин и пароль через пробел:\n`логин пароль`", parse_mode="Markdown")
        return

    login_val, password_val = parts[0], parts[1]
    await update.message.reply_text("🔑 Авторизуюсь на Dnevnik.ru...")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })

    try:
        # Важно: Вход отправляется именно на форму логина!
        login_url = "https://login.dnevnik.ru/login"
        res = session.post(
            login_url,
            data={'login': login_val, 'password': password_val, 'Caption': 'Войти'},
            timeout=10
        )
        
        # Проверяем, остался ли пользователь на странице логина (значит пароль неверный)
        if "login" in res.url.lower() or "error" in res.text.lower():
            await update.message.reply_text("❌ Ошибка входа: неверный логин или пароль!")
            return

        user_sessions[user_id] = {"session": session, "logged_in": True}
        await update.message.reply_text("✅ Успешный вход!", reply_markup=get_keyboard())

    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка соединения: {e}")

async def start_bot() -> None:
    if not TELEGRAM_TOKEN:
        print("Ошибка: Переменная окружения BOT_TOKEN не задана!")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.initialize()
    await app.updater.start_polling()
    await app.start()
    
    print("🤖 Бот успешно запущен!")
    
    while True:
        await asyncio.sleep(3600)

def main() -> None:
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()
