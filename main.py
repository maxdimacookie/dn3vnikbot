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
    """Универсальный парсер ДЗ для Dnevnik.ru"""
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
            return "❌ Сессия истекла! Скопируйте свежую строку `Cookie:` из вкладки Network в браузере."

        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Очищаем документ от ненужных скриптов и стилей
        for script in soup(["script", "style"]):
            script.extract()

        hw_items = []

        # 1. Сначала ищем ячейки таблиц
        rows = soup.find_all('tr')
        for tr in rows:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(['td', 'th'])]
            if len(cells) >= 2:
                row_text = " | ".join([c for c in cells if c])
                if len(row_text) > 5 and not any(bad in row_text.lower() for bad in ['посещаемость', 'средний балл', 'итоговые']):
                    hw_items.append(row_text)

        # 2. Если таблицы не дали результат, забираем текстовые блоки
        if not hw_items:
            for el in soup.find_all(['div', 'p', 'li', 'span']):
                text = el.get_text(strip=True)
                if 10 < len(text) < 300 and any(kw in text.lower() for kw in ['д/з', 'домашнее', 'задание', 'стр', 'упр', 'параграф']):
                    if text not in hw_items:
                        hw_items.append(text)

        if hw_items:
            result_text = "📋 **Ваше домашнее задание / Дневник:**\n\n"
            result_text += "\n\n".join(hw_items[:20])
            return result_text
        else:
            return f"ℹ️ Страница открыта, но текст ДЗ не найден. Возможно, на эту неделю нет заданий.\nСсылка: {target_url}"

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
