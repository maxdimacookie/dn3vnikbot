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
from playwright.async_api import async_playwright

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

def fetch_homework_data(raw_cookie: str, date_str: str):
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
        'Cookie': clean_cookie
    })

    try:
        resp = session.get(target_url, timeout=12, allow_redirects=False)
        
        if resp.status_code in [301, 302] and "login" in resp.headers.get("Location", "").lower():
            return "❌ **Ошибка сессии**\n\nСрок действия Cookie истёк. Пожалуйста, авторизуйтесь заново.", []

        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
        else:
            resp = session.get(target_url, timeout=12, allow_redirects=True)
            soup = BeautifulSoup(resp.text, 'html.parser')

        files = []
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True) or "Прикрепленный файл"
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
            return f"ℹ️ **Домашнее задание**\n\nНа **{date_str}** домашнего задания не найдено (или уроки без ДЗ).", files

    except Exception as e:
        return f"⚠️ **Ошибка соединения**\n\nНе удалось загрузить данные: {e}", []
# --- КЛАВИАТУРЫ ---

def get_main_keyboard(date_str: str) -> InlineKeyboardMarkup:
    dt = parse_date(date_str)
    prev_date = format_date(dt - timedelta(days=1))
    next_date = format_date(dt + timedelta(days=1))

    keyboard = [
        [
            InlineKeyboardButton("⬅️ Назад", callback_data=f"hw_{prev_date}"),
            InlineKeyboardButton("Вперед ➡️", callback_data=f"hw_{next_date}")
        ],
        [
            InlineKeyboardButton("📁 Файлы", callback_data=f"files_{date_str}"),
            InlineKeyboardButton("🔄 Завтра", callback_data=f"hw_{format_date(get_tomorrow_date())}")
        ],
        [
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
            InlineKeyboardButton("🚪 Выйти", callback_data="logout_menu")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_logout_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("🔑 Войти через Госуслуги", callback_data="login_gosuslugi")],
        [InlineKeyboardButton("📋 Ввести Cookie вручную", callback_data="enter_cookie")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help_info")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cookie_prompt_keyboard(has_last_cookie: bool) -> InlineKeyboardMarkup:
    row = []
    if has_last_cookie:
        row.append(InlineKeyboardButton("📥 Загрузить последний", callback_data="load_last_cookie"))
    row.append(InlineKeyboardButton("🏠 Домой", callback_data="home"))
    return InlineKeyboardMarkup([row])

def get_files_keyboard(files: list, date_str: str) -> InlineKeyboardMarkup:
    keyboard = []
    for idx, (title, url) in enumerate(files, 1):
        btn_label = f"📥 Скачать: {title[:20]}" if len(title) > 20 else f"📥 Скачать: {title}"
        keyboard.append([InlineKeyboardButton(btn_label, url=url)])
    keyboard.append([InlineKeyboardButton("🏠 Домой", callback_data=f"hw_{date_str}")])
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard(hours: int) -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("➖ 1ч", callback_data=f"set_interval_{hours - 1}"),
            InlineKeyboardButton(f"⏱ {hours} ч.", callback_data="ignore"),
            InlineKeyboardButton("➕ 1ч", callback_data=f"set_interval_{hours + 1}")
        ],
        [InlineKeyboardButton("🏠 Домой", callback_data="home_refresh")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Домой", callback_data="home")]])

async def delete_message_safe(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# --- АВТОМАГИЯ АВТОРИЗАЦИИ ЧЕРЕЗ PLAYWRIGHT ---

async def start_gosuslugi_login(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int, login: str, password: str):
    auth_state = user_sessions[user_id]['auth_state']
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context_bw = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    page = await context_bw.new_page()

    auth_state.update({'playwright': playwright, 'browser': browser, 'page': page, 'status': 'WAITING_SMS'})

    try:
        await page.goto("https://login.dnevnik.ru/login/gosuslugi")
        await page.wait_for_load_state("networkidle")
        await page.fill('input[id="login"]', login)
        await page.fill('input[id="password"]', password)
        await page.click('button[type="submit"]')
        await asyncio.sleep(4)

        if "code" in page.url or await page.is_visible('input[name="code"]'):
            await context.bot.send_message(chat_id=chat_id, text="📲 **Введите SMS-код**, отправленный Госуслугами:", parse_mode="Markdown")
        else:
            await finalize_gosuslugi_login(user_id, context, chat_id)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка входа через Госуслуги: {e}")
        await close_playwright(auth_state)

async def finish_gosuslugi_sms(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int, sms_code: str):
    auth_state = user_sessions[user_id]['auth_state']
    page = auth_state['page']
    try:
        await page.fill('input[name="code"]', sms_code)
        await page.click('button[type="submit"]')
        await asyncio.sleep(5)
        await finalize_gosuslugi_login(user_id, context, chat_id)
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка ввода SMS-кода: {e}")
        await close_playwright(auth_state)

async def finalize_gosuslugi_login(user_id: int, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    auth_state = user_sessions[user_id]['auth_state']
    page = auth_state['page']
    try:
        cookies = await page.context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        user_data = user_sessions[user_id]
        user_data['cookie'] = cookie_str
        user_data['last_cookie'] = cookie_str
        tomorrow_str = format_date(get_tomorrow_date())
        user_data['active_date'] = tomorrow_str

        hw_text, _ = fetch_homework_data(cookie_str, tomorrow_str)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"✅ **Успешный вход через Госуслуги!**\n\n{hw_text}",
            reply_markup=get_main_keyboard(tomorrow_str),
            parse_mode="Markdown"
        )
    finally:
        await close_playwright(auth_state)

async def close_playwright(auth_state: dict):
    if auth_state.get('browser'):
        await auth_state['browser'].close()
    if auth_state.get('playwright'):
        await auth_state['playwright'].stop()
    auth_state.clear()
# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    await delete_message_safe(context, chat_id, update.message.message_id)

    user_data = user_sessions.setdefault(user_id, {
        'cookie': None, 'last_cookie': None, 'interval': 1,
        'active_date': format_date(get_tomorrow_date()), 'auth_state': {}
    })

    if user_data['cookie']:
        date_str = user_data['active_date']
        text, _ = fetch_homework_data(user_data['cookie'], date_str)
        await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=get_main_keyboard(date_str), parse_mode="Markdown")
    else:
        await context.bot.send_message(chat_id=chat_id, text="🚪 **Авторизация**\n\nВы не вошли в систему. Выберите действие ниже:", reply_markup=get_logout_keyboard(), parse_mode="Markdown")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    await delete_message_safe(context, chat_id, update.message.message_id)

    user_data = user_sessions.setdefault(user_id, {
        'cookie': None, 'last_cookie': None, 'interval': 1,
        'active_date': format_date(get_tomorrow_date()), 'auth_state': {}
    })

    auth_state = user_data['auth_state']

    if auth_state.get('status') == 'WAITING_GOSU_DATA':
        try:
            login, password = text.split('\n')
            auth_state['status'] = 'PROCESSING'
            await context.bot.send_message(chat_id, "⏳ Подключаюсь к Госуслугам...")
            await start_gosuslugi_login(user_id, context, chat_id, login.strip(), password.strip())
        except ValueError:
            await context.bot.send_message(chat_id, "⚠️ Отправьте логин и пароль в две строчки!\nПример:\n+79001112233\nМойПароль123")
        return

    elif auth_state.get('status') == 'WAITING_SMS':
        auth_state['status'] = 'PROCESSING'
        await context.bot.send_message(chat_id, "⏳ Проверяю SMS-код...")
        await finish_gosuslugi_sms(user_id, context, chat_id, text)
        return

    user_data['cookie'] = text
    user_data['last_cookie'] = text
    tomorrow_str = format_date(get_tomorrow_date())
    user_data['active_date'] = tomorrow_str

    hw_text, _ = fetch_homework_data(text, tomorrow_str)
    msg = await context.bot.send_message(chat_id=chat_id, text=hw_text, reply_markup=get_main_keyboard(tomorrow_str), parse_mode="Markdown")
    context.user_data['main_msg_id'] = msg.message_id

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    user_id = update.effective_user.id if update.effective_user else 0
    data_code = query.data
    if data_code == "ignore":
        return

    user_data = user_sessions.setdefault(user_id, {
        'cookie': None, 'last_cookie': None, 'interval': 1,
        'active_date': format_date(get_tomorrow_date()), 'auth_state': {}
    })
    context.user_data['main_msg_id'] = query.message.message_id

    if data_code == "login_gosuslugi":
        user_data['auth_state']['status'] = 'WAITING_GOSU_DATA'
        await query.edit_message_text("🔑 **Авторизация через Госуслуги**\n\nПришлите логин и пароль **в двух строчках**:\n\n`+79001234567`\n`Пароль`", parse_mode="Markdown")

    elif data_code.startswith("hw_") or data_code == "home":
        target_date = data_code.replace("hw_", "") if data_code.startswith("hw_") else user_data['active_date']
        user_data['active_date'] = target_date

        if not user_data['cookie']:
            await query.edit_message_text("🚪 **Авторизация**\n\nПожалуйста, воспользуйтесь входом.", reply_markup=get_logout_keyboard(), parse_mode="Markdown")
            return

        hw_text, _ = fetch_homework_data(user_data['cookie'], target_date)
        try:
            await query.edit_message_text(text=hw_text, reply_markup=get_main_keyboard(target_date), parse_mode="Markdown")
        except Exception:
            pass

    elif data_code == "logout_menu":
        user_data['cookie'] = None
        await query.edit_message_text("🚪 **Вы вышли из аккаунта**", reply_markup=get_logout_keyboard(), parse_mode="Markdown")

    elif data_code == "enter_cookie":
        has_last = user_data['last_cookie'] is not None
        await query.edit_message_text("🔑 **Ввод Cookie**\n\nОтправьте Cookie сообщением:", reply_markup=get_cookie_prompt_keyboard(has_last), parse_mode="Markdown")

    elif data_code == "load_last_cookie":
        if user_data['last_cookie']:
            user_data['cookie'] = user_data['last_cookie']
            tomorrow_str = format_date(get_tomorrow_date())
            user_data['active_date'] = tomorrow_str
            hw_text, _ = fetch_homework_data(user_data['cookie'], tomorrow_str)
            await query.edit_message_text(text=hw_text, reply_markup=get_main_keyboard(tomorrow_str), parse_mode="Markdown")

    elif data_code == "help_info":
        help_text = "❓ Нажмите **'Войти через Госуслуги'**, отправьте логин/пароль и SMS-код!"
        await query.edit_message_text(text=help_text, reply_markup=get_back_home_keyboard(), parse_mode="Markdown")

    elif data_code.startswith("files_"):
        target_date = data_code.replace("files_", "")
        if not user_data['cookie']:
            await query.edit_message_text("🚪 **Авторизация**", reply_markup=get_logout_keyboard(), parse_mode="Markdown")
            return
        _, files = fetch_homework_data(user_data['cookie'], target_date)
        files_text = f"📁 **Прикреплённые файлы на {target_date}:**\n\nНайдено: {len(files)} шт." if files else f"📁 Файлов нет."
        await query.edit_message_text(text=files_text, reply_markup=get_files_keyboard(files, target_date), parse_mode="Markdown")

    elif data_code == "settings":
        current_hrs = user_data.get('interval', 1)
        await query.edit_message_text(text=f"⚙️ **Интервал автообновления:** {current_hrs} ч.", reply_markup=get_settings_keyboard(current_hrs), parse_mode="Markdown")

    elif data_code.startswith("set_interval_"):
        try:
            new_val = int(data_code.replace("set_interval_", ""))
            if 1 <= new_val <= 24:
                user_data['interval'] = new_val
                await query.edit_message_text(text=f"⚙️ **Интервал автообновления:** {new_val} ч.", reply_markup=get_settings_keyboard(new_val), parse_mode="Markdown")
        except ValueError:
            pass

    elif data_code == "home_refresh":
        tomorrow_str = format_date(get_tomorrow_date())
        user_data['active_date'] = tomorrow_str
        if user_data['cookie']:
            hw_text, _ = fetch_homework_data(user_data['cookie'], tomorrow_str)
            await query.edit_message_text(text=hw_text, reply_markup=get_main_keyboard(tomorrow_str), parse_mode="Markdown")

async def start_bot() -> None:
    if not TELEGRAM_TOKEN:
        print("Ошибка: TELEGRAM_TOKEN не задан!")
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
