import logging
import os
import threading
import asyncio
from datetime import datetime, timedelta
from flask import Flask
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

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

def get_tomorrow_date() -> datetime:
    return datetime.now() + timedelta(days=1)

def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")

def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%d.%m.%Y")

def get_nav_keyboard(current_date_str: str) -> InlineKeyboardMarkup:
    dt = parse_date(current_date_str)
    prev_date = format_date(dt - timedelta(days=1))
    next_date = format_date(dt + timedelta(days=1))

    keyboard = [
        [
            InlineKeyboardButton("⬅️ Назад", callback_data=f"hw_{prev_date}"),
            InlineKeyboardButton("Вперед ➡️", callback_data=f"hw_{next_date}")
        ],
        [
            InlineKeyboardButton("📁 Файлы", callback_data=f"files_{current_date_str}"),
            InlineKeyboardButton("🔄 Завтра", callback_data=f"hw_{format_date(get_tomorrow_date())}")
        ],
        [
            InlineKeyboardButton("🚪 Выйти", callback_data="logout")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

async def delete_message_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    # Удаляем сообщение с командой /start
    await delete_message_safe(context, chat_id, update.message.message_id)

    if user_id in user_sessions:
        tomorrow_str = format_date(get_tomorrow_date())
        data = fetch_homework(user_sessions[user_id], tomorrow_str)
        await context.bot.send_message(
            chat_id=chat_id,
            text=data,
            reply_markup=get_nav_keyboard(tomorrow_str),
            parse_mode="Markdown"
        )
        return

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🔑 **Авторизация**\n\nОтправьте мне строку Cookie из браузера со страницы `schools.dnevnik.ru`:",
        parse_mode="Markdown"
    )
    context.user_data['prompt_msg_id'] = msg.message_id

def fetch_homework_data(raw_cookie: str, date_str: str):
    """Возвращает кортеж (результат_текста_ДЗ, список_ссылок_на_файлы)"""
    target_url = (
        f"https://schools.dnevnik.ru/v2/r/saratov/homework"
        f"?school=53421&tab=&studyYear=2026&subject="
        f"&datefrom={date_str}&dateto={date_str}&choose=%D0%9F%D0%BE%D0%BA%D0%B0%D0%B7%D0%B0%D1%82%D1%8C"
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
            return "❌ **Ошибка доступа**\n\nСессия отклонена сервером! Нажмите Выйти и введите свежий Cookie.", []

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
        else:
            resp = session.get(target_url, timeout=12, allow_redirects=True)
            soup = BeautifulSoup(resp.text, 'html.parser')

        # Сбор прикреплённых файлов (ссылок на материалы)
        files = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True) or "Файл"
            if any(ext in href.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.png', '.jpg', '.jpeg', 'hw.lecta.ru', 'download', 'file', 'attachments']):
                if not href.startswith('http'):
                    href = 'https://schools.dnevnik.ru' + href
                files.append((text, href))

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
                    
                    if any("предмет" in c.lower() or "школа" in c.lower() for c in filtered_cells):
                        continue

                    subject = ""
                    homework = ""

                    if len(filtered_cells) >= 3:
                        if "моу" in filtered_cells[0].lower() or "сош" in filtered_cells[0].lower():
                            subject = filtered_cells[1]
                            homework = ""
                        else:
                            homework = filtered_cells[0]
                            subject = filtered_cells[2]

                    clean_hw = homework.strip()

                    if len(clean_hw) > 1:
                        hw_items.append(f"📌 **{subject}:** {clean_hw}")

        if hw_items:
            result_text = f"📋 **Ваше Домашнее Задание на {date_str}:**\n\n"
            result_text += "\n\n".join(hw_items)
            return result_text, files
        else:
            return f"ℹ️ **Домашнее задание**\n\nНа {date_str} домашнего задания не найдено.", files

    except Exception as e:
        return f"⚠️ **Ошибка соединения**\n\nНе удалось получить данные: {e}", []

def fetch_homework(raw_cookie: str, date_str: str) -> str:
    text, _ = fetch_homework_data(raw_cookie, date_str)
    return text

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()
    
    # Сохраняем Cookie
    user_sessions[user_id] = text

    # Удаляем сообщение пользователя с Cookie
    await delete_message_safe(context, chat_id, update.message.message_id)

    # Удаляем предыдущее приглашение отправить Cookie
    if 'prompt_msg_id' in context.user_data:
        await delete_message_safe(context, chat_id, context.user_data['prompt_msg_id'])
        del context.user_data['prompt_msg_id']

    # Показываем ДЗ сразу на завтра
    tomorrow_str = format_date(get_tomorrow_date())
    data = fetch_homework(text, tomorrow_str)

    await context.bot.send_message(
        chat_id=chat_id,
        text=data,
        reply_markup=get_nav_keyboard(tomorrow_str),
        parse_mode="Markdown"
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    user_id = update.effective_user.id if update.effective_user else 0
    data_code = query.data

    if data_code == "logout":
        if user_id in user_sessions:
            del user_sessions[user_id]
        await query.edit_message_text(
            "🚪 **Выход выполнен**\n\nВы успешно вышли. Отправьте новый Cookie, чтобы войти снова.",
            parse_mode="Markdown"
        )
        return

    if data_code.startswith("hw_"):
        target_date_str = data_code.replace("hw_", "")
        
        if user_id not in user_sessions:
            await query.edit_message_text(
                "❌ **Ошибка авторизации**\n\nСессия истекла. Пожалуйста, отправьте Cookie снова.",
                parse_mode="Markdown"
            )
            return

        homework_text = fetch_homework(user_sessions[user_id], target_date_str)

        try:
            await query.edit_message_text(
                text=homework_text,
                reply_markup=get_nav_keyboard(target_date_str),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    if data_code.startswith("files_"):
        target_date_str = data_code.replace("files_", "")

        if user_id not in user_sessions:
            await query.answer("❌ Сессия истекла!", show_alert=True)
            return

        _, files = fetch_homework_data(user_sessions[user_id], target_date_str)

        if files:
            file_list_str = "\n".join([f"• [{name}]({url})" for name, url in files])
            files_msg = f"📁 **Прикреплённые файлы на {target_date_str}:**\n\n{file_list_str}"
        else:
            files_msg = f"📁 **Прикреплённые файлы на {target_date_str}:**\n\nФайлы и вложения на эту дату отсутствуют."

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=files_msg,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

async def start_bot() -> None:
    if not TELEGRAM_TOKEN:
        print("Ошибка: Переменная окружения BOT_TOKEN не задана!")
        return

    app_tg = Application.builder().token(TELEGRAM_TOKEN).build()
    app_tg.add_handler(CommandHandler("start", start_command))
    app_tg.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app_tg.add_handler(CallbackQueryHandler(handle_callback))

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
