import logging
import os
import threading
import asyncio
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

TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")

user_sessions = {}

def get_keyboard():
    keyboard = [
        [KeyboardButton("📚 Получить ДЗ")],
        [KeyboardButton("🚪 Выйти / Сбросить Cookie")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0

    if user_id in user_sessions:
        await update.message.reply_text(
            "Вы уже авторизованы! Нажмите кнопку ниже, чтобы получить ДЗ.",
            reply_markup=get_keyboard()
        )
        return

    await update.message.reply_text(
        "Привет! Из-за принудительного входа через Госуслуги авторизация работает по **Cookie**.\n\n"
        "Отправьте мне значение ключа `dnevnik_id` из браузера:",
        parse_mode="Markdown"
    )

def fetch_homework(cookie_val: str) -> str:
    """Запрос ДЗ с использованием сессионных куки"""
    target_url = "https://dnevnik.ru/r/saratov/marks"
    
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    # Подставляем авторизационный куки
    session.cookies.set('dnevnik_id', cookie_val, domain='dnevnik.ru')

    try:
        resp = session.get(target_url, timeout=12)
        
        # Если перенаправило на страницу логина — куки устарел
        if "login" in resp.url.lower():
            return "❌ Ошибка: Сессия истекла! Пожалуйста, скопируйте новый `dnevnik_id` из браузера и отправьте его боту."

        soup = BeautifulSoup(resp.text, 'html.parser')
        hw_items = []
        
        for row in soup.find_all(['tr', 'div', 'p', 'td'], class_=['homework', 'task', 'work', 'work-item', 'dnevnik-hw']):
            text_content = row.get_text(strip=True)
            if text_content and text_content not in hw_items:
                hw_items.append(text_content)

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
            return f"ℹ️ Не удалось распарсить блоки ДЗ. Проверьте страницу в браузере: {target_url}"

    except Exception as e:
        return f"⚠️ Ошибка при запросе к Dnevnik.ru: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    if text == "📚 Получить ДЗ":
        if user_id not in user_sessions:
            await update.message.reply_text("Сначала отправьте ваш `dnevnik_id`!", parse_mode="Markdown")
            return
        await update.message.reply_text("⏳ Запрашиваю данные с Дневника...")
        data = fetch_homework(user_sessions[user_id])
        await update.message.reply_text(data, parse_mode="Markdown")
        return

    if text == "🚪 Выйти / Сбросить Cookie":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("Cookie сброшен. Отправьте новый `dnevnik_id` для входа.")
        return

    # Сохраняем переданный куки
    user_sessions[user_id] = text
    await update.message.reply_text("✅ Cookie сохранен! Проверяю получение ДЗ...", reply_markup=get_keyboard())
    
    # Сразу пробуем сделать тестовый запрос
    data = fetch_homework(text)
    await update.message.reply_text(data, parse_mode="Markdown")

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
