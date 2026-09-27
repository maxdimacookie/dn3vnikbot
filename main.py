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
        "Привет! Для получения ДЗ отправьте мне полную строку `Cookie:` из вкладки Network в браузере:",
        parse_mode="Markdown"
    )

def fetch_homework(raw_cookie: str) -> str:
    """Парсер специальной страницы Домашних Заданий (v2)"""
    # Используем прямую страницу с домашними заданиями
    target_url = "https://schools.dnevnik.ru/v2/r/saratov/homework"
    
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
        
        # Удаляем скрипты, шапку и стили
        for bad_tag in soup(["script", "style", "header", "footer", "nav"]):
            bad_tag.extract()

        hw_items = []

        # Парсим строки таблицы с ДЗ
        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            for tr in rows:
                # Извлекаем текст из всех ячеек строки
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(['td', 'th'])]
                if cells:
                    # Фильтруем пустые элементы и соединяем разделителем
                    filtered_cells = [c for c in cells if c and len(c) > 1]
                    if len(filtered_cells) >= 2:
                        line = " | ".join(filtered_cells)
                        # Исключаем технический мусор
                        bad_words = ['профиль', 'настройки', 'выйти', 'помощь', 'серия', 'номер']
                        if not any(bad in line.lower() for bad in bad_words):
                            hw_items.append(line)

        # Резервный поиск по блокам, если таблица не собралась
        if not hw_items:
            for div in soup.find_all(['div', 'td', 'li'], class_=['homework', 'task', 'work']):
                text = div.get_text(" ", strip=True)
                if text and len(text) > 5 and text not in hw_items:
                    hw_items.append(text)

        if hw_items:
            result_text = "📋 **Ваше Домашнее Задание:**\n\n"
            result_text += "\n\n".join(hw_items[:20])
            return result_text
        else:
            return f"ℹ️ Страница открыта, но записей с ДЗ на этой странице не найдено.\nСсылка: {target_url}"

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
        await update.message.reply_text("⏳ Запрашиваю домашнее задание...")
        data = fetch_homework(user_sessions[user_id])
        await update.message.reply_text(data, parse_mode="Markdown")
        return

    if text == "🚪 Выйти / Сбросить Cookie":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("Cookie сброшен. Отправьте новую строку `Cookie:` для входа.")
        return

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
    while True:
        await asyncio.sleep(3600)

def main() -> None:
    threading.Thread(target=run_web_server, daemon=True).start()
    asyncio.run(start_bot())

if __name__ == "__main__":
    main()
