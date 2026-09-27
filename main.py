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

# Токен берётся из переменных окружения Render (переменная BOT_TOKEN)
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
        "Привет! Для получения ДЗ отправьте мне полную строку `Cookie:` из вкладки Network в браузере:",
        parse_mode="Markdown"
    )

def fetch_homework(raw_cookie: str) -> str:
    """Точечный парсер оценок и ДЗ для Dnevnik.ru"""
    target_url = "https://dnevnik.ru/r/saratov/marks"
    
    session = requests.Session()
    clean_cookie = raw_cookie.replace("Cookie:", "").strip()
    
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Cookie': clean_cookie
    })

    try:
        resp = session.get(target_url, timeout=12, allow_redirects=True)
        
        if "login" in resp.url.lower():
            return "❌ Сессия истекла! Скопируйте свежую строку `Cookie:` из вкладки Network."

        soup = BeautifulSoup(resp.text, 'html.parser')
        hw_items = []

        # 1. Поиск по специфическим классам Дневника (предметы, задания, оценки)
        items = soup.find_all(['td', 'div', 'tr', 'li'], class_=['subject', 'work', 'homework', 'mark', 'task'])
        for item in items:
            text = item.get_text(" ", strip=True)
            if text and len(text) > 3 and text not in hw_items:
                if not any(bad in text.lower() for bad in ['профиль', 'выйти', 'настройки', 'дневник.ру']):
                    hw_items.append(text)

        # 2. Если по классам ничего не нашлось, забираем данные из всех таблиц на странице
        if not hw_items:
            tables = soup.find_all('table')
            for table in tables:
                for tr in table.find_all('tr'):
                    cells = [td.get_text(" ", strip=True) for td in tr.find_all(['td', 'th'])]
                    if len(cells) >= 1:
                        line = " | ".join([c for c in cells if c])
                        if len(line) > 3 and line not in hw_items:
                            if not any(bad in line.lower() for bad in ['профиль', 'выйти', 'настройки', 'помощь']):
                                hw_items.append(line)

        if hw_items:
            result_text = "📋 **Данные с вашей страницы Dnevnik.ru:**\n\n"
            result_text += "\n\n".join(hw_items[:25])
            return result_text
        else:
            return f"ℹ️ Таблицы не найдены. Возможно, на этой неделе нет записей.\nСсылка: {target_url}"

    except Exception as e:
        return f"⚠️ Ошибка соединения: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    if text == "📚 Получить ДЗ":
        if user_id not in user_sessions:
            await update.message.reply_text("Сначала отправьте вашу строку `Cookie:` из браузера!", parse_mode="Markdown")
            return
        await update.message.reply_text("⏳ Запрашиваю данные с Дневника...")
        data = fetch_homework(user_sessions[user_id])
        await update.message.reply_text(data, parse_mode="Markdown")
        return

    if text == "🚪 Выйти / Сбросить Cookie":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("Cookie сброшен. Отправьте новую строку `Cookie:` для входа.")
        return

    # Сохраняем строку Cookie
    user_sessions[user_id] = text
    await update.message.reply_text("✅ Cookie сохранен! Проверяю получение ДЗ...", reply_markup=get_keyboard())
    
    data = fetch_homework(text)
    await update.message.reply_text(data, parse_mode="Markdown")

async def start_bot() -> None:
    if not TELEGRAM_TOKEN:
        print("Ошибка: Переменная окружения BOT_TOKEN не задана на Render!")
        return

    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start_command))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app_tg.initialize()
    await app_tg.updater.start_polling()
    await app_tg.start()
    
    print("🤖 Бот успешно запущен!")
    
    # Бесконечный цикл удерживает скрипт активным
    while True:
        await asyncio.sleep(3600)

def main() -> None:
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_web_server, daemon=True).start()
    # Запускаем Telegram бота в основном потоке
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()
