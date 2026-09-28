import os
import sys
import logging
import asyncio
import threading
from datetime import datetime, timedelta
from flask import Flask
import requests
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)
from playwright.async_api import async_playwright

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
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

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- ХРАНИЛИЩЕ СОСТОЯНИЙ И СЕССИЙ ---
# user_sessions[user_id] = {
#     'cookie': str, 'last_cookie': str, 'interval': int, 'active_date': str,
#     'status': str, 'playwright': ..., 'browser': ..., 'page': ...
# }
user_sessions = {}

async def close_playwright_session(user_id: int):
    """Безопасное закрытие браузера Playwright для конкретного пользователя"""
    session = user_sessions.get(user_id)
    if session:
        try:
            if session.get('browser'):
                await session['browser'].close()
            if session.get('playwright'):
                await session['playwright'].stop()
        except Exception as e:
            logger.error(f"Ошибка при закрытии Playwright сессии {user_id}: {e}")
        finally:
            session['playwright'] = None
            session['browser'] = None
            session['page'] = None
            session['status'] = None

def get_tomorrow_date() -> datetime:
    return datetime.now() + timedelta(days=1)

def format_date(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")

def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%d.%m.%Y")

def get_lessons_word(count: int) -> str:
    """Склонение слова 'урок' в зависимости от количества"""
    if count % 10 == 1 and count % 100 != 11:
        return f"Надо сделать {count} урок"
    elif 2 <= count % 10 <= 4 and not (12 <= count % 100 <= 14):
        return f"Надо сделать {count} урока"
    else:
        return f"Надо сделать {count} уроков"

# --- КЛАВИАТУРЫ ---

def get_main_keyboard(date_str: str) -> InlineKeyboardMarkup:
    """Главная клавиатура из 6 кнопок"""
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
    """Клавиатура входа (после выхода)"""
    keyboard = [
        [InlineKeyboardButton("🔑 Войти через Госуслуги", callback_data="login_gosuslugi")],
        [InlineKeyboardButton("🍪 Ввести Cookie вручную", callback_data="enter_cookie")],
        [InlineKeyboardButton("❓ Помощь", callback_data="help_info")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены при вводе данных Госуслуг"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel_auth")]])

def get_cookie_prompt_keyboard(has_last_cookie: bool) -> InlineKeyboardMarkup:
    """Клавиатура при запросе ввода Cookie"""
    row = []
    if has_last_cookie:
        row.append(InlineKeyboardButton("📥 Загрузить последний", callback_data="load_last_cookie"))
    row.append(InlineKeyboardButton("🏠 Домой", callback_data="home"))
    return InlineKeyboardMarkup([row])

def get_files_keyboard(files: list, date_str: str) -> InlineKeyboardMarkup:
    """Кнопки скачивания файлов + Домой"""
    keyboard = []
    for idx, (title, url) in enumerate(files, 1):
        btn_label = f"📥 Скачать: {title[:20]}" if len(title) > 20 else f"📥 Скачать: {title}"
        keyboard.append([InlineKeyboardButton(btn_label, url=url)])
    keyboard.append([InlineKeyboardButton("🏠 Домой", callback_data=f"hw_{date_str}")])
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard(hours: int) -> InlineKeyboardMarkup:
    """Клавиатура выбора интервала обновления"""
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

# --- ПАРСИНГ ДНЕВНИКА С НОВЫМ ФОРМАТИРОВАНИЕМ ---

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
                        # Вёрстка каждого предмета с цитированием
                        hw_items.append(f">📌 **{subject}:** {clean_hw}")

        if hw_items:
            count = len(hw_items)
            lessons_info = get_lessons_word(count)
            
            # Собираем красивое сообщение с цитатами
            result_text = f"📋 **Домашнее задание на {date_str}:**\n\n"
            result_text += "\n>\n".join(hw_items)
            result_text += f"\n\n**{lessons_info}**"
            return result_text, files
        else:
            return f"ℹ️ **Домашнее задание на {date_str}:**\n\n> Заданий не найдено (или нет уроков).\n\n**Надо сделать 0 уроков**", files

    except Exception as e:
        return f"⚠️ **Ошибка соединения**\n\nНе удалось загрузить данные: {e}", []

# --- АВТОРИЗАЦИЯ ЧЕРЕЗ ГОСУСЛУГИ (PLAYWRIGHT) ---

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

        session.update({'playwright': pw, 'browser': browser, 'page': page})

        await status_msg.edit_text("🔗 Загружаю форму входа Госуслуг...", reply_markup=get_cancel_keyboard())
        await page.goto("https://login.dnevnik.ru/login/gosuslugi", timeout=30000, wait_until="domcontentloaded")

        await status_msg.edit_text("✍️ Ввожу логин и пароль...", reply_markup=get_cancel_keyboard())

        # Заполнение полей логина и пароля
        await page.fill('input[id="login"]', login)
        await page.fill('input[id="password"]', password)
        await page.click('button[type="submit"]')

        await asyncio.sleep(4)

        # Проверка 2FA (SMS-кода)
        if "code" in page.url or await page.is_visible('input[name="code"]'):
            session['status'] = 'WAITING_SMS'
            await status_msg.edit_text(
                "📲 **Введите SMS-код**, отправленный Госуслугами:",
                parse_mode="Markdown",
                reply_markup=get_cancel_keyboard()
            )
        else:
            await finalize_gosuslugi_login(user_id, status_msg)

    except Exception as e:
        logger.error(f"Ошибка входа через Госуслуги: {e}")
        await status_msg.edit_text(
            f"❌ Ошибка авторизации Госуслуг: {e}\n\nПопробуйте ещё раз или воспользуйтесь вводом Cookie.",
            reply_markup=get_logout_keyboard()
        )
        await close_playwright_session(user_id)

async def process_gosuslugi_sms(user_id: int, sms_code: str, status_msg, context: ContextTypes.DEFAULT_TYPE):
    session = user_sessions.get(user_id)
    if not session or not session.get('page'):
        await status_msg.edit_text("❌ Сессия была сброшена. Начните заново.", reply_markup=get_logout_keyboard())
        return

    page = session['page']
    try:
        await page.fill('input[name="code"]', sms_code)
        await page.click('button[type="submit"]')
        await asyncio.sleep(5)

        await finalize_gosuslugi_login(user_id, status_msg)

    except Exception as e:
        logger.error(f"Ошибка ввода SMS: {e}")
        await status_msg.edit_text(
            f"❌ Неверный код или ошибка отправки: {e}",
            reply_markup=get_cancel_keyboard()
        )
        session['status'] = 'WAITING_SMS'

async def finalize_gosuslugi_login(user_id: int, status_msg):
    session = user_sessions.get(user_id)
    if not session or not session.get('page'):
        return

    page = session['page']
    try:
        cookies = await page.context.cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])

        if not cookie_str or len(cookie_str) < 10:
            await status_msg.edit_text(
                "❌ Не удалось извлечь Cookie после входа. Попробуйте войти вручную.",
                reply_markup=get_logout_keyboard()
            )
            return

        session['cookie'] = cookie_str
        session['last_cookie'] = cookie_str
        tomorrow_str = format_date(get_tomorrow_date())
        session['active_date'] = tomorrow_str

        hw_text, _ = fetch_homework_data(cookie_str, tomorrow_str)

        await status_msg.edit_text(
            text=hw_text,
            reply_markup=get_main_keyboard(tomorrow_str),
            parse_mode="Markdown"
        )
    finally:
        await close_playwright_session(user_id)

# --- ОБРАБОТЧИКИ ТЕЛЕГРАМ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    await delete_message_safe(context, chat_id, update.message.message_id)
    await close_playwright_session(user_id)

    user_data = user_sessions.setdefault(user_id, {
        'cookie': None,
        'last_cookie': None,
        'interval': 1,
        'active_date': format_date(get_tomorrow_date()),
        'status': None
    })

    if user_data['cookie']:
        date_str = user_data['active_date']
        text, _ = fetch_homework_data(user_data['cookie'], date_str)
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=get_main_keyboard(date_str),
            parse_mode="Markdown"
        )
        context.user_data['main_msg_id'] = msg.message_id
    else:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text="👋 **Привет! Я бот для работы с Дневник.ру.**\n\nВыберите способ авторизации:",
            reply_markup=get_logout_keyboard(),
            parse_mode="Markdown"
        )
        context.user_data['main_msg_id'] = msg.message_id

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0
    text = update.message.text.strip()

    await delete_message_safe(context, chat_id, update.message.message_id)

    user_data = user_sessions.setdefault(user_id, {
        'cookie': None,
        'last_cookie': None,
        'interval': 1,
        'active_date': format_date(get_tomorrow_date()),
        'status': None
    })

    status = user_data.get('status')

    # ШАГ 1: Ввод логина и пароля Госуслуг
    if status == 'WAITING_CREDENTIALS':
        lines = text.split('\n')
        if len(lines) < 2:
            status_msg = await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ **Пришлите данные в две строчки:**\n\nПервая строчка — Логин (телефон/email)\nВторая строчка — Пароль",
                reply_markup=get_cancel_keyboard(),
                parse_mode="Markdown"
            )
            return

        login = lines[0].strip()
        password = lines[1].strip()

        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="🔄 **Запускаю браузер Госуслуг...**",
            reply_markup=get_cancel_keyboard(),
            parse_mode="Markdown"
        )
        user_data['status'] = 'PROCESSING'

        asyncio.create_task(process_gosuslugi_login(user_id, login, password, status_msg, context))
        return

    # ШАГ 2: Ввод 2FA / SMS-кода
    elif status == 'WAITING_SMS':
        sms_code = text.strip()
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="⏳ **Отправляю SMS-код...**",
            reply_markup=get_cancel_keyboard(),
            parse_mode="Markdown"
        )
        user_data['status'] = 'PROCESSING'

        asyncio.create_task(process_gosuslugi_sms(user_id, sms_code, status_msg, context))
        return

    # ШАГ 3: Обычный ввод Cookie
    elif status == 'WAITING_COOKIE' or not user_data['cookie']:
        user_data['cookie'] = text
        user_data['last_cookie'] = text
        user_data['status'] = None
        tomorrow_str = format_date(get_tomorrow_date())
        user_data['active_date'] = tomorrow_str

        hw_text, _ = fetch_homework_data(text, tomorrow_str)

        target_msg_id = context.user_data.get('main_msg_id')
        if target_msg_id:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=target_msg_id,
                    text=hw_text,
                    reply_markup=get_main_keyboard(tomorrow_str),
                    parse_mode="Markdown"
                )
                return
            except Exception:
                pass

        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=hw_text,
            reply_markup=get_main_keyboard(tomorrow_str),
            parse_mode="Markdown"
        )
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
        'cookie': None,
        'last_cookie': None,
        'interval': 1,
        'active_date': format_date(get_tomorrow_date()),
        'status': None
    })

    context.user_data['main_msg_id'] = query.message.message_id

    # --- КНОПКИ АВТОРИЗАЦИИ ГОСУСЛУГ ---
    if data_code == "login_gosuslugi":
        await close_playwright_session(user_id)
        user_data['status'] = 'WAITING_CREDENTIALS'

        await query.edit_message_text(
            text=(
                "🔐 **Авторизация через Госуслуги**\n\n"
                "Пришлите ваши **Логин** (телефон/email) и **Пароль** одним сообщением в две строчки:\n\n"
                "`+79001234567`\n"
                "`ВашПароль`"
            ),
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard()
        )

    elif data_code == "cancel_auth":
        await close_playwright_session(user_id)
        await query.edit_message_text(
            text="❌ Авторизация отменена.\n\nВыберите нужный пункт меню:",
            reply_markup=get_logout_keyboard(),
            parse_mode="Markdown"
        )

    # --- НАВИГАЦИЯ И ГЛАВНАЯ ---
    elif data_code.startswith("hw_") or data_code == "home":
        if data_code.startswith("hw_"):
            target_date = data_code.replace("hw_", "")
            user_data['active_date'] = target_date
        else:
            target_date = user_data['active_date']

        if not user_data['cookie']:
            await query.edit_message_text(
                text="🚪 **Авторизация**\n\nВы вышли из системы. Пожалуйста, авторизуйтесь.",
                reply_markup=get_logout_keyboard(),
                parse_mode="Markdown"
            )
            return

        hw_text, _ = fetch_homework_data(user_data['cookie'], target_date)
        try:
            await query.edit_message_text(
                text=hw_text,
                reply_markup=get_main_keyboard(target_date),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    # --- ВЫХОД И КУКИ ---
    elif data_code == "logout_menu":
        user_data['cookie'] = None
        await close_playwright_session(user_id)
        await query.edit_message_text(
            text="🚪 **Вы вышли из аккаунта**\n\nВыберите способ входа:",
            reply_markup=get_logout_keyboard(),
            parse_mode="Markdown"
        )

    elif data_code == "enter_cookie":
        await close_playwright_session(user_id)
        user_data['status'] = 'WAITING_COOKIE'
        has_last = user_data['last_cookie'] is not None
        await query.edit_message_text(
            text="🔑 **Ввод Cookie**\n\nОтправьте новую строку Cookie сообщением в этот чат:",
            reply_markup=get_cookie_prompt_keyboard(has_last),
            parse_mode="Markdown"
        )

    elif data_code == "load_last_cookie":
        if user_data['last_cookie']:
            user_data['cookie'] = user_data['last_cookie']
            tomorrow_str = format_date(get_tomorrow_date())
            user_data['active_date'] = tomorrow_str
            hw_text, _ = fetch_homework_data(user_data['cookie'], tomorrow_str)
            await query.edit_message_text(
                text=hw_text,
                reply_markup=get_main_keyboard(tomorrow_str),
                parse_mode="Markdown"
            )

    elif data_code == "help_info":
        help_text = (
            "❓ **Инструкция по получению Cookie:**\n\n"
            "1. Зайдите на сайт **schools.dnevnik.ru** через браузер.\n"
            "2. Нажмите на вашу созданную Закладку-букмарклет.\n"
            "3. Нажмите на красную кнопку для копирования.\n"
            "4. Вставьте скопированный текст прямо в этот чат!"
        )
        await query.edit_message_text(
            text=help_text,
            reply_markup=get_back_home_keyboard(),
            parse_mode="Markdown"
        )

    # --- ФАЙЛЫ ---
    elif data_code.startswith("files_"):
        target_date = data_code.replace("files_", "")
        if not user_data['cookie']:
            await query.edit_message_text(
                text="🚪 **Авторизация**\n\nВы не авторизованы.",
                reply_markup=get_logout_keyboard(),
                parse_mode="Markdown"
            )
            return

        _, files = fetch_homework_data(user_data['cookie'], target_date)

        if files:
            files_text = f"📁 **Прикреплённые файлы на {target_date}:**\n\nНайдено файлов: **{len(files)} шт.** Нажмите на кнопку ниже для скачивания."
        else:
            files_text = f"📁 **Прикреплённые файлы на {target_date}:**\n\nФайлы и вложения на эту дату отсутствуют."

        await query.edit_message_text(
            text=files_text,
            reply_markup=get_files_keyboard(files, target_date),
            parse_mode="Markdown"
        )

    # --- НАСТРОЙКИ ---
    elif data_code == "settings":
        current_hrs = user_data.get('interval', 1)
        text = (
            "⚙️ **Настройки автообновления**\n\n"
            f"Текущий интервал обновления: **{current_hrs} ч.**\n"
            "Укажите интервал от 1 до 24 часов с помощью кнопок ниже:"
        )
        await query.edit_message_text(
            text=text,
            reply_markup=get_settings_keyboard(current_hrs),
            parse_mode="Markdown"
        )

    elif data_code.startswith("set_interval_"):
        try:
            new_val = int(data_code.replace("set_interval_", ""))
            if 1 <= new_val <= 24:
                user_data['interval'] = new_val
                text = (
                    "⚙️ **Настройки автообновления**\n\n"
                    f"Текущий интервал обновления: **{new_val} ч.**\n"
                    "Укажите интервал от 1 до 24 часов с помощью кнопок ниже:"
                )
                await query.edit_message_text(
                    text=text,
                    reply_markup=get_settings_keyboard(new_val),
                    parse_mode="Markdown"
                )
        except ValueError:
            pass

    elif data_code == "home_refresh":
        tomorrow_str = format_date(get_tomorrow_date())
        user_data['active_date'] = tomorrow_str
        
        if user_data['cookie']:
            hw_text, _ = fetch_homework_data(user_data['cookie'], tomorrow_str)
            await query.edit_message_text(
                text=hw_text,
                reply_markup=get_main_keyboard(tomorrow_str),
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text(
                text="🚪 **Авторизация**\n\nВы не авторизованы. Пожалуйста, войдите.",
                reply_markup=get_logout_keyboard(),
                parse_mode="Markdown"
            )

# --- ЗАПУСК ---

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("❌ ОШИБКА: Переменная BOT_TOKEN не найдена!", flush=True)
        sys.exit(1)

    print("🤖 Бот успешно запущен!", flush=True)

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()

if __name__ == "__main__":
    main()
