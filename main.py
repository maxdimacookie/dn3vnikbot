import logging
import os
import threading
import asyncio
from datetime import datetime, timedelta
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
        [KeyboardButton("📚 Получить ДЗ на завтра")],
        [KeyboardButton("🚪 Выйти / Сбросить Cookie")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def send_long_message(update: Update, text: str):
    """Отправка текста частями, если он превышает лимит Telegram"""
    max_len = 4000
    if len(text) <= max_len:
        await update.message.reply_text(text, parse_mode="Markdown")
        return

    parts = []
    while len(text) > max_len:
        split_at = text.rfind('\n', 0, max_len)
        if split_at == -1:
            split_at = max_len
        parts.append(text[:split_at])
        text = text[split_at:].strip()
    if text:
        parts.append(text)

    for part in parts:
        await update.message.reply_text(part, parse_mode="Markdown")

def get_tomorrow_date_str() -> str:
    """Возвращает завтрашнюю дату в формате ДД.ММ.ГГГГ"""
    tomorrow = datetime.now() + timedelta(days=1)
    return tomorrow.strftime("%d.%m.%Y")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    user_id = update.effective_user.id if update.effective_user else 0

    if user_id in user_sessions:
        await update.message.reply_text(
            "Вы уже авторизованы! Нажмите кнопку ниже, чтобы получить ДЗ на завтра.",
            reply_markup=get_keyboard()
        )
        return

    await update.message.reply_text(
        "Привет! Отправьте мне строку Cookie из браузера со страницы `schools.dnevnik.ru`:",
        parse_mode="Markdown"
    )

def fetch_homework(raw_cookie: str) -> str:
    tomorrow_str = get_tomorrow_date_str()
    
    target_url = (
        f"https://schools.dnevnik.ru/v2/r/saratov/homework"
        f"?school=53421&tab=&studyYear=2026&subject="
        f"&datefrom={tomorrow_str}&dateto={tomorrow_str}&choose=%D0%9F%D0%BE%D0%BA%D0%B0%D0%B7%D0%B0%D1%82%D1%8C"
    )
    
    session = requests.Session()
    clean_cookie = raw_cookie.strip()
    if clean_cookie.lower().startswith("cookie:"):
        clean_cookie = clean_cookie[7:].strip()
    
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
        'Cache-Control': 'max-age=0',
        'Connection': 'keep-alive',
        'Host': 'schools.dnevnik.ru',
        'Referer': 'https://schools.dnevnik.ru/',
        'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'Cookie': clean_cookie
    })

    try:
        resp = session.get(target_url, timeout=12, allow_redirects=False)
        
        if resp.status_code in [301, 302] and "login" in resp.headers.get("Location", "").lower():
            return "❌ Сессия отклонена сервером! Скопируйте свежую строку Cookie со страницы schools.dnevnik.ru."

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
        else:
            resp = session.get(target_url, timeout=12, allow_redirects=True)
            soup = BeautifulSoup(resp.text, 'html.parser')

        for bad_tag in soup(["script", "style", "header", "footer", "nav"]):
            bad_tag.extract()

        hw_items = []

        tables = soup.find_all('table')
        for table in tables:
            rows = table.find_all('tr')
            for tr in rows:
                cells = [td.get_text(" ", strip=True) for td in tr.find_all(['td', 'th'])]
                if cells:
                    filtered_cells = [c for c in cells if c and len(c) > 0]
                    
                    # Игнорируем шапку таблицы
                    if any("предмет" in c.lower() or "школа" in c.lower() for c in filtered_cells):
                        continue

                    subject = ""
                    homework = ""

                    if len(filtered_cells) >= 3:
                        # Разбираем структуру: [ДЗ (опционально)], [Школа], [Предмет], ...
                        if "моу" in filtered_cells[0].lower() or "сош" in filtered_cells[0].lower():
                            # Формат без текста ДЗ: [Школа], [Предмет], [Урок], ...
                            subject = filtered_cells[1]
                            homework = ""
                        else:
                            # Формат с текстом ДЗ: [ДЗ], [Школа], [Предмет], ...
                            homework = filtered_cells[0]
                            subject = filtered_cells[2]

                    # Очищаем текст ДЗ и проверяем его длину
                    clean_hw = homework.strip()

                    # Фильтруем то, у чего длина ДЗ 1 символ или меньше (точки, тире, пустота)
                    if len(clean_hw) > 1:
                        hw_items.append(f"📌 **{subject}:** {clean_hw}")

        if hw_items:
            result_text = f"📋 **Ваше Домашнее Задание на {tomorrow_str}:**\n\n"
            result_text += "\n\n".join(hw_items)
            return result_text
        else:
            return f"ℹ️ На {tomorrow_str} домашнего задания не найдено (или уроки без ДЗ).\nURL: {target_url}"

    except Exception as e:
        return f"⚠️ Ошибка соединения: {e}"

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    if text in ["📚 Получить ДЗ", "📚 Получить ДЗ на завтра"]:
        if user_id not in user_sessions:
            await update.message.reply_text("Сначала отправьте вашу строку Cookie из браузера!")
            return
        tomorrow_str = get_tomorrow_date_str()
        await update.message.reply_text(f"⏳ Запрашиваю домашнее задание на завтра ({tomorrow_str})...")
        data = fetch_homework(user_sessions[user_id])
        await send_long_message(update, data)
        return

    if text == "🚪 Выйти / Сбросить Cookie":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await update.message.reply_text("Cookie сброшен. Отправьте новую строку Cookie для входа.")
        return

    user_sessions[user_id] = text
    tomorrow_str = get_tomorrow_date_str()
    await update.message.reply_text(
        f"✅ Cookie сохранен! Проверяю получение ДЗ на завтра ({tomorrow_str})...", 
        reply_markup=get_keyboard()
    )
    
    data = fetch_homework(text)
    await send_long_message(update, data)

async def start_bot() -> None:
    if not TELEGRAM_TOKEN:
        print("Ошибка: Переменная окружения BOT_TOKEN не задана!")
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
